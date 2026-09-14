"""Transport abstraction for talking Modbus holding registers to an inverter.

The concrete implementation today is Solarman V5 over TCP 8899; RS485 and plain
Modbus-TCP can be added later behind the same Protocol.

THIS MODULE MUST NOT IMPORT homeassistant.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """Base class for every transport failure."""


class TransportConnectionError(TransportError):
    """The socket could not be opened, or died mid-request.

    Base class for the two specific causes below. Code that only needs to
    know "the connection is unusable, back off and retry" can keep catching
    this base class; code that needs to react differently to "logger busy"
    vs. "no reply" should catch the subclasses instead.
    """


class TransportBusyError(TransportConnectionError):
    """Another client already holds the logger's one TCP slot.

    Each Solarman V5 logger accepts exactly ONE TCP client at a time. This is
    pysolarmanv5's NoSocketAvailableError (or an equivalent connection-refused
    at the socket level) made visible as its own type instead of being
    flattened into a generic connection failure -- "someone else has the
    logger" (a leftover justice_watch.py, a second config entry pointed at
    the same IP, Solarman's own cloud client) is the single most common
    failure mode of this hardware and callers (coordinator backoff, the
    probe's retry loop) need to be able to tell it apart from a timeout.
    """


class TransportTimeoutError(TransportConnectionError):
    """A connection attempt or request exceeded SOCKET_TIMEOUT with no reply.

    Distinct from TransportBusyError: the socket was ours to use, the device
    (or the network path to it) simply never answered in time.
    """


class TransportProtocolError(TransportError):
    """A malformed or unexpected frame came back (V5FrameError, empty reply)."""


class UnsupportedRegisterError(TransportError):
    """The device answered IllegalDataAddress: this block does not exist.

    This is the signal the probe uses to mark a block unsupported.
    """


class InvalidRegisterValueError(TransportError):
    """The device answered IllegalDataValue: the write was refused."""


@runtime_checkable
class Transport(Protocol):
    """Minimal async Modbus holding-register transport.

    Connection-ownership contract: the CALLER owns connect() and close(), not
    the transport. An implementation MAY silently re-establish a socket that
    dropped underneath a read/write the caller itself initiated (e.g. a TCP
    connection the logger reset mid-poll) -- that is a private recovery
    detail. It MUST NOT open a new connection on its own after the caller has
    explicitly called close(). close() is how a caller frees the logger's one
    TCP slot for another tool (the "pause switch"); a transport that
    reconnects on its own after that silently steals the logger back and
    defeats the entire point of pausing. Concretely: calling read_holding or
    write_holding after an explicit close(), with no intervening connect(),
    must raise TransportConnectionError rather than opening a socket.
    """

    @property
    def connected(self) -> bool:
        """True when a usable connection is open."""

    async def connect(self) -> None:
        """Open the connection. Raises TransportConnectionError on failure."""

    async def close(self) -> None:
        """Close the connection. Must be safe to call when already closed.

        After close() returns, `connected` is False and stays False until the
        caller calls connect() again -- see the class docstring's connection-
        ownership contract.
        """

    async def read_holding(self, addr: int, count: int) -> list[int]:
        """Read `count` holding registers starting at `addr`.

        Raises TransportConnectionError (or a subclass) if called while not
        connected -- either before the first connect() or after an explicit
        close() with no intervening connect(). Never partially fills the
        result: either every register in [addr, addr + count) comes back, or
        the call raises.
        """

    async def write_holding(self, addr: int, value: int) -> None:
        """Write a single holding register.

        Raises TransportConnectionError (or a subclass) if called while not
        connected, under the same rule as read_holding. A successful return
        means the device ACCEPTED the write request -- it is not proof the
        value stuck. Callers that need certainty must read_holding the same
        register back afterward; this method never does that on their
        behalf.
        """

    def atomic(self) -> AbstractAsyncContextManager[AtomicOperations]:
        """Hold the transport exclusively for more than one operation.

        A plain `read_holding`/`write_holding` each acquire the transport's
        lock for a single operation and release it before returning -- that
        is enough for one-shot calls, but NOT enough for a caller that needs
        a write followed by its own read-back to happen as one unit (e.g. the
        repo-wide "every write re-reads and raises if it differs" invariant;
        the compare-and-raise itself still belongs to the caller, never to
        write_holding). `atomic()` is that unit:

            async with transport.atomic() as t:
                await t.write_holding(addr, value)
                readback = await t.read_holding(addr, 1)
                if readback != [value]:
                    raise InvalidRegisterValueError("write did not stick")

        Implementations MUST:
        - serialise `atomic()` against read_holding/write_holding AND against
          a second, concurrent `atomic()` -- it is the SAME lock, not an
          independent one that would let something else interleave mid-block;
        - never leak the lock, whether the block exits normally, raises, or
          is cancelled mid-flight;
        - invalidate the yielded object once the block exits: calling its
          read_holding/write_holding afterward MUST raise RuntimeError rather
          than silently operating outside the lock (a caller that stashes the
          handle and uses it later is exactly the bug this requirement
          exists to catch).

        Do NOT call the transport's own public read_holding/write_holding
        from inside an `atomic()` block on the same task -- both acquire the
        same non-reentrant lock this context manager already holds, so doing
        so deadlocks. Use the object this method yields instead; it exposes
        read_holding/write_holding that reuse the lock already held.
        """


@runtime_checkable
class AtomicOperations(Protocol):
    """What `atomic()` yields: read/write access for the lifetime of the
    `async with transport.atomic() as t:` block that produced it.

    Using `t` after that block has exited must raise RuntimeError -- see
    `Transport.atomic`'s docstring.
    """

    async def read_holding(self, addr: int, count: int) -> list[int]:
        """Same contract as Transport.read_holding, without re-acquiring the
        lock -- the enclosing atomic() block already holds it.
        """

    async def write_holding(self, addr: int, value: int) -> None:
        """Same contract as Transport.write_holding, without re-acquiring the
        lock -- the enclosing atomic() block already holds it.
        """

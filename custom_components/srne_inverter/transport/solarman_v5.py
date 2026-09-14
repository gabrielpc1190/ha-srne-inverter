"""Solarman V5 (LSW-5 WiFi logger, TCP 8899) transport.

One logger accepts exactly ONE TCP client, so a single connection is held open
and every request is serialised through a private asyncio.Lock.

THIS MODULE MUST NOT IMPORT homeassistant.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from pysolarmanv5 import NoSocketAvailableError, PySolarmanV5Async, V5FrameError
from umodbus.exceptions import (
    IllegalDataAddressError,
    IllegalDataValueError,
    ModbusError,
)

from .base import (
    InvalidRegisterValueError,
    TransportBusyError,
    TransportConnectionError,
    TransportProtocolError,
    TransportTimeoutError,
    UnsupportedRegisterError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8899
DEFAULT_TIMEOUT = 10.0

# Bound for _discard_client()'s best-effort disconnect of a client we are
# abandoning (a dead stale client found by connect(), a client whose connect()
# attempt failed, or the client close() is releasing). Deliberately NOT tied
# to `self.timeout` -- that value governs how long we wait for the DEVICE to
# answer a request, whereas this one only bounds how long we wait for the
# LIBRARY's socket teardown (writer.write/drain/close/wait_closed) to finish
# on a socket that may be half-open and sitting on TCP retransmits. Without
# this, a stalled disconnect() could hang connect()'s recovery path or
# close()'s pause-switch path indefinitely -- the coordinator's backoff loop
# would never receive the error it needs in order to back off at all.
DISCARD_TIMEOUT = 2.0

_Phase = Literal["connect", "request"]


class SolarmanV5Transport:
    """Async Modbus-over-Solarman-V5 transport for a single logger.

    Connection-ownership contract (see transport.base.Transport): the CALLER
    owns connect() and close(). This class never opens a socket except from
    its own public connect() method -- read_holding/write_holding only ever
    use the client already stored in self._client, or raise
    TransportConnectionError if there is none. That is what makes "MUST NOT
    reconnect after an explicit close()" trivially true here: there is no
    code path, anywhere but connect(), that can create a new client.

    Locking: the lock is PRIVATE. read_holding/write_holding each acquire it
    for a single operation. Do not publish the raw lock for callers to
    bracket a write+read-back with -- asyncio.Lock is not reentrant, and a
    caller doing `async with transport.lock: await transport.write_holding(...);
    await transport.read_holding(...)` would deadlock forever, because both
    of those methods try to acquire the very lock the caller is already
    holding. Callers that need to bracket a write followed by its read-back
    atomically (the transport-level half of the repo-wide "every write
    re-reads and raises if it differs" invariant -- the compare-and-raise
    itself still belongs to the caller, per the Transport Protocol's
    docstring) must use `atomic()` instead, which acquires the lock once and
    yields un-locked read/write methods for the duration of the block.
    close() also takes this lock, so a pause request waits for any in-flight
    operation to finish on its own terms instead of yanking the connection
    out from under it (see close()'s docstring).
    """

    def __init__(
        self,
        host: str,
        serial: int,
        *,
        port: int = DEFAULT_PORT,
        slave_id: int = 1,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.serial = serial
        self.port = port
        self.slave_id = slave_id
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._client: PySolarmanV5Async | None = None

    @property
    def connected(self) -> bool:
        """True when a client exists AND nothing indicates its background
        reader loop has died underneath us.

        Two independent live signals, because pysolarmanv5's async client
        gives us two different corpses depending on how the reader died:

        1. `.reader`/`.writer` are nulled by `_conn_keeper`'s tail -- but only
           when its `while True` loop actually RETURNS (a caught
           `ConnectionResetError`, or a clean EOF). Both attributes stay
           non-None while that happens.
        2. Any OTHER exception from `reader.read()` (ETIMEDOUT after an AP
           reboot or a NAT-table eviction, ConnectionAbortedError, ...) is
           NOT caught by `_conn_keeper`'s narrow `except ConnectionResetError`
           -- it propagates straight out of the loop, so the tail that nulls
           `.reader`/`.writer` never runs, but the task itself is `done()`
           (with that exception set). Missing this second signal is exactly
           how a session can die from a timeout-shaped cause and still read
           as connected forever, permanently defeating connect()'s own
           dead-session recovery below.

        Checking both means a session that died in the background is
        reported as disconnected immediately, under either failure shape,
        without waiting for a caller to attempt (and fail) a read or write
        first.
        """
        if self._client is None or self._client.reader is None:
            return False
        reader_task = self._client.reader_task
        if reader_task is not None and reader_task.done():
            return False
        return True

    async def connect(self) -> None:
        """Open the connection, or rebuild it if the session died silently.

        Takes the same private lock as read_holding/write_holding/close()/
        atomic() for its ENTIRE body -- not just the discard steps -- so that
        a connect() racing a close() (a manual pause landing mid-reconnect) or
        two overlapping connect() calls (a reload racing a backoff retry)
        cannot interleave. Without this, a close() that finds `_client is
        None` while a connect() is still in flight would return immediately,
        and the in-flight connect() would then assign its client afterward --
        leaving the logger held after an explicit pause, which is exactly the
        contract this transport exists to guarantee never happens. Same lock
        also means two concurrent connect() calls cannot both pass the
        `self.connected` check and each open their own socket to a device with
        exactly one slot, leaking the first.
        """
        async with self._lock:
            if self.connected:
                return
            if self._client is not None:
                # We still hold a reference, but the live check above says
                # the session is dead (the reader task detected it in the
                # background, or died from an uncaught read exception).
                # Discard it before building a replacement -- otherwise its
                # abandoned socket, if still technically open, would sit on
                # the logger's one TCP slot alongside the new one.
                await self._discard_client(self._client)
                self._client = None
            client = PySolarmanV5Async(
                self.host,
                self.serial,
                port=self.port,
                mb_slave_id=self.slave_id,
                socket_timeout=self.timeout,
                auto_reconnect=False,
                verbose=False,
            )
            try:
                async with asyncio.timeout(self.timeout):
                    await client.connect()
            except Exception as err:
                # Our own deadline races the library's own `wait_for` and
                # always fires first (both are set to `self.timeout`), so
                # most connect failures reach us as a plain TimeoutError, not
                # wrapped by the library at all. But if the library's
                # connect() completed (or failed with something other than a
                # timeout) right underneath our cancellation, `client` may
                # already hold a live socket -- discard it (bounded by its
                # own DISCARD_TIMEOUT, well inside this method's lock scope)
                # so it does not sit open, occupying the logger's one TCP
                # slot until garbage collection.
                await self._discard_client(client)
                raise self._translate(err, phase="connect") from err
            self._client = client
            _LOGGER.debug("Connected to logger %s (%s)", self.serial, self.host)

    async def close(self) -> None:
        """Close the connection. Waits for any in-flight read/write first.

        close() takes the same private lock read_holding/write_holding/
        connect()/atomic() use, so a pause request that arrives mid-poll
        does not cancel the in-flight operation's reader task out from under
        it (which -- with pysolarmanv5's async client -- would otherwise
        leave that operation parked on a queue nothing will ever feed, so it
        silently eats the full `timeout` and reports itself as a
        TransportTimeoutError, even though the true cause was a deliberate
        pause, not a network fault). Waiting instead means the in-flight
        operation resolves on its own terms -- success or its own genuine
        timeout -- and close() proceeds right after. Do not call close() from
        within an `atomic()` block on the same task; that still deadlocks,
        symmetric to any other "don't hold a lock and then wait on it again"
        rule -- it is not the pattern Finding 1 was about (bracketing a
        write+read-back), and `atomic()`'s own docstring says as much.

        `self._client` is nulled only AFTER `_discard_client` returns, not
        before -- if this coroutine is cancelled while the discard is still
        in flight, the client stays referenced by `self._client` rather than
        being silently dropped. A dropped-but-still-open socket would be a
        leak nothing could ever retry (connect()'s stale-client discard only
        fires when `self._client is not None`); leaving the reference in
        place means the NEXT connect() or close() call gets another chance to
        finish disconnecting it.
        """
        async with self._lock:
            client = self._client
            if client is None:
                return
            await self._discard_client(client)
            self._client = None

    async def read_holding(self, addr: int, count: int) -> list[int]:
        async with self._lock:
            return await self._read_holding_locked(addr, count)

    async def write_holding(self, addr: int, value: int) -> None:
        async with self._lock:
            await self._write_holding_locked(addr, value)

    @asynccontextmanager
    async def atomic(self) -> AsyncIterator["_LockedOperations"]:
        """Hold the transport exclusively for more than one operation.

        Use this to bracket a write followed by a read-back of the same
        register without releasing the lock in between -- e.g. to honour the
        repo-wide "every write re-reads and raises if it differs" invariant,
        which `write_holding` deliberately never does on the caller's
        behalf (see transport.base.Transport's docstring):

            async with transport.atomic() as t:
                await t.write_holding(addr, value)
                readback = await t.read_holding(addr, 1)
                if readback != [value]:
                    raise InvalidRegisterValueError("write did not stick")

        The object yielded exposes read_holding/write_holding that reuse the
        lock already held by this context manager -- calling the transport's
        own public read_holding/write_holding from inside this block would
        deadlock, since asyncio.Lock is not reentrant.

        The yielded object is invalidated the instant this block exits --
        normally, via an exception, or via cancellation (the `finally` runs
        in all three cases). Stashing it and calling it later would silently
        run outside the lock, which is worse than not having atomic() at
        all: two Modbus requests could end up in flight at once on a client
        whose data_queue holds exactly one response, so one request's answer
        gets delivered to the other's waiter, or gets dropped for a sequence
        mismatch and times out. A late call raises RuntimeError instead.
        """
        async with self._lock:
            session = _LockedOperations(self)
            try:
                yield session
            finally:
                session._invalidate()

    async def _read_holding_locked(self, addr: int, count: int) -> list[int]:
        """read_holding's body, assuming the caller already holds self._lock."""
        client = self._require_client()
        try:
            async with asyncio.timeout(self.timeout):
                return list(
                    await client.read_holding_registers(
                        register_addr=addr, quantity=count
                    )
                )
        except Exception as err:
            raise self._translate(err, phase="request") from err

    async def _write_holding_locked(self, addr: int, value: int) -> None:
        """write_holding's body, assuming the caller already holds self._lock."""
        client = self._require_client()
        try:
            async with asyncio.timeout(self.timeout):
                await client.write_holding_register(register_addr=addr, value=value)
        except Exception as err:
            raise self._translate(err, phase="request") from err

    def _require_client(self) -> PySolarmanV5Async:
        if self._client is None:
            raise TransportConnectionError("transport is not connected")
        return self._client

    @staticmethod
    async def _discard_client(client: PySolarmanV5Async) -> None:
        """Best-effort close of a client we are abandoning without going
        through the public close() (a dead session found by connect(), or a
        connect() attempt we are giving up on). Safe to call on a client
        whose connect() never succeeded -- disconnect() no-ops when its
        reader_task/writer were never set.

        Bounded by DISCARD_TIMEOUT, separately from `self.timeout`:
        disconnect() does `writer.write(b"")`, `await writer.drain()`,
        `writer.close()`, `await writer.wait_closed()` -- on a half-open
        socket those can sit on TCP retransmits for minutes. Without this
        bound, a stalled disconnect() would hang whichever public method
        called this (connect() or close()) for that long, and the
        coordinator's backoff loop would never receive the error it needs in
        order to back off from anything -- it would just stop, silently,
        instead of degrading. Giving up after DISCARD_TIMEOUT and treating
        the client as discarded either way is safe: this is a best-effort
        cleanup, not a correctness requirement, and the exception this
        raises internally on expiry is swallowed by the same `except
        Exception` as any other disconnect failure -- but an externally
        raised CancelledError (the caller's own task being cancelled, not
        this timeout's own deadline) is NOT swallowed here, and must not be:
        see close()'s docstring for why the caller needs to see that.
        """
        try:
            async with asyncio.timeout(DISCARD_TIMEOUT):
                await client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort, never raise from here
            _LOGGER.debug(
                "Ignoring error while discarding a stale client for %s",
                client.address if hasattr(client, "address") else "?",
                exc_info=True,
            )

    def _translate(self, err: Exception, *, phase: _Phase) -> Exception:
        """Map pysolarmanv5 / umodbus / socket errors onto the transport
        taxonomy. Phase-aware: the SAME pysolarmanv5 exception means a
        different thing depending on when it was raised (see
        NoSocketAvailableError below) -- flattening that distinction away by
        call site, not just by exception type, is what made the earlier
        version of this function tell users the opposite of the truth (a
        mistyped IP reported as "another client has the logger"; an actual
        stolen session reported only after a full timeout).

        Order matters for one pair: builtin TimeoutError is an OSError
        subclass on this interpreter (verified on 3.14 -- and true since
        3.10, since asyncio.TimeoutError became an alias of the builtin
        TimeoutError), so the TimeoutError check MUST run before the generic
        OSError branch, or every timeout would be misreported as a plain
        TransportConnectionError instead of the more specific
        TransportTimeoutError the coordinator needs.
        """
        if isinstance(err, IllegalDataAddressError):
            return UnsupportedRegisterError(str(err) or "illegal data address")
        if isinstance(err, IllegalDataValueError):
            return InvalidRegisterValueError(str(err) or "illegal data value")
        if isinstance(err, NoSocketAvailableError):
            return self._translate_no_socket_available(err, phase)
        if isinstance(err, (TimeoutError, asyncio.TimeoutError)):
            return TransportTimeoutError(str(err) or "request timed out")
        if isinstance(err, V5FrameError):
            return TransportProtocolError(str(err) or "malformed V5 frame")
        if isinstance(err, ModbusError):
            # Any other Modbus exception response (wrong function code,
            # wrong slave id, device busy at the Modbus layer, gateway
            # errors): none of these has a dedicated type in our taxonomy,
            # so surface it as a protocol-level problem rather than silently
            # folding it into one of the two specific cases above.
            return TransportProtocolError(f"{type(err).__name__}: {err}")
        if isinstance(err, OSError):
            # Refused or otherwise-failed at the socket level, and NOT a
            # timeout (that case is handled above): connection reset, network
            # unreachable, etc.
            return TransportConnectionError(f"{type(err).__name__}: {err}")
        return TransportProtocolError(f"{type(err).__name__}: {err}")

    def _translate_no_socket_available(
        self, err: NoSocketAvailableError, phase: _Phase
    ) -> Exception:
        """pysolarmanv5's PySolarmanV5Async.connect() wraps EVERY exception
        raised while opening the socket -- refusal, host/network
        unreachable, DNS failure, its own wait_for timeout losing the race
        against ours -- into this one type (pysolarmanv5_async.py's
        connect(), a blanket `except Exception`). None of those connect-phase
        causes mean "someone else has the logger"; they all mean "no session
        was ever established", which is a plain TransportConnectionError.

        The SAME exception type, raised instead from a read/write on a
        connection we ourselves already established, means something
        different: the session is gone from underneath us (the connection's
        AttributeError-on-a-None-writer path once the background reader task
        has died) -- "someone/something else has the logger" (a leftover
        justice_watch.py, a second config entry on the same IP, Solarman's
        own cloud client) is the right story there, so THAT case maps to the
        specific TransportBusyError.

        err.__cause__ carries the real underlying exception (with errno,
        where there is one) from the `raise ... from e` in the library --
        surfaced in the message here instead of discarded, for whichever of
        the two taxonomy types this resolves to.
        """
        detail = self._describe_cause(err)
        if phase == "connect":
            return TransportConnectionError(
                f"could not open a connection to {self.host}:{self.port}: {detail}"
            )
        return TransportBusyError(
            f"established session to {self.host}:{self.port} was lost or"
            f" taken: {detail}"
        )

    @staticmethod
    def _describe_cause(err: Exception) -> str:
        """Render err.__cause__ (set via `raise ... from e` in pysolarmanv5)
        for the message, including errno when the cause has one, instead of
        discarding it -- it is the only thing that distinguishes "refused",
        "unreachable" and "DNS failure" from each other.
        """
        cause = err.__cause__
        if cause is None:
            return str(err) or type(err).__name__
        errno = getattr(cause, "errno", None)
        if errno is not None:
            return f"{type(cause).__name__} (errno {errno}): {cause}"
        return f"{type(cause).__name__}: {cause}"


class _LockedOperations:
    """Read/write access to a SolarmanV5Transport while its lock is already
    held. Returned only by SolarmanV5Transport.atomic(); never construct
    directly. Invalidated by atomic()'s `finally` the instant its `async
    with` block exits -- see AtomicOperations' docstring in transport.base
    for why a stashed, post-block handle must not keep working.
    """

    def __init__(self, transport: SolarmanV5Transport) -> None:
        self._transport = transport
        self._valid = True

    def _invalidate(self) -> None:
        self._valid = False

    def _check_valid(self) -> None:
        if not self._valid:
            raise RuntimeError(
                "this atomic() handle is no longer valid -- it can only be "
                "used inside the `async with transport.atomic() as t:` "
                "block that produced it; the block has already exited, and "
                "using the handle afterward would run outside the lock "
                "atomic() exists to provide"
            )

    async def read_holding(self, addr: int, count: int) -> list[int]:
        self._check_valid()
        return await self._transport._read_holding_locked(addr, count)

    async def write_holding(self, addr: int, value: int) -> None:
        self._check_valid()
        await self._transport._write_holding_locked(addr, value)

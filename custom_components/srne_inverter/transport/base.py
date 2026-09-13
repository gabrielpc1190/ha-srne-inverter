"""Transport abstraction for talking Modbus holding registers to an inverter.

The concrete implementation today is Solarman V5 over TCP 8899; RS485 and plain
Modbus-TCP can be added later behind the same Protocol.

THIS MODULE MUST NOT IMPORT homeassistant.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """Base class for every transport failure."""


class TransportConnectionError(TransportError):
    """The socket could not be opened, or died mid-request.

    Includes the logger's "one client only" refusal (NoSocketAvailableError)
    and request timeouts.
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
    """Minimal async Modbus holding-register transport."""

    @property
    def connected(self) -> bool:
        """True when a usable connection is open."""

    async def connect(self) -> None:
        """Open the connection. Raises TransportConnectionError on failure."""

    async def close(self) -> None:
        """Close the connection. Must be safe to call when already closed."""

    async def read_holding(self, addr: int, count: int) -> list[int]:
        """Read `count` holding registers starting at `addr`."""

    async def write_holding(self, addr: int, value: int) -> None:
        """Write a single holding register."""

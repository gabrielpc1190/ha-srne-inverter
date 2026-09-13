"""In-memory transport serving the recorded Casa Justice registers.

Mirrors the real firmware's behaviour: addresses outside the verified blocks
raise UnsupportedRegisterError, and the registers the firmware refuses to write
raise InvalidRegisterValueError.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportConnectionError,
    UnsupportedRegisterError,
)

# Verified absent on firmware V8.18.006 (Casa Justice, 2026-09-12).
DEFAULT_UNSUPPORTED: tuple[range, ...] = (
    range(0x0112, 0x0200),
    range(0x0240, 0xE000),
    range(0xE030, 0xE200),
    range(0xE21F, 0xF000),
)

# Writes the firmware answers IllegalDataValue to.
WRITE_REJECTED: frozenset[int] = frozenset({0xE20F, 0xE20B, 0xE21D, 0xE039})


class FakeTransport:
    """Test double implementing the Transport protocol."""

    def __init__(
        self,
        registers: dict[int, int],
        *,
        unsupported: Iterable[range] = DEFAULT_UNSUPPORTED,
        fail_connect: bool = False,
        read_errors: Sequence[Exception | None] | None = None,
    ) -> None:
        self.registers = dict(registers)
        self.unsupported = tuple(unsupported)
        self.fail_connect = fail_connect
        self._read_errors = list(read_errors or [])
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int]] = []
        self.connect_count = 0
        self.close_count = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_count += 1
        if self.fail_connect:
            raise TransportConnectionError("fake: cannot open connection")
        self._connected = True

    async def close(self) -> None:
        self.close_count += 1
        self._connected = False

    def _next_error(self) -> Exception | None:
        if not self._read_errors:
            return None
        return self._read_errors.pop(0)

    async def read_holding(self, addr: int, count: int) -> list[int]:
        self.reads.append((addr, count))
        error = self._next_error()
        if error is not None:
            raise error
        for offset in range(count):
            address = addr + offset
            if any(address in span for span in self.unsupported):
                raise UnsupportedRegisterError(f"fake: 0x{address:04X} absent")
            if address not in self.registers:
                raise UnsupportedRegisterError(
                    f"fake: 0x{address:04X} not in recorded capture"
                )
        return [self.registers[addr + i] for i in range(count)]

    async def write_holding(self, addr: int, value: int) -> None:
        self.writes.append((addr, value))
        if addr in WRITE_REJECTED:
            raise InvalidRegisterValueError(f"fake: 0x{addr:04X} refuses writes")
        if any(addr in span for span in self.unsupported):
            raise UnsupportedRegisterError(f"fake: 0x{addr:04X} absent")
        self.registers[addr] = value

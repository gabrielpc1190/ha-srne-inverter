"""In-memory transport serving the recorded Casa Justice registers.

Mirrors the real firmware's behaviour: addresses the device does not have
raise UnsupportedRegisterError, and the registers the firmware refuses to
write raise InvalidRegisterValueError.

Sharp edge this module exists specifically to avoid: a hole in what we
happened to CAPTURE is not the same claim as "the device does not have this
register". Only addresses in `unsupported` (a verified-absent set, derived
from the design spec, not from what the fixture happens to contain) raise
UnsupportedRegisterError. An address that is simply missing from the
`registers` dict -- and not declared unsupported -- raises `LookupError`
instead: that is the harness telling you its own fixture is incomplete, and
it must never be read as a claim about the device. See
`tests.conftest.load_justice_registers_synthetic_complete` for a registers
dict with no such holes (at the cost of synthetic filler values).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportConnectionError,
    UnsupportedRegisterError,
)

# Verified absent on firmware V8.18.006 (Casa Justice inverter 1; design spec
# docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md
# Section 3). Each range's upper bound is capped at the address where the
# next block registers.BLOCKS declares PRESENT begins -- an absence claim
# must never extend past a block we know exists. Anywhere not covered by one
# of these ranges (and not in `registers`) is UNKNOWN, not absent -- see the
# module docstring; do not add a range here "to be safe".
DEFAULT_UNSUPPORTED: tuple[range, ...] = (
    # After "battery" (0x0100-0x010E); "faults" (0x0200) is the next present
    # block. 0x0112 itself answered IllegalDataAddress on the real logger.
    range(0x0112, 0x0200),
    # After "control_high" (0xE200-0xE21E); "meter" (0xF02C) is next.
    # 0xE21F itself answered IllegalDataAddress on the real logger.
    range(0xE21F, 0xF02C),
    # Inside the settings_high/config area. Starts at 0xE03A, NOT 0xE030:
    # 0xE039 is deliberately excluded from this range because it is
    # write-rejected (see WRITE_REJECTED below), which is only a coherent
    # firmware answer for an address that EXISTS -- it must never also be
    # claimed absent here, or the fake could answer IllegalDataAddress to a
    # read and IllegalDataValue to a write at the same address, which no
    # real Modbus device can do.
    # Bounded at 0xE100, NOT extended to 0xE200: tests/fixtures/
    # justice_inv1_blocks.json recorded real, non-zero values across
    # 0xE100-0xE109, 0xE116-0xE120 and 0xE121-0xE12F (its "config_pre" /
    # "config" / "config_post" capture blocks) -- direct evidence against
    # marking that span absent, even though registers.py does not (yet)
    # model it as one of its 10 BLOCKS.
    range(0xE03A, 0xE100),
)

# Writes the firmware answers IllegalDataValue to (verified at Casa Justice).
# 0xE039 is intentionally NOT in DEFAULT_UNSUPPORTED -- see the comment above.
WRITE_REJECTED: frozenset[int] = frozenset({0xE20F, 0xE20B, 0xE21D, 0xE039})


class FakeTransport:
    """Test double implementing the Transport protocol.

    See the class docstring of `custom_components.srne_inverter.transport
    .base.Transport` for the connection-ownership contract this fake
    enforces: read_holding/write_holding raise TransportConnectionError
    unless a connect() has happened and no close() has happened since.
    """

    def __init__(
        self,
        registers: dict[int, int],
        *,
        unsupported: Iterable[range] = DEFAULT_UNSUPPORTED,
        connect_error: Exception | None = None,
        connect_errors: Sequence[Exception | None] | None = None,
        read_errors: Sequence[Exception | None] | None = None,
        write_errors: Sequence[Exception | None] | None = None,
        no_stick_writes: Iterable[int] = (),
    ) -> None:
        """Build a fake transport.

        Args:
            registers: {address: raw_value}, copied -- mutating the caller's
                dict afterward, or this transport's writes, never cross over.
            unsupported: address ranges that always answer
                UnsupportedRegisterError (device fact), independent of
                `registers`. Defaults to DEFAULT_UNSUPPORTED.
            connect_error: if set, and `connect_errors` is empty/exhausted,
                every connect() call raises this exact exception instance
                instead of succeeding (sticky -- it does not get consumed).
                Typically a TransportConnectionError, TransportBusyError or
                TransportTimeoutError. Kept for the simple "always fails"
                case; see `connect_errors` for "fails N times, then
                succeeds".
            connect_errors: a queue of Exception | None, consumed one entry
                per connect() call regardless of prior attempts -- an
                Exception is raised (and popped), None means "succeed this
                call" (and pops too, so it is consumed like any other
                entry). While this queue has entries, it takes priority over
                `connect_error`; once exhausted, `connect_error` (if any)
                applies to every later call. This is what lets a test model
                this hardware's single most common real failure mode --
                "another client held the logger, then released it" --
                inside a retry loop the test does not control, e.g.
                `connect_errors=[TransportBusyError(...), None]`.
            read_errors: a queue of Exception | None, consumed one entry per
                read_holding call regardless of address -- an Exception is
                raised (and popped), None means "succeed normally this call".
                Once exhausted, every later call succeeds normally.
            write_errors: same queue semantics as read_errors, but for
                write_holding. Use this to simulate value-dependent firmware
                rejections a specific test wants to force (e.g. a write the
                real device would answer IllegalDataValue to). Deliberately
                the ONLY way to model that: FakeTransport never validates a
                written value against registers.py's WriteSpec ranges, and
                must not grow that -- a fake must never implement the logic
                (Task 11/12's write validation) that the tests exist to
                check.
            no_stick_writes: addresses where write_holding is accepted (no
                exception, and the call IS recorded in `writes`) but the
                stored value is never actually updated -- the read-back stays
                whatever it was before. This is how to test the
                "write succeeded but didn't stick" path honestly; see
                test_fake_transport.py for the pattern.
        """
        self.registers = dict(registers)
        self.unsupported = tuple(unsupported)
        self.connect_error = connect_error
        self._connect_errors = list(connect_errors or [])
        self._read_errors = list(read_errors or [])
        self._write_errors = list(write_errors or [])
        self.no_stick_writes = frozenset(no_stick_writes)
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int]] = []
        self.connect_count = 0
        self.close_count = 0
        self._connected = False
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    def _next_connect_error(self) -> Exception | None:
        if self._connect_errors:
            return self._connect_errors.pop(0)
        return self.connect_error

    async def connect(self) -> None:
        self.connect_count += 1
        error = self._next_connect_error()
        if error is not None:
            raise error
        self._connected = True

    async def close(self) -> None:
        self.close_count += 1
        self._connected = False

    def _next_read_error(self) -> Exception | None:
        if not self._read_errors:
            return None
        return self._read_errors.pop(0)

    def _next_write_error(self) -> Exception | None:
        if not self._write_errors:
            return None
        return self._write_errors.pop(0)

    async def read_holding(self, addr: int, count: int) -> list[int]:
        async with self._lock:
            return await self._read_holding_locked(addr, count)

    async def write_holding(self, addr: int, value: int) -> None:
        async with self._lock:
            await self._write_holding_locked(addr, value)

    @asynccontextmanager
    async def atomic(self) -> AsyncIterator["_FakeLockedOperations"]:
        """See transport.base.Transport.atomic -- mirrors the real
        SolarmanV5Transport's atomic() exactly: acquires the SAME lock
        read_holding/write_holding use (so it genuinely serialises against
        them, not against some independent lock that would let a concurrent
        plain read/write slip in mid-block), yields un-locked operations for
        the duration of the block, and invalidates the yielded handle the
        instant the block exits -- normally, via an exception, or via
        cancellation -- so a caller that stashes it and calls it later gets
        RuntimeError instead of silently running outside the lock. This is
        what lets Task 7/12/15 test a write-plus-read-back bracket against
        this fake instead of only against the real transport.
        """
        async with self._lock:
            session = _FakeLockedOperations(self)
            try:
                yield session
            finally:
                session._invalidate()

    async def _read_holding_locked(self, addr: int, count: int) -> list[int]:
        # Not-connected is checked BEFORE recording the attempt: a call the
        # fake refuses outright never touched anything, so it must not show
        # up in `reads` -- otherwise "the pause switch left the log empty"
        # (Task 12) could never be asserted honestly.
        if not self._connected:
            raise TransportConnectionError(
                f"fake: not connected, cannot read 0x{addr:04X}"
            )
        self.reads.append((addr, count))
        error = self._next_read_error()
        if error is not None:
            raise error
        for offset in range(count):
            address = addr + offset
            if any(address in span for span in self.unsupported):
                raise UnsupportedRegisterError(f"fake: 0x{address:04X} absent")
            if address not in self.registers:
                raise LookupError(
                    f"fixture gap: 0x{address:04X} is not in this "
                    "FakeTransport's registers and is not declared "
                    "unsupported either. This is a hole in the recorded "
                    "capture, NOT a claim that the device lacks this "
                    "register -- seed it (e.g. via "
                    "tests.conftest.load_justice_registers_synthetic_complete)"
                    " or add it to `unsupported` if it is verified absent."
                )
        return [self.registers[addr + i] for i in range(count)]

    async def _write_holding_locked(self, addr: int, value: int) -> None:
        # Same rule as read_holding: not-connected is checked BEFORE
        # recording the attempt.
        if not self._connected:
            raise TransportConnectionError(
                f"fake: not connected, cannot write 0x{addr:04X}"
            )
        self.writes.append((addr, value))
        error = self._next_write_error()
        if error is not None:
            raise error
        if addr in WRITE_REJECTED:
            raise InvalidRegisterValueError(f"fake: 0x{addr:04X} refuses writes")
        if any(addr in span for span in self.unsupported):
            raise UnsupportedRegisterError(f"fake: 0x{addr:04X} absent")
        if addr not in self.registers:
            # Mirrors read_holding's fixture-gap check: an address that is
            # neither recorded nor declared unsupported is UNKNOWN, and a
            # silent, confident write+read-back round trip through an
            # address nothing knows anything about is not evidence of
            # anything -- it must be as loud as the equivalent read.
            raise LookupError(
                f"fixture gap: 0x{addr:04X} is not in this FakeTransport's "
                "registers and is not declared unsupported either. Writing "
                "to an address nothing knows about would silently succeed "
                "and read back cleanly, which is not evidence the device "
                "accepts it -- seed it first (e.g. via "
                "tests.conftest.load_justice_registers_synthetic_complete) "
                "or add it to `unsupported` if it is verified absent."
            )
        if addr in self.no_stick_writes:
            return
        self.registers[addr] = value


class _FakeLockedOperations:
    """Read/write access to a FakeTransport while its lock is already held.

    Returned only by FakeTransport.atomic(); never construct directly.
    Mirrors custom_components.srne_inverter.transport.solarman_v5's
    _LockedOperations exactly, including the post-block invalidation.
    """

    def __init__(self, transport: FakeTransport) -> None:
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

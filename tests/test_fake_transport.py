"""The fake transport must behave like the real logger, including its errors.

Fix round 1 (2026-09-13): the reviewer found that verbatim fidelity to the
Task 3 brief was not evidence of correctness against the recorded capture and
the design spec. These tests exist to pin the corrected behaviour so the same
defects cannot silently come back:
  - a fixture gap (LookupError) is not a device fact (UnsupportedRegisterError)
  - DEFAULT_UNSUPPORTED must not contradict captured evidence
  - the connection-ownership contract (caller owns connect/close) is enforced
  - write_errors / no_stick_writes make the "accepted but rejected/ignored"
    paths directly testable
"""

import pytest

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    Transport,
    TransportBusyError,
    TransportConnectionError,
    UnsupportedRegisterError,
)
from tests.fake_transport import FakeTransport


# --- reads -------------------------------------------------------------


async def test_read_returns_recorded_values(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    assert await transport.read_holding(0x0100, 3) == [55, 531, 65527]
    assert transport.reads == [(0x0100, 3)]


async def test_read_of_absent_block_raises_unsupported(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(UnsupportedRegisterError):
        await transport.read_holding(0x0112, 4)
    with pytest.raises(UnsupportedRegisterError):
        await transport.read_holding(0xE21F, 2)


async def test_absent_block_is_distinguished_from_fixture_gap(justice_registers):
    """Same two addresses as above, but with no declared unsupported ranges
    at all: they must now raise LookupError (fixture gap), not
    UnsupportedRegisterError (device fact). Before the fix round 1, both
    branches raised the same exception, so this test would have passed even
    with an empty `unsupported` set -- exactly how the 0xE030-vs-0xE03A
    defect in DEFAULT_UNSUPPORTED went uncaught."""
    transport = FakeTransport(justice_registers, unsupported=())
    await transport.connect()
    with pytest.raises(LookupError):
        await transport.read_holding(0x0112, 4)
    with pytest.raises(LookupError):
        await transport.read_holding(0xE21F, 2)
    # And a LookupError is emphatically not a TransportError subclass -- a
    # caller catching the transport taxonomy must not accidentally swallow it.
    assert not isinstance(LookupError(), UnsupportedRegisterError)


async def test_partial_block_read_raises_without_partial_data(justice_registers):
    """Reading past the end of what was captured must fail outright, never
    return a truncated list -- callers must not be able to mistake a partial
    read for a complete one."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    # "battery" (registers.BLOCKS) is 0x0100-0x010E (15 registers); asking
    # for 20 walks straight into the fixture gap past 0x010E.
    with pytest.raises(LookupError):
        await transport.read_holding(0x0100, 20)


async def test_read_errors_are_injectable_once(justice_registers):
    transport = FakeTransport(
        justice_registers, read_errors=[TransportConnectionError("boom"), None]
    )
    await transport.connect()
    with pytest.raises(TransportConnectionError):
        await transport.read_holding(0x0100, 1)
    assert await transport.read_holding(0x0100, 1) == [55]


# --- writes --------------------------------------------------------------


async def test_write_then_read_back(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    await transport.write_holding(0xE01E, 16)
    assert await transport.read_holding(0xE01E, 1) == [16]
    assert transport.writes == [(0xE01E, 16)]


async def test_write_to_rejected_register_raises(justice_registers):
    """E20F/E20B/E21D/E039 are rejected by the real firmware."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(InvalidRegisterValueError):
        await transport.write_holding(0xE20F, 2)


@pytest.mark.parametrize("addr", [0xE20B, 0xE21D, 0xE039])
async def test_write_to_other_rejected_registers_raises(justice_registers, addr):
    """The other three WRITE_REJECTED addresses -- only 0xE20F had a test."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(InvalidRegisterValueError):
        await transport.write_holding(addr, 1)


async def test_e039_read_and_write_answers_are_not_contradictory(justice_registers):
    """The exact defect the reviewer measured in fix round 1: 0xE039 is
    write-rejected (IllegalDataValue, which only makes sense for an address
    that exists), so a read of 0xE039 must never answer
    UnsupportedRegisterError (IllegalDataAddress, "this address does not
    exist") -- that pair of answers is not physically possible from a real
    Modbus device. 0xE039 is not inside any registers.BLOCKS block, so the
    fixture has no data for it either way; the read must raise LookupError
    (honest "we don't know"), not UnsupportedRegisterError (a false claim of
    absence)."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(LookupError) as read_exc_info:
        await transport.read_holding(0xE039, 1)
    assert not isinstance(read_exc_info.value, UnsupportedRegisterError)
    with pytest.raises(InvalidRegisterValueError):
        await transport.write_holding(0xE039, 1)


async def test_write_to_unsupported_address_raises_unsupported(justice_registers):
    """Writing to an address the device does not have at all must raise
    UnsupportedRegisterError, not InvalidRegisterValueError -- these are
    different firmware answers (IllegalDataAddress vs. IllegalDataValue)."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(UnsupportedRegisterError):
        await transport.write_holding(0x0112, 1)


async def test_write_errors_are_injectable_once(justice_registers):
    """Symmetric with read_errors -- lets a test simulate a value-dependent
    firmware rejection (e.g. a write the real device would answer
    IllegalDataValue to) without needing that address in WRITE_REJECTED."""
    transport = FakeTransport(
        justice_registers,
        write_errors=[InvalidRegisterValueError("fake: bad value"), None],
    )
    await transport.connect()
    with pytest.raises(InvalidRegisterValueError):
        await transport.write_holding(0xE01E, 999)
    await transport.write_holding(0xE01E, 16)
    assert await transport.read_holding(0xE01E, 1) == [16]
    assert transport.writes == [(0xE01E, 999), (0xE01E, 16)]


async def test_no_stick_write_produces_read_back_mismatch(justice_registers):
    """The correct recipe for testing a write the firmware accepts but does
    not actually apply: declare the address via `no_stick_writes` at
    construction time. (The write-then-read-back-mismatch recipe in the
    original Task 3 report -- mutate `transport.registers` after calling
    write_holding -- does NOT work when the write and the read-back happen
    in the same coroutine/await, which is exactly the coordinator's and the
    write service's shape; that recipe was wrong and is replaced by this.)
    """
    transport = FakeTransport(justice_registers, no_stick_writes=(0xE01E,))
    await transport.connect()
    before = justice_registers[0xE01E]
    await transport.write_holding(0xE01E, 16)
    assert transport.writes == [(0xE01E, 16)]  # the attempt IS recorded
    assert await transport.read_holding(0xE01E, 1) == [before]  # but never stuck


# --- connect / close lifecycle --------------------------------------------


async def test_connect_failure_is_injectable(justice_registers):
    transport = FakeTransport(
        justice_registers, connect_error=TransportConnectionError("boom")
    )
    with pytest.raises(TransportConnectionError):
        await transport.connect()


async def test_connect_busy_error_is_injectable(justice_registers):
    """TransportBusyError is a TransportConnectionError subclass, so a test
    (or a caller) can inject/catch the specific "logger already has a
    client" case, not just the generic bucket."""
    transport = FakeTransport(
        justice_registers, connect_error=TransportBusyError("fake: logger busy")
    )
    with pytest.raises(TransportBusyError):
        await transport.connect()
    with pytest.raises(TransportConnectionError):
        # Also catchable via the base class, per the additive contract.
        transport2 = FakeTransport(
            justice_registers, connect_error=TransportBusyError("fake: logger busy")
        )
        await transport2.connect()


async def test_connected_stays_false_after_failed_connect(justice_registers):
    transport = FakeTransport(
        justice_registers, connect_error=TransportConnectionError("boom")
    )
    with pytest.raises(TransportConnectionError):
        await transport.connect()
    assert transport.connected is False
    assert transport.connect_count == 1


async def test_connect_close_lifecycle_and_counters(justice_registers):
    transport = FakeTransport(justice_registers)
    assert transport.connected is False
    assert transport.connect_count == 0
    assert transport.close_count == 0

    await transport.connect()
    assert transport.connected is True
    assert transport.connect_count == 1

    await transport.close()
    assert transport.connected is False
    assert transport.close_count == 1

    # close() must be safe to call again while already closed.
    await transport.close()
    assert transport.close_count == 2
    assert transport.connected is False


async def test_read_before_connect_raises_connection_error(justice_registers):
    transport = FakeTransport(justice_registers)
    with pytest.raises(TransportConnectionError):
        await transport.read_holding(0x0100, 1)


async def test_write_before_connect_raises_connection_error(justice_registers):
    transport = FakeTransport(justice_registers)
    with pytest.raises(TransportConnectionError):
        await transport.write_holding(0xE01E, 1)


async def test_read_after_close_raises_connection_error(justice_registers):
    """The connection-ownership contract: the caller owns connect/close, and
    a read after an explicit close() must fail rather than silently
    reconnecting -- otherwise the pause switch (spec Section 4.2) would not
    actually free the logger for another tool."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    await transport.close()
    with pytest.raises(TransportConnectionError):
        await transport.read_holding(0x0100, 1)


async def test_write_after_close_raises_connection_error(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    await transport.close()
    with pytest.raises(TransportConnectionError):
        await transport.write_holding(0xE01E, 1)


# --- data integrity --------------------------------------------------------


async def test_registers_dict_is_copied_not_aliased(justice_registers):
    original_value = justice_registers[0x0100]
    transport = FakeTransport(justice_registers)

    # Mutating the caller's dict after construction must not leak in.
    justice_registers[0x0100] = 999999
    await transport.connect()
    assert await transport.read_holding(0x0100, 1) == [original_value]

    # And the transport's own write must not leak back into the caller's dict.
    await transport.write_holding(0x0100, 1)
    assert justice_registers[0x0100] == 999999


# --- Protocol conformance --------------------------------------------------


def test_fake_transport_satisfies_protocol():
    """The brief's own stated verification criterion, made an actual
    assertion instead of something checked once by hand -- this is what will
    catch Task 4's real transport drifting from the Protocol."""
    transport = FakeTransport({})
    assert isinstance(transport, Transport)

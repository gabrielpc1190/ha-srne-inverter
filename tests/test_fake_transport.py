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

Fix round 2 (2026-09-13, same day): the fix itself introduced new defects,
found by re-review with a mutation battery. Additional tests pin:
  - a call the fake refuses for not being connected leaves `reads`/`writes`
    untouched (the not-connected guard now runs before the append)
  - `connect_errors` lets a busy-then-recovers retry sequence be expressed
  - DEFAULT_UNSUPPORTED's *content* is a tested property, not just a script
    in a report: zero overlap with recorded addresses, zero overlap with any
    address `registers.BLOCKS` declares present
  - writes are exactly as loud as reads about an address nothing knows
    anything about (both raise LookupError, not just reads)

Fix round 2 (task 4, re-review, 2026-09-13): `atomic()` (Task 4's Finding 1
fix shape) was added to the Transport Protocol and to this fake so Tasks
7/12/15 can test a write-plus-read-back bracket against it, not only against
the real transport. Additional tests pin:
  - atomic() genuinely serialises against a concurrent plain read/write --
    it must not be a no-op context manager that returns self
  - the handle atomic() yields is invalidated the instant its block exits
"""

import asyncio

import pytest

from custom_components.srne_inverter.registers import BLOCKS
from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    Transport,
    TransportBusyError,
    TransportConnectionError,
    UnsupportedRegisterError,
)
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport


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
    with pytest.raises(LookupError) as first_exc:
        await transport.read_holding(0x0112, 4)
    with pytest.raises(LookupError) as second_exc:
        await transport.read_holding(0xE21F, 2)
    # The exceptions FakeTransport actually raised must not be
    # UnsupportedRegisterError -- checking a freshly constructed, unrelated
    # LookupError() here (fix round 1's original assertion) is tautological,
    # since it never depends on FakeTransport's behaviour at all. Checking
    # the two instances the code under test actually produced is the
    # assertion that could fail if that ever changed.
    assert not isinstance(first_exc.value, UnsupportedRegisterError)
    assert not isinstance(second_exc.value, UnsupportedRegisterError)


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


@pytest.mark.parametrize("addr", [0xE130, 0x023A])
async def test_write_to_unknown_address_raises_lookup_error(justice_registers, addr):
    """Fix round 2, Finding 4 -- same defect class as round 1's Finding 1,
    now on the write path. The reviewer measured that writing to an address
    that is neither recorded nor declared unsupported (0xE130, just past
    the settings_high/config gap; 0x023A, one past the end of the declared
    "inverter_b" block) silently succeeded and read back cleanly, while the
    equivalent read already correctly raised LookupError. Writes must be as
    loud as reads about an address nothing knows anything about."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    assert addr not in justice_registers  # sanity: genuinely unknown, not just untested
    with pytest.raises(LookupError):
        await transport.write_holding(addr, 1)


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
    # Fix round 2, Finding 1: a call the fake refused outright must not show
    # up in the log -- Task 12's natural pause-switch assertion is
    # `transport.reads == []`, and that can only ever hold if a refused call
    # never got appended in the first place.
    assert transport.reads == []


async def test_write_before_connect_raises_connection_error(justice_registers):
    transport = FakeTransport(justice_registers)
    with pytest.raises(TransportConnectionError):
        await transport.write_holding(0xE01E, 1)
    assert transport.writes == []


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
    # Fix round 2, Finding 1: same as the before-connect case above -- the
    # refused read after close() must not appear in `reads` either.
    assert transport.reads == []


async def test_write_after_close_raises_connection_error(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    await transport.close()
    with pytest.raises(TransportConnectionError):
        await transport.write_holding(0xE01E, 1)
    assert transport.writes == []


async def test_connect_errors_queue_lets_a_busy_retry_succeed(justice_registers):
    """Fix round 2, Finding 2: `connect_error` alone is sticky-only, so
    "busy, busy, then through" -- this hardware's single most common real
    failure mode, another client holding the logger and then releasing it --
    could not be expressed when the retry loop lives inside the code under
    test. `connect_errors` is a queue, like `read_errors`/`write_errors`."""
    transport = FakeTransport(
        justice_registers,
        connect_errors=[
            TransportBusyError("fake: logger busy"),
            TransportBusyError("fake: logger busy"),
            None,
        ],
    )
    with pytest.raises(TransportBusyError):
        await transport.connect()
    with pytest.raises(TransportBusyError):
        await transport.connect()
    await transport.connect()  # third attempt: queue says None -> succeeds
    assert transport.connected is True
    assert transport.connect_count == 3


async def test_connect_errors_queue_defers_to_sticky_connect_error_when_exhausted(
    justice_registers,
):
    """Once the `connect_errors` queue runs out, `connect_error` (if set)
    takes back over for every later call -- the "keep the sticky behaviour"
    half of the fix round 2 instruction."""
    transport = FakeTransport(
        justice_registers,
        connect_errors=[None],
        connect_error=TransportBusyError("fake: logger busy"),
    )
    await transport.connect()  # queue's only entry: None -> succeeds
    assert transport.connected is True
    await transport.close()
    with pytest.raises(TransportBusyError):
        await transport.connect()  # queue exhausted -> falls back to sticky
    assert transport.connected is False


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
    assertion instead of something checked once by hand.

    Corrected in fix round 2 (Finding 5): this does NOT catch "Task 4's real
    transport drifting from the Protocol" in any general sense, and must not
    be trusted to. `Transport` is `@runtime_checkable`, and per `typing`'s
    own documentation such a check only verifies that the named methods and
    attributes EXIST on the object -- not their signatures, not whether
    `read_holding` is actually a coroutine function, not the
    connection-ownership contract in the class docstring. It WOULD catch a
    transport that is missing one of the four methods or `connected`
    entirely (e.g. a typo'd rename). It would NOT catch a real transport
    whose `read_holding` takes the arguments in the wrong order, that
    reconnects silently after close(), or that returns a generator instead
    of a coroutine. Treat this as a cheap smoke test for "did I forget to
    implement a method", not as proof of Protocol conformance.
    """
    transport = FakeTransport({})
    assert isinstance(transport, Transport)


# --- DEFAULT_UNSUPPORTED properties -----------------------------------------


def test_default_unsupported_never_covers_a_recorded_address(justice_registers):
    """Fix round 2, Finding 3: turn the report's one-off regression-gate
    script into an actual test. Property: DEFAULT_UNSUPPORTED must have zero
    intersection with any address our own recorded capture has real data
    for -- if it did, the fake would claim IllegalDataAddress for an address
    we have live proof responds. This is exactly what would have caught
    round 1's Critical-2 defect (0xE100-0xE12F wrongly marked absent) the
    moment it was introduced, instead of only in a manual reviewer pass."""
    recorded_addresses = set(justice_registers)
    unsupported_addresses = {addr for span in DEFAULT_UNSUPPORTED for addr in span}
    overlap = recorded_addresses & unsupported_addresses
    assert overlap == set(), (
        "DEFAULT_UNSUPPORTED wrongly claims these recorded addresses are "
        f"absent: {sorted(hex(a) for a in overlap)}"
    )


def test_default_unsupported_never_covers_a_declared_present_block():
    """Companion property to the one above: DEFAULT_UNSUPPORTED must also
    have zero intersection with any address inside any of registers.BLOCKS's
    10 declared-present blocks -- independent of whether our capture happens
    to cover that address. This is the property that would have caught
    round 1's Critical-1 defect (5 of the 10 declared blocks reading as
    "absent" purely from fixture gaps) if `unsupported` had ever been
    widened to paper over a gap instead of fixing the fixture."""
    declared_present_addresses = {addr for block in BLOCKS for addr in block.addresses}
    unsupported_addresses = {addr for span in DEFAULT_UNSUPPORTED for addr in span}
    overlap = declared_present_addresses & unsupported_addresses
    assert overlap == set(), (
        "DEFAULT_UNSUPPORTED wrongly claims these declared-present "
        f"addresses are absent: {sorted(hex(a) for a in overlap)}"
    )


# --- synthetic-complete fixture ---------------------------------------------


async def test_synthetic_complete_fixture_covers_every_declared_block(
    justice_registers_synthetic_complete,
):
    """Fix round 2, Finding 6: the fixture round 1 added specifically so
    Task 5's probe could exercise all 10 declared blocks had zero test
    users -- it was never actually run. Exercise it here: every address
    inside every block registers.BLOCKS declares present must resolve
    without raising anything at all."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    await transport.connect()
    for block in BLOCKS:
        values = await transport.read_holding(block.addr, block.count)
        assert len(values) == block.count


# --- atomic() ----------------------------------------------------------


async def test_atomic_serialises_against_a_concurrent_plain_read(justice_registers):
    """Task 4 fix round 2, Finding 1: FakeTransport's atomic() must
    genuinely serialise against read_holding/write_holding -- not be a
    no-op context manager that returns self -- or a Task 12 test asserting
    a write+read-back bracket against this fake would prove nothing.
    """
    transport = FakeTransport(justice_registers)
    await transport.connect()
    order: list[str] = []
    atomic_started = asyncio.Event()

    async def do_atomic():
        async with transport.atomic() as t:
            order.append("atomic-start")
            atomic_started.set()
            await asyncio.sleep(0.02)
            await t.write_holding(0x0100, 1)
            order.append("atomic-end")

    async def do_read():
        await atomic_started.wait()
        await transport.read_holding(0x0100, 1)
        order.append("read-done")

    await asyncio.gather(do_atomic(), do_read())
    assert order == ["atomic-start", "atomic-end", "read-done"]


async def test_atomic_handle_is_invalidated_after_the_block_exits(justice_registers):
    """Same invalidation requirement as the real transport's atomic() --
    using the handle after its block has exited must raise RuntimeError
    rather than silently running outside the lock.
    """
    transport = FakeTransport(justice_registers)
    await transport.connect()
    async with transport.atomic() as t:
        await t.read_holding(0x0100, 1)
    with pytest.raises(RuntimeError):
        await t.read_holding(0x0100, 1)
    with pytest.raises(RuntimeError):
        await t.write_holding(0x0100, 1)

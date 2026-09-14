"""Coordinator: tiers, pauses, backoff, connection toggle, write read-back.

Deviation from the task brief, applying to nearly every test here: the
brief's tests were written against the `justice_registers` fixture (the
RAW recorded capture of Casa Justice inverter 1), which has real capture
gaps inside 5 of the 10 declared BLOCKS (faults, the tail of inverter_b,
all of control_high, most of meter, part of device_info -- see
tests/conftest.py's own docstring). FakeTransport treats a gap in what it
was fed as a fixture-completeness bug and raises plain `LookupError`
(never `UnsupportedRegisterError`) for it -- which `probe()` does NOT catch,
so `coordinator.async_probe()` would raise an uncaught LookupError on the
very first probe in nearly every test below. Every test that needs a full,
successful probe (i.e. all of them, since `async_probe()` always attempts
every block) uses `justice_registers_synthetic_complete` instead -- the
fixture built exactly for this ("Task 5's probe/coordinator tests"; see its
docstring in tests/conftest.py).
"""

import asyncio
from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter import coordinator as coordinator_module
from custom_components.srne_inverter import registers as R
from custom_components.srne_inverter.const import BACKOFF_SECONDS, DOMAIN
from custom_components.srne_inverter.coordinator import SrneCoordinator
from custom_components.srne_inverter.probe import BlockSupport, ProbeFailedError
from custom_components.srne_inverter.transport.base import (
    TransportBusyError,
    TransportConnectionError,
    TransportTimeoutError,
)
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport


class _FakeClock:
    """Stand-in for the `time` module inside coordinator.py's own namespace.

    Rebinding `coordinator_module.time` to an instance of this (via
    `monkeypatch.setattr`) only changes what `coordinator.py`'s `time.
    monotonic()` calls see -- it does NOT touch the real, global `time`
    module object, so asyncio's own clock (and therefore every real
    `asyncio.sleep()` in probe()/`_read_due_blocks()`) is unaffected. That
    separation is exactly what makes this safe: real sleeps still pace the
    fake reads (harmless, just slow), while the coordinator's tier/backoff
    bookkeeping runs on a clock this fixture fully controls.
    """

    def __init__(self, start: float) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture(name="entry")
def entry_fixture(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="3548208972", title="Justice Inv 1")
    entry.add_to_hass(hass)
    return entry


def build(hass, entry, transport, **kwargs):
    return SrneCoordinator(
        hass, entry, transport,
        scan_interval=kwargs.get("scan_interval", 10),
        warm_interval=kwargs.get("warm_interval", 60),
        cold_interval=kwargs.get("cold_interval", 300),
    )


async def test_probe_then_first_cycle_only_rereads_hot(
    hass, entry, justice_registers_synthetic_complete
):
    """Fix round 1, Finding 6 (and the resulting docstring "History" note on
    SrneCoordinator): async_probe() seeds every tier's next deadline from
    the probe's own read time instead of clearing the schedule. The probe
    just read every block once, so the cycle right after it must not pay
    for a second full ten-block pass -- only HOT is due again immediately
    (via the "nothing due yet -> read HOT anyway" fallback, since a 10 s
    update_interval means HOT's own deadline is about to be reached in real
    production too). This replaces the brief's original
    `test_probe_then_first_cycle_reads_all_tiers`, whose "read everything
    again" premise was exactly what made the wasteful double-pass mandatory
    in the first place."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    assert len(transport.reads) == len(R.BLOCKS)
    transport.reads.clear()

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    hot = sorted(b.addr for b in R.BLOCKS if b.tier is R.BlockTier.HOT)
    assert sorted(addr for addr, _ in transport.reads) == hot
    assert coordinator.data.values["battery_soc"] == 55
    # WARM data from the probe itself survives even though this cycle never
    # re-read it.
    assert coordinator.data.values["boost_voltage"] == pytest.approx(57.6)


async def test_second_cycle_reads_hot_only(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    transport.reads.clear()
    await coordinator.async_refresh()
    hot = [b.addr for b in R.BLOCKS if b.tier is R.BlockTier.HOT]
    assert sorted(addr for addr, _ in transport.reads) == sorted(hot)


async def test_warm_and_cold_values_survive_hot_only_cycles(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.data.values["boost_voltage"] == pytest.approx(57.6)
    assert coordinator.data.values["running_days"] == 0


async def test_unsupported_blocks_are_never_polled(
    hass, entry, justice_registers_synthetic_complete
):
    # Simulate a unit with no "meter" block: drop its registers AND declare
    # its address range unsupported. Dropping the registers alone is not
    # enough against this fake -- an address that is merely absent from the
    # registers dict (and not declared unsupported) raises LookupError
    # ("fixture incomplete"), not UnsupportedRegisterError ("device fact"),
    # which is exactly the distinction tests/fake_transport.py's module
    # docstring warns never to blur. Only the latter makes probe() classify
    # a block UNSUPPORTED instead of blowing up.
    partial = {
        k: v for k, v in justice_registers_synthetic_complete.items()
        if k < 0xF000
    }
    meter = R.block_by_addr(0xF02C)
    unsupported = DEFAULT_UNSUPPORTED + (range(meter.addr, meter.addr + meter.count),)
    transport = FakeTransport(partial, unsupported=unsupported)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    transport.reads.clear()
    await coordinator.async_refresh()
    assert 0xF02C not in [addr for addr, _ in transport.reads]


async def test_unsupported_answer_mid_cycle_does_not_fail_the_cycle(
    hass, entry, justice_registers_synthetic_complete
):
    """Fix round 1, Finding 1: a block that answers IllegalDataAddress
    (UnsupportedRegisterError) mid-poll is a HEALTHY round trip proving the
    block does not exist -- not evidence the link is broken. Before the fix,
    `_read_due_blocks` let this propagate as a plain TransportError, which
    closed the socket, armed backoff and failed the whole cycle even though
    every other due block had already been read successfully. Simulates a
    block the probe left UNKNOWN (a transient failure while probing) that
    turns out, on the first real poll where its tier is due, to be
    genuinely absent."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()

    meter = R.block_by_addr(0xF02C)
    coordinator.probe_result.support[meter.addr] = BlockSupport.UNKNOWN
    transport.unsupported = transport.unsupported + (
        range(meter.addr, meter.addr + meter.count),
    )
    # async_probe() (Fix round 1, Finding 6) seeds COLD due 300 s out; force
    # it due now without waiting for real or fake time to pass, so this
    # cycle actually attempts the now-absent meter block.
    coordinator._tier_due.pop(R.BlockTier.COLD, None)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.failure_count == 0
    assert transport.close_count == 0
    assert coordinator.support[meter.addr] is BlockSupport.UNSUPPORTED
    assert coordinator.data.values["battery_soc"] == 55


async def test_pause_landing_mid_cycle_is_not_booked_as_a_failure(
    hass, entry, justice_registers_synthetic_complete
):
    """Fix round 1, Finding 2: async_set_connection_enabled(False) landing
    while a cycle is already reading blocks must not be booked as a
    TransportError -- it is a deliberate user action (e.g. pausing to run
    justice_watch.py), not a device or link failure. Before the fix, the
    next block's read raised TransportConnectionError once the socket was
    closed underneath it, which incremented failure_count, armed backoff
    and logged an HA error for exactly what the user asked for."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()  # settle into steady state (HOT only)

    refresh_task = asyncio.ensure_future(coordinator.async_refresh())
    # Block 0 (battery) has no pre-read pause and completes almost
    # instantly; block 1 (faults) waits BLOCK_PAUSE=0.25 s first. 0.05 s
    # lands comfortably inside that window, well before block 1 is
    # attempted.
    await asyncio.sleep(0.05)
    await coordinator.async_set_connection_enabled(False)
    await refresh_task

    assert coordinator.failure_count == 0
    assert coordinator.last_update_success is True
    assert transport.close_count == 1   # only the toggle's own close()


async def test_connection_error_marks_update_failed_and_closes(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    transport._read_errors = [TransportConnectionError("dead")] * 20
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert transport.close_count >= 1
    assert coordinator.failure_count == 1


async def test_backoff_skips_the_socket_entirely(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    transport._read_errors = [TransportConnectionError("dead")] * 20
    await coordinator.async_refresh()
    transport.reads.clear()
    await coordinator.async_refresh()   # still inside the 2 s backoff window
    assert transport.reads == []
    assert coordinator.last_update_success is False


async def test_backoff_and_failure_count_reset_on_success(
    hass, entry, justice_registers_synthetic_complete, monkeypatch
):
    """Fix round 1, Finding 3: `_register_success` resetting `failure_count`
    and the backoff deadline/index was correct but undefended -- mutating
    either into a no-op still passed all of Task 7's original tests. If
    that ever regresses silently, after four lifetime transient glitches
    the delay pins at BACKOFF_SECONDS[-1]=60 s forever and every failure
    from then on schedules a needless re-probe. Uses the frozen clock (not
    real sleeps) so the pin is exact and fast."""
    clock = _FakeClock(5000.0)
    monkeypatch.setattr(coordinator_module, "time", clock)

    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    assert coordinator.last_update_success is True

    transport._read_errors = [TransportConnectionError("dead")]
    await coordinator.async_refresh()
    assert coordinator.failure_count == 1
    assert coordinator._backoff_until == pytest.approx(5000.0 + BACKOFF_SECONDS[0])

    clock.advance(BACKOFF_SECONDS[0] + 1)   # past the backoff window
    await coordinator.async_refresh()       # no error queued: succeeds
    assert coordinator.last_update_success is True
    assert coordinator.failure_count == 0
    assert coordinator._backoff_until == 0.0

    transport._read_errors = [TransportConnectionError("dead again")]
    clock.advance(1)
    await coordinator.async_refresh()
    assert coordinator.failure_count == 1
    # Not BACKOFF_SECONDS[1]: the index must have reset to 0 along with
    # failure_count, or this second-ever failure would already be escalated.
    assert coordinator._backoff_until == pytest.approx(clock.monotonic() + BACKOFF_SECONDS[0])


async def test_tier_deadlines_are_honoured_not_just_the_fallback(
    hass, entry, justice_registers_synthetic_complete, monkeypatch
):
    """Fix round 1, Finding 4: both of Task 7's original tier tests only
    drove back-to-back async_refresh() calls with no elapsed time, which
    exercises the "nothing due -> read HOT anyway" fallback branch, never
    the actual deadline comparison in `_read_due_blocks`. A regression that
    scheduled WARM/COLD to fire once and never again (frozen sensors for
    every settings/meter entity, forever) would have shipped green. Pinned
    here with the frozen clock instead of real sleeps."""
    clock = _FakeClock(9000.0)
    monkeypatch.setattr(coordinator_module, "time", clock)

    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    # Right after probe, at the same frozen instant: nothing is due yet
    # (async_probe() just seeded every tier `interval` seconds out), so this
    # falls back to HOT only -- not the behaviour under test here.
    await coordinator.async_refresh()
    transport.reads.clear()

    clock.advance(61)   # past scan(10s) and warm(60s), not cold(300s)
    await coordinator.async_refresh()
    hot_and_warm = sorted(
        b.addr for b in R.BLOCKS if b.tier in (R.BlockTier.HOT, R.BlockTier.WARM)
    )
    assert sorted(addr for addr, _ in transport.reads) == hot_and_warm
    transport.reads.clear()

    clock.advance(400)   # past every tier's interval
    await coordinator.async_refresh()
    assert sorted(addr for addr, _ in transport.reads) == sorted(b.addr for b in R.BLOCKS)


async def test_busy_and_timeout_failures_are_distinguishable(
    hass, entry, justice_registers_synthetic_complete
):
    """The taxonomy (TransportBusyError vs TransportTimeoutError, both
    TransportConnectionError subclasses) must survive into whatever the
    coordinator surfaces -- this is called out as the project's number-one
    support question, so it gets its own direct test rather than relying on
    an incidental log line. Not in the brief; added because that fact is
    otherwise unverified."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()

    transport._read_errors = [TransportBusyError("logger busy")]
    await coordinator.async_refresh()
    busy_message = str(coordinator.last_exception)

    # Reset backoff via the public toggle (not by poking _backoff_until
    # directly) so the next injected error is the one actually consumed,
    # instead of the cycle short-circuiting on "still backing off".
    await coordinator.async_set_connection_enabled(False)
    await coordinator.async_set_connection_enabled(True)
    assert coordinator.last_update_success is True

    transport._read_errors = [TransportTimeoutError("no reply")]
    await coordinator.async_refresh()
    timeout_message = str(coordinator.last_exception)

    assert "TransportBusyError" in busy_message
    assert "TransportTimeoutError" in timeout_message
    assert busy_message != timeout_message


async def test_probe_propagates_probe_failed_when_nothing_supported(hass, entry):
    """A wrong slave id makes every block answer IllegalDataAddress -- probe()
    must raise ProbeFailedError rather than the coordinator swallowing it
    into a "successful" probe with zero supported blocks (probe.py's own
    "Fix round 1, Finding 1"). Not in the brief; added because propagation
    through async_probe() specifically -- as opposed to probe() itself,
    already covered by test_probe.py -- was otherwise unverified, and a
    later task (config entry setup -> ConfigEntryNotReady) depends on it.

    Fix round 1, Finding 12: also pins that this failure path closes the
    transport. async_probe() itself connects if needed, so a caller that
    only catches ProbeFailedError and raises ConfigEntryNotReady (exactly
    what task-8-brief.md does) would otherwise abandon a connected client --
    stranding the logger's one TCP slot across every retry, locking out
    this integration's own next attempt, the CLI probe and
    justice_watch.py all at once."""
    transport = FakeTransport(
        {}, unsupported=tuple(range(b.addr, b.addr + b.count) for b in R.BLOCKS)
    )
    coordinator = build(hass, entry, transport)
    with pytest.raises(ProbeFailedError):
        await coordinator.async_probe()
    assert transport.connected is False
    assert transport.close_count == 1


async def test_support_property_returns_a_copy_not_the_live_dict(
    hass, entry, justice_registers_synthetic_complete
):
    """Fix round 2 (Task 7 review): `coordinator.support` used to return
    `self.probe_result.support` itself -- the exact dict `_read_due_blocks`
    mutates in place for the UnsupportedRegisterError reclassification (Fix
    round 1, Finding 1) and that block-polling and `supported_field_keys()`
    read directly. Any of the eight downstream tasks mutating what looked
    like a harmless snapshot would silently corrupt the coordinator's own
    capability map -- no error, no test to catch it, blast radius is which
    entities exist and which blocks get polled."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()

    borrowed = coordinator.support
    borrowed[0x0100] = BlockSupport.UNSUPPORTED
    borrowed.clear()

    assert coordinator.support[0x0100] is BlockSupport.SUPPORTED
    assert coordinator.probe_result.support[0x0100] is BlockSupport.SUPPORTED
    assert "battery_soc" in coordinator.supported_field_keys()


async def test_write_field_reads_back_and_updates_state(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()

    field = R.field_by_key("soc_low_alarm")
    await coordinator.async_write_field(field, 16)
    assert transport.writes == [(0xE01E, 16)]
    assert coordinator.data.values["soc_low_alarm"] == 16


async def test_write_mismatch_raises(hass, entry, justice_registers_synthetic_complete):
    # Deviation from the brief: it simulated a non-sticking write by
    # subclassing FakeTransport and overriding the PUBLIC write_holding().
    # That technique is silently defeated by writing through atomic()
    # (required for this method -- see coordinator.py's async_write_raw
    # docstring): atomic()'s yielded handle calls the private
    # _write_holding_locked directly, never the public method a subclass
    # would override. FakeTransport already ships the sanctioned mechanism
    # for this exact scenario -- `no_stick_writes` -- documented in its own
    # docstring as "how to test the write succeeded but didn't stick path
    # honestly"; this uses that instead.
    transport = FakeTransport(
        justice_registers_synthetic_complete, no_stick_writes=[0xE01E]
    )
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    with pytest.raises(HomeAssistantError, match="read back"):
        await coordinator.async_write_field(R.field_by_key("soc_low_alarm"), 16)


async def test_write_rejected_by_firmware_raises(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_write_raw(0xE20F, 2)


async def test_out_of_range_write_is_refused_before_touching_the_bus(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_write_field(R.field_by_key("soc_low_alarm"), 250)
    assert transport.writes == []


async def test_write_read_back_is_atomic_against_a_concurrent_poll(
    hass, entry, justice_registers_synthetic_complete
):
    """Fix round 1, Finding 5 (mutation M6): reverting async_write_raw to
    two independently-locked write_holding/read_holding calls (instead of
    one atomic() bracket) passed every one of Task 7's original tests,
    because none of them ran anything concurrently. This runs a write and a
    poll cycle concurrently and asserts the write's read-back is the
    IMMEDIATE next operation after its own write in FakeTransport's ordered
    `operations` log -- i.e. nothing, including the coordinator's own poll,
    can land between them. Under a real asyncio.Lock this is deterministic
    (the poll's read_holding call blocks on the lock atomic() is holding for
    the whole WRITE_SETTLE sleep), not a timing race."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    transport.operations.clear()

    field = R.field_by_key("soc_low_alarm")
    write_task = asyncio.ensure_future(coordinator.async_write_field(field, 20))
    # Let the write run up to its first real suspension point (the
    # WRITE_SETTLE sleep, still inside the atomic() bracket) before starting
    # the poll -- otherwise the poll could win the race for the lock first,
    # which would prove nothing either way.
    await asyncio.sleep(0)
    poll_task = asyncio.ensure_future(coordinator.async_refresh())
    await asyncio.gather(write_task, poll_task)

    write_index = transport.operations.index(("write", 0xE01E, 20))
    readback_index = transport.operations.index(
        ("read", 0xE01E, 1), write_index + 1
    )
    assert readback_index == write_index + 1, transport.operations


async def test_reconnecting_before_a_write_never_deadlocks_inside_atomic(
    hass, entry, justice_registers_synthetic_complete
):
    """Fix round 1, Finding 5 (mutation M7): a conditional `connect()`
    call moved INSIDE the atomic() bracket deadlocks the first time it
    actually needs to reconnect -- exactly the hazard transport/base.py's
    Transport.atomic() docstring calls out as "the most likely of the three
    to be reached by accident". Every other committed write test calls
    async_write_raw with the transport already connected, so that branch
    (and this hazard) was never exercised. This disconnects the transport
    directly first (NOT via the connection toggle, which would also flip
    _connection_enabled and make async_write_raw refuse the write before
    ever reaching the connect check) so the real "reconnect, THEN enter
    atomic()" code path actually runs. Bounded by wait_for so a regression
    fails fast and legibly instead of via the suite's --timeout=30
    backstop."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()

    await transport.close()
    assert transport.connected is False

    read_back = await asyncio.wait_for(
        coordinator.async_write_raw(0xE01E, 22), timeout=2.0
    )
    assert read_back == 22
    assert transport.connect_count == 2   # once from probe, once from this


async def test_disabling_the_connection_closes_and_stops_polling(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()

    await coordinator.async_set_connection_enabled(False)
    assert transport.connected is False
    assert coordinator.update_interval is None
    transport.reads.clear()
    connect_count_before_stray_tick = transport.connect_count
    await coordinator.async_refresh()
    assert transport.reads == []
    assert coordinator.last_update_success is False
    # Fix round 1 (Task 12 review, Finding 5): `reads == []` alone only
    # proves `_read_due_blocks()` never ran -- it says nothing about
    # whether `transport.connect()` ran first. Moving
    # `_async_update_data`'s `if not self._connection_enabled: raise
    # UpdateFailed(...)` guard to AFTER the `if not self.transport.
    # connected: await self.transport.connect()` line above it would leave
    # `reads == []` true (the moved guard would still raise UpdateFailed
    # before `_read_due_blocks()` is ever reached) while silently
    # reconnecting the transport first -- handing the logger's one TCP
    # slot right back the instant a stray, already-scheduled HA tick fires
    # (`update_interval = None` does not cancel one already in flight; see
    # `SrneCoordinator`'s own "Connection toggle" docstring), the exact
    # thing disabling the connection exists to prevent. Confirmed by hand:
    # with the guard moved, this assertion pair goes red while `reads ==
    # []` above stays green.
    assert transport.connected is False
    assert transport.connect_count == connect_count_before_stray_tick

    await coordinator.async_set_connection_enabled(True)
    assert coordinator.update_interval == timedelta(seconds=10)
    assert coordinator.last_update_success is True


async def test_supported_field_keys_excludes_missing_blocks(
    hass, entry, justice_registers_synthetic_complete
):
    partial = {
        k: v for k, v in justice_registers_synthetic_complete.items()
        if k < 0xF000
    }
    meter = R.block_by_addr(0xF02C)
    unsupported = DEFAULT_UNSUPPORTED + (range(meter.addr, meter.addr + meter.count),)
    transport = FakeTransport(partial, unsupported=unsupported)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    keys = coordinator.supported_field_keys()
    assert "battery_soc" in keys
    assert "running_days" not in keys
    assert "battery_power" in keys      # derived, both inputs available

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

from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter import registers as R
from custom_components.srne_inverter.const import DOMAIN
from custom_components.srne_inverter.coordinator import SrneCoordinator
from custom_components.srne_inverter.probe import ProbeFailedError
from custom_components.srne_inverter.transport.base import (
    TransportBusyError,
    TransportConnectionError,
    TransportTimeoutError,
)
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport


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


async def test_probe_then_first_cycle_reads_all_tiers(
    hass, entry, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    assert len(transport.reads) == len(R.BLOCKS)
    transport.reads.clear()

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    # First cycle after a probe is due on every tier.
    assert len(transport.reads) == len(R.BLOCKS)
    assert coordinator.data.values["battery_soc"] == 55


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
    later task (config entry setup -> ConfigEntryNotReady) depends on it."""
    transport = FakeTransport(
        {}, unsupported=tuple(range(b.addr, b.addr + b.count) for b in R.BLOCKS)
    )
    coordinator = build(hass, entry, transport)
    with pytest.raises(ProbeFailedError):
        await coordinator.async_probe()


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
    await coordinator.async_refresh()
    assert transport.reads == []
    assert coordinator.last_update_success is False

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

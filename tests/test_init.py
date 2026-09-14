"""Entry setup wires probe -> coordinator -> platforms and cleans up on unload.

Deviation from the task brief, applying to every test that needs a full,
successful setup (all of them except the busy-logger one): the brief's tests
were written against the `justice_registers` fixture (the RAW recorded
capture of Casa Justice inverter 1), which has real capture gaps inside 5 of
the 10 declared `registers.BLOCKS` (faults, the tail of inverter_b, all of
control_high, most of meter, part of device_info -- see tests/conftest.py's
own docstring, and tests/test_coordinator.py's module docstring, which hit
the exact same issue one task earlier). `coordinator.async_probe()` always
probes every block, and `FakeTransport` raises plain `LookupError` (never
caught by `probe()`) for an address that is neither recorded nor declared
`unsupported` -- so a setup driven against the plain `justice_registers`
fixture raises an uncaught `LookupError` out of `async_setup_entry`, not a
clean `ConfigEntryNotReady`/`SETUP_RETRY`. Every test below that runs a real
probe uses `justice_registers_synthetic_complete` instead (confirmed by hand:
`battery_soc`'s block, "battery" at 0x0100, has zero gaps in either fixture,
so `battery_soc == 55` holds unchanged). The one test that does NOT need a
full probe (`test_setup_raises_not_ready_when_connect_fails`) fails at
`connect()`, before any block is ever read, so the fixture's gaps never
matter there -- kept on the plain `justice_registers` fixture, matching the
brief.

Second deviation, same root cause as tests/test_coordinator.py's own: the
brief's `FakeTransport(..., fail_connect=True)` keyword no longer exists --
replaced by `connect_errors`, a queue consumed one entry per `connect()`
call. `connect_errors=[TransportConnectionError(...)]` models a connect-phase
failure (refused, unreachable, or another client's TCP session already
occupying the logger's one slot) -- deliberately the GENERIC
`TransportConnectionError`, not `TransportBusyError`: Task 4's phase-aware
`_translate()` maps every connect-phase `NoSocketAvailableError` to the
generic type, and reserves `TransportBusyError` for an ESTABLISHED session
discovered stolen during a later read/write (never observed at `connect()`
itself). An earlier version of this test used `TransportBusyError` here,
which was wrong for exactly that reason -- fixed per Task 8's Fix round 1,
Finding 5, since Task 13's error mapping will be written from this file.

Fix round 1 (Opus review) also added three tests this round originally
lacked: `test_setup_raises_not_ready_when_no_block_is_supported` (Finding 1
-- the brief's own OPENING requirement, a wrong slave id producing zero
SUPPORTED blocks, had ZERO coverage: narrowing `except TransportError` in
`async_setup_entry` to exclude `ProbeFailedError` left every original test
green), `test_setup_raises_not_ready_when_the_first_refresh_fails` (Finding
6 -- the probe can succeed while the very next read still fails), and two
assertions added to `test_setup_probes_and_stores_runtime_data` pinning that
the probe actually ran and classified capability (Finding 2 -- replacing
`await coordinator.async_probe()` with `pass` also left every original test
green, because the first refresh happens to read every block anyway when
`coordinator.support == {}`).
"""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter.const import (
    CONF_COLD_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    CONF_WARM_INTERVAL,
    DOMAIN,
)
from custom_components.srne_inverter.transport.base import TransportConnectionError
from tests.fake_transport import FakeTransport

ENTRY_DATA = {
    CONF_HOST: "192.168.188.240",
    CONF_PORT: 8899,
    CONF_SERIAL: "3548208972",
    CONF_SLAVE_ID: 1,
}
ENTRY_OPTIONS = {
    CONF_SCAN_INTERVAL: 10,
    CONF_WARM_INTERVAL: 60,
    CONF_COLD_INTERVAL: 300,
}


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


async def setup_entry(hass, transport, *, unique_id="3548208972", title="Justice Inv 1"):
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id=unique_id, title=title,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_setup_probes_and_stores_runtime_data(
    hass, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data.values["battery_soc"] == 55
    assert entry.runtime_data.transport is transport
    # Fix round 1, Finding 2: pins that the probe actually ran and its
    # classification reaches the coordinator's public capability API --
    # NOT redundant with the assertions above. Replacing
    # `await coordinator.async_probe()` in async_setup_entry with `pass`
    # leaves `battery_soc == 55` and `runtime_data.transport is transport`
    # both green (the coordinator's own first refresh happens to read every
    # block anyway when `support == {}`), but leaves `probe_result` `None`
    # and `supported_field_keys()` empty -- silently losing the per-unit
    # capability detection this integration exists for.
    assert entry.runtime_data.coordinator.probe_result is not None
    assert "battery_soc" in entry.runtime_data.coordinator.supported_field_keys()


async def test_setup_raises_not_ready_when_connect_fails(hass, justice_registers):
    """Connect-phase failure (refused, unreachable, another client's TCP
    session already on the logger's one slot) -- generic
    TransportConnectionError, per Task 4's phase-aware translate(). This is
    NOT the "wrong slave id" scenario (see
    test_setup_raises_not_ready_when_no_block_is_supported below): a wrong
    slave id connects fine and fails inside probe() itself.
    """
    transport = FakeTransport(
        justice_registers,
        connect_errors=[TransportConnectionError("connection refused")],
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="x", title="Unreachable",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    # The logger's one TCP slot must not be left held across the retry wait --
    # connect() never succeeded, so this is really "close() is safe to call
    # on a transport that was never connected", but it pins the observable
    # outcome the design's "one connection slot" fact cares about either way.
    assert transport.connected is False


async def test_setup_raises_not_ready_when_no_block_is_supported(
    hass, justice_registers_synthetic_complete
):
    """Fix round 1, Finding 1 -- the brief's own OPENING requirement, and
    the whole reason this design has a probe step at all: a wrong slave id
    (this site's own confusable `.240`/slave 1 vs. `.242`/slave 2 pair)
    connects fine but makes every block answer IllegalDataAddress, i.e.
    UNSUPPORTED -- zero SUPPORTED blocks, `probe()` raises `ProbeFailedError`
    (a `TransportError`). This must surface as SETUP_RETRY, never a device
    silently marked LOADED with zero entities that polls nothing forever.
    Narrowing `except TransportError` in `async_setup_entry` to
    `except TransportConnectionError` (excluding `ProbeFailedError`, which is
    a direct `TransportError` subclass, not a `TransportConnectionError` one)
    leaves every OTHER test in this file green -- this is the only test that
    actually drives a `ProbeFailedError` out of a successful `connect()`.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=(range(0x0000, 0x10000),),
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="wrong-slave-id", title="Wrong slave id",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert transport.connected is False


async def test_setup_raises_not_ready_when_the_first_refresh_fails(
    hass, justice_registers_synthetic_complete
):
    """Fix round 1, Finding 6 -- the probe can succeed completely (every
    block SUPPORTED) and the very next read still fail, e.g. the session
    dying the instant after the probe finished. `read_errors` is a queue of
    exactly 10 `None`s (one per `registers.BLOCKS` block, consumed in order
    by the probe's own 10 reads) followed by one real failure, which lands
    on the coordinator's OWN first refresh cycle -- seeded to re-read only
    the HOT tier after a successful probe (Task 7's own scheduling), whose
    first block is "battery".
    `coordinator.async_config_entry_first_refresh()` converts that into
    `ConfigEntryNotReady` on Home Assistant's own behalf (verified against
    the installed `DataUpdateCoordinator` source, not this file's code); by
    the time that happens `async_setup_entry` itself has nothing left to
    close. Checked by mutation which of the coordinator's OWN mechanisms
    actually guarantees that (not assumed): `_register_failure`'s own
    `await self.transport.close()` closes it first in the normal case, but
    removing ONLY that line still leaves this test green, because the setup
    then fails (`ConfigEntryNotReady` propagates) and the coordinator's
    on_unload safety net (Fix round 1, Finding 4 -- registered by
    `DataUpdateCoordinator.__init__`) closes it independently. Only
    removing BOTH turns this red -- on this specific path (a failure during
    INITIAL setup, as opposed to a later scheduled poll on an already-LOADED
    entry, where the on_unload safety net does not apply) the two
    mechanisms are genuinely redundant with each other, not just decorative.

    Also pins Fix round 1, Finding 3's interface-doc addition: `entry.
    runtime_data` was already set (the probe succeeded) before this failure,
    and SURVIVES it -- present here even though `entry.state` ends up
    `SETUP_RETRY`, not `LOADED`. Tasks 13-15 must gate on `entry.state`,
    never on `hasattr(entry, "runtime_data")`.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[None] * 10
        + [TransportConnectionError("session died right after the probe")],
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="first-refresh-fails", title="First refresh fails",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert transport.connected is False
    # runtime_data survives, pointing at a dead coordinator -- see the
    # docstring above.
    assert entry.runtime_data.coordinator.last_update_success is False


async def test_unload_closes_the_socket(hass, justice_registers_synthetic_complete):
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert transport.connected is False


async def test_changing_options_reloads_the_entry(
    hass, justice_registers_synthetic_complete
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    close_count_before = transport.close_count
    connect_count_before = transport.connect_count
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        hass.config_entries.async_update_entry(
            entry, options={**ENTRY_OPTIONS, CONF_SCAN_INTERVAL: 30}
        )
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.scan_interval == 30
    # Fix round 1, Finding 6 -- on a one-slot device, this is the assertion
    # that matters most about reload: it must actually release the logger
    # (old coordinator's async_shutdown -> transport.close()) before the
    # fresh coordinator reopens it (async_probe() -> transport.connect()),
    # not just end up LOADED with the new option applied.
    assert transport.close_count > close_count_before
    assert transport.connect_count > connect_count_before


async def test_setup_failure_after_connecting_still_closes_the_socket(
    hass, justice_registers_synthetic_complete
):
    """Not from the brief.

    Pins the fact this task's brief calls out explicitly: "unload must
    release [the logger's one TCP slot] even when setup failed partway".
    `hass.config_entries.async_forward_entry_setups` is patched to raise
    AFTER the probe has already connected the transport and
    `entry.runtime_data` has been set -- Home Assistant's own config-entry
    machinery does NOT call this integration's `async_unload_entry` in that
    case (verified against `ConfigEntry.async_unload`'s installed source: it
    only calls it when the entry reached `LOADED`, and this entry never
    does). What DOES close the transport here is a mechanism `__init__.py`
    relies on but does not itself create: `SrneCoordinator.__init__` (Task 7)
    passes `config_entry=entry` to `DataUpdateCoordinator.__init__`, which
    registers `entry.async_on_unload(self.async_shutdown)` on its own --
    `async_shutdown` awaits `transport.close()`. Confirmed by hand: stubbing
    `SrneCoordinator.async_shutdown` to a no-op via monkeypatch turns this
    red (`transport.connected` stays `True`, `close_count` stays `0`).
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="partial-failure", title="Partial failure",
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.srne_inverter.build_transport",
            return_value=transport,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("boom: platform setup exploded"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert transport.connected is False
    assert transport.close_count >= 1

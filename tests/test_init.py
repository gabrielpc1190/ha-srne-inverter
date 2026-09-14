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
full probe (`test_setup_raises_not_ready_when_the_logger_is_busy`) fails at
`connect()`, before any block is ever read, so the fixture's gaps never
matter there -- kept on the plain `justice_registers` fixture, matching the
brief.

Second deviation, same root cause as tests/test_coordinator.py's own: the
brief's `FakeTransport(..., fail_connect=True)` keyword no longer exists --
replaced by `connect_errors`, a queue consumed one entry per `connect()`
call. `connect_errors=[TransportBusyError(...)]` models this site's most
common real failure (this site's own confusable `.240`/slave 1 vs.
`.242`/slave 2 pair, or a leftover justice_watch.py holding the logger).
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
from custom_components.srne_inverter.transport.base import TransportBusyError
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


async def test_setup_raises_not_ready_when_the_logger_is_busy(
    hass, justice_registers
):
    transport = FakeTransport(
        justice_registers, connect_errors=[TransportBusyError("logger busy")]
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="x", title="Busy",
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
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        hass.config_entries.async_update_entry(
            entry, options={**ENTRY_OPTIONS, CONF_SCAN_INTERVAL: 30}
        )
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.scan_interval == 30


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

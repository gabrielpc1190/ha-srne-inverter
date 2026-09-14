"""Diagnostics: enough to debug a unit without SSH, with the host and the
serial redacted -- and, just as importantly, something USEFUL (not a crash)
when the entry never finished setting up, because that is exactly when
someone downloads it.

Deviation from the task brief, same root cause as tests/test_init.py's own
module docstring (this task's own instructions flagged it too):
`coordinator.async_probe()` always probes every one of `registers.BLOCKS`'
10 blocks, and the raw recorded Casa Justice capture (`justice_registers`)
has real gaps inside 5 of them. `FakeTransport` raises plain `LookupError`
(never caught by `probe()`) for an address that is neither recorded nor
declared `unsupported` -- so both of the brief's tests, which drive a full
setup through `justice_registers`, would crash with an uncaught
`LookupError` out of the probe long before either test's own assertions.
Fixed by switching to `justice_registers_synthetic_complete` throughout,
exactly as every earlier task in this plan that runs a real probe already
does.

Second deviation, same shape as tests/test_init.py's: the brief's
`test_diagnostics_lists_unsupported_blocks` builds `partial` by simply
dropping every key `>= 0xF000` from the registers dict, with no
`unsupported=` declaration. That models "the meter block is a fixture
gap", not "the meter block is absent on this device" -- `FakeTransport`
raises `LookupError` for the former and `UnsupportedRegisterError` (which
is what actually produces `BlockSupport.UNSUPPORTED`) only for the latter.
Fixed by also declaring the meter block's own address range `unsupported=`,
on top of the fixture switch above.

Third addition, not in the brief at all: two tests pinning what the
download looks like for an entry that never reached `LOADED` -- the case
the task's own instructions call out by name ("think about what
diagnostics should return for an entry that failed to set up, because that
is exactly when someone downloads it"). One covers a probe/connect failure
(`entry.runtime_data` never assigned at all); the other covers a dead
first refresh (`entry.runtime_data` survives, pointing at a coordinator
whose probe DID succeed but whose `coordinator.data` is still `None`) --
these are the two structurally different "failed setup" shapes
`task-8-report.md`'s Fix round 1 documents, and they must not collapse
into the same diagnostics output.
"""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter.const import DOMAIN
from custom_components.srne_inverter.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.srne_inverter.transport.base import TransportConnectionError
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport
from tests.test_init import ENTRY_DATA, ENTRY_OPTIONS, setup_entry


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


async def test_diagnostics_contents(hass, justice_registers_synthetic_complete):
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["state"] == "loaded"
    assert diag["entry"]["data"]["host"] == "**REDACTED**"
    assert diag["entry"]["data"]["serial"] == "**REDACTED**"
    assert diag["entry"]["data"]["slave_id"] == 1
    assert diag["support"]["0x0100"] == "supported"
    assert diag["registers"]["0x0100"] == 55
    assert diag["values"]["battery_soc"] == 55
    assert diag["coordinator"]["connection_enabled"] is True
    assert diag["coordinator"]["failure_count"] == 0
    assert diag["coordinator"]["last_update_success"] is True
    assert diag["probe_errors"] == {}


async def test_diagnostics_lists_unsupported_blocks(
    hass, justice_registers_synthetic_complete
):
    partial = {
        k: v for k, v in justice_registers_synthetic_complete.items() if k < 0xF000
    }
    transport = FakeTransport(
        partial, unsupported=DEFAULT_UNSUPPORTED + (range(0xF02C, 0xF02C + 24),)
    )
    entry = await setup_entry(hass, transport)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["support"]["0xF02C"] == "unsupported"
    assert "0xF02C" in diag["probe_errors"]


async def test_diagnostics_on_a_probe_failure_does_not_crash(
    hass, justice_registers_synthetic_complete
):
    """A wrong slave id (this site's own confusable `.240`/slave 1 vs.
    `.242`/slave 2 pair) leaves every block UNSUPPORTED -- `probe()` raises
    `ProbeFailedError`, `async_setup_entry` never assigns
    `entry.runtime_data` at all, and the entry lands on `SETUP_RETRY`. This
    is the case the task's own instructions call out: exactly the moment
    someone is most likely to download diagnostics. It must return a
    plain, empty-but-complete dict, never raise `AttributeError` out of
    `entry.runtime_data.coordinator` -- which is what the brief's own
    Step 3 code, taken verbatim, would do here.
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
    assert not hasattr(entry, "runtime_data")

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["state"] == "setup_retry"
    assert diag["entry"]["data"]["host"] == "**REDACTED**"
    assert diag["coordinator"] == {}
    assert diag["support"] == {}
    assert diag["probe_errors"] == {}
    assert diag["registers"] == {}
    assert diag["values"] == {}


async def test_diagnostics_on_a_dead_first_refresh_still_shows_the_probe(
    hass, justice_registers_synthetic_complete
):
    """A different failed-setup shape than the one above: the probe itself
    succeeds completely (every block SUPPORTED), and the session dies on
    the coordinator's own first read cycle right after -- `entry.
    runtime_data` SURVIVES this (assigned before the first refresh ever
    runs, per `__init__.py`), pointing at a coordinator whose `probe_result`
    is real and whose `data` is still `None` (the first refresh never
    succeeded). Diagnostics must show the support map from that surviving
    probe_result -- proving this module does not simply gate everything on
    `entry.state is LOADED`, only on whether `entry.runtime_data` exists at
    all.
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
    assert hasattr(entry, "runtime_data")

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["state"] == "setup_retry"
    assert diag["coordinator"]["last_update_success"] is False
    assert diag["support"]["0x0100"] == "supported"
    assert diag["probe_errors"] == {}
    # The first refresh never completed, so there is no register/value
    # snapshot yet -- unlike the support map above, which survives from the
    # probe alone.
    assert diag["registers"] == {}
    assert diag["values"] == {}

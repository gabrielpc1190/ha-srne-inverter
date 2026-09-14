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

Third addition, not in the brief at all: tests pinning what the download
looks like for an entry that never reached `LOADED` -- the case the
task's own instructions call out by name ("think about what diagnostics
should return for an entry that failed to set up, because that is exactly
when someone downloads it"). These cover the two structurally different
"failed setup" shapes `task-8-report.md`'s Fix round 1 documents (probe/
connect failure: `entry.runtime_data` never assigned; dead first refresh:
`entry.runtime_data` survives, pointing at a coordinator whose probe DID
succeed but whose `coordinator.data` is still `None`).

Fix round 1 (Opus review) added the tests below the "--- Fix round 1 ---"
marker: a redaction leak (the host reappearing inside free-form error
text, unredacted, even though `entry.data["host"]` itself was scrubbed),
the two failed-setup causes being indistinguishable from each other, raw
registers being discarded exactly when they were the only data left (the
probe succeeded, the coordinator's own refresh never did), the inverter's
own serial shipping unmasked while the logger's serial was redacted, and
the `UNKNOWN` support state never being asserted anywhere.
"""

import json

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter import registers as R
from custom_components.srne_inverter.const import DOMAIN
from custom_components.srne_inverter.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.srne_inverter.transport.base import (
    TransportBusyError,
    TransportConnectionError,
)
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport
from tests.test_init import ENTRY_DATA, ENTRY_OPTIONS, setup_entry


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


async def test_diagnostics_contents(hass, justice_registers_synthetic_complete):
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["state"] == "loaded"
    assert diag["entry"]["reason"] is None
    assert diag["entry"]["data"]["host"] == "**REDACTED**"
    assert diag["entry"]["data"]["serial"] == "**REDACTED**"
    assert diag["entry"]["data"]["slave_id"] == 1
    assert diag["support"]["0x0100"] == "supported"
    assert diag["registers"]["0x0100"] == 55
    assert diag["values"]["battery_soc"] == 55
    assert diag["registers_source"] == "coordinator"
    assert diag["coordinator"]["connection_enabled"] is True
    assert diag["coordinator"]["failure_count"] == 0
    assert diag["coordinator"]["last_update_success"] is True
    assert diag["coordinator"]["last_exception"] is None
    assert diag["probe_errors"] == {}
    # generated_at is a real, parseable timestamp -- not asserting an
    # exact value (it is "now"), just that it is present and ISO8601.
    from datetime import datetime

    datetime.fromisoformat(diag["generated_at"])


async def test_diagnostics_masks_the_device_info_tail(
    hass, justice_registers_synthetic_complete
):
    """Fix round 1: `values["inverter_serial"]` (at the time believed to be
    the INVERTER's own serial) used to ship unmasked while
    `entry.data["serial"]` (the LOGGER's serial) was fully redacted a few
    keys up -- an inconsistency, not a deliberate choice.

    2026-09-14 correction: that field is not actually a serial (live
    evidence -- see registers.py's own comment on 0x0018-0x001B) and was
    renamed `device_info_tail` / `FieldKind.HEX_WORDS`. The masking
    behaviour this test pins is UNCHANGED and still applies to it, as a
    defensive default for "an unverified multi-word diagnostic value", not
    because it turned out to be identifying after all.
    """
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    diag = await async_get_config_entry_diagnostics(hass, entry)

    real_value = R.decode(justice_registers_synthetic_complete)["device_info_tail"]
    masked = diag["values"]["device_info_tail"]
    assert masked != real_value
    assert masked.endswith(real_value[-4:])
    assert set(masked[: -len(real_value[-4:])]) == {"*"}
    assert real_value not in json.dumps(diag)


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


async def test_diagnostics_reports_unknown_blocks(
    hass, justice_registers_synthetic_complete
):
    """Fix round 1: `UNKNOWN` was never asserted anywhere in this file's
    tests -- a regression collapsing it into `UNSUPPORTED` (or dropping it
    from `as_diagnostics()`) would have stayed green. `faults` (`0x0200`,
    `BLOCKS[1]`) fails both of its probe attempts with a connection error
    (retried once, per `probe.py`'s default `retries=1`) while every other
    block succeeds -- `battery` first (1 read), then 8 more (1 read each),
    for 1 + 2 + 8 = 11 read_errors entries total, matching `probe()`'s own
    read count exactly. Nothing ever reclassifies an UNKNOWN block to
    SUPPORTED on a later successful read (only a fresh `async_probe()`
    does that -- see `coordinator.py`'s own `_read_due_blocks`), so this
    holds even after the entry reaches LOADED.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[None]
        + [TransportConnectionError("timeout"), TransportConnectionError("timeout")]
        + [None] * 8,
    )
    entry = await setup_entry(hass, transport)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["support"]["0x0200"] == "unknown"
    assert "0x0200" in diag["probe_errors"]


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
    assert "ProbeFailedError" in diag["entry"]["reason"]
    assert ENTRY_DATA[CONF_HOST] not in diag["entry"]["reason"]
    assert diag["coordinator"] == {}
    assert diag["support"] == {}
    assert diag["probe_errors"] == {}
    assert diag["registers_source"] == "none"
    assert diag["registers"] == {}
    assert diag["values"] == {}


async def test_diagnostics_distinguishes_the_two_probe_failure_causes(
    hass, justice_registers_synthetic_complete
):
    """Fix round 1: the reviewer measured a wrong slave id and a refused
    connection producing byte-identical payloads (once `entry.title` is
    ignored) -- yet `entry.reason` differs between them, and this is the
    ENTIRE first question of any support case here. Two full setups, two
    different `entry.reason` strings, neither containing the host.
    """
    wrong_slave_transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=(range(0x0000, 0x10000),),
    )
    wrong_slave_entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="wrong-slave-id-2", title="Wrong slave id",
    )
    wrong_slave_entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport",
        return_value=wrong_slave_transport,
    ):
        await hass.config_entries.async_setup(wrong_slave_entry.entry_id)
        await hass.async_block_till_done()

    refused_transport = FakeTransport(
        justice_registers_synthetic_complete,
        connect_errors=[TransportConnectionError("connection refused")],
    )
    refused_entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="refused", title="Refused",
    )
    refused_entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport",
        return_value=refused_transport,
    ):
        await hass.config_entries.async_setup(refused_entry.entry_id)
        await hass.async_block_till_done()

    wrong_slave_diag = await async_get_config_entry_diagnostics(hass, wrong_slave_entry)
    refused_diag = await async_get_config_entry_diagnostics(hass, refused_entry)

    assert wrong_slave_diag["entry"]["reason"] != refused_diag["entry"]["reason"]
    assert "ProbeFailedError" in wrong_slave_diag["entry"]["reason"]
    assert "TransportConnectionError" in refused_diag["entry"]["reason"]
    assert ENTRY_DATA[CONF_HOST] not in wrong_slave_diag["entry"]["reason"]
    assert ENTRY_DATA[CONF_HOST] not in refused_diag["entry"]["reason"]


async def test_diagnostics_on_a_dead_first_refresh_still_shows_the_probe(
    hass, justice_registers_synthetic_complete
):
    """A different failed-setup shape than the ones above: the probe itself
    succeeds completely (every block SUPPORTED), and the session dies on
    the coordinator's own first read cycle right after -- `entry.
    runtime_data` SURVIVES this (assigned before the first refresh ever
    runs, per `__init__.py`), pointing at a coordinator whose `probe_result`
    is real and whose `data` is still `None` (the first refresh never
    succeeded).

    Fix round 1: this used to assert `registers == {}` / `values == {}`
    here -- exactly the "raw registers discarded when they are the only
    data left" defect. The probe read all 178 registers across the 10
    declared blocks (15+8+19+23+24+24+16+15+10+24) before the session
    died; this now asserts the fallback to `probe_result.registers`
    actually surfaces them, plus `coordinator.last_exception` carrying the
    real cause (host redacted) since `entry.reason` is empty on this path.
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
    assert diag["coordinator"]["last_exception"] is not None
    assert "TransportConnectionError" in diag["coordinator"]["last_exception"]
    assert ENTRY_DATA[CONF_HOST] not in diag["coordinator"]["last_exception"]
    assert diag["support"]["0x0100"] == "supported"
    assert diag["probe_errors"] == {}
    # The probe's own snapshot survives even though the coordinator's own
    # refresh cycle never completed -- this is the whole point of the fix.
    assert diag["registers_source"] == "probe"
    assert len(diag["registers"]) == 178
    assert diag["registers"]["0x0100"] == 55
    assert diag["values"]["battery_soc"] == 55


async def test_diagnostics_redacts_the_host_from_probe_error_text(
    hass, justice_registers_synthetic_complete
):
    """Fix round 1, the redaction-leak finding itself: a stolen session
    (`TransportBusyError`, this hardware's most common real failure) embeds
    `self.host` directly in its message
    (`transport/solarman_v5.py`'s `_translate`). `entry.data["host"]` is
    redacted a few keys up in the SAME payload; the error text next to it
    must be too, or the redaction earns trust it does not deserve. Asserts
    the host does not appear ANYWHERE in the serialised payload -- not just
    that the `host` key itself is redacted, which is the assertion that
    would have missed this.
    """
    host = ENTRY_DATA[CONF_HOST]
    stolen = TransportBusyError(
        f"established session to {host}:8899 was lost or taken: stolen by another client"
    )
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[None] + [stolen, stolen] + [None] * 8,
    )
    entry = await setup_entry(hass, transport)
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["support"]["0x0200"] == "unknown"
    assert "0x0200" in diag["probe_errors"]
    # The message keeps its diagnostic value...
    assert "lost or taken" in diag["probe_errors"]["0x0200"]
    # ...but the host itself is gone, from this string specifically...
    assert host not in diag["probe_errors"]["0x0200"]
    assert "**REDACTED**" in diag["probe_errors"]["0x0200"]
    # ...and, the assertion that actually catches a leak anywhere else in
    # the payload (a new field added later that also embeds the host would
    # be caught here even if nobody remembers to add a field-specific
    # assertion for it):
    assert host not in json.dumps(diag)

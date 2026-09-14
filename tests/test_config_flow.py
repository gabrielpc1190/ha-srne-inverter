"""Config and options flow."""

from unittest.mock import patch, AsyncMock

import pytest
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntryState,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter.config_flow import STEP_USER_SCHEMA, SrneConfigFlow
from custom_components.srne_inverter.const import (
    CONF_COLD_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    CONF_WARM_INTERVAL,
    DOMAIN,
)
from custom_components.srne_inverter.logger_web import LoggerWebError
from custom_components.srne_inverter.transport.base import (
    TransportBusyError,
    TransportConnectionError,
    TransportProtocolError,
    TransportTimeoutError,
)
from tests.fake_transport import FakeTransport
from tests.test_init import setup_entry

USER_INPUT = {
    CONF_NAME: "Justice Inv 1",
    CONF_HOST: "192.168.188.240",
    CONF_PORT: 8899,
    CONF_SERIAL: "3548208972",
    CONF_SLAVE_ID: 1,
    CONF_SCAN_INTERVAL: 10,
}
VALIDATE = "custom_components.srne_inverter.config_flow.build_probe_transport"
FETCH = "custom_components.srne_inverter.config_flow.async_fetch_logger_serial"


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


async def test_user_flow_creates_the_entry(hass, justice_registers):
    transport = FakeTransport(justice_registers)
    with (
        patch(VALIDATE, return_value=transport),
        patch("custom_components.srne_inverter.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Justice Inv 1"
    assert result["data"][CONF_SERIAL] == "3548208972"
    assert result["options"][CONF_SCAN_INTERVAL] == 10
    assert transport.close_count >= 1


async def test_empty_serial_is_scraped_from_status_html(hass, justice_registers):
    transport = FakeTransport(justice_registers)
    with (
        patch(VALIDATE, return_value=transport),
        patch(FETCH, new=AsyncMock(return_value="3548208972")) as fetch,
        patch("custom_components.srne_inverter.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SERIAL: ""}
        )
    fetch.assert_awaited_once()
    assert result["data"][CONF_SERIAL] == "3548208972"


async def test_serial_scrape_failure_shows_an_error(hass):
    with patch(FETCH, new=AsyncMock(side_effect=LoggerWebError("401"))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SERIAL: ""}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_read_serial"}


async def test_connect_failure_shows_cannot_connect(hass, justice_registers):
    """Connect-phase failure (refused, unreachable, mistyped IP -- the single
    most likely user error in this form) maps to the generic
    TransportConnectionError, per Task 4's phase-aware translate() and the
    task-13 brief's Constraint 4.

    Deviation from the brief: the brief's own test used
    `FakeTransport(justice_registers, fail_connect=True)`. That keyword does
    not exist on FakeTransport (see tests/fake_transport.py's constructor and
    tests/test_init.py's own documented deviation, which fixed the identical
    defect one task earlier) -- replaced with the established
    `connect_errors` queue, `TransportConnectionError`, matching
    tests/test_init.py:test_setup_raises_not_ready_when_connect_fails
    exactly. Also renamed from the brief's `test_busy_logger_shows_
    cannot_connect`: a connect-phase failure is never actually "busy" in
    this taxonomy (TransportBusyError is reserved for a session stolen
    during a read/write, never observed at connect()) -- keeping "busy" in
    the name would perpetuate the exact misconception Constraint 4 warns
    against.
    """
    transport = FakeTransport(
        justice_registers,
        connect_errors=[TransportConnectionError("connection refused")],
    )
    with patch(VALIDATE, return_value=transport):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_wrong_slave_shows_invalid_slave(hass, justice_registers):
    transport = FakeTransport(
        justice_registers, read_errors=[TransportProtocolError("Empty")] * 4
    )
    with patch(VALIDATE, return_value=transport):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["errors"] == {"base": "invalid_slave"}


async def test_read_phase_busy_shows_logger_busy(hass, justice_registers):
    """Fix round 1, Finding 3: an established session stolen DURING the
    read phase is a positive signal (TransportBusyError specifically, not
    just any TransportConnectionError) and must not be folded into
    `cannot_connect` -- that message is reserved for a connection that never
    opened at all, which this is not: `connect()` above already succeeded.
    """
    transport = FakeTransport(
        justice_registers,
        read_errors=[TransportBusyError("established session was lost or taken")],
    )
    with patch(VALIDATE, return_value=transport):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["errors"] == {"base": "logger_busy"}


async def test_read_phase_timeout_shows_invalid_slave_not_cannot_connect(
    hass, justice_registers
):
    """Fix round 1, Finding 3's own concrete example: a wrong slave id on a
    silent bus does not always answer with a clean Modbus exception -- it
    can just time out (no NAK, pure silence) at the READ phase, after
    `connect()` already succeeded. `TransportTimeoutError` is a SUBCLASS of
    `TransportConnectionError`; before this fix, wrapping connect() and both
    reads in one try/except made this indistinguishable from a connect
    failure and reported `cannot_connect`, sending the user to check the
    IP/network when the slave id they typed was the only wrong thing on
    screen.
    """
    transport = FakeTransport(
        justice_registers,
        read_errors=[TransportTimeoutError("no reply")],
    )
    with patch(VALIDATE, return_value=transport):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["errors"] == {"base": "invalid_slave"}


async def test_serial_mismatch_shows_wrong_serial(hass, justice_registers):
    """Fix round 1, Finding 4: pysolarmanv5's own V5 frame validator
    (pysolarmanv5/pysolarmanv5.py's `_v5_frame_decoder`) raises this exact
    message when the device that answered is stamped with a different
    Solarman V5 logger serial than the one entered -- the SERIAL field is
    what's wrong here, not the slave id, and deserves its own message
    naming it instead of the generic `invalid_slave`, which never mentions
    the serial at all.
    """
    transport = FakeTransport(
        justice_registers,
        read_errors=[
            TransportProtocolError(
                "V5 frame contains incorrect data logger serial number"
            )
        ],
    )
    with patch(VALIDATE, return_value=transport):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["errors"] == {"base": "wrong_serial"}


async def test_duplicate_serial_aborts(hass, justice_registers):
    MockConfigEntry(domain=DOMAIN, unique_id="3548208972").add_to_hass(hass)
    with patch(VALIDATE, return_value=FakeTransport(justice_registers)):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_duplicate_serial_does_not_mutate_the_existing_entry(
    hass, justice_registers_synthetic_complete
):
    """Regression pin, Fix round 1, Finding 1 (the important one: it
    damages something that was working). The old code called
    `_abort_if_unique_id_configured(updates={CONF_HOST: host})`, which
    rewrites the LIVE entry's host as a side effect of aborting -- before
    any validation of the new host ever runs. Re-adding an already-
    configured serial with a mistyped host used to silently point a
    working, LOADED entry at the wrong address.

    Uses a REALLY loaded entry (test_init.setup_entry), not just
    MockConfigEntry.add_to_hass: a mock-only entry's `.data` would "prove"
    nothing here, since the bug is specifically about mutating an entry
    Home Assistant considers live. Measured against the pre-fix code before
    writing this test: it failed here, with `entry.data[CONF_HOST]` equal to
    the typo'd address instead of the original one.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    original_host = entry.data[CONF_HOST]
    assert entry.state is ConfigEntryState.LOADED

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.168.188.99"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == original_host


async def test_options_flow_updates_intervals(
    hass, justice_registers_synthetic_complete
):
    """Fix round 1, Finding 2: rewritten against a REALLY loaded entry
    (test_init.setup_entry), not just MockConfigEntry.add_to_hass. A
    mock-only entry never runs the real `async_setup_entry`, so
    `entry.update_listeners` stays empty -- which is exactly why the
    original version of this test stayed green even against a `/tmp` copy
    with `SrneOptionsFlow` rebased onto `OptionsFlowWithReload` (see
    test_options_flow_with_reload_raises_against_a_loaded_entry below,
    which pins that specific gap). This version also confirms the
    coordinator itself picks up the new scan_interval after the flow's own
    reload, matching the end-to-end check the Opus review ran by hand
    (10 s -> submit 20/120/600 -> coordinator.update_interval == 20).
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    assert entry.update_listeners

    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SCAN_INTERVAL: 20,
                CONF_WARM_INTERVAL: 120,
                CONF_COLD_INTERVAL: 600,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 20
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.scan_interval == 20


async def test_options_flow_with_reload_raises_against_a_loaded_entry(
    hass, justice_registers_synthetic_complete
):
    """Regression pin, Fix round 1, Finding 2. `SrneOptionsFlow` itself
    must stay a plain `OptionsFlow` -- that constraint is unconditional and
    this test never touches the shipped class. What it pins is that IF a
    future change rebased it onto `OptionsFlowWithReload` (the failure mode
    this whole finding is about: "modernising" to a class HA's own docs
    make sound like the natural choice), this suite would catch it: HA
    itself raises `ValueError` at `config_entries.py`'s
    `async_finish_flow`, once `automatic_reload` is True (the default for
    `OptionsFlowWithReload`) AND the entry has an update listener -- which
    ours does, from `async_setup_entry`, the moment it is genuinely loaded.

    A throwaway local subclass stands in for the regression rather than
    editing `config_flow.py`: swapping `SrneConfigFlow.async_get_options_flow`
    for the duration of this one test is enough to drive HA's own flow
    manager through the exact code path a real rebase would reach.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    assert entry.update_listeners  # the precondition the ValueError needs

    class _RegressedOptionsFlow(OptionsFlowWithReload):
        """Never shipped. Stands in for a future SrneOptionsFlow rebased
        onto OptionsFlowWithReload, purely to prove this suite would catch
        that regression against a genuinely loaded entry."""

        async def async_step_init(self, user_input=None):
            if user_input is not None:
                return self.async_create_entry(data=user_input)
            return self.async_show_form(
                step_id="init", data_schema=vol.Schema({})
            )

    with patch.object(
        SrneConfigFlow,
        "async_get_options_flow",
        staticmethod(lambda config_entry: _RegressedOptionsFlow()),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        with pytest.raises(ValueError, match="OptionsFlowWithReload"):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {}
            )


def test_port_outside_valid_range_is_rejected_by_the_schema():
    """Small fix, Fix round 1: CONF_PORT had no vol.Range, unlike slave_id
    and scan_interval in the same schema -- port 0 or 99999 used to reach
    _async_validate and fail later as a confusing network-level error
    instead of being refused in the form itself.
    """
    with pytest.raises(vol.Invalid):
        STEP_USER_SCHEMA({**USER_INPUT, CONF_PORT: 99999})
    with pytest.raises(vol.Invalid):
        STEP_USER_SCHEMA({**USER_INPUT, CONF_PORT: 0})
    STEP_USER_SCHEMA({**USER_INPUT, CONF_PORT: 8899})

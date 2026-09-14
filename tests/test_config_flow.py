"""Config and options flow."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter.const import (
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    DOMAIN,
)
from custom_components.srne_inverter.logger_web import LoggerWebError
from custom_components.srne_inverter.transport.base import (
    TransportConnectionError,
    TransportProtocolError,
)
from tests.fake_transport import FakeTransport

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


async def test_options_flow_updates_intervals(hass, justice_registers):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="3548208972", data=USER_INPUT,
        options={CONF_SCAN_INTERVAL: 10, "warm_interval": 60, "cold_interval": 300},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.srne_inverter.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SCAN_INTERVAL: 20, "warm_interval": 120, "cold_interval": 600},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 20

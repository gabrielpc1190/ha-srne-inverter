"""Config and options flow for srne_inverter.

Replaces the Task 8 placeholder (see git history) that existed only to
satisfy manifest.json's `"config_flow": true` while the real flow had not
been written yet.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_COLD_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    CONF_WARM_INTERVAL,
    DEFAULT_COLD_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLAVE_ID,
    DEFAULT_WARM_INTERVAL,
    DOMAIN,
    SOCKET_TIMEOUT,
)
from .logger_web import LoggerWebError, async_fetch_logger_serial
from .transport.base import (
    Transport,
    TransportConnectionError,
    TransportError,
)
from .transport.solarman_v5 import SolarmanV5Transport

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="SRNE Inverter"): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Optional(CONF_SERIAL, default=""): str,
        vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=247)
        ),
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=600)
        ),
    }
)


def build_probe_transport(
    host: str, serial: int, port: int, slave_id: int
) -> Transport:
    """Build the throwaway transport the flow uses to validate (patched in tests)."""
    return SolarmanV5Transport(
        host, serial, port=port, slave_id=slave_id, timeout=SOCKET_TIMEOUT
    )


class SrneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for one inverter behind one logger."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect connection details, then actually talk to the device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            serial_text = str(user_input.get(CONF_SERIAL, "")).strip()

            if not serial_text:
                try:
                    serial_text = await async_fetch_logger_serial(self.hass, host)
                except LoggerWebError as err:
                    _LOGGER.debug("Serial scrape failed for %s: %s", host, err)
                    errors["base"] = "cannot_read_serial"

            if not errors:
                if not serial_text.isdigit():
                    errors["base"] = "invalid_serial"
                else:
                    await self.async_set_unique_id(serial_text)
                    self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                    errors = await self._async_validate(
                        host,
                        int(serial_text),
                        user_input[CONF_PORT],
                        user_input[CONF_SLAVE_ID],
                    )

            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_HOST: host,
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_SERIAL: serial_text,
                        CONF_SLAVE_ID: user_input[CONF_SLAVE_ID],
                    },
                    options={
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                        CONF_WARM_INTERVAL: DEFAULT_WARM_INTERVAL,
                        CONF_COLD_INTERVAL: DEFAULT_COLD_INTERVAL,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def _async_validate(
        self, host: str, serial: int, port: int, slave_id: int
    ) -> dict[str, str]:
        """Open a real connection and read two known registers.

        Error mapping (task-13-brief's Constraint 4, verified against
        transport/solarman_v5.py's phase-aware `_translate()`): `connect()`
        NEVER raises `TransportBusyError` -- every connect-phase
        `NoSocketAvailableError` (refused, unreachable, DNS failure, a
        mistyped IP) becomes the plain `TransportConnectionError` caught
        below. `TransportBusyError` is a SUBCLASS of that, reserved for an
        established session discovered stolen during a later read/write --
        it is still caught by the same `except TransportConnectionError`
        branch here (correct: the flow only has one "cannot reach it" error
        message, `cannot_connect` -- see translations/en.json for why that
        message does not blame "another client" as its primary story).
        """
        transport = build_probe_transport(host, serial, port, slave_id)
        try:
            await transport.connect()
            await transport.read_holding(0x0014, 1)   # firmware word
            await transport.read_holding(0x0100, 1)   # battery SOC
        except TransportConnectionError as err:
            _LOGGER.debug("Cannot connect to %s: %s", host, err)
            return {"base": "cannot_connect"}
        except TransportError as err:
            # Empty / Acknowledge / IllegalDataAddress here means the slave id
            # is wrong: the logger answered but the inverter did not.
            _LOGGER.debug("Bad reply from %s slave %s: %s", host, slave_id, err)
            return {"base": "invalid_slave"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating %s", host)
            return {"base": "unknown"}
        finally:
            await transport.close()
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        """Return the options flow handler."""
        return SrneOptionsFlow()


class SrneOptionsFlow(OptionsFlow):
    """Tune the three polling intervals without re-entering the connection.

    Deliberately a plain `OptionsFlow`, NOT `OptionsFlowWithReload` (task-13
    brief's Constraint 2): `async_setup_entry` in `__init__.py` already
    registers `entry.add_update_listener(async_reload_entry)`. HA 2026.9's
    `ConfigEntriesFlowManager.async_finish_flow` raises `ValueError("Config
    entry update listeners should not be used with OptionsFlowWithReload")`
    (verified against this venv's installed config_entries.py, line ~3958)
    the instant an entry with ANY update listener completes an
    `OptionsFlowWithReload`. A plain `OptionsFlow` does not set
    `automatic_reload`, so that check never fires; `async_update_entry`
    still fires the registered update listener on its own (independent of
    `automatic_reload`) whenever the options actually changed, which is what
    drives `async_reload_entry` below.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=600)),
                vol.Required(
                    CONF_WARM_INTERVAL,
                    default=options.get(CONF_WARM_INTERVAL, DEFAULT_WARM_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Required(
                    CONF_COLD_INTERVAL,
                    default=options.get(CONF_COLD_INTERVAL, DEFAULT_COLD_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=30, max=86400)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

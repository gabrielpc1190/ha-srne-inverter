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
    TransportBusyError,
    TransportConnectionError,
    TransportError,
    TransportProtocolError,
)
from .transport.solarman_v5 import SolarmanV5Transport

_LOGGER = logging.getLogger(__name__)

# pysolarmanv5's own V5 frame validator raises exactly this text (via
# V5FrameError, translated to TransportProtocolError by
# transport/solarman_v5.py's _translate) when the device that answered is
# stamped with a different Solarman V5 logger serial than the one we sent --
# see pysolarmanv5/pysolarmanv5.py's _v5_frame_decoder. Fix round 1, Finding
# 4: distinguishing this from every other TransportProtocolError is what
# lets _async_validate return "wrong_serial" instead of the generic
# "invalid_slave", which never mentions the serial field at all.
_WRONG_SERIAL_MARKER = "data logger serial number"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="SRNE Inverter"): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
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
                    # Fix round 1, Finding 1: no `updates=` here. The old
                    # code passed `updates={CONF_HOST: host}`, which
                    # rewrites the LIVE entry's host as a side effect of
                    # aborting -- BEFORE `_async_validate` below ever runs,
                    # so a typo here (the classic re-add-to-check-settings
                    # mistake) silently pointed a working, LOADED entry at
                    # the wrong address and kicked off a reload that lands
                    # it in SETUP_RETRY, with nothing on screen connecting
                    # cause to effect. HA itself is deprecating exactly this
                    # combination for entries with an update listener (ours
                    # has one, since Task 8) -- `report_usage(...,
                    # breaks_in_ha_version="2026.12.0")` in the installed
                    # config_entries.py. This integration has no reconfigure
                    # step (Task 16+ scope, not this one): the only way to
                    # point an existing serial at a new host is to remove
                    # the entry and re-add it, which is safe because the
                    # serial becomes free the moment the old entry is gone.
                    self._abort_if_unique_id_configured()
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
        """Open a real connection, THEN read two known registers as a
        deliberately separate phase.

        Fix round 1, Finding 3: the original version of this method wrapped
        `connect()` and both reads in ONE try/except, catching
        `TransportConnectionError`. `TransportBusyError` and
        `TransportTimeoutError` are both SUBCLASSES of that -- so a read
        that failed with either (which, per Task 4's phase-aware
        `_translate()`, can only happen AFTER `connect()` already
        succeeded) was reported as `cannot_connect`, exactly the same as a
        connect that never opened at all. Concretely: a wrong slave id on a
        silent bus (no NAK, just silence) times out at the READ phase, and
        used to send the user to check the IP/network when the slave id
        they typed was the only wrong thing on screen; a session stolen
        mid-check used to be indistinguishable from a mistyped IP, even
        though at the read phase that is a POSITIVE signal, not a guess.

        Splitting into two try blocks fixes both: the first can only ever
        raise `TransportConnectionError` (per Task 4, `connect()` never
        raises `TransportBusyError`) and is the only place `cannot_connect`
        is returned from; the second sees `TransportBusyError` specifically
        (an honest "it answered, then someone took it" story --
        `logger_busy`) and folds every other read-phase `TransportError`
        (including a read-phase `TransportTimeoutError`) into
        `invalid_slave`, EXCEPT for one that gets its own message (Fix
        round 1, Finding 4): pysolarmanv5's own V5 frame validator raises a
        distinctive message when the device that answered is stamped with
        a different Solarman V5 logger serial than the one we sent --
        `_WRONG_SERIAL_MARKER` -- which means the SERIAL field is what's
        wrong, not the slave id, and deserves its own `wrong_serial`
        message naming that field instead of leaving the user to guess
        which one of two numbers on screen is the actual problem.
        """
        transport = build_probe_transport(host, serial, port, slave_id)
        try:
            try:
                await transport.connect()
            except TransportConnectionError as err:
                _LOGGER.debug("Cannot connect to %s: %s", host, err)
                return {"base": "cannot_connect"}

            try:
                await transport.read_holding(0x0014, 1)   # firmware word
                await transport.read_holding(0x0100, 1)   # battery SOC
            except TransportBusyError as err:
                _LOGGER.debug("Logger busy validating %s: %s", host, err)
                return {"base": "logger_busy"}
            except TransportProtocolError as err:
                if _WRONG_SERIAL_MARKER in str(err):
                    _LOGGER.debug(
                        "Serial mismatch validating %s: %s", host, err
                    )
                    return {"base": "wrong_serial"}
                _LOGGER.debug(
                    "Bad reply from %s slave %s: %s", host, slave_id, err
                )
                return {"base": "invalid_slave"}
            except TransportError as err:
                # Empty / Acknowledge / IllegalDataAddress / a read-phase
                # timeout (silence, not a NAK, but every bit as diagnostic
                # on hardware this confusable) here means the slave id is
                # wrong: the logger answered our connection, but the
                # inverter did not.
                _LOGGER.debug(
                    "Bad reply from %s slave %s: %s", host, slave_id, err
                )
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

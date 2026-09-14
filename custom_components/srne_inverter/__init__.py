"""The SRNE Inverter (Solarman V5) integration.

Setup order: build the transport, run the capability probe over the open
connection, hang a coordinator + transport off `entry.runtime_data`, run the
coordinator's first refresh, then forward to the platforms. A probe that
finds no supported block (a dead link, or this site's own confusable
`.240`/slave 1 vs. `.242`/slave 2 pair) raises `ConfigEntryNotReady` instead
of setting up a device with zero entities that would silently poll nothing
forever -- see `.coordinator.SrneCoordinator.async_probe`'s own docstring for
why that check lives there and not here.

THIS MODULE MAY import homeassistant (unlike registers.py/probe.py/
transport/*, which the guard tests in tests/test_core_is_ha_free.py hold to
zero homeassistant imports at any nesting).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

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
    PLATFORMS,
    SOCKET_TIMEOUT,
)
from .coordinator import SrneCoordinator
from .transport.base import Transport, TransportError
from .transport.solarman_v5 import SolarmanV5Transport

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SrneRuntimeData:
    """Everything the platforms need, hung off the config entry."""

    coordinator: SrneCoordinator
    transport: Transport
    added_field_keys: set[str] = dc_field(default_factory=set)


type SrneConfigEntry = ConfigEntry[SrneRuntimeData]

# Deviation from the task brief: the brief's own Step 5 code groups
# `from .services import async_setup_services` with the other top-of-file
# imports. `services.py` imports `.button`, and `button.py` (Task 12,
# unchanged) does `from . import SrneConfigEntry` at ITS OWN module top
# level -- if `.services` were imported before the `SrneConfigEntry` type
# alias above is actually bound in this module's namespace, that resolves
# to a circular ImportError ("cannot import name 'SrneConfigEntry' from
# partially initialized module") the moment Python reaches this file's own
# top. Placing the import here, after `SrneConfigEntry` is already bound,
# is the minimal fix: `entity.py` already avoids the same hazard the other
# way (leaving its own `entry` parameter untyped, "to avoid a circular
# import"), so this codebase already treats the hazard as real, not
# hypothetical.
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain services. Entry-agnostic: these three services
    (read_register, write_register, reprobe) resolve their own config entry
    from the device_id in each call, so they only need to be registered
    once per Home Assistant instance, not once per config entry.
    """
    async_setup_services(hass)
    return True


def build_transport(entry: SrneConfigEntry) -> Transport:
    """Create the transport for this entry.

    A free function, not a method, specifically so a test can `patch(
    "custom_components.srne_inverter.build_transport", return_value=fake)`
    and exercise async_setup_entry/async_unload_entry/async_reload_entry
    against `tests.fake_transport.FakeTransport` without ever opening a real
    socket -- the no-network-in-tests rule (the harness allows 127.0.0.1,
    which would silently open a REAL socket) makes that patch point load-
    bearing, not just a convenience.
    """
    return SolarmanV5Transport(
        entry.data[CONF_HOST],
        int(entry.data[CONF_SERIAL]),
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        slave_id=entry.data.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
        timeout=SOCKET_TIMEOUT,
    )


async def async_setup_entry(hass: HomeAssistant, entry: SrneConfigEntry) -> bool:
    """Probe the unit, start the coordinator and forward the platforms.

    Safety net this function relies on but does not itself set up: building
    `SrneCoordinator` below, with `config_entry=entry` (Task 7), makes
    `DataUpdateCoordinator.__init__` call
    `entry.async_on_unload(self.async_shutdown)` on our behalf -- and our own
    `SrneCoordinator.async_shutdown` awaits `self.transport.close()`. That
    registration exists from the moment the coordinator object is
    constructed, i.e. strictly BEFORE `async_probe()` ever opens the
    connection. Home Assistant's own config-entry setup machinery
    (`ConfigEntry._ConfigEntry__async_setup_with_context`, verified against
    this venv's installed homeassistant==2026.9.2) runs every
    `entry.async_on_unload(...)` callback unconditionally whenever this
    function does not return `True` -- `finally: if not result: await
    self._async_process_on_unload(hass)` -- regardless of which line raised
    or what exception type. This matters because `ConfigEntry.async_unload`
    (and therefore this module's OWN `async_unload_entry`) is only ever
    called by Home Assistant once an entry reaches `LOADED`; an entry that
    never gets there (e.g. a `RuntimeError` out of
    `async_forward_entry_setups`, below, after the probe already connected)
    would otherwise leave the logger's one TCP slot held with no code path
    left to release it -- locking out this integration's own retry, the
    standalone CLI probe and Gabriel's own justice_watch.py simultaneously.
    Confirmed by reverting the coordinator's `config_entry=entry` and
    watching `test_setup_failure_after_connecting_still_closes_the_socket`
    go red. `close()` being idempotent on both transports is what makes it
    safe for this same callback to also fire (harmlessly) after the explicit
    `await transport.close()` in the probe's `except` block below, or after
    `async_unload_entry`'s own `coordinator.async_shutdown()` call on a
    normal, successful unload.
    """
    transport = build_transport(entry)

    coordinator = SrneCoordinator(
        hass,
        entry,
        transport,
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        warm_interval=entry.options.get(CONF_WARM_INTERVAL, DEFAULT_WARM_INTERVAL),
        cold_interval=entry.options.get(CONF_COLD_INTERVAL, DEFAULT_COLD_INTERVAL),
    )

    try:
        await coordinator.async_probe()
    except TransportError as err:
        # coordinator.async_probe() already closes the transport itself on a
        # ProbeFailedError (its own Fix round 1, Finding 12) -- the case
        # where every block ends UNSUPPORTED/UNKNOWN, e.g. this site's own
        # confusable `.240`/slave 1 vs. `.242`/slave 2 pair: connect()
        # succeeds fine there, and it is probe() itself that fails. This
        # `await transport.close()` is still here, unconditionally, for the
        # OTHER TransportError shape -- connect() itself failing (refused,
        # unreachable, or another client's TCP session already occupying
        # the logger's one slot; Task 4's phase-aware translate() maps EVERY
        # connect-phase NoSocketAvailableError to plain
        # TransportConnectionError, never TransportBusyError, which is
        # reserved for an ESTABLISHED session discovered stolen during a
        # later read/write) is raised from async_probe() before its own
        # try/except around probe() is ever reached, so nothing upstream has
        # closed anything yet in that case. close() is idempotent either
        # way. Surfacing `type(err).__name__` (not just `str(err)`) matters:
        # it is how the config entry's own SETUP_RETRY reason can be told
        # apart from "the inverter isn't replying" without enabling debug
        # logging -- Task 7 already keeps this distinction in its own
        # UpdateFailed message, for the same reason.
        await transport.close()
        raise ConfigEntryNotReady(
            f"cannot probe {entry.data[CONF_HOST]}: {type(err).__name__}: {err}"
        ) from err

    entry.runtime_data = SrneRuntimeData(coordinator=coordinator, transport=transport)

    # Raises ConfigEntryNotReady itself if this first cycle fails (HA's own
    # behaviour, verified against DataUpdateCoordinator.
    # async_config_entry_first_refresh in this venv) -- a TransportError here
    # is already handled by the coordinator's own backoff bookkeeping
    # (_register_failure closes the transport before UpdateFailed reaches
    # this call), so there is nothing further to close in that path either.
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # This entry now has an update listener. Task 13's options flow MUST use
    # a plain `OptionsFlow`, never `OptionsFlowWithReload` -- HA 2026.9
    # raises `ValueError("Config entry update listeners should not be used
    # with OptionsFlowWithReload")` from `async_finish_flow` (verified
    # against the installed config_entries.py) the instant an entry with
    # ANY update listener completes such a flow. `async_reload_entry` below
    # already does the reload this integration needs.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SrneConfigEntry) -> bool:
    """Unload the platforms and free the logger's single TCP slot.

    Only reached by Home Assistant when the entry is `LOADED` (see
    `async_setup_entry`'s docstring for the state this depends on) -- the
    coordinator's own `entry.async_on_unload(self.async_shutdown)`
    registration (from its constructor, Task 7) is what covers every OTHER
    state a failed setup could have left the entry in.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.coordinator.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: SrneConfigEntry) -> None:
    """Reload after the options flow changed the polling intervals.

    `hass.config_entries.async_reload` unloads (freeing the transport, see
    `async_unload_entry`) then re-runs `async_setup_entry` from scratch --
    a fresh `SrneCoordinator` picks up the new `entry.options` values, and
    `build_transport` is called again (patched to the same fake in tests, a
    new real transport otherwise).
    """
    await hass.config_entries.async_reload(entry.entry_id)

"""Diagnostics for a configured inverter.

This is the artefact that leaves the site: Gabriel pastes it into a support
conversation when something is wrong with a client's unit. It exists
because this project's whole design is "do not assume every unit is
identical" -- diagnostics is where that pays off, by exporting the per-unit
probe result (which blocks THIS unit actually answered SUPPORTED/
UNSUPPORTED/UNKNOWN -- see `.probe.BlockSupport`/`.probe.ProbeResult`,
recomputed fresh at every setup and never persisted to disk) alongside the
raw register values next to their decoded ones -- a reader looking only at
a decoded value cannot tell a scale error from a device fault; the raw
value is what lets them tell those apart.

THIS MODULE MAY import homeassistant (unlike registers.py/probe.py/
transport/*, which the guard tests in tests/test_core_is_ha_free.py hold to
zero homeassistant imports at any nesting -- diagnostics.py is deliberately
not in that guard's scanned list).

`entry.runtime_data` (see `custom_components.srne_inverter.SrneRuntimeData`)
is only assigned once the capability probe has succeeded
(`async_setup_entry`); it survives a LATER failure too -- a dead first
refresh (`SETUP_RETRY`) or a platform-forwarding bug (`SETUP_ERROR`) both
leave it pointing at a coordinator whose transport has already been
closed, but whose probe result (and, for the first-refresh-failure case,
nothing yet from `coordinator.data`, which is still `None`) is exactly what
a support session needs. It does NOT exist at all if the probe or the
connect itself never got that far (this site's own confusable `.240`/
slave 1 vs. `.242`/slave 2 pair, or the logger refusing the connection) --
and that is precisely the case someone is most likely downloading
diagnostics to understand, so this function must not raise on it.

Deliberately NOT gated on a bare `hasattr(entry, "runtime_data")` check:
that attribute can be `True` against a dead coordinator (SETUP_RETRY/
SETUP_ERROR after the probe already succeeded), so its mere presence must
never be read as "this entry is healthy". `entry.state` is included in the
payload verbatim so a reader can tell live data from stale/absent data
without guessing from what else is (or isn't) populated; accessing
`entry.runtime_data` itself is still guarded, but with `try/except
AttributeError` rather than a `hasattr` pre-check used as a health signal.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import SrneConfigEntry
from .const import CONF_SERIAL

# Redacted: the host (a LAN address, but still site-identifying) and the
# unit's serial number. Deliberately NOT redacted: `slave_id` (meaningless
# without the host), the polling intervals, and every probe/register/value
# field below -- none of those identify the site or its owner, and the
# raw register dump is the entire reason this file exists. The logger's own
# web UI basic-auth credentials (admin/admin) are hardcoded in
# `logger_web.py`, never stored in `entry.data`, so there is nothing to
# redact for them here.
TO_REDACT = {CONF_HOST, CONF_SERIAL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SrneConfigEntry
) -> dict[str, Any]:
    """Dump the entry, the probe's support map, raw registers and values."""
    diagnostics: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "state": entry.state.value,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {},
        "support": {},
        "probe_errors": {},
        "registers": {},
        "values": {},
    }

    try:
        coordinator = entry.runtime_data.coordinator
    except AttributeError:
        # The probe or the connect itself never succeeded -- see the module
        # docstring. `entry.state` above already says why; there is nothing
        # else to add, and every other section stays at its empty default.
        return diagnostics

    probe_result = coordinator.probe_result
    data = coordinator.data

    diagnostics["coordinator"] = {
        "connection_enabled": coordinator.connection_enabled,
        "failure_count": coordinator.failure_count,
        "last_update_success": coordinator.last_update_success,
        "scan_interval": coordinator.scan_interval,
        "warm_interval": coordinator.warm_interval,
        "cold_interval": coordinator.cold_interval,
    }
    # probe_result is, in practice, always set by the time runtime_data
    # exists (async_probe() populates it before async_setup_entry ever
    # assigns entry.runtime_data) -- the `if probe_result` guards stay
    # anyway, defensively, rather than assuming that ordering can never
    # change under this module's feet.
    diagnostics["support"] = probe_result.as_diagnostics() if probe_result else {}
    diagnostics["probe_errors"] = (
        {f"0x{addr:04X}": text for addr, text in probe_result.errors.items()}
        if probe_result
        else {}
    )
    # data (coordinator.data, a SrneData) is None until the coordinator's
    # first refresh cycle actually succeeds -- e.g. still None on the
    # SETUP_RETRY produced when the probe succeeds but the session dies on
    # the very next read. Raw registers and decoded values are absent then;
    # the support map and probe_errors above are not, and are frequently
    # the whole answer to "did this unit ever get probed at all".
    diagnostics["registers"] = (
        {f"0x{addr:04X}": value for addr, value in sorted(data.registers.items())}
        if data
        else {}
    )
    diagnostics["values"] = dict(data.values) if data else {}
    return diagnostics

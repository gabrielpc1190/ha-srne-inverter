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
closed, but whose probe result is exactly what a support session needs. It
does NOT exist at all if the probe or the connect itself never got that far
(this site's own confusable `.240`/slave 1 vs. `.242`/slave 2 pair, or the
logger refusing the connection) -- and that is precisely the case someone
is most likely downloading diagnostics to understand, so this function must
not raise on it.

Fix round 1 (Opus review) added three things the first cut was missing,
all in service of the same goal -- a reader who was not in this
conversation must be able to tell, from the file alone, what went wrong:

1. **Redaction leak.** `async_redact_data` only touches dict VALUES keyed
   by `TO_REDACT` -- it cannot reach inside a free-form error STRING.
   `TransportConnectionError`/`TransportBusyError` (`transport/
   solarman_v5.py`'s `_translate`) both embed `self.host` directly in their
   text (e.g. "established session to 192.168.188.240:8899 was lost or
   taken"), and this package's own `ConfigEntryNotReady` message
   (`__init__.py`) does the same. Both used to land in `probe_errors`,
   `entry.reason` and `coordinator.last_exception` verbatim -- the exact
   host `entry.data["host"]` was redacted a few keys up would reappear a
   few keys down, in the one place someone most needs the file scrubbed
   (a stolen session is this hardware's single most common real failure).
   `_redact_text` below scrubs it out of every string this module exports.
2. **No cause for a failed setup.** A wrong slave id and a refused/busy
   logger used to produce byte-identical payloads once `entry.title` is
   ignored -- yet the cause was sitting in two places this file wasn't
   reading: `entry.reason` (set by Home Assistant's own config-entry
   machinery whenever `ConfigEntryNotReady` is raised -- meaningful for a
   probe/connect failure, since `__init__.py`'s own `except TransportError`
   branch raises it WITH a message; usually empty for a dead-first-refresh,
   since `DataUpdateCoordinator._async_config_entry_first_refresh`
   synthesizes a bare `ConfigEntryNotReady` with no message of its own) and
   `coordinator.last_exception` (the real cause in exactly that second
   case -- an `UpdateFailed` wrapping the underlying `TransportError`).
   Exporting both covers each failure shape's own source of truth.
3. **Raw registers discarded exactly when they are the only data left.**
   `coordinator.data` is `None` until the coordinator's OWN first refresh
   cycle succeeds -- but a probe that read every block completely, followed
   by a session that died on the very next read, leaves `probe_result.
   registers` fully populated (every register the probe itself read) while
   `coordinator.data` stays `None` forever (that entry never reaches
   `LOADED`, so no further refresh happens either). This file used to
   report an empty `registers`/`values` in exactly that case. It now falls
   back to `probe_result.registers` (decoded via `registers.decode()`
   locally, the same function `coordinator.py` itself uses) when
   `coordinator.data` is unavailable -- `registers_source` says which one a
   reader is looking at.

Also fixed: `values["inverter_serial"]` (the INVERTER's own serial, decoded
from a `registers.FieldKind.SERIAL` field) used to ship unmasked while
`entry.data["serial"]` (the LOGGER's serial) was fully redacted a few keys
up -- an inconsistency, not a deliberate choice. It is now masked to its
last 4 characters, and a `generated_at` timestamp plus `registers_source`
make clear that the register/value dump below is a CUMULATIVE snapshot
across three independently-scheduled tiers (HOT every `scan_interval` s,
WARM every `warm_interval` s, COLD every `cold_interval` s -- see the
`coordinator` section's own interval fields), not everything read at the
same instant this file was generated.

Deliberately NOT gated on a bare `hasattr(entry, "runtime_data")` check:
that attribute can be `True` against a dead coordinator (SETUP_RETRY/
SETUP_ERROR after the probe already succeeded), so its mere presence must
never be read as "this entry is healthy". `entry.state` is included in the
payload verbatim so a reader can tell live data from stale/absent data
without guessing from what else is (or isn't) populated; accessing
`entry.runtime_data` itself is a plain `getattr(entry, "runtime_data",
None)` -- a value to branch on, not an exception to catch.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import SrneConfigEntry
from . import registers as R
from .const import CONF_SERIAL

# Redacted: the host (a LAN address, but still site-identifying) and the
# LOGGER's own serial number. Deliberately NOT redacted: `slave_id`
# (meaningless without the host), the polling intervals, and every probe/
# register/value field below -- none of those identify the site or its
# owner, and the raw register dump is the entire reason this file exists.
# The logger's own web UI basic-auth credentials (admin/admin) are
# hardcoded in `logger_web.py`, never stored in `entry.data`, so there is
# nothing to redact for them here. The INVERTER's own serial
# (`values["inverter_serial"]`) is a decoded VALUE, not a dict key
# `async_redact_data` can reach -- see `_mask_serial`/`_redact_values`
# below, and `_redact_text` for the free-form error strings that can embed
# the host the same way `TO_REDACT` embeds it as a dict value.
TO_REDACT = {CONF_HOST, CONF_SERIAL}

# The one field kind that decodes to an identifying string today
# (registers.FIELDS has exactly one: "inverter_serial"). Computed from the
# field table rather than hardcoding the key, so a future SERIAL-kind field
# is covered automatically instead of silently shipping unmasked.
_SERIAL_VALUE_KEYS = frozenset(f.key for f in R.FIELDS if f.kind is R.FieldKind.SERIAL)


def _redact_text(text: str | None, host: str) -> str | None:
    """Scrub this entry's own host out of a free-form error string.

    `async_redact_data` cannot do this -- it only replaces dict VALUES
    keyed by `TO_REDACT`, and these are exception messages, not dict
    values. Plain substring replacement is enough: `host` is the exact
    string `build_transport` passed into `SolarmanV5Transport(...)`, which
    is also the exact string every transport-layer error message embeds
    (`transport/solarman_v5.py`'s `_translate`).
    """
    if text is None or not host:
        return text
    return text.replace(host, "**REDACTED**")


def _mask_serial(value: str) -> str:
    """Keep the last 4 characters, mask the rest with `*`.

    Enough to cross-reference against a known unit in a support
    conversation without shipping the whole serial in the clear -- same
    reasoning `TO_REDACT` already applies to the logger's own serial.
    """
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def _redact_values(values: dict[str, object]) -> dict[str, object]:
    """Copy `values`, masking every `FieldKind.SERIAL`-decoded field."""
    redacted = dict(values)
    for key in _SERIAL_VALUE_KEYS:
        value = redacted.get(key)
        if isinstance(value, str):
            redacted[key] = _mask_serial(value)
    return redacted


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SrneConfigEntry
) -> dict[str, Any]:
    """Dump the entry, the probe's support map, raw registers and values."""
    host = entry.data.get(CONF_HOST) or ""
    diagnostics: dict[str, Any] = {
        "generated_at": dt_util.utcnow().isoformat(),
        "entry": {
            "title": entry.title,
            "state": entry.state.value,
            # Set by Home Assistant's own config-entry machinery whenever
            # ConfigEntryNotReady is raised -- meaningful for a probe/
            # connect failure (this package's own message, WITH text);
            # usually empty for a dead-first-refresh (HA synthesizes a bare
            # ConfigEntryNotReady there -- see coordinator["last_exception"]
            # below for that case's real cause instead).
            "reason": _redact_text(entry.reason, host),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {},
        "support": {},
        "probe_errors": {},
        # "none": no data was ever read (probe/connect failed outright).
        # "probe": the probe read every block once, but the coordinator's
        # own refresh cycle never succeeded, so this is a one-shot snapshot.
        # "coordinator": the normal case -- cumulative, tiered live data.
        "registers_source": "none",
        "registers": {},
        "values": {},
    }

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        # The probe or the connect itself never succeeded -- see the
        # module docstring. entry["reason"] above already says why (a
        # probe/connect failure is the one case that reliably has a
        # message); there is nothing else to add, and every other section
        # stays at its empty default.
        return diagnostics

    coordinator = runtime_data.coordinator
    probe_result = coordinator.probe_result
    data = coordinator.data

    diagnostics["coordinator"] = {
        "connection_enabled": coordinator.connection_enabled,
        "failure_count": coordinator.failure_count,
        "last_update_success": coordinator.last_update_success,
        "scan_interval": coordinator.scan_interval,
        "warm_interval": coordinator.warm_interval,
        "cold_interval": coordinator.cold_interval,
        # The dead-first-refresh case's own source of truth: the probe
        # succeeded (so entry["reason"] is empty), but the very next read
        # failed -- this is where that failure's real cause lives.
        "last_exception": (
            _redact_text(str(coordinator.last_exception), host)
            if coordinator.last_exception is not None
            else None
        ),
    }
    # probe_result is, in practice, always set by the time runtime_data
    # exists (async_probe() populates it before async_setup_entry ever
    # assigns entry.runtime_data) -- the `if probe_result` guards stay
    # anyway, defensively, rather than assuming that ordering can never
    # change under this module's feet.
    diagnostics["support"] = probe_result.as_diagnostics() if probe_result else {}
    diagnostics["probe_errors"] = (
        {
            f"0x{addr:04X}": _redact_text(text, host)
            for addr, text in probe_result.errors.items()
        }
        if probe_result
        else {}
    )

    if data is not None:
        # The normal case: the coordinator's own cumulative, tiered
        # register snapshot (never purged when a block is later
        # reclassified UNSUPPORTED mid-poll -- see task-10-report.md's own
        # note that this file explicitly depends on that).
        diagnostics["registers"] = {
            f"0x{addr:04X}": value for addr, value in sorted(data.registers.items())
        }
        diagnostics["values"] = _redact_values(dict(data.values))
        diagnostics["registers_source"] = "coordinator"
    elif probe_result is not None and probe_result.registers:
        # The coordinator's own refresh cycle never succeeded (dead first
        # refresh), but the probe itself already read every SUPPORTED
        # block once -- that snapshot is the only data this unit ever
        # produced, and it must not be reported as empty.
        diagnostics["registers"] = {
            f"0x{addr:04X}": value
            for addr, value in sorted(probe_result.registers.items())
        }
        diagnostics["values"] = _redact_values(R.decode(probe_result.registers))
        diagnostics["registers_source"] = "probe"

    return diagnostics

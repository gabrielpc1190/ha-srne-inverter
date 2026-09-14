"""Placeholder config flow, fully implemented in a later task.

Not part of this task's own file list -- created here because
`manifest.json` already declares `"config_flow": true` (Task 1) and Home
Assistant's config-entry setup machinery refuses to proceed past
`integration.async_get_platform("config_flow")` for ANY domain declared that
way if the module does not exist and importably register a `ConfigFlow`
handler -- verified directly against this venv's installed
homeassistant==2026.9.2 (`ConfigEntry._ConfigEntry__async_setup_with_context`
sets `SETUP_ERROR` and returns before ever calling `async_setup_entry` on
`ImportError`; `ConfigEntry.async_migrate` separately requires
`config_entries.HANDLERS[DOMAIN]` to be registered with a matching
`VERSION`/`MINOR_VERSION`, or setup ends in `MIGRATION_ERROR` instead).
Without this stub, no entry ever reaches `async_setup_entry` at all, which
would make Task 8's own tests -- and every later task's, since
`tests/test_init.py`'s `setup_entry()` helper is what they build on -- fail
for a reason that has nothing to do with `__init__.py` itself.

Task 13 replaces this with the real user step (host/port/serial/slave id,
scraping the serial off the logger's own status page) and the options flow
for the three polling intervals.
"""

from __future__ import annotations

from homeassistant import config_entries

from .const import DOMAIN


class SrneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Placeholder handler -- see the module docstring."""

    VERSION = 1
    MINOR_VERSION = 1

"""Domain-level services: raw register access and on-demand reprobe.

THIS MODULE MAY import homeassistant (unlike registers.py/probe.py/
transport/*, which the guard tests in tests/test_core_is_ha_free.py hold to
zero homeassistant imports at any nesting).

These are the sharpest tools in this integration. `read_register` is inert
(no side effects); `reprobe` reuses the already-guarded `async_reprobe` free
function from `button.py` (it refuses outright while the connection is
paused -- see that module's Fix round 1, Finding 1 -- and this module MUST
keep calling that guarded path, never `coordinator.async_probe()` directly).
`write_register` writes an arbitrary raw value to an arbitrary register,
bypassing every WriteSpec bound the number/select entities enforce
(registers.py's `encode()` is never consulted). Two independent safety nets
apply to every write, in order:

1. `_refuse_known_unwritable` (this module, below) refuses BEFORE any wire
   traffic for the handful of addresses this project has concrete, verified
   firmware facts about (a write this firmware is known to reject outright,
   or an address verified absent on this firmware entirely). This does NOT
   make the service safe in general -- it has none of the number/select
   entities' per-parameter range checks, by design (it is the escape
   hatch): every OTHER address is sent to the device exactly as given.
2. `SrneCoordinator.async_write_raw` (coordinator.py, Task 7) re-reads the
   register after writing and raises `HomeAssistantError` if the read-back
   does not match, or if the transport itself reports a rejection
   (`InvalidRegisterValueError`/`UnsupportedRegisterError`, both
   `TransportError` subclasses) -- this is what catches every address NOT
   covered by (1), including one this project has no prior facts about at
   all. Neither net is silent: a refused or unverifiable write always
   raises, it never reports success without having genuinely happened.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .button import async_reprobe
from .const import (
    DOMAIN,
    SERVICE_READ_REGISTER,
    SERVICE_REPROBE,
    SERVICE_WRITE_REGISTER,
)

ATTR_DEVICE_ID = "device_id"
ATTR_ADDRESS = "address"
ATTR_COUNT = "count"
ATTR_VALUE = "value"

# Verified absent on firmware V8.18.006 (Casa Justice inverter 1; design spec
# docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md
# Section 3) -- the SAME facts tests/fake_transport.py's DEFAULT_UNSUPPORTED
# encodes for the test double (test_services.py's
# test_known_facts_match_the_fake_transports_ground_truth pins the two
# staying in sync). Deliberately a SEPARATE literal here, not imported from
# tests/: production code must not depend on the test tree, and this
# integration's real runtime behaviour must not depend on FakeTransport
# either. Every one of Gabriel's units (Casa Justice x2, Yoga x6) is the same
# base SRNE hardware/firmware family per this repo's own CLAUDE.md -- if a
# future unit turns out to differ, the write still fails safely (this is a
# fail-FAST guard, not the only guard; see the module docstring's net 2).
KNOWN_ABSENT_ON_THIS_FIRMWARE: tuple[range, ...] = (
    range(0x0112, 0x0200),
    range(0xE21F, 0xF02C),
    range(0xE03A, 0xE100),
)

# Writes the firmware answers IllegalDataValue to, verified at Casa Justice,
# firmware V8.18.006 (see registers.py's own comment on 0xE20B/0xE20F, and
# tests/fake_transport.py's WRITE_REJECTED for the mirrored test-double
# facts). 0xE039 is deliberately NOT in KNOWN_ABSENT_ON_THIS_FIRMWARE above:
# it is a real, existing, READABLE register that only refuses writes -- the
# two sets are disjoint on purpose, matching what a real Modbus device can
# actually answer (an address cannot be simultaneously "does not exist" and
# "exists but refuses this specific write").
KNOWN_WRITE_REJECTED: frozenset[int] = frozenset({0xE20F, 0xE20B, 0xE21D, 0xE039})


def _address(value: Any) -> int:
    """Accept 256, '256' or '0x0100'."""
    if isinstance(value, int):
        parsed = value
    else:
        parsed = int(str(value), 0)
    if not 0 <= parsed <= 0xFFFF:
        raise vol.Invalid(f"register address out of range: {value}")
    return parsed


READ_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_ADDRESS): _address,
        vol.Optional(ATTR_COUNT, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=64)
        ),
    }
)
WRITE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_ADDRESS): _address,
        vol.Required(ATTR_VALUE): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    }
)
REPROBE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})


def _entry_for_device(hass: HomeAssistant, device_id: str):
    """Resolve a device_id to this integration's LOADED config entry.

    Gates on `entry.state is ConfigEntryState.LOADED`, never on
    `hasattr(entry, "runtime_data")`: `runtime_data` survives a failed setup
    (present in both SETUP_RETRY and SETUP_ERROR, pointing at a coordinator
    whose transport is already closed) and is only cleared by Home
    Assistant on a *successful* unload -- see task-8-report.md's public
    interface section. A service call against a device whose entry just
    dropped into SETUP_RETRY (the logger went down between polls) is a
    realistic thing for a user or automation to do, and must get a clean
    "not loaded" error, not an attempt to operate a dead coordinator.
    """
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(
            f"unknown device_id {device_id}",
            translation_domain=DOMAIN,
            translation_key="unknown_device",
            translation_placeholders={"device_id": device_id},
        )
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and entry.state is ConfigEntryState.LOADED
        ):
            return entry
    raise ServiceValidationError(
        f"device {device_id} has no loaded {DOMAIN} config entry",
        translation_domain=DOMAIN,
        translation_key="entry_not_loaded",
        translation_placeholders={"device_id": device_id},
    )


def _refuse_known_unwritable(address: int) -> None:
    """Refuse a write, before any wire traffic, to an address this project
    has concrete, verified firmware facts about.

    Deliberately narrow: this is NOT a general safety net (write_register
    has none, by design -- it is the escape hatch the number/select
    entities' own WriteSpec bounds do not apply to). It only refuses the
    handful of addresses independently known, from this exact firmware
    (verified at Casa Justice, V8.18.006), to be either write-rejected or
    entirely absent -- attempting either would be a guaranteed, wasted
    round trip to the device at best, and the whole reason this check
    exists is the brief's own framing: a service that only discovers this
    via a generic transport exception after the fact is not wrong, but a
    clearer, immediate refusal is strictly better for both the operator and
    the device.
    """
    if address in KNOWN_WRITE_REJECTED:
        raise ServiceValidationError(
            f"0x{address:04X} is known to reject writes on this firmware "
            "(verified at Casa Justice, V8.18.006) -- refusing before "
            "sending anything to the device",
            translation_domain=DOMAIN,
            translation_key="write_rejected_known",
            translation_placeholders={"address": f"0x{address:04X}"},
        )
    if any(address in span for span in KNOWN_ABSENT_ON_THIS_FIRMWARE):
        raise ServiceValidationError(
            f"0x{address:04X} falls inside a range verified absent on this "
            "firmware (V8.18.006) -- refusing before sending anything to "
            "the device",
            translation_domain=DOMAIN,
            translation_key="write_absent_block",
            translation_placeholders={"address": f"0x{address:04X}"},
        )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the domain services once (entry-agnostic, called from
    async_setup -- these are NOT per-config-entry, and are registered even
    before any entry is loaded)."""

    async def handle_read(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data[ATTR_DEVICE_ID])
        address = call.data[ATTR_ADDRESS]
        values = await entry.runtime_data.coordinator.async_read_raw(
            address, call.data[ATTR_COUNT]
        )
        return {"address": f"0x{address:04X}", "values": values}

    async def handle_write(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data[ATTR_DEVICE_ID])
        address = call.data[ATTR_ADDRESS]
        value = call.data[ATTR_VALUE]
        _refuse_known_unwritable(address)
        read_back = await entry.runtime_data.coordinator.async_write_raw(address, value)
        return {
            "address": f"0x{address:04X}",
            "written": value,
            "readback": read_back,
        }

    async def handle_reprobe(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data[ATTR_DEVICE_ID])
        # async_reprobe (button.py) is the ONE shared, guarded entry point --
        # it refuses outright while the connection is paused (Task 12 Fix
        # round 1, Finding 1). Calling coordinator.async_probe() directly
        # here would route around that guard and reopen a logger the user
        # explicitly told the integration to let go of.
        return await async_reprobe(hass, entry)

    hass.services.async_register(
        DOMAIN, SERVICE_READ_REGISTER, handle_read,
        schema=READ_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_WRITE_REGISTER, handle_write,
        schema=WRITE_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REPROBE, handle_reprobe,
        schema=REPROBE_SCHEMA, supports_response=SupportsResponse.ONLY,
    )

"""Domain services operating on a device_id: read_register, write_register,
reprobe.

Fixture deviation, same root cause as every other runtime task since Task 8:
`coordinator.async_probe()` (run inside `setup_entry()`) always probes every
one of `registers.BLOCKS`' 10 blocks, and the raw recorded `justice_registers`
fixture has real capture gaps inside 5 of them. Every test below that runs a
real setup uses `justice_registers_synthetic_complete` instead -- see
tests/conftest.py's own docstring, and tests/test_init.py's module docstring,
which hit the identical issue first.

`enable_custom_integrations` is requested explicitly by every test that calls
`hass`, not relied on via autouse: `tests/test_init.py` defines an
`autouse=True` wrapper fixture around it, but pytest autouse fixtures are
scoped to the file they are declared in -- it does not reach across into this
file.
"""

import json
import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter import services as services_module
from custom_components.srne_inverter.const import CONF_SERIAL, CONF_SLAVE_ID, DOMAIN
from custom_components.srne_inverter.services import (
    KNOWN_ABSENT_ON_THIS_FIRMWARE,
    KNOWN_WRITE_REJECTED,
)
from custom_components.srne_inverter.transport.base import (
    TransportConnectionError,
    TransportTimeoutError,
)
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport, WRITE_REJECTED
from tests.test_init import ENTRY_OPTIONS, setup_entry

# Casa Justice's real second unit (CLAUDE.md) -- used only as fixture data to
# build a genuinely SEPARATE config entry/device for the "targets the right
# device" test below; no connection is ever attempted to either real IP.
INVERTER_2_DATA = {
    CONF_HOST: "192.168.188.242",
    CONF_PORT: 8899,
    CONF_SERIAL: "3548738877",
    CONF_SLAVE_ID: 2,
}


async def device_id_for(hass, entry) -> str:
    """Deviation from the brief: `dr.async_get(hass).async_get_device({(DOMAIN,
    "3548208972")})` (the brief's own helper, verbatim) raises RuntimeError on
    this venv's installed homeassistant==2026.9.2 -- `async_get_device` is
    deprecated ("device identifiers ... no longer unique across config
    entries") and calling it from a frame Home Assistant cannot attribute to
    a real integration (a test file, not a component under
    custom_components/) makes `report_usage` raise instead of merely
    logging. `async_get_device_by_identifier` is the documented replacement
    and needs the owning `entry.entry_id`, which this project's tests always
    have on hand. Uses `entry.unique_id` (== the serial, for every entry
    built here) rather than a hardcoded literal, so this same helper serves
    a second, differently-serialled entry (see
    test_write_register_targets_the_addressed_device_not_just_any_loaded_one)."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.unique_id), entry.entry_id
    )
    return device.id


async def setup_second_entry(hass, transport):
    """A second, genuinely separate config entry/device (Casa Justice
    inverter 2's real host/serial/slave_id, per CLAUDE.md) -- deliberately
    NOT built through tests/test_init.py's own `setup_entry()`, which
    hardcodes `ENTRY_DATA` (inverter 1's host/serial/slave_id) and has no
    parameter to override it. A local helper here, rather than modifying
    that shared, already-reviewed file for one test's sake."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=INVERTER_2_DATA, options=ENTRY_OPTIONS,
        unique_id="3548738877", title="Justice Inv 2",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_read_register_returns_values(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    response = await hass.services.async_call(
        DOMAIN, "read_register",
        {"device_id": await device_id_for(hass, entry), "address": "0x0100", "count": 3},
        blocking=True, return_response=True,
    )
    assert response["address"] == "0x0100"
    assert response["values"] == [55, 531, 65527]


async def test_write_register_returns_the_read_back(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    response = await hass.services.async_call(
        DOMAIN, "write_register",
        {"device_id": await device_id_for(hass, entry), "address": "0xE01E", "value": 16},
        blocking=True, return_response=True,
    )
    assert response == {"address": "0xE01E", "written": 16, "readback": 16}
    assert transport.writes == [(0xE01E, 16)]


async def test_write_register_rejected_by_firmware_raises(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """From the brief, verbatim scenario (0xE20F). This address is caught by
    services.py's OWN proactive guard (KNOWN_WRITE_REJECTED) before ever
    reaching the transport -- see
    test_write_register_known_write_rejected_refuses_without_touching_the_wire
    below for the assertion that actually distinguishes "the guard fired"
    from "the transport would have rejected it anyway", which this test
    alone cannot tell apart (both raise HomeAssistantError here).
    """
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "write_register",
            {"device_id": await device_id_for(hass, entry), "address": "0xE20F", "value": 2},
            blocking=True, return_response=True,
        )


async def test_write_register_known_write_rejected_refuses_without_touching_the_wire(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """0xE039 sits at the exact boundary between KNOWN_WRITE_REJECTED and
    KNOWN_ABSENT_ON_THIS_FIRMWARE's third range (which starts at 0xE03A, not
    0xE030 -- tests/fake_transport.py's own DEFAULT_UNSUPPORTED comment
    explains why: 0xE039 is a real, existing, READABLE register that only
    refuses writes, so it must never also be classified absent). Asserts the
    specific translation_key, not just the exception type: a guard that
    mixed up "write-rejected" with "absent" would still raise
    ServiceValidationError and this test would not catch the mix-up without
    checking the key. Also asserts the wire was never touched at all --
    proof this is the PROACTIVE guard firing, not the transport's own (also
    correct) rejection.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN, "write_register",
            {"device_id": await device_id_for(hass, entry), "address": "0xE039", "value": 1},
            blocking=True, return_response=True,
        )
    assert exc_info.value.translation_key == "write_rejected_known"
    assert transport.writes == []


async def test_write_register_known_absent_register_refuses_without_touching_the_wire(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """0x0112 is the first address of KNOWN_ABSENT_ON_THIS_FIRMWARE's first
    range (right after the "battery" block, 0x0100-0x010E). Also correctly
    rejected by the transport itself (UnsupportedRegisterError, from
    FakeTransport's matching DEFAULT_UNSUPPORTED range) if it ever reached
    the wire -- this test pins that services.py's OWN proactive guard is
    what actually fires, by asserting the wire was never touched.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN, "write_register",
            {"device_id": await device_id_for(hass, entry), "address": "0x0112", "value": 1},
            blocking=True, return_response=True,
        )
    assert exc_info.value.translation_key == "write_absent_block"
    assert transport.writes == []


async def test_write_register_raises_when_the_readback_does_not_match(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """FakeTransport deliberately never validates a written value against
    registers.py's WriteSpec ranges (a fake must not implement the logic
    under test) -- `no_stick_writes` is the sanctioned way to model "the
    device accepted the write but it didn't stick", and this is what the
    coordinator's own async_write_raw read-back check (Task 7) is for.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete, no_stick_writes=[0xE01E]
    )
    entry = await setup_entry(hass, transport)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "write_register",
            {"device_id": await device_id_for(hass, entry), "address": "0xE01E", "value": 16},
            blocking=True, return_response=True,
        )
    # The write itself DID reach the device (recorded in `writes`) -- it just
    # didn't stick. Confirms the service really goes through
    # coordinator.async_write_raw's verified-write path, not some shortcut
    # that skips the read-back check.
    assert transport.writes == [(0xE01E, 16)]


async def test_reprobe_service_returns_the_support_map(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    response = await hass.services.async_call(
        DOMAIN, "reprobe",
        {"device_id": await device_id_for(hass, entry)},
        blocking=True, return_response=True,
    )
    assert response["0x0100"] == "supported"


async def test_reprobe_service_refuses_while_paused(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Constraint from task-12's review (Fix round 1, Finding 1): pressing
    reprobe while the connection switch is off must refuse, not silently
    reopen the logger the user just told it to let go of.
    `handle_reprobe` must call the SAME guarded `async_reprobe()` the button
    calls, never `coordinator.async_probe()` directly -- this is the test
    that would go red if a future edit "simplified" handle_reprobe to skip
    async_reprobe's guard.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_set_connection_enabled(False)
    connect_count_before = transport.connect_count
    device_id = await device_id_for(hass, entry)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "reprobe",
            {"device_id": device_id},
            blocking=True, return_response=True,
        )

    # Refused before ever touching the transport again -- connect_count must
    # not have moved. This is the exact betrayal Task 12's Fix round 1 exists
    # to prevent: pressing reprobe while paused used to silently reconnect
    # the logger while the switch still read "off".
    assert transport.connect_count == connect_count_before


async def test_unknown_device_raises_validation_error(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "read_register",
            {"device_id": "does-not-exist", "address": 256, "count": 1},
            blocking=True, return_response=True,
        )


async def test_service_call_against_a_failed_reload_raises_validation_error(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Constraint 5: `entry.runtime_data` survives a failed setup (present in
    both SETUP_RETRY and SETUP_ERROR), and a service call against that dead
    entry is a realistic thing for a user or automation to do --
    `_entry_for_device` must gate on `entry.state`, never on
    `hasattr(entry, "runtime_data")`.

    Built the way task-8-report.md's own Finding 6 test does: a first,
    successful setup registers the device; a reload's fresh setup attempt
    gets past the probe (`entry.runtime_data` reassigned to a brand-new
    SrneRuntimeData) and then fails on the coordinator's own first refresh,
    landing `entry.state` at SETUP_RETRY with `runtime_data` still present,
    pointing at a coordinator whose transport is already closed. The device
    registry entry from the FIRST successful load survives the reload (Home
    Assistant does not delete devices on unload), so `device_id` is still
    resolvable -- exactly the scenario a `hasattr`-based guard would get
    wrong: it would find `runtime_data` present and hand back the DEAD entry
    instead of refusing.
    """
    good_transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, good_transport)
    device_id = await device_id_for(hass, entry)

    dying_transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[None] * 10
        + [TransportConnectionError("session died right after the probe")],
    )
    with patch(
        "custom_components.srne_inverter.build_transport",
        return_value=dying_transport,
    ):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    # The trap: runtime_data IS present here, pointing at the now-dead
    # coordinator -- a hasattr-based guard would treat this device as usable.
    assert hasattr(entry, "runtime_data")
    assert entry.runtime_data.coordinator.last_update_success is False

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "read_register",
            {"device_id": device_id, "address": "0x0100", "count": 1},
            blocking=True, return_response=True,
        )


def test_known_facts_match_the_fake_transports_ground_truth():
    """Guards against drift between services.py's own hardcoded firmware
    facts (used to refuse a write proactively, before any wire traffic) and
    tests/fake_transport.py's simulation of the exact same verified facts.
    If Casa Justice's firmware is ever re-verified with a different result,
    this is the test that forces both places to be updated together.
    """
    assert KNOWN_WRITE_REJECTED == WRITE_REJECTED
    assert KNOWN_ABSENT_ON_THIS_FIRMWARE == DEFAULT_UNSUPPORTED


# ---- Fix round 1 -------------------------------------------------------


async def test_write_register_reports_the_write_landed_when_only_the_readback_is_lost(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Important (production): `coordinator.async_write_raw`
    used to give the write phase and the read-back phase the SAME error
    message ("write to 0x... failed: ..."), for both. If the logger drops
    the session during WRITE_SETTLE -- AFTER the device already accepted
    and stored the write, BEFORE the read-back could confirm it -- the
    register genuinely holds the new value while the user is told the
    WRITE failed. Confirms both halves: the write really did land (the fake
    genuinely stored it), and the message the user sees says a write
    happened and only the CONFIRMATION was lost -- not the old, ambiguous
    "write ... failed" wording, which a user would reasonably read as
    "nothing happened" and either retry (redundant, harmless here, but not
    guaranteed in general) or assume the OLD value is still in effect and
    act on that (wrong, on a client's production inverter).
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    device_id = await device_id_for(hass, entry)

    # Inject the failure on the very next read only. No scheduled poll can
    # land in between this line and the service call below (scan_interval
    # is 10s in ENTRY_OPTIONS and no fake or real time advances here), so
    # this lands exactly on the write's own read-back, not on anything else.
    transport._read_errors = [
        TransportTimeoutError("session dropped during WRITE_SETTLE")
    ]

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            DOMAIN, "write_register",
            {"device_id": device_id, "address": "0xE01E", "value": 33},
            blocking=True, return_response=True,
        )

    message = str(exc_info.value)
    assert message.startswith("wrote 0xE01E=33")
    assert "could not confirm it stuck" in message
    assert "may have already reached the device" in message
    # The write itself DID land -- the fake genuinely stored it and recorded
    # it, even though the service call raised.
    assert transport.writes == [(0xE01E, 33)]
    assert transport.registers[0xE01E] == 33


async def test_write_register_logs_at_info_with_address_value_and_outcome(
    hass, justice_registers_synthetic_complete, enable_custom_integrations, caplog
):
    """Fix round 1, Important (production): the write path had NO logging
    at all -- at default INFO (production's normal level), a raw write to
    an unmapped register left zero trace, for a service whose entire
    purpose is doing things no entity -- and therefore no normal HA
    state-change history -- will let you do. "What changed on the inverter
    last night?" was unanswerable. Confirms both a successful write and a
    refused one are logged at INFO with the address and value present.
    """
    caplog.set_level(logging.INFO, logger="custom_components.srne_inverter.services")

    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    device_id = await device_id_for(hass, entry)

    await hass.services.async_call(
        DOMAIN, "write_register",
        {"device_id": device_id, "address": "0xE01E", "value": 16},
        blocking=True, return_response=True,
    )
    success_messages = [
        r.getMessage() for r in caplog.records
        if r.name == "custom_components.srne_inverter.services"
    ]
    assert any("0xE01E" in m and "16" in m for m in success_messages)
    assert any("OK" in m for m in success_messages)

    caplog.clear()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "write_register",
            {"device_id": device_id, "address": "0xE20F", "value": 2},
            blocking=True, return_response=True,
        )
    failure_messages = [
        r.getMessage() for r in caplog.records
        if r.name == "custom_components.srne_inverter.services"
    ]
    assert any("0xE20F" in m for m in failure_messages)
    assert any("FAILED" in m for m in failure_messages)


async def test_write_register_targets_the_addressed_device_not_just_any_loaded_one(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Important (test): nothing previously proved the service
    acts on the device it was actually asked for -- a `_entry_for_device`
    regression that returned "the first LOADED entry" regardless of
    `device_id` left all 11 original tests passing (only one entry was ever
    loaded in any of them). Casa Justice already runs two entries; Yoga
    will run six. Two independently-tracked FakeTransports, one per entry:
    the write must land on transport_b's own writes/registers, and
    transport_a (the FIRST-loaded entry) must stay completely untouched.
    """
    transport_a = FakeTransport(justice_registers_synthetic_complete)
    entry_a = await setup_entry(hass, transport_a)  # loaded FIRST

    transport_b = FakeTransport(justice_registers_synthetic_complete)
    entry_b = await setup_second_entry(hass, transport_b)  # loaded SECOND

    device_id_b = await device_id_for(hass, entry_b)
    response = await hass.services.async_call(
        DOMAIN, "write_register",
        {"device_id": device_id_b, "address": "0xE01E", "value": 42},
        blocking=True, return_response=True,
    )
    assert response == {"address": "0xE01E", "written": 42, "readback": 42}
    assert transport_b.writes == [(0xE01E, 42)]
    assert transport_a.writes == []


async def test_write_register_ignores_writespec_bounds_by_design(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Minor: pins that write_register deliberately does NOT
    consult registers.py's WriteSpec (the per-field bounds the number/select
    entities enforce) -- it is the escape hatch, on purpose. 0xE01E is
    soc_low_alarm, WriteSpec(0, 100); 60000 is nowhere near reachable
    through the number entity, but the raw service must still accept it
    (FakeTransport, deliberately, does not enforce WriteSpec either -- the
    fake genuinely stores 60000). A future "safety" edit that quietly added
    a WriteSpec check here (turning this back into a second `number`
    entity) would turn this red, with no other test catching it.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    response = await hass.services.async_call(
        DOMAIN, "write_register",
        {"device_id": await device_id_for(hass, entry), "address": "0xE01E", "value": 60000},
        blocking=True, return_response=True,
    )
    assert response == {"address": "0xE01E", "written": 60000, "readback": 60000}


@pytest.mark.parametrize(
    "bad_value", [16.7, True, False],
    ids=["fractional-float", "bool-true", "bool-false"],
)
async def test_write_register_rejects_fractional_and_boolean_values(
    hass, justice_registers_synthetic_complete, enable_custom_integrations, bad_value
):
    """Fix round 1, Minor: `vol.Coerce(int)` silently truncated a float
    (16.7 -> 16) and accepted `True`/`False` as `1`/`0` (bool is an int
    subclass in Python) -- surprising for a service whose whole point is
    sending the EXACT value asked for. `_strict_int` (services.py) now
    rejects both outright at the schema layer, before the handler ever
    runs -- confirmed here as a HA-schema-level `vol.Invalid`
    (`hass.services.async_call` re-raises schema failures directly, see
    `homeassistant/core.py`'s own `async_call`), not a `HomeAssistantError`.
    """
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "write_register",
            {"device_id": await device_id_for(hass, entry), "address": "0xE01E", "value": bad_value},
            blocking=True, return_response=True,
        )


async def test_address_rejects_boolean_even_though_bool_is_an_int_subclass(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Minor: `_address`'s old `isinstance(value, int)` check
    let a bare `True` (Python: `isinstance(True, int) is True`) through as
    address 1 -- e.g. a YAML automation with `address: yes` would silently
    target register 0x0001 instead of failing loudly. `_strict_int` fixes
    the shared root cause.
    """
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "read_register",
            {"device_id": await device_id_for(hass, entry), "address": True, "count": 1},
            blocking=True, return_response=True,
        )


def test_every_translation_key_used_has_a_translated_exception_message():
    """Fix round 1, Minor: nothing previously validated that every
    `translation_key` services.py raises with has a matching entry in BOTH
    translation files -- a typo would silently degrade the frontend to the
    plain-English fallback (see homeassistant.helpers.translation's own
    `async_get_exception_message`: "We return the translation key when [it]
    was not found in the cache"), with no test noticing. Derives the set of
    keys from services.py's OWN source (a regex over the actual file, not a
    hand-maintained list here) so a new `translation_key` added later
    without a matching translation entry is caught automatically.
    """
    source = Path(services_module.__file__).read_text()
    keys = set(re.findall(r'translation_key="([^"]+)"', source))
    assert keys, "expected at least one translation_key in services.py"

    translations_dir = Path(services_module.__file__).parent / "translations"
    for lang in ("en", "es"):
        translations = json.loads((translations_dir / f"{lang}.json").read_text())
        exceptions = translations.get("exceptions", {})
        for key in keys:
            assert key in exceptions, f"{lang}.json is missing exceptions.{key}"
            assert exceptions[key].get("message"), (
                f"{lang}.json exceptions.{key} has no message"
            )

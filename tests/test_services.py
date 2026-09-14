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

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.srne_inverter.const import DOMAIN
from custom_components.srne_inverter.services import (
    KNOWN_ABSENT_ON_THIS_FIRMWARE,
    KNOWN_WRITE_REJECTED,
)
from custom_components.srne_inverter.transport.base import TransportConnectionError
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport, WRITE_REJECTED
from tests.test_init import setup_entry


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
    have on hand."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "3548208972"), entry.entry_id
    )
    return device.id


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

"""Base entity: device info, availability and unique ids.

Deviation 1 from the task brief, same root cause the brief itself warns
about and tests/test_init.py already hit one task earlier: both of the
brief's tests called `tests.test_init.setup_entry()` (or, for the second
test, would have) against the plain `justice_registers` fixture.
`coordinator.async_probe()` always probes every one of `registers.BLOCKS`'
10 blocks, and our capture of Casa Justice inverter 1 has real holes inside
5 of them -- `FakeTransport` raises a plain, uncaught `LookupError` for an
address that is neither recorded nor declared `unsupported` (see
`tests/fake_transport.py`'s module docstring and `tests/conftest.py`'s
`load_justice_registers_synthetic_complete` docstring). Both tests below use
`justice_registers_synthetic_complete` instead. Confirmed by hand this does
not touch the values either test actually cares about: `battery_soc`'s block
("battery" at 0x0100) and `firmware_version`'s address (0x0014, read as part
of the recorded "machine" block, raw 818) have zero gaps in the raw capture,
so both `battery_soc == 55` and `sw_version == "V8.18"` are real recorded
values, not synthetic filler -- only the OTHER addresses inside 5 blocks get
the synthetic 0xF00D filler, and neither test asserts on one of those.

Deviation 2a: `dr.async_get(hass).async_get_device({(DOMAIN, "3548208972")})`,
exactly as the brief's snippet has it, raises `RuntimeError` on this venv's
homeassistant==2026.9.2 -- `async_get_device` is deprecated (device
identifiers/connections are no longer guaranteed unique across config
entries) and this HA version's deprecation-reporting defaults to
`ReportBehavior.ERROR` for a call it cannot attribute to a loaded
integration frame (a raw test module, in this case), turning the deprecation
into a hard failure rather than a warning. Confirmed empirically: running
the brief's lookup as written raises
`RuntimeError: Detected code that calls 'device_registry.async_get_device',
which is deprecated ... use 'async_get_device_by_identifier' ...` --
exactly the replacement HA's own error message names. Fixed by using
`dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "3548208972"),
entry.entry_id)` instead, which needs `entry.entry_id` (available from
`setup_entry()`'s return value) alongside the identifier tuple.

Deviation 2b: the brief's `test_device_is_registered_with_serial_identifier`
calls only `setup_entry(hass, transport)` and then expects a device to
already be in the registry. That does not hold as written: this task's
`__init__.py` (Task 8) forwards to six platform modules that are still
no-op stubs (`sensor.py` etc. all `return` immediately -- Task 9's own brief
says "Every platform in Tasks 10, 11 and 12 subclasses [SrneEntity]", i.e.
none of them do yet). A device is registered by Home Assistant's own
`entity_platform` machinery when an entity with `device_info` is actually
added via `async_add_entities` -- `entity.py` alone, with nothing wired into
a real platform, adds no entity and therefore registers no device. Verified
by running the brief's test as written (adjusted only for the fixture
swap): `dr.async_get(hass).async_get_device(...)` returned `None`, not a
device with the wrong fields -- confirming this is a missing entity, not a
`DeviceInfo` mapping bug. Fixed by patching `sensor.async_setup_entry` for
the duration of the test to do what Task 10 will really do: build one
`SrneFieldEntity` and hand it to `async_setup_field_platform`. This drives
the real `entity_platform`/`device_registry` machinery (not a hand-rolled
substitute), and is exactly the shape `async_setup_field_platform` exists to
support -- it is not a workaround invented to force the test to pass.

The second test (`test_entities_become_unavailable_after_a_failed_cycle`) is
marked `xfail` per the brief's own note: it names
`sensor.justice_inv_1_battery_soc`, an entity only the REAL sensor platform
(Task 10) creates. It still uses `justice_registers_synthetic_complete`
(not the brief's plain `justice_registers`) for the same reason as above --
so that once Task 10 removes the `xfail` marker, the test fails or passes on
its own merits (does `sensor.justice_inv_1_battery_soc` exist and go
unavailable?) rather than on an unrelated `LookupError` out of
`async_setup_entry` before the sensor platform is even reached.
"""

from unittest.mock import patch

import pytest
from homeassistant.helpers import device_registry as dr

from custom_components.srne_inverter import registers as R
from custom_components.srne_inverter.const import DOMAIN
from custom_components.srne_inverter.entity import (
    SrneFieldEntity,
    async_setup_field_platform,
)
from tests.fake_transport import FakeTransport
from tests.test_init import setup_entry


async def _add_battery_soc_sensor(hass, entry, async_add_entities):
    """Stand-in for what Task 10's real sensor.async_setup_entry will do:
    build one SrneFieldEntity and hand it to async_setup_field_platform.
    Patched into sensor.py's own async_setup_entry slot for the duration of
    a test -- see the module docstring's "Deviation 2". Replaces the target
    outright (`patch(..., new=...)`), not via `side_effect` on an
    auto-generated `AsyncMock`, so there is no ambiguity about whether the
    coroutine this returns actually gets awaited."""
    field = R.field_by_key("battery_soc")

    def builder():
        return [SrneFieldEntity(entry.runtime_data.coordinator, entry, field)]

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


async def test_device_is_registered_with_serial_identifier(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    with patch(
        "custom_components.srne_inverter.sensor.async_setup_entry",
        new=_add_battery_soc_sensor,
    ):
        entry = await setup_entry(hass, transport)
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "3548208972"), entry.entry_id
    )
    assert device is not None
    assert device.manufacturer == "SRNE"
    assert device.sw_version == "V8.18"
    assert device.name == "Justice Inv 1"


@pytest.mark.xfail(reason="sensor platform lands in Task 10", strict=False)
async def test_entities_become_unavailable_after_a_failed_cycle(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    from custom_components.srne_inverter.transport.base import (
        TransportConnectionError,
    )

    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    state = hass.states.get("sensor.justice_inv_1_battery_soc")
    assert state is not None and state.state == "55"

    transport._read_errors = [TransportConnectionError("dead")] * 20
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.justice_inv_1_battery_soc").state == "unavailable"

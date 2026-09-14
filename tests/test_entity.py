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

The second test (`test_entities_become_unavailable_after_a_failed_cycle`) was
marked `xfail` per the brief's own note, on purpose, as a handoff to Task 10:
it names `sensor.justice_inv_1_battery_soc`, an entity only the REAL sensor
platform (Task 10) creates. It already used `justice_registers_synthetic_complete`
(not the brief's plain `justice_registers`) for the same reason as above --
so that once Task 10 removed the `xfail` marker, the test would fail or pass
on its own merits (does `sensor.justice_inv_1_battery_soc` exist and go
unavailable?) rather than on an unrelated `LookupError` out of
`async_setup_entry` before the sensor platform is even reached. Task 10 has
now landed `sensor.py` and removed the marker -- this passes for real: the
entity exists with state "55" after setup (`custom_components.srne_inverter.
sensor.SrneSensor.native_value` reading the coordinator's decoded
`battery_soc`), and goes `unavailable` after `transport._read_errors` is
loaded with 20 failures and a refresh runs, because `CoordinatorEntity.
available` (inherited unchanged by `SrneEntity`, see entity.py's own
docstring) reads `coordinator.last_update_success`, which the coordinator's
own `_register_failure` sets to `False` on a failed cycle.

Fix round 1 (Opus review of the original commit) found one Critical and
three Important defects, none of which any test above exercised. Five tests
below were added to close that:

- `test_reprobe_signal_adds_a_newly_supported_entity` -- Findings 1
  (CRITICAL: the dispatcher target was a bare `lambda`, so HA ran it on a
  worker thread via `HassJobType.Executor` and silently dropped every
  entity a re-probe should have added -- see entity.py's own inline comment
  on the fix) and 2 (no test ever fired `SIGNAL_NEW_ENTITIES` at all, which
  is exactly why Finding 1 shipped green). Drives the REAL dispatcher
  (`async_dispatcher_send`), not a direct call to the internal callback --
  a direct call would never have caught Finding 1, since the bug was
  entirely about how HA'S OWN dispatcher invokes an unmarked callable, not
  about what the callable does once invoked.
- `test_unique_id_is_serial_underscore_key` -- Finding 3: pins the exact
  `f"{serial}_{key}"` scheme against a real entity registry lookup.
- `test_field_entity_unavailable_when_its_block_has_never_been_read` and
  `test_field_entity_unavailable_after_its_block_is_reclassified_unsupported_mid_poll`
  -- both halves of Finding 4. The second one is not a redundant restatement
  of the first: `coordinator._registers` is cumulative and is never purged
  for a block reclassified `UNSUPPORTED` mid-poll, so `field.key in
  coordinator.data.values` alone (this task's original implementation)
  stayed `True` forever once a field had been read at least once -- fixed
  in `entity.py`'s `SrneFieldEntity.available` by also checking
  `coordinator.support`, not just in the test.
- `test_raw_value_exposes_every_word_of_a_multi_word_field` -- Finding 5
  (Minor): `raw_value` used to read only a multi-word field's FIRST
  register.

Finding 6 (Minor, `added_field_keys` is one set shared by all six platforms
with no enforced per-platform namespace) and the deferred docstring
correction (device_info is read once at entity-ADD time, not refreshed every
COLD cycle) were both documentation-only fixes inside `entity.py` itself --
neither changes observable behaviour a test here could pin beyond what the
existing tests already cover, so neither gets a new test.
"""

from unittest.mock import patch

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.srne_inverter import registers as R
from custom_components.srne_inverter.const import DOMAIN, SIGNAL_NEW_ENTITIES
from custom_components.srne_inverter.entity import (
    SrneFieldEntity,
    async_setup_field_platform,
)
from custom_components.srne_inverter.probe import BlockSupport
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport
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


async def _add_battery_and_hardware_version_sensors(hass, entry, async_add_entities):
    """A second stand-in, closer to a real platform's builder() shape than
    `_add_battery_soc_sensor` above: builds entities for whichever of TWO
    fields, in two DIFFERENT blocks, are currently supported and not yet
    added. Needed for `test_reprobe_signal_adds_a_newly_supported_entity`
    below -- a re-probe must be able to add a genuinely NEW entity for one
    field without re-adding or disturbing the other. Namespaces its own
    tracking keys ("test:...") in `entry.runtime_data.added_field_keys`,
    per entity.py's own documented convention (Fix round 1, Finding 6)."""
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder():
        supported = coordinator.supported_field_keys()
        new = []
        for key in ("battery_soc", "hardware_version"):
            tracked = f"test:{key}"
            if key in supported and tracked not in added:
                added.add(tracked)
                new.append(SrneFieldEntity(coordinator, entry, R.field_by_key(key)))
        return new

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


async def test_reprobe_signal_adds_a_newly_supported_entity(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Findings 1 (CRITICAL) and 2. Sets up a unit whose
    device_info block (0x0014, holding hardware_version) is UNSUPPORTED --
    no hardware_version entity exists. The block is then made readable again
    (what a corrected re-probe, or a firmware update, would produce) and
    re-probed for real over the SAME transport (`coordinator.async_probe()`,
    exactly what Task 14's reprobe service/button will call). Firing the
    REAL dispatcher signal (`async_dispatcher_send`, not a direct call to
    entity.py's internal callback -- a direct call would never have caught
    Finding 1, which is entirely about how HA's OWN dispatcher invokes an
    unmarked callable) must then add the new entity.
    """
    device_info = R.block_by_addr(0x0014)
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=DEFAULT_UNSUPPORTED
        + (range(device_info.addr, device_info.addr + device_info.count),),
    )
    with patch(
        "custom_components.srne_inverter.sensor.async_setup_entry",
        new=_add_battery_and_hardware_version_sensors,
    ):
        entry = await setup_entry(hass, transport)

    coordinator = entry.runtime_data.coordinator
    assert hass.states.get("sensor.justice_inv_1_battery_soc") is not None
    assert hass.states.get("sensor.justice_inv_1_hardware_version") is None
    assert "hardware_version" not in coordinator.supported_field_keys()

    # The block answers normally again -- re-probe for real over the same
    # transport, the same way a corrected re-probe would.
    transport.unsupported = DEFAULT_UNSUPPORTED
    await coordinator.async_probe()
    assert "hardware_version" in coordinator.supported_field_keys()
    # async_probe() alone updates coordinator.probe_result/coordinator.
    # _registers but NOT the publicly exposed coordinator.data snapshot --
    # that only changes on an actual refresh cycle. A real reprobe flow
    # (Task 14) runs a refresh right after re-probing for exactly this
    # reason; without it the entity below would exist but read as
    # unavailable (its own field key genuinely missing from the STALE
    # coordinator.data.values, not a bug in the entity itself).
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    async_dispatcher_send(hass, SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id))
    await hass.async_block_till_done()

    state = hass.states.get("sensor.justice_inv_1_hardware_version")
    assert state is not None
    assert state.state != "unavailable"


async def test_unique_id_is_serial_underscore_key(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Finding 3: pins the `f"{serial}_{key}"` unique-id scheme
    against a real entity registry lookup. Once entities exist in a user's
    instance, changing this scheme orphans every one of them -- HA creates a
    brand-new set with `_2` suffixes instead of recognising the same
    entities, silently losing their history/customisation."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    with patch(
        "custom_components.srne_inverter.sensor.async_setup_entry",
        new=_add_battery_soc_sensor,
    ):
        await setup_entry(hass, transport)
    registry_entry = er.async_get(hass).async_get("sensor.justice_inv_1_battery_soc")
    assert registry_entry is not None
    assert registry_entry.unique_id == "3548208972_battery_soc"


async def test_field_entity_unavailable_when_its_block_has_never_been_read(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Finding 4 (first half). Fails BOTH of probe()'s attempts
    (`retries=1`, so 2 attempts) at exactly the device_info block (the 9th
    of the 10 blocks in registers.BLOCKS' own declared order) while every
    other block succeeds -- this classifies device_info UNKNOWN (a
    transient failure, never UNSUPPORTED), so hardware_version has
    genuinely never produced a value: it is absent from
    `coordinator.data.values` entirely, while the coordinator itself stays
    healthy (`last_update_success is True`, since one UNKNOWN block does
    not fail a probe that has other SUPPORTED blocks)."""
    from custom_components.srne_inverter.transport.base import (
        TransportConnectionError,
    )

    read_errors = (
        [None] * 8  # battery, faults, inverter_a, inverter_b, settings_low,
        # settings_high, control_low, control_high -- all succeed.
        + [TransportConnectionError("timeout")] * 2  # device_info: both attempts fail.
        + [None]  # meter succeeds.
    )
    transport = FakeTransport(justice_registers_synthetic_complete, read_errors=read_errors)
    entry = await setup_entry(hass, transport)
    coordinator = entry.runtime_data.coordinator

    assert coordinator.probe_result.support[0x0014] is BlockSupport.UNKNOWN
    assert coordinator.last_update_success is True
    assert "hardware_version" not in coordinator.data.values

    entity = SrneFieldEntity(coordinator, entry, R.field_by_key("hardware_version"))
    assert entity.available is False


async def test_field_entity_unavailable_after_its_block_is_reclassified_unsupported_mid_poll(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Finding 4 (second half) -- the half the original
    `field.key in values` check alone could never catch. The battery block
    (HOT tier, always re-read every cycle per coordinator.py's own
    "nothing due -> HOT" fallback) answers normally at setup, then starts
    answering UnsupportedRegisterError on the very next poll. The block
    gets reclassified UNSUPPORTED, but `coordinator._registers` is
    cumulative and is NEVER purged for a block that stops being polled --
    battery_soc's LAST decoded value stays sitting in
    `coordinator.data.values` forever. `available` must still go False, or
    a field nobody is polling anymore would keep reporting a frozen value
    as if it were live.
    """
    battery = R.block_by_addr(0x0100)
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    coordinator = entry.runtime_data.coordinator
    entity = SrneFieldEntity(coordinator, entry, R.field_by_key("battery_soc"))
    assert entity.available is True  # sanity: normal before the mutation below.

    transport.unsupported = DEFAULT_UNSUPPORTED + (
        range(battery.addr, battery.addr + battery.count),
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.support[battery.addr] is BlockSupport.UNSUPPORTED
    # The stale value is still there -- this is what makes the check above
    # non-trivial: a naive `field.key in values` alone would stay True.
    assert "battery_soc" in coordinator.data.values
    assert entity.available is False


async def test_raw_value_exposes_every_word_of_a_multi_word_field(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1, Finding 5 (Minor): `raw_value` used to read only a
    multi-word field's FIRST register. Pins that a 4-word field
    (inverter_serial) and a 2-word field (load_energy_total) both return
    every one of their words, in address order -- not a single truncated
    int -- while a one-word field's `raw_value` is still a plain int,
    unchanged."""
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    coordinator = entry.runtime_data.coordinator

    serial_field = R.field_by_key("inverter_serial")
    serial_entity = SrneFieldEntity(coordinator, entry, serial_field)
    raw = serial_entity.raw_value
    assert raw == [
        coordinator.data.registers.get(serial_field.address + i) for i in range(4)
    ]

    energy_field = R.field_by_key("load_energy_total")
    energy_entity = SrneFieldEntity(coordinator, entry, energy_field)
    raw2 = energy_entity.raw_value
    assert raw2 == [
        coordinator.data.registers.get(energy_field.address + i) for i in range(2)
    ]

    single_field = R.field_by_key("battery_soc")
    single_entity = SrneFieldEntity(coordinator, entry, single_field)
    assert single_entity.raw_value == coordinator.data.registers.get(single_field.address)

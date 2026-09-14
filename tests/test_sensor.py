"""Sensor and binary_sensor platforms.

Every test here calls `setup_entry()` with `justice_registers_synthetic_complete`,
never the plain `justice_registers` fixture the brief's own Step 1 snippet used --
a known trap documented in Task 8/9's own test files: `coordinator.async_probe()`
always probes every one of `registers.BLOCKS`' 10 blocks, and the raw capture of
Casa Justice inverter 1 has real holes inside 5 of them. `FakeTransport` raises a
bare, uncaught `LookupError` ("fixture gap") for an address that is neither
recorded nor declared `unsupported`, which crashes `async_setup_entry` outright
instead of producing a usable ConfigEntryState -- see tests/test_init.py's module
docstring and tests/conftest.py's `load_justice_registers_synthetic_complete`
docstring for the full story.

Two further, task-10-specific corrections on top of that swap, both confirmed by
hand against the fixtures (see tests/conftest.py's SYNTHETIC_FILL docstring):

- The "faults" block (0x0200-0x0207, fault_word_1-4) has ZERO recorded raws in
  either fixture -- the synthetic-complete fixture fills all four words with the
  nonzero 0xF00D filler. Taken as-is, that makes the fault binary sensor come up
  ON in every test, including the brief's own "is_off_when_all_words_are_zero"
  one. Both fault tests below explicitly zero (or set) the four fault-word
  addresses on top of the synthetic-complete base instead of trusting either
  fixture to already read "no fault".
- `test_sensors_of_unsupported_blocks_are_not_created` needs the "meter" block
  (0xF02C) to be classified UNSUPPORTED by the probe, not merely absent from the
  registers dict -- an absent-but-undeclared address is exactly the LookupError
  trap above, not a "this block doesn't exist" signal. The registers dict is
  trimmed AND the block's address range is passed via `unsupported=`, so the
  probe genuinely classifies it UNSUPPORTED instead of crashing.
"""

from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.srne_inverter.const import SIGNAL_NEW_ENTITIES
from custom_components.srne_inverter.registers import field_by_key
from custom_components.srne_inverter.sensor import _display_value
from custom_components.srne_inverter.transport.base import UnsupportedRegisterError
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport
from tests.test_init import setup_entry


async def test_core_sensors_exist_with_decoded_values(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    assert hass.states.get("sensor.justice_inv_1_battery_soc").state == "55"
    assert hass.states.get("sensor.justice_inv_1_battery_voltage").state == "53.1"
    assert hass.states.get("sensor.justice_inv_1_battery_current").state == "0.9"
    assert hass.states.get("sensor.justice_inv_1_machine_state").state == "AC bypass"
    assert hass.states.get("sensor.justice_inv_1_grid_charge_current").state == "1.0"


async def test_derived_sensors_exist(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    assert hass.states.get("sensor.justice_inv_1_battery_power") is not None


async def test_read_only_enum_fields_are_sensors_not_selects(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    assert hass.states.get("sensor.justice_inv_1_charge_priority") is not None
    assert hass.states.get("select.justice_inv_1_charge_priority") is None


async def test_sensor_exposes_raw_and_address_attributes(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    attrs = hass.states.get("sensor.justice_inv_1_battery_current").attributes
    assert attrs["address"] == "0x0102"
    assert attrs["raw"] == 65527


async def test_fault_binary_sensor_is_off_when_all_words_are_zero(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    registers = {
        **justice_registers_synthetic_complete,
        0x0204: 0, 0x0205: 0, 0x0206: 0, 0x0207: 0,
    }
    await setup_entry(hass, FakeTransport(registers))
    state = hass.states.get("binary_sensor.justice_inv_1_fault_active")
    assert state.state == "off"
    assert state.attributes["device_class"] == "problem"


async def test_fault_binary_sensor_turns_on(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    # Fix round 1 (Opus review, Finding 6): the previous version of this
    # test already baked 0x0205=0x0040 into the registers dict BEFORE
    # setup, so the refresh below re-read the SAME value -- no transition
    # was ever exercised, only a second (redundant) read of an already-ON
    # sensor. This version starts genuinely OFF (all four fault words
    # zero, confirmed by the sibling "is_off" test), then mutates the
    # FakeTransport's own `registers` dict in place -- the "faults" block
    # is HOT tier, always re-read on the next refresh regardless of
    # elapsed wall-clock time (coordinator.py's own `_read_due_blocks`
    # fallback: `if not due: due = {BlockTier.HOT}`) -- so the transition
    # is real, not incidental.
    registers = {
        **justice_registers_synthetic_complete,
        0x0204: 0, 0x0205: 0, 0x0206: 0, 0x0207: 0,
    }
    transport = FakeTransport(registers)
    entry = await setup_entry(hass, transport)
    assert hass.states.get("binary_sensor.justice_inv_1_fault_active").state == "off"

    transport.registers[0x0205] = 0x0040
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.justice_inv_1_fault_active")
    assert state.state == "on"
    assert state.attributes["fault_word_2"] == "0x0040"


async def test_fault_binary_sensor_not_created_when_faults_block_is_unsupported(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    # Fix round 1 (Opus review, Finding 5): the FAULT_FIELD_KEYS support
    # gate in binary_sensor.py's own builder() (`if not all(key in
    # supported for key in FAULT_FIELD_KEYS): return []`) had no test at
    # all -- a Yoga unit lacking the "faults" block would previously have
    # been indistinguishable, in test coverage, from one that has it.
    # Declares the block's address range unsupported explicitly (same
    # pattern as test_sensors_of_unsupported_blocks_are_not_created below)
    # so the probe genuinely classifies it UNSUPPORTED instead of hitting
    # the LookupError fixture-gap trap.
    partial = {
        k: v for k, v in justice_registers_synthetic_complete.items()
        if not (0x0200 <= k < 0x0208)
    }
    transport = FakeTransport(
        partial, unsupported=(*DEFAULT_UNSUPPORTED, range(0x0200, 0x0208))
    )
    await setup_entry(hass, transport)
    assert hass.states.get("binary_sensor.justice_inv_1_fault_active") is None


async def test_sensors_of_unsupported_blocks_are_not_created(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    partial = {
        k: v for k, v in justice_registers_synthetic_complete.items() if k < 0xF02C
    }
    transport = FakeTransport(
        partial, unsupported=(*DEFAULT_UNSUPPORTED, range(0xF02C, 0x10000))
    )
    await setup_entry(hass, transport)
    assert hass.states.get("sensor.justice_inv_1_battery_soc") is not None
    # Fix round 1 (Opus review, Finding 1, CRITICAL): the real entity id is
    # "total_running_days" (registers.py's Field.label is "Total Running
    # Days", and entity ids are slugified from the label, per task-9-report
    # .md's Public interface section -- NOT "running_days" (the Field.key,
    # a different string). The wrong id used to make this assertion pass
    # regardless of whether the exclusion logic worked at all --
    # hass.states.get() of a nonexistent id is always None -- so deleting
    # `sensor.py`'s `field.key not in supported` guard entirely left this
    # test green. See this file's own falsifiability note in the task-10
    # report's "Fix round 1" section.
    assert hass.states.get("sensor.justice_inv_1_total_running_days") is None


async def test_writable_fields_are_not_sensors(
    hass, justice_registers_synthetic_complete, enable_custom_integrations, caplog
):
    """Fix round 1 (Opus review, Finding 4): sensor.py's own `field.write is
    not None` exclusion (the "sensor selection rule" in this task's brief)
    had no direct test -- the existing
    test_read_only_enum_fields_are_sensors_not_selects only proves
    select.justice_inv_1_charge_priority doesn't exist, which is tautological
    while select.py is still a Task-11 stub (it `return`s immediately
    regardless of what sensor.py does).

    First draft of this test asserted only `hass.states.get(...) is None`
    for battery_type/equalize_voltage, same shape as every other "not
    created" test in this file -- and it turned out to be JUST as
    undefended as Finding 1's `running_days` typo, for a different reason,
    caught only by actually reintroducing the mutation (deleting `field.write
    is not None`, keeping `field.key not in supported`) in a throwaway copy:
    the state assertions kept passing. Every writable Field in registers.py
    happens to have `category="config"` (verified: `[f.key for f in FIELDS
    if f.write is not None and f.category != "config"]` is empty), and HA's
    own `EntityPlatform._async_add_entity` refuses outright to add a
    `config`-category entity to the `sensor` domain (`HomeAssistantError:
    ... cannot be added as the entity category is set to config`, confirmed
    against the installed source) -- so even with sensor.py's own guard
    deleted, `hass.states.get("sensor.justice_inv_1_battery_type")` is STILL
    `None`, just via HA's registry rejecting it after sensor.py already
    tried, not via sensor.py declining to try. `caplog` on that exact error
    text is what actually discriminates the two: it is empty with the guard
    present, and non-empty (one ERROR per writable field) with it removed --
    confirmed both ways in the throwaway copy before trusting this. Kept the
    state assertions too: they document the observable (still true) even
    though they are not, on their own, proof of anything.
    """
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    assert hass.states.get("sensor.justice_inv_1_battery_type") is None
    assert hass.states.get("sensor.justice_inv_1_equalize_voltage") is None
    assert "entity category is set to config" not in caplog.text


async def test_sensor_metadata_unit_device_class_state_class_are_pinned(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1 (Opus review, Finding 3): unit/device_class/state_class
    were unasserted anywhere -- deleting any of the three assignments in
    SrneSensor.__init__/SrneDerivedSensor.__init__ still left the full
    suite green. Concrete cost cited by the review: losing
    device_class="energy" on pv_energy_today only logs an HA warning, so
    the sensor silently drops off the energy dashboard with nothing
    failing loudly. Pins a representative FIELDS entry (battery_voltage),
    the specific energy-dashboard field the review named
    (pv_energy_today), and one DERIVED entry (battery_power) -- covering
    both SrneSensor and SrneDerivedSensor's own metadata wiring.
    """
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))

    voltage = hass.states.get("sensor.justice_inv_1_battery_voltage").attributes
    assert voltage["unit_of_measurement"] == "V"
    assert voltage["device_class"] == "voltage"
    assert voltage["state_class"] == "measurement"

    energy = hass.states.get("sensor.justice_inv_1_pv_energy_today").attributes
    assert energy["unit_of_measurement"] == "kWh"
    assert energy["device_class"] == "energy"
    assert energy["state_class"] == "total_increasing"

    power = hass.states.get("sensor.justice_inv_1_battery_power").attributes
    assert power["unit_of_measurement"] == "W"
    assert power["device_class"] == "power"
    assert power["state_class"] == "measurement"


async def test_derived_sensor_goes_unavailable_when_an_input_field_is_reclassified_unsupported(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1 (Opus review, Finding 2, the failure class this whole
    project exists to avoid: a confident wrong number).

    SrneDerivedSensor used to inherit SrneEntity's plain, coordinator-only
    `available` (== coordinator.last_update_success). coordinator._registers
    is cumulative and never purged for a block reclassified UNSUPPORTED
    mid-poll (coordinator.py's own Fix round 1, Finding 1) -- so when
    "inverter_b" (0x0223, holding load_power_l2) starts answering
    UnsupportedRegisterError on a LATER cycle, load_power_l2's own sensor
    correctly goes unavailable (SrneFieldEntity.available, entity.py's own
    Fix round 1, Finding 4), but load_power_total kept computing
    `load_power_l1 + <frozen, stale load_power_l2>` forever -- a plausible,
    silently wrong watt figure with nothing on the dashboard indicating it.

    read_errors is a queue of 17 `None`s (consumed one per read_holding
    call, matching the probe's 10 blocks + the first refresh's 4 HOT-tier
    blocks + the second refresh's first 3 HOT-tier blocks -- battery,
    faults, inverter_a -- in registers.BLOCKS' own declared order) followed
    by one real UnsupportedRegisterError, landing exactly on the SECOND
    refresh's read of "inverter_b" (the 4th HOT-tier block). Confirmed by
    hand against transport.reads before writing this (not guessed): the
    18th read_holding call (0-indexed 17) is 0x0223 on the second refresh,
    matching test_setup_raises_not_ready_when_the_first_refresh_fails'
    established "queue N Nones then the real error" convention.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        read_errors=[None] * 17 + [UnsupportedRegisterError("fake: 0x0223 absent")],
    )
    entry = await setup_entry(hass, transport)
    total_before = hass.states.get("sensor.justice_inv_1_load_power_total")
    assert total_before is not None and total_before.state != "unavailable"

    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("sensor.justice_inv_1_load_power_l2").state == "unavailable"
    assert hass.states.get("sensor.justice_inv_1_load_power_total").state == "unavailable"


async def test_enum_sensor_with_unmapped_raw_reports_unknown_not_crash(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1 (Opus review, Finding 6): the unmapped-enum guard in
    SrneSensor.native_value (an "Unknown (N)" raw code, from
    registers.EnumMap.label(), returning None instead of a value not in
    self._attr_options) was verified by the reviewer by hand (raw 9 ->
    "unknown", raw preserved in the `raw` attribute) but had no test of its
    own. CHARGE_STATE's map only covers {0, 1, 2, 4, 6, 8} -- 9 is
    deliberately absent, matching the reviewer's own example.
    """
    field = field_by_key("charge_state")
    registers = {**justice_registers_synthetic_complete, field.address: 9}
    await setup_entry(hass, FakeTransport(registers))
    state = hass.states.get("sensor.justice_inv_1_charge_state")
    assert state.state == "unknown"
    assert state.attributes["raw"] == 9


async def test_reprobe_signal_does_not_duplicate_entities(
    hass, justice_registers_synthetic_complete, enable_custom_integrations, caplog
):
    """Fix round 1 (Opus review, Finding 6): the `added_field_keys` dedup
    guard each builder() checks (`if entity_key in added: continue`) was
    never exercised by firing SIGNAL_NEW_ENTITIES a second time -- the
    signal a future re-probe (Task 14/15) will actually use to pick up
    newly-SUPPORTED fields without a restart.

    Fix round 2 (Opus re-review): the first version of this test asserted
    only `hass.states.async_entity_ids("sensor")` was unchanged
    before/after -- and that survived the guard being deleted entirely,
    because HA's OWN entity registry independently refuses a second entity
    carrying an already-registered `unique_id`
    ("Platform srne_inverter does not generate unique IDs... already
    exists - ignoring", one ERROR line per field, confirmed by the reviewer
    by actually removing the guard) -- so the entity-id-set assertion was
    proving HA's own registry behaviour, not this integration's dedup
    guard. `sensor.py`'s builder() now logs a DEBUG line naming the skipped
    key on that branch specifically (added in this round for exactly this
    reason); asserting on THAT text is what actually discriminates "our
    guard skipped it before HA ever saw a duplicate" from "HA rejected a
    duplicate our own code tried to create". Kept the entity-id-set
    assertion alongside it: still a true, useful observable, just not
    sufficient proof by itself (same lesson as Finding 4's caplog fix, one
    round earlier).
    """
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    before = set(hass.states.async_entity_ids("sensor"))
    caplog.clear()
    async_dispatcher_send(hass, SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id))
    await hass.async_block_till_done()
    after = set(hass.states.async_entity_ids("sensor"))
    assert after == before
    assert "already added, skipping re-probe rebuild" in caplog.text
    assert "does not generate unique IDs" not in caplog.text


def test_display_value_coerces_whole_float_only_at_precision_zero_or_none():
    """Fix round 1 (Opus review, Finding 6): the `precision == 0` branch of
    _display_value (currently reached only by Derived.load_power_total) had
    no direct test -- every existing FIELDS-backed assertion exercises
    `precision is None` (battery_soc) or a positive precision
    (grid_charge_current), never exactly 0. A plain, synchronous, no-`hass`
    test: _display_value is a pure function, isolating this from any HA
    setup machinery.
    """
    assert _display_value(48.0, 0) == 48
    assert isinstance(_display_value(48.0, 0), int)
    assert _display_value(55.0, None) == 55
    assert isinstance(_display_value(55.0, None), int)
    # Positive precision is left alone even when the value happens to be a
    # whole number -- grid_charge_current ("1.0", precision=1) must never
    # collapse to "1".
    assert _display_value(1.0, 1) == 1.0
    assert isinstance(_display_value(1.0, 1), float)


async def test_derived_sensor_precision_zero_shows_whole_number_not_trailing_zero(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Companion to the pure-function test above: confirms
    SrneDerivedSensor.native_value actually WIRES `derived.precision`
    through to `_display_value` for a real entity, not just that the
    helper itself is correct in isolation. load_power_l2 (0x0232) has no
    recorded raw in either fixture (see this file's module docstring) --
    the synthetic 0xF00D filler is used here ONLY to exercise the display
    code path end to end; the resulting wattage is not, and must never be
    cited as, real Casa Justice data.
    """
    l1 = field_by_key("load_power_l1")
    l2 = field_by_key("load_power_l2")
    expected = int(
        justice_registers_synthetic_complete[l1.address]
        + justice_registers_synthetic_complete[l2.address]
    )
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    state = hass.states.get("sensor.justice_inv_1_load_power_total")
    assert state.state == str(expected)
    assert "." not in state.state

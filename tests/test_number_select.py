"""Writable platforms: numbers and selects, always with read-back.

Three deviations from the task brief's own Step 1 snippet, each confirmed by
hand before writing the test this way rather than as originally given:

1. `setup_entry()` calls here use `justice_registers_synthetic_complete`, not
   the plain `justice_registers` fixture the brief used. `async_probe()`
   always reads all 10 of `registers.BLOCKS`; "control_high" (0xE210-0xE21E,
   holding `bms_communication`/`bms_protocol`) and "faults" (0x0200-0x0207)
   have zero recorded raws in the real Casa Justice capture (see
   `tests/conftest.py`'s own docstring and `tests/test_init.py`'s module
   docstring, which hit this exact issue two tasks earlier) -- driving setup
   off the plain fixture raises an uncaught `LookupError: fixture gap:
   0x0200` out of `async_setup_entry`, before a single entity exists.
   Verified by hand: swapping the fixture back in reproduces exactly that
   traceback.

2. `test_number_entities_expose_the_write_range`'s `boost_charge_voltage`
   bounds are 48.0-58.4 V (raw 120-146, `WriteSpec(120, 146)`), not the
   brief's own 40.0/64.0. Those wider numbers are the exact blanket
   40-64 V range this task's brief text calls out as the thing NOT to
   reintroduce ("An earlier review found the plan had given every 0.4-scale
   voltage threshold a blanket range... tightened to the manufacturer's
   per-parameter values... do NOT widen them back") -- the Step 1 code block
   itself was never updated after that fix landed in `registers.py`.
   `tests/test_registers.py::test_voltage_threshold_write_ranges_match_manual`
   already pins `boost_voltage`'s raw range at the unit level (120, 146);
   this file adds the corresponding entity-level assertion instead of
   copying the stale numbers forward.

3. `test_firmware_rejection_surfaces_as_an_error`'s `ClampingTransport`
   (subclassing `FakeTransport` and overriding the PUBLIC `write_holding`)
   is silently defeated by `coordinator.async_write_raw`'s required use of
   `atomic()`: the yielded handle's `write_holding` calls
   `FakeTransport._write_holding_locked` directly, never the public method a
   subclass would override, so the override never runs and the write
   actually sticks -- the opposite of what the test's own
   `pytest.raises(HomeAssistantError, match="read back")` needs. Confirmed
   by hand: pasting the brief's `ClampingTransport` as written and running
   the resulting test fails with no exception raised at all, not the
   expected one. `tests/test_coordinator.py::test_write_mismatch_raises`
   found and fixed the identical bug one task earlier, at the coordinator
   layer, using the fake's sanctioned `no_stick_writes` mechanism instead
   ("a write that is recorded but does not stick" -- documented in
   `FakeTransport.__init__`'s own docstring); this file follows that same
   precedent at the entity layer.

Two tests beyond the brief's six:

- `test_a_fully_supported_unit_gets_18_numbers_and_5_selects` pins the total
  entity count this task's brief text asserts ("those 23 fields are yours to
  claim") -- 18 non-enum writable fields become numbers, 5 enum writable
  fields become selects, matching `registers.FIELDS`.
- `test_firmware_rejection_via_write_error_surfaces_as_an_error` exercises
  the OTHER write-failure shape: the device answers `IllegalDataValue`
  outright (`InvalidRegisterValueError`, via `write_errors`), which surfaces
  as a `HomeAssistantError` WITHOUT "read back" in it -- distinct wording
  from a write that is silently accepted but does not stick. See this file's
  deviation note 3 above and `coordinator.async_write_raw`'s docstring for
  why the two shapes produce different messages.

Fix round 1 (Opus re-review) additions -- see task-11-report.md's own "Fix
round 1" section for the falsifiability runs behind each:

- `test_out_of_range_refusal_names_our_bounds_not_ha_defaults` (Important):
  the original `test_number_out_of_range_is_refused` (`soc_low_alarm`/250)
  and the original `soc_low_alarm` min/max assertions in
  `test_number_entities_expose_the_write_range` both happen to use a field
  whose REAL range (0-100) is identical to `NumberEntity`'s own installed
  defaults (`DEFAULT_MIN_VALUE=0.0`/`DEFAULT_MAX_VALUE=100.0`) -- so neither
  one can tell "our WriteSpec-derived bounds are wired to this entity" apart
  from "HA's own defaults happen to produce the same number". Measured:
  deleting `SrneNumber.__init__`'s entire bounds-assignment block left 8 of
  this file's (then) 9 tests green. The new test uses `equalize_voltage`
  (48.0-58.0 V, exact at this scale) and a value (65.0) chosen to be inside
  HA's default range but outside ours, then asserts the raised
  `ServiceValidationError`'s `translation_placeholders` NAME our bounds
  (`"48.0"`/`"58.0"`), not HA's defaults -- the one assertion shape that
  actually distinguishes the two.
- `test_number_entities_expose_the_write_range` additionally pins the three
  ampere-scale writable fields (`charge_stop_current`,
  `ac_charge_current_limit`, `max_charge_current` -- minor point 2: these
  had no entity-level bound assertion anywhere before this round).
- `test_reprobe_signal_does_not_duplicate_number_entities` and
  `test_reprobe_signal_does_not_duplicate_select_entities` (minor point 3):
  `number.py`/`select.py`'s own `added_field_keys` dedup branches had no
  signal and no test, unlike `sensor.py`'s equivalent (Fix round 2, commit
  `ce62625`) -- both platforms now log a DEBUG line naming the skipped key
  on that branch, and the new tests assert on that text via `caplog`, plus
  a negative assertion that HA's own "does not generate unique IDs"
  rejection text never appears (proving the guard skipped the key before
  HA's registry ever saw a duplicate, not that HA cleaned up after one this
  code shouldn't have tried to create).

2026-09-14 (`overdischarge_voltage` max widened 120 -> 122, see
`registers.py`): `test_number_entities_expose_the_write_range` gained
`over_discharge_voltage` entity-level assertions (state 48.8, min 40.0,
max 48.8), extending this file's existing voltage-threshold bound-pinning
mechanism rather than adding a parallel one. This is the entity-level twin
of `test_registers.py::test_voltage_threshold_write_ranges_match_manual`'s
now-updated `overdischarge_voltage` row (100, 122). Falsifiability: with
`registers.py`'s `max_raw` reverted to 120, this test's own `max ==
pytest.approx(48.8)` assertion goes red (actual 48.0); widened past 122 it
also goes red (actual > 48.8) -- see task-2-e00d-widening-report.md for the
recorded transcript.
"""

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.srne_inverter.const import SIGNAL_NEW_ENTITIES
from custom_components.srne_inverter.transport.base import InvalidRegisterValueError
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport
from tests.test_init import setup_entry


async def test_number_entities_expose_the_write_range(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Pins bounds for a field per writable-field shape: a percentage field
    whose real range happens to coincide with HA's own NumberEntity
    defaults (0-100, `soc_low_alarm`), a 0.4-scale voltage threshold
    (`boost_charge_voltage`), a second voltage threshold whose max was
    deliberately widened past the manual (`over_discharge_voltage`, see
    below), and the three ampere-scale fields (Fix round 1, minor point 2
    -- previously pinned nowhere at all).

    `soc_low_alarm`'s min/max assertions below do NOT by themselves prove
    this entity's own bounds wiring works -- `NumberEntity`'s installed
    defaults are `DEFAULT_MIN_VALUE=0.0`/`DEFAULT_MAX_VALUE=100.0`, so they
    would read identically even with `SrneNumber.__init__`'s entire bounds
    block deleted. Confirmed by hand (Fix round 1 falsifiability run,
    recorded in task-11-report.md): deleting that block leaves 8 of this
    file's 9 original tests green, this one included, for exactly that
    reason. `test_out_of_range_refusal_names_our_bounds_not_ha_defaults`
    below is the test that actually discriminates our wiring from HA's
    defaults; kept here anyway because it is still a true statement about
    the entity's displayed state.
    """
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    state = hass.states.get("number.justice_inv_1_soc_low_alarm")
    assert state.state == "15.0"
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 100

    voltage = hass.states.get("number.justice_inv_1_boost_charge_voltage")
    assert voltage.state == "57.6"
    # Manufacturer's per-parameter range (manual item 09: 48-58.4 V), NOT the
    # blanket 40-64 V this task's brief warns against reintroducing -- see
    # this file's module docstring, deviation 2. This one DOES discriminate
    # our wiring from HA's defaults (58.4 != 100).
    assert voltage.attributes["min"] == pytest.approx(48.0)
    assert voltage.attributes["max"] == pytest.approx(58.4)
    assert voltage.attributes["step"] == pytest.approx(0.4)

    # overdischarge_voltage: max is 48.8 V (raw 122), NOT the inverter
    # manual's own printed 48.0 V (raw 120) -- widened because Casa Justice
    # inv1 has 0xE00D recorded at raw 122 in both captures of
    # justice_inv1_settings.json, deliberately set to follow the battery
    # manual's 49 V shutdown voltage instead (see registers.py's
    # overdischarge_voltage comment for the full evidence chain). Pinning
    # 48.8 here, not 48.0, is what catches this bound being reverted to 120
    # OR widened past 122 -- either mutation moves this assertion off 48.8.
    overdischarge = hass.states.get("number.justice_inv_1_over_discharge_voltage")
    assert overdischarge.state == "48.8"
    assert overdischarge.attributes["min"] == pytest.approx(40.0)
    assert overdischarge.attributes["max"] == pytest.approx(48.8)
    assert overdischarge.attributes["step"] == pytest.approx(0.4)

    # Fix round 1, minor point 2: the three current-scale (0.1 A) writable
    # fields, previously pinned nowhere in this file -- only the ten
    # voltage thresholds had any entity-level bound assertion at all.
    charge_stop_current = hass.states.get("number.justice_inv_1_charge_stop_current")
    assert charge_stop_current.attributes["min"] == pytest.approx(0.0)
    assert charge_stop_current.attributes["max"] == pytest.approx(10.0)

    ac_charge_limit = hass.states.get("number.justice_inv_1_ac_charge_current_limit")
    assert ac_charge_limit.attributes["min"] == pytest.approx(0.0)
    assert ac_charge_limit.attributes["max"] == pytest.approx(120.0)

    max_charge_current = hass.states.get("number.justice_inv_1_max_charge_current")
    assert max_charge_current.attributes["min"] == pytest.approx(0.0)
    assert max_charge_current.attributes["max"] == pytest.approx(200.0)


async def test_out_of_range_refusal_names_our_bounds_not_ha_defaults(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Fix round 1 (Opus review, Important): the ONLY test that can
    actually tell "our WriteSpec-derived bounds are wired to this entity"
    apart from "HA's own NumberEntity defaults (0-100) happen to let this
    through/refuse it anyway".

    `equalize_voltage`'s real range is 48.0-58.0 V (`WriteSpec(120, 145)`,
    exact at this scale -- no float noise to fight in the assertion). 65.0
    is chosen deliberately: ABOVE our real max (58.0) but still INSIDE HA's
    own NumberEntity default range (0.0-100.0). With the entity's bounds
    wired correctly, `homeassistant.components.number`'s own
    `async_set_value` service handler checks `value < entity.min_value or
    value > entity.max_value` BEFORE ever calling `async_set_native_value`
    and raises `ServiceValidationError` (a `HomeAssistantError` subclass)
    with `translation_placeholders["min_value"]`/`["max_value"]` set from
    `entity.min_value`/`entity.max_value` -- i.e. FROM `_attr_native_min_value`/
    `_attr_native_max_value`, which is exactly what this test pins by
    asserting their string values name 48.0/58.0, not 0.0/100.0.

    Falsifiability (Fix round 1, recorded in task-11-report.md): deleting
    `SrneNumber.__init__`'s bounds-assignment block makes `native_min_value`/
    `native_max_value` fall back to HA's `DEFAULT_MIN_VALUE`/
    `DEFAULT_MAX_VALUE` (0.0/100.0) -- 65.0 is INSIDE that default range, so
    HA's own service-level check no longer fires at all, and this test's
    `pytest.raises(ServiceValidationError)` fails to match (the call instead
    proceeds to `registers.encode()`, which raises a plain `ValueError` --
    wrapped as a plain `HomeAssistantError`, not a `ServiceValidationError`,
    by `coordinator.async_write_field` -- a different exception TYPE, not
    just different text). Confirmed by hand: this exact mutation turns this
    test red while leaving `test_number_out_of_range_is_refused` (below,
    `soc_low_alarm`/250) green, which is precisely the gap this test closes.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    with pytest.raises(ServiceValidationError) as excinfo:
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": "number.justice_inv_1_equalize_voltage", "value": 65.0},
            blocking=True,
        )
    assert excinfo.value.translation_placeholders["min_value"] == "48.0"
    assert excinfo.value.translation_placeholders["max_value"] == "58.0"
    assert transport.writes == []


async def test_setting_a_number_writes_and_reads_back(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.justice_inv_1_soc_low_alarm", "value": 16},
        blocking=True,
    )
    assert transport.writes == [(0xE01E, 16)]
    assert hass.states.get("number.justice_inv_1_soc_low_alarm").state == "16.0"


async def test_number_out_of_range_is_refused(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    with pytest.raises((HomeAssistantError, ValueError)):
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": "number.justice_inv_1_soc_low_alarm", "value": 250},
            blocking=True,
        )
    assert transport.writes == []


async def test_select_options_and_current_value(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    state = hass.states.get("select.justice_inv_1_output_priority")
    assert state.attributes["options"] == ["SOL", "UTI", "SBU", "SUB"]
    assert hass.states.get("select.justice_inv_1_battery_type").state == "USER"


async def test_selecting_an_option_writes_the_raw_code(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.justice_inv_1_output_priority", "option": "SBU"},
        blocking=True,
    )
    assert transport.writes == [(0xE204, 2)]
    assert hass.states.get("select.justice_inv_1_output_priority").state == "SBU"


async def test_firmware_rejection_surfaces_as_an_error(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """E215 (bms_communication) exists and is writable; simulate a device
    that accepts the write but the value does not stick, via
    `no_stick_writes` -- see this file's module docstring, deviation 3, for
    why the brief's own `ClampingTransport` subclass cannot actually produce
    this scenario.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete, no_stick_writes=[0xE215]
    )
    await setup_entry(hass, transport)
    with pytest.raises(HomeAssistantError, match="read back"):
        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": "select.justice_inv_1_bms_communication", "option": "CAN"},
            blocking=True,
        )


async def test_firmware_rejection_via_write_error_surfaces_as_an_error(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """The OTHER write-failure shape: the device answers IllegalDataValue
    outright (InvalidRegisterValueError) instead of silently accepting and
    dropping the write. Injected via `write_errors`, the fake's documented
    mechanism for a value-dependent firmware rejection. This is what a user
    sees when the device refuses the write itself, as opposed to the
    "accepted but didn't stick" case above -- distinguishable in the UI by
    the absence of "read back" in the error text (see
    `coordinator.async_write_raw`'s two distinct HomeAssistantError
    messages).
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        write_errors=[InvalidRegisterValueError("fake: 0xE01E refuses this value")],
    )
    await setup_entry(hass, transport)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": "number.justice_inv_1_soc_low_alarm", "value": 16},
            blocking=True,
        )
    assert "read back" not in str(excinfo.value)
    assert transport.writes == [(0xE01E, 16)]


async def test_a_fully_supported_unit_gets_18_numbers_and_5_selects(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Pins the total entity count for a unit where every block is
    SUPPORTED: 18 non-enum writable fields (numbers) + 5 enum writable
    fields (selects) == the 23 writable fields in `registers.FIELDS`.
    """
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    assert len(hass.states.async_entity_ids("number")) == 18
    assert len(hass.states.async_entity_ids("select")) == 5


async def test_writable_fields_of_an_unsupported_block_are_not_created(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """A field must not get a number/select entity when its own block is not
    currently classified SUPPORTED -- the same rule sensor.py/binary_sensor.py
    already enforce (Task 10), exercised here for the writable platforms.
    "control_low" (0xE200-0xE20F) holds `output_priority` (select) and
    `ac_charge_current_limit`/`max_charge_current` (numbers); marking it
    unsupported must drop exactly those three, while a writable field from a
    different, still-SUPPORTED block (`soc_low_alarm`, in "settings_high")
    stays.
    """
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=(*DEFAULT_UNSUPPORTED, range(0xE200, 0xE210)),
    )
    await setup_entry(hass, transport)
    assert hass.states.get("select.justice_inv_1_output_priority") is None
    assert hass.states.get("number.justice_inv_1_ac_charge_current_limit") is None
    assert hass.states.get("number.justice_inv_1_max_charge_current") is None
    assert hass.states.get("number.justice_inv_1_soc_low_alarm") is not None


async def test_reprobe_signal_does_not_duplicate_number_entities(
    hass, justice_registers_synthetic_complete, enable_custom_integrations, caplog
):
    """Fix round 1 (Opus review, minor point 3): `number.py`'s own
    `added_field_keys` dedup branch had no signal and no test, unlike
    `sensor.py`'s equivalent guard (Fix round 2, commit ce62625) -- firing
    `SIGNAL_NEW_ENTITIES` a second time with nothing newly supported used to
    be provable only by "the entity id set didn't change", which HA's own
    entity registry would produce anyway even with this guard deleted (a
    duplicate unique_id is independently rejected there, logged as "...does
    not generate unique IDs... already exists - ignoring"). The debug line
    `number.py`'s builder() now emits on this branch specifically
    discriminates "our guard skipped it" from "HA cleaned up after us".

    A first draft of this test asserted only the bare substring
    `"already added, skipping re-probe rebuild" in caplog.text`, with no
    check on WHICH logger emitted it -- and that passed even with this
    file's own mutation of `number.py`'s log line alone, because
    `SIGNAL_NEW_ENTITIES` is ONE shared dispatcher signal every platform on
    this entry listens to (`entity.py`'s own `async_setup_field_platform`
    docstring); firing it re-runs ALL SIX platforms' builders in the same
    call, and `sensor.py`/`select.py`'s own (unmutated) dedup branches log
    the exact same text for their own already-added fields. Filtering
    `caplog.records` by `record.name == "custom_components.srne_inverter.
    number"` is what actually isolates THIS platform's own guard.
    """
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    before = set(hass.states.async_entity_ids("number"))
    caplog.clear()
    async_dispatcher_send(hass, SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id))
    await hass.async_block_till_done()
    after = set(hass.states.async_entity_ids("number"))
    assert after == before
    own_debug_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.srne_inverter.number"
    ]
    assert any(
        "already added, skipping re-probe rebuild" in line for line in own_debug_lines
    ), caplog.text
    assert "does not generate unique IDs" not in caplog.text


async def test_reprobe_signal_does_not_duplicate_select_entities(
    hass, justice_registers_synthetic_complete, enable_custom_integrations, caplog
):
    """Same as `test_reprobe_signal_does_not_duplicate_number_entities`
    above, for `select.py`'s own dedup branch (Fix round 1, minor point 3) --
    including the same logger-name filter, for the same reason (one shared
    `SIGNAL_NEW_ENTITIES` re-runs every platform's builder, not just this
    one).
    """
    entry = await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    before = set(hass.states.async_entity_ids("select"))
    caplog.clear()
    async_dispatcher_send(hass, SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id))
    await hass.async_block_till_done()
    after = set(hass.states.async_entity_ids("select"))
    assert after == before
    own_debug_lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "custom_components.srne_inverter.select"
    ]
    assert any(
        "already added, skipping re-probe rebuild" in line for line in own_debug_lines
    ), caplog.text
    assert "does not generate unique IDs" not in caplog.text

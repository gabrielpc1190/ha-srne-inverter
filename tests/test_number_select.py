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
"""

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.srne_inverter.transport.base import InvalidRegisterValueError
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport
from tests.test_init import setup_entry


async def test_number_entities_expose_the_write_range(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    state = hass.states.get("number.justice_inv_1_soc_low_alarm")
    assert state.state == "15.0"
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 100

    voltage = hass.states.get("number.justice_inv_1_boost_charge_voltage")
    assert voltage.state == "57.6"
    # Manufacturer's per-parameter range (manual item 09: 48-58.4 V), NOT the
    # blanket 40-64 V this task's brief warns against reintroducing -- see
    # this file's module docstring, deviation 2.
    assert voltage.attributes["min"] == pytest.approx(48.0)
    assert voltage.attributes["max"] == pytest.approx(58.4)
    assert voltage.attributes["step"] == pytest.approx(0.4)


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

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
    registers = {
        **justice_registers_synthetic_complete,
        0x0204: 0, 0x0205: 0x0040, 0x0206: 0, 0x0207: 0,
    }
    entry = await setup_entry(hass, FakeTransport(registers))
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.justice_inv_1_fault_active")
    assert state.state == "on"
    assert state.attributes["fault_word_2"] == "0x0040"


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
    assert hass.states.get("sensor.justice_inv_1_running_days") is None

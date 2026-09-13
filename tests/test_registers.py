"""Register map and decoder, validated against the recorded Justice capture."""

import pytest

from custom_components.srne_inverter import registers as R


def test_blocks_cover_only_verified_regions():
    addrs = {b.addr: b.count for b in R.BLOCKS}
    assert addrs[0x0014] == 10
    assert addrs[0x0100] == 15
    assert addrs[0xF02C] == 24
    # Nothing may reach into regions the firmware answers IllegalDataAddress for.
    for block in R.BLOCKS:
        last = block.addr + block.count - 1
        assert not (0x0112 <= block.addr <= 0x01FF)
        assert not (0xE030 <= block.addr <= 0xE1FF)
        assert last <= 0xE21E or block.addr >= 0xF000 or last < 0xE000
        assert block.count <= 24


def test_every_field_lives_inside_its_block():
    for field in R.FIELDS:
        block = R.block_by_addr(field.block_addr)
        assert 0 <= field.offset < block.count
        assert field.offset + field.words <= block.count


def test_field_keys_and_labels_are_unique():
    keys = [f.key for f in R.FIELDS] + [d.key for d in R.DERIVED]
    assert len(keys) == len(set(keys))


def test_decode_battery_block(justice_registers):
    values = R.decode(justice_registers)
    assert values["battery_soc"] == 55
    assert values["battery_voltage"] == pytest.approx(53.1)
    # raw 65527 == -9; published sign is flipped so positive means charging.
    assert values["battery_current"] == pytest.approx(0.9)
    assert values["charge_state"] == "Quick charge (CC)"


def test_decode_inverter_block(justice_registers):
    values = R.decode(justice_registers)
    assert values["machine_state"] == "AC bypass"
    assert values["bus_voltage"] == pytest.approx(526.6)
    assert values["grid_voltage_l1"] == pytest.approx(122.5)
    assert values["grid_voltage_l2"] == pytest.approx(121.8)
    assert values["output_voltage_l2"] == pytest.approx(123.1)
    assert values["output_current_l2"] == pytest.approx(0.5)
    assert values["grid_charge_current"] == pytest.approx(1.0)
    assert values["load_power_l1"] == 175
    assert values["temperature_inverter"] == pytest.approx(42.1)


def test_decode_voltage_thresholds_use_x04_scale(justice_registers):
    values = R.decode(justice_registers)
    assert values["boost_voltage"] == pytest.approx(57.6)   # raw 144
    assert values["float_voltage"] == pytest.approx(57.6)
    assert values["battery_type"] == "USER"                 # raw 0


def test_decode_derived_battery_power(justice_registers):
    values = R.decode(justice_registers)
    # 53.1 V x 0.9 A = 47.79 W, positive because it is charging.
    assert values["battery_power"] == pytest.approx(53.1 * 0.9, rel=1e-3)


def test_decode_firmware_version(justice_registers):
    values = R.decode(justice_registers)
    assert values["firmware_version"] == "V8.18"
    assert values["hardware_version"] == "V3.04"


def test_decode_fault_words_as_hex(justice_registers):
    values = R.decode({**justice_registers, 0x0204: 0x0041})
    assert values["fault_word_1"] == "0x0041"


def test_decode_skips_missing_registers(justice_registers):
    partial = {k: v for k, v in justice_registers.items() if k < 0xE000}
    values = R.decode(partial)
    assert "battery_soc" in values
    assert "boost_voltage" not in values
    assert "battery_power" in values


def test_decode_u32_total_counters():
    values = R.decode({0xF034: 5000, 0xF035: 1})
    assert values["battery_charge_ah_total"] == 5000 + 65536


def test_encode_roundtrip_number():
    field = R.field_by_key("boost_voltage")
    assert R.encode(field, 57.6) == 144
    assert field.write is not None
    assert field.write.min_raw == 100
    assert field.write.max_raw == 160


def test_encode_roundtrip_enum():
    field = R.field_by_key("output_priority")
    assert R.encode(field, "SBU") == 2
    with pytest.raises(ValueError):
        R.encode(field, "NOPE")


def test_charge_priority_is_read_only():
    """Firmware rejects writes to E20F (verified at Casa Justice)."""
    assert R.field_by_key("charge_priority").write is None
    for key in ("ac_input_range",):
        assert R.field_by_key(key).write is None

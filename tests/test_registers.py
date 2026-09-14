"""Register map and decoder, validated against the recorded Justice capture."""

import json

import pytest

from custom_components.srne_inverter import registers as R
from tests.conftest import FIXTURES

# Verified-present segments on firmware V8.18.006 (inclusive), from the plan's
# device facts. A block's full extent (addr..last), not just its start address,
# must fit entirely inside one of these -- half-in-half-out is a bug, not a
# degraded-but-safe read.
_VERIFIED_SEGMENTS = (
    (0x0014, 0x001D),  # device_info
    (0x0100, 0x010E),  # battery
    (0x0200, 0x023F),  # faults + inverter_a + inverter_b
    (0xE000, 0xE02F),  # settings_low + settings_high
    (0xE200, 0xE21E),  # control_low + control_high
    (0xF02C, 0xF043),  # meter
)


def test_blocks_cover_only_verified_regions():
    addrs = {b.addr: b.count for b in R.BLOCKS}
    assert addrs[0x0014] == 10
    assert addrs[0x0100] == 15
    assert addrs[0xF02C] == 24
    # Nothing may reach into regions the firmware answers IllegalDataAddress for.
    # Checked over the block's FULL extent (addr..last), not just block.addr --
    # Block(0x0100, 24) starts inside the verified battery segment but would end
    # at 0x0117, inside the absent 0x0112+ region, and must still be caught.
    for block in R.BLOCKS:
        last = block.addr + block.count - 1
        assert any(
            seg_start <= block.addr and last <= seg_end
            for seg_start, seg_end in _VERIFIED_SEGMENTS
        ), f"{block.name} (0x{block.addr:04X}..0x{last:04X}) leaves a verified segment"
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
    assert field.write.min_raw == 120   # manual item 09: 48-58.4 V
    assert field.write.max_raw == 146


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


@pytest.mark.parametrize(
    ("key", "min_raw", "max_raw"),
    [
        # raw = manual volts / 0.4. See
        # docs/2026-09-13_manual-bluesun-spi10k_tabla-de-parametros.md
        # (SPI-10K-UP), confirmed row by row against that file -- except
        # overdischarge_voltage (see its own comment below and the matching
        # one in registers.py): its max is deliberately widened past that
        # manual's printed row.
        ("boost_voltage", 120, 146),                 # item 09: 48-58.4 V
        ("float_voltage", 120, 146),                 # item 11: 48-58.4 V
        ("equalize_voltage", 120, 145),               # item 17: 48-58 V
        # item 12 prints 40-48 V (raw 100-120), but Casa Justice inv1 has
        # this register recorded at raw 122 (48.8 V) in
        # tests/fixtures/justice_inv1_settings.json, set deliberately to
        # follow the battery manual's 49 V shutdown voltage instead -- see
        # registers.py's overdischarge_voltage comment for the full chain.
        ("overdischarge_voltage", 100, 122),          # item 12: 40-48 V manual, widened to 48.8 V
        ("undervoltage_alarm", 100, 130),             # item 14: 40-52 V
        ("discharge_limit_voltage", 100, 130),        # item 15: 40-52 V
        ("undervoltage_recovery", 110, 136),          # item 35: 44-54.4 V
        ("recharge_voltage", 110, 135),               # item 37: 44-54 V
        ("battery_to_mains_voltage", 100, 130),       # item 04: 40-52 V
        ("mains_to_battery_voltage", 120, 150),       # item 05: 48-60 V
    ],
)
def test_voltage_threshold_write_ranges_match_manual(key, min_raw, max_raw):
    """Manufacturer's per-parameter ranges, not a blanket 40-64 V.

    A too-wide range here would let a Number entity (Task 11) set e.g.
    Over-discharge Voltage to 64 V, which means "shut inverter output down
    whenever the battery is below 64 V" -- i.e. always. (overdischarge_voltage's
    own max is a deliberate, narrow exception to "matches the manual exactly" --
    48.8 V, not 64 V -- see the parametrize comment above and registers.py.)
    """
    write = R.field_by_key(key).write
    assert write is not None
    assert write.min_raw == min_raw
    assert write.max_raw == max_raw


def test_unverified_write_ranges_are_read_only():
    """No source in this repo backs a write range for these two registers.

    Neither the YAML profile, the design spec's v1 Numbers list, the manual's
    LCD-settable parameters, nor tests/fixtures/justice_inv1_settings.json's
    recorded successful writes cover E005/E006. Read-only until a live
    safe-write test proves the real range.
    """
    assert R.field_by_key("overvoltage_threshold").write is None
    assert R.field_by_key("charge_limit_voltage").write is None


def test_encode_rejects_off_step_values():
    """A value the register cannot represent must raise, not round quietly."""
    field = R.field_by_key("boost_voltage")  # scale=0.4, write=(120, 146)
    with pytest.raises(ValueError):
        R.encode(field, 39.9)   # 39.9 / 0.4 = 99.75, not an integer raw
    with pytest.raises(ValueError):
        R.encode(field, 57.75)  # 57.75 / 0.4 = 144.375, not an integer raw
    assert R.encode(field, 57.6) == 144  # still exact on-grid


def test_grid_current_l2_reads_from_0x0238_not_0x022b(
    probe_inv1_registers, probe_inv2_registers
):
    """Fix (live run, Casa Justice, 2026-09-14): grid_current_l2 used to map
    to 0x022B (design decision 1, locked, chosen from the YAML profile over
    the older justice_inverters_read.py's 0x0238). Measured on BOTH units
    with machine_state=2 ("AC bypass", so the grid feeds the load): 0x022B
    read 0.0 A on both while the load visibly drew current; 0x0238 tracked
    it, exactly as grid current should in bypass. The old script was right.
    See registers.py's own comment on the 0x0223 block for the full trace.
    """
    field = R.field_by_key("grid_current_l2")
    assert field.address == 0x0238

    for registers, expected_raw in (
        (probe_inv1_registers, 46),   # docs/evidence/2026-09-14_probe-inv1.json
        (probe_inv2_registers, 185),  # docs/evidence/2026-09-14_probe-inv2.json
    ):
        assert registers[0x0238] == expected_raw
        assert registers[0x022B] == 0  # the old, wrong mapping: a confident 0.0 A
        values = R.decode(registers)
        assert values["grid_current_l2"] == pytest.approx(expected_raw * 0.1)

    # 0x022B is deliberately unmapped -- nothing in FIELDS may point at it.
    assert all(f.address != 0x022B for f in R.FIELDS)


def test_device_info_tail_is_not_claimed_as_a_serial(
    probe_inv1_registers, probe_inv2_registers
):
    """Fix (live run, Casa Justice, 2026-09-14): 0x0018-0x001B used to be
    decoded as `inverter_serial`, marked "unverified" (design decision 5).
    Live: inv1 (slave 1) reads [0, 0, 1, 45], inv2 (slave 2) reads
    [0, 0, 2, 45] -- the third word is the slave id / RS485 address, not a
    serial. The field is renamed `device_info_tail` (FieldKind.HEX_WORDS,
    not SERIAL, which no longer exists) so it stops claiming more than is
    known.
    """
    field = R.field_by_key("device_info_tail")
    assert field.kind is R.FieldKind.HEX_WORDS
    assert field.address == 0x0018

    for registers, slave_id in (
        (probe_inv1_registers, 1),
        (probe_inv2_registers, 2),
    ):
        assert [registers[0x0018 + i] for i in range(4)] == [0, 0, slave_id, 45]
        # The third word matches this unit's own separately-decoded
        # rs485_address (0xE200) exactly -- independent confirmation it is
        # the slave id, not part of some serial.
        assert R.decode(registers)["rs485_address"] == slave_id

    assert "inverter_serial" not in {f.key for f in R.FIELDS}
    assert R.FieldKind("hex_words") is R.FieldKind.HEX_WORDS
    assert not hasattr(R.FieldKind, "SERIAL")


def test_decode_matches_golden_snapshot(justice_registers):
    """Every decodable field, not just the ~19 individually asserted above.

    Closes a real gap: test_decode_voltage_thresholds_use_x04_scale alone
    cannot distinguish boost_voltage/float_voltage/equalize_voltage (E008,
    E009, E007), because all three happen to read raw 144 in this fixture --
    swapping boost_voltage and float_voltage would still pass that test.
    """
    golden = json.loads(
        (FIXTURES / "justice_inv1_decoded_golden.json").read_text()
    )
    values = R.decode(justice_registers)
    assert values == golden


def test_decode_distinguishes_float_from_boost_and_equalize(
    justice_settings_after_bms_off,
):
    """Guards against a float_voltage (E009) address swap with boost_voltage
    (E008) or equalize_voltage (E007) -- a real hole the golden snapshot above
    cannot see.

    In the `before` capture (what justice_registers/the golden snapshot use),
    E007, E008 and E009 all read raw 144 because a live BMS pins them to the
    same fixed value while connected, so swapping which Field points at which
    of those three addresses would still produce an identical decoded output
    there. justice_inv1_settings.json's `after_bms_off` map, taken moments
    later with the BMS disconnected, is a second real capture where they are
    NOT all equal (E007=142, E008=142, E009=140) -- enough to prove
    float_voltage is not wired to the same address as boost_voltage or
    equalize_voltage.

    This closes the float-vs-{boost,equalize} half of the hole. It does NOT
    close a boost_voltage/equalize_voltage (E008/E007) swap against each
    other: E007 and E008 are identical in every capture THIS fixture has
    (144/144 in `before`, 142/142 in `after_bms_off`). That remaining half
    was genuinely unfalsifiable with the evidence on hand at the time this
    test was written -- it needed a new capture where they differ, which did
    not exist yet. It exists now: test_decode_distinguishes_boost_from_equalize
    below closes it, using a 2026-09-14 live capture obtained specifically
    for that purpose. Do not delete THIS test as "redundant with the golden
    snapshot"; it is still the only test that can see the float-vs-the-other-
    two half at all.
    """
    values = R.decode(justice_settings_after_bms_off)
    assert values["float_voltage"] == pytest.approx(140 * 0.4)      # 56.0 V
    assert values["boost_voltage"] == pytest.approx(142 * 0.4)      # 56.8 V
    assert values["equalize_voltage"] == pytest.approx(142 * 0.4)   # 56.8 V


def test_decode_distinguishes_boost_from_equalize(justice_equalize_distinct):
    """Closes the boost_voltage/equalize_voltage (E008/E007) half of the
    ambiguity that test_decode_distinguishes_float_from_boost_and_equalize
    above explicitly could NOT close: every capture in this repo until now
    had E007 and E008 read the identical raw value (144/144 with the BMS
    active, 142/142 in `after_bms_off`), so a Field swap between those two
    specifically could not be detected by decoding alone -- boost_voltage
    and equalize_voltage could have had their addresses (E008/E007) swapped
    and every existing test, including the golden snapshot, would still
    pass.

    docs/evidence/2026-09-14_inv1-equalize-distinct.json is the first
    capture where they differ, obtained live at Casa Justice inverter 1:
    BMS communication was briefly disabled (E215 -> 0, the gate that
    otherwise pins E007-E009 to one BMS-dictated value -- see registers.py's
    own equalize_voltage/boost_voltage/float_voltage comment), E007 was
    written to a value distinct from E008/E009, then both E007 and E215
    were restored immediately afterward (progress.md's TASK 17 write-phase
    TRIAL D; all 48 E000-E02F registers confirmed unchanged after restore).
    Raw: E007=142 (equalize), E008=144 (boost), E009=144 (float), scale 0.4
    -> 56.8 V / 57.6 V / 57.6 V. With THIS half now closed too, an
    equalize_voltage/boost_voltage address swap is no longer unfalsifiable.
    """
    values = R.decode(justice_equalize_distinct)
    assert values["equalize_voltage"] == pytest.approx(142 * 0.4)  # 56.8 V
    assert values["boost_voltage"] == pytest.approx(144 * 0.4)     # 57.6 V
    assert values["float_voltage"] == pytest.approx(144 * 0.4)     # 57.6 V
    assert values["equalize_voltage"] != values["boost_voltage"]

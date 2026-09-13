"""Register map for SRNE / BlueSun energy-storage inverters (SRNE protocol V1.96).

Single source of truth for blocks, fields, scales, enums and write ranges.
Derived from docs/reference/inverter-bridge_srne_map.py and
docs/reference/ha-solarman-profile_srne_bluesun_justice.yaml, cross-checked
against the live capture of Casa Justice inverter 1 (firmware V8.18.006).

THIS MODULE MUST NOT IMPORT homeassistant -- tools/probe.py imports it on hosts
where Home Assistant is not installed. It also has no intra-package imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field as dc_field
from enum import StrEnum


class BlockTier(StrEnum):
    """How often a block is polled."""

    HOT = "hot"    # every scan_interval (default 10 s)
    WARM = "warm"  # every warm_interval (default 60 s)
    COLD = "cold"  # every cold_interval (default 300 s)


class FieldKind(StrEnum):
    """How a field's raw words become a state value."""

    NUMBER = "number"    # raw * scale, optionally signed / 2 words
    ENUM = "enum"        # raw -> EnumMap label
    HEX = "hex"          # raw -> "0xNNNN" (fault words)
    VERSION = "version"  # raw 818 -> "V8.18"
    SERIAL = "serial"    # N words -> "AABBCCDD"


@dataclass(frozen=True, slots=True)
class EnumMap:
    """Raw value <-> human label mapping for an enum register."""

    key: str
    values: dict[int, str] = dc_field(default_factory=dict)

    def label(self, raw: int) -> str:
        return self.values.get(raw, f"Unknown ({raw})")

    def raw_for(self, label: str) -> int | None:
        for raw, text in self.values.items():
            if text == label:
                return raw
        return None

    @property
    def options(self) -> list[str]:
        return [self.values[key] for key in sorted(self.values)]


@dataclass(frozen=True, slots=True)
class WriteSpec:
    """Allowed raw register range for a writable field.

    Ranges are in RAW register units, before `Field.scale` is applied.
    """

    min_raw: int
    max_raw: int
    step_raw: int = 1


@dataclass(frozen=True, slots=True)
class Block:
    """A contiguous run of holding registers read in one Modbus request."""

    addr: int
    count: int
    name: str
    tier: BlockTier

    @property
    def addresses(self) -> range:
        return range(self.addr, self.addr + self.count)


@dataclass(frozen=True, slots=True)
class Field:
    """One decoded value inside a block."""

    block_addr: int
    offset: int
    key: str
    label: str
    kind: FieldKind = FieldKind.NUMBER
    scale: float = 1.0
    signed: bool = False
    words: int = 1
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    enum: EnumMap | None = None
    write: WriteSpec | None = None
    category: str | None = None  # None | "config" | "diagnostic"
    precision: int | None = None

    @property
    def address(self) -> int:
        return self.block_addr + self.offset


@dataclass(frozen=True, slots=True)
class Derived:
    """A value computed from already-decoded fields (never read from Modbus)."""

    key: str
    label: str
    op: str  # "multiply" | "add"
    inputs: tuple[str, ...]
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    precision: int | None = None


CHARGE_STATE = EnumMap(
    "charge_state",
    {0: "Off", 1: "Quick charge (CC)", 2: "Constant voltage",
     4: "Float", 6: "Li activation", 8: "Full"},
)
MACHINE_STATE = EnumMap(
    "machine_state",
    {0: "Init", 1: "Standby", 2: "AC bypass", 3: "Inverter"},
)
BATTERY_TYPE = EnumMap(
    "battery_type",
    {0: "USER", 1: "SLD", 2: "FLD", 3: "GEL", 4: "L14", 5: "L15",
     6: "L16", 7: "N13", 8: "N14", 9: "No battery"},
)
OUTPUT_PRIORITY = EnumMap(
    "output_priority", {0: "SOL", 1: "UTI", 2: "SBU", 3: "SUB"}
)
CHARGE_PRIORITY = EnumMap(
    "charge_priority",
    {0: "CSO (PV first)", 1: "CUB (utility first)",
     2: "SNU (PV+utility)", 3: "OSO (PV only)"},
)
BMS_COMMUNICATION = EnumMap(
    "bms_communication", {0: "SLA (off)", 1: "RS485", 2: "CAN"}
)
BMS_PROTOCOL = EnumMap(
    "bms_protocol",
    {0: "PAC", 1: "RDA", 2: "AOG", 3: "OLT", 4: "HWD", 5: "DAQ", 6: "WOW",
     7: "SRNE", 8: "PYL", 9: "UOL", 10: "Code 10", 11: "Code 11",
     12: "Code 12", 13: "Code 13", 14: "Code 14", 15: "Code 15"},
)
BMS_CHARGE_LIMIT_MODE = EnumMap(
    "bms_charge_limit_mode", {0: "LCSET", 1: "LCBMS", 2: "LCINV"}
)

# Blocks are capped at 24 registers so a half-present region degrades per-half.
BLOCKS: tuple[Block, ...] = (
    Block(0x0100, 15, "battery", BlockTier.HOT),
    Block(0x0200, 8, "faults", BlockTier.HOT),
    Block(0x0210, 19, "inverter_a", BlockTier.HOT),
    Block(0x0223, 23, "inverter_b", BlockTier.HOT),
    Block(0xE000, 24, "settings_low", BlockTier.WARM),
    Block(0xE018, 24, "settings_high", BlockTier.WARM),
    Block(0xE200, 16, "control_low", BlockTier.WARM),
    Block(0xE210, 15, "control_high", BlockTier.WARM),
    Block(0x0014, 10, "device_info", BlockTier.COLD),
    Block(0xF02C, 24, "meter", BlockTier.COLD),
)

V, A, HZ, C, W, VA, PCT, AH, KWH, D = (
    "V", "A", "Hz", "°C", "W", "VA", "%", "Ah", "kWh", "d")
MEAS, TOTI = "measurement", "total_increasing"

FIELDS: tuple[Field, ...] = (
    # --- battery 0x0100 -------------------------------------------------
    Field(0x0100, 0, "battery_soc", "Battery SOC", unit=PCT,
          device_class="battery", state_class=MEAS),
    Field(0x0100, 1, "battery_voltage", "Battery Voltage", scale=0.1, unit=V,
          device_class="voltage", state_class=MEAS, precision=1),
    # 0x0102: raw is NEGATIVE while charging on this firmware. scale -0.1 both
    # applies the 0.1 A unit and flips the sign, so positive = charging.
    Field(0x0100, 2, "battery_current", "Battery Current", scale=-0.1,
          signed=True, unit=A, device_class="current", state_class=MEAS,
          precision=1),
    Field(0x0100, 3, "battery_temperature", "Battery Temperature", scale=0.1,
          signed=True, unit=C, device_class="temperature", state_class=MEAS,
          precision=1),
    Field(0x0100, 11, "charge_state", "Charge State", kind=FieldKind.ENUM,
          enum=CHARGE_STATE),

    # --- faults 0x0200 --------------------------------------------------
    Field(0x0200, 4, "fault_word_1", "Fault Word 1", kind=FieldKind.HEX,
          category="diagnostic"),
    Field(0x0200, 5, "fault_word_2", "Fault Word 2", kind=FieldKind.HEX,
          category="diagnostic"),
    Field(0x0200, 6, "fault_word_3", "Fault Word 3", kind=FieldKind.HEX,
          category="diagnostic"),
    Field(0x0200, 7, "fault_word_4", "Fault Word 4", kind=FieldKind.HEX,
          category="diagnostic"),

    # --- inverter_a 0x0210 ----------------------------------------------
    Field(0x0210, 0, "machine_state", "Machine State", kind=FieldKind.ENUM,
          enum=MACHINE_STATE),
    Field(0x0210, 2, "bus_voltage", "Bus Voltage", scale=0.1, unit=V,
          device_class="voltage", state_class=MEAS, precision=1),
    Field(0x0210, 3, "grid_voltage_l1", "Grid Voltage L1", scale=0.1, unit=V,
          device_class="voltage", state_class=MEAS, precision=1),
    Field(0x0210, 4, "grid_current_l1", "Grid Current L1", scale=0.1, unit=A,
          device_class="current", state_class=MEAS, precision=1),
    Field(0x0210, 5, "grid_frequency", "Grid Frequency", scale=0.01, unit=HZ,
          device_class="frequency", state_class=MEAS, precision=2),
    Field(0x0210, 6, "output_voltage_l1", "Output Voltage L1", scale=0.1,
          unit=V, device_class="voltage", state_class=MEAS, precision=1),
    Field(0x0210, 7, "output_current_l1", "Output Current L1", scale=0.1,
          signed=True, unit=A, device_class="current", state_class=MEAS,
          precision=1),
    Field(0x0210, 8, "output_frequency", "Output Frequency", scale=0.01,
          unit=HZ, device_class="frequency", state_class=MEAS, precision=2),
    Field(0x0210, 11, "load_power_l1", "Load Power L1", unit=W,
          device_class="power", state_class=MEAS),
    Field(0x0210, 12, "load_apparent_power_l1", "Load Apparent Power L1",
          unit=VA, device_class="apparent_power", state_class=MEAS),
    # 0x021E -- current the charger draws from the grid. The key diagnostic
    # register for the Casa Justice "no carga desde red" case.
    Field(0x0210, 14, "grid_charge_current", "Grid Charge Current", scale=0.1,
          unit=A, device_class="current", state_class=MEAS, precision=1),
    Field(0x0210, 15, "load_percent", "Load Percentage", unit=PCT,
          state_class=MEAS),
    Field(0x0210, 16, "temperature_dc_dc", "Temperature DC-DC", scale=0.1,
          signed=True, unit=C, device_class="temperature", state_class=MEAS,
          precision=1),
    Field(0x0210, 17, "temperature_inverter", "Temperature Inverter",
          scale=0.1, signed=True, unit=C, device_class="temperature",
          state_class=MEAS, precision=1),
    Field(0x0210, 18, "temperature_transformer", "Temperature Transformer",
          scale=0.1, signed=True, unit=C, device_class="temperature",
          state_class=MEAS, precision=1),

    # --- inverter_b 0x0223 (offsets: address - 0x0223) --------------------
    Field(0x0223, 7, "grid_voltage_l2", "Grid Voltage L2", scale=0.1, unit=V,
          device_class="voltage", state_class=MEAS, precision=1),   # 0x022A
    Field(0x0223, 8, "grid_current_l2", "Grid Current L2", scale=0.1, unit=A,
          device_class="current", state_class=MEAS, precision=1),   # 0x022B
    Field(0x0223, 9, "output_voltage_l2", "Output Voltage L2", scale=0.1,
          unit=V, device_class="voltage", state_class=MEAS, precision=1),
    Field(0x0223, 11, "output_current_l2", "Output Current L2", scale=0.1,
          signed=True, unit=A, device_class="current", state_class=MEAS,
          precision=1),                                             # 0x022E
    Field(0x0223, 13, "load_current_l2", "Load Current L2", scale=0.1, unit=A,
          device_class="current", state_class=MEAS, precision=1),   # 0x0230
    Field(0x0223, 15, "load_power_l2", "Load Power L2", unit=W,
          device_class="power", state_class=MEAS),                  # 0x0232
    Field(0x0223, 17, "load_apparent_power_l2", "Load Apparent Power L2",
          unit=VA, device_class="apparent_power", state_class=MEAS),  # 0x0234

    # --- settings_low 0xE000 ---------------------------------------------
    Field(0xE000, 1, "pv_charge_current_max", "PV Charge Current Max",
          scale=0.1, unit=A, device_class="current", category="diagnostic",
          precision=1),
    Field(0xE000, 2, "battery_capacity", "Battery Capacity", unit=AH,
          category="diagnostic"),
    Field(0xE000, 3, "system_voltage", "System Voltage", unit=V,
          device_class="voltage", category="diagnostic"),
    Field(0xE000, 4, "battery_type", "Battery Type", kind=FieldKind.ENUM,
          enum=BATTERY_TYPE, write=WriteSpec(0, 9), category="config"),
    # E005/E006: writable maxima existed in no source in this repo (not the YAML
    # profile, not the design spec's v1 Numbers list, not the manual's LCD
    # parameters, and neither register appears among the successful writes in
    # tests/fixtures/justice_inv1_settings.json). Read-only until a live
    # safe-write test establishes the real range.
    Field(0xE000, 5, "overvoltage_threshold", "Over-voltage Threshold",
          scale=0.4, unit=V, device_class="voltage",
          category="diagnostic", precision=1),
    Field(0xE000, 6, "charge_limit_voltage", "Charge Limit Voltage",
          scale=0.4, unit=V, device_class="voltage",
          category="diagnostic", precision=1),
    # Write ranges below are the manufacturer's per-parameter ranges from
    # docs/2026-09-13_manual-bluesun-spi10k_tabla-de-parametros.md (SPI-10K-UP),
    # NOT a blanket 40-64 V -- e.g. letting overdischarge_voltage reach 64 V
    # would mean "shut inverter output down whenever the battery is below 64 V",
    # i.e. always. raw = manual volts / 0.4.
    Field(0xE000, 7, "equalize_voltage", "Equalize Voltage", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(120, 145),
          category="config", precision=1),  # item 17: 48-58 V
    Field(0xE000, 8, "boost_voltage", "Boost Charge Voltage", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(120, 146),
          category="config", precision=1),  # item 09: 48-58.4 V
    Field(0xE000, 9, "float_voltage", "Float Charge Voltage", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(120, 146),
          category="config", precision=1),  # item 11: 48-58.4 V
    Field(0xE000, 10, "recharge_voltage", "Recharge Voltage", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(110, 135),
          category="config", precision=1),  # item 37: 44-54 V
    Field(0xE000, 11, "undervoltage_recovery", "Under-voltage Recovery",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(110, 136), category="config",
          precision=1),  # item 35: 44-54.4 V
    Field(0xE000, 12, "undervoltage_alarm", "Under-voltage Alarm", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(100, 130),
          category="config", precision=1),  # item 14: 40-52 V
    Field(0xE000, 13, "overdischarge_voltage", "Over-discharge Voltage",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 120), category="config",
          precision=1),  # item 12: 40-48 V
    Field(0xE000, 14, "discharge_limit_voltage", "Discharge Limit Voltage",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 130), category="config",
          precision=1),  # item 15: 40-52 V
    Field(0xE000, 15, "discharge_stop_soc", "Discharge Stop SOC", unit=PCT,
          write=WriteSpec(0, 100), category="config"),

    # --- settings_high 0xE018 (offsets: address - 0xE018) -----------------
    Field(0xE018, 3, "battery_to_mains_voltage", "Battery-to-Mains Voltage",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 130), category="config",
          precision=1),  # E01B, item 04: 40-52 V
    Field(0xE018, 4, "charge_stop_current", "Charge Stop Current", scale=0.1,
          unit=A, device_class="current", write=WriteSpec(0, 100),
          category="config", precision=1),                             # E01C
    Field(0xE018, 5, "charge_stop_soc", "Charge Stop SOC", unit=PCT,
          write=WriteSpec(0, 100), category="config"),                 # E01D
    Field(0xE018, 6, "soc_low_alarm", "SOC Low Alarm", unit=PCT,
          write=WriteSpec(0, 100), category="config"),                 # E01E
    Field(0xE018, 7, "soc_to_line", "SOC To Line", unit=PCT,
          write=WriteSpec(0, 100), category="config"),                 # E01F
    Field(0xE018, 8, "soc_to_battery", "SOC To Battery", unit=PCT,
          write=WriteSpec(0, 100), category="config"),                 # E020
    Field(0xE018, 10, "mains_to_battery_voltage", "Mains-to-Battery Voltage",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(120, 150), category="config",
          precision=1),  # E022, item 05: 48-60 V
    Field(0xE018, 12, "li_activation_current", "Li Activation Current",
          scale=0.1, unit=A, device_class="current", category="diagnostic",
          precision=1),                                                # E024
    Field(0xE018, 13, "bms_charge_limit_mode", "BMS Charge Limit Mode",
          kind=FieldKind.ENUM, enum=BMS_CHARGE_LIMIT_MODE,
          write=WriteSpec(0, 2), category="config"),                   # E025

    # --- control_low 0xE200 ----------------------------------------------
    Field(0xE200, 0, "rs485_address", "RS485 Address", category="diagnostic"),
    Field(0xE200, 1, "parallel_mode", "Parallel Mode", category="diagnostic"),
    Field(0xE200, 4, "output_priority", "Output Priority", kind=FieldKind.ENUM,
          enum=OUTPUT_PRIORITY, write=WriteSpec(0, 3), category="config"),
    Field(0xE200, 5, "ac_charge_current_limit", "AC Charge Current Limit",
          scale=0.1, unit=A, device_class="current",
          write=WriteSpec(0, 1200), category="config", precision=1),
    Field(0xE200, 8, "output_voltage_setpoint", "Output Voltage Setpoint",
          scale=0.1, unit=V, device_class="voltage", category="diagnostic",
          precision=1),
    Field(0xE200, 9, "output_frequency_setpoint", "Output Frequency Setpoint",
          scale=0.01, unit=HZ, device_class="frequency",
          category="diagnostic", precision=2),
    Field(0xE200, 10, "max_charge_current", "Max Charge Current", scale=0.1,
          unit=A, device_class="current", write=WriteSpec(0, 2000),
          category="config", precision=1),
    # E20B and E20F are READ-ONLY: this firmware answers IllegalDataValue to
    # every write (verified at Casa Justice 2026-09-13).
    Field(0xE200, 11, "ac_input_range", "AC Input Range",
          category="diagnostic"),
    Field(0xE200, 15, "charge_priority", "Charge Priority",
          kind=FieldKind.ENUM, enum=CHARGE_PRIORITY, category="diagnostic"),

    # --- control_high 0xE210 (offsets: address - 0xE210) ------------------
    Field(0xE210, 4, "bms_error_stop", "BMS Error Stop",
          category="diagnostic"),                                      # E214
    Field(0xE210, 5, "bms_communication", "BMS Communication",
          kind=FieldKind.ENUM, enum=BMS_COMMUNICATION,
          write=WriteSpec(0, 2), category="config"),                   # E215
    Field(0xE210, 11, "bms_protocol", "BMS Protocol", kind=FieldKind.ENUM,
          enum=BMS_PROTOCOL, write=WriteSpec(0, 15), category="config"),  # E21B
    Field(0xE210, 14, "output_phase_mode", "Output Phase Mode",
          category="diagnostic"),                                      # E21E

    # --- device_info 0x0014 ----------------------------------------------
    Field(0x0014, 0, "firmware_version", "Firmware Version",
          kind=FieldKind.VERSION, category="diagnostic"),
    Field(0x0014, 3, "hardware_version", "Hardware Version",
          kind=FieldKind.VERSION, category="diagnostic"),
    # 0x0018-0x001B: meaning unverified, exposed as a diagnostic hex string.
    Field(0x0014, 4, "inverter_serial", "Inverter Serial",
          kind=FieldKind.SERIAL, words=4, category="diagnostic"),
    Field(0x0014, 8, "bms_version_1", "BMS Version 1", scale=0.01,
          category="diagnostic", precision=2),
    Field(0x0014, 9, "bms_version_2", "BMS Version 2", scale=0.01,
          category="diagnostic", precision=2),

    # --- meter 0xF02C -----------------------------------------------------
    Field(0xF02C, 0, "running_days", "Total Running Days", unit=D,
          state_class=TOTI),
    Field(0xF02C, 1, "battery_charge_ah_today", "Battery Charge Today",
          unit=AH, state_class=TOTI),
    Field(0xF02C, 2, "battery_discharge_ah_today", "Battery Discharge Today",
          unit=AH, state_class=TOTI),
    Field(0xF02C, 3, "pv_energy_today", "PV Energy Today", scale=0.1,
          unit=KWH, device_class="energy", state_class=TOTI, precision=1),
    Field(0xF02C, 4, "load_energy_today", "Load Energy Today", scale=0.1,
          unit=KWH, device_class="energy", state_class=TOTI, precision=1),
    Field(0xF02C, 8, "battery_charge_ah_total", "Battery Charge Total",
          words=2, unit=AH, state_class=TOTI),
    Field(0xF02C, 10, "battery_discharge_ah_total", "Battery Discharge Total",
          words=2, unit=AH, state_class=TOTI),
    Field(0xF02C, 14, "load_energy_total", "Load Energy Total", words=2,
          scale=0.1, unit=KWH, device_class="energy", state_class=TOTI,
          precision=1),
    Field(0xF02C, 16, "grid_charge_ah_today", "Grid Charge Today", unit=AH,
          state_class=TOTI),
)

DERIVED: tuple[Derived, ...] = (
    # Positive = charging (battery_current is already sign-normalised).
    # precision=1, not 0: battery_voltage/battery_current are already rounded
    # to 1 decimal each (Field.precision), so their product carries float noise
    # (e.g. -9590.400000000001) rather than a meaningful 2nd decimal. precision=0
    # would coarsen 53.1 V x 0.9 A = 47.79 W down to 48.0 W, which falls outside
    # the recorded-fixture test's pytest.approx(..., rel=1e-3) tolerance (+-0.048);
    # precision=1 -> 47.8, which stays inside it.
    Derived("battery_power", "Battery Power", "multiply",
            ("battery_voltage", "battery_current"), unit=W,
            device_class="power", state_class=MEAS, precision=1),
    Derived("load_power_total", "Load Power Total", "add",
            ("load_power_l1", "load_power_l2"), unit=W,
            device_class="power", state_class=MEAS, precision=0),
)

FAULT_FIELD_KEYS: tuple[str, ...] = (
    "fault_word_1", "fault_word_2", "fault_word_3", "fault_word_4",
)

_FIELDS_BY_KEY = {f.key: f for f in FIELDS}
_BLOCKS_BY_ADDR = {b.addr: b for b in BLOCKS}


def fields_for(block_addr: int) -> tuple[Field, ...]:
    """Return every field defined inside the given block."""
    return tuple(f for f in FIELDS if f.block_addr == block_addr)


def field_by_key(key: str) -> Field:
    """Return the field with this key, or raise KeyError."""
    return _FIELDS_BY_KEY[key]


def block_by_addr(addr: int) -> Block:
    """Return the block starting at this address, or raise KeyError."""
    return _BLOCKS_BY_ADDR[addr]


def _signed16(raw: int) -> int:
    return raw - 0x10000 if raw > 0x7FFF else raw


def _decode_field(field: Field, registers: Mapping[int, int]) -> object | None:
    words = [registers.get(field.address + i) for i in range(field.words)]
    if any(word is None for word in words):
        return None

    if field.kind is FieldKind.HEX:
        return f"0x{words[0]:04X}"
    if field.kind is FieldKind.VERSION:
        return f"V{words[0] // 100}.{words[0] % 100:02d}"
    if field.kind is FieldKind.SERIAL:
        return "".join(f"{word:04X}" for word in words)
    if field.kind is FieldKind.ENUM:
        assert field.enum is not None
        return field.enum.label(words[0])

    if field.words == 2:
        raw: float = words[0] | (words[1] << 16)
    elif field.signed:
        raw = _signed16(words[0])
    else:
        raw = words[0]

    value = raw * field.scale
    if field.precision is not None:
        value = round(value, field.precision)
    return value


def decode(registers: Mapping[int, int]) -> dict[str, object]:
    """Decode a raw {address: value} map into {field_key: state}.

    Fields whose registers are missing are omitted, so callers can decode a
    partial snapshot (one tier, or a unit missing a block) without guessing.
    """
    values: dict[str, object] = {}
    for field in FIELDS:
        decoded = _decode_field(field, registers)
        if decoded is not None:
            values[field.key] = decoded

    for derived in DERIVED:
        parts = [values.get(key) for key in derived.inputs]
        if any(not isinstance(part, (int, float)) for part in parts):
            continue
        numbers = [float(part) for part in parts]  # type: ignore[arg-type]
        if derived.op == "multiply":
            result = numbers[0]
            for number in numbers[1:]:
                result *= number
        elif derived.op == "add":
            result = sum(numbers)
        else:  # pragma: no cover - guarded by the DERIVED table
            raise ValueError(f"unknown derived op {derived.op}")
        if derived.precision is not None:
            result = round(result, derived.precision)
        values[derived.key] = result

    return values


def encode(field: Field, value: float | str) -> int:
    """Convert a state value back to the raw register word for a write.

    Raises ValueError if the field is read-only, the enum label is unknown, the
    value is not actually reachable at this field's scale (would be silently
    rounded to a different value), the raw value does not land on the
    WriteSpec's step_raw grid, or the resulting raw value falls outside the
    field's WriteSpec.
    """
    if field.write is None:
        raise ValueError(f"{field.key} is read-only on this firmware")

    if field.kind is FieldKind.ENUM:
        assert field.enum is not None
        raw = field.enum.raw_for(str(value))
        if raw is None:
            raise ValueError(f"{value!r} is not a valid option for {field.key}")
    else:
        raw_exact = float(value) / field.scale
        raw = round(raw_exact)
        # Reject values the register cannot actually represent (e.g. 57.75 V
        # at scale=0.4 would silently become 57.6 V) instead of rounding them
        # into a different, plausible-looking value.
        if abs(raw_exact - raw) > 1e-6:
            raise ValueError(
                f"{field.key}: {value!r} is not reachable at scale "
                f"{field.scale} (nearest raw {raw} = {raw * field.scale})"
            )
        step = field.write.step_raw
        if step > 1 and (raw - field.write.min_raw) % step != 0:
            raise ValueError(
                f"{field.key}: raw {raw} is not on the {step}-step grid "
                f"from {field.write.min_raw}"
            )

    if not field.write.min_raw <= raw <= field.write.max_raw:
        raise ValueError(
            f"{field.key}: raw {raw} outside "
            f"{field.write.min_raw}..{field.write.max_raw}"
        )
    return raw

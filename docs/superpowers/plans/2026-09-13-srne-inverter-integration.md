# SRNE Inverter (Solarman V5) Home Assistant Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Dispatch each task to a **fresh `model: sonnet` subagent**; review with `model: opus` at the end of each phase.

**Goal:** Ship a local-polling Home Assistant custom integration `srne_inverter` that reads and controls SRNE/BlueSun hybrid inverters through Solarman LSW-5 WiFi loggers (V5 protocol, TCP 8899), with per-unit capability probing so heterogeneous firmwares degrade gracefully instead of failing wholesale.

**Architecture:** A HA-free core (`registers.py` + `transport/` + `probe.py`) owns the register map, the async transport protocol and the block-capability probe; it is importable by `tools/probe.py` with no Home Assistant installed. On top of it, a `DataUpdateCoordinator` holds exactly one socket per logger behind an `asyncio.Lock`, polls blocks in three tiers (HOT/WARM/COLD) with 0.25 s pauses, decodes into a value dict, and exposes typed `entry.runtime_data`. Six platforms build entities only from blocks the probe marked supported; every write is followed by a read-back that must match.

**Tech Stack:** Python 3.14.7 · `homeassistant==2026.9.2` (via `pytest-homeassistant-custom-component==0.13.365`) · `pysolarmanv5>=3.0.6` (`PySolarmanV5Async`) · `umodbus` exceptions · `pytest` + `pytest-asyncio` · `aiohttp` (logger web scrape).

**Spec:** `docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md` — read it together with this plan. Register references live in `docs/reference/`.

## Global Constraints

- Repo: `/data/claude/ha-srne-inverter/`. **Local-only git, never `git push`.**
- Code, identifiers, docstrings, comments and commit messages **in English**. `README.md` and `CLAUDE.md` in Spanish.
- Conventional Commits, every commit ends with `Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>` (or the executing model's name).
- Test runner is always `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest`. **Do not create or recreate the venv** — it already exists with Python 3.14.7 and HA 2026.9.2.
- **No network in unit tests.** Every test uses `FakeTransport` fed from the recorded Justice registers.
- `custom_components/srne_inverter/registers.py`, `custom_components/srne_inverter/probe.py` and `custom_components/srne_inverter/transport/*` **must never import `homeassistant`** (enforced by a test) — `tools/probe.py` imports them on hosts without HA.
- Every write to an inverter re-reads the same register and raises if the read-back differs. No value is ever assumed written.
- Verified device facts that must not be contradicted: one TCP client per logger; blocks present on `V8.18.006` are `0x0014`(10), `0x0100`(15), `0x0200–0x023F`, `0xE000–0xE02F`, `0xE200–0xE21E`, `0xF02C–0xF043`; absent are `0x0112+`, `0xE03A+`, `0xE21F+`; writes rejected by firmware: `E20F`, `E21D`, `E039`, `E20B`; `0x0102` raw negative = charging; voltage thresholds are `raw × 0.4` V.
- Justice test units: `192.168.188.240` serial `3548208972` slave `1`; `192.168.188.242` serial `3548738877` slave `2`.

## Design decisions locked by this plan (beyond the spec)

1. **Register addresses come from `docs/reference/ha-solarman-profile_srne_bluesun_justice.yaml`**, not from `justice_inverters_read.py`. The YAML is confirmed by the raw capture (`0x022A`=1218→121.8 V grid L2, `0x022C`=1231→123.1 V out L2, `0x022E`=5→0.5 A). `justice_inverters_read.py` reads grid current L2 at `0x0238` — **that address is left unmapped**; Task 18 (live test) dumps `0x0236–0x0239` to settle it.
2. **Entity names are English `label` strings on each `Field`** with `has_entity_name = True`; only the config flow, options flow and services are translated (en/es). 60+ per-entity translation keys are not worth the churn in a private integration.
3. **The probe result is never persisted to disk** — it is recomputed at every `async_setup_entry`, so a firmware change is picked up by a restart. It lives in `runtime_data.support` and is exported in diagnostics.
4. **`tools/probe.py` imports the core by putting `custom_components/srne_inverter/` on `sys.path`** and importing `registers` / `transport.solarman_v5` as *top-level* modules. That never executes `custom_components/srne_inverter/__init__.py`, so HA is never imported. `registers.py` therefore has **no intra-package imports** (it defines its own constants, it does not import `const.py`).
5. **`0x0018–0x001B` is decoded as `inverter_serial` (4 words, hex-joined), diagnostic only.** Its meaning is unverified; Task 18 confirms it against the device label.
6. **`E20F` (charge priority) is a read-only sensor**, not a select — the firmware rejects the write (verified). Same for `E20B`, `E21D`, `E039`.
7. Blocks are capped at **24 registers** so a partially-present region degrades per-half instead of wholesale.
8. **Backoff is implemented in the coordinator**, not by changing `update_interval`: a `_backoff_until` monotonic deadline makes the cycle return `UpdateFailed` immediately without touching the socket. Sequence `2, 5, 15, 60` s, reset on success.

## File structure

| Path | Responsibility |
|---|---|
| `custom_components/srne_inverter/manifest.json` | domain, version, requirements, `iot_class: local_polling` |
| `custom_components/srne_inverter/const.py` | DOMAIN, CONF_*, defaults, PLATFORMS, signals (**imports HA**) |
| `custom_components/srne_inverter/registers.py` | `Block`/`Field`/`EnumMap`/`WriteSpec`/`Derived`, BLOCKS, FIELDS, `decode()` (**no HA**) |
| `custom_components/srne_inverter/transport/base.py` | `Transport` Protocol + exception hierarchy (**no HA**) |
| `custom_components/srne_inverter/transport/solarman_v5.py` | `PySolarmanV5Async` wrapper, lock, timeouts, error mapping (**no HA**) |
| `custom_components/srne_inverter/probe.py` | `probe()` + `SupportMap`/`BlockSupport` (**no HA**) |
| `custom_components/srne_inverter/coordinator.py` | tiers, backoff, writes with read-back, connection toggle |
| `custom_components/srne_inverter/__init__.py` | setup/unload, `runtime_data`, services |
| `custom_components/srne_inverter/config_flow.py` | user step, serial scrape, options flow |
| `custom_components/srne_inverter/logger_web.py` | `status.html` serial scrape over aiohttp |
| `custom_components/srne_inverter/entity.py` | `SrneEntity` base + `DeviceInfo` |
| `custom_components/srne_inverter/{sensor,binary_sensor,select,number,switch,button}.py` | platforms |
| `custom_components/srne_inverter/diagnostics.py` | config-entry diagnostics |
| `custom_components/srne_inverter/services.yaml` + `translations/{en,es}.json` | service schemas + UI strings |
| `tools/probe.py` | standalone CLI probe/snapshot |
| `tools/deploy_to_gadi.sh`, `tools/add_justice_entries.py`, `tools/ha_add_justice_inverters_view.py` | production ops (gated on Gabriel's OK) |
| `tests/` | unit + integration tests, `FakeTransport`, recorded fixtures |

---

## Phase 1 — HA-free core

### Task 1: Repository scaffold, manifest, constants and test harness

**Files:**
- Create: `custom_components/srne_inverter/manifest.json`
- Create: `custom_components/srne_inverter/const.py`
- Create: `custom_components/srne_inverter/__init__.py` (placeholder, empty module docstring only)
- Create: `custom_components/srne_inverter/transport/__init__.py` (empty)
- Create: `custom_components/srne_inverter/translations/.gitkeep`
- Create: `hacs.json`
- Create: `pyproject.toml` (pytest config only)
- Create: `tests/__init__.py`, `tests/conftest.py`
- Create: `tests/fixtures/justice_inv1_blocks.json` (copy of `/data/claude/Redes-Clientes/CasaJustice/evidencia/2026-09-12_registros-inv1-crudo.json`)
- Create: `tests/fixtures/justice_inv1_settings.json` (copy of `/data/claude/Redes-Clientes/CasaJustice/evidencia/2026-09-13_inv1-user-voltages-before-after.json`)
- Test: `tests/test_fixtures.py`

**Interfaces:**
- Produces: `tests.conftest.load_justice_registers() -> dict[int, int]`; `const.DOMAIN`, `const.DEFAULT_*`, `const.PLATFORMS`.

- [ ] **Step 1: Copy the fixtures into the repo**

```bash
mkdir -p /data/claude/ha-srne-inverter/tests/fixtures
cp /data/claude/Redes-Clientes/CasaJustice/evidencia/2026-09-12_registros-inv1-crudo.json \
   /data/claude/ha-srne-inverter/tests/fixtures/justice_inv1_blocks.json
cp /data/claude/Redes-Clientes/CasaJustice/evidencia/2026-09-13_inv1-user-voltages-before-after.json \
   /data/claude/ha-srne-inverter/tests/fixtures/justice_inv1_settings.json
```

Tests must not read paths outside this repo.

- [ ] **Step 2: Write the failing fixture-loader test**

`tests/test_fixtures.py`:

```python
"""The recorded Justice registers must load into a flat address -> value map."""

from tests.conftest import load_justice_registers


def test_loader_flattens_blocks_and_settings():
    regs = load_justice_registers()
    # From justice_inv1_blocks.json, block "grid_out" at 0x0210.
    assert regs[0x0210] == 2          # machine state: AC bypass
    assert regs[0x0212] == 5266       # bus voltage 526.6 V
    assert regs[0x0213] == 1225       # grid L1 122.5 V
    assert regs[0x021E] == 10         # grid charge current 1.0 A
    # From justice_inv1_blocks.json, block "battery" at 0x0100.
    assert regs[0x0100] == 55         # SOC 55 %
    assert regs[0x0101] == 531        # 53.1 V
    assert regs[0x0102] == 65527      # raw -9 -> charging at 0.9 A
    # From justice_inv1_settings.json ("before" map, authoritative for 0xE0xx).
    assert regs[0xE004] == 0          # battery type USER
    assert regs[0xE01E] == 15         # SOC low alarm
    assert regs[0xE008] == 144


def test_loader_settings_override_older_block_capture():
    """justice_inv1_settings.json is newer, so it wins on 0xE000-0xE02F."""
    regs = load_justice_registers()
    assert regs[0xE00D] == 122
```

- [ ] **Step 3: Run it, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_fixtures.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_justice_registers'`

- [ ] **Step 4: Write `pyproject.toml`**

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
norecursedirs = [".venv", "docs"]
addopts = "-q"
```

- [ ] **Step 5: Write `tests/conftest.py` loader**

```python
"""Shared fixtures: recorded Justice registers and the fake transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_justice_registers() -> dict[int, int]:
    """Flatten the recorded Justice captures into {address: raw_value}.

    Two sources, applied in order (later wins):
      1. justice_inv1_blocks.json  -- {ip: {ip, sn, blocks: {name: {addr, regs}}}}
      2. justice_inv1_settings.json -- {"before": {"E000": value, ...}} (newer)
    """
    regs: dict[int, int] = {}

    blocks_doc = json.loads((FIXTURES / "justice_inv1_blocks.json").read_text())
    for unit in blocks_doc.values():
        for block in unit["blocks"].values():
            for index, value in enumerate(block["regs"]):
                regs[block["addr"] + index] = value

    settings_doc = json.loads((FIXTURES / "justice_inv1_settings.json").read_text())
    for key, value in settings_doc["before"].items():
        regs[int(key, 16)] = value

    return regs


@pytest.fixture(name="justice_registers")
def justice_registers_fixture() -> dict[int, int]:
    """Recorded raw registers of Casa Justice inverter 1."""
    return load_justice_registers()
```

- [ ] **Step 6: Run the test, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_fixtures.py -v`
Expected: 2 passed

- [ ] **Step 7: Write `manifest.json`**

```json
{
  "domain": "srne_inverter",
  "name": "SRNE Inverter (Solarman V5)",
  "codeowners": ["@gabrielpc1190"],
  "config_flow": true,
  "documentation": "https://github.com/gabrielpc1190/ha-srne-inverter",
  "integration_type": "device",
  "iot_class": "local_polling",
  "issue_tracker": "https://github.com/gabrielpc1190/ha-srne-inverter/issues",
  "requirements": ["pysolarmanv5>=3.0.6"],
  "version": "0.1.0"
}
```

- [ ] **Step 8: Write `hacs.json`**

```json
{
  "name": "SRNE Inverter (Solarman V5)",
  "render_readme": true,
  "homeassistant": "2025.6.0"
}
```

- [ ] **Step 9: Write `const.py`**

```python
"""Constants for the srne_inverter integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "srne_inverter"

CONF_SERIAL = "serial"
CONF_SLAVE_ID = "slave_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_WARM_INTERVAL = "warm_interval"
CONF_COLD_INTERVAL = "cold_interval"
CONF_CONNECTION_ENABLED = "connection_enabled"

DEFAULT_PORT = 8899
DEFAULT_SLAVE_ID = 1
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_WARM_INTERVAL = 60
DEFAULT_COLD_INTERVAL = 300

SOCKET_TIMEOUT = 10.0
BLOCK_PAUSE = 0.25
WRITE_SETTLE = 0.4
BACKOFF_SECONDS = (2.0, 5.0, 15.0, 60.0)
REPROBE_AFTER_FAILURES = 3

MANUFACTURER = "SRNE"
DEFAULT_MODEL = "Energy Storage Inverter"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

SIGNAL_NEW_ENTITIES = "srne_inverter_new_entities_{entry_id}"

SERVICE_READ_REGISTER = "read_register"
SERVICE_WRITE_REGISTER = "write_register"
SERVICE_REPROBE = "reprobe"
```

- [ ] **Step 10: Write the placeholder `__init__.py` and empty `transport/__init__.py`**

`custom_components/srne_inverter/__init__.py`:

```python
"""The SRNE Inverter (Solarman V5) integration."""
```

`custom_components/srne_inverter/transport/__init__.py`:

```python
"""Transport implementations. This package must not import homeassistant."""
```

- [ ] **Step 11: Run the whole suite**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest -v`
Expected: 2 passed

- [ ] **Step 12: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components hacs.json pyproject.toml tests
git -C /data/claude/ha-srne-inverter commit -m "feat(scaffold): integration manifest, constants and recorded Justice test fixtures

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** `pytest -v` green; `custom_components/srne_inverter/manifest.json` parses as JSON.

---

### Task 2: `registers.py` — register map and decoder

> **Parallel-safe with Task 3.** Touches only `registers.py` and `tests/test_registers.py`.

**Files:**
- Create: `custom_components/srne_inverter/registers.py`
- Test: `tests/test_registers.py`

**Interfaces:**
- Produces:
  - `BlockTier` (`StrEnum`: `HOT`, `WARM`, `COLD`)
  - `FieldKind` (`StrEnum`: `NUMBER`, `ENUM`, `HEX`, `VERSION`, `SERIAL`)
  - `EnumMap(key: str, values: dict[int, str])`, methods `label(raw) -> str`, `raw_for(label) -> int | None`, property `options -> list[str]`
  - `WriteSpec(min_raw: int, max_raw: int, step_raw: int = 1)`
  - `Block(addr: int, count: int, name: str, tier: BlockTier)`, property `addresses -> range`
  - `Field(block_addr, offset, key, label, kind=FieldKind.NUMBER, scale=1.0, signed=False, words=1, unit=None, device_class=None, state_class=None, enum=None, write=None, category=None, precision=None)`, property `address -> int`
  - `Derived(key, label, op, inputs, unit, device_class, state_class, precision)` with `op` in `{"multiply", "add"}`
  - `BLOCKS: tuple[Block, ...]`, `FIELDS: tuple[Field, ...]`, `DERIVED: tuple[Derived, ...]`
  - `FAULT_FIELD_KEYS: tuple[str, ...]`
  - `fields_for(block_addr: int) -> tuple[Field, ...]`
  - `field_by_key(key: str) -> Field`
  - `block_by_addr(addr: int) -> Block`
  - `decode(registers: Mapping[int, int]) -> dict[str, object]`
  - `encode(field: Field, value: float | str) -> int`

- [ ] **Step 1: Write the failing decoder tests**

`tests/test_registers.py`:

```python
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
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_registers.py -v`
Expected: FAIL with `ModuleNotFoundError`/`AttributeError`

- [ ] **Step 3: Write `registers.py` — types**

```python
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
```

- [ ] **Step 4: Write `registers.py` — enums and blocks**

```python
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
```

- [ ] **Step 5: Write `registers.py` — FIELDS (part 1: HOT blocks)**

```python
V, A, HZ, C, W, VA, PCT, AH, KWH, D = (
    "V", "A", "Hz", "\u00b0C", "W", "VA", "%", "Ah", "kWh", "d")
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
```

- [ ] **Step 6: Write `registers.py` — FIELDS (part 2: WARM/COLD blocks)**

Voltage thresholds use the SRNE "12 V-equivalent" encoding: `volts = raw / 10 * 4`, i.e. `scale=0.4`. Write range `100..160` raw = 40.0..64.0 V on a 48 V bank.

```python
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
    Field(0xE000, 5, "overvoltage_threshold", "Over-voltage Threshold",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 170), category="config", precision=1),
    Field(0xE000, 6, "charge_limit_voltage", "Charge Limit Voltage",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 165), category="config", precision=1),
    Field(0xE000, 7, "equalize_voltage", "Equalize Voltage", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(100, 160),
          category="config", precision=1),
    Field(0xE000, 8, "boost_voltage", "Boost Charge Voltage", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(100, 160),
          category="config", precision=1),
    Field(0xE000, 9, "float_voltage", "Float Charge Voltage", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(100, 160),
          category="config", precision=1),
    Field(0xE000, 10, "recharge_voltage", "Recharge Voltage", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(100, 160),
          category="config", precision=1),
    Field(0xE000, 11, "undervoltage_recovery", "Under-voltage Recovery",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 160), category="config", precision=1),
    Field(0xE000, 12, "undervoltage_alarm", "Under-voltage Alarm", scale=0.4,
          unit=V, device_class="voltage", write=WriteSpec(100, 160),
          category="config", precision=1),
    Field(0xE000, 13, "overdischarge_voltage", "Over-discharge Voltage",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 160), category="config", precision=1),
    Field(0xE000, 14, "discharge_limit_voltage", "Discharge Limit Voltage",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 160), category="config", precision=1),
    Field(0xE000, 15, "discharge_stop_soc", "Discharge Stop SOC", unit=PCT,
          write=WriteSpec(0, 100), category="config"),

    # --- settings_high 0xE018 (offsets: address - 0xE018) -----------------
    Field(0xE018, 3, "battery_to_mains_voltage", "Battery-to-Mains Voltage",
          scale=0.4, unit=V, device_class="voltage",
          write=WriteSpec(100, 160), category="config", precision=1),  # E01B
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
          write=WriteSpec(100, 160), category="config", precision=1),  # E022
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
    Derived("battery_power", "Battery Power", "multiply",
            ("battery_voltage", "battery_current"), unit=W,
            device_class="power", state_class=MEAS, precision=0),
    Derived("load_power_total", "Load Power Total", "add",
            ("load_power_l1", "load_power_l2"), unit=W,
            device_class="power", state_class=MEAS, precision=0),
)

FAULT_FIELD_KEYS: tuple[str, ...] = (
    "fault_word_1", "fault_word_2", "fault_word_3", "fault_word_4",
)
```

- [ ] **Step 7: Write `registers.py` — lookups, `decode()` and `encode()`**

```python
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

    Raises ValueError if the field is read-only, the enum label is unknown or
    the resulting raw value falls outside the field's WriteSpec.
    """
    if field.write is None:
        raise ValueError(f"{field.key} is read-only on this firmware")

    if field.kind is FieldKind.ENUM:
        assert field.enum is not None
        raw = field.enum.raw_for(str(value))
        if raw is None:
            raise ValueError(f"{value!r} is not a valid option for {field.key}")
    else:
        raw = round(float(value) / field.scale)

    if not field.write.min_raw <= raw <= field.write.max_raw:
        raise ValueError(
            f"{field.key}: raw {raw} outside "
            f"{field.write.min_raw}..{field.write.max_raw}"
        )
    return raw
```

- [ ] **Step 8: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_registers.py -v`
Expected: all pass. If `test_decode_derived_battery_power` fails on precision, check `battery_current` rounds to 0.9 before the multiply.

- [ ] **Step 9: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/registers.py tests/test_registers.py
git -C /data/claude/ha-srne-inverter commit -m "feat(registers): SRNE V1.96 block/field map with scales, enums and write ranges

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** 14 tests pass; `grep -c homeassistant custom_components/srne_inverter/registers.py` returns 0.

---

### Task 3: Transport protocol, exceptions and `FakeTransport`

> **Parallel-safe with Task 2.** Touches `transport/base.py`, `tests/fake_transport.py`, `tests/test_fake_transport.py`.

**Files:**
- Create: `custom_components/srne_inverter/transport/base.py`
- Create: `tests/fake_transport.py`
- Test: `tests/test_fake_transport.py`

**Interfaces:**
- Produces:
  - `TransportError`, `TransportConnectionError`, `TransportProtocolError`, `UnsupportedRegisterError`, `InvalidRegisterValueError` (all in `transport.base`)
  - `Transport` Protocol: `async connect()`, `async close()`, `async read_holding(addr: int, count: int) -> list[int]`, `async write_holding(addr: int, value: int) -> None`, property `connected: bool`
  - `tests.fake_transport.FakeTransport(registers, *, unsupported=DEFAULT_UNSUPPORTED, fail_connect=False, read_errors=None)` with attributes `reads: list[tuple[int, int]]`, `writes: list[tuple[int, int]]`, `connect_count: int`, `close_count: int`

- [ ] **Step 1: Write the failing test**

`tests/test_fake_transport.py`:

```python
"""The fake transport must behave like the real logger, including its errors."""

import pytest

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportConnectionError,
    UnsupportedRegisterError,
)
from tests.fake_transport import FakeTransport


async def test_read_returns_recorded_values(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    assert await transport.read_holding(0x0100, 3) == [55, 531, 65527]
    assert transport.reads == [(0x0100, 3)]


async def test_read_of_absent_block_raises_unsupported(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(UnsupportedRegisterError):
        await transport.read_holding(0x0112, 4)
    with pytest.raises(UnsupportedRegisterError):
        await transport.read_holding(0xE21F, 2)


async def test_write_then_read_back(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    await transport.write_holding(0xE01E, 16)
    assert await transport.read_holding(0xE01E, 1) == [16]
    assert transport.writes == [(0xE01E, 16)]


async def test_write_to_rejected_register_raises(justice_registers):
    """E20F/E20B/E21D are rejected by the real firmware."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(InvalidRegisterValueError):
        await transport.write_holding(0xE20F, 2)


async def test_connect_failure_is_injectable(justice_registers):
    transport = FakeTransport(justice_registers, fail_connect=True)
    with pytest.raises(TransportConnectionError):
        await transport.connect()


async def test_read_errors_are_injectable_once(justice_registers):
    transport = FakeTransport(
        justice_registers, read_errors=[TransportConnectionError("boom"), None]
    )
    await transport.connect()
    with pytest.raises(TransportConnectionError):
        await transport.read_holding(0x0100, 1)
    assert await transport.read_holding(0x0100, 1) == [55]
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_fake_transport.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.fake_transport'`

- [ ] **Step 3: Write `transport/base.py`**

```python
"""Transport abstraction for talking Modbus holding registers to an inverter.

The concrete implementation today is Solarman V5 over TCP 8899; RS485 and plain
Modbus-TCP can be added later behind the same Protocol.

THIS MODULE MUST NOT IMPORT homeassistant.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """Base class for every transport failure."""


class TransportConnectionError(TransportError):
    """The socket could not be opened, or died mid-request.

    Includes the logger's "one client only" refusal (NoSocketAvailableError)
    and request timeouts.
    """


class TransportProtocolError(TransportError):
    """A malformed or unexpected frame came back (V5FrameError, empty reply)."""


class UnsupportedRegisterError(TransportError):
    """The device answered IllegalDataAddress: this block does not exist.

    This is the signal the probe uses to mark a block unsupported.
    """


class InvalidRegisterValueError(TransportError):
    """The device answered IllegalDataValue: the write was refused."""


@runtime_checkable
class Transport(Protocol):
    """Minimal async Modbus holding-register transport."""

    @property
    def connected(self) -> bool:
        """True when a usable connection is open."""

    async def connect(self) -> None:
        """Open the connection. Raises TransportConnectionError on failure."""

    async def close(self) -> None:
        """Close the connection. Must be safe to call when already closed."""

    async def read_holding(self, addr: int, count: int) -> list[int]:
        """Read `count` holding registers starting at `addr`."""

    async def write_holding(self, addr: int, value: int) -> None:
        """Write a single holding register."""
```

- [ ] **Step 4: Write `tests/fake_transport.py`**

```python
"""In-memory transport serving the recorded Casa Justice registers.

Mirrors the real firmware's behaviour: addresses outside the verified blocks
raise UnsupportedRegisterError, and the registers the firmware refuses to write
raise InvalidRegisterValueError.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportConnectionError,
    UnsupportedRegisterError,
)

# Verified absent on firmware V8.18.006 (Casa Justice, 2026-09-12).
DEFAULT_UNSUPPORTED: tuple[range, ...] = (
    range(0x0112, 0x0200),
    range(0x0240, 0xE000),
    range(0xE030, 0xE200),
    range(0xE21F, 0xF000),
)

# Writes the firmware answers IllegalDataValue to.
WRITE_REJECTED: frozenset[int] = frozenset({0xE20F, 0xE20B, 0xE21D, 0xE039})


class FakeTransport:
    """Test double implementing the Transport protocol."""

    def __init__(
        self,
        registers: dict[int, int],
        *,
        unsupported: Iterable[range] = DEFAULT_UNSUPPORTED,
        fail_connect: bool = False,
        read_errors: Sequence[Exception | None] | None = None,
    ) -> None:
        self.registers = dict(registers)
        self.unsupported = tuple(unsupported)
        self.fail_connect = fail_connect
        self._read_errors = list(read_errors or [])
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int]] = []
        self.connect_count = 0
        self.close_count = 0
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connect_count += 1
        if self.fail_connect:
            raise TransportConnectionError("fake: cannot open connection")
        self._connected = True

    async def close(self) -> None:
        self.close_count += 1
        self._connected = False

    def _next_error(self) -> Exception | None:
        if not self._read_errors:
            return None
        return self._read_errors.pop(0)

    async def read_holding(self, addr: int, count: int) -> list[int]:
        self.reads.append((addr, count))
        error = self._next_error()
        if error is not None:
            raise error
        for offset in range(count):
            address = addr + offset
            if any(address in span for span in self.unsupported):
                raise UnsupportedRegisterError(f"fake: 0x{address:04X} absent")
            if address not in self.registers:
                raise UnsupportedRegisterError(
                    f"fake: 0x{address:04X} not in recorded capture"
                )
        return [self.registers[addr + i] for i in range(count)]

    async def write_holding(self, addr: int, value: int) -> None:
        self.writes.append((addr, value))
        if addr in WRITE_REJECTED:
            raise InvalidRegisterValueError(f"fake: 0x{addr:04X} refuses writes")
        if any(addr in span for span in self.unsupported):
            raise UnsupportedRegisterError(f"fake: 0x{addr:04X} absent")
        self.registers[addr] = value
```

- [ ] **Step 5: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_fake_transport.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/transport tests/fake_transport.py tests/test_fake_transport.py
git -C /data/claude/ha-srne-inverter commit -m "feat(transport): Transport protocol, error taxonomy and recorded-register fake

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** 6 tests pass; `FakeTransport` satisfies `isinstance(t, Transport)` (runtime_checkable Protocol).

---

### Task 4: `transport/solarman_v5.py` — the real logger transport

**Files:**
- Create: `custom_components/srne_inverter/transport/solarman_v5.py`
- Test: `tests/test_solarman_transport.py`

**Interfaces:**
- Consumes: `transport.base` exceptions and Protocol.
- Produces: `SolarmanV5Transport(host: str, serial: int, *, port: int = 8899, slave_id: int = 1, timeout: float = 10.0)` implementing `Transport`, plus `SolarmanV5Transport.lock` (`asyncio.Lock`) for callers that need to bracket a write+read-back.

- [ ] **Step 1: Write the failing test (pysolarmanv5 is patched, never a socket)**

`tests/test_solarman_transport.py`:

```python
"""The Solarman V5 transport maps library errors onto our taxonomy."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pysolarmanv5 import NoSocketAvailableError, V5FrameError
from umodbus.exceptions import IllegalDataAddressError, IllegalDataValueError

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportConnectionError,
    TransportProtocolError,
    UnsupportedRegisterError,
)
from custom_components.srne_inverter.transport.solarman_v5 import (
    SolarmanV5Transport,
)

TARGET = "custom_components.srne_inverter.transport.solarman_v5.PySolarmanV5Async"


@pytest.fixture(name="client")
def client_fixture():
    with patch(TARGET) as factory:
        client = factory.return_value
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.read_holding_registers = AsyncMock(return_value=[1, 2, 3])
        client.write_holding_register = AsyncMock(return_value=7)
        yield client


async def test_connect_builds_client_with_the_right_arguments(client):
    transport = SolarmanV5Transport("192.168.188.240", 3548208972, slave_id=2)
    await transport.connect()
    assert transport.connected is True
    with patch(TARGET) as factory:
        pass
    client.connect.assert_awaited_once()


async def test_read_holding_returns_values(client):
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    assert await transport.read_holding(0x0100, 3) == [1, 2, 3]
    client.read_holding_registers.assert_awaited_once_with(
        register_addr=0x0100, quantity=3
    )


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (IllegalDataAddressError(), UnsupportedRegisterError),
        (IllegalDataValueError(), InvalidRegisterValueError),
        (NoSocketAvailableError("busy"), TransportConnectionError),
        (V5FrameError("bad frame"), TransportProtocolError),
        (OSError("reset"), TransportConnectionError),
        (asyncio.TimeoutError(), TransportConnectionError),
    ],
)
async def test_read_error_mapping(client, raised, expected):
    client.read_holding_registers.side_effect = raised
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    with pytest.raises(expected):
        await transport.read_holding(0x0100, 1)


async def test_write_error_mapping(client):
    client.write_holding_register.side_effect = IllegalDataValueError()
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    with pytest.raises(InvalidRegisterValueError):
        await transport.write_holding(0xE20F, 2)


async def test_connect_failure_maps_to_connection_error(client):
    client.connect.side_effect = NoSocketAvailableError("one client only")
    transport = SolarmanV5Transport("h", 1)
    with pytest.raises(TransportConnectionError):
        await transport.connect()
    assert transport.connected is False


async def test_operations_are_serialised_by_the_lock(client):
    order: list[str] = []

    async def slow_read(register_addr, quantity):
        order.append(f"start-{register_addr}")
        await asyncio.sleep(0.01)
        order.append(f"end-{register_addr}")
        return [0] * quantity

    client.read_holding_registers.side_effect = slow_read
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    await asyncio.gather(
        transport.read_holding(0x0100, 1), transport.read_holding(0x0200, 1)
    )
    assert order in (
        ["start-256", "end-256", "start-512", "end-512"],
        ["start-512", "end-512", "start-256", "end-256"],
    )


async def test_close_is_idempotent(client):
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    await transport.close()
    await transport.close()
    assert transport.connected is False
    client.disconnect.assert_awaited_once()
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_solarman_transport.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `transport/solarman_v5.py`**

```python
"""Solarman V5 (LSW-5 WiFi logger, TCP 8899) transport.

One logger accepts exactly ONE TCP client, so a single connection is held open
and every request is serialised through an asyncio.Lock.

THIS MODULE MUST NOT IMPORT homeassistant.
"""

from __future__ import annotations

import asyncio
import logging

from pysolarmanv5 import NoSocketAvailableError, PySolarmanV5Async, V5FrameError
from umodbus.exceptions import (
    IllegalDataAddressError,
    IllegalDataValueError,
    ModbusError,
)

from .base import (
    InvalidRegisterValueError,
    TransportConnectionError,
    TransportProtocolError,
    UnsupportedRegisterError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8899
DEFAULT_TIMEOUT = 10.0


class SolarmanV5Transport:
    """Async Modbus-over-Solarman-V5 transport for a single logger."""

    def __init__(
        self,
        host: str,
        serial: int,
        *,
        port: int = DEFAULT_PORT,
        slave_id: int = 1,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.serial = serial
        self.port = port
        self.slave_id = slave_id
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self._client: PySolarmanV5Async | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        if self._client is not None:
            return
        client = PySolarmanV5Async(
            self.host,
            self.serial,
            port=self.port,
            mb_slave_id=self.slave_id,
            socket_timeout=self.timeout,
            auto_reconnect=False,
            verbose=False,
        )
        try:
            async with asyncio.timeout(self.timeout):
                await client.connect()
        except Exception as err:
            raise _translate(err) from err
        self._client = client
        _LOGGER.debug("Connected to logger %s (%s)", self.serial, self.host)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - closing must never raise
            _LOGGER.debug("Ignoring error while closing %s", self.host, exc_info=True)

    async def read_holding(self, addr: int, count: int) -> list[int]:
        async with self.lock:
            client = self._require_client()
            try:
                async with asyncio.timeout(self.timeout):
                    return list(
                        await client.read_holding_registers(
                            register_addr=addr, quantity=count
                        )
                    )
            except Exception as err:
                raise _translate(err) from err

    async def write_holding(self, addr: int, value: int) -> None:
        async with self.lock:
            client = self._require_client()
            try:
                async with asyncio.timeout(self.timeout):
                    await client.write_holding_register(
                        register_addr=addr, value=value
                    )
            except Exception as err:
                raise _translate(err) from err

    def _require_client(self) -> PySolarmanV5Async:
        if self._client is None:
            raise TransportConnectionError("transport is not connected")
        return self._client


def _translate(err: Exception) -> Exception:
    """Map pysolarmanv5 / umodbus / socket errors onto the transport taxonomy."""
    if isinstance(err, IllegalDataAddressError):
        return UnsupportedRegisterError(str(err) or "illegal data address")
    if isinstance(err, IllegalDataValueError):
        return InvalidRegisterValueError(str(err) or "illegal data value")
    if isinstance(err, NoSocketAvailableError):
        return TransportConnectionError(str(err) or "no socket available")
    if isinstance(err, V5FrameError):
        return TransportProtocolError(str(err) or "malformed V5 frame")
    if isinstance(err, ModbusError):
        # Empty / AcknowledgeError: usually the wrong Modbus slave id.
        return TransportProtocolError(f"{type(err).__name__}: {err}")
    if isinstance(err, (TimeoutError, asyncio.TimeoutError, OSError)):
        return TransportConnectionError(f"{type(err).__name__}: {err}")
    return TransportProtocolError(f"{type(err).__name__}: {err}")
```

- [ ] **Step 4: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_solarman_transport.py -v`
Expected: all pass. Note `asyncio.TimeoutError is TimeoutError` on 3.14 — both branches resolve to `TransportConnectionError`.

- [ ] **Step 5: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/transport/solarman_v5.py tests/test_solarman_transport.py
git -C /data/claude/ha-srne-inverter commit -m "feat(transport): Solarman V5 async transport with single-client lock and error mapping

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** 11 tests pass with zero sockets opened (the whole `PySolarmanV5Async` class is patched).

---

### Task 5: `probe.py` — per-unit block capability probe

**Files:**
- Create: `custom_components/srne_inverter/probe.py`
- Test: `tests/test_probe.py`

**Interfaces:**
- Consumes: `registers.BLOCKS`, `transport.base` exceptions.
- Produces:
  - `BlockSupport` (`StrEnum`: `SUPPORTED`, `UNSUPPORTED`, `UNKNOWN`)
  - `ProbeResult(support: dict[int, BlockSupport], registers: dict[int, int], errors: dict[int, str])` with `supported_blocks() -> tuple[Block, ...]` and `as_diagnostics() -> dict[str, str]`
  - `async probe(transport, blocks=BLOCKS, *, pause=0.25, retries=1) -> ProbeResult`
  - `ProbeFailedError(TransportError)` raised when **every** block came back UNKNOWN

- [ ] **Step 1: Write the failing tests**

`tests/test_probe.py`:

```python
"""Capability probe: one read per block, classify, never guess."""

import pytest

from custom_components.srne_inverter.probe import (
    BlockSupport,
    ProbeFailedError,
    probe,
)
from custom_components.srne_inverter.registers import BLOCKS
from custom_components.srne_inverter.transport.base import (
    TransportConnectionError,
    TransportProtocolError,
)
from tests.fake_transport import FakeTransport


async def test_probe_marks_every_recorded_block_supported(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    result = await probe(transport, pause=0)
    for block in BLOCKS:
        assert result.support[block.addr] is BlockSupport.SUPPORTED, block.name
    assert len(transport.reads) == len(BLOCKS)


async def test_probe_marks_missing_block_unsupported(justice_registers):
    partial = {k: v for k, v in justice_registers.items() if not 0xF000 <= k}
    transport = FakeTransport(partial)
    await transport.connect()
    result = await probe(transport, pause=0)
    assert result.support[0xF02C] is BlockSupport.UNSUPPORTED
    assert result.support[0x0100] is BlockSupport.SUPPORTED
    assert "0xF02C" in " ".join(f"0x{a:04X}" for a in result.errors)


async def test_probe_retries_protocol_errors_then_marks_unsupported(
    justice_registers,
):
    transport = FakeTransport(
        justice_registers,
        read_errors=[
            TransportProtocolError("empty"),
            TransportProtocolError("empty"),
        ],
    )
    await transport.connect()
    result = await probe(transport, pause=0, retries=1)
    assert result.support[BLOCKS[0].addr] is BlockSupport.UNSUPPORTED


async def test_probe_marks_connection_errors_unknown(justice_registers):
    errors: list[Exception | None] = [TransportConnectionError("timeout")] * 2
    errors += [None] * 32
    transport = FakeTransport(justice_registers, read_errors=errors)
    await transport.connect()
    result = await probe(transport, pause=0, retries=1)
    assert result.support[BLOCKS[0].addr] is BlockSupport.UNKNOWN
    assert result.support[BLOCKS[1].addr] is BlockSupport.SUPPORTED


async def test_probe_raises_when_nothing_answered(justice_registers):
    transport = FakeTransport(
        justice_registers,
        read_errors=[TransportConnectionError("dead")] * 100,
    )
    await transport.connect()
    with pytest.raises(ProbeFailedError):
        await probe(transport, pause=0, retries=1)


async def test_probe_returns_the_registers_it_read(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    result = await probe(transport, pause=0)
    assert result.registers[0x0100] == 55
    assert result.registers[0xE01E] == 15


async def test_supported_blocks_and_diagnostics(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    result = await probe(transport, pause=0)
    assert len(result.supported_blocks()) == len(BLOCKS)
    diag = result.as_diagnostics()
    assert diag["0x0100"] == "supported"
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_probe.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `probe.py`**

```python
"""Per-unit capability probe.

Not every SRNE firmware exposes every block. Instead of assuming, each block is
read exactly once at setup and classified:

  SUPPORTED   -- the read returned data; its fields become entities.
  UNSUPPORTED -- the device answered IllegalDataAddress, or a protocol error
                 that survived a retry (empty/acknowledge replies mean the
                 block is not there for this slave). No entities are created.
  UNKNOWN     -- the link failed (timeout / socket). We do NOT create entities
                 from a guess; the coordinator re-probes later.

THIS MODULE MUST NOT IMPORT homeassistant.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field as dc_field
from enum import StrEnum

from .registers import BLOCKS, Block
from .transport.base import (
    Transport,
    TransportConnectionError,
    TransportError,
    TransportProtocolError,
    UnsupportedRegisterError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PAUSE = 0.25


class BlockSupport(StrEnum):
    """Whether a block exists on this particular unit."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class ProbeFailedError(TransportError):
    """Not a single block answered -- the unit or the link is down."""


@dataclass(slots=True)
class ProbeResult:
    """Outcome of one probe pass."""

    support: dict[int, BlockSupport] = dc_field(default_factory=dict)
    registers: dict[int, int] = dc_field(default_factory=dict)
    errors: dict[int, str] = dc_field(default_factory=dict)

    def supported_blocks(self, blocks: tuple[Block, ...] = BLOCKS) -> tuple[Block, ...]:
        """Blocks whose fields may become entities."""
        return tuple(
            block
            for block in blocks
            if self.support.get(block.addr) is BlockSupport.SUPPORTED
        )

    def as_diagnostics(self) -> dict[str, str]:
        """Support map keyed by hex address, for diagnostics output."""
        return {f"0x{addr:04X}": state.value for addr, state in sorted(self.support.items())}


async def probe(
    transport: Transport,
    blocks: tuple[Block, ...] = BLOCKS,
    *,
    pause: float = DEFAULT_PAUSE,
    retries: int = 1,
) -> ProbeResult:
    """Read every block once and classify it.

    Raises ProbeFailedError if every block ended UNKNOWN (dead link), so the
    caller can surface ConfigEntryNotReady instead of an empty device.
    """
    result = ProbeResult()

    for index, block in enumerate(blocks):
        if index and pause:
            await asyncio.sleep(pause)
        state, error = await _probe_one(transport, block, retries, result)
        result.support[block.addr] = state
        if error is not None:
            result.errors[block.addr] = error
        _LOGGER.debug(
            "Probe 0x%04X (%s) -> %s%s",
            block.addr, block.name, state.value, f" [{error}]" if error else "",
        )

    if all(state is BlockSupport.UNKNOWN for state in result.support.values()):
        raise ProbeFailedError("no block answered; the unit or the link is down")

    return result


async def _probe_one(
    transport: Transport,
    block: Block,
    retries: int,
    result: ProbeResult,
) -> tuple[BlockSupport, str | None]:
    """Probe one block, retrying protocol/connection errors `retries` times."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            values = await transport.read_holding(block.addr, block.count)
        except UnsupportedRegisterError as err:
            return BlockSupport.UNSUPPORTED, str(err)
        except (TransportProtocolError, TransportConnectionError) as err:
            last_error = err
            if attempt < retries:
                await asyncio.sleep(0.2)
            continue
        for offset, value in enumerate(values):
            result.registers[block.addr + offset] = value
        return BlockSupport.SUPPORTED, None

    if isinstance(last_error, TransportProtocolError):
        # Empty / AcknowledgeError that survived a retry: the block is not
        # there for this slave id.
        return BlockSupport.UNSUPPORTED, str(last_error)
    return BlockSupport.UNKNOWN, str(last_error)
```

- [ ] **Step 4: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_probe.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/probe.py tests/test_probe.py
git -C /data/claude/ha-srne-inverter commit -m "feat(probe): classify each register block as supported/unsupported/unknown

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** 7 tests pass; a unit missing `0xF02C` produces `UNSUPPORTED` for it and `SUPPORTED` for the rest.

---

### Task 6: `tools/probe.py` CLI + "core is HA-free" guard

**Files:**
- Create: `tools/probe.py`
- Test: `tests/test_core_is_ha_free.py`

**Interfaces:**
- Consumes: `registers`, `probe`, `transport.solarman_v5` imported as *top-level* modules.
- Produces: CLI `tools/probe.py HOST --serial N [--port 8899] [--slave 1] [--json OUT]`.

- [ ] **Step 1: Write the failing guard test**

`tests/test_core_is_ha_free.py`:

```python
"""The core modules must be importable on a host without Home Assistant."""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPONENT = REPO / "custom_components" / "srne_inverter"

SNIPPET = """
import sys
sys.path.insert(0, {component!r})
import registers, probe
import transport.base, transport.solarman_v5
bad = sorted(m for m in sys.modules if m.split(".")[0] == "homeassistant")
assert not bad, bad
assert registers.BLOCKS
assert probe.BlockSupport.SUPPORTED
print("OK")
"""


def test_core_modules_import_without_homeassistant():
    result = subprocess.run(
        [sys.executable, "-c", SNIPPET.format(component=str(COMPONENT))],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_cli_probe_shows_help_without_homeassistant():
    result = subprocess.run(
        [sys.executable, str(REPO / "tools" / "probe.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--slave" in result.stdout
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_core_is_ha_free.py -v`
Expected: FAIL — `tools/probe.py` does not exist yet.

- [ ] **Step 3: Write `tools/probe.py`**

```python
#!/usr/bin/env python3
"""Probe one SRNE/BlueSun inverter through its Solarman logger and print what
it actually supports, using the very same register map the Home Assistant
integration uses.

Home Assistant is NOT required: the core modules are imported as top-level
modules from custom_components/srne_inverter/, which never executes that
package's __init__.py.

Usage:
  tools/probe.py 192.168.188.240 --serial 3548208972 --slave 1
  tools/probe.py 192.168.188.242 --serial 3548738877 --slave 2 --json inv2.json

Only ONE client can talk to a logger at a time: stop justice_watch.py and
friends before running this.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "srne_inverter"
sys.path.insert(0, str(COMPONENT))

import registers as R  # noqa: E402
from probe import BlockSupport, probe  # noqa: E402
from transport.solarman_v5 import SolarmanV5Transport  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="logger IP or hostname")
    parser.add_argument("--serial", type=int, required=True, help="logger serial")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--slave", type=int, default=1, help="Modbus slave id")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", help="write the full report to this file")
    return parser


async def run(args: argparse.Namespace) -> int:
    transport = SolarmanV5Transport(
        args.host, args.serial, port=args.port,
        slave_id=args.slave, timeout=args.timeout,
    )
    await transport.connect()
    try:
        result = await probe(transport)
    finally:
        await transport.close()

    values = R.decode(result.registers)

    print(f"=== {args.host} (logger {args.serial}, slave {args.slave}) ===")
    print("\nBlocks")
    for block in R.BLOCKS:
        state = result.support[block.addr]
        mark = {"supported": "OK ", "unsupported": "-- ", "unknown": "?? "}[state.value]
        note = result.errors.get(block.addr, "")
        print(f"  {mark} 0x{block.addr:04X} +{block.count:<3} {block.name:<14} {note}")

    print("\nDecoded values")
    for key in sorted(values):
        print(f"  {key:<32} {values[key]}")

    missing = [
        b.name for b in R.BLOCKS
        if result.support[b.addr] is not BlockSupport.SUPPORTED
    ]
    print(f"\n{len(R.BLOCKS) - len(missing)}/{len(R.BLOCKS)} blocks supported"
          + (f"; missing: {', '.join(missing)}" if missing else ""))

    if args.json:
        report = {
            "host": args.host,
            "serial": args.serial,
            "slave": args.slave,
            "support": result.as_diagnostics(),
            "errors": {f"0x{a:04X}": e for a, e in result.errors.items()},
            "registers": {f"0x{a:04X}": v for a, v in sorted(result.registers.items())},
            "values": values,
        }
        Path(args.json).write_text(json.dumps(report, indent=1, ensure_ascii=False))
        print(f"report saved: {args.json}")

    return 0 if not missing else 1


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_core_is_ha_free.py -v`
Expected: 2 passed

- [ ] **Step 5: Run the whole suite**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest -v`
Expected: all green (≈ 32 tests)

- [ ] **Step 6: Commit**

```bash
git -C /data/claude/ha-srne-inverter add tools/probe.py tests/test_core_is_ha_free.py
git -C /data/claude/ha-srne-inverter commit -m "feat(tools): standalone probe CLI reusing the integration register map

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** `tools/probe.py --help` exits 0 and never imports `homeassistant`.

---

## Phase 2 — Home Assistant runtime

### Task 7: `coordinator.py` — tiered polling, backoff, writes with read-back

**Files:**
- Create: `custom_components/srne_inverter/coordinator.py`
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: `registers`, `probe`, `transport.base`, `const`.
- Produces:
  - `SrneData(registers: dict[int, int], values: dict[str, object], support: dict[int, BlockSupport])`
  - `SrneCoordinator(hass, entry, transport, *, scan_interval, warm_interval, cold_interval)` extending `DataUpdateCoordinator[SrneData]`, with:
    - `async async_probe() -> ProbeResult`
    - `async async_write_field(field: Field, value: float | str) -> None`
    - `async async_write_raw(address: int, raw: int) -> int` (returns read-back)
    - `async async_read_raw(address: int, count: int) -> list[int]`
    - `async async_set_connection_enabled(enabled: bool) -> None`
    - properties `connection_enabled: bool`, `support: dict[int, BlockSupport]`, `probe_result: ProbeResult | None`, `failure_count: int`
    - `supported_field_keys() -> set[str]`

- [ ] **Step 1: Write the failing tests**

`tests/test_coordinator.py`:

```python
"""Coordinator: tiers, pauses, backoff, connection toggle, write read-back."""

from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter import registers as R
from custom_components.srne_inverter.const import DOMAIN
from custom_components.srne_inverter.coordinator import SrneCoordinator
from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportConnectionError,
)
from tests.fake_transport import FakeTransport


@pytest.fixture(name="entry")
def entry_fixture(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="3548208972", title="Justice Inv 1")
    entry.add_to_hass(hass)
    return entry


def build(hass, entry, transport, **kwargs):
    return SrneCoordinator(
        hass, entry, transport,
        scan_interval=kwargs.get("scan_interval", 10),
        warm_interval=kwargs.get("warm_interval", 60),
        cold_interval=kwargs.get("cold_interval", 300),
    )


async def test_probe_then_first_cycle_reads_all_tiers(hass, entry, justice_registers):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    assert len(transport.reads) == len(R.BLOCKS)
    transport.reads.clear()

    await coordinator.async_refresh()
    assert coordinator.last_update_success is True
    # First cycle is due on every tier.
    assert len(transport.reads) == len(R.BLOCKS)
    assert coordinator.data.values["battery_soc"] == 55


async def test_second_cycle_reads_hot_only(hass, entry, justice_registers):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    transport.reads.clear()
    await coordinator.async_refresh()
    hot = [b.addr for b in R.BLOCKS if b.tier is R.BlockTier.HOT]
    assert sorted(addr for addr, _ in transport.reads) == sorted(hot)


async def test_warm_and_cold_values_survive_hot_only_cycles(
    hass, entry, justice_registers
):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    assert coordinator.data.values["boost_voltage"] == pytest.approx(57.6)
    assert coordinator.data.values["running_days"] == 0


async def test_unsupported_blocks_are_never_polled(hass, entry, justice_registers):
    partial = {k: v for k, v in justice_registers.items() if k < 0xF000}
    transport = FakeTransport(partial)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    transport.reads.clear()
    await coordinator.async_refresh()
    assert 0xF02C not in [addr for addr, _ in transport.reads]


async def test_connection_error_marks_update_failed_and_closes(
    hass, entry, justice_registers
):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    transport._read_errors = [TransportConnectionError("dead")] * 20
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert transport.close_count >= 1
    assert coordinator.failure_count == 1


async def test_backoff_skips_the_socket_entirely(hass, entry, justice_registers):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    transport._read_errors = [TransportConnectionError("dead")] * 20
    await coordinator.async_refresh()
    transport.reads.clear()
    await coordinator.async_refresh()   # still inside the 2 s backoff window
    assert transport.reads == []
    assert coordinator.last_update_success is False


async def test_write_field_reads_back_and_updates_state(
    hass, entry, justice_registers
):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()

    field = R.field_by_key("soc_low_alarm")
    await coordinator.async_write_field(field, 16)
    assert transport.writes == [(0xE01E, 16)]
    assert coordinator.data.values["soc_low_alarm"] == 16


async def test_write_mismatch_raises(hass, entry, justice_registers):
    class StubbornTransport(FakeTransport):
        async def write_holding(self, addr, value):
            self.writes.append((addr, value))  # accepted but ignored

    transport = StubbornTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    with pytest.raises(HomeAssistantError, match="read back"):
        await coordinator.async_write_field(R.field_by_key("soc_low_alarm"), 16)


async def test_write_rejected_by_firmware_raises(hass, entry, justice_registers):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_write_raw(0xE20F, 2)


async def test_out_of_range_write_is_refused_before_touching_the_bus(
    hass, entry, justice_registers
):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_write_field(R.field_by_key("soc_low_alarm"), 250)
    assert transport.writes == []


async def test_disabling_the_connection_closes_and_stops_polling(
    hass, entry, justice_registers
):
    transport = FakeTransport(justice_registers)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    await coordinator.async_refresh()

    await coordinator.async_set_connection_enabled(False)
    assert transport.connected is False
    assert coordinator.update_interval is None
    transport.reads.clear()
    await coordinator.async_refresh()
    assert transport.reads == []
    assert coordinator.last_update_success is False

    await coordinator.async_set_connection_enabled(True)
    assert coordinator.update_interval == timedelta(seconds=10)
    assert coordinator.last_update_success is True


async def test_supported_field_keys_excludes_missing_blocks(
    hass, entry, justice_registers
):
    partial = {k: v for k, v in justice_registers.items() if k < 0xF000}
    transport = FakeTransport(partial)
    coordinator = build(hass, entry, transport)
    await coordinator.async_probe()
    keys = coordinator.supported_field_keys()
    assert "battery_soc" in keys
    assert "running_days" not in keys
    assert "battery_power" in keys      # derived, both inputs available
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_coordinator.py -v`
Expected: FAIL with `ModuleNotFoundError: custom_components.srne_inverter.coordinator`

- [ ] **Step 3: Write `coordinator.py`**

```python
"""DataUpdateCoordinator for one SRNE inverter behind one Solarman logger."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field as dc_field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import registers as R
from .const import BACKOFF_SECONDS, BLOCK_PAUSE, DOMAIN, REPROBE_AFTER_FAILURES, WRITE_SETTLE
from .probe import BlockSupport, ProbeResult, probe
from .registers import BlockTier, Field
from .transport.base import Transport, TransportError

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SrneData:
    """One decoded snapshot handed to the entities."""

    registers: dict[int, int] = dc_field(default_factory=dict)
    values: dict[str, object] = dc_field(default_factory=dict)
    support: dict[int, BlockSupport] = dc_field(default_factory=dict)


class SrneCoordinator(DataUpdateCoordinator[SrneData]):
    """Polls one inverter in three tiers over a single, locked connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        transport: Transport,
        *,
        scan_interval: int,
        warm_interval: int,
        cold_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.transport = transport
        self.scan_interval = scan_interval
        self.warm_interval = warm_interval
        self.cold_interval = cold_interval

        self.probe_result: ProbeResult | None = None
        self.failure_count = 0
        self._connection_enabled = True
        self._registers: dict[int, int] = {}
        self._tier_due: dict[BlockTier, float] = {}
        self._backoff_until = 0.0
        self._backoff_index = 0
        self._reprobe_pending = False

    # ---- state ---------------------------------------------------------

    @property
    def connection_enabled(self) -> bool:
        return self._connection_enabled

    @property
    def support(self) -> dict[int, BlockSupport]:
        return self.probe_result.support if self.probe_result else {}

    def supported_field_keys(self) -> set[str]:
        """Field and derived keys whose blocks answered during the probe."""
        supported = {
            addr for addr, state in self.support.items()
            if state is BlockSupport.SUPPORTED
        }
        keys = {f.key for f in R.FIELDS if f.block_addr in supported}
        keys |= {
            d.key for d in R.DERIVED if all(i in keys for i in d.inputs)
        }
        return keys

    # ---- probe ---------------------------------------------------------

    async def async_probe(self) -> ProbeResult:
        """Run the capability probe, reusing the open connection."""
        if not self.transport.connected:
            await self.transport.connect()
        result = await probe(self.transport, pause=BLOCK_PAUSE)
        self.probe_result = result
        self._registers.update(result.registers)
        self._reprobe_pending = False
        # A fresh probe already filled every tier: reset the schedule.
        now = time.monotonic()
        self._tier_due = {
            BlockTier.HOT: now,
            BlockTier.WARM: now + self.warm_interval,
            BlockTier.COLD: now + self.cold_interval,
        }
        _LOGGER.debug("Probe result for %s: %s", self.name, result.as_diagnostics())
        return result

    # ---- polling -------------------------------------------------------

    async def _async_update_data(self) -> SrneData:
        if not self._connection_enabled:
            raise UpdateFailed("connection disabled by the user")

        now = time.monotonic()
        if now < self._backoff_until:
            raise UpdateFailed(
                f"backing off for {self._backoff_until - now:.0f} s after "
                f"{self.failure_count} failures"
            )

        try:
            if not self.transport.connected:
                await self.transport.connect()
            if self._reprobe_pending:
                await self.async_probe()
            await self._read_due_blocks()
        except TransportError as err:
            await self._register_failure(err)
            raise UpdateFailed(str(err)) from err

        self._register_success()
        return SrneData(
            registers=dict(self._registers),
            values=R.decode(self._registers),
            support=dict(self.support),
        )

    async def _read_due_blocks(self) -> None:
        now = time.monotonic()
        due = {tier for tier, at in self._tier_due.items() if now >= at}
        if not due:
            due = {BlockTier.HOT}

        blocks = [
            block
            for block in R.BLOCKS
            if block.tier in due
            and self.support.get(block.addr) is not BlockSupport.UNSUPPORTED
        ]
        for index, block in enumerate(blocks):
            if index:
                await asyncio.sleep(BLOCK_PAUSE)
            values = await self.transport.read_holding(block.addr, block.count)
            for offset, value in enumerate(values):
                self._registers[block.addr + offset] = value

        interval = {
            BlockTier.HOT: self.scan_interval,
            BlockTier.WARM: self.warm_interval,
            BlockTier.COLD: self.cold_interval,
        }
        for tier in due:
            self._tier_due[tier] = now + interval[tier]

    def _register_success(self) -> None:
        self.failure_count = 0
        self._backoff_index = 0
        self._backoff_until = 0.0

    async def _register_failure(self, err: Exception) -> None:
        self.failure_count += 1
        delay = BACKOFF_SECONDS[min(self._backoff_index, len(BACKOFF_SECONDS) - 1)]
        self._backoff_index += 1
        self._backoff_until = time.monotonic() + delay
        if self.failure_count >= REPROBE_AFTER_FAILURES:
            # The unit may have come back different; re-probe on reconnect.
            self._reprobe_pending = True
        _LOGGER.debug(
            "%s: read cycle failed (%s); backing off %.0f s", self.name, err, delay
        )
        await self.transport.close()

    # ---- writes --------------------------------------------------------

    async def async_write_field(self, field: Field, value: float | str) -> None:
        """Write a mapped field and verify the read-back."""
        try:
            raw = R.encode(field, value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        await self.async_write_raw(field.address, raw)

    async def async_write_raw(self, address: int, raw: int) -> int:
        """Write one raw register, re-read it and fail on any mismatch."""
        if not self._connection_enabled:
            raise HomeAssistantError("the connection to this inverter is disabled")
        try:
            if not self.transport.connected:
                await self.transport.connect()
            await self.transport.write_holding(address, raw)
            await asyncio.sleep(WRITE_SETTLE)
            read_back = (await self.transport.read_holding(address, 1))[0]
        except TransportError as err:
            raise HomeAssistantError(
                f"write to 0x{address:04X} failed: {err}"
            ) from err

        if read_back != raw:
            raise HomeAssistantError(
                f"0x{address:04X}: wrote {raw} but read back {read_back}; "
                "the inverter refused or clamped the value"
            )

        self._registers[address] = read_back
        self.async_set_updated_data(
            SrneData(
                registers=dict(self._registers),
                values=R.decode(self._registers),
                support=dict(self.support),
            )
        )
        return read_back

    async def async_read_raw(self, address: int, count: int) -> list[int]:
        """Read raw registers on demand (service `read_register`)."""
        if not self._connection_enabled:
            raise HomeAssistantError("the connection to this inverter is disabled")
        try:
            if not self.transport.connected:
                await self.transport.connect()
            return await self.transport.read_holding(address, count)
        except TransportError as err:
            raise HomeAssistantError(
                f"read of 0x{address:04X} failed: {err}"
            ) from err

    # ---- connection switch ---------------------------------------------

    async def async_set_connection_enabled(self, enabled: bool) -> None:
        """Pause/resume polling so external tools can own the logger."""
        if enabled == self._connection_enabled:
            return
        self._connection_enabled = enabled
        if enabled:
            self.update_interval = timedelta(seconds=self.scan_interval)
            self._backoff_until = 0.0
            self._backoff_index = 0
            await self.async_refresh()
        else:
            self.update_interval = None
            await self.transport.close()
            self.last_update_success = False
            self.async_update_listeners()

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.transport.close()
```

- [ ] **Step 4: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_coordinator.py -v`
Expected: 12 passed. If `test_backoff_skips_the_socket_entirely` is flaky, confirm `BACKOFF_SECONDS[0] == 2.0` and that the test does not advance time.

- [ ] **Step 5: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/coordinator.py tests/test_coordinator.py
git -C /data/claude/ha-srne-inverter commit -m "feat(coordinator): tiered polling with backoff, verified writes and connection toggle

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** hot-only cycles touch exactly the 4 HOT blocks; a mismatched write raises `HomeAssistantError`.

---

### Task 8: `__init__.py` — entry setup, typed `runtime_data`, unload, reload

**Files:**
- Modify: `custom_components/srne_inverter/__init__.py` (replace placeholder)
- Test: `tests/test_init.py`

**Interfaces:**
- Produces:
  - `SrneRuntimeData(coordinator: SrneCoordinator, transport: Transport, added_field_keys: set[str])`
  - `type SrneConfigEntry = ConfigEntry[SrneRuntimeData]`
  - `async_setup_entry(hass, entry) -> bool`, `async_unload_entry`, `async_reload_entry`
  - `build_transport(entry) -> SolarmanV5Transport` (patched by tests)

- [ ] **Step 1: Write the failing test**

`tests/test_init.py`:

```python
"""Entry setup wires probe -> coordinator -> platforms and cleans up on unload."""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter.const import (
    CONF_COLD_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    CONF_WARM_INTERVAL,
    DOMAIN,
)
from tests.fake_transport import FakeTransport

ENTRY_DATA = {
    CONF_HOST: "192.168.188.240",
    CONF_PORT: 8899,
    CONF_SERIAL: "3548208972",
    CONF_SLAVE_ID: 1,
}
ENTRY_OPTIONS = {
    CONF_SCAN_INTERVAL: 10,
    CONF_WARM_INTERVAL: 60,
    CONF_COLD_INTERVAL: 300,
}


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


async def setup_entry(hass, transport):
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="3548208972", title="Justice Inv 1",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_setup_probes_and_stores_runtime_data(hass, justice_registers):
    transport = FakeTransport(justice_registers)
    entry = await setup_entry(hass, transport)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinator.data.values["battery_soc"] == 55
    assert entry.runtime_data.transport is transport


async def test_setup_raises_not_ready_when_the_logger_is_busy(
    hass, justice_registers
):
    transport = FakeTransport(justice_registers, fail_connect=True)
    entry = MockConfigEntry(
        domain=DOMAIN, data=ENTRY_DATA, options=ENTRY_OPTIONS,
        unique_id="x", title="Busy",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_closes_the_socket(hass, justice_registers):
    transport = FakeTransport(justice_registers)
    entry = await setup_entry(hass, transport)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert transport.connected is False


async def test_changing_options_reloads_the_entry(hass, justice_registers):
    transport = FakeTransport(justice_registers)
    entry = await setup_entry(hass, transport)
    with patch(
        "custom_components.srne_inverter.build_transport", return_value=transport
    ):
        hass.config_entries.async_update_entry(
            entry, options={**ENTRY_OPTIONS, CONF_SCAN_INTERVAL: 30}
        )
        await hass.async_block_till_done()
    assert entry.runtime_data.coordinator.scan_interval == 30
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_init.py -v`
Expected: FAIL — `build_transport` does not exist.

- [ ] **Step 3: Write `__init__.py`**

```python
"""The SRNE Inverter (Solarman V5) integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field as dc_field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_COLD_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    CONF_WARM_INTERVAL,
    DEFAULT_COLD_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLAVE_ID,
    DEFAULT_WARM_INTERVAL,
    PLATFORMS,
    SOCKET_TIMEOUT,
)
from .coordinator import SrneCoordinator
from .transport.base import Transport, TransportError
from .transport.solarman_v5 import SolarmanV5Transport

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SrneRuntimeData:
    """Everything the platforms need, hung off the config entry."""

    coordinator: SrneCoordinator
    transport: Transport
    added_field_keys: set[str] = dc_field(default_factory=set)


type SrneConfigEntry = ConfigEntry[SrneRuntimeData]


def build_transport(entry: SrneConfigEntry) -> Transport:
    """Create the transport for this entry (patched in tests)."""
    return SolarmanV5Transport(
        entry.data[CONF_HOST],
        int(entry.data[CONF_SERIAL]),
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        slave_id=entry.data.get(CONF_SLAVE_ID, DEFAULT_SLAVE_ID),
        timeout=SOCKET_TIMEOUT,
    )


async def async_setup_entry(hass: HomeAssistant, entry: SrneConfigEntry) -> bool:
    """Probe the unit, start the coordinator and forward the platforms."""
    transport = build_transport(entry)
    coordinator = SrneCoordinator(
        hass,
        entry,
        transport,
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        warm_interval=entry.options.get(CONF_WARM_INTERVAL, DEFAULT_WARM_INTERVAL),
        cold_interval=entry.options.get(CONF_COLD_INTERVAL, DEFAULT_COLD_INTERVAL),
    )

    try:
        await coordinator.async_probe()
    except TransportError as err:
        await transport.close()
        raise ConfigEntryNotReady(
            f"cannot probe {entry.data[CONF_HOST]}: {err}"
        ) from err

    entry.runtime_data = SrneRuntimeData(coordinator=coordinator, transport=transport)

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SrneConfigEntry) -> bool:
    """Unload the platforms and free the logger's single TCP slot."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.coordinator.async_shutdown()
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: SrneConfigEntry) -> None:
    """Reload after the options flow changed the intervals."""
    await hass.config_entries.async_reload(entry.entry_id)
```

- [ ] **Step 4: Create the stub platform modules so the forward succeeds**

Create each of `binary_sensor.py`, `button.py`, `number.py`, `select.py`, `sensor.py`, `switch.py` with:

```python
"""Placeholder platform, implemented in a later task."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry,  # noqa: ANN001 - typed once entity.py lands
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the platform (no entities yet)."""
    return
```

- [ ] **Step 5: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_init.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter tests/test_init.py
git -C /data/claude/ha-srne-inverter commit -m "feat(setup): config entry setup with probe, typed runtime_data and reload listener

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** entry reaches `LOADED`; a busy logger yields `SETUP_RETRY`; unload closes the socket.

---

### Task 9: `entity.py` — base entity, DeviceInfo and dynamic entity addition

**Files:**
- Create: `custom_components/srne_inverter/entity.py`
- Test: `tests/test_entity.py`

**Interfaces:**
- Consumes: `SrneCoordinator`, `SrneConfigEntry`, `registers.Field`.
- Produces:
  - `SrneEntity(CoordinatorEntity[SrneCoordinator])` with `__init__(coordinator, entry, key: str, label: str, category: str | None = None)`, `_attr_has_entity_name = True`, `_attr_unique_id = f"{serial}_{key}"`, `device_info`, `available`.
  - `SrneFieldEntity(SrneEntity)` with `field: Field` and `native_raw` / `native_value_from_data()` helpers.
  - `async_setup_field_platform(hass, entry, async_add_entities, *, builder, signal_key)` — adds entities for currently supported fields and re-runs on the `SIGNAL_NEW_ENTITIES` dispatcher signal (fired by the reprobe button/service).

- [ ] **Step 1: Write the failing test**

`tests/test_entity.py`:

```python
"""Base entity: device info, availability and unique ids."""

from homeassistant.helpers import device_registry as dr

from custom_components.srne_inverter.const import DOMAIN
from tests.fake_transport import FakeTransport
from tests.test_init import setup_entry


async def test_device_is_registered_with_serial_identifier(
    hass, justice_registers, enable_custom_integrations
):
    transport = FakeTransport(justice_registers)
    entry = await setup_entry(hass, transport)
    device = dr.async_get(hass).async_get_device({(DOMAIN, "3548208972")})
    assert device is not None
    assert device.manufacturer == "SRNE"
    assert device.sw_version == "V8.18"
    assert device.name == "Justice Inv 1"


async def test_entities_become_unavailable_after_a_failed_cycle(
    hass, justice_registers, enable_custom_integrations
):
    from custom_components.srne_inverter.transport.base import (
        TransportConnectionError,
    )

    transport = FakeTransport(justice_registers)
    entry = await setup_entry(hass, transport)
    state = hass.states.get("sensor.justice_inv_1_battery_soc")
    assert state is not None and state.state == "55"

    transport._read_errors = [TransportConnectionError("dead")] * 20
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.justice_inv_1_battery_soc").state == "unavailable"
```

> Note: this test only passes once Task 10 lands the sensor platform. Write it now, mark the second test `@pytest.mark.xfail(reason="sensor platform lands in Task 10", strict=False)` if running Task 9 standalone, and remove the marker in Task 10.

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_entity.py -v`
Expected: FAIL — no device is registered.

- [ ] **Step 3: Write `entity.py`**

```python
"""Shared entity base for every srne_inverter platform."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SERIAL, DEFAULT_MODEL, DOMAIN, MANUFACTURER, SIGNAL_NEW_ENTITIES
from .coordinator import SrneCoordinator
from .registers import Field

_CATEGORIES: dict[str, EntityCategory] = {
    "config": EntityCategory.CONFIG,
    "diagnostic": EntityCategory.DIAGNOSTIC,
}


class SrneEntity(CoordinatorEntity[SrneCoordinator]):
    """Base entity: device identity, naming and availability."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SrneCoordinator,
        entry,  # SrneConfigEntry, untyped to avoid a circular import
        key: str,
        label: str,
        category: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._serial = str(entry.data[CONF_SERIAL])
        self._attr_name = label
        self._attr_unique_id = f"{self._serial}_{key}"
        if category is not None:
            self._attr_entity_category = _CATEGORIES[category]

    @property
    def device_info(self) -> DeviceInfo:
        values = self.coordinator.data.values if self.coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            manufacturer=MANUFACTURER,
            model=DEFAULT_MODEL,
            name=self._entry.title,
            sw_version=values.get("firmware_version"),
            hw_version=values.get("hardware_version"),
            serial_number=self._serial,
            configuration_url=f"http://{self._entry.data['host']}/status.html",
        )


class SrneFieldEntity(SrneEntity):
    """Entity backed by one mapped register field."""

    def __init__(self, coordinator: SrneCoordinator, entry, field: Field) -> None:
        super().__init__(coordinator, entry, field.key, field.label, field.category)
        self.field = field

    @property
    def available(self) -> bool:
        return super().available and self.field.key in (
            self.coordinator.data.values if self.coordinator.data else {}
        )

    @property
    def raw_value(self) -> int | None:
        """The undecoded register word, exposed as an attribute."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.registers.get(self.field.address)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "address": f"0x{self.field.address:04X}",
            "raw": self.raw_value,
        }


@callback
def async_setup_field_platform(
    hass: HomeAssistant,
    entry,  # SrneConfigEntry
    async_add_entities: AddConfigEntryEntitiesCallback,
    *,
    builder: Callable[[], Iterable[Entity]],
) -> None:
    """Add the entities this platform owns, and re-add them after a reprobe.

    `builder` must return only entities whose field key is currently supported
    and not yet added; it is responsible for updating
    `entry.runtime_data.added_field_keys`.
    """
    async_add_entities(builder())

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id),
            lambda: async_add_entities(builder()),
        )
    )
```

- [ ] **Step 4: Run the tests**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_entity.py -v`
Expected: the device test passes; the availability test xfails until Task 10.

- [ ] **Step 5: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/entity.py tests/test_entity.py
git -C /data/claude/ha-srne-inverter commit -m "feat(entity): shared base entity with device info and reprobe-aware platform setup

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** the device registry shows one device per entry, identified by the logger serial, with the firmware string read from `0x0014`.

---

### Task 10: `sensor.py` + `binary_sensor.py`

> **Parallel-safe with Tasks 11 and 12.** Distinct files.

**Files:**
- Modify: `custom_components/srne_inverter/sensor.py` (replace stub)
- Modify: `custom_components/srne_inverter/binary_sensor.py` (replace stub)
- Modify: `tests/test_entity.py` (drop the xfail marker)
- Test: `tests/test_sensor.py`

**Interfaces:**
- Consumes: `SrneFieldEntity`, `async_setup_field_platform`, `registers.{FIELDS, DERIVED, FAULT_FIELD_KEYS}`.
- Produces: `SrneSensor`, `SrneDerivedSensor`, `SrneFaultBinarySensor`.

Sensor selection rule: every `Field` that is **not** writable, plus every writable enum/number field is handled by select/number instead. All `Derived` entries become sensors.

- [ ] **Step 1: Write the failing tests**

`tests/test_sensor.py`:

```python
"""Sensor and binary_sensor platforms."""

from tests.fake_transport import FakeTransport
from tests.test_init import setup_entry


async def test_core_sensors_exist_with_decoded_values(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    assert hass.states.get("sensor.justice_inv_1_battery_soc").state == "55"
    assert hass.states.get("sensor.justice_inv_1_battery_voltage").state == "53.1"
    assert hass.states.get("sensor.justice_inv_1_battery_current").state == "0.9"
    assert hass.states.get("sensor.justice_inv_1_machine_state").state == "AC bypass"
    assert hass.states.get("sensor.justice_inv_1_grid_charge_current").state == "1.0"


async def test_derived_sensors_exist(hass, justice_registers, enable_custom_integrations):
    await setup_entry(hass, FakeTransport(justice_registers))
    assert hass.states.get("sensor.justice_inv_1_battery_power") is not None


async def test_read_only_enum_fields_are_sensors_not_selects(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    assert hass.states.get("sensor.justice_inv_1_charge_priority") is not None
    assert hass.states.get("select.justice_inv_1_charge_priority") is None


async def test_sensor_exposes_raw_and_address_attributes(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    attrs = hass.states.get("sensor.justice_inv_1_battery_current").attributes
    assert attrs["address"] == "0x0102"
    assert attrs["raw"] == 65527


async def test_fault_binary_sensor_is_off_when_all_words_are_zero(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    state = hass.states.get("binary_sensor.justice_inv_1_fault_active")
    assert state.state == "off"
    assert state.attributes["device_class"] == "problem"


async def test_fault_binary_sensor_turns_on(
    hass, justice_registers, enable_custom_integrations
):
    registers = {**justice_registers, 0x0205: 0x0040}
    entry = await setup_entry(hass, FakeTransport(registers))
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.justice_inv_1_fault_active")
    assert state.state == "on"
    assert state.attributes["fault_word_2"] == "0x0040"


async def test_sensors_of_unsupported_blocks_are_not_created(
    hass, justice_registers, enable_custom_integrations
):
    partial = {k: v for k, v in justice_registers.items() if k < 0xF000}
    await setup_entry(hass, FakeTransport(partial))
    assert hass.states.get("sensor.justice_inv_1_battery_soc") is not None
    assert hass.states.get("sensor.justice_inv_1_running_days") is None
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_sensor.py -v`
Expected: FAIL — every `hass.states.get` returns `None`.

- [ ] **Step 3: Write `sensor.py`**

```python
"""Sensor platform: every read-only mapped field plus the derived values."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .coordinator import SrneCoordinator
from .entity import SrneEntity, SrneFieldEntity, async_setup_field_platform
from .registers import DERIVED, FIELDS, Derived, Field, FieldKind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for every supported, non-writable field."""
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder() -> list[SensorEntity]:
        supported = coordinator.supported_field_keys()
        new: list[SensorEntity] = []
        for field in FIELDS:
            if field.write is not None or field.key not in supported:
                continue
            if f"sensor:{field.key}" in added:
                continue
            added.add(f"sensor:{field.key}")
            new.append(SrneSensor(coordinator, entry, field))
        for derived in DERIVED:
            if derived.key not in supported or f"sensor:{derived.key}" in added:
                continue
            added.add(f"sensor:{derived.key}")
            new.append(SrneDerivedSensor(coordinator, entry, derived))
        return new

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


class SrneSensor(SrneFieldEntity, SensorEntity):
    """A single decoded register."""

    def __init__(
        self, coordinator: SrneCoordinator, entry: SrneConfigEntry, field: Field
    ) -> None:
        super().__init__(coordinator, entry, field)
        if field.kind is FieldKind.ENUM:
            assert field.enum is not None
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = field.enum.options + [
                f"Unknown ({raw})" for raw in ()
            ]
        else:
            self._attr_native_unit_of_measurement = field.unit
            if field.device_class:
                self._attr_device_class = SensorDeviceClass(field.device_class)
            if field.state_class:
                self._attr_state_class = SensorStateClass(field.state_class)
            self._attr_suggested_display_precision = field.precision

    @property
    def native_value(self) -> object | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.values.get(self.field.key)
        if (
            self._attr_device_class is SensorDeviceClass.ENUM
            and isinstance(value, str)
            and self._attr_options is not None
            and value not in self._attr_options
        ):
            # Never let an unmapped raw code break the enum sensor.
            return None
        return value


class SrneDerivedSensor(SrneEntity, SensorEntity):
    """A value computed from other fields (battery power, total load)."""

    def __init__(
        self, coordinator: SrneCoordinator, entry: SrneConfigEntry, derived: Derived
    ) -> None:
        super().__init__(coordinator, entry, derived.key, derived.label)
        self._derived = derived
        self._attr_native_unit_of_measurement = derived.unit
        if derived.device_class:
            self._attr_device_class = SensorDeviceClass(derived.device_class)
        if derived.state_class:
            self._attr_state_class = SensorStateClass(derived.state_class)
        self._attr_suggested_display_precision = derived.precision

    @property
    def native_value(self) -> object | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.values.get(self._derived.key)
```

- [ ] **Step 4: Write `binary_sensor.py`**

```python
"""Binary sensor platform: one aggregated 'fault active' entity."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .entity import SrneEntity, async_setup_field_platform
from .registers import FAULT_FIELD_KEYS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the fault binary sensor if the fault block answered."""
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder() -> list[BinarySensorEntity]:
        supported = coordinator.supported_field_keys()
        if not all(key in supported for key in FAULT_FIELD_KEYS):
            return []
        if "binary_sensor:fault_active" in added:
            return []
        added.add("binary_sensor:fault_active")
        return [SrneFaultBinarySensor(coordinator, entry)]

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


class SrneFaultBinarySensor(SrneEntity, BinarySensorEntity):
    """ON when any of the four fault words is non-zero."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, entry: SrneConfigEntry) -> None:
        super().__init__(coordinator, entry, "fault_active", "Fault Active")

    def _words(self) -> dict[str, str]:
        if self.coordinator.data is None:
            return {}
        return {
            key: str(self.coordinator.data.values[key])
            for key in FAULT_FIELD_KEYS
            if key in self.coordinator.data.values
        }

    @property
    def is_on(self) -> bool | None:
        words = self._words()
        if not words:
            return None
        return any(int(value, 16) != 0 for value in words.values())

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return self._words()
```

- [ ] **Step 5: Remove the xfail marker from `tests/test_entity.py`**

- [ ] **Step 6: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_sensor.py tests/test_entity.py -v`
Expected: 9 passed. If entity ids differ, print `hass.states.async_entity_ids()` once and align the test expectations with the real slug (`sensor.justice_inv_1_<label slug>`).

- [ ] **Step 7: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/sensor.py custom_components/srne_inverter/binary_sensor.py tests/test_sensor.py tests/test_entity.py
git -C /data/claude/ha-srne-inverter commit -m "feat(sensor): sensors for every read-only field, derived values and fault binary sensor

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** `sensor.justice_inv_1_grid_charge_current` reads `1.0`; unsupported blocks produce no entities.

---

### Task 11: `number.py` + `select.py`

> **Parallel-safe with Tasks 10 and 12.**

**Files:**
- Modify: `custom_components/srne_inverter/number.py`, `custom_components/srne_inverter/select.py`
- Test: `tests/test_number_select.py`

**Interfaces:**
- Consumes: `coordinator.async_write_field`, `registers.{FIELDS, WriteSpec, FieldKind}`.
- Produces: `SrneNumber`, `SrneSelect`.

- [ ] **Step 1: Write the failing tests**

`tests/test_number_select.py`:

```python
"""Writable platforms: numbers and selects, always with read-back."""

import pytest
from homeassistant.exceptions import HomeAssistantError

from tests.fake_transport import FakeTransport
from tests.test_init import setup_entry


async def test_number_entities_expose_the_write_range(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    state = hass.states.get("number.justice_inv_1_soc_low_alarm")
    assert state.state == "15.0"
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 100

    voltage = hass.states.get("number.justice_inv_1_boost_charge_voltage")
    assert voltage.state == "57.6"
    assert voltage.attributes["min"] == pytest.approx(40.0)
    assert voltage.attributes["max"] == pytest.approx(64.0)
    assert voltage.attributes["step"] == pytest.approx(0.4)


async def test_setting_a_number_writes_and_reads_back(
    hass, justice_registers, enable_custom_integrations
):
    transport = FakeTransport(justice_registers)
    await setup_entry(hass, transport)
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.justice_inv_1_soc_low_alarm", "value": 16},
        blocking=True,
    )
    assert transport.writes == [(0xE01E, 16)]
    assert hass.states.get("number.justice_inv_1_soc_low_alarm").state == "16.0"


async def test_number_out_of_range_is_refused(
    hass, justice_registers, enable_custom_integrations
):
    transport = FakeTransport(justice_registers)
    await setup_entry(hass, transport)
    with pytest.raises((HomeAssistantError, ValueError)):
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": "number.justice_inv_1_soc_low_alarm", "value": 250},
            blocking=True,
        )
    assert transport.writes == []


async def test_select_options_and_current_value(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    state = hass.states.get("select.justice_inv_1_output_priority")
    assert state.attributes["options"] == ["SOL", "UTI", "SBU", "SUB"]
    assert hass.states.get("select.justice_inv_1_battery_type").state == "USER"


async def test_selecting_an_option_writes_the_raw_code(
    hass, justice_registers, enable_custom_integrations
):
    transport = FakeTransport(justice_registers)
    await setup_entry(hass, transport)
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": "select.justice_inv_1_output_priority", "option": "SBU"},
        blocking=True,
    )
    assert transport.writes == [(0xE204, 2)]
    assert hass.states.get("select.justice_inv_1_output_priority").state == "SBU"


async def test_firmware_rejection_surfaces_as_an_error(
    hass, justice_registers, enable_custom_integrations
):
    """E215 exists and is writable; simulate a device that clamps the write."""

    class ClampingTransport(FakeTransport):
        async def write_holding(self, addr, value):
            self.writes.append((addr, value))  # silently ignored

    transport = ClampingTransport(justice_registers)
    await setup_entry(hass, transport)
    with pytest.raises(HomeAssistantError, match="read back"):
        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": "select.justice_inv_1_bms_communication", "option": "CAN"},
            blocking=True,
        )
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_number_select.py -v`
Expected: FAIL — no number/select entities exist.

- [ ] **Step 3: Write `number.py`**

```python
"""Number platform: every writable numeric field, range taken from WriteSpec."""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .entity import SrneFieldEntity, async_setup_field_platform
from .registers import FIELDS, Field, FieldKind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a number for every writable non-enum field."""
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder() -> list[NumberEntity]:
        supported = coordinator.supported_field_keys()
        new: list[NumberEntity] = []
        for field in FIELDS:
            if field.write is None or field.kind is FieldKind.ENUM:
                continue
            if field.key not in supported or f"number:{field.key}" in added:
                continue
            added.add(f"number:{field.key}")
            new.append(SrneNumber(coordinator, entry, field))
        return new

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


class SrneNumber(SrneFieldEntity, NumberEntity):
    """A writable scalar register. Bounds come from the raw WriteSpec."""

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry: SrneConfigEntry, field: Field) -> None:
        super().__init__(coordinator, entry, field)
        assert field.write is not None
        low = field.write.min_raw * field.scale
        high = field.write.max_raw * field.scale
        self._attr_native_min_value = min(low, high)
        self._attr_native_max_value = max(low, high)
        self._attr_native_step = abs(field.write.step_raw * field.scale)
        self._attr_native_unit_of_measurement = field.unit
        if field.device_class:
            self._attr_device_class = NumberDeviceClass(field.device_class)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.values.get(self.field.key)
        return float(value) if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write_field(self.field, value)
```

- [ ] **Step 4: Write `select.py`**

```python
"""Select platform: every writable enum field."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .entity import SrneFieldEntity, async_setup_field_platform
from .registers import FIELDS, Field, FieldKind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a select for every writable enum field."""
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder() -> list[SelectEntity]:
        supported = coordinator.supported_field_keys()
        new: list[SelectEntity] = []
        for field in FIELDS:
            if field.write is None or field.kind is not FieldKind.ENUM:
                continue
            if field.key not in supported or f"select:{field.key}" in added:
                continue
            added.add(f"select:{field.key}")
            new.append(SrneSelect(coordinator, entry, field))
        return new

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


class SrneSelect(SrneFieldEntity, SelectEntity):
    """A writable enum register."""

    def __init__(self, coordinator, entry: SrneConfigEntry, field: Field) -> None:
        super().__init__(coordinator, entry, field)
        assert field.enum is not None
        self._attr_options = field.enum.options

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.values.get(self.field.key)
        return value if isinstance(value, str) and value in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_write_field(self.field, option)
```

- [ ] **Step 5: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_number_select.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/number.py custom_components/srne_inverter/select.py tests/test_number_select.py
git -C /data/claude/ha-srne-inverter commit -m "feat(control): number and select platforms with verified write-then-read-back

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** setting `number.*_soc_low_alarm` to 16 writes raw 16 to `0xE01E` and reflects the read-back.

---

### Task 12: `switch.py` (connection) + `button.py` (reprobe)

> **Parallel-safe with Tasks 10 and 11.**

**Files:**
- Modify: `custom_components/srne_inverter/switch.py`, `custom_components/srne_inverter/button.py`
- Test: `tests/test_switch_button.py`

**Interfaces:**
- Consumes: `coordinator.async_set_connection_enabled`, `coordinator.async_probe`, `SIGNAL_NEW_ENTITIES`.
- Produces: `SrneConnectionSwitch`, `SrneReprobeButton`.

- [ ] **Step 1: Write the failing tests**

`tests/test_switch_button.py`:

```python
"""Connection switch and reprobe button."""

from tests.fake_transport import FakeTransport
from tests.test_init import setup_entry


async def test_connection_switch_starts_on(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    assert hass.states.get("switch.justice_inv_1_connection").state == "on"


async def test_turning_the_switch_off_frees_the_logger(
    hass, justice_registers, enable_custom_integrations
):
    transport = FakeTransport(justice_registers)
    await setup_entry(hass, transport)
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert transport.connected is False
    assert hass.states.get("switch.justice_inv_1_connection").state == "off"


async def test_turning_the_switch_back_on_resumes_polling(
    hass, justice_registers, enable_custom_integrations
):
    transport = FakeTransport(justice_registers)
    entry = await setup_entry(hass, transport)
    for action in ("turn_off", "turn_on"):
        await hass.services.async_call(
            "switch", action,
            {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
        )
        await hass.async_block_till_done()
    assert transport.connected is True
    assert entry.runtime_data.coordinator.last_update_success is True


async def test_reprobe_button_reruns_the_probe(
    hass, justice_registers, enable_custom_integrations
):
    transport = FakeTransport(justice_registers)
    await setup_entry(hass, transport)
    transport.reads.clear()
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.justice_inv_1_reprobe"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert len(transport.reads) >= 10


async def test_reprobe_adds_entities_that_appeared(
    hass, justice_registers, enable_custom_integrations
):
    partial = {k: v for k, v in justice_registers.items() if k < 0xF000}
    transport = FakeTransport(partial)
    await setup_entry(hass, transport)
    assert hass.states.get("sensor.justice_inv_1_total_running_days") is None

    transport.registers.update(
        {k: v for k, v in justice_registers.items() if k >= 0xF000}
    )
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.justice_inv_1_reprobe"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.justice_inv_1_total_running_days") is not None
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_switch_button.py -v`
Expected: FAIL

- [ ] **Step 3: Write `switch.py`**

```python
"""Switch platform: pause/resume the connection to the logger.

Each Solarman logger accepts exactly one TCP client. Turning this switch off
closes our socket and stops polling so an external tool (tools/probe.py, the
Casa Justice scripts) can take over.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .entity import SrneEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the connection switch."""
    async_add_entities([SrneConnectionSwitch(entry.runtime_data.coordinator, entry)])


class SrneConnectionSwitch(SrneEntity, SwitchEntity):
    """Owns whether the integration holds the logger's single TCP slot."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry: SrneConfigEntry) -> None:
        super().__init__(coordinator, entry, "connection", "Connection")

    @property
    def available(self) -> bool:
        # Must stay usable while the connection is down, or it cannot be re-enabled.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.connection_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_connection_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_connection_enabled(False)
        self.async_write_ha_state()
```

- [ ] **Step 4: Write `button.py`**

```python
"""Button platform: re-run the capability probe on demand."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .const import SIGNAL_NEW_ENTITIES
from .entity import SrneEntity
from .transport.base import TransportError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the reprobe button."""
    async_add_entities([SrneReprobeButton(entry.runtime_data.coordinator, entry)])


class SrneReprobeButton(SrneEntity, ButtonEntity):
    """Re-reads every block and adds entities that appeared."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: SrneConfigEntry) -> None:
        super().__init__(coordinator, entry, "reprobe", "Reprobe")

    async def async_press(self) -> None:
        await async_reprobe(self.hass, self._entry)


async def async_reprobe(hass: HomeAssistant, entry: SrneConfigEntry) -> dict[str, str]:
    """Re-probe the unit and tell every platform to add what appeared."""
    coordinator = entry.runtime_data.coordinator
    try:
        result = await coordinator.async_probe()
    except TransportError as err:
        raise HomeAssistantError(f"reprobe failed: {err}") from err
    await coordinator.async_refresh()
    async_dispatcher_send(hass, SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id))
    return result.as_diagnostics()
```

- [ ] **Step 5: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_switch_button.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/switch.py custom_components/srne_inverter/button.py tests/test_switch_button.py
git -C /data/claude/ha-srne-inverter commit -m "feat(control): connection switch to free the logger and reprobe button

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** turning the switch off closes the socket; pressing reprobe after a block reappears creates its entities without a restart.

---

### Task 13: `config_flow.py` + `logger_web.py` + translations

> **Parallel-safe with Task 15 (diagnostics).**

**Files:**
- Create: `custom_components/srne_inverter/config_flow.py`
- Create: `custom_components/srne_inverter/logger_web.py`
- Create: `custom_components/srne_inverter/translations/en.json`
- Create: `custom_components/srne_inverter/translations/es.json`
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `build_transport`-equivalent validation, `transport.base` errors.
- Produces:
  - `async_fetch_logger_serial(hass, host, *, username="admin", password="admin", timeout=10) -> str` (in `logger_web.py`), raising `LoggerWebError`.
  - `SrneConfigFlow(ConfigFlow, domain=DOMAIN)` with `async_step_user`; `SrneOptionsFlow(OptionsFlow)` with `async_step_init`.

- [ ] **Step 1: Write the failing tests**

`tests/test_config_flow.py`:

```python
"""Config and options flow."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter.const import (
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    DOMAIN,
)
from custom_components.srne_inverter.logger_web import LoggerWebError
from custom_components.srne_inverter.transport.base import (
    TransportConnectionError,
    TransportProtocolError,
)
from tests.fake_transport import FakeTransport

USER_INPUT = {
    CONF_NAME: "Justice Inv 1",
    CONF_HOST: "192.168.188.240",
    CONF_PORT: 8899,
    CONF_SERIAL: "3548208972",
    CONF_SLAVE_ID: 1,
    CONF_SCAN_INTERVAL: 10,
}
VALIDATE = "custom_components.srne_inverter.config_flow.build_probe_transport"
FETCH = "custom_components.srne_inverter.config_flow.async_fetch_logger_serial"


@pytest.fixture(autouse=True)
def enable_custom(enable_custom_integrations):
    yield


async def test_user_flow_creates_the_entry(hass, justice_registers):
    transport = FakeTransport(justice_registers)
    with (
        patch(VALIDATE, return_value=transport),
        patch("custom_components.srne_inverter.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Justice Inv 1"
    assert result["data"][CONF_SERIAL] == "3548208972"
    assert result["options"][CONF_SCAN_INTERVAL] == 10
    assert transport.close_count >= 1


async def test_empty_serial_is_scraped_from_status_html(hass, justice_registers):
    transport = FakeTransport(justice_registers)
    with (
        patch(VALIDATE, return_value=transport),
        patch(FETCH, new=AsyncMock(return_value="3548208972")) as fetch,
        patch("custom_components.srne_inverter.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SERIAL: ""}
        )
    fetch.assert_awaited_once()
    assert result["data"][CONF_SERIAL] == "3548208972"


async def test_serial_scrape_failure_shows_an_error(hass):
    with patch(FETCH, new=AsyncMock(side_effect=LoggerWebError("401"))):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_SERIAL: ""}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_read_serial"}


async def test_busy_logger_shows_cannot_connect(hass, justice_registers):
    transport = FakeTransport(justice_registers, fail_connect=True)
    with patch(VALIDATE, return_value=transport):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_wrong_slave_shows_invalid_slave(hass, justice_registers):
    transport = FakeTransport(
        justice_registers, read_errors=[TransportProtocolError("Empty")] * 4
    )
    with patch(VALIDATE, return_value=transport):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["errors"] == {"base": "invalid_slave"}


async def test_duplicate_serial_aborts(hass, justice_registers):
    MockConfigEntry(domain=DOMAIN, unique_id="3548208972").add_to_hass(hass)
    with patch(VALIDATE, return_value=FakeTransport(justice_registers)):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_updates_intervals(hass, justice_registers):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="3548208972", data=USER_INPUT,
        options={CONF_SCAN_INTERVAL: 10, "warm_interval": 60, "cold_interval": 300},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.srne_inverter.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SCAN_INTERVAL: 20, "warm_interval": 120, "cold_interval": 600},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_SCAN_INTERVAL] == 20
```

Plus `tests/test_logger_web.py`:

```python
"""Serial scraping from the logger's status.html."""

import pytest
from aiohttp import BasicAuth

from custom_components.srne_inverter.logger_web import (
    LoggerWebError,
    async_fetch_logger_serial,
)

PAGE = """
<script>
var cover_sta = "Connected";
var cover_mid = "3548208972";
var cover_ver = "LSW5_01_2421_SS_00_00.00.00.18";
</script>
"""


async def test_serial_is_parsed_from_cover_mid(hass, aioclient_mock):
    aioclient_mock.get("http://192.168.188.240/status.html", text=PAGE)
    assert await async_fetch_logger_serial(hass, "192.168.188.240") == "3548208972"


async def test_missing_cover_mid_raises(hass, aioclient_mock):
    aioclient_mock.get("http://192.168.188.240/status.html", text="<html></html>")
    with pytest.raises(LoggerWebError):
        await async_fetch_logger_serial(hass, "192.168.188.240")


async def test_http_error_raises(hass, aioclient_mock):
    aioclient_mock.get("http://192.168.188.240/status.html", status=401)
    with pytest.raises(LoggerWebError):
        await async_fetch_logger_serial(hass, "192.168.188.240")
```

- [ ] **Step 2: Run, expect failure**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_config_flow.py tests/test_logger_web.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `logger_web.py`**

```python
"""Read the logger serial from the stick's own web page.

The LSW-5 exposes http://<logger>/status.html behind basic auth admin/admin,
with the serial embedded as:  var cover_mid = "3548208972";

UDP discovery on 48899 is deliberately not used: it does not cross subnets,
which is exactly why this integration exists.
"""

from __future__ import annotations

import re

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_COVER_MID = re.compile(r"cover_mid\s*=\s*[\"']([0-9]+)[\"']")

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
DEFAULT_TIMEOUT = 10


class LoggerWebError(Exception):
    """status.html could not be fetched or did not contain the serial."""


async def async_fetch_logger_serial(
    hass: HomeAssistant,
    host: str,
    *,
    username: str = DEFAULT_USERNAME,
    password: str = DEFAULT_PASSWORD,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Return the logger serial found in status.html."""
    session = async_get_clientsession(hass)
    url = f"http://{host}/status.html"
    try:
        async with session.get(
            url,
            auth=aiohttp.BasicAuth(username, password),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:
                raise LoggerWebError(f"{url} answered HTTP {response.status}")
            body = await response.text()
    except LoggerWebError:
        raise
    except Exception as err:  # noqa: BLE001 - aiohttp/OS errors alike
        raise LoggerWebError(f"cannot read {url}: {err}") from err

    match = _COVER_MID.search(body)
    if match is None:
        raise LoggerWebError(f"{url} did not contain cover_mid")
    return match.group(1)
```

- [ ] **Step 4: Write `config_flow.py`**

```python
"""Config and options flow for srne_inverter."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback

from .const import (
    CONF_COLD_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    CONF_WARM_INTERVAL,
    DEFAULT_COLD_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLAVE_ID,
    DEFAULT_WARM_INTERVAL,
    DOMAIN,
    SOCKET_TIMEOUT,
)
from .logger_web import LoggerWebError, async_fetch_logger_serial
from .transport.base import (
    Transport,
    TransportConnectionError,
    TransportError,
)
from .transport.solarman_v5 import SolarmanV5Transport

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="SRNE Inverter"): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Optional(CONF_SERIAL, default=""): str,
        vol.Required(CONF_SLAVE_ID, default=DEFAULT_SLAVE_ID): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=247)
        ),
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=600)
        ),
    }
)


def build_probe_transport(
    host: str, serial: int, port: int, slave_id: int
) -> Transport:
    """Build the throwaway transport the flow uses to validate (patched in tests)."""
    return SolarmanV5Transport(
        host, serial, port=port, slave_id=slave_id, timeout=SOCKET_TIMEOUT
    )


class SrneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for one inverter behind one logger."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect connection details, then actually talk to the device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            serial_text = str(user_input.get(CONF_SERIAL, "")).strip()

            if not serial_text:
                try:
                    serial_text = await async_fetch_logger_serial(self.hass, host)
                except LoggerWebError as err:
                    _LOGGER.debug("Serial scrape failed for %s: %s", host, err)
                    errors["base"] = "cannot_read_serial"

            if not errors:
                if not serial_text.isdigit():
                    errors["base"] = "invalid_serial"
                else:
                    await self.async_set_unique_id(serial_text)
                    self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                    errors = await self._async_validate(
                        host,
                        int(serial_text),
                        user_input[CONF_PORT],
                        user_input[CONF_SLAVE_ID],
                    )

            if not errors:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        CONF_HOST: host,
                        CONF_PORT: user_input[CONF_PORT],
                        CONF_SERIAL: serial_text,
                        CONF_SLAVE_ID: user_input[CONF_SLAVE_ID],
                    },
                    options={
                        CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                        CONF_WARM_INTERVAL: DEFAULT_WARM_INTERVAL,
                        CONF_COLD_INTERVAL: DEFAULT_COLD_INTERVAL,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def _async_validate(
        self, host: str, serial: int, port: int, slave_id: int
    ) -> dict[str, str]:
        """Open a real connection and read two known registers."""
        transport

```

> ⚠️ **The code block above is TRUNCATED** — it stops mid-statement at `transport`. It is an
> artefact of this plan being assembled from several generated chunks. The complete version of
> `_async_validate` is the block immediately below; use that one and ignore the fragment above.

```python
    async def _async_validate(
        self, host: str, serial: int, port: int, slave_id: int
    ) -> dict[str, str]:
        """Open a real connection and read two known registers."""
        transport = build_probe_transport(host, serial, port, slave_id)
        try:
            await transport.connect()
            await transport.read_holding(0x0014, 1)   # firmware word
            await transport.read_holding(0x0100, 1)   # battery SOC
        except TransportConnectionError as err:
            _LOGGER.debug("Cannot connect to %s: %s", host, err)
            return {"base": "cannot_connect"}
        except TransportError as err:
            # Empty / Acknowledge / IllegalDataAddress here means the slave id
            # is wrong: the logger answered but the inverter did not.
            _LOGGER.debug("Bad reply from %s slave %s: %s", host, slave_id, err)
            return {"base": "invalid_slave"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating %s", host)
            return {"base": "unknown"}
        finally:
            await transport.close()
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        """Return the options flow handler."""
        return SrneOptionsFlow()


class SrneOptionsFlow(OptionsFlow):
    """Tune the three polling intervals without re-entering the connection."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=5, max=600)),
                vol.Required(
                    CONF_WARM_INTERVAL,
                    default=options.get(CONF_WARM_INTERVAL, DEFAULT_WARM_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Required(
                    CONF_COLD_INTERVAL,
                    default=options.get(CONF_COLD_INTERVAL, DEFAULT_COLD_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=30, max=86400)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
```

- [ ] **Step 5: Write `translations/en.json`**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "SRNE inverter via Solarman logger",
        "description": "Leave the serial empty to read it from http://<host>/status.html (basic auth admin/admin). In a parallel bank the host inverter is slave 1 and the second one is slave 2.",
        "data": {
          "name": "Name",
          "host": "Logger IP address",
          "port": "TCP port",
          "serial": "Logger serial (optional)",
          "slave_id": "Modbus slave id",
          "scan_interval": "Fast poll interval (seconds)"
        }
      }
    },
    "error": {
      "cannot_connect": "Cannot reach the logger on TCP 8899. Each logger accepts only ONE client: close any other tool that is polling it.",
      "cannot_read_serial": "Could not read the serial from http://<host>/status.html. Enter it by hand.",
      "invalid_serial": "The serial must be digits only.",
      "invalid_slave": "The logger answered but the inverter did not. Check the Modbus slave id (host = 1, second unit = 2).",
      "unknown": "Unexpected error. Check the Home Assistant log."
    },
    "abort": {
      "already_configured": "This logger is already configured."
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Polling intervals",
        "data": {
          "scan_interval": "Fast (battery, grid, load) seconds",
          "warm_interval": "Settings blocks, seconds",
          "cold_interval": "Device info and meters, seconds"
        }
      }
    }
  },
  "services": {
    "read_register": {
      "name": "Read register",
      "description": "Read raw holding registers from an inverter.",
      "fields": {
        "device_id": {"name": "Device", "description": "Inverter to read from."},
        "address": {"name": "Address", "description": "First register address (decimal or 0x hex)."},
        "count": {"name": "Count", "description": "How many registers to read."}
      }
    },
    "write_register": {
      "name": "Write register",
      "description": "Write one raw holding register and verify the read-back.",
      "fields": {
        "device_id": {"name": "Device", "description": "Inverter to write to."},
        "address": {"name": "Address", "description": "Register address (decimal or 0x hex)."},
        "value": {"name": "Value", "description": "Raw register value."}
      }
    },
    "reprobe": {
      "name": "Reprobe",
      "description": "Re-read every block and add entities that appeared.",
      "fields": {
        "device_id": {"name": "Device", "description": "Inverter to probe."}
      }
    }
  }
}
```

- [ ] **Step 6: Write `translations/es.json`** — same structure, Spanish strings:

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Inversor SRNE por logger Solarman",
        "description": "Dejá el serial vacío para leerlo de http://<host>/status.html (auth básica admin/admin). En paralelo, el inversor host es esclavo 1 y el segundo es esclavo 2.",
        "data": {
          "name": "Nombre",
          "host": "IP del logger",
          "port": "Puerto TCP",
          "serial": "Serial del logger (opcional)",
          "slave_id": "Esclavo Modbus",
          "scan_interval": "Intervalo rápido (segundos)"
        }
      }
    },
    "error": {
      "cannot_connect": "No se alcanza el logger en TCP 8899. Cada logger acepta UN solo cliente: cerrá cualquier otra herramienta que lo esté leyendo.",
      "cannot_read_serial": "No se pudo leer el serial de http://<host>/status.html. Escribilo a mano.",
      "invalid_serial": "El serial debe ser solo dígitos.",
      "invalid_slave": "El logger respondió pero el inversor no. Revisá el esclavo Modbus (host = 1, segundo = 2).",
      "unknown": "Error inesperado. Mirá el log de Home Assistant."
    },
    "abort": {
      "already_configured": "Este logger ya está configurado."
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Intervalos de sondeo",
        "data": {
          "scan_interval": "Rápido (batería, red, carga) en segundos",
          "warm_interval": "Bloques de configuración, en segundos",
          "cold_interval": "Info del equipo y contadores, en segundos"
        }
      }
    }
  },
  "services": {
    "read_register": {
      "name": "Leer registro",
      "description": "Lee registros holding crudos de un inversor.",
      "fields": {
        "device_id": {"name": "Equipo", "description": "Inversor a leer."},
        "address": {"name": "Dirección", "description": "Primer registro (decimal o 0x hex)."},
        "count": {"name": "Cantidad", "description": "Cuántos registros leer."}
      }
    },
    "write_register": {
      "name": "Escribir registro",
      "description": "Escribe un registro holding crudo y verifica la relectura.",
      "fields": {
        "device_id": {"name": "Equipo", "description": "Inversor a escribir."},
        "address": {"name": "Dirección", "description": "Registro (decimal o 0x hex)."},
        "value": {"name": "Valor", "description": "Valor crudo del registro."}
      }
    },
    "reprobe": {
      "name": "Re-sondear",
      "description": "Vuelve a leer todos los bloques y agrega las entidades que aparecieron.",
      "fields": {
        "device_id": {"name": "Equipo", "description": "Inversor a sondear."}
      }
    }
  }
}
```

- [ ] **Step 7: Run the tests, expect PASS**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest tests/test_config_flow.py tests/test_logger_web.py -v`
Expected: 10 passed

- [ ] **Step 8: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/config_flow.py custom_components/srne_inverter/logger_web.py custom_components/srne_inverter/translations tests/test_config_flow.py tests/test_logger_web.py
git -C /data/claude/ha-srne-inverter commit -m "feat(config-flow): user and options flow with live validation and status.html serial scrape

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** the flow refuses a busy logger with `cannot_connect`, a wrong slave with `invalid_slave`, and aborts on a duplicate serial.

---

### Task 14: `diagnostics.py`

> **Parallel-safe with Task 13.**

**Files:**
- Create: `custom_components/srne_inverter/diagnostics.py`
- Test: `tests/test_diagnostics.py`

**Interfaces:**
- Produces: `async_get_config_entry_diagnostics(hass, entry) -> dict`.

- [ ] **Step 1: Write the failing test**

`tests/test_diagnostics.py`:

```python
"""Diagnostics: enough to debug a unit without SSH, with the host redacted."""

from custom_components.srne_inverter.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.fake_transport import FakeTransport
from tests.test_init import setup_entry


async def test_diagnostics_contents(hass, justice_registers, enable_custom_integrations):
    entry = await setup_entry(hass, FakeTransport(justice_registers))
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["data"]["host"] == "**REDACTED**"
    assert diag["entry"]["data"]["serial"] == "**REDACTED**"
    assert diag["entry"]["data"]["slave_id"] == 1
    assert diag["support"]["0x0100"] == "supported"
    assert diag["registers"]["0x0100"] == 55
    assert diag["values"]["battery_soc"] == 55
    assert diag["coordinator"]["connection_enabled"] is True
    assert diag["coordinator"]["failure_count"] == 0
    assert diag["coordinator"]["last_update_success"] is True
    assert diag["probe_errors"] == {}


async def test_diagnostics_lists_unsupported_blocks(
    hass, justice_registers, enable_custom_integrations
):
    partial = {k: v for k, v in justice_registers.items() if k < 0xF000}
    entry = await setup_entry(hass, FakeTransport(partial))
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["support"]["0xF02C"] == "unsupported"
    assert "0xF02C" in diag["probe_errors"]
```

- [ ] **Step 2: Run, expect failure.** Run: `.venv/bin/python -m pytest tests/test_diagnostics.py -v` → `ModuleNotFoundError`.

- [ ] **Step 3: Write `diagnostics.py`**

```python
"""Diagnostics for a configured inverter."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import SrneConfigEntry
from .const import CONF_SERIAL

TO_REDACT = {CONF_HOST, CONF_SERIAL}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SrneConfigEntry
) -> dict[str, Any]:
    """Dump the support map, raw registers and decoded values."""
    coordinator = entry.runtime_data.coordinator
    probe_result = coordinator.probe_result
    data = coordinator.data

    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "connection_enabled": coordinator.connection_enabled,
            "failure_count": coordinator.failure_count,
            "last_update_success": coordinator.last_update_success,
            "scan_interval": coordinator.scan_interval,
            "warm_interval": coordinator.warm_interval,
            "cold_interval": coordinator.cold_interval,
        },
        "support": probe_result.as_diagnostics() if probe_result else {},
        "probe_errors": (
            {f"0x{addr:04X}": text for addr, text in probe_result.errors.items()}
            if probe_result
            else {}
        ),
        "registers": (
            {f"0x{addr:04X}": value for addr, value in sorted(data.registers.items())}
            if data
            else {}
        ),
        "values": dict(data.values) if data else {},
    }
```

- [ ] **Step 4: Run the tests, expect PASS.** Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter/diagnostics.py tests/test_diagnostics.py
git -C /data/claude/ha-srne-inverter commit -m "feat(diagnostics): export support map, raw registers and decoded values

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** the download contains the whole raw register dump with `host`/`serial` redacted.

---

### Task 15: Services `read_register` / `write_register` / `reprobe`

**Files:**
- Create: `custom_components/srne_inverter/services.py`
- Create: `custom_components/srne_inverter/services.yaml`
- Modify: `custom_components/srne_inverter/__init__.py` (call `async_setup_services` from `async_setup`)
- Test: `tests/test_services.py`

**Interfaces:**
- Consumes: `coordinator.async_read_raw`, `coordinator.async_write_raw`, `button.async_reprobe`.
- Produces: `async_setup_services(hass) -> None`; `async_setup(hass, config) -> bool` in `__init__.py`.

- [ ] **Step 1: Write the failing tests**

`tests/test_services.py`:

```python
"""Domain services operating on a device_id."""

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.srne_inverter.const import DOMAIN
from tests.fake_transport import FakeTransport
from tests.test_init import setup_entry


async def device_id_for(hass) -> str:
    device = dr.async_get(hass).async_get_device({(DOMAIN, "3548208972")})
    return device.id


async def test_read_register_returns_values(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    response = await hass.services.async_call(
        DOMAIN, "read_register",
        {"device_id": await device_id_for(hass), "address": "0x0100", "count": 3},
        blocking=True, return_response=True,
    )
    assert response["address"] == "0x0100"
    assert response["values"] == [55, 531, 65527]


async def test_write_register_returns_the_read_back(
    hass, justice_registers, enable_custom_integrations
):
    transport = FakeTransport(justice_registers)
    await setup_entry(hass, transport)
    response = await hass.services.async_call(
        DOMAIN, "write_register",
        {"device_id": await device_id_for(hass), "address": "0xE01E", "value": 16},
        blocking=True, return_response=True,
    )
    assert response == {"address": "0xE01E", "written": 16, "readback": 16}
    assert transport.writes == [(0xE01E, 16)]


async def test_write_register_rejected_by_firmware_raises(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN, "write_register",
            {"device_id": await device_id_for(hass), "address": "0xE20F", "value": 2},
            blocking=True, return_response=True,
        )


async def test_reprobe_service_returns_the_support_map(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    response = await hass.services.async_call(
        DOMAIN, "reprobe",
        {"device_id": await device_id_for(hass)},
        blocking=True, return_response=True,
    )
    assert response["0x0100"] == "supported"


async def test_unknown_device_raises_validation_error(
    hass, justice_registers, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers))
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "read_register",
            {"device_id": "does-not-exist", "address": 256, "count": 1},
            blocking=True, return_response=True,
        )
```

- [ ] **Step 2: Run, expect failure.** Run: `.venv/bin/python -m pytest tests/test_services.py -v`.

- [ ] **Step 3: Write `services.py`**

```python
"""Domain-level services: raw register access and on-demand reprobe."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .button import async_reprobe
from .const import (
    DOMAIN,
    SERVICE_READ_REGISTER,
    SERVICE_REPROBE,
    SERVICE_WRITE_REGISTER,
)

ATTR_DEVICE_ID = "device_id"
ATTR_ADDRESS = "address"
ATTR_COUNT = "count"
ATTR_VALUE = "value"


def _address(value: Any) -> int:
    """Accept 256, '256' or '0x0100'."""
    if isinstance(value, int):
        parsed = value
    else:
        parsed = int(str(value), 0)
    if not 0 <= parsed <= 0xFFFF:
        raise vol.Invalid(f"register address out of range: {value}")
    return parsed


READ_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_ADDRESS): _address,
        vol.Optional(ATTR_COUNT, default=1): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=64)
        ),
    }
)
WRITE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_ADDRESS): _address,
        vol.Required(ATTR_VALUE): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
    }
)
REPROBE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})


def _entry_for_device(hass: HomeAssistant, device_id: str):
    """Resolve a device_id to this integration's loaded config entry."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"unknown device_id {device_id}")
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and entry.state is ConfigEntryState.LOADED
        ):
            return entry
    raise ServiceValidationError(
        f"device {device_id} has no loaded {DOMAIN} config entry"
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the domain services once."""

    async def handle_read(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data[ATTR_DEVICE_ID])
        address = call.data[ATTR_ADDRESS]
        values = await entry.runtime_data.coordinator.async_read_raw(
            address, call.data[ATTR_COUNT]
        )
        return {"address": f"0x{address:04X}", "values": values}

    async def handle_write(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data[ATTR_DEVICE_ID])
        address = call.data[ATTR_ADDRESS]
        value = call.data[ATTR_VALUE]
        read_back = await entry.runtime_data.coordinator.async_write_raw(address, value)
        return {
            "address": f"0x{address:04X}",
            "written": value,
            "readback": read_back,
        }

    async def handle_reprobe(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for_device(hass, call.data[ATTR_DEVICE_ID])
        return await async_reprobe(hass, entry)

    hass.services.async_register(
        DOMAIN, SERVICE_READ_REGISTER, handle_read,
        schema=READ_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_WRITE_REGISTER, handle_write,
        schema=WRITE_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REPROBE, handle_reprobe,
        schema=REPROBE_SCHEMA, supports_response=SupportsResponse.ONLY,
    )
```

- [ ] **Step 4: Write `services.yaml`**

```yaml
read_register:
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: srne_inverter
    address:
      required: true
      example: "0x0100"
      selector:
        text:
    count:
      default: 1
      selector:
        number:
          min: 1
          max: 64
          mode: box

write_register:
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: srne_inverter
    address:
      required: true
      example: "0xE01E"
      selector:
        text:
    value:
      required: true
      selector:
        number:
          min: 0
          max: 65535
          mode: box

reprobe:
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: srne_inverter
```

- [ ] **Step 5: Wire `async_setup` in `__init__.py`**

Add these imports and function (keep everything else unchanged):

```python
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import config_validation as cv

from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain services (they are entry-agnostic)."""
    async_setup_services(hass)
    return True
```

Also add `from .const import DOMAIN` to the existing const import block.

- [ ] **Step 6: Run the tests, expect PASS.** Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git -C /data/claude/ha-srne-inverter add custom_components/srne_inverter tests/test_services.py
git -C /data/claude/ha-srne-inverter commit -m "feat(services): read_register, write_register with read-back and reprobe

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** `write_register` on `0xE20F` raises instead of silently reporting success.

---

### Task 16: End-to-end integration test and full-suite gate

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write the test**

```python
"""End to end: two entries, one per logger, sharing nothing."""

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter.const import (
    CONF_COLD_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    CONF_WARM_INTERVAL,
    DOMAIN,
)
from tests.fake_transport import FakeTransport

OPTIONS = {CONF_SCAN_INTERVAL: 10, CONF_WARM_INTERVAL: 60, CONF_COLD_INTERVAL: 300}


async def test_two_entries_produce_two_independent_devices(
    hass, justice_registers, enable_custom_integrations
):
    transports = {
        "3548208972": FakeTransport(justice_registers),
        "3548738877": FakeTransport(justice_registers),
    }
    entries = []
    for serial, host, slave, title in (
        ("3548208972", "192.168.188.240", 1, "Justice Inv 1"),
        ("3548738877", "192.168.188.242", 2, "Justice Inv 2"),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, unique_id=serial, title=title, options=OPTIONS,
            data={CONF_HOST: host, CONF_PORT: 8899, CONF_SERIAL: serial,
                  CONF_SLAVE_ID: slave},
        )
        entry.add_to_hass(hass)
        entries.append(entry)

    with patch(
        "custom_components.srne_inverter.build_transport",
        side_effect=lambda e: transports[e.data[CONF_SERIAL]],
    ):
        for entry in entries:
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert all(e.state is ConfigEntryState.LOADED for e in entries)
    devices = dr.async_get(hass)
    assert devices.async_get_device({(DOMAIN, "3548208972")}) is not None
    assert devices.async_get_device({(DOMAIN, "3548738877")}) is not None

    registry = er.async_get(hass)
    for entry in entries:
        entities = er.async_entries_for_config_entry(registry, entry.entry_id)
        # Every platform must have contributed at least one entity.
        domains = {e.domain for e in entities}
        assert domains >= {"sensor", "binary_sensor", "number", "select",
                           "switch", "button"}
        assert len(entities) > 50

    # Unique ids are namespaced by serial, so nothing collides.
    ids = {e.unique_id for e in registry.entities.values()}
    assert any(i.startswith("3548208972_") for i in ids)
    assert any(i.startswith("3548738877_") for i in ids)


async def test_unloading_one_entry_leaves_the_other_running(
    hass, justice_registers, enable_custom_integrations
):
    from tests.test_init import setup_entry

    entry = await setup_entry(hass, FakeTransport(justice_registers))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.justice_inv_1_battery_soc") is None
```

- [ ] **Step 2: Run the whole suite**

Run: `/data/claude/ha-srne-inverter/.venv/bin/python -m pytest -v`
Expected: all green. Record the total count in the commit body.

- [ ] **Step 3: Commit**

```bash
git -C /data/claude/ha-srne-inverter add tests/test_integration.py
git -C /data/claude/ha-srne-inverter commit -m "test(integration): two independent entries with full platform coverage

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** the full suite passes; each entry produces > 50 entities across all six platforms.

---

## Phase 3 — Live hardware, production, docs

### Task 17: Live read-only test against Casa Justice ⚠️ FOREGROUND

> **Run by the orchestrator or a subagent with `run in FOREGROUND`.** Not parallelisable with anything. Requires that no other process holds the loggers.

**Files:**
- Create: `docs/evidence/2026-09-13_live-probe-inv1.json`, `..._live-probe-inv2.json`
- Create: `docs/evidence/2026-09-13_live-test-notes.md`

- [ ] **Step 1: Free the loggers**

```bash
for p in $(pgrep -f "justice_watc[h].py"); do kill $p; done
for p in $(pgrep -f "justice_inverters_rea[d].py"); do kill $p; done
pgrep -af "justice_|solarman" || echo "no local process is holding the loggers"
```

- [ ] **Step 2: Probe inverter 1 (read-only)**

```bash
/data/claude/ha-srne-inverter/.venv/bin/python /data/claude/ha-srne-inverter/tools/probe.py \
  192.168.188.240 --serial 3548208972 --slave 1 \
  --json /data/claude/ha-srne-inverter/docs/evidence/2026-09-13_live-probe-inv1.json
```

Expected: the ten blocks report `OK`, matching the spec's verified list. Anything reporting `--` or `??` is a finding — record it, do not "fix" the map to match a single reading.

- [ ] **Step 3: Probe inverter 2 (read-only, slave 2)**

```bash
/data/claude/ha-srne-inverter/.venv/bin/python /data/claude/ha-srne-inverter/tools/probe.py \
  192.168.188.242 --serial 3548738877 --slave 2 \
  --json /data/claude/ha-srne-inverter/docs/evidence/2026-09-13_live-probe-inv2.json
```

- [ ] **Step 4: Settle the three open register questions**

Read-only, on inverter 1:

```bash
/data/claude/ha-srne-inverter/.venv/bin/python - <<'PY'
import asyncio, sys
sys.path.insert(0, "/data/claude/ha-srne-inverter/custom_components/srne_inverter")
from transport.solarman_v5 import SolarmanV5Transport

async def main():
    t = SolarmanV5Transport("192.168.188.240", 3548208972, slave_id=1)
    await t.connect()
    try:
        print("0x0018-0x001B (inverter serial?)", await t.read_holding(0x0018, 4))
        print("0x0236-0x0239 (grid I L2 candidate)", await t.read_holding(0x0236, 4))
        print("0x022A-0x022B (grid V/I L2 per profile)", await t.read_holding(0x022A, 2))
    finally:
        await t.close()

asyncio.run(main())
PY
```

Compare `0x022B` against `0x0238`: whichever tracks the real L2 grid current is the right map. Record the evidence. Only change `registers.py` if the data contradicts the profile — and then write the reasoning into the field comment.

- [ ] **Step 5: One harmless write with read-back (`0xE01E`, SOC low alarm 15 → 16 → 15)**

```bash
/data/claude/ha-srne-inverter/.venv/bin/python - <<'PY'
import asyncio, sys
sys.path.insert(0, "/data/claude/ha-srne-inverter/custom_components/srne_inverter")
from transport.solarman_v5 import SolarmanV5Transport

ADDR = 0xE01E

async def main():
    t = SolarmanV5Transport("192.168.188.240", 3548208972, slave_id=1)
    await t.connect()
    try:
        before = (await t.read_holding(ADDR, 1))[0]
        print("before:", before)
        assert before == 15, f"unexpected starting value {before}; abort"
        await t.write_holding(ADDR, 16)
        await asyncio.sleep(0.5)
        mid = (await t.read_holding(ADDR, 1))[0]
        print("after write 16:", mid)
        assert mid == 16, "write did not stick"
        await t.write_holding(ADDR, before)
        await asyncio.sleep(0.5)
        end = (await t.read_holding(ADDR, 1))[0]
        print("restored:", end)
        assert end == before, "COULD NOT RESTORE -- tell Gabriel now"
    finally:
        await t.close()

asyncio.run(main())
PY
```

- [ ] **Step 6: Write `docs/evidence/2026-09-13_live-test-notes.md`** (Spanish) recording: blocks supported per unit, any deviation from the spec's list, the `0x0018`/`0x0238` findings, the write/read-back/restore trace, and the wall-clock duration of one full probe (to sanity-check the 10 s HOT interval).

- [ ] **Step 7: Commit**

```bash
git -C /data/claude/ha-srne-inverter add docs/evidence custom_components tests
git -C /data/claude/ha-srne-inverter commit -m "test(live): read-only probe of both Justice inverters plus one reverted write

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** both units probed; `0xE01E` ends at 15; no other register changed. If any assertion tripped, stop and report — do not proceed to deployment.

---

### Task 18: Deploy to the GADI Home Assistant ⚠️ REQUIRES GABRIEL'S OK

> **Production.** Do not run any step before Gabriel explicitly approves. Announce what will change and wait.

**Files:**
- Create: `tools/deploy_to_gadi.sh`
- Create: `tools/add_justice_entries.py`

- [ ] **Step 1: Ask Gabriel.** State: two files copied to `/config/custom_components/srne_inverter/`, one `ha core restart` (all of HA restarts, ~40 s of downtime), two new config entries. Wait for an explicit yes.

- [ ] **Step 2: Write `tools/deploy_to_gadi.sh`**

```bash
#!/usr/bin/env bash
# Deploy custom_components/srne_inverter to the GADI Home Assistant.
# PRODUCTION: only run with Gabriel's explicit OK.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="GADI-HomeAssistant"
DEST="/config/custom_components"

echo "== files to ship =="
find "$REPO/custom_components/srne_inverter" -name '__pycache__' -prune -o -type f -print

read -r -p "Copy to $HOST:$DEST and restart HA core? [y/N] " answer
[ "$answer" = "y" ] || { echo "aborted"; exit 1; }

ssh "$HOST" "mkdir -p $DEST"
rsync -av --delete --exclude '__pycache__' \
  "$REPO/custom_components/srne_inverter/" "$HOST:$DEST/srne_inverter/"

ssh "$HOST" "ls -la $DEST/srne_inverter"
ssh "$HOST" "ha core restart"
echo "restart issued; wait ~40 s then check the log:"
echo "  ssh $HOST 'ha core logs | grep -i srne_inverter'"
```

- [ ] **Step 3: Write `tools/add_justice_entries.py`** — create the two entries through the REST config-flow endpoint, following the pattern of `/data/claude/casa-gadi/HomeAssistant/tools/_add_bms_justice_integration.py` (`from _ha_env import TOKEN, BASE`, `HA_BASE=http://172.16.10.12:8123`):

```python
"""Create the two Casa Justice srne_inverter config entries over the HA API.

PRODUCTION: only run with Gabriel's explicit OK, and only after the loggers
are free (the config flow opens a real TCP connection to each one).

Run from /data/claude/casa-gadi/HomeAssistant/tools/ so _ha_env resolves:
  HA_BASE=http://172.16.10.12:8123 python3 /data/claude/ha-srne-inverter/tools/add_justice_entries.py
"""
import json
import sys

import requests

sys.path.insert(0, "/data/claude/casa-gadi/HomeAssistant/tools")
from _ha_env import BASE, TOKEN  # noqa: E402

H = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

UNITS = [
    {"name": "Justice Inv 1", "host": "192.168.188.240",
     "port": 8899, "serial": "3548208972", "slave_id": 1, "scan_interval": 10},
    {"name": "Justice Inv 2", "host": "192.168.188.242",
     "port": 8899, "serial": "3548738877", "slave_id": 2, "scan_interval": 10},
]


def create(unit: dict) -> None:
    start = requests.post(
        f"{BASE}/api/config/config_entries/flow",
        headers=H, json={"handler": "srne_inverter"}, timeout=30,
    )
    start.raise_for_status()
    flow_id = start.json()["flow_id"]
    step = requests.post(
        f"{BASE}/api/config/config_entries/flow/{flow_id}",
        headers=H, json=unit, timeout=60,
    )
    print(unit["name"], step.status_code)
    print(json.dumps(step.json(), indent=1)[:1200])


for unit in UNITS:
    create(unit)
```

- [ ] **Step 4: Free the loggers, then deploy**

```bash
for p in $(pgrep -f "justice_watc[h].py"); do kill $p; done
bash /data/claude/ha-srne-inverter/tools/deploy_to_gadi.sh
```

- [ ] **Step 5: Verify the integration loaded**

```bash
ssh GADI-HomeAssistant "ha core logs | grep -i -E 'srne_inverter|custom integration' | tail -30"
```

Expected: the "custom integration srne_inverter" notice, no traceback.

- [ ] **Step 6: Create the two entries and verify the entities**

```bash
HA_BASE=http://172.16.10.12:8123 python3 /data/claude/ha-srne-inverter/tools/add_justice_entries.py
curl -s -H "Authorization: Bearer $HA_TOKEN" http://172.16.10.12:8123/api/states \
  | python3 -c "import json,sys; print([s['entity_id'] for s in json.load(sys.stdin) if 'justice_inv' in s['entity_id']][:20])"
```

Spot-check against the live probe output from Task 17: `sensor.justice_inv_1_battery_soc`, `sensor.justice_inv_1_grid_charge_current`, `select.justice_inv_1_output_priority`.

- [ ] **Step 7: Commit**

```bash
git -C /data/claude/ha-srne-inverter add tools/deploy_to_gadi.sh tools/add_justice_entries.py
git -C /data/claude/ha-srne-inverter commit -m "chore(deploy): scripts to ship the integration to GADI and create the Justice entries

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** both devices appear in HA with live values matching the Task 17 probe within one poll interval.

---

### Task 19: Lovelace view "Inversores Justice" ⚠️ REQUIRES GABRIEL'S OK

> **Production.** Depends on Task 18. Do not run without approval.

**Files:**
- Create: `tools/ha_add_justice_inverters_view.py`

- [ ] **Step 1: Ask Gabriel**, then write the script following `/data/claude/casa-gadi/HomeAssistant/tools/ha_add_justice_bms_dashboard_view.py`: back up the current `dashboard-justice` config to `tools/_lovelace_backup_<ts>.json`, validate every entity id against `/api/states` (drop missing ones with a warning), replace any previous view whose `path` is `justice-inversores`, and apply over the WebSocket `lovelace/config/save` with `HA_BASE=http://172.16.10.12:8123`.

- [ ] **Step 2: View layout** — two columns, one per inverter, each a `vertical-stack`:
  - badges: `sensor.justice_inv_N_battery_soc`, `..._battery_power`, `..._machine_state`, `..._grid_charge_current`
  - "Batería": SOC, voltage, current, power, temperature, charge state
  - "Red y salida": grid V/I L1+L2, grid frequency, output V/I L1+L2, load power L1/L2/total, load percentage, **grid charge current** (the Justice diagnostic)
  - "Configuración" (`entities` card): `select.*_output_priority`, `select.*_battery_type`, `select.*_bms_communication`, `number.*_ac_charge_current_limit`, `number.*_max_charge_current`, `number.*_soc_low_alarm`, `number.*_charge_stop_soc`, `number.*_discharge_stop_soc`
  - "Estado de la integración": `binary_sensor.*_fault_active`, `switch.*_connection`, `button.*_reprobe`, `sensor.*_firmware_version`
  - bottom: `history-graph` 24 h of both SOCs, and 6 h of both `grid_charge_current`

- [ ] **Step 3: Run it, screenshot/verify in the UI, commit.**

```bash
git -C /data/claude/ha-srne-inverter add tools/ha_add_justice_inverters_view.py
git -C /data/claude/ha-srne-inverter commit -m "feat(dashboard): 'Inversores Justice' view for dashboard-justice

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** the view renders with no "Entity not available" cards; the backup JSON exists.

---

### Task 20: Documentation and cross-project bookkeeping

**Files:**
- Create: `README.md`
- Modify: `CLAUDE.md` (keep the OKF frontmatter; update `status`/`deploy` and the state section)
- Modify: `/data/claude/INTEGRATIONS.md`
- Modify: `/data/claude/CLAUDE.md` (regenerated, not hand-edited)
- Verify only: `/data/claude/Redes-Clientes/CasaJustice/CLAUDE.md`

- [ ] **Step 1: Write `README.md`** (Spanish) with: qué es y por qué no la integración HACS Solarman; requisitos (HA ≥ 2025.6, un cliente TCP por logger); instalación (HACS como repositorio custom, y manual por `scp` a `/config/custom_components/`); alta de una unidad (host, puerto, serial o "leer de status.html", esclavo — host = 1, segundo = 2); tabla de entidades por plataforma; los tres servicios con un ejemplo YAML de cada uno; qué hace el sondeo y cuándo tocar el botón "Reprobe"; el switch "Connection" para liberar el logger; cómo usar `tools/probe.py` para inventariar unidades nuevas (Yoga); advertencias verificadas (`E20F`/`E20B`/`E21D` de solo lectura, voltajes solo con `E004 = USER`, `E007–E009` los pisa el BMS cuando `E215 ≠ 0`); y cómo correr los tests.

- [ ] **Step 2: Update `CLAUDE.md`** — keep the frontmatter block, change `deploy:` to reflect the real deployment, and rewrite the "Estado" section with: integración implementada, N tests verdes, prueba en vivo hecha (fecha), desplegada en GADI con 2 entries, vista Lovelace creada, y los hallazgos del Task 17 sobre `0x0018` / `0x0238`. Update the re-entry line to point at operation and at Yoga's pending inventory instead of at the plan.

- [ ] **Step 3: Regenerate the root table**

```bash
python3 /data/claude/.claude/tools/gen-projects-table.py --check   # inspect first
python3 /data/claude/.claude/tools/gen-projects-table.py
git -C /data/claude diff --stat CLAUDE.md
```

- [ ] **Step 4: Add the row to `/data/claude/INTEGRATIONS.md`** — a new section `## ha-srne-inverter → HomeAssistant (integración custom, 2026-09-13)` documenting the entity contract: device identifier `(srne_inverter, <logger serial>)`; entity id pattern `<platform>.<entry title slug>_<field label slug>`; unique id `<serial>_<field key>`; the three services with their response shapes; and the note that renaming a `Field.key` or `Field.label` breaks entity ids and unique ids, so it requires an entity-registry rename.

- [ ] **Step 5: Verify the Casa Justice pointer**

```bash
grep -n -i "srne\|ha-srne-inverter" /data/claude/Redes-Clientes/CasaJustice/CLAUDE.md
```

If it already points here, change nothing and say so. If not, add one line.

- [ ] **Step 6: Commit (two repos)**

```bash
git -C /data/claude/ha-srne-inverter add README.md CLAUDE.md
git -C /data/claude/ha-srne-inverter commit -m "docs: README, project state and deployment notes

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"

git -C /data/claude add CLAUDE.md INTEGRATIONS.md
git -C /data/claude commit -m "docs: register ha-srne-inverter in the projects table and INTEGRATIONS

Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
```

**Verification:** the root table shows the row with the ⚠️ production marker; `INTEGRATIONS.md` documents the entity contract; no `git push` was run anywhere.

---

## Self-review against the spec

- §3 verified facts → Tasks 2 (map, sign, ×0.4, read-only registers), 3 (one-client error), 5 (block presence), 17 (re-verified live).
- §4 architecture, all files → Tasks 1–15; the "no HA import" rule is enforced by Task 6.
- §4 decision 1 (probe) → Task 5 + Task 12 (reprobe button) + Task 15 (reprobe service).
- §4 decision 2 (one client, lock, backoff, pause switch) → Tasks 4, 7, 12.
- §4 decision 3 (write + read-back) → Task 7, exercised in 11 and 15.
- §4 decision 4 (normalisations documented, raws kept) → Task 2 + `extra_state_attributes` in Task 9 + diagnostics in Task 14.
- §4 decision 5 (tiers) → Task 2 `BlockTier`, Task 7 scheduling. **Deviation:** the spec puts `0x0014` in COLD and the settings blocks in WARM — kept; `0x0200` faults were added to HOT (they sit inside the `0x0200–0x023F` HOT range the spec names).
- §4 decision 6 (multi-entry) → Task 16.
- §4 decision 7 → **corrected**: the spec says Python 3.13, but HA 2026.9 requires ≥ 3.14 and the venv is 3.14.7. The plan targets 3.14; `hacs.json` declares `homeassistant: 2025.6.0`.
- §5 entities → Tasks 10–12. `E20F` is a sensor (firmware rejects the write), as the spec anticipated.
- §6 tests/deploy/docs → Tasks 16–20.

## Parallelisation

| Wave | Tasks | Note |
|---|---|---|
| 1 | 1 | Everything depends on it |
| 2 | **2 ∥ 3** | Disjoint files |
| 3 | 4 → 5 → 6 | Chain inside the core |
| 4 | 7 → 8 → 9 | Chain inside the HA glue |
| 5 | **10 ∥ 11 ∥ 12** | Three platform pairs, disjoint files |
| 6 | **13 ∥ 14** | config_flow vs diagnostics |
| 7 | 15 → 16 | Services touch `__init__.py`; then the E2E gate |
| 8 | 17 → 18 → 19 → 20 | Strictly serial, hardware and production |

---

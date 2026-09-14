"""Shared fixtures: recorded Justice registers, and a block-complete variant
padded with clearly-marked synthetic filler.

There is no shared FakeTransport fixture -- tests construct their own with
whatever `unsupported=`/`connect_error=`/`read_errors=`/`write_errors=`/
`no_stick_writes=` combination they need (see tests/fake_transport.py); a
single shared instance would fit almost none of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.srne_inverter.registers import BLOCKS

FIXTURES = Path(__file__).parent / "fixtures"

# Live-hardware evidence from the 2026-09-14 Task 17 run against both real
# Casa Justice units (docs/evidence/*.json) -- separate from tests/fixtures/
# (Tasks 1-16's captures): these are newer, narrower captures made to settle
# specific open register questions, not full-suite golden data. Never edited
# to plug a test hole, same discipline as tests/fixtures/.
EVIDENCE = Path(__file__).parent.parent / "docs" / "evidence"

# Obviously-fake filler for load_justice_registers_synthetic_complete(). Not a
# plausible raw register value for any real field here -- chosen so a
# synthetic address is recognizable at a glance if it ever leaks into a log
# or a decoded value.
SYNTHETIC_FILL = 0xF00D


def load_justice_registers() -> dict[int, int]:
    """Flatten the recorded Justice captures into {address: raw_value}.

    Two sources, applied in order (later wins):
      1. justice_inv1_blocks.json  -- {ip: {ip, sn, blocks: {name: {addr, regs}}}}
      2. justice_inv1_settings.json -- {"before": {"E000": value, ...}} (newer)

    justice_inv1_blocks.json is a single-unit capture (Casa Justice inverter 1
    only). If it ever grows a second unit's capture, merging every unit's blocks
    into one flat map would silently mix two devices' registers together; raise
    instead of guessing which unit is "inverter 1".
    """
    regs: dict[int, int] = {}

    blocks_doc = json.loads((FIXTURES / "justice_inv1_blocks.json").read_text())
    if len(blocks_doc) != 1:
        raise ValueError(
            "justice_inv1_blocks.json must contain exactly one unit, found "
            f"{len(blocks_doc)}: {sorted(blocks_doc)}"
        )
    (unit,) = blocks_doc.values()
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


def load_justice_registers_synthetic_complete() -> dict[int, int]:
    """Recorded Justice registers, padded with SYNTHETIC filler so every
    address inside every block `registers.BLOCKS` declares PRESENT is
    readable.

    =====================================================================
    WARNING -- SYNTHETIC DATA: every value this function adds beyond what
    load_justice_registers() already returns is FABRICATED (the constant
    SYNTHETIC_FILL, 0xF00D) for test-coverage purposes only. It is NOT a
    recorded, verified or "real" value from any device. Never cite a value
    that came from here as device evidence in a report, spec, commit
    message or comment -- the whole point of this function's name is to
    make that mistake hard to make by accident.
    =====================================================================

    Why this exists: our capture of Casa Justice inverter 1 has real holes
    inside blocks the design spec verifies are PRESENT on this firmware
    (e.g. most of "faults", the tail of "inverter_b", all of
    "control_high", most of "meter", part of "device_info") -- gaps in what
    we happened to record, not evidence the device lacks those registers
    (see tests/fake_transport.py's module docstring). Feeding
    load_justice_registers() straight into FakeTransport therefore makes 5
    of the 10 declared-present BLOCKS raise on a full-block read, which
    would wrongly look like a probe result to a test not paying close
    attention. This function exists so Task 5's probe/coordinator tests can
    exercise every declared block without tripping over that gap, without
    ever touching tests/fixtures/justice_inv1_blocks.json or
    justice_inv1_settings.json (recorded evidence from a client's
    production inverter -- never edited to plug test holes).
    """
    complete = load_justice_registers()
    for block in BLOCKS:
        for address in block.addresses:
            complete.setdefault(address, SYNTHETIC_FILL)
    return complete


@pytest.fixture(name="justice_registers_synthetic_complete")
def justice_registers_synthetic_complete_fixture() -> dict[int, int]:
    """Recorded Justice registers, synthetically completed -- see
    load_justice_registers_synthetic_complete()'s docstring. Some of the
    values this returns are FAKE; never treat them as device evidence.
    "synthetic" stays in the fixture name on purpose (fix round 2, Finding
    6): a caller wiring this into a test should see what it is from the
    name alone, without having to go read the loader's docstring."""
    return load_justice_registers_synthetic_complete()


def load_justice_settings_after_bms_off() -> dict[int, int]:
    """Flatten justice_inv1_settings.json's `after_bms_off` map into
    {address: raw_value}.

    A second REAL capture of the same settings registers as
    load_justice_registers()'s `before` map, taken moments later after the
    BMS was disconnected. While a live BMS is connected it pins E007-E009 to
    the same fixed value (144 in `before`), which is why those three registers
    are indistinguishable there; `after_bms_off` is the one capture in this
    repo where they are not all equal (E007=142, E008=142, E009=140), which is
    exactly why test_registers.py uses it to guard against a float_voltage/
    boost_voltage/equalize_voltage address swap. Never edited to plug test
    holes -- read as-is from the recorded file.
    """
    settings_doc = json.loads((FIXTURES / "justice_inv1_settings.json").read_text())
    return {
        int(key, 16): value for key, value in settings_doc["after_bms_off"].items()
    }


@pytest.fixture(name="justice_settings_after_bms_off")
def justice_settings_after_bms_off_fixture() -> dict[int, int]:
    """Recorded Justice inverter 1 settings registers, captured after the BMS
    was disconnected -- see load_justice_settings_after_bms_off()'s docstring."""
    return load_justice_settings_after_bms_off()


def _load_probe_registers(filename: str) -> dict[int, int]:
    """Flatten a tools/probe.py --json capture's `registers` map ({"0xNNNN":
    raw_value, ...}) into {address: raw_value}. Used for the 2026-09-14
    Task 17 live probes of both Casa Justice units (docs/evidence/
    2026-09-14_probe-inv1.json / _probe-inv2.json), each a full 10/10-block
    snapshot -- unlike tests/fixtures/justice_inv1_blocks.json, these cover
    every address inside registers.BLOCKS with no internal gaps.
    """
    doc = json.loads((EVIDENCE / filename).read_text())
    return {int(key, 16): value for key, value in doc["registers"].items()}


@pytest.fixture(name="probe_inv1_registers")
def probe_inv1_registers_fixture() -> dict[int, int]:
    """Live 2026-09-14 full-probe capture of Casa Justice inverter 1
    (192.168.188.240, serial 3548208972, slave 1) -- see
    docs/evidence/2026-09-14_probe-inv1.json."""
    return _load_probe_registers("2026-09-14_probe-inv1.json")


@pytest.fixture(name="probe_inv2_registers")
def probe_inv2_registers_fixture() -> dict[int, int]:
    """Live 2026-09-14 full-probe capture of Casa Justice inverter 2
    (192.168.188.242, serial 3548738877, slave 2) -- see
    docs/evidence/2026-09-14_probe-inv2.json."""
    return _load_probe_registers("2026-09-14_probe-inv2.json")


def load_justice_equalize_distinct() -> dict[int, int]:
    """Flatten docs/evidence/2026-09-14_inv1-equalize-distinct.json into
    {address: raw_value} for E007/E008/E009 only.

    The first (and only) real capture in this repo where equalize_voltage
    (E007), boost_voltage (E008) and float_voltage (E009) read three
    DIFFERENT raw values (142/144/144) -- obtained live at Casa Justice
    inverter 1 by briefly disabling BMS communication (E215 -> 0, the gate
    that otherwise pins all three to one BMS-dictated value) and writing
    E007 to a value distinct from E008/E009, then restoring both E007 and
    E215 immediately afterward (progress.md's TASK 17 write-phase TRIAL D;
    all 48 E000-E02F registers confirmed unchanged after restore). Every
    earlier capture in this repo (`before`/`after_bms_off` in
    justice_inv1_settings.json) has E007 and E008 reading the SAME raw
    value, which is exactly why this evidence exists: it is the only data
    that can catch an E007/E008 (equalize/boost) address swap. Never edited
    to plug a test hole -- read as-is from the recorded file.
    """
    doc = json.loads((EVIDENCE / "2026-09-14_inv1-equalize-distinct.json").read_text())
    return {
        0xE007: doc["E007_equalize"],
        0xE008: doc["E008_boost"],
        0xE009: doc["E009_float"],
    }


@pytest.fixture(name="justice_equalize_distinct")
def justice_equalize_distinct_fixture() -> dict[int, int]:
    """E007/E008/E009 from the one real capture where they differ -- see
    load_justice_equalize_distinct()'s docstring."""
    return load_justice_equalize_distinct()

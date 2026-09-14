"""Shared fixtures: recorded Client registers, and a block-complete variant
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

# Obviously-fake filler for load_client_registers_synthetic_complete(). Not a
# plausible raw register value for any real field here -- chosen so a
# synthetic address is recognizable at a glance if it ever leaks into a log
# or a decoded value.
SYNTHETIC_FILL = 0xF00D


def load_client_registers() -> dict[int, int]:
    """Flatten the recorded Client captures into {address: raw_value}.

    Two sources, applied in order (later wins):
      1. client_inv1_blocks.json  -- {ip: {ip, sn, blocks: {name: {addr, regs}}}}
      2. client_inv1_settings.json -- {"before": {"E000": value, ...}} (newer)

    client_inv1_blocks.json is a single-unit capture (Client A site inverter 1
    only). If it ever grows a second unit's capture, merging every unit's blocks
    into one flat map would silently mix two devices' registers together; raise
    instead of guessing which unit is "inverter 1".
    """
    regs: dict[int, int] = {}

    blocks_doc = json.loads((FIXTURES / "client_inv1_blocks.json").read_text())
    if len(blocks_doc) != 1:
        raise ValueError(
            "client_inv1_blocks.json must contain exactly one unit, found "
            f"{len(blocks_doc)}: {sorted(blocks_doc)}"
        )
    (unit,) = blocks_doc.values()
    for block in unit["blocks"].values():
        for index, value in enumerate(block["regs"]):
            regs[block["addr"] + index] = value

    settings_doc = json.loads((FIXTURES / "client_inv1_settings.json").read_text())
    for key, value in settings_doc["before"].items():
        regs[int(key, 16)] = value

    return regs


@pytest.fixture(name="client_registers")
def client_registers_fixture() -> dict[int, int]:
    """Recorded raw registers of Client A site inverter 1."""
    return load_client_registers()


def load_client_registers_synthetic_complete() -> dict[int, int]:
    """Recorded Client registers, padded with SYNTHETIC filler so every
    address inside every block `registers.BLOCKS` declares PRESENT is
    readable.

    =====================================================================
    WARNING -- SYNTHETIC DATA: every value this function adds beyond what
    load_client_registers() already returns is FABRICATED (the constant
    SYNTHETIC_FILL, 0xF00D) for test-coverage purposes only. It is NOT a
    recorded, verified or "real" value from any device. Never cite a value
    that came from here as device evidence in a report, spec, commit
    message or comment -- the whole point of this function's name is to
    make that mistake hard to make by accident.
    =====================================================================

    Why this exists: our capture of Client A site inverter 1 has real holes
    inside blocks the design spec verifies are PRESENT on this firmware
    (e.g. most of "faults", the tail of "inverter_b", all of
    "control_high", most of "meter", part of "device_info") -- gaps in what
    we happened to record, not evidence the device lacks those registers
    (see tests/fake_transport.py's module docstring). Feeding
    load_client_registers() straight into FakeTransport therefore makes 5
    of the 10 declared-present BLOCKS raise on a full-block read, which
    would wrongly look like a probe result to a test not paying close
    attention. This function exists so Task 5's probe/coordinator tests can
    exercise every declared block without tripping over that gap, without
    ever touching tests/fixtures/client_inv1_blocks.json or
    client_inv1_settings.json (recorded evidence from a client's
    production inverter -- never edited to plug test holes).
    """
    complete = load_client_registers()
    for block in BLOCKS:
        for address in block.addresses:
            complete.setdefault(address, SYNTHETIC_FILL)
    return complete


@pytest.fixture(name="client_registers_complete")
def client_registers_complete_fixture() -> dict[int, int]:
    """Recorded Client registers, synthetically completed -- see
    load_client_registers_synthetic_complete()'s docstring. Some of the
    values this returns are FAKE; never treat them as device evidence."""
    return load_client_registers_synthetic_complete()


def load_client_settings_after_bms_off() -> dict[int, int]:
    """Flatten client_inv1_settings.json's `after_bms_off` map into
    {address: raw_value}.

    A second REAL capture of the same settings registers as
    load_client_registers()'s `before` map, taken moments later after the
    BMS was disconnected. While a live BMS is connected it pins E007-E009 to
    the same fixed value (144 in `before`), which is why those three registers
    are indistinguishable there; `after_bms_off` is the one capture in this
    repo where they are not all equal (E007=142, E008=142, E009=140), which is
    exactly why test_registers.py uses it to guard against a float_voltage/
    boost_voltage/equalize_voltage address swap. Never edited to plug test
    holes -- read as-is from the recorded file.
    """
    settings_doc = json.loads((FIXTURES / "client_inv1_settings.json").read_text())
    return {
        int(key, 16): value for key, value in settings_doc["after_bms_off"].items()
    }


@pytest.fixture(name="client_settings_after_bms_off")
def client_settings_after_bms_off_fixture() -> dict[int, int]:
    """Recorded Client inverter 1 settings registers, captured after the BMS
    was disconnected -- see load_client_settings_after_bms_off()'s docstring."""
    return load_client_settings_after_bms_off()

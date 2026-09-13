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

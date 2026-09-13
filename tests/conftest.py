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

"""Shared fixtures: recorded Client registers and the fake transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_client_registers() -> dict[int, int]:
    """Flatten the recorded Client captures into {address: raw_value}.

    Two sources, applied in order (later wins):
      1. client_inv1_blocks.json  -- {ip: {ip, sn, blocks: {name: {addr, regs}}}}
      2. client_inv1_settings.json -- {"before": {"E000": value, ...}} (newer)
    """
    regs: dict[int, int] = {}

    blocks_doc = json.loads((FIXTURES / "client_inv1_blocks.json").read_text())
    for unit in blocks_doc.values():
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

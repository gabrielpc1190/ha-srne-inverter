"""Functional tests for tools/probe.py's pure formatting logic.

`run()` itself is not exercised here -- it opens a real transport, and no
unit test may touch a socket (see tests/test_core_is_ha_free.py for the
HA-free guard and the repo-wide no-network-in-tests policy; the harness
ALLOWS 127.0.0.1, which is exactly why a localhost-aimed test would open a
REAL socket silently instead of failing loudly). `render_report()` has no
such problem: it is a pure function of (args, ProbeResult) with no I/O beyond
stdout/stderr and an optional --json file, so it is tested directly here
against hand-built ProbeResults.

Task-6 review, Minor 12: this file did not exist before fix round 1 -- the
block report, the "N/M blocks supported" line and the --json shape were
only ever exercised on live hardware, which is precisely why nothing kept
that output shape from silently rotting.

Loading tools/probe.py: `importlib.util.spec_from_file_location` +
`exec_module`, NOT `from custom_components.srne_inverter.probe import
BlockSupport` -- tools/probe.py imports `registers`/`probe`/`transport.*` as
BARE top-level modules (a different set of module objects than the dotted
`custom_components.srne_inverter.*` ones this same pytest session may
already have loaded from other test files). `BlockSupport` is a StrEnum:
`bare_probe.BlockSupport.SUPPORTED == "supported"` would still be True
either way (str equality), but render_report()'s own `is not
BlockSupport.SUPPORTED` check is an IDENTITY comparison against whichever
`BlockSupport` class tools/probe.py itself imported -- building a
ProbeResult with the WRONG (dotted) enum class's members would make every
block compare unequal by identity and look "missing" even when marked
SUPPORTED. Sourcing `ProbeResult`/`BlockSupport`/`BLOCKS` from the loaded
`probe_cli` module's own attributes (`probe_cli.ProbeResult`,
`probe_cli.BlockSupport`, `probe_cli.R.BLOCKS`) sidesteps this entirely by
construction, rather than by remembering not to mix the two.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_probe_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_probe_cli_under_test", REPO / "tools" / "probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(name="probe_cli", scope="module")
def probe_cli_fixture() -> ModuleType:
    return _load_probe_cli()


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = dict(
        host="192.168.188.240", serial=3548208972, slave=1,
        port=8899, timeout=10.0, json=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _fully_supported_result(probe_cli: ModuleType, registers: dict[int, int]):
    """A ProbeResult with every declared block SUPPORTED, built from the
    CLI's own bare-imported BlockSupport/BLOCKS -- see the module docstring
    for why that sourcing matters."""
    result = probe_cli.ProbeResult()
    for block in probe_cli.R.BLOCKS:
        result.support[block.addr] = probe_cli.BlockSupport.SUPPORTED
        for offset in range(block.count):
            result.registers[block.addr + offset] = registers[block.addr + offset]
    return result


def test_render_report_marks_unsupported_block_and_lists_it_missing(
    probe_cli, justice_registers_synthetic_complete, capsys
):
    result = _fully_supported_result(probe_cli, justice_registers_synthetic_complete)
    control_high = next(b for b in probe_cli.R.BLOCKS if b.name == "control_high")
    result.support[control_high.addr] = probe_cli.BlockSupport.UNSUPPORTED
    result.errors[control_high.addr] = "illegal data address"
    for offset in range(control_high.count):
        result.registers.pop(control_high.addr + offset, None)

    missing = probe_cli.render_report(_args(), result)

    assert missing == ["control_high"]
    out = capsys.readouterr().out
    assert "9/10 blocks supported; missing: control_high" in out
    assert "-- " in out
    assert "illegal data address" in out
    assert "battery_soc" in out  # decoded values section is populated


def test_render_report_writes_json_shape(
    probe_cli, justice_registers_synthetic_complete, tmp_path
):
    result = _fully_supported_result(probe_cli, justice_registers_synthetic_complete)
    out_path = tmp_path / "report.json"

    missing = probe_cli.render_report(_args(json=str(out_path)), result)

    assert missing == []
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["host"] == "192.168.188.240"
    assert written["serial"] == 3548208972
    assert written["support"]["0x0100"] == "supported"
    assert "battery_soc" in written["values"]
    assert isinstance(written["registers"]["0x0100"], int)


def test_render_report_json_write_failure_is_a_warning_not_a_crash(
    probe_cli, justice_registers_synthetic_complete, capsys
):
    """Task-6 review, Minor 9: a --json path whose directory does not exist
    used to raise a raw FileNotFoundError, exiting 1 -- the same code as the
    meaningful "some blocks missing". render_report() must not raise, must
    still return the probe's own missing-blocks verdict, and must warn on
    stderr instead."""
    result = _fully_supported_result(probe_cli, justice_registers_synthetic_complete)

    missing = probe_cli.render_report(
        _args(json="/nonexistent-dir/x.json"), result
    )

    assert missing == []
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "/nonexistent-dir/x.json" in err


def test_render_report_tolerates_a_partial_result(probe_cli):
    """A ProbeResult from a failed probe (ProbeFailedError.result) may be
    partial -- not every block was necessarily reached before the probe
    gave up. render_report() must not KeyError on a block with no entry in
    .support at all (as opposed to one entry classifying it UNSUPPORTED/
    UNKNOWN)."""
    result = probe_cli.ProbeResult()
    first = probe_cli.R.BLOCKS[0]
    result.support[first.addr] = probe_cli.BlockSupport.UNKNOWN
    result.errors[first.addr] = "established session was lost or taken"
    # Every other declared block: never reached, no entry at all.

    missing = probe_cli.render_report(_args(), result)

    assert missing == [b.name for b in probe_cli.R.BLOCKS]
    assert missing[0] == first.name

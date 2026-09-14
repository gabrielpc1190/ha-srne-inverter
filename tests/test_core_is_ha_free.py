"""The core modules must be importable on a host without Home Assistant.

A grep for the string "homeassistant" is NOT a real check here: registers.py's
own mandated docstring contains the sentence "THIS MODULE MUST NOT IMPORT
homeassistant", so `grep -c homeassistant registers.py` returns 1 on a module
that imports nothing of the sort -- the grep measures the docstring, not the
import graph. What actually matters is whether importing these modules, as
tools/probe.py imports them (bare top-level modules, via sys.path, never
executing custom_components/srne_inverter/__init__.py), ever pulls
`homeassistant` into sys.modules. The first test below runs that exact import
in a FRESH subprocess and inspects sys.modules afterward -- a subprocess
because `homeassistant` may already be sitting in the current process's
sys.modules from unrelated test collection (pytest-homeassistant-custom-
component, other test modules in this same suite), which would make an
in-process check pass or fail for the wrong reason.
"""

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

"""The core modules -- and the CLI that is built on them -- must be
importable, and RUNNABLE, on a host without Home Assistant.

A grep for the string "homeassistant" is NOT a real check here: registers.py's
own mandated docstring contains the sentence "THIS MODULE MUST NOT IMPORT
homeassistant", so `grep -c homeassistant registers.py` returns 1 on a module
that imports nothing of the sort -- the grep measures the docstring, not the
import graph.

Three tests, each catching something the others structurally cannot:

1. `test_core_modules_import_without_homeassistant` -- imports the four core
   modules as bare top-level modules (as tools/probe.py imports them) in a
   FRESH subprocess and inspects sys.modules afterward. Subprocess, because
   `homeassistant` is almost certainly already in the PARENT pytest
   process's sys.modules by the time this test runs (this same suite collects
   pytest-homeassistant-custom-component fixtures elsewhere) -- checking
   in-process would just always read "yes", regardless of what these four
   imports actually pulled in.

2. `test_cli_help_never_imports_homeassistant_anywhere` -- runs tools/probe.py
   `--help` as the real script entry point (via `runpy.run_path`, so
   `if __name__ == "__main__":` fires exactly as it would from a shell),
   with a `sys.meta_path` finder inserted FIRST that raises ImportError on
   any `homeassistant*` import, anywhere in the process, by anything.
   Fix round 1 (2026-09-13, task-6 review, Critical 3): the ORIGINAL version
   of this test only checked `returncode == 0` and `"--slave" in stdout` --
   since `homeassistant` IS installed in this venv, that version stayed
   green through an `import homeassistant` planted directly in
   tools/probe.py itself, through a new core module imported only by the
   CLI, and through the CLI being "simplified" to import
   `custom_components.srne_inverter.probe` (which executes that package's
   `__init__.py`) while a later task fills that `__init__.py` with a real HA
   import -- i.e. it had ZERO power to catch the exact regression this
   guard exists for. All three evasions were reproduced against the actual
   `git HEAD` of this repo (see task-6-report.md's "Fix round 1" section for
   the transcripts) and this version catches all three, by construction --
   it does not enumerate known-bad shapes, it makes every `homeassistant*`
   import raise, so any path that reaches one fails the same way.

3. `test_no_homeassistant_import_at_any_nesting` -- a STATIC complement,
   because 1 and 2 only see imports that actually EXECUTE during a plain
   import / a `--help` run. A lazy `import homeassistant` inside a function
   body (e.g. registers.decode(), called on every successful probe --
   reproduced: this would let the CLI connect, hold the logger's one slot
   through an entire probe, and only then crash, on a client's site, with no
   HA to fall back on) never runs under 1 or 2, since neither ever CALLS
   decode(). A `if TYPE_CHECKING: from homeassistant.core import
   HomeAssistant` is likewise invisible at runtime (harmless today, but the
   usual first step toward a real one). `ast.walk()` finds Import/ImportFrom
   nodes at ANY nesting depth -- function bodies, conditionals, whatever --
   independent of whether that code path is ever executed, which is exactly
   what 1 and 2 cannot do and what a grep would do unreliably (it would also
   flag this very module's own comments).

Honest residual (documented per the task-6 review's own ask -- "a known,
written-down gap is fine; an unknown one is not"), reproduced by hand
against `/tmp` copies, not fixed by this round:
  - `import homeassistant` inside `custom_components/srne_inverter/__init__.py`
    is deliberately NOT scanned by test 3 and deliberately never reached by
    tests 1/2 (they never execute that file) -- that file is HA-side code by
    design, and is expected to import homeassistant starting with a later
    task. Neither test asserts the POSITIVE "the CLI never executes
    __init__.py" property beyond the fact that these three tests all stay
    green today; that property is exercised indirectly, not asserted
    directly.
  - A core module imported ONLY by a future test module (not by any of
    registers.py/probe.py/transport.*/tools/probe.py) is invisible to test 3's
    fixed file list and to tests 1/2's fixed import list -- adding a file to
    SCANNED_FILES/test 1's SNIPPET is a manual step whenever a new core
    module is added to the package (Tasks 7-16 will add coordinator.py,
    entities, config_flow, etc. -- all of those are meant to import
    homeassistant and are correctly OUT of scope for all three tests here;
    only a genuinely HA-free-required addition would need to be added).
  - A dynamic `importlib.import_module(f"homeassistant.{part}")` built from a
    runtime string that never contains the literal substring "homeassistant"
    (e.g. assembled character-by-character) would defeat test 3's static
    scan, though it would still be caught live by tests 1/2 if that code path
    executes during either. Considered too contrived to defend against
    further here.
"""

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPONENT = REPO / "custom_components" / "srne_inverter"
TOOLS_PROBE = REPO / "tools" / "probe.py"

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


CLI_IMPORT_GUARD = """
import runpy
import sys


class _BlockHomeAssistant:
    \"\"\"sys.meta_path finder: raise on any homeassistant* import, defer to
    the real finders for everything else. Inserted first so it is asked
    about every import in the process before any real finder is.\"\"\"

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "homeassistant":
            raise ImportError(
                f"blocked import of {{name!r}} by the HA-free guard"
            )
        return None


sys.meta_path.insert(0, _BlockHomeAssistant())
sys.argv = [{script!r}, "--help"]
runpy.run_path({script!r}, run_name="__main__")
"""


def test_cli_help_never_imports_homeassistant_anywhere():
    result = subprocess.run(
        [sys.executable, "-c", CLI_IMPORT_GUARD.format(script=str(TOOLS_PROBE))],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--slave" in result.stdout


# Every module the CLI's own promise ("Home Assistant is NOT required")
# covers: the four core modules PLUS tools/probe.py itself. Deliberately
# excludes custom_components/srne_inverter/__init__.py -- see the module
# docstring's "Honest residual".
SCANNED_FILES: tuple[Path, ...] = (
    COMPONENT / "registers.py",
    COMPONENT / "probe.py",
    COMPONENT / "transport" / "__init__.py",
    COMPONENT / "transport" / "base.py",
    COMPONENT / "transport" / "solarman_v5.py",
    TOOLS_PROBE,
)


def _homeassistant_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "homeassistant":
                    found.append(f"{path.name}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "homeassistant":
                found.append(f"{path.name}:{node.lineno}: from {node.module} import ...")
    return found


def test_no_homeassistant_import_at_any_nesting():
    violations: list[str] = []
    for path in SCANNED_FILES:
        violations.extend(_homeassistant_imports(path))
    assert not violations, violations

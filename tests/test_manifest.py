"""manifest.json and hacs.json are hand-edited JSON with no other coverage.

If a later task drops a comma or the version key, pytest must fail here instead
of the defect surfacing on Gabriel's production Home Assistant, where the loader
silently blocks the integration from loading (homeassistant/loader.py) and every
entity vanishes with no test having failed.
"""

from __future__ import annotations

import json
from pathlib import Path

from awesomeversion import AwesomeVersion, AwesomeVersionStrategy

from custom_components.srne_inverter import const

MANIFEST_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "srne_inverter"
    / "manifest.json"
)
HACS_PATH = Path(__file__).parent.parent / "hacs.json"


def test_manifest_is_valid_and_loadable():
    manifest = json.loads(MANIFEST_PATH.read_text())

    assert manifest["domain"] == const.DOMAIN
    assert manifest["config_flow"] is True

    version = AwesomeVersion(manifest["version"])
    assert version.strategy == AwesomeVersionStrategy.SEMVER


def test_hacs_json_is_valid():
    hacs = json.loads(HACS_PATH.read_text())
    assert hacs["name"]

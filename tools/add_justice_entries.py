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

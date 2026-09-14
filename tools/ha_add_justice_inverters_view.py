"""Add an 'Inversores Justice' view (Casa Principal Justice, 2 x SRNE/BlueSun
10 kW read through `srne_inverter`, entities `*.justice_inv_1_*` / `*_2_*`) to
the 'dashboard-justice' storage dashboard. Two columns, one per inverter.

Validates every entity against /api/states first (missing ones are dropped
with a warning), backs up the current dashboard config to
tools/_lovelace_backup_*.json, then applies via WS lovelace/config/save.
Idempotent: replaces any previous 'justice-inversores' view. Run with
HA_BASE=http://172.16.10.12:8123 (LAN) -- the Cloudflare URL answers 403 to
/api/states.
"""
from __future__ import annotations
import asyncio, json, time, urllib.request
from pathlib import Path
import websockets
import sys
sys.path.insert(0, "/data/claude/casa-gadi/HomeAssistant/tools")
from _ha_env import TOKEN, BASE

WS = BASE.replace("https", "wss").replace("http", "ws") + "/api/websocket"
DASH = "dashboard-justice"
VIEW_PATH = "justice-inversores"
UNITS = (1, 2)


def inv_column(n: int) -> dict:
    S = f"sensor.justice_inv_{n}_"
    NU = f"number.justice_inv_{n}_"
    SE = f"select.justice_inv_{n}_"
    bateria = {"type": "entities", "title": f"Inv {n} — Batería", "show_header_toggle": False, "entities": [
        {"entity": f"{S}battery_soc", "name": "SOC", "secondary_info": "last-changed"},
        {"entity": f"{S}battery_voltage", "name": "Voltaje"},
        {"entity": f"{S}battery_current", "name": "Corriente"},
        {"entity": f"{S}battery_power", "name": "Potencia"},
        {"entity": f"{S}battery_temperature", "name": "Temperatura"},
        {"entity": f"{S}charge_state", "name": "Estado de carga"},
    ]}
    red_salida = {"type": "entities", "title": f"Inv {n} — Red y salida", "show_header_toggle": False, "entities": [
        {"type": "section", "label": "Red (entrada)"},
        {"entity": f"{S}grid_voltage_l1", "name": "Voltaje L1"},
        {"entity": f"{S}grid_voltage_l2", "name": "Voltaje L2"},
        {"entity": f"{S}grid_current_l1", "name": "Corriente L1"},
        {"entity": f"{S}grid_current_l2", "name": "Corriente L2"},
        {"entity": f"{S}grid_frequency", "name": "Frecuencia"},
        {"type": "section", "label": "Salida"},
        {"entity": f"{S}output_voltage_l1", "name": "Voltaje L1"},
        {"entity": f"{S}output_voltage_l2", "name": "Voltaje L2"},
        {"entity": f"{S}output_current_l1", "name": "Corriente L1"},
        {"entity": f"{S}output_current_l2", "name": "Corriente L2"},
        {"type": "section", "label": "Carga"},
        {"entity": f"{S}load_power_l1", "name": "Potencia L1"},
        {"entity": f"{S}load_power_l2", "name": "Potencia L2"},
        {"entity": f"{S}load_power_total", "name": "Potencia total"},
        {"entity": f"{S}load_percentage", "name": "% de carga"},
        {"type": "section", "label": "Diagnóstico Justice"},
        {"entity": f"{S}grid_charge_current", "name": "Corriente de carga desde red"},
    ]}
    config = {"type": "entities", "title": f"Inv {n} — Configuración", "show_header_toggle": False, "entities": [
        {"entity": f"{SE}output_priority", "name": "Prioridad de salida"},
        {"entity": f"{SE}battery_type", "name": "Tipo de batería"},
        {"entity": f"{SE}bms_communication", "name": "Comunicación BMS"},
        {"entity": f"{NU}ac_charge_current_limit", "name": "Límite corriente carga AC"},
        {"entity": f"{NU}max_charge_current", "name": "Corriente máx. de carga"},
        {"entity": f"{NU}soc_low_alarm", "name": "Alarma SOC bajo"},
        {"entity": f"{NU}charge_stop_soc", "name": "SOC de corte de carga"},
        {"entity": f"{NU}discharge_stop_soc", "name": "SOC de corte de descarga"},
    ]}
    estado = {"type": "entities", "title": f"Inv {n} — Estado de la integración", "show_header_toggle": False, "entities": [
        {"entity": f"binary_sensor.justice_inv_{n}_fault_active", "name": "Falla activa"},
        {"entity": f"switch.justice_inv_{n}_connection", "name": "Conexión (pausar para liberar el logger)"},
        {"entity": f"button.justice_inv_{n}_reprobe", "name": "Re-sondear capacidades"},
        {"entity": f"{S}firmware_version", "name": "Firmware"},
    ]}
    return {"type": "vertical-stack", "cards": [bateria, red_salida, config, estado]}


def build_view() -> dict:
    B = lambda e, name: {"type": "entity", "show_name": True, "show_state": True, "show_icon": True, "entity": e, "name": name}
    badges = []
    for n in UNITS:
        S = f"sensor.justice_inv_{n}_"
        badges += [
            B(f"{S}battery_soc", f"Inv {n} SOC"),
            B(f"{S}battery_power", f"Inv {n} Potencia batería"),
            B(f"{S}machine_state", f"Inv {n} Estado"),
            B(f"{S}grid_charge_current", f"Inv {n} Carga desde red"),
        ]
    columnas = {"type": "grid", "columns": 2, "square": False, "cards": [inv_column(n) for n in UNITS]}
    graphs = {"type": "vertical-stack", "cards": [
        {"type": "history-graph", "title": "SOC por inversor (últimas 24 h)", "hours_to_show": 24,
         "entities": [f"sensor.justice_inv_{n}_battery_soc" for n in UNITS]},
        {"type": "history-graph", "title": "Corriente de carga desde red (últimas 6 h)", "hours_to_show": 6,
         "entities": [f"sensor.justice_inv_{n}_grid_charge_current" for n in UNITS]},
    ]}
    return {"title": "Inversores Justice", "path": VIEW_PATH, "icon": "mdi:flash", "subview": False,
            "badges": badges, "cards": [
                {"type": "markdown", "content": "## Inversores SRNE/BlueSun 10 kW — Casa Principal Justice\nInv 1 = host del paralelo (`192.168.188.240`). Inv 2 = `192.168.188.242`."},
                columnas, graphs,
            ]}


def prune(view: dict, existing: set[str]) -> list[str]:
    """Drop entities HA does not know; return the dropped ids."""
    dropped = []
    def keep(e):
        eid = e.get("entity") if isinstance(e, dict) else e
        if eid and eid not in existing:
            dropped.append(eid); return False
        return True
    view["badges"] = [b for b in view["badges"] if keep(b)]
    def walk(card):
        if "entities" in card:
            card["entities"] = [e for e in card["entities"] if not (isinstance(e, dict) and "entity" in e) and not isinstance(e, str) or keep(e)]
        for c in card.get("cards", []): walk(c)
    for c in view["cards"]: walk(c)
    return dropped


async def rpc(ws, mid, msg):
    await ws.send(json.dumps(dict(msg, id=mid)))
    while True:
        r = json.loads(await ws.recv())
        if r.get("id") == mid and r.get("type") == "result": return r


async def main():
    req = urllib.request.Request(f"{BASE}/api/states", headers={"Authorization": f"Bearer {TOKEN}"})
    existing = {s["entity_id"] for s in json.load(urllib.request.urlopen(req, timeout=20))}
    view = build_view(); dropped = prune(view, existing)
    if dropped: print("⚠️ entidades no existentes, omitidas:", dropped)
    async with websockets.connect(WS, max_size=40 * 1024 * 1024) as ws:
        await ws.recv(); await ws.send(json.dumps({"type": "auth", "access_token": TOKEN})); await ws.recv()
        cfg = await rpc(ws, 1, {"type": "lovelace/config", "url_path": DASH})
        if not cfg.get("success"): print("ERROR fetching config:", cfg); return
        full = cfg["result"]
        bk = Path(__file__).with_name(f"_lovelace_backup_{DASH}_{time.strftime('%Y%m%d_%H%M%S')}.json")
        bk.write_text(json.dumps(full, indent=1)); print("backup:", bk.name)
        views = [v for v in full.get("views", []) if v.get("path") != VIEW_PATH]
        views.append(view); full["views"] = views
        save = await rpc(ws, 2, {"type": "lovelace/config/save", "url_path": DASH, "config": full})
        print("OK — vista guardada" if save.get("success") else f"ERROR saving: {save}", "| vistas:", [v.get("path") or v.get("title") for v in views])


if __name__ == "__main__":
    asyncio.run(main())

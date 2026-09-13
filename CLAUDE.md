---
type: project
title: ha-srne-inverter
description: "Integración custom de Home Assistant (`srne_inverter`) para inversores SRNE/BlueSun leídos por logger Solarman (V5, TCP 8899): monitoreo y control local sin nube para Casa Principal Justice (2) y Yoga (6). **Local-only** (sin respaldo GitHub)."
production: true
status: active
stack: [python, home-assistant, pysolarmanv5, modbus, pytest]
deploy: "custom_components/srne_inverter → /config/custom_components/ del HA de GADI (172.16.10.12), `ha core restart`; aún NO desplegado"
repo: local-only
tags: [gadi, clientes, rowley, solar, home-assistant]
related: ["Casa Justice (Main House)", "HomeAssistant", "inverter-bridge"]
---

# ha-srne-inverter — integración HA para inversores SRNE/BlueSun vía logger Solarman

## Qué es y por qué

Gabriel tiene inversores BlueSun 10 kW (base **SRNE**, fw `V8.18.006`, mapa Modbus SRNE V1.96) con logger WiFi
**Solarman LSW-5** en **Casa Principal Justice (2, paralelo split-phase)** y **Yoga (6)**, más unidades guardadas.
Quiere monitorearlos y ajustarlos **desde el HA de GADI sin la nube de Solarman**, con valores reales por registro
y sin suponer que todas las unidades son iguales. Decisión del 2026-09-13 (~10:45 CST): **integración propia**
`srne_inverter`, en vez de la integración HACS Solarman (davidrapan) — esa depende de descubrimiento UDP que no
cruza subredes y sus perfiles no sondean capacidades por unidad. GADI (2 SunGoldPower por RS485 con
`inverter-bridge`) queda fuera: el logger no daría más datos.

## Estado (2026-09-13 11:00 CST)

- ✅ **Diseño aprobado en alcance**: [`docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md`](docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md)
  (hechos verificados de los equipos, arquitectura, entidades v1, pruebas, despliegue). Leerlo antes de tocar código.
- ✅ **Referencias de registros** en [`docs/reference/`](docs/reference/): `inverter-bridge_srne_map.py` (mapa SRNE en
  producción en GADI), `ha-solarman-profile_srne_bluesun_justice.yaml` (64 ítems con escalas/enums/rangos ya decididos),
  `ha-solarman-profile_srne_asf_upstream.yaml`.
- ✅ **Entorno de tests**: `.venv` (gitignored) creado con `uv` (instalado en el usuario: `~/.local/bin/uv`, Python
  gestionado **3.14.7**), `homeassistant==2026.9.2` vía `pytest-homeassistant-custom-component==0.13.365`,
  `pysolarmanv5` 3.0.6, `pytest-asyncio`. ⚠️ HA 2026.9 exige Python ≥ 3.14 (3.13 no resuelve). Recrear:
  `uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python pytest-homeassistant-custom-component==0.13.365 "pysolarmanv5>=3.0.6" pytest-asyncio`.
- ⏳ **Plan de implementación**: lo redacta un subagente Opus en
  `docs/superpowers/plans/2026-09-13-srne-inverter-integration.md` (si el archivo no existe, el plan no llegó a
  escribirse: regenerarlo con un subagente `model: opus` a partir de la spec — el prompt pide formato
  `superpowers:writing-plans`, tareas con TDD, transporte falso con los crudos de Justice, prueba en vivo solo
  lectura, despliegue y vista Lovelace marcados "requieren OK de Gabriel").
- ⬜ Código: nada escrito todavía (`custom_components/` no existe).

## Cómo retomar (línea de reentrada)

> Abrí `/data/claude/ha-srne-inverter/CLAUDE.md` y la spec. Ejecutá el plan
> `docs/superpowers/plans/2026-09-13-srne-inverter-integration.md` **una tarea por subagente Sonnet
> (`model: sonnet` explícito)**, con revisión Opus (`model: opus`) al final de cada tramo, según
> `superpowers:subagent-driven-development`. Tests con `.venv/bin/python -m pytest`. Antes de la tarea de prueba
> en vivo, matá los monitores que tengan abierto el logger: `for p in $(pgrep -f "justice_watc[h].py"); do kill $p; done`
> (cada logger acepta UN cliente TCP). Despliegue al HA real y vista "Inversores Justice" solo con OK de Gabriel.

## Datos de los equipos de prueba (Casa Principal Justice)

| Inversor | Logger IP (lease estático) | Serial logger | Esclavo Modbus |
|---|---|---|---|
| 1 (host del paralelo) | `192.168.188.240` | `3548208972` | 1 |
| 2 | `192.168.188.242` | `3548738877` | 2 |

Web del logger: `http://<ip>/status.html` (auth básica `admin`/`admin`) expone el serial como `cover_mid`. Los de
Yoga: pendiente inventariar (barrido TCP 8899 en la VLAN IoT de Yoga; router `10.45.14.59`). Herramientas ya
existentes que hablan V5 con estos equipos: `Redes-Clientes/CasaJustice/tools/` (`justice_inverters_read.py`,
`justice_watch.py`, `justice_test_register.py`, `justice_set_user_voltages.py`).

## Convenciones

- Código, identifiers, commits (Conventional Commits + `Co-Authored-By: Claude …`) en inglés; docs en español.
- **Repo local-only**: sin remoto hasta que Gabriel decida (opción: GitHub privado `gabrielpc1190/ha-srne-inverter`
  para instalar por HACS como repositorio custom). Nunca push sin OK.
- `registers.py` y `transport/` **no importan `homeassistant`** (los usa también `tools/probe.py`).
- Toda escritura a un inversor relee el registro y falla si no coincide. Nada de valores asumidos.

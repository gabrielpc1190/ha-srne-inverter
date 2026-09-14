---
type: project
title: ha-srne-inverter
description: "Integración custom de Home Assistant (`srne_inverter`) para inversores SRNE/BlueSun leídos por logger Solarman (V5, TCP 8899): monitoreo y control local sin nube para Casa Principal Justice (2) y Yoga (6). **Local-only** (sin respaldo GitHub)."
production: true
status: active
stack: [python, home-assistant, pysolarmanv5, modbus, pytest]
deploy: "custom_components/srne_inverter → /config/custom_components/ del HA de GADI (172.16.10.12), `ha core restart`; código listo, revisado y probado contra hardware real — aún NO desplegado"
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

## Estado (2026-09-14, tras la prueba en vivo)

- ✅ **Diseño aprobado en alcance**: [`docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md`](docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md)
  (hechos verificados de los equipos, arquitectura, entidades v1, pruebas, despliegue). Tiene notas de corrección
  🔴 2026-09-14 que reflejan lo que la prueba en vivo refutó del diseño original — leerlas antes de asumir el spec
  al pie de la letra.
- ✅ **Plan ejecutado completo**: [`docs/superpowers/plans/2026-09-13-srne-inverter-integration.md`](docs/superpowers/plans/2026-09-13-srne-inverter-integration.md)
  — las 16 tareas de software (1–16, núcleo sin HA + runtime HA) están **cerradas**, cada una con revisión Opus y
  al menos una ronda de fix. Ledger completo, decisión por decisión, en
  [`.superpowers/sdd/2026-09-13-srne-inverter-integration/progress.md`](.superpowers/sdd/2026-09-13-srne-inverter-integration/progress.md)
  (no editar — es registro histórico).
- ✅ **Código terminado**: ~43 commits en la rama `feat/srne-inverter-integration` (`git log --oneline master..HEAD`),
  **252 pruebas pasando, 0 fallos**. Incluye: núcleo sin dependencia de `homeassistant` (`registers.py` con mapa
  SRNE V1.96, `transport/` Solarman V5 async con lock single-client y taxonomía de errores, `probe.py` de sondeo
  de capacidades por unidad, `tools/probe.py` CLI standalone), `coordinator.py` con sondeo escalonado HOT/WARM/COLD
  y reintentos con backoff, **61 sensores + 1 binary_sensor + 18 number + 5 select** generados dinámicamente desde
  el mapa de registros, `switch.py` (pausa de conexión, para liberar el logger a otra herramienta), `button.py`
  (re-sondeo), `config_flow.py` (formulario en/es con scrape del serial de `status.html`), `diagnostics.py`
  (con scrubbing de host/serial) y `services.py` (`read_register`, `write_register`, `reprobe`).
- ✅ **Primera corrida contra hardware real, 2026-09-14** — Casa Principal Justice, los dos inversores
  (`192.168.188.240` esclavo 1, `192.168.188.242` esclavo 2). Detalle completo en
  [`docs/evidence/2026-09-14_live-test-notes.md`](docs/evidence/2026-09-14_live-test-notes.md).
  - Sondeo: **10/10 bloques soportados en ambos**, sondeo completo en 3,7 s (inv1) / 3,5 s (inv2) — contra un
    plazo de 60 s y un intervalo HOT de 10 s, margen amplio.
  - Escritura: **4 ensayos autorizados** (SOC bajo `0xE01E` 15→16→15; ecualización `0xE007`→142 con BMS activo,
    rechazada limpiamente por el firmware; tipo de batería `0xE004` 6→USER(0)→6, con restauración de otros 9
    registros verificada; ecualización con BMS suspendido, primera captura con equalize/boost/float distintos).
    Los cuatro con foto previa de los 48 registros `E000-E02F`, relectura y comparación final: **cero deriva**.
  - **Cuatro hechos del mapa de registros que la prueba corrigió** (ya aplicados en el commit `a9e2795`,
    "fix(registers): correct 4 register-map facts found by the first live run", 252 pruebas tras el fix):
    1. `grid_current_l2` estaba en `0x022B` — con carga real (15,5 A / 16,9 A y `machine_state=2`, bypass CA)
       leyó **0,0 A en ambos equipos**. El correcto es `0x0238` (15,6 A / 17,3 A, siguiendo la carga). `0x022B`
       quedó deliberadamente sin mapear; su significado real se desconoce. El script viejo
       `justice_inverters_read.py` tenía razón; el perfil YAML (fuente elegida en el diseño) estaba mal.
    2. `0x0018-0x001B` **no es el número de serie** del inversor: lee `[0,0,1,45]` (inv1) y `[0,0,2,45]` (inv2) —
       el tercer valor es el ID de esclavo Modbus, no un serial. Campo renombrado a `device_info_tail`.
    3. `0xE03A+` **no está ausente**, al contrario de lo que decía el spec original: responde `[0,0]`, y más
       adelante en el mismo tramo `0xE100`/`0xE116`/`0xE121` devuelven datos estructurados. Todo `0xE03A-0xE12F`
       está presente en este firmware.
    4. `0x0112+` y `0xE21F+` **sí están genuinamente ausentes** — esto confirmó el spec, no lo contradijo
       (`IllegalDataAddress` en ambos rangos).
  - 🔴 **Hecho operativo que hay que dejar visible, porque si no alguien lo va a reportar como bug**: con
    comunicación BMS activa (`E215 = 1`, el estado normal deliberado en Casa Justice — Gabriel prefiere que la
    batería le dicte los valores al inversor), el firmware **rechaza** las escrituras a `E007`/`E008`/`E009`
    (ecualización/boost/flotación) con `IllegalDataValue` de Modbus (`InvalidRegisterValueError` en nuestro
    transporte). Las tres entidades `number` correspondientes van a fallar **siempre** en una unidad gestionada
    por BMS — es el comportamiento buscado (fallo seguro y ruidoso, no un valor que aparenta aceptarse), y el
    registro que habilita esas escrituras es **`E215`**, no el tipo de batería `E004`. Los umbrales existen para
    el caso de una batería que NO se comunica con el inversor, que Gabriel confirmó el 2026-09-14 que la
    integración debe soportar igual.
- ✅ **Decisiones de Gabriel, 2026-09-14**: autorizó el despliegue (tarea 18), la vista Lovelace (tarea 19) y las
  escrituras de la prueba en vivo; y decidió que el repo puede pasar a **GitHub privado**
  (`gabrielpc1190/ha-srne-inverter`), lo que habilita instalar por HACS como repositorio custom. **Todavía no se
  hizo** — nada se pushea sin OK explícito por separado (además del OK de "puede ir a GitHub").
- ⬜ **Falta**: tarea 18 (desplegar al HA de GADI), tarea 19 (vista Lovelace) y tarea 20 (documentación del plan).
  Hay una revisión del commit `a9e2795` en vuelo — mirar su veredicto antes de desplegar.

## Cómo retomar (línea de reentrada)

> Abrí `/data/claude/ha-srne-inverter/CLAUDE.md`, el ledger
> [`.superpowers/sdd/2026-09-13-srne-inverter-integration/progress.md`](.superpowers/sdd/2026-09-13-srne-inverter-integration/progress.md)
> y [`docs/evidence/2026-09-14_live-test-notes.md`](docs/evidence/2026-09-14_live-test-notes.md). Revisar el
> veredicto pendiente sobre el commit `a9e2795` antes de tocar nada más. Luego, con las tres autorizaciones de
> Gabriel ya dadas (despliegue, Lovelace, GitHub privado):
> 1. **Tarea 18** — copiar `custom_components/srne_inverter` a `/config/custom_components/` del HA de GADI
>    (`172.16.10.12`, alias SSH `GADI-HomeAssistant`, ya tiene HACS y 11 integraciones personalizadas),
>    `ha core restart`, configurar las dos entradas (Justice inv1/inv2) desde la UI.
> 2. **Tarea 19** — vista Lovelace "Inversores Justice".
> 3. **Tarea 20** — documentación final del plan (README/HACS, no confundir con este CLAUDE.md).
> Antes de reabrir cualquier prueba contra hardware real, matá los monitores que tengan abierto el logger:
> `for p in $(pgrep -f "justice_watc[h].py"); do kill $p; done` (cada logger Solarman acepta UN cliente TCP).
> Tests con `.venv/bin/python -m pytest -q` (252 pruebas, ~394 s corridas en foreground — no backgroundear, un
> agente ya perdió una corrida por matar el directorio de un pytest backgroundeado a medias).

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
- **Repo local-only, sin remoto todavía**: Gabriel ya decidió (2026-09-14) que puede pasar a GitHub **privado**
  (`gabrielpc1190/ha-srne-inverter`, para instalar por HACS como repositorio custom) — falta ejecutarlo. Nunca
  push sin OK explícito, incluso después de crear el remoto.
- `registers.py` y `transport/` **no importan `homeassistant`** (los usa también `tools/probe.py`).
- Toda escritura a un inversor relee el registro y falla si no coincide. Nada de valores asumidos.

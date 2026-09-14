# Notas de la prueba en vivo — `srne_inverter` contra Casa Justice (Tarea 17)

Registro técnico de la corrida real de la Tarea 17 del plan
(`docs/superpowers/plans/2026-09-13-srne-inverter-integration.md`) contra los
dos inversores reales de Casa Principal Justice. Complementa, sin repetir,
los archivos de evidencia cruda en este mismo directorio.

## Discrepancia de fecha (y de nombre) respecto del plan

El plan pide este archivo como `docs/evidence/2026-09-13_live-test-notes.md`
y los volcados de sondeo como `2026-09-13_live-probe-inv1.json` /
`..._live-probe-inv2.json` (ver Task 17, "Files"). La corrida real ocurrió el
**2026-09-14**, un día después de escrito el plan, y los archivos que en
efecto se generaron llevan esa fecha y además dejaron caer el infijo
`live-`: `2026-09-14_probe-inv1.json`, `2026-09-14_probe-inv2.json`. Este
documento se nombra en consecuencia, `2026-09-14_live-test-notes.md`, para
quedar junto a la evidencia real en vez de junto a la fecha que anticipó el
plan. Quien busque `live-probe-inv1.json` no lo va a encontrar — el volcado
de sondeo es `2026-09-14_probe-inv1.json`.

## Equipos y alcance

| Inversor | IP del logger | Serial del logger | Esclavo Modbus |
|---|---|---|---|
| 1 (host del paralelo) | `192.168.188.240` | `3548208972` | 1 |
| 2 | `192.168.188.242` | `3548738877` | 2 |

Firmware en ambos: `V8.18.006`. Antes de arrancar se verificó que ningún
proceso local tuviera abierto un socket contra los loggers (ver última
sección) — cada logger Solarman acepta un solo cliente TCP a la vez.

## 1. Sondeo de capacidades

Volcados crudos: `2026-09-14_probe-inv1.json`, `2026-09-14_probe-inv2.json`.

Resultado: **10 de 10 bloques soportados en ambos inversores** — todos los
bloques que el mapa de `registers.py` declara (`0x0014`, `0x0100`, `0x0200`,
`0x0210`, `0x0223`, `0xE000`, `0xE018`, `0xE200`, `0xE210`, `0xF02C`)
respondieron `"supported"` en el campo `support` de los dos JSON, sin ningún
error en `errors`.

Duración de un sondeo completo (los 10 bloques, uno tras otro, con las
pausas entre bloques que impone el coordinador): **3,7 s en inv1, 3,5 s en
inv2**. Contra el plazo de 60 s que el sondeo tiene asignado y el intervalo
de 10 s del tier HOT, el margen es amplio — un sondeo completo consume
~35-37 % del intervalo HOT y ~6 % del plazo total del sondeo. No hay indicio
de que el escalonamiento HOT/WARM/COLD necesite ajuste por tiempo de
sondeo.

## 2. Hallazgos contra lo que afirmaba el spec/plan

Los cuatro hallazgos de abajo ya están corregidos en el código (commit
`a9e2795`, "fix(registers): correct 4 register-map facts found by the first
live run") y en la spec (`docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md`,
notas "🔴 Corrección 2026-09-14"). Este documento registra la medición que
motivó cada corrección, no repite el diff de código.

### 2.1 `grid_current_l2`: `0x022B` estaba mal, es `0x0238`

El perfil YAML (`ha-solarman-profile_srne_bluesun_justice.yaml`), fuente de
verdad elegida por la decisión de diseño 1 del plan, mapeaba corriente de
red L2 a `0x022B`. En vivo, con `machine_state = 2` (bypass CA: la red
alimenta la carga) y carga real de **15,5 A en inv1 y 16,9 A en inv2**,
`0x022B` leyó **0,0 A en los dos equipos**. `0x0238` sí siguió la carga:
**15,6 A y 17,3 A** respectivamente — dentro de lo esperable como corriente
de red en bypass.

Esta lectura de alta carga se tomó con un script ad-hoc durante el paso 4 de
la Tarea 17 y no quedó en un JSON de evidencia aparte; los valores están
registrados en el comentario de campo de `registers.py` (línea ~250) y en el
cuerpo del commit `a9e2795`. Lo que sí queda en archivo, como corroboración
independiente con carga baja, es el propio `2026-09-14_probe-inv1.json`: ahí
`0x0230` (`load_current_l2`) y `0x0238` coinciden en **4,6 A**, mientras
`0x022B` lee **0**.

Conclusión: el script viejo `justice_inverters_read.py` (que siempre leyó
`0x0238`) tenía razón; el perfil YAML no. El mapa se corrigió a `0x0238` y
`0x022B` quedó deliberadamente sin mapear — su significado real es
desconocido.

### 2.2 `0x0018-0x001B` no es el número de serie del inversor

`registers.py` lo decodificaba como `inverter_serial` (decisión de diseño 5,
marcada "unverified" en el plan). Verificado directamente en los volcados de
sondeo:

- inv1 (`2026-09-14_probe-inv1.json`): `0x0018=0, 0x0019=0, 0x001A=1, 0x001B=45`
- inv2 (`2026-09-14_probe-inv2.json`): `0x0018=0, 0x0019=0, 0x001A=2, 0x001B=45`

El tercer valor (`1` / `2`) coincide exactamente con el ID de esclavo Modbus
de cada unidad, no con ningún serial. El campo se renombró a
`device_info_tail` en el código; la identidad de entidad ya dependía de
`CONF_SERIAL` de la config entry, no de este registro.

### 2.3 `0xE03A+` sí existe — la spec decía lo contrario

El spec original y el plan (Global Constraints) listaban `0xE03A+` como
ausente en este firmware. Falso: leído en vivo, `0xE03A` devuelve `[0, 0]`
(presente, en cero) y más adelante en el mismo rango `0xE100` devuelve
`[0, 10, 9, 65523]`, `0xE116` devuelve `[45, 0, 100, 0]` y `0xE121` devuelve
`[200, 10, 3750, 5]` — datos estructurados, no relleno. Todo el tramo
`0xE03A-0xE12F` está presente en este firmware.

Igual que en 2.1, estas lecturas se hicieron con un script ad-hoc fuera del
`probe.py` estándar (ese rango no forma parte de ningún `Block` declarado en
`registers.py`) y no generaron un JSON de evidencia propio; quedaron
documentadas en el comentario de `tests/fake_transport.py` (alrededor de la
línea 45) y en el punto 3 del commit `a9e2795`, que retiró `0xE03A-0xE0FF`
de `FakeTransport.DEFAULT_UNSUPPORTED`.

### 2.4 `0x0112+` y `0xE21F+` sí están ausentes — esto lo confirma, no lo contradice

A diferencia de 2.3, esta afirmación del spec/plan resultó correcta: ambos
rangos devolvieron `IllegalDataAddress` de Modbus al leerlos, consistente
con lo ya declarado. Sin cambios de código.

## 3. Estado de configuración encontrado (antes de cualquier escritura)

Volcado de referencia: `2026-09-14_inv1-settings-before-writes.json` — los
48 registros `0xE000-0xE02F` de inv1, capturados antes de tocar nada. El
sondeo completo (sección 1) confirma que inv2 está en el mismo estado para
los registros que comparte ese sondeo (`0xE215=1` en ambos JSON de sondeo).

Los dos equipos tienen:

- `E004 = 6` — tipo de batería preset de litio (no `USER`, que es `0`).
- `E215 = 1` — comunicación BMS activa (RS485).

Consecuencia medida: con el BMS mandando, ecualización (`E007`), boost
(`E008`) y flotación (`E009`) leen los tres **144** (57,6 V, con la escala
`0,4` del banco), y el corte por sobredescarga (`E00D`) lee **116**
(46,4 V) — todos valores dictados por el BMS, no por el preset de fábrica.

Gabriel confirmó que el modo BMS es deliberado en este sitio: prefiere que
la batería le dicte los valores al inversor antes que fijarlos a mano. La
integración tiene que soportar igual el caso contrario — una batería que no
se comunica y necesita esos umbrales escritos manualmente.

## 4. Ensayos de escritura

Los cuatro ensayos siguientes fueron autorizados por Gabriel en la sesión en
vivo. Procedimiento común a los cuatro: foto previa de los 48 registros
`E000-E02F`, escritura, relectura inmediata, restauración explícita al valor
original, comparación final registro por registro. Ese patrón sigue el
requisito del plan ("toda escritura relee el mismo registro y falla si no
coincide") mediante `assert` dentro del propio script — no todos los pasos
intermedios quedaron volcados a un JSON aparte; donde no hay archivo crudo
se aclara abajo.

### A — SOC bajo (`0xE01E`): 15 → 16 → 15

Escritura, relectura y restauración verificadas inline (assert en el script
de la Tarea 17, paso 5) contra el valor de partida documentado en
`2026-09-14_inv1-settings-before-writes.json` (`E01E = 15`). No se generó un
JSON aparte del estado intermedio (`16`) ni del estado restaurado — la
verificación es la traza de consola del propio ensayo. Es la primera prueba
de que el camino de escritura de la integración (escritura → relectura →
comparación) funciona contra hardware real, no solo contra `FakeTransport`.

### B — Ecualización (`0xE007`) a 142 con BMS activo: rechazada

Con `E215 = 1` todavía puesto, se intentó `E007: 144 → 142`. El firmware
**rechazó la escritura** con `IllegalDataValue` de Modbus, que el transporte
(`transport/solarman_v5.py`) traduce a `InvalidRegisterValueError`. No fue
una relectura discordante — fue un rechazo limpio en el momento de escribir.
Sin deriva posterior: `E007` nunca cambió de `144`.

### C — Tipo de batería (`0xE004`): 6 → 0 (USER) → 6

Volcados: `2026-09-14_inv1-settings-before-writes.json` (antes, `E004=6`) y
`2026-09-14_inv1-settings-user-mode.json` (con `E004=0`, USER). El diff
entre ambos archivos confirma que pasar a USER recargó valores de fábrica en
otros 9 registros, no solo en `E004`:

| Registro | Campo | Antes (BMS) | En USER |
|---|---|---|---|
| `E00A` | — | 134 | 130 |
| `E00B` | — | 132 | 130 |
| `E00C` | — | 124 | 110 |
| `E00E` | — | 108 | 100 |
| `E01B` | `battery_to_mains_voltage` | 123 | 109 |
| `E010` | — (temporizador) | 30 | 5 |
| `E011` | — (temporizador) | 5 | 120 |
| `E013` | — (temporizador) | 5 | 30 |
| `E023` | — (temporizador) | 5 | 120 |

La escritura de ecualización (`E007 → 142`) también fue rechazada en modo
USER — lo que aisló que el rechazo del ensayo B no depende de estar en modo
USER o BMS, sino que es un comportamiento propio del firmware ante esa
combinación de registros en ese momento. Volver `E004` a `6` restauró estos
9 registros; esa restauración está confirmada por comparación inline
(consola del ensayo) contra el archivo `..._before-writes.json`, sin que se
haya guardado un tercer JSON del estado ya restaurado.

### D — Ecualización con BMS suspendido: primera captura con los tres valores distintos

Con `E215` (comunicación BMS) puesto brevemente en `0`, la escritura
`E007 → 142` fue **aceptada de inmediato** — a diferencia del ensayo B, que
falló con el mismo valor y el mismo registro bajo BMS activo. Esto aísla que
el rechazo es específico de `E215=1`, no un límite genérico del registro.

Captura resultante en `2026-09-14_inv1-equalize-distinct.json`:

```
E007 (equalize) = 142
E008 (boost)    = 144
E009 (float)    = 144
```

Es la primera vez que ecualización, boost y flotación difieren entre sí en
una captura de este repo — hasta este ensayo, un intercambio de direcciones
entre `E007` y `E008` en el código habría sido indetectable contra los datos
disponibles, porque los tres siempre coincidían en `144`. Esto cierra ese
hueco del mapa de registros.

Después de la captura: `E007` restaurado a `144`, `E215` restaurado a `1`.
Comparación final contra los 48 registros de
`2026-09-14_inv1-settings-before-writes.json`: **sin deriva** — mismo
patrón de verificación inline que en los ensayos A y C, sin un cuarto JSON
de "estado final" guardado aparte.

## 5. Dato operativo para quien use la integración

Con `E215 = 1` (el estado normal y deliberado en este sitio, sección 3), el
firmware **rechaza** las escrituras a `E007`/`E008`/`E009` — confirmado en
el ensayo B y corroborado en el ensayo C bajo modo USER. Las entidades
`number` de esos tres umbrales van a fallar siempre, con
`InvalidRegisterValueError`, en una unidad gestionada por BMS. Ese es el
comportamiento buscado — fallo seguro y ruidoso, no un valor que parece
aceptado y no lo está — y los umbrales escribibles existen para cubrir el
caso de una batería sin comunicación con el inversor, donde el usuario sí
necesita fijarlos a mano.

## 6. Nota de procedimiento — los loggers no se repusieron porque no había nada que reponer

El plan (Task 17, paso 1) manda matar `justice_watch.py` y
`justice_inverters_read.py` antes de la prueba, para liberar el socket único
de cada logger, pero no incluye un paso que los vuelva a levantar al
terminar. En esta corrida se verificó explícitamente que no había ningún
proceso local tomando los loggers — ni proceso corriendo, ni servicio
systemd, ni timer, ni cron asociado a esos scripts — así que no hubo nada
que reponer y el hueco del plan no tuvo efecto esta vez.

Se deja anotado porque el hueco es real: si `justice_watch.py` o
`justice_inverters_read.py` alguna vez pasan a ser persistentes (systemd,
cron), este paso del plan va a necesitar una contraparte explícita de
restauración, o la próxima prueba en vivo los va a dejar caídos sin que
nadie lo note hasta que falte el monitoreo.

## Referencias

- Plan: `docs/superpowers/plans/2026-09-13-srne-inverter-integration.md`, Task 17.
- Spec, notas de corrección 2026-09-14: `docs/superpowers/specs/2026-09-13-srne-inverter-integration-design.md`.
- Commit de código con las 4 correcciones: `a9e2795` ("fix(registers): correct 4 register-map facts found by the first live run").
- Evidencia cruda en este directorio: `2026-09-14_probe-inv1.json`, `2026-09-14_probe-inv2.json`, `2026-09-14_inv1-settings-before-writes.json`, `2026-09-14_inv1-settings-user-mode.json`, `2026-09-14_inv1-equalize-distinct.json`.
- Caso abierto: `casos/2026-09-13_ha-srne-inverter-integracion-ha-propia-para-inversores-srne-bluesun-via-logger-solarman-diseno-listo-falta-planimplementacionprueba-en-vivodespliegue-ok-de-gabriel.md`.

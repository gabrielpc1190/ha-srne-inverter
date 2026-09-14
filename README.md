# srne_inverter — integración custom de Home Assistant

Integración local para inversores solares **SRNE / BlueSun 10 kW** (mapa Modbus SRNE V1.96) leídos a través de su
logger WiFi **Solarman LSW-5** (protocolo V5, TCP 8899) — sin depender de la nube de Solarman. Monitorea batería,
red, salida, configuración y estado del equipo, y permite escribir los parámetros que el firmware acepta, con
relectura y verificación en cada escritura.

Producción: Casa Principal Justice (2 inversores en paralelo split-phase), desplegada en el Home Assistant de
Casa GADI (`172.16.10.12`). Yoga (6 unidades) está pendiente de inventariar — ver el apartado de `tools/probe.py`.

## Por qué una integración propia y no la de HACS (davidrapan)

Existe una integración de terceros para Solarman en HACS (`davidrapan/ha-solarman`), pero no sirve para este caso
por dos motivos verificados durante el diseño (2026-09-13):

- **Depende de descubrimiento UDP** para encontrar el logger, y el UDP de descubrimiento de Solarman no cruza
  subredes/VLANs — Casa GADI administra estos inversores desde otra red que la de los loggers.
- **Sus perfiles no sondean capacidades por unidad**: asumen que todos los registros de un perfil existen en
  todas las unidades. Este proyecto tiene evidencia directa de que eso no es cierto ni siquiera entre dos
  inversores idénticos del mismo sitio (ver "Qué hace el sondeo" abajo) — de ahí el sondeo de capacidades por
  bloque que sí trae `srne_inverter`.

## Requisitos

- Home Assistant **≥ 2025.6** (`hacs.json` declara ese mínimo; el entorno de desarrollo usa
  `homeassistant==2026.9.2` sobre **Python 3.14.7**).
- Paquete `pysolarmanv5>=3.0.6,<4` (lo instala HA solo, vía `manifest.json`; para `tools/probe.py` fuera de HA
  hay que instalarlo a mano, ver más abajo).
- **Un solo cliente TCP por logger.** El logger Solarman LSW-5 acepta una única conexión activa — si otra
  herramienta (`justice_watch.py`, `justice_inverters_read.py`, `tools/probe.py`, un navegador contra
  `status.html`) ya tiene el socket, la integración (o esa otra herramienta) no va a poder conectar. Ver el
  switch "Connection" más abajo para liberar el logger sin desinstalar nada.

## Instalación

**Por HACS** (repositorio custom) — método en uso en el HA de GADI desde el 2026-09-14: el repo vive en GitHub
público (`https://github.com/gabrielpc1190/ha-srne-inverter`, rama `main` — el token OAuth de fábrica de HACS no
tiene scope y no puede ver repos privados de nadie, así que se hizo público a propósito). Se agrega en HACS como
repositorio custom (tipo "Integración") y se instala desde ahí; las actualizaciones de código llegan haciendo
"Redownload" en HACS, sin volver a copiar archivos a mano.

**Manual, por `scp`/`tar` sobre SSH** (método de respaldo si HACS no está disponible; fue el método del primer
despliegue real a Casa GADI antes de pasar a HACS): el shell Alpine del addon SSH de HA OS no trae `rsync`, así
que se empaqueta y se copia por `tar`:

```bash
tar -C custom_components -cf - srne_inverter | ssh GADI-HomeAssistant \
  'tar -C /config/custom_components -xf -'
ssh GADI-HomeAssistant 'ha core restart'
```

Después del restart, HA va a listar `srne_inverter` como integración custom no verificada por HA (mensaje
esperado en el log, no es un error) y ya se puede dar de alta desde **Ajustes → Dispositivos y servicios →
Agregar integración → SRNE Inverter (Solarman V5)**.

## Alta de una unidad

El formulario de configuración pide:

| Campo | Qué va | Notas |
|---|---|---|
| Nombre | Nombre de la config entry | Se usa como nombre del dispositivo y como prefijo de todos los `entity_id` (ver abajo). Ej. "Justice Inv 1". |
| Host | IP del logger Solarman | Ej. `192.168.188.240`. |
| Puerto | TCP del logger | Por defecto `8899`, casi nunca cambia. |
| Serial | Serial del **logger** (no del inversor) | Opcional en el formulario — si se deja vacío, la integración lo scrapea sola de `http://<host>/status.html` (auth básica `admin`/`admin`, campo `cover_mid` de esa página). Si se completa a mano, sacarlo de ahí también. |
| Esclavo Modbus | ID de esclavo | El inversor host del paralelo es `1`; el segundo (y sucesivos) son `2`, `3`, ... |
| Intervalo de sondeo | Segundos del tier HOT | Por defecto `10` s (WARM `60` s y COLD `300` s se ajustan luego por opciones). |

El **serial del logger** (no un número de serie del inversor — ver la advertencia sobre `device_info_tail` más
abajo) es lo que identifica al dispositivo en HA: `device_info.identifiers = (srne_inverter, <serial>)`.

## Tabla de entidades por plataforma

Las entidades se generan dinámicamente desde el mapa de `registers.py` — **solo aparecen los campos cuyo bloque
el sondeo (probe) marcó como soportado** en esa unidad. En Casa Justice, con 10/10 bloques soportados en los dos
inversores, cada config entry expone (verificado en vivo, 2026-09-14): **61 sensor + 1 binary_sensor + 18 number
+ 5 select**, más 1 switch y 1 button que siempre existen (no dependen del sondeo).

| Plataforma | De dónde sale | Ejemplos (`registers.py`) |
|---|---|---|
| `sensor` | Todo `Field` sin `write=` (o de solo lectura mecánica: `HEX`, `VERSION`, `HEX_WORDS`), más 2 valores `Derived` (`battery_power`, `load_power_total`) | `battery_soc`, `battery_voltage`, `grid_current_l2`, `charge_state`, `machine_state`, `fault_word_1-4`, `firmware_version`, `device_info_tail`, `running_days`, `pv_energy_today` |
| `binary_sensor` | Una sola entidad agregada, `fault_active`, presente solo si el bloque `faults` (`0x0200`) está soportado | `binary_sensor.<entry>_fault_active` (`True` si algún `fault_word_1-4` es distinto de `0x0000`) |
| `number` | Todo `Field` de tipo `NUMBER` con `write=WriteSpec(...)` — el rango viene literal de esa `WriteSpec`, nunca calculado | `equalize_voltage`, `boost_voltage`, `float_voltage`, `soc_low_alarm`, `max_charge_current`, `battery_capacity`* |
| `select` | Todo `Field` de tipo `ENUM` con `write=WriteSpec(...)` | `battery_type`, `output_priority`, `bms_charge_limit_mode`, `bms_communication`, `bms_protocol` |
| `switch` | Una sola entidad fija por entry, no depende del sondeo | `switch.<entry>_connection` (ver "Switch Connection" abajo) |
| `button` | Una sola entidad fija por entry, no depende del sondeo | `button.<entry>_reprobe` (ver "Sondeo" abajo) |

`entity_id` sigue el patrón `<plataforma>.<slug del nombre de la entry>_<slug del label del campo>` (HA lo arma
solo por `has_entity_name = True`), ej. `sensor.justice_inv_1_battery_soc`,
`number.justice_inv_1_equalize_voltage`, `select.justice_inv_1_output_priority`,
`binary_sensor.justice_inv_1_fault_active`. El `unique_id` interno (lo que HA usa para no perder el historial si
se renombra la entidad) es `<serial del logger>_<field_key>`, ej. `3548208972_battery_soc` — **no** depende del
nombre de la entry, solo del serial y de la clave interna del campo (`Field.key` en `registers.py`).

## Servicios

Los tres servicios (`custom_components/srne_inverter/services.yaml`) operan por **dirección de registro cruda**,
para diagnóstico y para tocar registros que no tienen entidad propia (ej. escribir directamente a un registro
`config`/`diagnostic` sin pasar por el formulario de opciones). Los tres piden `device_id` (selector de
dispositivo, filtrado a la integración `srne_inverter`).

**`srne_inverter.read_register`** — lee N registros desde una dirección:

```yaml
service: srne_inverter.read_register
data:
  device_id: <id del dispositivo Justice Inv 1>
  address: "0x0100"
  count: 5
```

**`srne_inverter.write_register`** — escribe un valor crudo (0-65535) a un registro; releé y falla si no coincide,
igual que toda escritura de la integración; si la dirección es una de las conocidas como rechazadas por el
firmware (`0xE20B`, `0xE20F`, `0xE21D`, `0xE039` en `services.py::KNOWN_WRITE_REJECTED`), el error lo dice
explícitamente en vez de solo repetir el `IllegalDataValue` crudo de Modbus:

```yaml
service: srne_inverter.write_register
data:
  device_id: <id del dispositivo Justice Inv 1>
  address: "0xE01E"
  value: 16
```

**`srne_inverter.reprobe`** — dispara el mismo sondeo de capacidades que corre al agregar la entry, sin
argumentos más allá del dispositivo (equivalente a tocar el botón "Reprobe"):

```yaml
service: srne_inverter.reprobe
data:
  device_id: <id del dispositivo Justice Inv 1>
```

## Qué hace el sondeo (probe) y cuándo tocar "Reprobe"

Al agregar la entry (y cada vez que se presiona el botón **Reprobe**), la integración lee los 10 bloques que
declara `registers.py` (`0x0014`, `0x0100`, `0x0200`, `0x0210`, `0x0223`, `0xE000`, `0xE018`, `0xE200`, `0xE210`,
`0xF02C`) y clasifica cada uno como soportado, no soportado o desconocido — **nunca asume** que todos los bloques
existen en todas las unidades, porque en la prueba en vivo de Casa Justice quedó demostrado que hasta el mismo
modelo de inversor puede tener rangos ausentes según el firmware. El resultado del sondeo **nunca se persiste a
disco**: se recalcula siempre desde cero (al arrancar HA, o al presionar "Reprobe"), así que un cambio de
firmware se refleja solo con un re-sondeo, sin reinstalar nada.

Tocar el botón **Reprobe** (`button.<entry>_reprobe`) tiene sentido cuando el equipo pudo haber cambiado de
capacidades desde el último sondeo: después de una actualización de firmware, tras reconectar el logger, o si se
sospecha que un bloque que antes contestaba "no soportado" ahora sí responde. Las entidades nuevas que aparezcan
se agregan solas, sin reiniciar HA (vía `SIGNAL_NEW_ENTITIES`).

## Switch "Connection" — liberar el logger a otra herramienta

Cada logger Solarman acepta **un solo cliente TCP a la vez**. El switch `switch.<entry>_connection` (categoría
`config`) apaga la conexión de la integración (cierra el socket y detiene el sondeo) sin desinstalar ni
deshabilitar la entry — así se libera el logger para que otra herramienta (`tools/probe.py`, un script de
diagnóstico standalone, el propio `status.html` del logger) pueda conectarse. El switch queda siempre disponible
(no se apaga solo cuando la conexión está pausada), justamente para poder reactivarla desde la misma UI.

## `tools/probe.py` — inventariar unidades nuevas (pendiente: Yoga)

`tools/probe.py` es el mismo sondeo de capacidades que usa la integración, pero como CLI standalone que **no
requiere Home Assistant instalado** (pone `custom_components/srne_inverter/` en `sys.path` e importa
`registers`/`transport.solarman_v5` como módulos de nivel superior, sin ejecutar `__init__.py`). Sirve para
correr en la laptop de un técnico o en cualquier host con red hacia el logger:

```bash
pip install "pysolarmanv5>=3.0.6,<4"
tools/probe.py 192.168.188.240 --serial 3548208972 --slave 1
tools/probe.py 192.168.188.242 --serial 3548738877 --slave 2 --json inv2.json
```

Antes de correrlo contra un logger que ya tiene otro cliente conectado, hay que liberar ese cliente (matar el
proceso, o apagar el switch "Connection" si es esta misma integración) — el logger no distingue "IP equivocada"
de "logger ocupado" a nivel de socket, así que ambos casos dan el mismo código de salida de conexión fallida
(ver el docstring del script para la tabla completa de códigos de salida).

**Pendiente real**: los 6 inversores de Yoga no están inventariados todavía — falta un barrido TCP 8899 en la
VLAN IoT de Yoga (router `10.45.14.59`) para levantar sus IPs y seriales antes de poder darlos de alta. Este
mismo `tools/probe.py` es la herramienta para hacerlo una vez que se tengan las IPs.

## Advertencias verificadas

- **`ac_input_range` (`0xE20B`) y `charge_priority` (`0xE20F`) son de solo lectura** en este firmware
  (`V8.18.006`): `registers.py` no les da `write=`, y el propio comentario del código confirma que el equipo
  responde `IllegalDataValue` a cualquier intento de escritura (verificado en Casa Justice). **Corrección sobre
  lo que se venía diciendo del plan original**: `0xE21D` y `0xE039` también están confirmados como rechazados
  por el firmware ante una escritura, pero **no son campos decodificados en `registers.py`** — no tienen sensor
  ni ninguna otra entidad asociada; solo aparecen en `services.py::KNOWN_WRITE_REJECTED`, la lista que usa el
  servicio `write_register` para devolver un error explícito ("dirección conocida como rechazada por el
  firmware") en vez de solo repetir el `IllegalDataValue` crudo de Modbus. Si se necesita ver su valor, hay que
  leerlos con el servicio `read_register`.
- **Los voltajes de carga (`equalize_voltage`/`boost_voltage`/`float_voltage`, registros `E007`-`E009`) solo se
  pueden escribir con `battery_type` (`E004`) en `USER`** — y aun así, si `bms_communication` (`E215`) está
  activo (`≠ 0`, el estado normal en Casa Justice porque el BMS le dicta los valores al inversor), el firmware
  **rechaza igual** las tres escrituras con `InvalidRegisterValueError`, sin importar `E004`. Confirmado en la
  prueba en vivo del 2026-09-14: con `E215 = 1` la escritura a `E007` fue rechazada tanto en modo BMS como en
  modo `USER`; solo con `E215` puesto en `0` (BMS suspendido) la misma escritura fue aceptada de inmediato. Es
  comportamiento buscado (fallo seguro y ruidoso, no un valor que aparenta aceptarse) para proteger una batería
  con BMS activo — los umbrales existen para el caso de una batería que NO se comunica con el inversor.

## Cómo correr los tests

```bash
.venv/bin/python -m pytest -q
```

Desde la raíz del repo, con el venv ya creado (Python 3.14.7 + `homeassistant==2026.9.2`, no recrearlo). Son
**252 pruebas, ~6-7 minutos** en foreground. **No lo corras en background**: un pytest backgroundeado a medias ya
hizo perder una corrida completa en este proyecto (el proceso quedó huérfano y el directorio de trabajo se
recicló antes de que terminara). Sin red — todo corre contra `FakeTransport` con fixtures grabadas de Casa
Justice, nunca contra un inversor real.

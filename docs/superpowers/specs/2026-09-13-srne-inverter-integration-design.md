# Diseño — integración `srne_inverter` para Home Assistant (inversores SRNE/BlueSun vía logger Solarman)

Fecha: 2026-09-13 · Autor: Claude con Gabriel · Estado: aprobado en alcance por Gabriel (chat 2026-09-13 ~10:45 CST)

## 1. Problema y objetivo

Gabriel tiene inversores híbridos de 10 kW marca BlueSun (base **SRNE**, firmware `V8.18.006`, mapa Modbus SRNE
"Energy Storage Inverter" V1.96) en varias casas de Nicolás Rowley: **Casa Principal Justice (2, en paralelo
split-phase)**, **Yoga (6)** y otros guardados para un sitio futuro. Cada inversor lleva un **logger WiFi Solarman
LSW-5** (`LSW5_01_2421_SS_00_00.00.00.18`) que habla el protocolo local **V5 en TCP 8899** (Modbus RTU
encapsulado) y sube datos a la nube de Solarman. Hoy el monitoreo depende de esa nube.

Objetivo: **monitoreo y control local desde el Home Assistant de Casa GADI (`172.16.10.12`, HA 2026.9.1)**,
sin nube, con **valores reales por registro** (nada asumido a partir de otro modelo) y tolerante a que las
unidades no sean idénticas (bloques de registros que un firmware no tiene).

Fuera de alcance v1: los 2 inversores SunGoldPower de GADI (ya cubiertos por `inverter-bridge` por RS485;
el logger no daría más datos), transporte RS485/Modbus-TCP directo (queda previsto en la arquitectura),
agregados por banco/sitio (se hacen con templates de HA), HACS oficial.

## 2. Por qué integración propia y no HACS Solarman (davidrapan)

- Obtiene el serial del logger **solo por descubrimiento UDP 48899**, que no responde entre subredes
  (probado desde DevClaude y desde el host de HA; TCP 8899 y la web del logger sí llegan). Habría que parchear.
- Sus perfiles agrupan registros en rangos: un bloque ausente en un firmware tumba la lectura completa; no
  sondea capacidades por unidad.
- Con 8+ unidades heterogéneas conviene tener el mapa, el sondeo y las escrituras bajo control propio.
  Referencias conservadas en `docs/reference/`: perfil upstream `srne_asf.yaml`, perfil custom que
  escribimos (64 ítems, sintaxis verificada contra el código de davidrapan) y `srne_map.py` de inverter-bridge.

## 3. Hechos verificados sobre los equipos (no suponer lo contrario)

- Loggers: un **solo cliente TCP a la vez** en 8899; responden `NoSocketAvailableError` al segundo cliente.
  Reconectan solos tras cortes. IPs por DHCP con lease estático en el MikroTik de cada casa.
- Serial del logger: en `http://<logger>/status.html` (auth básica `admin`/`admin`) como `var cover_mid = "3548208972"`.
  El de Justice inv1 = `3548208972` (`192.168.188.240`, esclavo 1), inv2 = `3548738877` (`.242`, esclavo 2).
- Esclavo Modbus en paralelo: host = 1, segundo = 2 (el logger del inv2 responde `Empty`/`AcknowledgeError` con esclavo 1).
- Bloques que responden en `V8.18.006`: `0x0014` (10), `0x0100` (15), `0x0200–0x023F`, `0xE000–0xE02F`,
  `0xE200–0xE21E`, `0xF02C–0xF043`. **No existen**: `0x0112+` (bloque BMS), `0xE21F+`.
  Escrituras rechazadas por el inversor (IllegalDataValue): `E20F`, `E21D`, `E039`, `E20B`; `E21B` acepta 0–15.
  🔴 **Corrección 2026-09-14** (prueba en vivo, Task 17): `0xE03A+` marcado "no existe" arriba era **incorrecto**
  — lectura en vivo de `0xE03A` devuelve `[0, 0]` (presente, en cero) y `0xE100`/`0xE116`/`0xE121` devuelven datos
  estructurados no-cero (`[0,10,9,65523]`, `[45,0,100,0]`, `[200,10,3750,5]`). Todo el rango `0xE03A–0xE12F` está
  **PRESENTE**; `0x0112+` y `0xE21F+` sí se confirmaron ausentes (IllegalDataAddress real). Ver
  `tests/fake_transport.py`'s `DEFAULT_UNSUPPORTED` y `services.py`'s `KNOWN_ABSENT_ON_THIS_FIRMWARE`, corregidos.
- Semántica confirmada en sitio: `0x0102` corriente de batería **negativa = cargando** (se normaliza a
  positivo = carga); `0x0103` temperatura de batería devuelve 0 con BMS en PYL; `0x010B` estado de carga
  (1 = quick/CC, 2 = CV, 4 = float, 6 = activación Li, 8 = full); `0x0210` estado (2 = AC bypass, 3 = inversor);
  `0x021E` corriente de carga desde red (×0,1 A) — el dato clave; umbrales E005–E00E y E01B/E022 en
  "equivalente 12 V" (`raw/10×4` V); `E204` prioridad de salida 0 SOL / 1 UTI / 2 SBU / **3 SUB** (con UTI el
  cargador de red queda en ~1 A — causa del caso de Justice); `E215` BMS 0 SLA / 1 RS485 / 2 CAN; `E21B`
  protocolo (8 = PYL); `E004` tipo (0 USER … 6 L16); `E201` paralelo; `E21E` fase.
  Con BMS activo (`E215 = 1`, el estado normal deliberado en Justice), el firmware **rechaza** escrituras a
  `E007`/`E008`/`E009` (equalize/boost/float) los TRES, no solo float como se pensaba antes de la prueba en
  vivo — `IllegalDataValue`, confirmado 2026-09-14 (ver `transport/base.py`'s `InvalidRegisterValueError`).
  🔴 **Corrección 2026-09-14**: `0x0018–0x001B`, mapeado como "SN inversor" en la arquitectura (§4) y como
  `inverter_serial` en `registers.py`, **no es un serial**. En vivo: inv1 `[0, 0, 1, 45]`, inv2 `[0, 0, 2, 45]`
  — la tercera palabra es el esclavo Modbus (1/2), coincide exactamente con `rs485_address` (`0xE200`). Renombrado
  a `device_info_tail` (`FieldKind.HEX_WORDS`, ya no `SERIAL`). El SN real del inversor sigue sin conocerse; el
  identificador de dispositivo en HA usa el SN del **logger** (`CONF_SERIAL`, de la config entry), no este registro.
  🔴 **Corrección 2026-09-14**: `grid_current_l2` mapeado a `0x022B` (decisión de diseño 1) estaba **mal** — en
  bypass CA (machine_state=2) leía 0,0 A en ambas unidades mientras la carga real era 15,5/16,9 A; `0x0238` sí
  seguía la corriente de carga (15,6/17,3 A), como corresponde a corriente de red en bypass. Movido a `0x0238`;
  `0x022B` queda sin mapear (significado desconocido).
- Lecturas: ~0,3 s por bloque por logger; usar pausas cortas entre bloques y un solo lock por conexión.

## 4. Arquitectura

```
custom_components/srne_inverter/
  manifest.json          domain srne_inverter, requirements ["pysolarmanv5>=3.0.6"], iot_class local_polling
  const.py               DOMAIN, CONF_*, defaults (port 8899, slave 1, scan 10 s, warm 60 s, cold 300 s)
  registers.py           MAPA: Block(addr, size, name, tier) + Field(block, offset, key, scale, signed, unit,
                         device_class, state_class, enum, writable spec) — derivado de inverter-bridge/srne_map.py
                         + lo aprendido en Justice. ÚNICA fuente de verdad; la CLI y los tests lo importan.
  transport/
    base.py              Protocolo Transport: connect/close/read_holding(addr, n)/write_holding(addr, value)
    solarman_v5.py       PySolarmanV5Async (host, serial, port, slave, socket_timeout) + lock + reconexión
    (futuro) modbus_tcp.py / modbus_rtu.py
  coordinator.py         DataUpdateCoordinator: sondeo inicial por bloque → SupportMap; lecturas por tier
                         (HOT cada scan, WARM cada 60 s, COLD cada 300 s); decodificación; backoff; unavailable
  config_flow.py         UI: nombre, host, puerto, serial (opcional: "leer del logger" vía status.html),
                         esclavo, intervalo; validación = conectar + leer 0x0100/0x0014; unique_id = serial.
                         Options flow: intervalos, "conexión habilitada".
  entity.py              base: DeviceInfo (serial LOGGER (config entry) + 0x0018 "device_info_tail",
                         NO es SN del inversor -- corrección 2026-09-14, ver §3; fw 0x0014), disponibilidad
  sensor.py / select.py / number.py / switch.py / button.py / binary_sensor.py / diagnostics.py
  services.yaml + services: read_register, write_register (raw, con readback), reprobe
  translations/en.json, es.json
tools/probe.py           CLI: sondea una unidad (host, serial, esclavo) con el MISMO registers.py y
                         reporta bloques soportados + snapshot decodificado (inventario de las otras unidades)
tests/                   pytest con transporte falso (respuestas grabadas de Justice): decodificación, sondeo,
                         tiers, escrituras con readback, config flow
hacs.json, README.md, CLAUDE.md
```

Decisiones:

1. **Sondeo (probe) al configurar y en cada carga**: cada `Block` se lee una vez; `IllegalDataAddress`/error
   Modbus ⇒ no soportado (se guarda en `entry.runtime_data.support` y en diagnóstico); timeout ⇒ reintento y,
   si persiste, "desconocido" (se reintenta en el siguiente ciclo, sin crear entidades hasta confirmar).
   Solo los campos de bloques soportados crean entidades. Botón/servicio `reprobe` para repetirlo.
2. **Un cliente por logger**: la conexión vive en el coordinator con un `asyncio.Lock`; se mantiene abierta;
   ante error se cierra y reabre con backoff (2, 5, 15, 60 s). `switch.<inv>_connection` (o servicio
   `pause`) cierra el socket y detiene el polling para dejar el logger libre a herramientas externas.
3. **Escrituras**: `select`/`number` escriben un registro y **releen** el mismo registro; si no coincide, se
   lanza `HomeAssistantError` con el valor leído. Ningún valor se "asume" escrito. Las escrituras que el
   firmware rechaza quedan documentadas y las entidades correspondientes son de solo lectura.
4. **Normalizaciones explícitas y documentadas** en `registers.py` (signo de corriente, ×0,4 V, enums).
   Los raw se conservan en atributos/diagnóstico.
5. **Tiers**: HOT = 0x0100, 0x0200–0x023F; WARM = E000–E02F, E200–E21E; COLD = 0x0014, F02C–F043.
6. **Multi-entrada**: N entries independientes (una por logger); nombre de entry libre ("Justice Inv 1",
   "Yoga Inv 3"); `device_info` con `identifiers={(DOMAIN, serial)}`, modelo y firmware leídos.
7. **Compatibilidad**: HA ≥ 2025.6 (runtime_data, DataUpdateCoordinator moderno), Python 3.13.

## 5. Entidades v1 (por inversor)

Sensores: SOC, V bat, I bat (+carga), T bat, estado de carga (enum), potencia bat (calc), estado máquina
(enum), bus V, red L1/L2 V, I, Hz, salida L1/L2 V, I, Hz, carga L1/L2 W, VA, % carga, I carga desde red,
T DC-DC / inversor / trafo, fallas F1–F4 (raw hex) + `binary_sensor` "falla activa", contadores hoy/total
(Ah carga/descarga, kWh carga, Ah desde red, días), firmware, SN.
Selects: E204 prioridad salida, E20F prioridad carga (solo lectura si el firmware rechaza — verificado que
rechaza en Justice: entonces sensor), E004 tipo batería, E215 comunicación BMS, E21B protocolo BMS, E025 modo
límite BMS.
Numbers: E205 I carga AC (0–120 A), E20A I carga máx (0–200), E01C I fin de carga (0–10), E01D/E00F/E01E/
E01F/E020 SOC %, E008/E009/E00A/E00B/E00C/E00D/E00E voltajes (paso 0,4 V; el inversor los rechaza salvo
tipo USER — se informa el error).
Switch: conexión habilitada. Button: re-sondear. Diagnostics: raw por bloque + mapa de soporte + config.

## 6. Pruebas y despliegue

- Unit tests con transporte falso alimentado con los crudos de Justice (`Redes-Clientes/CasaJustice/evidencia/*.json`).
- Prueba en vivo contra Justice inv1/inv2 (parar antes cualquier herramienta que use el logger): lectura completa,
  sondeo, una escritura inocua con readback (p. ej. `E01E` 15→16→15).
- Despliegue: `scp -r custom_components/srne_inverter GADI-HomeAssistant:/config/custom_components/` +
  `ha core restart`; alta de 2 entries por config flow (UI o WS); vista "Inversores Justice" en `dashboard-justice`.
- Docs: README (instalación HACS custom repo / manual), CLAUDE.md del proyecto, fila en el CLAUDE.md raíz,
  `INTEGRATIONS.md` (contrato de entidades), puntero desde `Redes-Clientes/CasaJustice/CLAUDE.md`.

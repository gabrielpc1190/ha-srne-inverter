"""Constants for the srne_inverter integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "srne_inverter"

CONF_SERIAL = "serial"
CONF_SLAVE_ID = "slave_id"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_WARM_INTERVAL = "warm_interval"
CONF_COLD_INTERVAL = "cold_interval"
CONF_CONNECTION_ENABLED = "connection_enabled"

DEFAULT_PORT = 8899
DEFAULT_SLAVE_ID = 1
DEFAULT_SCAN_INTERVAL = 10
DEFAULT_WARM_INTERVAL = 60
DEFAULT_COLD_INTERVAL = 300

SOCKET_TIMEOUT = 10.0
BLOCK_PAUSE = 0.25
WRITE_SETTLE = 0.4
BACKOFF_SECONDS = (2.0, 5.0, 15.0, 60.0)
REPROBE_AFTER_FAILURES = 3

MANUFACTURER = "SRNE"
DEFAULT_MODEL = "Energy Storage Inverter"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

SIGNAL_NEW_ENTITIES = "srne_inverter_new_entities_{entry_id}"

SERVICE_READ_REGISTER = "read_register"
SERVICE_WRITE_REGISTER = "write_register"
SERVICE_REPROBE = "reprobe"

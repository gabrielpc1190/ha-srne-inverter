"""Solarman V5 (LSW-5 WiFi logger, TCP 8899) transport.

One logger accepts exactly ONE TCP client, so a single connection is held open
and every request is serialised through an asyncio.Lock.

THIS MODULE MUST NOT IMPORT homeassistant.
"""

from __future__ import annotations

import asyncio
import logging

from pysolarmanv5 import NoSocketAvailableError, PySolarmanV5Async, V5FrameError
from umodbus.exceptions import (
    IllegalDataAddressError,
    IllegalDataValueError,
    ModbusError,
)

from .base import (
    InvalidRegisterValueError,
    TransportBusyError,
    TransportConnectionError,
    TransportProtocolError,
    TransportTimeoutError,
    UnsupportedRegisterError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 8899
DEFAULT_TIMEOUT = 10.0


class SolarmanV5Transport:
    """Async Modbus-over-Solarman-V5 transport for a single logger.

    Connection-ownership contract (see transport.base.Transport): the CALLER
    owns connect() and close(). This class never opens a socket except from
    its own public connect() method -- read_holding/write_holding only ever
    use the client connect() already created, or raise TransportConnectionError
    if there is none. That is what makes "MUST NOT reconnect after an explicit
    close()" trivially true here: there is no code path, anywhere but
    connect(), that can create a new client.
    """

    def __init__(
        self,
        host: str,
        serial: int,
        *,
        port: int = DEFAULT_PORT,
        slave_id: int = 1,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.serial = serial
        self.port = port
        self.slave_id = slave_id
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self._client: PySolarmanV5Async | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        if self._client is not None:
            return
        client = PySolarmanV5Async(
            self.host,
            self.serial,
            port=self.port,
            mb_slave_id=self.slave_id,
            socket_timeout=self.timeout,
            auto_reconnect=False,
            verbose=False,
        )
        try:
            async with asyncio.timeout(self.timeout):
                await client.connect()
        except Exception as err:
            raise _translate(err) from err
        self._client = client
        _LOGGER.debug("Connected to logger %s (%s)", self.serial, self.host)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - closing must never raise
            _LOGGER.debug("Ignoring error while closing %s", self.host, exc_info=True)

    async def read_holding(self, addr: int, count: int) -> list[int]:
        async with self.lock:
            client = self._require_client()
            try:
                async with asyncio.timeout(self.timeout):
                    return list(
                        await client.read_holding_registers(
                            register_addr=addr, quantity=count
                        )
                    )
            except Exception as err:
                raise _translate(err) from err

    async def write_holding(self, addr: int, value: int) -> None:
        async with self.lock:
            client = self._require_client()
            try:
                async with asyncio.timeout(self.timeout):
                    await client.write_holding_register(
                        register_addr=addr, value=value
                    )
            except Exception as err:
                raise _translate(err) from err

    def _require_client(self) -> PySolarmanV5Async:
        if self._client is None:
            raise TransportConnectionError("transport is not connected")
        return self._client


def _translate(err: Exception) -> Exception:
    """Map pysolarmanv5 / umodbus / socket errors onto the transport taxonomy.

    Order matters for one pair: builtin TimeoutError is an OSError subclass
    on this interpreter (verified on 3.14 -- and true since 3.10, since
    asyncio.TimeoutError became an alias of the builtin TimeoutError), so the
    TimeoutError check MUST run before the generic OSError branch, or every
    timeout would be misreported as a plain TransportConnectionError instead
    of the more specific TransportTimeoutError the coordinator needs.
    """
    if isinstance(err, IllegalDataAddressError):
        return UnsupportedRegisterError(str(err) or "illegal data address")
    if isinstance(err, IllegalDataValueError):
        return InvalidRegisterValueError(str(err) or "illegal data value")
    if isinstance(err, NoSocketAvailableError):
        # This hardware accepts exactly one TCP client. pysolarmanv5 raises
        # this both when connect() itself is refused/fails for any reason
        # (verified in pysolarmanv5_async.py: PySolarmanV5Async.connect()
        # wraps every exception -- refusal, timeout, DNS failure, anything
        # -- into this one type) and when a request finds the connection
        # already gone underneath it. "Someone/something else has the
        # logger" is by far the most common real cause (a leftover
        # justice_watch.py, a second config entry on the same IP, Solarman's
        # own cloud client), which is why this maps to the specific
        # TransportBusyError rather than the generic connection error --
        # the coordinator needs to tell "busy" apart from "timed out" to
        # avoid backing off forever with nothing useful to tell the user.
        return TransportBusyError(str(err) or "no socket available")
    if isinstance(err, (TimeoutError, asyncio.TimeoutError)):
        return TransportTimeoutError(str(err) or "request timed out")
    if isinstance(err, V5FrameError):
        return TransportProtocolError(str(err) or "malformed V5 frame")
    if isinstance(err, ModbusError):
        # Any other Modbus exception response (wrong function code, wrong
        # slave id, device busy at the Modbus layer, gateway errors): none
        # of these has a dedicated type in our taxonomy, so surface it as a
        # protocol-level problem rather than silently folding it into one of
        # the two specific cases above.
        return TransportProtocolError(f"{type(err).__name__}: {err}")
    if isinstance(err, OSError):
        # Refused or otherwise-failed at the socket level, and NOT a
        # timeout (that case is handled above): connection reset, network
        # unreachable, etc.
        return TransportConnectionError(f"{type(err).__name__}: {err}")
    return TransportProtocolError(f"{type(err).__name__}: {err}")

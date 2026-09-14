"""The Solarman V5 transport maps library errors onto our taxonomy."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pysolarmanv5 import NoSocketAvailableError, V5FrameError
from umodbus.exceptions import IllegalDataAddressError, IllegalDataValueError

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportConnectionError,
    TransportProtocolError,
    UnsupportedRegisterError,
)
from custom_components.srne_inverter.transport.solarman_v5 import (
    SolarmanV5Transport,
)

TARGET = "custom_components.srne_inverter.transport.solarman_v5.PySolarmanV5Async"


@pytest.fixture(name="client")
def client_fixture():
    with patch(TARGET) as factory:
        client = factory.return_value
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.read_holding_registers = AsyncMock(return_value=[1, 2, 3])
        client.write_holding_register = AsyncMock(return_value=7)
        yield client


async def test_connect_builds_client_with_the_right_arguments(client):
    transport = SolarmanV5Transport("192.168.188.240", 3548208972, slave_id=2)
    await transport.connect()
    assert transport.connected is True
    with patch(TARGET) as factory:
        pass
    client.connect.assert_awaited_once()


async def test_read_holding_returns_values(client):
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    assert await transport.read_holding(0x0100, 3) == [1, 2, 3]
    client.read_holding_registers.assert_awaited_once_with(
        register_addr=0x0100, quantity=3
    )


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (IllegalDataAddressError(), UnsupportedRegisterError),
        (IllegalDataValueError(), InvalidRegisterValueError),
        (NoSocketAvailableError("busy"), TransportConnectionError),
        (V5FrameError("bad frame"), TransportProtocolError),
        (OSError("reset"), TransportConnectionError),
        (asyncio.TimeoutError(), TransportConnectionError),
    ],
)
async def test_read_error_mapping(client, raised, expected):
    client.read_holding_registers.side_effect = raised
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    with pytest.raises(expected):
        await transport.read_holding(0x0100, 1)


async def test_write_error_mapping(client):
    client.write_holding_register.side_effect = IllegalDataValueError()
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    with pytest.raises(InvalidRegisterValueError):
        await transport.write_holding(0xE20F, 2)


async def test_connect_failure_maps_to_connection_error(client):
    client.connect.side_effect = NoSocketAvailableError("one client only")
    transport = SolarmanV5Transport("h", 1)
    with pytest.raises(TransportConnectionError):
        await transport.connect()
    assert transport.connected is False


async def test_operations_are_serialised_by_the_lock(client):
    order: list[str] = []

    async def slow_read(register_addr, quantity):
        order.append(f"start-{register_addr}")
        await asyncio.sleep(0.01)
        order.append(f"end-{register_addr}")
        return [0] * quantity

    client.read_holding_registers.side_effect = slow_read
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    await asyncio.gather(
        transport.read_holding(0x0100, 1), transport.read_holding(0x0200, 1)
    )
    assert order in (
        ["start-256", "end-256", "start-512", "end-512"],
        ["start-512", "end-512", "start-256", "end-256"],
    )


async def test_close_is_idempotent(client):
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    await transport.close()
    await transport.close()
    assert transport.connected is False
    client.disconnect.assert_awaited_once()


async def test_read_after_close_raises_without_reconnecting(client):
    """The pause-switch contract: close() must free the logger for good.

    A read after an explicit close(), with no intervening connect(), must
    raise TransportConnectionError -- and must NOT silently open a new
    socket to satisfy the read. The single connect() count proves no
    reconnect attempt was made.
    """
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    await transport.close()
    with pytest.raises(TransportConnectionError):
        await transport.read_holding(0x0100, 1)
    client.connect.assert_awaited_once()


async def test_write_after_close_raises_without_reconnecting(client):
    """Same pause-switch contract as above, for write_holding."""
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    await transport.close()
    with pytest.raises(TransportConnectionError):
        await transport.write_holding(0x0100, 1)
    client.connect.assert_awaited_once()


async def test_read_before_first_connect_raises(client):
    """Never-connected is the same contract as connected-then-closed."""
    transport = SolarmanV5Transport("h", 1)
    with pytest.raises(TransportConnectionError):
        await transport.read_holding(0x0100, 1)
    client.read_holding_registers.assert_not_awaited()

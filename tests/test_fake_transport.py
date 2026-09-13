"""The fake transport must behave like the real logger, including its errors."""

import pytest

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportConnectionError,
    UnsupportedRegisterError,
)
from tests.fake_transport import FakeTransport


async def test_read_returns_recorded_values(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    assert await transport.read_holding(0x0100, 3) == [55, 531, 65527]
    assert transport.reads == [(0x0100, 3)]


async def test_read_of_absent_block_raises_unsupported(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(UnsupportedRegisterError):
        await transport.read_holding(0x0112, 4)
    with pytest.raises(UnsupportedRegisterError):
        await transport.read_holding(0xE21F, 2)


async def test_write_then_read_back(justice_registers):
    transport = FakeTransport(justice_registers)
    await transport.connect()
    await transport.write_holding(0xE01E, 16)
    assert await transport.read_holding(0xE01E, 1) == [16]
    assert transport.writes == [(0xE01E, 16)]


async def test_write_to_rejected_register_raises(justice_registers):
    """E20F/E20B/E21D are rejected by the real firmware."""
    transport = FakeTransport(justice_registers)
    await transport.connect()
    with pytest.raises(InvalidRegisterValueError):
        await transport.write_holding(0xE20F, 2)


async def test_connect_failure_is_injectable(justice_registers):
    transport = FakeTransport(justice_registers, fail_connect=True)
    with pytest.raises(TransportConnectionError):
        await transport.connect()


async def test_read_errors_are_injectable_once(justice_registers):
    transport = FakeTransport(
        justice_registers, read_errors=[TransportConnectionError("boom"), None]
    )
    await transport.connect()
    with pytest.raises(TransportConnectionError):
        await transport.read_holding(0x0100, 1)
    assert await transport.read_holding(0x0100, 1) == [55]

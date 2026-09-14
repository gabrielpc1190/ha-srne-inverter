"""The Solarman V5 transport maps library errors onto our taxonomy."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pysolarmanv5 import NoSocketAvailableError, V5FrameError
from umodbus.exceptions import IllegalDataAddressError, IllegalDataValueError

from custom_components.srne_inverter.transport.base import (
    InvalidRegisterValueError,
    TransportBusyError,
    TransportConnectionError,
    TransportProtocolError,
    TransportTimeoutError,
    UnsupportedRegisterError,
)
from custom_components.srne_inverter.transport.solarman_v5 import (
    SolarmanV5Transport,
)

TARGET = "custom_components.srne_inverter.transport.solarman_v5.PySolarmanV5Async"


@pytest.fixture(name="factory")
def factory_fixture():
    with patch(TARGET) as factory:
        client = factory.return_value
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.read_holding_registers = AsyncMock(return_value=[1, 2, 3])
        client.write_holding_register = AsyncMock(return_value=7)
        yield factory


@pytest.fixture(name="client")
def client_fixture(factory):
    return factory.return_value


async def test_connect_builds_client_with_the_right_arguments(factory, client):
    """Also pins auto_reconnect=False -- flipping it to True silently
    restores the library's own reconnect-after-drop behaviour, which is
    exactly the pause-switch violation this module exists to prevent.
    """
    transport = SolarmanV5Transport("192.168.188.240", 3548208972, slave_id=2)
    await transport.connect()
    assert transport.connected is True
    factory.assert_called_once_with(
        "192.168.188.240",
        3548208972,
        port=8899,
        mb_slave_id=2,
        socket_timeout=10.0,
        auto_reconnect=False,
        verbose=False,
    )
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
    """A connect-phase NoSocketAvailableError must be the PLAIN base class,
    not TransportBusyError -- see test_read_busy_error_is_the_specific_
    subclass for the read/write-phase case, which IS Busy. This is the
    phase-aware split from Finding 2: pysolarmanv5's connect() wraps every
    failure (refused, unreachable, DNS failure) into this one exception
    type, and none of those connect-phase causes mean "someone else has the
    logger" -- only a request finding an established session gone means
    that.
    """
    client.connect.side_effect = NoSocketAvailableError("one client only")
    transport = SolarmanV5Transport("h", 1)
    with pytest.raises(TransportConnectionError) as exc_info:
        await transport.connect()
    assert type(exc_info.value) is TransportConnectionError
    assert transport.connected is False


async def test_read_busy_error_is_the_specific_subclass(client):
    """Pins the phase-aware split's other half, and mutation M1 (Busy
    flattened back to plain TransportConnectionError): a read/write finding
    NoSocketAvailableError means our established session was taken, which is
    specifically TransportBusyError, not just any TransportConnectionError.
    """
    client.read_holding_registers.side_effect = NoSocketAvailableError("taken")
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    with pytest.raises(TransportBusyError) as exc_info:
        await transport.read_holding(0x0100, 1)
    assert type(exc_info.value) is TransportBusyError


async def test_read_timeout_is_the_specific_subclass(client):
    """Pins mutation M1 (Timeout flattened back to plain
    TransportConnectionError) AND M2 (generic OSError branch checked before
    TimeoutError -- TimeoutError IS an OSError subclass on this interpreter,
    so swapping the branch order would make this same scenario resolve to
    the wrong, generic type) AND M3 (the asyncio.timeout guards deleted --
    without them nothing would ever raise here at all within the bound
    below, and the test would fail by timing out itself rather than getting
    a wrong exception type).
    """

    async def hang(**kwargs):
        await asyncio.sleep(0.3)

    client.read_holding_registers.side_effect = hang
    transport = SolarmanV5Transport("h", 1, timeout=0.01)
    await transport.connect()
    with pytest.raises(TransportTimeoutError) as exc_info:
        await transport.read_holding(0x0100, 1)
    assert type(exc_info.value) is TransportTimeoutError


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


async def test_atomic_brackets_write_and_readback_without_deadlock(client):
    """Finding 1: the brief published `.lock` "for callers that need to
    bracket a write+read-back" -- but read_holding/write_holding take that
    same non-reentrant lock themselves, so doing exactly that under
    `async with transport.lock:` deadlocks forever. `atomic()` is the fix:
    it acquires the private lock once and yields un-locked operations, so
    a write followed by its own read-back completes instead of hanging.
    The 1s bound turns "hangs forever" into "this test fails" instead of
    "this test never finishes" if that regresses.
    """
    client.write_holding_register.return_value = 42
    client.read_holding_registers.return_value = [42]
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()

    async with asyncio.timeout(1.0):
        async with transport.atomic() as t:
            await t.write_holding(0x0100, 42)
            result = await t.read_holding(0x0100, 1)

    assert result == [42]
    client.write_holding_register.assert_awaited_once_with(
        register_addr=0x0100, value=42
    )
    client.read_holding_registers.assert_awaited_once_with(
        register_addr=0x0100, quantity=1
    )


async def test_atomic_and_plain_read_share_the_same_lock(client):
    """Proves atomic() and read_holding/write_holding serialise against each
    other, not just against their own kind -- i.e. atomic() is not a second,
    independent lock that would let a concurrent plain read_holding slip in
    mid-transaction.
    """
    order: list[str] = []
    write_started = asyncio.Event()

    async def slow_write(**kwargs):
        order.append("atomic-write-start")
        write_started.set()
        await asyncio.sleep(0.02)
        order.append("atomic-write-end")
        return kwargs["value"]

    async def instant_read(**kwargs):
        order.append("read-executed")
        return [9]

    client.write_holding_register.side_effect = slow_write
    client.read_holding_registers.side_effect = instant_read
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()

    async def do_atomic():
        async with transport.atomic() as t:
            await t.write_holding(0x0100, 1)

    async def do_read():
        await write_started.wait()
        await transport.read_holding(0x0200, 1)

    await asyncio.gather(do_atomic(), do_read())
    assert order == ["atomic-write-start", "atomic-write-end", "read-executed"]


async def test_connected_reflects_a_session_that_died_in_the_background(client):
    """pysolarmanv5's own reader task nulls .reader/.writer when the
    connection dies without us noticing via a failed read/write (see
    pysolarmanv5_async.py's _conn_keeper). `connected` must reflect that
    immediately, not just after the next failed request finds out the hard
    way.
    """
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    assert transport.connected is True
    client.reader = None  # simulate _conn_keeper detecting a dead socket
    assert transport.connected is False


async def test_connect_rebuilds_a_dead_session_instead_of_a_silent_no_op():
    """Before this fix, connect() early-returned as a no-op whenever
    `_client is not None`, regardless of whether the session behind it was
    still alive -- so a coordinator's backoff-and-retry loop calling
    connect() again after a drop got an instant no-op and never actually
    reconnected. Uses two distinct client mocks (via side_effect) because
    the fix must build and connect a genuinely NEW client, not reuse the
    dead one.
    """
    first = AsyncMock()
    first.connect = AsyncMock()
    first.disconnect = AsyncMock()
    second = AsyncMock()
    second.connect = AsyncMock()

    with patch(TARGET, side_effect=[first, second]):
        transport = SolarmanV5Transport("h", 1)
        await transport.connect()
        assert transport.connected is True  # first.reader is an auto-mock: truthy

        first.reader = None  # the background reader task found the socket dead
        assert transport.connected is False

        await transport.connect()  # must not be a silent no-op

        assert transport.connected is True  # now backed by `second`
        first.disconnect.assert_awaited_once()  # the stale client was discarded
        second.connect.assert_awaited_once()  # a genuinely new session was opened


async def test_connect_timeout_discards_the_abandoned_client(client):
    """Finding 6: a client abandoned after a connect timeout must be
    disconnect()ed, not dropped on the floor -- an unclosed socket would sit
    on the logger's one TCP slot until garbage collection, manufacturing a
    self-inflicted "busy" on the very next attempt.
    """

    async def hang():
        await asyncio.sleep(10)

    client.connect.side_effect = hang
    transport = SolarmanV5Transport("h", 1, timeout=0.01)
    with pytest.raises(TransportTimeoutError):
        await transport.connect()
    client.disconnect.assert_awaited_once()
    assert transport.connected is False


async def test_close_waits_for_an_in_flight_read_before_disconnecting(client):
    """Finding 4: close() must not cancel an in-flight read/write out from
    under it. With the real library, doing so leaves that read parked on a
    queue nothing will ever feed, so it silently eats the full `timeout` and
    reports itself as a spurious TransportTimeoutError -- mislabelling every
    deliberate pause as a network fault. close() now takes the same lock, so
    it waits for the in-flight operation to finish on its own terms first.
    """
    order: list[str] = []
    read_started = asyncio.Event()

    async def slow_read(**kwargs):
        order.append("read-start")
        read_started.set()
        await asyncio.sleep(0.02)
        order.append("read-end")
        return [0]

    client.read_holding_registers.side_effect = slow_read
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()

    async def do_close():
        await read_started.wait()
        order.append("close-start")
        await transport.close()
        order.append("close-end")

    await asyncio.gather(transport.read_holding(0x0100, 1), do_close())
    assert order == ["read-start", "close-start", "read-end", "close-end"]
    assert transport.connected is False

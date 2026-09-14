"""The Solarman V5 transport maps library errors onto our taxonomy."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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


def _live_reader_task() -> MagicMock:
    """A plain (non-async) stand-in for asyncio.Task.

    `reader_task` on the real PySolarmanV5Async is a genuine asyncio.Task,
    whose `.done()` is a SYNCHRONOUS method. Leaving it as the default
    auto-attribute on an AsyncMock/MagicMock-based client is a trap:
    `client.reader_task` would be a truthy mock, and `.done()` on THAT would
    return another truthy mock (or, on an AsyncMock, an unawaited coroutine)
    -- both read as "done" under `if ...: return False` in `connected`,
    making every connected client look dead. This gives each fixture client
    a `reader_task` whose `.done()` returns a real `False`, matching a
    healthy, still-running reader loop.
    """
    task = MagicMock()
    task.done.return_value = False
    return task


@pytest.fixture(name="factory")
def factory_fixture():
    with patch(TARGET) as factory:
        client = factory.return_value
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.read_holding_registers = AsyncMock(return_value=[1, 2, 3])
        client.write_holding_register = AsyncMock(return_value=7)
        client.reader_task = _live_reader_task()
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
    first.reader_task = _live_reader_task()
    second = AsyncMock()
    second.connect = AsyncMock()
    second.reader_task = _live_reader_task()

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


# --- Fix round 2 (re-review): atomic() handle invalidation, connect()/close()
# races, bounded discard, cause preservation, and the reader-task death mode
# _conn_keeper's own tail misses. -----------------------------------------


async def test_atomic_handle_is_invalidated_after_the_block_exits(client):
    """Finding 2 (re-review): the object atomic() yields must stop working
    the instant the block exits. Before this fix, a caller that stashed the
    handle (or a helper that returned it) could keep calling read_holding/
    write_holding through it afterward, completely outside the lock -- e.g.
    a write executed in the middle of an unrelated in-flight read, on a
    client whose data_queue holds exactly one response.
    """
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    async with transport.atomic() as t:
        await t.write_holding(0x0100, 1)
    with pytest.raises(RuntimeError):
        await t.write_holding(0x0100, 2)
    with pytest.raises(RuntimeError):
        await t.read_holding(0x0100, 1)


async def test_atomic_handle_is_invalidated_even_if_the_block_raises(client):
    """Same as above, but through the exception exit path -- atomic()'s
    `finally` must invalidate the handle regardless of how the block ends.
    """
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    with pytest.raises(ValueError):
        async with transport.atomic() as t:
            raise ValueError("caller's own compare-and-raise")
    with pytest.raises(RuntimeError):
        await t.read_holding(0x0100, 1)


async def test_close_racing_an_in_flight_connect_still_wins(client):
    """Finding B / Item 3 (re-review): connect() now shares the same lock as
    close(), so a pause landing mid-reconnect cannot find `_client is None`,
    return immediately, and then have the in-flight connect() assign its
    client afterward -- which would leave the logger held after an explicit
    pause, the exact contract this transport exists to guarantee.
    """
    connect_started = asyncio.Event()

    async def slow_connect():
        connect_started.set()
        await asyncio.sleep(0.02)

    client.connect.side_effect = slow_connect
    transport = SolarmanV5Transport("h", 1)

    async def do_close():
        await connect_started.wait()
        await transport.close()

    await asyncio.gather(transport.connect(), do_close())
    assert transport.connected is False
    client.disconnect.assert_awaited_once()


async def test_overlapping_connects_do_not_leak_a_socket(client):
    """Finding B / Item 3 (re-review): two connect() calls racing (e.g. a
    manual reload during a backoff retry) must not both pass the "am I
    connected" check and each build/connect their own client -- that opens
    two sockets to a device with exactly one slot and leaks the first. A
    deliberate delay inside connect() forces the interleaving window every
    run, rather than leaving the race's outcome up to scheduler luck.
    """

    async def slow_connect():
        await asyncio.sleep(0.02)

    client.connect.side_effect = slow_connect
    transport = SolarmanV5Transport("h", 1)
    await asyncio.gather(transport.connect(), transport.connect())
    assert transport.connected is True
    # The second call, serialised behind the same lock, must see
    # `self.connected` already True once it gets its turn, and return
    # without building or connecting anything a second time.
    client.connect.assert_awaited_once()


async def test_connect_does_not_hang_when_discarding_a_stalled_stale_client(client):
    """Finding C / Item 4 (re-review): _discard_client's `await
    client.disconnect()` used to have no bound of its own, so a stalled
    disconnect() (a half-open socket sitting on TCP retransmits) could hang
    connect()'s stale-client recovery path forever -- the coordinator's
    backoff loop would never receive the error it needs in order to back off
    from anything; it would just stop.
    """

    async def hang():
        await asyncio.sleep(30)

    async def fake_connect():
        # A real connect() populates .reader afresh; the shared mock needs
        # this spelled out or it would keep reporting the .reader=None we
        # set below even after a "successful" second connect() call.
        client.reader = MagicMock()

    client.connect.side_effect = fake_connect
    client.disconnect.side_effect = hang
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    client.reader = None  # the session died in the background

    async with asyncio.timeout(3.0):  # DISCARD_TIMEOUT (2.0s) plus margin
        await transport.connect()  # must not hang on the stalled discard

    assert transport.connected is True
    assert client.connect.await_count == 2


async def test_connect_failure_path_does_not_hang_when_discard_stalls(client):
    """Same bound, the OTHER call site: connect()'s except-branch discards
    the client it just failed to connect. If THAT disconnect() also stalls,
    connect() must still surface the original connect failure within a
    bounded time, not hang indefinitely.
    """

    async def hang_connect():
        await asyncio.sleep(30)

    async def hang_disconnect():
        await asyncio.sleep(30)

    client.connect.side_effect = hang_connect
    client.disconnect.side_effect = hang_disconnect
    transport = SolarmanV5Transport("h", 1, timeout=0.01)

    async with asyncio.timeout(3.0):  # connect's own 0.01s + DISCARD_TIMEOUT
        with pytest.raises(TransportTimeoutError):
            await transport.connect()


async def test_close_does_not_hang_when_disconnect_stalls(client):
    """Same bound, close()'s own use of _discard_client -- pre-existing
    exposure the review flagged as no worse than before, fixed for free by
    bounding _discard_client itself rather than each call site separately.
    """

    async def hang():
        await asyncio.sleep(30)

    client.disconnect.side_effect = hang
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()

    async with asyncio.timeout(3.0):
        await transport.close()

    assert transport.connected is False


async def test_connect_failure_message_preserves_the_cause_and_errno(client):
    """Finding 5 (re-review): the re-reviewer's mutation D2 (discard
    err.__cause__ again) stayed green against the round-1 suite -- nothing
    tested that the real errno survives into the error message. It is the
    only thing that distinguishes a refused connection from an unreachable
    host from a DNS failure, which is the entire point of the phase-aware
    mapping (Finding 2).
    """
    cause = OSError("Connection refused")
    cause.errno = 111
    no_socket = NoSocketAvailableError("cannot open connection")
    no_socket.__cause__ = cause
    client.connect.side_effect = no_socket
    transport = SolarmanV5Transport("h", 1)
    with pytest.raises(TransportConnectionError) as exc_info:
        await transport.connect()
    message = str(exc_info.value)
    assert "111" in message, message
    assert "OSError" in message, message


async def test_connected_reflects_a_reader_task_that_died_without_nulling_reader(
    client,
):
    """Finding 6 (re-review, promoted from the "residual" list): pysolarmanv5's
    `_conn_keeper` only nulls `.reader`/`.writer` when its `while True` loop
    RETURNS (a caught ConnectionResetError, or a clean EOF). An ETIMEDOUT (an
    AP reboot, a NAT-table eviction) or ConnectionAbortedError raised from
    `reader.read()` is NOT caught by that narrow except clause -- it escapes
    the loop entirely, so the tail that nulls .reader/.writer never runs,
    even though the task itself is `done()` (with that exception set). This
    is Finding 3 from round 1 surviving on a second, narrower path.
    """
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()
    assert transport.connected is True

    # Simulate the reader task dying from an uncaught exception WITHOUT
    # _conn_keeper's tail (which nulls .reader/.writer) ever running: only
    # reader_task.done() reflects the death, .reader stays exactly as it was.
    dead_task = asyncio.get_running_loop().create_future()
    dead_task.set_result(None)  # only .done() is consulted here
    client.reader_task = dead_task

    assert transport.connected is False


async def test_connect_rebuilds_when_only_the_reader_task_died():
    """Same recovery proof as test_connect_rebuilds_a_dead_session_instead_
    of_a_silent_no_op, but through the reader-task-death path instead of the
    reader-is-None path -- connect() must discard and rebuild either way.
    """
    first = AsyncMock()
    first.connect = AsyncMock()
    first.disconnect = AsyncMock()
    first.reader_task = _live_reader_task()
    second = AsyncMock()
    second.connect = AsyncMock()
    second.reader_task = _live_reader_task()

    with patch(TARGET, side_effect=[first, second]):
        transport = SolarmanV5Transport("h", 1)
        await transport.connect()
        assert transport.connected is True

        dead_task = asyncio.get_running_loop().create_future()
        dead_task.set_result(None)
        first.reader_task = dead_task  # died without nulling .reader

        assert transport.connected is False

        await transport.connect()  # must not be a silent no-op

        assert transport.connected is True  # now backed by `second`
        first.disconnect.assert_awaited_once()
        second.connect.assert_awaited_once()


async def test_cancelling_close_does_not_strand_the_client(client):
    """Finding 7 (re-review, promoted from the "residual" list): close()
    used to null `self._client` BEFORE the `_discard_client` await it was
    about to do, so a cancellation mid-disconnect released the lock but left
    NOTHING referencing the client -- the socket leaked forever, because
    connect()'s "discard a stale client" step only fires when `self._client
    is not None`, and it no longer was. close() now nulls `self._client`
    only AFTER the discard returns, so a cancellation leaves the client
    still tracked, and a later attempt can retry disconnecting it.
    """
    disconnect_started = asyncio.Event()

    async def hang():
        disconnect_started.set()
        await asyncio.sleep(30)

    client.disconnect.side_effect = hang
    transport = SolarmanV5Transport("h", 1)
    await transport.connect()

    close_task = asyncio.ensure_future(transport.close())
    await disconnect_started.wait()
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    # The client must still be tracked, not silently dropped. There is no
    # public signal for "still tracked but the socket may still be open" --
    # that ambiguity is exactly what a leak would hide -- so this reaches
    # into the private attribute deliberately.
    assert transport._client is client

    # A later close() attempt must retry discarding the SAME stranded
    # client rather than treating it as already gone.
    client.disconnect.side_effect = None
    await transport.close()
    assert transport.connected is False
    assert client.disconnect.await_count == 2  # the cancelled attempt + the retry

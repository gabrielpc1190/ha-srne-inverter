"""Connection switch and reprobe button.

Deviation from the task brief, same root cause `tests/test_init.py` and
`tests/test_entity.py` already document: every test below that calls
`setup_entry()` uses `justice_registers_synthetic_complete`, not the plain
`justice_registers` fixture the brief's tests were written against.
`coordinator.async_probe()` always probes every one of `registers.BLOCKS`'
10 blocks, and the raw Casa Justice capture has real holes inside 5 of
them -- `FakeTransport` raises an uncaught `LookupError` ("fixture gap")
for an address that is neither recorded nor declared `unsupported`, which
`probe()` does not catch. Confirmed by hand: running this file's tests
against the plain fixture, as the brief has them, raises exactly that
`LookupError` out of `setup_entry()` before any switch/button assertion is
ever reached.

Second, more specific deviation, in `test_reprobe_adds_entities_that_appeared`
only: the brief's version filters `justice_registers` down to
`{k: v for k, v in justice_registers.items() if k < 0xF000}` to make the
"meter" block (0xF02C, holding `running_days`) currently absent, then later
adds those keys back via `transport.registers.update(...)` before
re-probing. That leaves the meter block's addresses simply MISSING from the
fake's `registers` dict -- not declared `unsupported` -- which is exactly
the `LookupError` fixture-gap trap above, regardless of which base fixture
is filtered: the very first probe (inside `setup_entry`) tries to read
0xF02C and crashes before the test's own assertions are ever reached, it
does not gracefully classify the block UNKNOWN. Fixed using the same
pattern already reviewed and landed for exactly this "block currently
absent, later reappears" scenario (`tests/test_sensor.py`'s
`test_sensors_of_unsupported_blocks_are_not_created`, `tests/test_entity.py`'s
`test_reprobe_signal_adds_a_newly_supported_entity`): declare the meter
block's address range `unsupported` explicitly, so the first probe
genuinely classifies it UNSUPPORTED (no crash, and a real, defensible
reason `coordinator.supported_field_keys()` excludes `running_days`), then
flip `transport.unsupported` back to the default before re-probing -- the
registers were never removed from `transport.registers` in the first place
(unlike the brief's version, `justice_registers_synthetic_complete` was
never filtered), so nothing needs to be added back.
"""

import asyncio
import time

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.srne_inverter import registers as R
from tests.fake_transport import DEFAULT_UNSUPPORTED, FakeTransport
from tests.test_init import setup_entry


async def test_connection_switch_starts_on(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    await setup_entry(hass, FakeTransport(justice_registers_synthetic_complete))
    assert hass.states.get("switch.justice_inv_1_connection").state == "on"


async def test_turning_the_switch_off_frees_the_logger(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert transport.connected is False
    assert hass.states.get("switch.justice_inv_1_connection").state == "off"


async def test_switch_available_while_paused_and_can_be_turned_back_on(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """THE trap this task exists to fix, pinned directly rather than just
    incidentally: pausing sets `coordinator.last_update_success = False`,
    which would grey out (make `unavailable`) any switch built naively on
    `CoordinatorEntity.available`. A switch that went unavailable the
    instant it was switched off could never be switched back on through the
    UI -- the very control the user just used to pause it. Asserts the
    switch's state is explicitly NOT "unavailable" right after turning off
    (not merely that it happens to equal "off" -- an unavailable entity
    would fail that equality too, but the point of this test is to pin the
    AVAILABILITY contract itself, not one incidental consequence of it),
    that the coordinator really is reporting stale data underneath it at
    that moment (so this isn't passing by accident because nothing actually
    went stale), and that turning it back on from that state actually
    works.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    coordinator = entry.runtime_data.coordinator

    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get("switch.justice_inv_1_connection")
    assert state.state == "off"
    assert state.state != "unavailable"
    assert coordinator.last_update_success is False

    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get("switch.justice_inv_1_connection")
    assert state.state == "on"
    assert transport.connected is True
    assert coordinator.last_update_success is True


async def test_turning_the_switch_back_on_resumes_polling(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    for action in ("turn_off", "turn_on"):
        await hass.services.async_call(
            "switch", action,
            {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
        )
        await hass.async_block_till_done()
    assert transport.connected is True
    assert entry.runtime_data.coordinator.last_update_success is True


async def test_reprobe_button_reruns_the_probe(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    transport.reads.clear()
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.justice_inv_1_reprobe"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert len(transport.reads) >= 10


async def test_reprobe_adds_entities_that_appeared(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    meter = R.block_by_addr(0xF02C)
    transport = FakeTransport(
        justice_registers_synthetic_complete,
        unsupported=DEFAULT_UNSUPPORTED
        + (range(meter.addr, meter.addr + meter.count),),
    )
    await setup_entry(hass, transport)
    assert hass.states.get("sensor.justice_inv_1_total_running_days") is None

    transport.unsupported = DEFAULT_UNSUPPORTED
    await hass.services.async_call(
        "button", "press",
        {"entity_id": "button.justice_inv_1_reprobe"}, blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.justice_inv_1_total_running_days")
    assert state is not None
    assert state.state != "unavailable"


# ---- Fix round 1 (Opus review, task-12-review.md) --------------------------
#
# Five findings, all Important. Items 1-4 are genuine defects in the
# original switch.py/button.py/coordinator.py; item 5 is a coverage gap
# (the coordinator's own pause-vs-connect ordering was already correct, but
# nothing in the suite would have noticed if it regressed). See this task's
# report ("Fix round 1" section) for the falsifiability transcript of each.


async def test_reprobe_button_refuses_while_paused(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Finding 1: `async_reprobe` used to ignore `coordinator.
    connection_enabled` entirely -- `coordinator.async_probe()` has no idea
    the user paused anything and happily calls `transport.connect()`
    regardless. Measured without the guard: pressing reprobe while paused
    silently reopened the logger (10 reads, `transport.connected` back to
    `True`) while the switch itself kept reading "off" and nothing ever
    closed the transport again afterward -- precisely the betrayal this
    switch exists to prevent (Gabriel pauses to hand the logger to
    `tools/probe.py`/`justice_watch.py`, and finds it still held). Pins
    that the button now refuses cleanly instead, and that refusing leaves
    the transport exactly as it was.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    await hass.async_block_till_done()
    assert transport.connected is False
    connect_count_before = transport.connect_count
    reads_before = list(transport.reads)

    with pytest.raises(HomeAssistantError, match="paused"):
        await hass.services.async_call(
            "button", "press",
            {"entity_id": "button.justice_inv_1_reprobe"}, blocking=True,
        )
    await hass.async_block_till_done()

    assert transport.connected is False
    assert transport.connect_count == connect_count_before
    assert transport.reads == reads_before
    assert hass.states.get("switch.justice_inv_1_connection").state == "off"


async def test_reprobe_button_stays_available_while_paused(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Finding 2: the button used to inherit `CoordinatorEntity.available`
    (`coordinator.last_update_success`), so it went `unavailable` the
    instant the connection was paused -- exactly the moment a user is most
    likely to reach for it (to hand the logger to another tool, or to
    check whether it is safe to take back). Measured consequence: `HA`'s
    own service-call target resolution
    (`homeassistant/helpers/service.py`) silently drops an unavailable
    entity before the platform's `async_press` is ever reached -- a
    WARNING logged, nothing else, no exception a caller could act on. The
    override that fixes this (`return True`, same shape as the switch's
    own) defends the OTHER staleness cause named by this same finding
    (any backoff window) identically, since both flow through the same
    `coordinator.last_update_success` flag this override no longer reads.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get("button.justice_inv_1_reprobe")
    assert state.state != "unavailable"


async def test_turning_the_switch_off_returns_without_waiting_for_teardown(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Finding 3: `async_turn_off` used to `await` the coordinator's
    `async_set_connection_enabled(False)` call in full, and that call
    itself used to `await transport.close()` directly -- on the real
    transport, up to ~14 s under lock contention (a 10 s connect plus two
    2 s teardown windows). The service call (this test's own
    `hass.services.async_call(..., blocking=True)`) therefore used to sit
    there for the same duration before returning, contradicting Task 7's
    own contract for this method and this file's own prior docstring
    claim that the switch reads "off" immediately.

    Injects the same technique the reviewer used to measure the original
    bug: wraps the fake's own `close()` with an artificial delay, then
    asserts the SERVICE CALL itself returns in well under that delay --
    proving the teardown genuinely no longer blocks this call's own
    caller. `await hass.async_block_till_done()` afterward is what waits
    for the now-background close to actually finish (HA's own semantics:
    it drains every task the coordinator scheduled via the config entry's
    task-tracking API, not just the ones a caller explicitly awaited).
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    await setup_entry(hass, transport)
    real_close = transport.close

    async def slow_close() -> None:
        await asyncio.sleep(3.0)
        await real_close()

    transport.close = slow_close

    start = time.monotonic()
    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"turn_off blocked for {elapsed:.2f}s waiting on teardown"
    assert hass.states.get("switch.justice_inv_1_connection").state == "off"

    # The teardown did still happen -- just not on this call's own critical
    # path. Wait for it explicitly before trusting `transport.connected`.
    await hass.async_block_till_done()
    assert transport.connected is False


async def test_turning_on_while_a_previous_close_is_still_in_flight_reconnects(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """Finding 4: with no coordination between the two, a `turn_on` landing
    while a just-issued `turn_off`'s own background close() is still
    tearing down used to race it on the transport's single shared lock.
    The transport's own close-generation latch (`transport/base.py`'s
    `Transport.connect()` docstring) exists to stop a STALE, already-
    queued `connect()` from reopening a logger the user just explicitly
    closed -- but it cannot distinguish that from a fresh, legitimate
    reconnect that merely started while the close was still running:
    either way the epoch changes underneath it and it silently aborts.
    Measured without `async_set_connection_enabled`'s fix (await any
    pending `self._close_task` before reconnecting): switch left reading
    "on", `transport.connected` stuck `False`, `connect_count` never
    incremented, one failure booked, every data entity stuck unavailable,
    NOTHING logged -- recovering only at the next scheduled tick.

    Injects the same artificial close delay as the test above, but this
    time calls `turn_on` immediately after `turn_off`'s own (now-fast)
    return, with no `hass.async_block_till_done()` in between -- so the
    background close from `turn_off` is still genuinely running when
    `turn_on` starts.
    """
    transport = FakeTransport(justice_registers_synthetic_complete)
    entry = await setup_entry(hass, transport)
    coordinator = entry.runtime_data.coordinator
    real_close = transport.close

    async def slow_close() -> None:
        await asyncio.sleep(1.0)
        await real_close()

    transport.close = slow_close

    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    # turn_off's own service call already returned (per the test above),
    # but its background close() is still running (1 s injected delay) --
    # fire turn_on right now, racing it for real, not just in theory.
    await hass.services.async_call(
        "switch", "turn_on",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get("switch.justice_inv_1_connection").state == "on"
    assert transport.connected is True
    assert coordinator.last_update_success is True
    assert coordinator.failure_count == 0

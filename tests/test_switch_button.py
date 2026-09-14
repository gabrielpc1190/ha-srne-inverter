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

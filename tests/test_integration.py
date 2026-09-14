"""End-to-end: two config entries, one per logger, sharing nothing.

Every single-task test in this suite (test_init.py, test_entity.py,
test_sensor.py, test_number_select.py, test_switch_button.py,
test_services.py, test_config_flow.py, test_diagnostics.py, ...) sets up
exactly ONE config entry at a time. That leaves the one fact the whole
design actually depends on -- Casa Justice is two inverters, two loggers,
two entries, per the locked "multiple config entries" decision -- with zero
coverage: nothing proves two entries can coexist without silently sharing a
transport, a device, a unique id, or a failure/pause state. This file's job
is exactly that: two entries, same domain, same fixture data, different
serials, and assertions that would fail if either entry could see or touch
the other's world.

Fixture deviation, same root cause as every runtime task since Task 8
(tests/test_init.py's own module docstring first documents it):
`coordinator.async_probe()` always probes every one of `registers.BLOCKS`'
10 blocks, and the raw recorded `justice_registers` fixture has real capture
gaps inside 5 of them -- `FakeTransport` raises an uncaught `LookupError`
("fixture gap") for an address that is neither recorded nor declared
`unsupported`, which `probe()` does not catch. The task brief's starter code
for BOTH tests below used the plain `justice_registers` fixture; every test
here that runs a real, fully-supported setup uses
`justice_registers_synthetic_complete` instead. (`justice_registers` itself
is intentionally unused in this file, not merely uncited.)

Second (undocumented in the brief, found while running it) deviation: adding
BOTH entries to hass with `entry.add_to_hass(hass)` and only THEN looping
`await hass.config_entries.async_setup(entry.entry_id)` for each -- exactly
what the brief's own test 1 does -- raises `OperationNotAllowed` on the
SECOND iteration. Root cause has nothing to do with this integration:
`homeassistant.setup._async_setup_component`, reached because the domain
isn't in `hass.config.components` yet on the first call, sees BOTH entries
already registered via `hass.config_entries.async_entries(domain)` and sets
up every one of them concurrently right there (verified against the
installed source, "Add to components before the entry.async_setup"); the
loop's second, explicit `async_setup` call then finds that entry already
`LOADED` and refuses ("needs to be in the NOT_LOADED state"). Fixed by
`_setup_entries()` below: add and set up one entry at a time, which is also
closer to how Home Assistant actually starts up config entries on a real
box (sequentially, not all pre-registered before any of them load).

Third deviation: the brief's own `dr.async_get(hass).async_get_device(...)`
raises `RuntimeError` on HA 2026.9.2 -- `DeviceRegistry.async_get_device` is
deprecated with `core_behavior=ReportBehavior.ERROR` (verified against the
installed `homeassistant.helpers.device_registry` source), which
`homeassistant.helpers.frame.report_usage` turns into a raised error for a
caller outside a recognized integration frame, which a test module is.
Replaced by `DeviceRegistry.async_get_device_by_identifier(identifier,
config_entry_id)`, which takes a single `(domain, serial)` tuple plus the
owning entry's id, not a set -- and is not deprecated.

Fourth deviation, not one of the two defects flagged ahead of time: the
brief's OWN `test_unloading_one_entry_leaves_the_other_running` never
actually sets up a second entry. Its name promises proof that "the other"
keeps running, but the brief's body calls the single-entry `setup_entry()`
helper once, unloads that one entry, and only checks that ITS OWN sensor
disappeared -- exactly the "does this test even reach the state it claims"
trap this task's brief warns about applied to its own starter code. Fixed
below by actually creating two entries and asserting the surviving one's
state, transport and entities are untouched.

Fifth deviation, also found by running the brief's own literal test 2 body
in isolation before touching it: its assertion
`hass.states.get("sensor.justice_inv_1_battery_soc") is None` after an
unload is factually wrong for a normally-registered entity.
`homeassistant.helpers.entity.Entity.async_remove`'s own docstring is
explicit -- "If the entity has a non disabled entry in the entity registry,
the entity's state will be set to unavailable" -- and
`EntityPlatform.async_reset` (what a config-entry unload actually runs)
calls it with the default `force_remove=False`. Confirmed by hand: running
the brief's exact single-entry setup/unload sequence leaves the state
`unavailable, restored=True`, never gone. Fixed to assert `state is not
None` and `state.state == "unavailable"` instead.

Two more scenarios beyond the brief's pair, because a green two-entry setup
test does not by itself prove independence under failure or under a pause --
the two operations the design doc calls out as exactly the ones that must
never cross entries:
  - test_a_failed_probes_setup_retry_does_not_affect_the_sibling_entry:
    a wrong slave id (this site's own confusable .240/slave-1 vs.
    .242/slave-2 pair) makes one entry's probe find zero SUPPORTED blocks.
    That entry must land in SETUP_RETRY with no device and no entities,
    while the healthy sibling entry loads normally.
  - test_pausing_one_entrys_connection_does_not_affect_the_other: turning
    off one entry's connection switch (freeing that logger's one TCP
    client slot) must not touch the sibling's transport, coordinator or
    entity states.
"""

from collections import Counter
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.srne_inverter.const import (
    CONF_COLD_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SLAVE_ID,
    CONF_WARM_INTERVAL,
    DOMAIN,
)
from tests.fake_transport import FakeTransport

OPTIONS = {CONF_SCAN_INTERVAL: 10, CONF_WARM_INTERVAL: 60, CONF_COLD_INTERVAL: 300}

# Both real Casa Justice units, per docs/superpowers/specs/2026-09-13-srne-
# inverter-integration-design.md -- inverter 1 is the parallel host.
UNITS = (
    ("3548208972", "192.168.188.240", 1, "Justice Inv 1"),
    ("3548738877", "192.168.188.242", 2, "Justice Inv 2"),
)

# A fully-supported unit (every block SUPPORTED, only reachable in tests via
# justice_registers_synthetic_complete) yields exactly this many entities per
# platform -- independently confirmed by tests/test_number_select.py's own
# `== 18` / `== 5` assertions and by progress.md's Task 10 entity-inventory
# check (82 fields - 23 writable = 59, + 2 derived = 61 sensors + 1 binary
# sensor). switch/button are always exactly one each, regardless of probe
# outcome (switch.py/button.py add their single entity unconditionally).
EXPECTED_ENTITY_COUNTS = {
    "sensor": 61,
    "binary_sensor": 1,
    "number": 18,
    "select": 5,
    "switch": 1,
    "button": 1,
}


async def _setup_entries(hass, transports, units=UNITS):
    """Add and set up each entry one at a time, inside a single patch of
    `build_transport`. See this module's own docstring (second deviation)
    for why "add both, then set up both in a loop" -- the brief's own
    pattern -- is not safe to use here: `async_setup`'s return value is
    collected per entry (the FIRST config-entry-only-integration setup call
    triggers `homeassistant.setup._async_setup_component`, which returns
    True/False for the COMPONENT, not the entry), so callers that need to
    know whether a SPECIFIC entry loaded should check `entry.state`
    afterward rather than trust the returned list positionally beyond "did
    the call to set up this entry_id itself raise".
    """
    entries = []
    results = []
    with patch(
        "custom_components.srne_inverter.build_transport",
        side_effect=lambda e: transports[e.data[CONF_SERIAL]],
    ):
        for serial, host, slave, title in units:
            entry = MockConfigEntry(
                domain=DOMAIN, unique_id=serial, title=title, options=OPTIONS,
                data={CONF_HOST: host, CONF_PORT: 8899, CONF_SERIAL: serial,
                      CONF_SLAVE_ID: slave},
            )
            entry.add_to_hass(hass)
            results.append(await hass.config_entries.async_setup(entry.entry_id))
            entries.append(entry)
        await hass.async_block_till_done()
    return entries, results


async def test_two_entries_produce_two_independent_devices(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transports = {
        "3548208972": FakeTransport(justice_registers_synthetic_complete),
        "3548738877": FakeTransport(justice_registers_synthetic_complete),
    }
    entries, results = await _setup_entries(hass, transports)

    assert results == [True, True]
    assert all(e.state is ConfigEntryState.LOADED for e in entries)

    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    all_ids: set[str] = set()
    for entry, (serial, _host, _slave, title) in zip(entries, UNITS):
        device = devices.async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
        assert device is not None
        assert device.name == title

        entities = er.async_entries_for_config_entry(registry, entry.entry_id)
        by_domain = Counter(e.domain for e in entities)
        # Every one of the six platforms contributed exactly the entity
        # count a fully-supported unit is supposed to produce -- not just
        # "at least one of each" (a platform silently under-producing, e.g.
        # one number entity swallowed by the added_field_keys namespacing
        # bug Task 9's own review found and fixed, would pass a looser
        # ">= 1 per domain" check).
        assert by_domain == EXPECTED_ENTITY_COUNTS

        ids = {e.unique_id for e in entities}
        assert all(i.startswith(f"{serial}_") for i in ids)
        # Namespaced by serial with no slave-id component (the locked
        # design decision): nothing from this entry's id set may already be
        # claimed by the other entry.
        assert all_ids.isdisjoint(ids)
        all_ids |= ids

    # Two genuinely separate transport objects were used, and both ended up
    # connected -- not one instance shared between the two "devices" above
    # (which would make them two registry entries pointed at the same
    # socket, the exact failure mode a shared-state bug would produce while
    # still passing every assertion above it).
    assert transports["3548208972"] is not transports["3548738877"]
    assert transports["3548208972"].connected is True
    assert transports["3548738877"].connected is True


async def test_unloading_one_entry_leaves_the_other_running(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transports = {
        "3548208972": FakeTransport(justice_registers_synthetic_complete),
        "3548738877": FakeTransport(justice_registers_synthetic_complete),
    }
    entries, results = await _setup_entries(hass, transports)
    assert results == [True, True]

    assert await hass.config_entries.async_unload(entries[0].entry_id)
    await hass.async_block_till_done()

    # Fifth deviation (module docstring): the brief asserts
    # `hass.states.get(...) is None` here. `Entity.async_remove`'s own
    # docstring says otherwise -- "If the entity has a non disabled entry
    # in the entity registry, the entity's state will be set to
    # unavailable" -- and `EntityPlatform.async_reset` (what an entry
    # unload actually runs) calls it with the default `force_remove=False`.
    # Confirmed by hand against a single-entry setup/unload, matching the
    # brief's own literal test 2 body: the state after unload is
    # `unavailable, restored=True`, never gone. `is None` would only hold
    # for a disabled entity or a full config-entry REMOVAL (force_remove),
    # neither of which applies to a plain unload.
    state = hass.states.get("sensor.justice_inv_1_battery_soc")
    assert state is not None
    assert state.state == "unavailable"
    # The unloaded entry's own logger slot was actually freed, not merely
    # marked unloaded in the registry.
    assert transports["3548208972"].connected is False

    # The entry that was NOT unloaded is untouched: still LOADED, still
    # holding its own logger's connection, still serving its own entities
    # with live (non-stale) data.
    assert entries[1].state is ConfigEntryState.LOADED
    assert transports["3548738877"].connected is True
    state = hass.states.get("sensor.justice_inv_2_battery_soc")
    assert state is not None
    assert state.state != "unavailable"


async def test_a_failed_probes_setup_retry_does_not_affect_the_sibling_entry(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    """A wrong slave id (this site's own confusable .240/slave-1 vs.
    .242/slave-2 pair) connects fine but makes every block answer
    IllegalDataAddress -- zero SUPPORTED blocks, `probe()` raises
    `ProbeFailedError`, `async_setup_entry` must turn that into
    `ConfigEntryNotReady`/SETUP_RETRY with no device and no entities (the
    "never a device with zero entities" fact) -- and, the part no
    single-entry test can show, without taking the healthy sibling entry
    down or leaving it partially set up. Sets the FAILING entry up first
    specifically to check a failure doesn't corrupt shared hass/registry
    state (dispatcher signals, service registration, CONFIG_SCHEMA) that
    the sibling's OWN setup depends on next.
    """
    bad = FakeTransport(
        justice_registers_synthetic_complete, unsupported=(range(0x0000, 0x10000),)
    )
    good = FakeTransport(justice_registers_synthetic_complete)
    # UNITS[0] (3548208972) gets the bad transport and is set up FIRST --
    # entries[0]=bad, entries[1]=good.
    transports = {"3548208972": bad, "3548738877": good}
    entries, results = await _setup_entries(hass, transports)

    assert results == [False, True]
    assert entries[0].state is ConfigEntryState.SETUP_RETRY
    assert entries[1].state is ConfigEntryState.LOADED

    devices = dr.async_get(hass)
    assert devices.async_get_device_by_identifier(
        (DOMAIN, "3548208972"), entries[0].entry_id
    ) is None
    assert devices.async_get_device_by_identifier(
        (DOMAIN, "3548738877"), entries[1].entry_id
    ) is not None

    registry = er.async_get(hass)
    assert er.async_entries_for_config_entry(registry, entries[0].entry_id) == []
    good_entities = er.async_entries_for_config_entry(registry, entries[1].entry_id)
    assert Counter(e.domain for e in good_entities) == EXPECTED_ENTITY_COUNTS

    # The failing entry's own connect() succeeded (a real device answering
    # IllegalDataAddress to every block is still a live TCP session) --
    # freeing that logger's one slot for the next retry is backed by THREE
    # independent layers here, confirmed by mutation one at a time:
    # `coordinator.async_probe()`'s own `except ProbeFailedError` close,
    # `async_setup_entry`'s `except TransportError` close, and (the one that
    # actually fires once the first two are both removed) HA's own
    # `entry.async_on_unload(coordinator.async_shutdown)` registration --
    # __init__.py's own docstring calls this the safety net for exactly
    # this "never returned True" case, and `async_shutdown`'s own final
    # `await self.transport.close()` is what makes it bite here too. Only
    # gutting ALL THREE turns this assertion red. The healthy sibling is
    # unaffected either way.
    assert bad.connected is False
    assert good.connected is True


async def test_pausing_one_entrys_connection_does_not_affect_the_other(
    hass, justice_registers_synthetic_complete, enable_custom_integrations
):
    transports = {
        "3548208972": FakeTransport(justice_registers_synthetic_complete),
        "3548738877": FakeTransport(justice_registers_synthetic_complete),
    }
    entries, results = await _setup_entries(hass, transports)
    assert results == [True, True]

    await hass.services.async_call(
        "switch", "turn_off",
        {"entity_id": "switch.justice_inv_1_connection"}, blocking=True,
    )
    await hass.async_block_till_done()

    assert transports["3548208972"].connected is False
    assert hass.states.get("switch.justice_inv_1_connection").state == "off"
    assert entries[0].runtime_data.coordinator.last_update_success is False

    # The sibling logger's own socket, coordinator and entity states are
    # untouched -- "pausing frees THAT logger's slot" is a per-transport
    # fact, not a per-integration one a shared lock or module-level flag
    # could get backwards.
    assert transports["3548738877"].connected is True
    assert entries[1].runtime_data.coordinator.last_update_success is True
    assert hass.states.get("switch.justice_inv_2_connection").state == "on"
    state = hass.states.get("sensor.justice_inv_2_battery_soc")
    assert state is not None
    assert state.state != "unavailable"

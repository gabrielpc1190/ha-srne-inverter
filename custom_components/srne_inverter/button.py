"""Button platform: re-run the capability probe on demand.

`probe()` (invoked here via `coordinator.async_probe()`) is never persisted
and always recomputed from scratch (see `probe.py`'s own module docstring)
-- this button is how a user picks up a firmware change, or a block that
started answering after some config change on the unit, without restarting
Home Assistant. Firing `SIGNAL_NEW_ENTITIES` afterward is what tells every
platform's own dispatcher subscription (`entity.py`'s
`async_setup_field_platform`) to re-run its `builder()` and add whatever is
newly `SUPPORTED`.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .const import SIGNAL_NEW_ENTITIES
from .coordinator import SrneCoordinator
from .entity import SrneEntity
from .transport.base import TransportError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the reprobe button.

    One per entry, always -- see `switch.py`'s `async_setup_entry` docstring
    for why this bypasses `async_setup_field_platform`/`SIGNAL_NEW_ENTITIES`
    (this entity's own existence never depends on a probe result).
    """
    async_add_entities([SrneReprobeButton(entry.runtime_data.coordinator, entry)])


class SrneReprobeButton(SrneEntity, ButtonEntity):
    """Re-reads every block and adds entities that appeared."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SrneCoordinator, entry: SrneConfigEntry) -> None:
        super().__init__(coordinator, entry, "reprobe", "Reprobe")

    @property
    def available(self) -> bool:
        """Always available, for the same reason as `SrneConnectionSwitch.
        available` (`switch.py`): inheriting `CoordinatorEntity.available`
        (`coordinator.last_update_success`) would make this button
        unavailable during the exact two situations a user is most likely
        to want it -- right after a failed poll (any backoff window) or
        while the connection is deliberately paused (Fix round 1, Task 12
        review, Finding 2). Measured consequence of NOT overriding this:
        `button.press` on an unavailable entity is a silent no-op inside
        Home Assistant's own service-call machinery (`helpers/service.py`'s
        target resolution drops unavailable entities before the platform's
        own `async_press` is ever called) -- one WARNING line in the log
        and nothing else, at the moment a re-probe is most wanted.
        """
        return True

    async def async_press(self) -> None:
        await async_reprobe(self.hass, self._entry)


async def async_reprobe(hass: HomeAssistant, entry: SrneConfigEntry) -> dict[str, str]:
    """Re-probe the unit and tell every platform to add what appeared.

    A guard, then three steps, in order:

    0. Refuses outright if `coordinator.connection_enabled` is `False`
       (Fix round 1, Finding 1) -- see the guard's own comment below for
       why this lives here rather than inside `SrneCoordinator.async_probe`.

    1. `coordinator.async_probe()` -- reconnects if needed and re-reads
       every declared block from scratch, replacing `coordinator.
       probe_result`/`coordinator.support` and merging the fresh registers
       into the running snapshot. `ProbeFailedError` (raised if NOT ONE
       block answers SUPPORTED, e.g. a wrong slave id) is a `TransportError`
       subclass and is turned into `HomeAssistantError` here so pressing the
       button on a dead unit surfaces a clean UI notification instead of an
       unhandled traceback; any other `TransportError` (a plain connect
       failure) is handled the same way.
    2. `coordinator.async_refresh()` -- `async_probe()` alone updates
       `coordinator.probe_result`/`coordinator._registers` but NOT the
       publicly exposed `coordinator.data` snapshot (that only changes on a
       refresh cycle); without this, a newly-supported field's entity would
       be added by step 3 below but read `unavailable` (its own key
       genuinely missing from the still-stale `coordinator.data.values`)
       until the next scheduled poll happened to run.
    3. `async_dispatcher_send(hass, SIGNAL_NEW_ENTITIES...)` -- tells every
       platform's `async_setup_field_platform` subscription to re-run its
       own `builder()`; each platform adds only the keys newly present in
       `coordinator.supported_field_keys()` and not already in
       `entry.runtime_data.added_field_keys` (see `entity.py`'s own
       docstring for that shared-set convention).

    Interaction with an in-flight scheduled poll: `async_probe()` does not
    hold the transport lock for its whole duration -- like the coordinator's
    own `_read_due_blocks`, each block's `read_holding` call takes the
    transport's lock only for that one call, with a pause in between. A
    reprobe landing while a scheduled poll cycle (`_async_update_data`,
    itself serialised against OTHER refreshes by
    `DataUpdateCoordinator`'s own debounce lock, but NOT against this
    direct `async_probe()` call) is mid-flight can therefore interleave
    block-by-block with it: neither read corrupts the other (the
    transport's lock still serialises each individual call), but the two
    block-scanning passes can race, and the logger sees a temporarily
    higher read rate than either pass alone would produce. Both write into
    the SAME `coordinator._registers` dict with the freshly read value for
    whichever address they touch, so the end result is still every block
    that answered, read at least once -- there is no torn or mixed-up
    register value, just possibly-overlapping traffic on the wire while
    both passes are in flight. The button's own caller only ever sees the
    already-awaited result of ITS OWN probe pass.
    """
    coordinator = entry.runtime_data.coordinator
    if not coordinator.connection_enabled:
        # Fix round 1 (Task 12 review, Finding 1, Important): async_probe()
        # itself has no idea the user paused the connection -- it happily
        # calls transport.connect() if the transport is not already
        # connected, same as any other caller. Measured without this
        # guard: pressing reprobe while paused reopened the logger (10
        # reads, transport.connected True) while the switch still read
        # "off" and nothing ever closed it again -- exactly the betrayal
        # this switch exists to prevent (Gabriel pauses, goes to run
        # tools/probe.py or justice_watch.py, and finds the logger's one
        # TCP slot taken by the integration he just told to let go of it).
        # `async_reprobe` is the ONE shared entry point both this button
        # and Task 15's future `reprobe` service will call -- fixing it
        # here, not inside `SrneCoordinator.async_probe()` itself, keeps
        # the guard exactly where every current and future caller of a
        # user-triggered reprobe already goes through.
        raise HomeAssistantError(
            "cannot reprobe while the connection is paused -- turn the "
            "connection switch back on first"
        )
    try:
        result = await coordinator.async_probe()
    except TransportError as err:
        raise HomeAssistantError(f"reprobe failed: {err}") from err
    await coordinator.async_refresh()
    async_dispatcher_send(hass, SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id))
    return result.as_diagnostics()

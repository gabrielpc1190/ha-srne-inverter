"""Switch platform: pause/resume the connection to the logger.

Each Solarman logger accepts exactly one TCP client. Turning this switch off
closes our socket and stops polling so an external tool (tools/probe.py, the
Casa Justice scripts) can take over.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .coordinator import SrneCoordinator
from .entity import SrneEntity

# Fix round 1 (Task 12 review, Finding 4): defense in depth alongside
# coordinator.async_set_connection_enabled's own await-the-pending-close-
# task fix (see coordinator.py) -- that fix is what makes a genuine
# turn_on-during-teardown race resolve correctly rather than silently
# wedge, but nothing stopped HA from dispatching a SECOND async_turn_on/
# async_turn_off call into this entity while a first one is still
# in-flight in the first place. HA's own `PARALLEL_UPDATES` module
# constant limits how many of THIS platform's own service-call handlers
# (async_turn_on/async_turn_off) may be in flight at once; `1` means the
# second call simply waits its turn instead of running concurrently with
# the first.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the connection switch.

    One per entry, always -- unlike the field-backed platforms (sensor,
    binary_sensor, number, select), this entity's existence does not depend
    on what the probe found, so it is added directly here instead of going
    through `entity.py`'s `async_setup_field_platform`/`SIGNAL_NEW_ENTITIES`
    machinery, which exists for entities that can newly appear after a
    reprobe -- this one is always present from the very first setup.
    """
    async_add_entities([SrneConnectionSwitch(entry.runtime_data.coordinator, entry)])


class SrneConnectionSwitch(SrneEntity, SwitchEntity):
    """Owns whether the integration holds the logger's single TCP slot.

    Subclasses `SrneEntity` directly, NOT `SrneFieldEntity` (this entity has
    no backing `Field`), and uses a plain, non-`Field` key ("connection").
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SrneCoordinator, entry: SrneConfigEntry) -> None:
        super().__init__(coordinator, entry, "connection", "Connection")

    @property
    def available(self) -> bool:
        """Always available -- deliberately NOT `super().available`.

        `SrneEntity`/`CoordinatorEntity.available` is
        `coordinator.last_update_success`. Turning the connection off sets
        that flag to `False` (correctly: the polled data really is stale).
        A switch that inherited this rule unchanged would go unavailable
        the INSTANT it is switched off -- greying out in HA's UI, so the
        very control the user just used to pause the connection would be
        the one control they could no longer use to resume it. Task 7's
        public interface and Task 9's `SrneEntity` docstring both flag this
        by name as this task's problem to solve; `SrneEntity` itself leaves
        `available` un-shadowed for exactly this reason, so this override
        is clean.

        Always returning `True` is safe here specifically because this
        entity's own state (`is_on`, from `coordinator.connection_enabled`)
        is never stale the way polled DATA is -- the connection-enabled
        flag is set synchronously by `async_set_connection_enabled` and
        reflects the true current intent regardless of whether the last
        poll succeeded.
        """
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.connection_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Resume polling.

        `async_set_connection_enabled(True)` restores the scan-interval
        cadence, resets backoff and awaits an immediate `async_refresh()`
        -- by the time this returns, `coordinator.last_update_success`
        already reflects the reconnect attempt. `async_write_ha_state()`
        is still called explicitly (not relying solely on the coordinator
        listener callback `async_set_connection_enabled` also triggers)
        so this entity's own state is current the instant this call
        returns, not just eventually.
        """
        await self.coordinator.async_set_connection_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Pause polling and free the logger's one TCP slot.

        Fix round 1 (Task 12 review, Finding 3): this docstring used to
        claim the entity reads "off" immediately because this call "does
        not add any further blocking of its own" on top of the
        coordinator's own await -- that was wrong. Before this fix round,
        `async_set_connection_enabled(False)` itself `await`ed
        `transport.close()` directly, so THIS call (an HA service call
        under `blocking=True`) sat there for however long that teardown
        took -- up to ~14 s on the real transport under lock contention --
        before returning, and the entity's own state stayed whatever it
        was until then. `async_set_connection_enabled` now schedules that
        close as a tracked background task instead of awaiting it inline
        (see its own docstring in `coordinator.py`), so THIS call returns
        as soon as the flag is flipped and the affected entities are
        marked unavailable -- genuinely fast, not just documented as such.
        `async_write_ha_state()` still runs immediately after, so the
        switch itself reads "off" the instant this service call returns,
        while the actual teardown finishes separately in the background
        (observable in a test via `await hass.async_block_till_done()`,
        which waits for that tracked task too).
        """
        await self.coordinator.async_set_connection_enabled(False)
        self.async_write_ha_state()

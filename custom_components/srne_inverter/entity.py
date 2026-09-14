"""Shared entity base for every srne_inverter platform.

`SrneEntity` is what every platform in Tasks 10-12 subclasses -- its
availability rule and unique-id scheme propagate into every entity this
integration creates. `async_setup_field_platform` is the one function a
platform's own `async_setup_entry` calls: it adds the entities currently
supported and re-adds on a re-probe via `SIGNAL_NEW_ENTITIES`.

THIS MODULE MAY import homeassistant (unlike registers.py/probe.py/
transport/*, which the guard tests in tests/test_core_is_ha_free.py hold to
zero homeassistant imports at any nesting).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_SERIAL, DEFAULT_MODEL, DOMAIN, MANUFACTURER, SIGNAL_NEW_ENTITIES
from .coordinator import SrneCoordinator
from .probe import BlockSupport
from .registers import Field

_CATEGORIES: dict[str, EntityCategory] = {
    "config": EntityCategory.CONFIG,
    "diagnostic": EntityCategory.DIAGNOSTIC,
}


class SrneEntity(CoordinatorEntity[SrneCoordinator]):
    """Base entity: device identity, naming and availability.

    Availability is deliberately left as `CoordinatorEntity`'s own
    (`self.coordinator.last_update_success`), not overridden here -- this is
    what "a switch built naively on CoordinatorEntity goes unavailable when
    paused" (Task 7's public interface, "connection toggle" note) is about.
    Task 12's connection switch entity must override `available` itself
    (e.g. `True`, or `connection_enabled or last_update_success`) so the
    toggle stays usable while paused; nothing here should make that harder.
    `SrneFieldEntity` below narrows availability further (per-field), which
    is a normal subclass override of this same property, not a fight against
    it.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SrneCoordinator,
        entry,  # SrneConfigEntry, untyped to avoid a circular import
        key: str,
        label: str,
        category: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._serial = str(entry.data[CONF_SERIAL])
        self._attr_name = label
        self._attr_unique_id = f"{self._serial}_{key}"
        if category is not None:
            self._attr_entity_category = _CATEGORIES[category]

    @property
    def device_info(self) -> DeviceInfo:
        """One device per config entry, identified by the logger's serial.

        `sw_version`/`hw_version` come from the decoded `device_info` block
        (0x0014, COLD tier). Fix round 1 (Task 9 review, deferred item):
        this used to claim they fill in blank on "the first COLD-tier cycle"
        if they are `None` at add time -- that is not how HA actually
        applies this property. `entity_platform.py`'s `_async_add_entity`
        (verified against the installed source, ~line 956: `if device_info
        := entity.device_info:`) reads this property exactly ONCE, when the
        entity is first added to the entity/device registries -- not on
        every coordinator update. In practice this block has almost always
        already been read by the time any entity is added at all: probing
        (`SrneCoordinator.async_probe`) reads every declared block, including
        COLD ones, and `async_setup_entry` only forwards to the platforms
        (which is what actually adds entities) AFTER that probe and the
        first refresh both complete. The only way `sw_version`/`hw_version`
        show up blank here is a block probed `UNKNOWN` (a transient failure,
        not `UNSUPPORTED`) -- and if that happens, they stay blank in the
        device registry until the entry is reloaded (a fresh setup re-probes
        from scratch), NOT until a later COLD-tier poll happens to succeed:
        a later successful read updates `coordinator.data.values`, but
        nothing re-reads this already-registered device's `device_info`
        because of it.
        """
        values = self.coordinator.data.values if self.coordinator.data else {}
        return DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            manufacturer=MANUFACTURER,
            model=DEFAULT_MODEL,
            name=self._entry.title,
            sw_version=values.get("firmware_version"),
            hw_version=values.get("hardware_version"),
            serial_number=self._serial,
            configuration_url=f"http://{self._entry.data[CONF_HOST]}/status.html",
        )


class SrneFieldEntity(SrneEntity):
    """Entity backed by one mapped register field."""

    def __init__(self, coordinator: SrneCoordinator, entry, field: Field) -> None:
        super().__init__(coordinator, entry, field.key, field.label, field.category)
        self.field = field

    @property
    def available(self) -> bool:
        """Unavailable if the coordinator is stale, OR this field's own
        block is not currently classified SUPPORTED, OR this specific field
        has never produced a value at all.

        Fix round 1 (Task 9 review, Finding 4): the second check
        (`coordinator.support`) is not redundant with the third
        (`field.key in values`) -- `coordinator._registers` is CUMULATIVE
        and is never purged for a block that gets reclassified UNSUPPORTED
        mid-poll (coordinator.py's own Fix round 1, Finding 1: the block is
        skipped going forward, but its LAST successfully read raw values
        stay exactly where they were). Verified by hand: after a block that
        was SUPPORTED at probe time starts answering
        `UnsupportedRegisterError` on a later cycle, `coordinator.data.
        values[field.key]` still held that field's stale, frozen last value
        -- `field.key in values` alone stayed `True` forever, which would
        have shown a no-longer-polled field as available with data that can
        never change again. Checking `coordinator.support` first closes
        that hole for a block reclassified UNSUPPORTED; `field.key in
        values` still does the separate job of catching a block that has
        never been successfully read at all (UNKNOWN at probe time, or not
        yet reached by a re-probe) -- neither check subsumes the other.
        """
        if not super().available:
            return False
        if self.coordinator.support.get(self.field.block_addr) is BlockSupport.UNSUPPORTED:
            return False
        values = self.coordinator.data.values if self.coordinator.data else {}
        return self.field.key in values

    @property
    def raw_value(self) -> int | list[int | None] | None:
        """The undecoded register word(s), exposed as an attribute.

        Fix round 1 (Task 9 review, Finding 5): a field spanning more than
        one register (`field.words > 1` -- `inverter_serial`, words=4, and
        the three multi-word energy totals) used to return only the FIRST
        word via a single `.get(field.address)` lookup, silently dropping
        every other word the field is actually made of. A support session
        reading `raw: 0` next to a correct-looking kWh total would
        reasonably conclude the register was broken, when the truth is the
        SECOND word (the one that actually carries the value at this
        firmware's scale) was never shown at all. Returns a plain `int` for
        a one-word field (unchanged), or a list of each word in address
        order for a wider one; a `None` entry in that list means that
        specific register hasn't been read yet, same meaning as the
        scalar `None` case below.
        """
        if self.coordinator.data is None:
            return None
        registers = self.coordinator.data.registers
        if self.field.words == 1:
            return registers.get(self.field.address)
        return [
            registers.get(self.field.address + offset)
            for offset in range(self.field.words)
        ]

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "address": f"0x{self.field.address:04X}",
            "raw": self.raw_value,
        }


@callback
def async_setup_field_platform(
    hass: HomeAssistant,
    entry,  # SrneConfigEntry
    async_add_entities: AddConfigEntryEntitiesCallback,
    *,
    builder: Callable[[], Iterable[Entity]],
) -> None:
    """Add the entities this platform owns, and re-add them after a reprobe.

    `builder` must return only entities whose field key is currently
    supported and not yet added; it is responsible for updating
    `entry.runtime_data.added_field_keys` itself (this function does not
    touch that set).

    `entry.runtime_data.added_field_keys` is ONE set shared by all six
    platforms on a given entry, not one set per platform (Task 9 review,
    Finding 6) -- a `builder()` MUST namespace every key it adds with its
    own platform's domain, e.g. `f"sensor:{field.key}"` /
    `f"binary_sensor:{field.key}"` (exactly what `sensor.py` and
    `binary_sensor.py` already do), never a bare `field.key`. Two platforms
    that both happened to add the same BARE key would silently step on each
    other through this shared set: whichever platform's `builder()` runs
    first claims the key, and the second platform's own `if key in added:
    skip` check would then skip adding ITS entity too -- with no error, no
    log line, just one platform quietly missing an entity a user would
    otherwise expect. Namespacing by platform makes that collision
    structurally impossible as long as every platform follows the
    convention; nothing in this function enforces it (it never sees which
    platform is calling it), so a new platform in a future task copying an
    existing `builder()` as a template is by far the safest way to get this
    right.

    One shared DISPATCHER SIGNAL per entry (`SIGNAL_NEW_ENTITIES`, from
    `const.py`) is used by every platform, not one per platform -- a
    separate, harmless design choice from the `added_field_keys` namespacing
    above: `const.py` only declares a `{entry_id}`-keyed format string, with
    no per-platform slot, and a platform whose own `builder()` finds nothing
    newly supported just adds an empty list, which
    `AddConfigEntryEntitiesCallback` already treats as a no-op (verified
    against the installed `EntityPlatform._async_schedule_add_entities`
    source: `if not entities: return`) -- so sharing the one signal costs
    nothing.
    """
    async_add_entities(builder())

    @callback
    def _on_new_entities() -> None:
        # Fix round 1 (Task 9 review, Finding 1, CRITICAL): this used to be
        # a bare `lambda: async_add_entities(builder())`, passed straight to
        # async_dispatcher_connect with no `@callback` marker. HA's dispatcher
        # classifies an unmarked plain function via
        # `get_hassjob_callable_job_type` -- verified against the installed
        # `homeassistant.core` source: neither a coroutine function nor
        # `is_callback()` (which only checks for a `_hass_callback` attribute
        # that `homeassistant.core.callback` sets) -- so it fell through to
        # `HassJobType.Executor` and HA ran it via `loop.run_in_executor` on a
        # WORKER THREAD. `async_add_entities` (an `AddConfigEntryEntitiesCallback`)
        # itself calls `hass.async_create_task_internal`, which requires the
        # CURRENTLY RUNNING event loop -- called from a worker thread, that
        # raised `RuntimeError: loop ... is not the running loop`, and the
        # dispatcher's own `catch_log_exception` wrapper silently swallowed
        # it into one ERROR log line. Net effect, confirmed by reproducing it
        # (see task-9-report.md's Fix round 1 falsifiability evidence): a
        # re-probe's SIGNAL_NEW_ENTITIES fire added ZERO entities, silently --
        # exactly the "a firmware change is picked up without a restart"
        # feature this whole per-unit-probe design exists to deliver, simply
        # not working. A `@callback`-decorated function (any callable HA can
        # mark, not specifically a nested `def` over a lambda) fixes it: HA
        # then classifies it Callback and runs it directly on the event loop,
        # where `async_add_entities`'s own task-creation is safe.
        async_add_entities(builder())

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id),
            _on_new_entities,
        )
    )

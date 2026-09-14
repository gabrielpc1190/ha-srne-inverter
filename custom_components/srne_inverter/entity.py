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
        (0x0014, COLD tier) and are `None` until that block has been read at
        least once -- HA tolerates `None` here, it just leaves the field
        blank in the device page until the first COLD-tier cycle lands.
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
        """Unavailable if the coordinator is stale, OR this specific field's
        block hasn't produced a value -- e.g. a COLD-tier field before its
        first read, or a block a mid-poll UnsupportedRegisterError just
        reclassified UNSUPPORTED (coordinator.py's own Fix round 1, Finding
        1)."""
        return super().available and self.field.key in (
            self.coordinator.data.values if self.coordinator.data else {}
        )

    @property
    def raw_value(self) -> int | None:
        """The undecoded register word, exposed as an attribute."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.registers.get(self.field.address)

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

    One shared signal per entry (`SIGNAL_NEW_ENTITIES`, from `const.py`) is
    used by every platform, not one per platform: `const.py` only declares a
    `{entry_id}`-keyed format string, with no per-platform slot, and a
    platform whose own `builder()` finds nothing newly supported just adds
    an empty list, which `async_add_entities` already treats as a no-op --
    so sharing the one signal is harmless, not merely convenient.
    """
    async_add_entities(builder())

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_ENTITIES.format(entry_id=entry.entry_id),
            lambda: async_add_entities(builder()),
        )
    )

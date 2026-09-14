"""Binary sensor platform: one aggregated 'fault active' entity."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .coordinator import SrneCoordinator
from .entity import SrneEntity, async_setup_field_platform
from .registers import FAULT_FIELD_KEYS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the fault binary sensor if the fault block answered.

    Only one entity ever exists on this platform: a device whose "faults"
    block (0x0200, holding fault_word_1-4) came back UNSUPPORTED gets no
    fault entity at all rather than one permanently stuck `unavailable`.
    """
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder() -> list[BinarySensorEntity]:
        supported = coordinator.supported_field_keys()
        if not all(key in supported for key in FAULT_FIELD_KEYS):
            return []
        if "binary_sensor:fault_active" in added:
            return []
        added.add("binary_sensor:fault_active")
        return [SrneFaultBinarySensor(coordinator, entry)]

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


class SrneFaultBinarySensor(SrneEntity, BinarySensorEntity):
    """ON when any of the four fault words is non-zero."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: SrneCoordinator, entry: SrneConfigEntry) -> None:
        super().__init__(coordinator, entry, "fault_active", "Fault Active")

    def _words(self) -> dict[str, str]:
        if self.coordinator.data is None:
            return {}
        return {
            key: str(self.coordinator.data.values[key])
            for key in FAULT_FIELD_KEYS
            if key in self.coordinator.data.values
        }

    @property
    def is_on(self) -> bool | None:
        words = self._words()
        if not words:
            return None
        return any(int(value, 16) != 0 for value in words.values())

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return self._words()

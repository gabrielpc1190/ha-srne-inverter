"""Number platform: every writable numeric field, range taken from WriteSpec.

Bounds are read verbatim from `Field.write` (a `WriteSpec`); nothing here
computes or widens a range. Voltage-threshold fields in particular carry
per-parameter ranges cross-checked against the manufacturer's manual
(`registers.py`'s own comments) -- a field with `write is None` (unverified
range, or firmware-read-only) simply gets no entity here, never a guessed
fallback.
"""

from __future__ import annotations

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .entity import SrneFieldEntity, async_setup_field_platform
from .registers import FIELDS, Field, FieldKind


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a number for every writable non-enum field."""
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder() -> list[NumberEntity]:
        supported = coordinator.supported_field_keys()
        new: list[NumberEntity] = []
        for field in FIELDS:
            if field.write is None or field.kind is FieldKind.ENUM:
                continue
            if field.key not in supported or f"number:{field.key}" in added:
                continue
            added.add(f"number:{field.key}")
            new.append(SrneNumber(coordinator, entry, field))
        return new

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


class SrneNumber(SrneFieldEntity, NumberEntity):
    """A writable scalar register. Bounds come from the raw WriteSpec."""

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry: SrneConfigEntry, field: Field) -> None:
        super().__init__(coordinator, entry, field)
        assert field.write is not None
        low = field.write.min_raw * field.scale
        high = field.write.max_raw * field.scale
        self._attr_native_min_value = min(low, high)
        self._attr_native_max_value = max(low, high)
        self._attr_native_step = abs(field.write.step_raw * field.scale)
        self._attr_native_unit_of_measurement = field.unit
        if field.device_class:
            self._attr_device_class = NumberDeviceClass(field.device_class)

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.values.get(self.field.key)
        return float(value) if isinstance(value, (int, float)) else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write_field(self.field, value)

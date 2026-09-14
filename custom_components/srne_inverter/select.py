"""Select platform: every writable enum field."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .entity import SrneFieldEntity, async_setup_field_platform
from .registers import FIELDS, Field, FieldKind

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a select for every writable enum field."""
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder() -> list[SelectEntity]:
        supported = coordinator.supported_field_keys()
        new: list[SelectEntity] = []
        for field in FIELDS:
            if field.write is None or field.kind is not FieldKind.ENUM:
                continue
            if field.key not in supported:
                continue
            entity_key = f"select:{field.key}"
            if entity_key in added:
                # Fix round 1 (Opus review): same fix as number.py's own
                # builder(), mirroring sensor.py's Fix round 2 (commit
                # ce62625) -- see that file's identical branch for why a
                # debug line here (not just the state-set assertion in the
                # test) is what actually proves this guard ran, instead of
                # HA's own entity registry silently rejecting a duplicate
                # unique_id on this guard's behalf.
                _LOGGER.debug("%s already added, skipping re-probe rebuild", entity_key)
                continue
            added.add(entity_key)
            new.append(SrneSelect(coordinator, entry, field))
        return new

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


class SrneSelect(SrneFieldEntity, SelectEntity):
    """A writable enum register."""

    def __init__(self, coordinator, entry: SrneConfigEntry, field: Field) -> None:
        super().__init__(coordinator, entry, field)
        assert field.enum is not None
        self._attr_options = field.enum.options

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.values.get(self.field.key)
        return value if isinstance(value, str) and value in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_write_field(self.field, option)

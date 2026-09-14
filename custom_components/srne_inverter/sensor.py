"""Placeholder platform, implemented in a later task."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry,  # noqa: ANN001 - typed once entity.py lands
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the platform (no entities yet)."""
    return

"""Sensor platform: every read-only mapped field plus the derived values."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SrneConfigEntry
from .coordinator import SrneCoordinator
from .entity import SrneEntity, SrneFieldEntity, async_setup_field_platform
from .probe import BlockSupport
from .registers import DERIVED, FIELDS, Derived, Field, FieldKind, field_by_key

_LOGGER = logging.getLogger(__name__)


def _display_value(value: object, precision: int | None) -> object:
    """Coerce a whole-number float to `int` for a field with no fractional
    precision, so a count/percentage register reads as HA's clean "55"
    instead of "55.0" on a dashboard.

    `registers.decode()`'s `_decode_field` always computes `raw *
    field.scale`, and `Field.scale` defaults to `1.0` (a Python float) --
    `55 * 1.0` is `55.0`, a float, even though the register carries no
    fractional information at all. Verified there is no NUMBER-kind field in
    `FIELDS` that combines a non-unit `scale` with `precision is None`: every
    field with no declared precision is exactly this "raw register value,
    unscaled" case, so rounding it to a plain int never discards real
    information. `precision == 0` (currently only `Derived.load_power_total`)
    means the same thing one step later -- `round(x, 0)` in Python still
    returns a `float`, e.g. `round(47.8, 0) == 48.0`.

    Left alone (returned unchanged) whenever `precision` is a positive int:
    that is a field/derived value HA's `state` property does NOT round on
    its own (`suggested_display_precision` only hints the frontend, per
    `SensorEntity.state`'s installed source) -- `grid_charge_current`
    (`precision=1`) must keep showing "1.0", not "1", even on a reading
    that happens to land on a whole amp.
    """
    if precision in (None, 0) and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SrneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for every supported, non-writable field.

    Sensor selection rule (per this task's brief): every Field that is NOT
    writable becomes a sensor; a writable field (has a WriteSpec) is a
    select's or number's job instead (Tasks 11/12). All Derived entries
    always become sensors -- nothing else can write a computed value back to
    the inverter.
    """
    coordinator = entry.runtime_data.coordinator
    added = entry.runtime_data.added_field_keys

    def builder() -> list[SensorEntity]:
        supported = coordinator.supported_field_keys()
        new: list[SensorEntity] = []
        for field in FIELDS:
            if field.write is not None or field.key not in supported:
                continue
            entity_key = f"sensor:{field.key}"
            if entity_key in added:
                # Fix round 2 (Opus re-review, the one survivor from Fix
                # round 1's mutation testing): this branch used to have no
                # signal of its own -- deleting it entirely left
                # `hass.states.async_entity_ids()` unchanged before/after a
                # SIGNAL_NEW_ENTITIES re-fire, because HA's OWN entity
                # registry independently rejects a second entity carrying
                # the same unique_id ("Platform srne_inverter does not
                # generate unique IDs... already exists - ignoring", one
                # ERROR line per field, confirmed by the reviewer with the
                # guard removed) -- a test asserting on entity state alone
                # was proving HA's behaviour, not this guard's. A debug line
                # naming the skipped key gives this branch its own
                # observable, and is exactly what a support session
                # diagnosing "why didn't my re-probe add anything new" would
                # want to see.
                _LOGGER.debug("%s already added, skipping re-probe rebuild", entity_key)
                continue
            added.add(entity_key)
            new.append(SrneSensor(coordinator, entry, field))
        for derived in DERIVED:
            if derived.key not in supported:
                continue
            entity_key = f"sensor:{derived.key}"
            if entity_key in added:
                _LOGGER.debug("%s already added, skipping re-probe rebuild", entity_key)
                continue
            added.add(entity_key)
            new.append(SrneDerivedSensor(coordinator, entry, derived))
        return new

    async_setup_field_platform(hass, entry, async_add_entities, builder=builder)


class SrneSensor(SrneFieldEntity, SensorEntity):
    """A single decoded register."""

    def __init__(
        self, coordinator: SrneCoordinator, entry: SrneConfigEntry, field: Field
    ) -> None:
        super().__init__(coordinator, entry, field)
        if field.kind is FieldKind.ENUM:
            assert field.enum is not None
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = field.enum.options
        else:
            self._attr_native_unit_of_measurement = field.unit
            if field.device_class:
                self._attr_device_class = SensorDeviceClass(field.device_class)
            if field.state_class:
                self._attr_state_class = SensorStateClass(field.state_class)
            self._attr_suggested_display_precision = field.precision

    @property
    def native_value(self) -> object | None:
        if self.coordinator.data is None:
            return None
        value = _display_value(
            self.coordinator.data.values.get(self.field.key), self.field.precision
        )
        # `_attr_device_class`/`_attr_options` are HA's metaclass-generated
        # `CachedProperties` (see homeassistant.helpers.entity.CachedProperties):
        # each is backed by a private `__attr_*` instance attribute that is
        # ONLY created the first time the `_attr_*` name is ASSIGNED. A field
        # with no `device_class` (e.g. running_days) never assigns
        # `self._attr_device_class` in __init__ -- reading it directly here
        # would raise `AttributeError` for every such sensor, not just
        # return a falsy default. HA's own `device_class`/`options` cached
        # properties guard the same read with `hasattr(self, "_attr_...")`
        # for exactly this reason; `getattr(..., None)` is the equivalent
        # guard at the call site.
        if (
            getattr(self, "_attr_device_class", None) is SensorDeviceClass.ENUM
            and isinstance(value, str)
            and getattr(self, "_attr_options", None) is not None
            and value not in self._attr_options
        ):
            # Never let an unmapped raw code break the enum sensor: HA
            # raises if an enum sensor's state is not one of its declared
            # `options`, and registers.EnumMap.label() returns
            # "Unknown (N)" for any raw value that isn't in its map --
            # report unavailable instead of crashing the entity.
            return None
        return value


class SrneDerivedSensor(SrneEntity, SensorEntity):
    """A value computed from other fields (battery power, total load)."""

    def __init__(
        self, coordinator: SrneCoordinator, entry: SrneConfigEntry, derived: Derived
    ) -> None:
        super().__init__(coordinator, entry, derived.key, derived.label)
        self._derived = derived
        self._input_fields = tuple(field_by_key(key) for key in derived.inputs)
        self._attr_native_unit_of_measurement = derived.unit
        if derived.device_class:
            self._attr_device_class = SensorDeviceClass(derived.device_class)
        if derived.state_class:
            self._attr_state_class = SensorStateClass(derived.state_class)
        self._attr_suggested_display_precision = derived.precision

    @property
    def available(self) -> bool:
        """Unavailable if the coordinator itself is stale, OR any INPUT
        field's own block has been reclassified UNSUPPORTED, OR any input
        hasn't produced a value yet.

        Mirrors `SrneFieldEntity.available`'s per-field narrowing
        (`entity.py`'s own Fix round 1, Finding 4) -- required here
        separately because a `Derived` is not a `Field` and this class does
        not subclass `SrneFieldEntity`, so it inherits none of that
        narrowing for free. Without this override, `coordinator._registers`
        being cumulative (never purged for a block reclassified UNSUPPORTED
        mid-poll -- coordinator.py's own Fix round 1, Finding 1) would let
        this class keep computing e.g. `load_power_l1 + <frozen, no-longer-
        updated load_power_l2>` forever after `load_power_l2`'s own block
        stops answering: `load_power_l2`'s OWN sensor correctly goes
        unavailable (via `SrneFieldEntity.available`), but `load_power_total`
        would keep publishing a plausible-looking, silently wrong number --
        exactly the failure class ("a confident wrong number on the
        dashboard") this whole project exists to avoid.
        """
        if not super().available:
            return False
        support = self.coordinator.support
        values = self.coordinator.data.values if self.coordinator.data else {}
        for field in self._input_fields:
            if support.get(field.block_addr) is BlockSupport.UNSUPPORTED:
                return False
            if field.key not in values:
                return False
        return True

    @property
    def native_value(self) -> object | None:
        if self.coordinator.data is None:
            return None
        return _display_value(
            self.coordinator.data.values.get(self._derived.key), self._derived.precision
        )

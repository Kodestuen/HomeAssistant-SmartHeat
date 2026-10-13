"""Diagnostic sensors that show what a SmartHeat zone has learned."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SmartHeatConfigEntry, zone_config
from .const import CONF_RETURN_SENSOR, CONF_SUPPLY_SENSOR, DOMAIN, SIGNAL_UPDATE
from .controller import Controller
from .entity import SmartHeatEntity


@dataclass(frozen=True, kw_only=True)
class SmartHeatSensorDescription(SensorEntityDescription):
    """Describes a SmartHeat diagnostic sensor."""

    value_fn: Callable[[Controller, float], float | None]
    needs_supply: bool = False
    needs_return: bool = False


SENSORS: tuple[SmartHeatSensorDescription, ...] = (
    SmartHeatSensorDescription(
        key="learned_overshoot",
        translation_key="learned_overshoot",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda c, now: c.model.flat_overshoot,
    ),
    SmartHeatSensorDescription(
        key="heat_storage_factor",
        translation_key="heat_storage_factor",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        needs_return=True,
        value_fn=lambda c, now: c.model.beta,
    ),
    SmartHeatSensorDescription(
        key="holding_difference",
        translation_key="holding_difference",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        needs_return=True,
        value_fn=lambda c, now: c.model.holding_delta,
    ),
    SmartHeatSensorDescription(
        key="learning_cycles",
        translation_key="learning_cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c, now: c.model.cycles,
    ),
    SmartHeatSensorDescription(
        key="last_peak_error",
        translation_key="last_peak_error",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda c, now: c.model.last_peak_error,
    ),
    SmartHeatSensorDescription(
        key="heat_up_rate",
        translation_key="heat_up_rate",
        native_unit_of_measurement="°C/h",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda c, now: c.model.heat_up_rate,
    ),
    SmartHeatSensorDescription(
        key="valve_duty",
        translation_key="valve_duty",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda c, now: c.duty_cycle(now),
    ),
    SmartHeatSensorDescription(
        key="supply_return_difference",
        translation_key="supply_return_difference",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        needs_supply=True,
        needs_return=True,
        value_fn=lambda c, now: c.supply_return_delta,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartHeatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the zone's diagnostic sensors, and remove those it no longer has."""
    config = zone_config(entry)
    has_supply = bool(config.get(CONF_SUPPLY_SENSOR))
    has_return = bool(config.get(CONF_RETURN_SENSOR))
    entity_registry = er.async_get(hass)
    provided: list[SmartHeatSensorDescription] = []
    for description in SENSORS:
        if (has_supply or not description.needs_supply) and (
            has_return or not description.needs_return
        ):
            provided.append(description)
        elif entity_id := entity_registry.async_get_entity_id(
            SENSOR_DOMAIN, DOMAIN, f"{entry.entry_id}_{description.key}"
        ):
            # The supply or return sensor it needs was removed from the zone: remove
            # the entity too, or it would linger as unavailable
            entity_registry.async_remove(entity_id)
    async_add_entities(SmartHeatSensor(entry, description) for description in provided)


class SmartHeatSensor(SmartHeatEntity, SensorEntity):
    """A learned value or statistic of the zone, updated with every control step."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: SmartHeatSensorDescription

    def __init__(
        self, entry: SmartHeatConfigEntry, description: SmartHeatSensorDescription
    ) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        value = self.entity_description.value_fn(
            self._controller, dt_util.utcnow().timestamp()
        )
        return None if value is None else round(value, 3)

    async def async_added_to_hass(self) -> None:
        """Follow the climate entity's control steps."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE.format(self._entry.entry_id),
                self.async_write_ha_state,
            )
        )

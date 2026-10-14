"""Config flow for SmartHeat."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from . import zone_config, zone_strategy
from .const import (
    CONF_AIM_OFFSET,
    CONF_AWAY_TEMP,
    CONF_COAST_WINDOW,
    CONF_ECO_TEMP,
    CONF_FROST_TEMP,
    CONF_HYSTERESIS,
    CONF_INVERT_VALVE,
    CONF_MAX_RETURN,
    CONF_MAX_TEMP,
    CONF_MIN_CLOSED,
    CONF_MIN_OPEN,
    CONF_MIN_TEMP,
    CONF_OUTSIDE_SENSOR,
    CONF_PULSE_FLUSH,
    CONF_PULSE_INTERVAL,
    CONF_RETURN_SENSOR,
    CONF_ROOM_SENSOR,
    CONF_ROOM_SILENT,
    CONF_SENSOR_TIMEOUT,
    CONF_STRATEGY,
    CONF_SUPPLY_SENSOR,
    CONF_SYSTEM_TYPE,
    CONF_VALVE,
    CONF_WINDOW_SENSOR,
    DOMAIN,
    STRATEGIES,
    STRATEGY_TICKLE,
    SYSTEM_TYPES,
    SYSTEM_UNDERFLOOR,
    default_options,
)

TEMPERATURE_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
)
OUTSIDE_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(
        filter=[
            selector.EntityFilterSelectorConfig(
                domain="sensor", device_class="temperature"
            ),
            selector.EntityFilterSelectorConfig(domain="weather"),
        ]
    )
)
FLUSH_GAP = 15  # minutes a pulse interval leaves at least after the minimum pulse


def _number(
    minimum: float, maximum: float, step: float, unit: str
) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


ENTITIES_SCHEMA = {
    vol.Required(CONF_VALVE): selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["switch", "input_boolean", "valve"])
    ),
    vol.Required(CONF_ROOM_SENSOR): TEMPERATURE_SENSOR,
    vol.Optional(CONF_RETURN_SENSOR): TEMPERATURE_SENSOR,
    vol.Optional(CONF_SUPPLY_SENSOR): TEMPERATURE_SENSOR,
    vol.Optional(CONF_WINDOW_SENSOR): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="binary_sensor")
    ),
}

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): selector.TextSelector(),
        vol.Required(
            CONF_SYSTEM_TYPE, default=SYSTEM_UNDERFLOOR
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=SYSTEM_TYPES,
                translation_key=CONF_SYSTEM_TYPE,
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
        **ENTITIES_SCHEMA,
    }
)

CELSIUS = UnitOfTemperature.CELSIUS
MINUTES = UnitOfTime.MINUTES
HOURS = UnitOfTime.HOURS
OPTIONS = {
    **ENTITIES_SCHEMA,
    vol.Required(CONF_INVERT_VALVE): selector.BooleanSelector(),
    vol.Required(CONF_MIN_TEMP): _number(5, 30, 0.5, CELSIUS),
    vol.Required(CONF_MAX_TEMP): _number(10, 35, 0.5, CELSIUS),
    vol.Required(CONF_ECO_TEMP): _number(5, 30, 0.5, CELSIUS),
    vol.Required(CONF_AWAY_TEMP): _number(5, 30, 0.5, CELSIUS),
    vol.Required(CONF_FROST_TEMP): _number(0, 15, 0.5, CELSIUS),
    vol.Required(CONF_HYSTERESIS): _number(0.1, 2, 0.1, CELSIUS),
    vol.Required(CONF_AIM_OFFSET): _number(-0.5, 0.5, 0.05, CELSIUS),
    vol.Required(CONF_MIN_OPEN): _number(0, 120, 1, MINUTES),
    vol.Required(CONF_MIN_CLOSED): _number(0, 120, 1, MINUTES),
    vol.Required(CONF_COAST_WINDOW): _number(10, 720, 5, MINUTES),
    vol.Required(CONF_MAX_RETURN): _number(20, 90, 1, CELSIUS),
    vol.Required(CONF_SENSOR_TIMEOUT): _number(5, 240, 1, MINUTES),
}
UNDERFLOOR_OPTIONS = {
    vol.Required(CONF_ROOM_SILENT): _number(0, 48, 1, HOURS),
    vol.Required(CONF_PULSE_INTERVAL): _number(30, 120, 5, MINUTES),
    vol.Required(CONF_PULSE_FLUSH): _number(5, 30, 1, MINUTES),
}
# Underfloor zones choose their control method; the outdoor temperature is for the
# tickle pulses. In the form they come right after the other entities.
UNDERFLOOR_ENTITIES = {
    vol.Optional(CONF_OUTSIDE_SENSOR): OUTSIDE_SENSOR,
    vol.Required(CONF_STRATEGY): selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=STRATEGIES,
            translation_key=CONF_STRATEGY,
            mode=selector.SelectSelectorMode.LIST,
        )
    ),
}


def options_schema(system_type: str) -> vol.Schema:
    """Return the settings form for a zone of this system type."""
    if system_type == SYSTEM_UNDERFLOOR:
        return vol.Schema(
            {
                **ENTITIES_SCHEMA,
                **UNDERFLOOR_ENTITIES,
                **OPTIONS,
                **UNDERFLOOR_OPTIONS,
            }
        )
    return vol.Schema(OPTIONS)


def _zones(
    hass: HomeAssistant, exclude: ConfigEntry | None = None
) -> list[dict[str, Any]]:
    """Return the settings of the SmartHeat zones, but for the one excluded."""
    return [
        zone_config(entry)
        for entry in hass.config_entries.async_entries(DOMAIN, include_ignore=False)
        if entry is not exclude
    ]


def _validate(
    data: dict[str, Any],
    system_type: str,
    other_zones: Iterable[dict[str, Any]] = (),
) -> dict[str, str]:
    """Return the errors in a zone's settings.

    other_zones are the settings of the other SmartHeat zones.
    """
    errors: dict[str, str] = {}
    if CONF_RETURN_SENSOR in data:
        if data[CONF_ROOM_SENSOR] == data[CONF_RETURN_SENSOR]:
            errors[CONF_RETURN_SENSOR] = "same_sensor"
    elif system_type == SYSTEM_UNDERFLOOR:
        # The heat a floor stores varies too much to close early by an average alone
        errors[CONF_RETURN_SENSOR] = "return_required"
    if CONF_MIN_TEMP in data and data[CONF_MIN_TEMP] >= data[CONF_MAX_TEMP]:
        errors[CONF_MAX_TEMP] = "min_above_max"
    outside = data.get(CONF_OUTSIDE_SENSOR)
    if outside and outside in (data[CONF_ROOM_SENSOR], data.get(CONF_RETURN_SENSOR)):
        errors[CONF_OUTSIDE_SENSOR] = "outside_same_sensor"
    if (
        CONF_PULSE_FLUSH in data
        and data[CONF_PULSE_FLUSH] > data[CONF_PULSE_INTERVAL] - FLUSH_GAP
    ):
        errors[CONF_PULSE_FLUSH] = "flush_too_long"
    # A pulse ends at its own loop's return temperature, not at a mix of loops: a zone
    # on tickle pulses can't share its return sensor with another zone, either way
    if return_sensor := data.get(CONF_RETURN_SENSOR):
        sharing = [
            zone_strategy(zone)
            for zone in other_zones
            if zone.get(CONF_RETURN_SENSOR) == return_sensor
        ]
        if sharing and data.get(CONF_STRATEGY) == STRATEGY_TICKLE:
            errors[CONF_STRATEGY] = "return_shared"
        elif STRATEGY_TICKLE in sharing:
            errors.setdefault(CONF_RETURN_SENSOR, "return_shared")
    return errors


class SmartHeatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a SmartHeat zone."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the zone's name, system type, valve and sensors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_VALVE: user_input[CONF_VALVE]})
            system_type = user_input[CONF_SYSTEM_TYPE]
            if not (errors := _validate(user_input, system_type, _zones(self.hass))):
                settings = dict(user_input)
                name = settings.pop(CONF_NAME)
                system_type = settings.pop(CONF_SYSTEM_TYPE)
                # Everything but the system type lives in options, so the options
                # flow can change it, including removing the optional sensors.
                return self.async_create_entry(
                    title=name,
                    data={CONF_SYSTEM_TYPE: system_type},
                    options={**default_options(system_type), **settings},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SmartHeatOptionsFlow:
        """Return the options flow."""
        return SmartHeatOptionsFlow()


class SmartHeatOptionsFlow(OptionsFlow):
    """Change a zone's entities and tuning."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show all settings, prefilled with the current values."""
        errors: dict[str, str] = {}
        system_type = self.config_entry.data[CONF_SYSTEM_TYPE]
        if user_input is not None:
            self._async_abort_entries_match({CONF_VALVE: user_input[CONF_VALVE]})
            others = _zones(self.hass, self.config_entry)
            if not (errors := _validate(user_input, system_type, others)):
                return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                options_schema(system_type),
                user_input or zone_config(self.config_entry),
            ),
            errors=errors,
        )

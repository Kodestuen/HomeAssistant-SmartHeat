"""Config flow for SmartHeat."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME, UnitOfTemperature, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import zone_config
from .const import (
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
    CONF_RETURN_SENSOR,
    CONF_ROOM_SENSOR,
    CONF_SENSOR_TIMEOUT,
    CONF_SUPPLY_SENSOR,
    CONF_SYSTEM_TYPE,
    CONF_VALVE,
    CONF_WINDOW_SENSOR,
    DOMAIN,
    SYSTEM_TYPES,
    SYSTEM_UNDERFLOOR,
    default_options,
)

TEMPERATURE_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
)


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
OPTIONS_SCHEMA = vol.Schema(
    {
        **ENTITIES_SCHEMA,
        vol.Required(CONF_INVERT_VALVE): selector.BooleanSelector(),
        vol.Required(CONF_MIN_TEMP): _number(5, 30, 0.5, CELSIUS),
        vol.Required(CONF_MAX_TEMP): _number(10, 35, 0.5, CELSIUS),
        vol.Required(CONF_ECO_TEMP): _number(5, 30, 0.5, CELSIUS),
        vol.Required(CONF_AWAY_TEMP): _number(5, 30, 0.5, CELSIUS),
        vol.Required(CONF_FROST_TEMP): _number(0, 15, 0.5, CELSIUS),
        vol.Required(CONF_HYSTERESIS): _number(0.1, 2, 0.1, CELSIUS),
        vol.Required(CONF_MIN_OPEN): _number(0, 120, 1, MINUTES),
        vol.Required(CONF_MIN_CLOSED): _number(0, 120, 1, MINUTES),
        vol.Required(CONF_COAST_WINDOW): _number(10, 720, 5, MINUTES),
        vol.Required(CONF_MAX_RETURN): _number(20, 90, 1, CELSIUS),
        vol.Required(CONF_SENSOR_TIMEOUT): _number(5, 240, 1, MINUTES),
    }
)


def _validate(data: dict[str, Any], system_type: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    if CONF_RETURN_SENSOR in data:
        if data[CONF_ROOM_SENSOR] == data[CONF_RETURN_SENSOR]:
            errors[CONF_RETURN_SENSOR] = "same_sensor"
    elif system_type == SYSTEM_UNDERFLOOR:
        # The heat a floor stores varies too much to close early by an average alone
        errors[CONF_RETURN_SENSOR] = "return_required"
    if CONF_MIN_TEMP in data and data[CONF_MIN_TEMP] >= data[CONF_MAX_TEMP]:
        errors[CONF_MAX_TEMP] = "min_above_max"
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
            if not (errors := _validate(user_input, user_input[CONF_SYSTEM_TYPE])):
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
        if user_input is not None:
            self._async_abort_entries_match({CONF_VALVE: user_input[CONF_VALVE]})
            system_type = self.config_entry.data[CONF_SYSTEM_TYPE]
            if not (errors := _validate(user_input, system_type)):
                return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, user_input or zone_config(self.config_entry)
            ),
            errors=errors,
        )

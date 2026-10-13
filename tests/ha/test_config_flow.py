"""Tests for the SmartHeat config and options flows."""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.smartheat.const import (
    CONF_HYSTERESIS,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_RETURN_SENSOR,
    CONF_ROOM_SENSOR,
    CONF_SUPPLY_SENSOR,
    CONF_SYSTEM_TYPE,
    CONF_VALVE,
    DOMAIN,
    SYSTEM_RADIATOR,
    SYSTEM_UNDERFLOOR,
    default_options,
)

ENTITIES = {
    CONF_VALVE: "switch.living_room_valve",
    CONF_ROOM_SENSOR: "sensor.living_room_temperature",
    CONF_RETURN_SENSOR: "sensor.living_room_return",
}


def zone_entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={CONF_SYSTEM_TYPE: SYSTEM_UNDERFLOOR},
        options={**default_options(SYSTEM_UNDERFLOOR), **ENTITIES, **options},
    )


async def test_user_flow_creates_zone(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch("custom_components.smartheat.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Living room", CONF_SYSTEM_TYPE: SYSTEM_UNDERFLOOR, **ENTITIES},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room"
    assert result["data"] == {CONF_SYSTEM_TYPE: SYSTEM_UNDERFLOOR}
    assert result["options"] == {**default_options(SYSTEM_UNDERFLOOR), **ENTITIES}


async def test_room_and_return_sensor_must_differ(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Living room",
            CONF_SYSTEM_TYPE: SYSTEM_RADIATOR,
            **ENTITIES,
            CONF_RETURN_SENSOR: ENTITIES[CONF_ROOM_SENSOR],
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_RETURN_SENSOR: "same_sensor"}


async def test_valve_used_twice_aborts(hass: HomeAssistant) -> None:
    zone_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Kitchen", CONF_SYSTEM_TYPE: SYSTEM_RADIATOR, **ENTITIES},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_changes_settings_and_removes_sensor(
    hass: HomeAssistant,
) -> None:
    entry = zone_entry(**{CONF_SUPPLY_SENSOR: "sensor.living_room_supply"})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**default_options(SYSTEM_UNDERFLOOR), **ENTITIES, CONF_HYSTERESIS: 0.5},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HYSTERESIS] == 0.5
    assert CONF_SUPPLY_SENSOR not in entry.options


async def test_options_flow_rejects_min_above_max(hass: HomeAssistant) -> None:
    entry = zone_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **default_options(SYSTEM_UNDERFLOOR),
            **ENTITIES,
            CONF_MIN_TEMP: 25,
            CONF_MAX_TEMP: 20,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAX_TEMP: "min_above_max"}

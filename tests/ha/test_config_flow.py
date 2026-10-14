"""Tests for the SmartHeat config and options flows."""

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.smartheat.const import (
    CONF_AIM_OFFSET,
    CONF_HYSTERESIS,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_RETURN_SENSOR,
    CONF_ROOM_SENSOR,
    CONF_ROOM_SILENT,
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
NO_RETURN = {key: value for key, value in ENTITIES.items() if key != CONF_RETURN_SENSOR}


def zone_entry(system_type: str = SYSTEM_UNDERFLOOR, **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={CONF_SYSTEM_TYPE: system_type},
        options={**default_options(system_type), **ENTITIES, **options},
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


async def test_radiator_zone_without_return_sensor(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    with patch("custom_components.smartheat.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Bedroom", CONF_SYSTEM_TYPE: SYSTEM_RADIATOR, **NO_RETURN},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SYSTEM_TYPE: SYSTEM_RADIATOR}
    assert result["options"] == {**default_options(SYSTEM_RADIATOR), **NO_RETURN}


async def test_underfloor_zone_needs_return_sensor(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Living room", CONF_SYSTEM_TYPE: SYSTEM_UNDERFLOOR, **NO_RETURN},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_RETURN_SENSOR: "return_required"}


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


async def test_options_flow_removes_return_sensor_of_radiator_zone(
    hass: HomeAssistant,
) -> None:
    entry = zone_entry(SYSTEM_RADIATOR)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**default_options(SYSTEM_RADIATOR), **NO_RETURN}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_RETURN_SENSOR not in entry.options


async def test_options_flow_keeps_return_sensor_of_underfloor_zone(
    hass: HomeAssistant,
) -> None:
    entry = zone_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**default_options(SYSTEM_UNDERFLOOR), **NO_RETURN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_RETURN_SENSOR: "return_required"}
    assert entry.options[CONF_RETURN_SENSOR] == ENTITIES[CONF_RETURN_SENSOR]


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


@pytest.mark.parametrize(
    ("system_type", "has_silent_option"),
    [(SYSTEM_UNDERFLOOR, True), (SYSTEM_RADIATOR, False)],
)
async def test_room_sensor_silent_option_for_underfloor_zones(
    hass: HomeAssistant, system_type: str, has_silent_option: bool
) -> None:
    entry = zone_entry(system_type)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    fields = {str(key) for key in result["data_schema"].schema}
    assert (CONF_ROOM_SILENT in fields) is has_silent_option
    assert CONF_HYSTERESIS in fields

    options = {**default_options(system_type), **ENTITIES}
    if has_silent_option:
        options[CONF_ROOM_SILENT] = 12
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], options
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options.get(CONF_ROOM_SILENT) == (12 if has_silent_option else None)


async def test_zone_from_before_the_silent_option_gets_its_default(
    hass: HomeAssistant,
) -> None:
    options = {**default_options(SYSTEM_UNDERFLOOR), **ENTITIES}
    del options[CONF_ROOM_SILENT]  # saved by SmartHeat 0.3.0
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={CONF_SYSTEM_TYPE: SYSTEM_UNDERFLOOR},
        options=options,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    [field] = [key for key in result["data_schema"].schema if key == CONF_ROOM_SILENT]
    assert field.description == {"suggested_value": 6}


@pytest.mark.parametrize("system_type", [SYSTEM_UNDERFLOOR, SYSTEM_RADIATOR])
async def test_aim_offset_option_for_every_zone(
    hass: HomeAssistant, system_type: str
) -> None:
    options = {**default_options(system_type), **ENTITIES}
    del options[CONF_AIM_OFFSET]  # saved by SmartHeat 0.3
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={CONF_SYSTEM_TYPE: system_type},
        options=options,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    [field] = [key for key in schema if key == CONF_AIM_OFFSET]
    assert field.description == {"suggested_value": 0.0}
    number = schema[field].config
    assert (number["min"], number["max"], number["step"]) == (-0.5, 0.5, 0.05)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**options, CONF_AIM_OFFSET: 0.15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_AIM_OFFSET] == 0.15

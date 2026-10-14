"""Tests for the SmartHeat config and options flows."""

from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.smartheat.const import (
    CONF_AIM_OFFSET,
    CONF_HYSTERESIS,
    CONF_MAX_TEMP,
    CONF_MIN_TEMP,
    CONF_OUTSIDE_SENSOR,
    CONF_PULSE_FLUSH,
    CONF_PULSE_INTERVAL,
    CONF_RETURN_SENSOR,
    CONF_ROOM_SENSOR,
    CONF_ROOM_SILENT,
    CONF_STRATEGY,
    CONF_SUPPLY_SENSOR,
    CONF_SYSTEM_TYPE,
    CONF_VALVE,
    CONF_WINDOW_SENSOR,
    DOMAIN,
    STRATEGY_THERMOSTAT,
    STRATEGY_TICKLE,
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


async def test_new_zones_start_on_the_thermostat(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    # The control method is chosen later, under Configure
    assert CONF_STRATEGY not in {str(key) for key in result["data_schema"].schema}

    with patch("custom_components.smartheat.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_NAME: "Living room", CONF_SYSTEM_TYPE: SYSTEM_UNDERFLOOR, **ENTITIES},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_STRATEGY] == STRATEGY_THERMOSTAT
    assert result["options"][CONF_PULSE_INTERVAL] == 45
    assert result["options"][CONF_PULSE_FLUSH] == 12
    assert CONF_OUTSIDE_SENSOR not in result["options"]


async def test_options_form_of_each_system_type(hass: HomeAssistant) -> None:
    fields: dict[str, list[str]] = {}
    for system_type in (SYSTEM_UNDERFLOOR, SYSTEM_RADIATOR):
        entry = zone_entry(system_type)
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        fields[system_type] = [str(key) for key in result["data_schema"].schema]

    # Every zone has the aim offset. Underfloor zones also choose their control
    # method, with the tickle's settings and outdoor temperature.
    assert CONF_AIM_OFFSET in fields[SYSTEM_RADIATOR]
    assert set(fields[SYSTEM_UNDERFLOOR]) - set(fields[SYSTEM_RADIATOR]) == {
        CONF_OUTSIDE_SENSOR,
        CONF_STRATEGY,
        CONF_ROOM_SILENT,
        CONF_PULSE_INTERVAL,
        CONF_PULSE_FLUSH,
    }
    # The outdoor temperature comes with the other entities, then the method
    assert fields[SYSTEM_UNDERFLOOR][:7] == [
        CONF_VALVE,
        CONF_ROOM_SENSOR,
        CONF_RETURN_SENSOR,
        CONF_SUPPLY_SENSOR,
        CONF_WINDOW_SENSOR,
        CONF_OUTSIDE_SENSOR,
        CONF_STRATEGY,
    ]


async def test_zone_from_before_the_control_method_is_a_thermostat(
    hass: HomeAssistant,
) -> None:
    options = {**default_options(SYSTEM_UNDERFLOOR), **ENTITIES}
    for key in (CONF_STRATEGY, CONF_PULSE_INTERVAL, CONF_PULSE_FLUSH):
        del options[key]  # saved by SmartHeat 0.3
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={CONF_SYSTEM_TYPE: SYSTEM_UNDERFLOOR},
        options=options,
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    suggested = {
        str(key): key.description and key.description.get("suggested_value")
        for key in schema
    }
    assert suggested[CONF_STRATEGY] == STRATEGY_THERMOSTAT
    assert suggested[CONF_PULSE_INTERVAL] == 45
    assert suggested[CONF_PULSE_FLUSH] == 12
    assert suggested[CONF_OUTSIDE_SENSOR] is None

    selectors = {str(key): value for key, value in schema.items()}
    method = selectors[CONF_STRATEGY].config
    assert method["options"] == [STRATEGY_THERMOSTAT, STRATEGY_TICKLE]
    assert method["translation_key"] == "strategy"
    interval = selectors[CONF_PULSE_INTERVAL].config
    assert (interval["min"], interval["max"], interval["step"]) == (30, 120, 5)
    flush = selectors[CONF_PULSE_FLUSH].config
    assert (flush["min"], flush["max"], flush["step"]) == (5, 30, 1)
    # A temperature sensor or a weather entity
    assert selectors[CONF_OUTSIDE_SENSOR].config["filter"] == [
        {"domain": ["sensor"], "device_class": ["temperature"]},
        {"domain": ["weather"]},
    ]


async def test_options_flow_switches_to_tickle_pulses(hass: HomeAssistant) -> None:
    entry = zone_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **default_options(SYSTEM_UNDERFLOOR),
            **ENTITIES,
            CONF_STRATEGY: STRATEGY_TICKLE,
            CONF_OUTSIDE_SENSOR: "weather.home",
            CONF_PULSE_INTERVAL: 30,
            CONF_PULSE_FLUSH: 15,  # the longest a 30 minute interval allows
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_STRATEGY] == STRATEGY_TICKLE
    assert entry.options[CONF_OUTSIDE_SENSOR] == "weather.home"
    assert entry.options[CONF_PULSE_INTERVAL] == 30
    assert entry.options[CONF_PULSE_FLUSH] == 15


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        (
            {CONF_PULSE_INTERVAL: 30, CONF_PULSE_FLUSH: 16},
            {CONF_PULSE_FLUSH: "flush_too_long"},
        ),
        (
            {CONF_OUTSIDE_SENSOR: ENTITIES[CONF_ROOM_SENSOR]},
            {CONF_OUTSIDE_SENSOR: "outside_same_sensor"},
        ),
        (
            {CONF_OUTSIDE_SENSOR: ENTITIES[CONF_RETURN_SENSOR]},
            {CONF_OUTSIDE_SENSOR: "outside_same_sensor"},
        ),
    ],
)
async def test_options_flow_rejects_unusable_pulse_settings(
    hass: HomeAssistant, changes: dict[str, Any], errors: dict[str, str]
) -> None:
    entry = zone_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    options = {**default_options(SYSTEM_UNDERFLOOR), **ENTITIES, **changes}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], options
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors


@pytest.mark.parametrize(
    ("strategy", "errors"),
    [(STRATEGY_TICKLE, {CONF_STRATEGY: "return_shared"}), (STRATEGY_THERMOSTAT, {})],
)
async def test_tickle_pulses_need_a_return_sensor_of_their_own(
    hass: HomeAssistant, strategy: str, errors: dict[str, str]
) -> None:
    # Another zone has its return sensor on the same pipe, e.g. the manifold's
    zone_entry(
        **{
            CONF_VALVE: "switch.bedroom_valve",
            CONF_ROOM_SENSOR: "sensor.bedroom_temperature",
        }
    ).add_to_hass(hass)
    entry = zone_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**default_options(SYSTEM_UNDERFLOOR), **ENTITIES, CONF_STRATEGY: strategy},
    )
    if errors:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == errors
        assert entry.options[CONF_STRATEGY] == STRATEGY_THERMOSTAT
    else:
        # The thermostat may share it
        assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("other_strategy", "errors"),
    [
        (STRATEGY_TICKLE, {CONF_RETURN_SENSOR: "return_shared"}),
        (STRATEGY_THERMOSTAT, {}),
    ],
)
async def test_return_sensor_of_tickle_pulses_cant_be_taken_by_another_zone(
    hass: HomeAssistant, other_strategy: str, errors: dict[str, str]
) -> None:
    # The bedroom has its return sensor on the same pipe and may run tickle pulses
    zone_entry(
        **{
            CONF_VALVE: "switch.bedroom_valve",
            CONF_ROOM_SENSOR: "sensor.bedroom_temperature",
            CONF_STRATEGY: other_strategy,
        }
    ).add_to_hass(hass)
    entry = zone_entry(**{CONF_RETURN_SENSOR: "sensor.living_room_return_2"})
    entry.add_to_hass(hass)

    # A thermostat zone that takes it is turned down too
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**default_options(SYSTEM_UNDERFLOOR), **ENTITIES}
    )
    if errors:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == errors
        assert entry.options[CONF_RETURN_SENSOR] == "sensor.living_room_return_2"
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY

    # And so is a new zone
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch("custom_components.smartheat.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_NAME: "Kitchen",
                CONF_SYSTEM_TYPE: SYSTEM_RADIATOR,
                **ENTITIES,
                CONF_VALVE: "switch.kitchen_valve",
                CONF_ROOM_SENSOR: "sensor.kitchen_temperature",
            },
        )
        await hass.async_block_till_done()
    if errors:
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == errors
    else:
        assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_radiator_zones_have_no_control_method(hass: HomeAssistant) -> None:
    entry = zone_entry(SYSTEM_RADIATOR)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                **default_options(SYSTEM_RADIATOR),
                **ENTITIES,
                CONF_STRATEGY: STRATEGY_TICKLE,
            },
        )

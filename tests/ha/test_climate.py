"""Tests for the SmartHeat climate entity, with a fake valve and fake sensors."""

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache,
)

from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_ECO,
    PRESET_NONE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component

from custom_components.smartheat.const import (
    CONF_MIN_CLOSED,
    CONF_MIN_OPEN,
    CONF_RETURN_SENSOR,
    CONF_ROOM_SENSOR,
    CONF_SYSTEM_TYPE,
    CONF_VALVE,
    CONF_WINDOW_SENSOR,
    DOMAIN,
    SYSTEM_RADIATOR,
    default_options,
)

VALVE = "input_boolean.test_valve"
ROOM = "sensor.test_room"
RETURN = "sensor.test_return"
WINDOW = "binary_sensor.test_window"
CLIMATE = "climate.test_zone"


def set_temperature(hass: HomeAssistant, entity_id: str, value: float) -> None:
    hass.states.async_set(
        entity_id, str(value), {"unit_of_measurement": "°C", "device_class": "temperature"}
    )


async def setup_zone(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """Set up a radiator zone (initial overshoot 0.2 °C) without minimum valve times."""
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"test_valve": None}}
    )
    set_temperature(hass, ROOM, 21.0)
    set_temperature(hass, RETURN, 30.0)
    hass.states.async_set(WINDOW, STATE_OFF)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test zone",
        data={CONF_SYSTEM_TYPE: SYSTEM_RADIATOR},
        options={
            **default_options(SYSTEM_RADIATOR),
            CONF_VALVE: VALVE,
            CONF_ROOM_SENSOR: ROOM,
            CONF_RETURN_SENSOR: RETURN,
            CONF_WINDOW_SENSOR: WINDOW,
            CONF_MIN_OPEN: 0,
            CONF_MIN_CLOSED: 0,
            **options,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_idles_at_target(hass: HomeAssistant) -> None:
    await setup_zone(hass)
    state = hass.states.get(CLIMATE)
    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_TEMPERATURE] == 21.0
    assert state.attributes["current_temperature"] == 21.0
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    assert state.attributes["reason"] == "idle"
    assert hass.states.get(VALVE).state == STATE_OFF


async def test_opens_when_cold_and_closes_before_target(hass: HomeAssistant) -> None:
    await setup_zone(hass)
    set_temperature(hass, ROOM, 20.5)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_ON
    assert hass.states.get(CLIMATE).attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING

    set_temperature(hass, ROOM, 20.85)  # closes at 21 - 0.2
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_OFF
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["coasting"] is True
    assert attributes["reason"] == "coasting"


async def test_window_and_off_close_the_valve(hass: HomeAssistant) -> None:
    await setup_zone(hass)
    set_temperature(hass, ROOM, 20.0)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_ON

    hass.states.async_set(WINDOW, STATE_ON)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_OFF
    assert hass.states.get(CLIMATE).attributes["reason"] == "window_open"

    hass.states.async_set(WINDOW, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_ON

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: CLIMATE, ATTR_HVAC_MODE: HVACMode.OFF},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_OFF
    assert hass.states.get(CLIMATE).attributes[ATTR_HVAC_ACTION] == HVACAction.OFF


async def test_presets_and_target(hass: HomeAssistant) -> None:
    await setup_zone(hass)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: CLIMATE, ATTR_PRESET_MODE: PRESET_ECO},
        blocking=True,
    )
    assert hass.states.get(CLIMATE).attributes[ATTR_TEMPERATURE] == 19.0

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: CLIMATE, ATTR_TEMPERATURE: 22.5},
        blocking=True,
    )
    await hass.async_block_till_done()
    state = hass.states.get(CLIMATE)
    assert state.attributes[ATTR_TEMPERATURE] == 22.5
    assert state.attributes[ATTR_PRESET_MODE] == PRESET_NONE
    assert state.attributes["comfort_temperature"] == 22.5
    assert hass.states.get(VALVE).state == STATE_ON  # 21 °C is below 22.5 - 0.3


async def test_room_sensor_loss_closes_valve_after_timeout(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await setup_zone(hass)
    set_temperature(hass, ROOM, 20.0)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_ON

    hass.states.async_set(ROOM, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_ON
    assert hass.states.get(CLIMATE).attributes["reason"] == "room_sensor_unavailable"

    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_OFF
    assert hass.states.get(CLIMATE).attributes["reason"] == "room_sensor_lost"


async def test_restores_mode_and_target(hass: HomeAssistant) -> None:
    mock_restore_cache(
        hass,
        [
            State(
                CLIMATE,
                HVACMode.OFF,
                {"comfort_temperature": 22.5, ATTR_PRESET_MODE: PRESET_NONE},
            )
        ],
    )
    await setup_zone(hass)
    state = hass.states.get(CLIMATE)
    assert state.state == HVACMode.OFF
    assert state.attributes[ATTR_TEMPERATURE] == 22.5


async def test_diagnostic_sensors_and_reset_learning(hass: HomeAssistant) -> None:
    entry = await setup_zone(hass)
    assert hass.states.get("sensor.test_zone_learned_overshoot").state == "0.2"
    assert hass.states.get("sensor.test_zone_learning_cycles").state == "0"
    assert hass.states.get("sensor.test_zone_supply_return_difference") is None

    entry.runtime_data.controller.model.add_cycle(0.5, room_close=20.8, return_close=40.0)
    await hass.services.async_call(
        DOMAIN, "reset_learning", {ATTR_ENTITY_ID: CLIMATE}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.test_zone_learning_cycles").state == "0"
    assert hass.states.get("sensor.test_zone_learned_overshoot").state == "0.2"


async def test_learning_is_saved_on_unload(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_zone(hass)
    entry.runtime_data.controller.model.add_cycle(0.5, room_close=20.8, return_close=40.0)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["cycles"] == 1

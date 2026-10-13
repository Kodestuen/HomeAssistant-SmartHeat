"""Tests for the SmartHeat climate entity, with a fake valve and fake sensors."""

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
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
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.setup import async_setup_component

from custom_components.smartheat.const import (
    CONF_HYSTERESIS,
    CONF_MIN_CLOSED,
    CONF_MIN_OPEN,
    CONF_RETURN_SENSOR,
    CONF_ROOM_SENSOR,
    CONF_SUPPLY_SENSOR,
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
SUPPLY = "sensor.test_supply"
WINDOW = "binary_sensor.test_window"
CLIMATE = "climate.test_zone"


def set_temperature(hass: HomeAssistant, entity_id: str, value: float) -> None:
    hass.states.async_set(
        entity_id,
        str(value),
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )


async def setup_zone(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    """Set up a radiator zone (initial overshoot 0.2 °C) without minimum valve times.

    An option set to None is left out, like a sensor left empty in the settings.
    """
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"test_valve": None}}
    )
    set_temperature(hass, ROOM, 21.0)
    set_temperature(hass, RETURN, 30.0)
    hass.states.async_set(WINDOW, STATE_OFF)
    settings = {
        **default_options(SYSTEM_RADIATOR),
        CONF_VALVE: VALVE,
        CONF_ROOM_SENSOR: ROOM,
        CONF_RETURN_SENSOR: RETURN,
        CONF_WINDOW_SENSOR: WINDOW,
        CONF_MIN_OPEN: 0,
        CONF_MIN_CLOSED: 0,
        **options,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test zone",
        data={CONF_SYSTEM_TYPE: SYSTEM_RADIATOR},
        options={key: value for key, value in settings.items() if value is not None},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def save_options(
    hass: HomeAssistant, entry: MockConfigEntry, options: dict[str, Any]
) -> None:
    """Save the zone's settings through the options flow, which reloads it."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], options
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def zone_sensors(
    entity_registry: er.EntityRegistry, entry: MockConfigEntry
) -> dict[str, str]:
    """Return the entity IDs of the zone's registered sensors, by key."""
    prefix = f"{entry.entry_id}_"
    registered = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    return {
        item.unique_id.removeprefix(prefix): item.entity_id
        for item in registered
        if item.domain == "sensor"
    }


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


async def test_zone_without_return_sensor(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    entry = await setup_zone(hass, **{CONF_RETURN_SENSOR: None})
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["return_sensor_entity_id"] is None
    assert attributes["return_temperature"] is None  # RETURN exists, but isn't read
    sensors = zone_sensors(entity_registry, entry)
    assert "learned_overshoot" in sensors
    assert "heat_storage_factor" not in sensors
    assert "holding_difference" not in sensors

    set_temperature(hass, ROOM, 20.5)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_ON

    set_temperature(hass, ROOM, 20.85)  # closes at 21 - 0.2, the average overshoot
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_OFF
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["reason"] == "coasting"
    assert attributes["predicted_overshoot"] == 0.2
    assert attributes["close_threshold"] == 20.8

    # β and h, learned before the return sensor was removed, stay unused
    model = entry.runtime_data.controller.model
    model.beta, model.beta_samples, model.holding_delta = 0.5, 3, 4.0
    set_temperature(hass, ROOM, 20.9)
    await hass.async_block_till_done()
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["trained"] is False
    assert attributes["predicted_overshoot"] == 0.2


@pytest.mark.parametrize("return_sensor", [RETURN, None])
async def test_supply_sensor_attributes(
    hass: HomeAssistant, return_sensor: str | None
) -> None:
    set_temperature(hass, SUPPLY, 50.0)
    await setup_zone(
        hass, **{CONF_SUPPLY_SENSOR: SUPPLY, CONF_RETURN_SENSOR: return_sensor}
    )
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["supply_temperature"] == 50.0
    assert attributes["no_heat_source"] is False
    # These compare the supply with the return, so they need both sensors
    compared = {"supply_return_delta": 20.0, "loop_saturated": False}
    if return_sensor:
        assert {key: attributes[key] for key in compared} == compared
    else:
        assert compared.keys().isdisjoint(attributes)


@pytest.mark.parametrize(
    ("removed", "dependent_sensors"),
    [
        (
            CONF_RETURN_SENSOR,
            {"heat_storage_factor", "holding_difference", "supply_return_difference"},
        ),
        (CONF_SUPPLY_SENSOR, {"supply_return_difference"}),
    ],
)
async def test_removed_sensor_takes_its_diagnostic_sensors_along(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    removed: str,
    dependent_sensors: set[str],
) -> None:
    set_temperature(hass, SUPPLY, 50.0)
    entry = await setup_zone(hass, **{CONF_SUPPLY_SENSOR: SUPPLY})
    sensors = zone_sensors(entity_registry, entry)
    assert dependent_sensors <= sensors.keys()

    options = dict(entry.options)
    removed_entity_id = options.pop(removed)
    await save_options(hass, entry, options)
    assert zone_sensors(entity_registry, entry) == {
        key: entity_id
        for key, entity_id in sensors.items()
        if key not in dependent_sensors
    }
    for key in dependent_sensors:
        assert hass.states.get(sensors[key]) is None  # not left behind as unavailable

    # Adding the sensor back brings them back, under their old entity IDs
    await save_options(hass, entry, {**options, removed: removed_entity_id})
    assert zone_sensors(entity_registry, entry) == sensors
    for entity_id in sensors.values():
        assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


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

    model = entry.runtime_data.controller.model
    model.add_cycle(0.5, room_close=20.8, return_close=40.0)
    model.beta, model.beta_samples, model.holding_delta = 0.5, 3, 4.0
    set_temperature(hass, ROOM, 21.1)  # a new reading runs a control step
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["trained"] is True
    assert hass.states.get("sensor.test_zone_learning_cycles").state == "1"

    await hass.services.async_call(
        DOMAIN, "reset_learning", {ATTR_ENTITY_ID: CLIMATE}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["trained"] is False
    assert hass.states.get("sensor.test_zone_learning_cycles").state == "0"
    assert hass.states.get("sensor.test_zone_learned_overshoot").state == "0.2"


async def test_learning_is_saved_on_unload(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry = await setup_zone(hass)
    entry.runtime_data.controller.model.add_cycle(
        0.5, room_close=20.8, return_close=40.0
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["cycles"] == 1


async def test_exposes_zone_configuration(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> None:
    entry = await setup_zone(hass, **{CONF_HYSTERESIS: 0.5})
    state = hass.states.get(CLIMATE)
    configuration = {
        "system_type": SYSTEM_RADIATOR,
        "valve_entity_id": VALVE,
        "room_sensor_entity_id": ROOM,
        "return_sensor_entity_id": RETURN,
        "supply_sensor_entity_id": None,
        "window_sensor_entity_id": WINDOW,
        "invert_valve": False,
    }
    tuning = {
        key: entry.options[key]
        for key in (
            "hysteresis",
            "min_open_minutes",
            "min_closed_minutes",
            "coast_window_minutes",
            "max_return_temp",
            "sensor_timeout_minutes",
            "eco_temp",
            "away_temp",
            "frost_temp",
        )
    }
    expected = configuration | tuning
    assert {key: state.attributes[key] for key in expected} == expected
    assert state.attributes["trained"] is False
    # The recorder skips the configuration, but keeps what was learned
    unrecorded = state.state_info["unrecorded_attributes"]
    assert unrecorded.issuperset(expected)
    assert "trained" not in unrecorded

    [device] = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert device.model == "Radiator zone"
    assert device.model_id == SYSTEM_RADIATOR

    # Removing the window sensor reloads the zone, which rebuilds the attributes
    options = dict(entry.options)
    del options[CONF_WINDOW_SENSOR]
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["window_sensor_entity_id"] is None


async def test_device_takes_room_sensor_area_once(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    living_room = area_registry.async_create("Living room")
    entity_registry.async_get_or_create(
        "sensor", "test", "room", suggested_object_id="test_room"
    )
    entity_registry.async_update_entity(ROOM, area_id=living_room.id)
    entry = await setup_zone(hass)

    [device] = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert device.area_id == living_room.id
    # The area was set after the entities were added, so it is not in their IDs
    climate_unique_id = f"{entry.entry_id}_climate"
    assert (
        entity_registry.async_get_entity_id(CLIMATE_DOMAIN, DOMAIN, climate_unique_id)
        == CLIMATE
    )

    # From then on the area is the user's choice, also when they clear it
    kitchen = area_registry.async_create("Kitchen")
    for area_id in (kitchen.id, None):
        device_registry.async_update_device(device.id, area_id=area_id)
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert device_registry.async_get(device.id).area_id == area_id


@pytest.mark.parametrize(
    ("sensor_area", "sensor_device_area", "zone_area"),
    [
        (None, "Hallway", "Hallway"),
        ("Bedroom", "Hallway", "Bedroom"),
        (None, None, None),
    ],
)
async def test_device_takes_area_of_room_sensor_or_its_device(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    sensor_area: str | None,
    sensor_device_area: str | None,
    zone_area: str | None,
) -> None:
    # The room sensor's own area comes first, then that of its device, if any
    area_ids = {
        name: area_registry.async_create(name).id for name in ("Bedroom", "Hallway")
    }
    sensor_entry = MockConfigEntry(domain="test")
    sensor_entry.add_to_hass(hass)
    sensor_device = device_registry.async_get_or_create(
        config_entry_id=sensor_entry.entry_id, identifiers={("test", "thermometer")}
    )
    if sensor_device_area:
        device_registry.async_update_device(
            sensor_device.id, area_id=area_ids[sensor_device_area]
        )
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "room",
        suggested_object_id="test_room",
        device_id=sensor_device.id,
        original_name="Room temperature",  # needed for an area of its own
    )
    if sensor_area:
        entity_registry.async_update_entity(ROOM, area_id=area_ids[sensor_area])
    entry = await setup_zone(hass)

    [device] = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert device.area_id == (area_ids[zone_area] if zone_area else None)

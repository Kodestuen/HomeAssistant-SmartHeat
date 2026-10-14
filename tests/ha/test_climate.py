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
from homeassistant.util import dt as dt_util

from custom_components.smartheat.const import (
    CONF_AIM_OFFSET,
    CONF_HYSTERESIS,
    CONF_MIN_CLOSED,
    CONF_MIN_OPEN,
    CONF_RETURN_SENSOR,
    CONF_ROOM_SENSOR,
    CONF_ROOM_SILENT,
    CONF_SUPPLY_SENSOR,
    CONF_SYSTEM_TYPE,
    CONF_VALVE,
    CONF_WINDOW_SENSOR,
    DOMAIN,
    SYSTEM_RADIATOR,
    SYSTEM_UNDERFLOOR,
    default_options,
)
from custom_components.smartheat.controller import HOLD_INTERVAL_S, Grid, zone_phase

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


async def setup_zone(
    hass: HomeAssistant,
    *,
    system_type: str = SYSTEM_RADIATOR,
    entry_id: str | None = None,
    **options: Any,
) -> MockConfigEntry:
    """Set up a zone without minimum valve times, a radiator zone unless asked.

    A radiator zone starts from an overshoot of 0.2 °C, an underfloor zone from 0.4 °C.
    An option set to None is left out, like a sensor left empty in the settings.
    """
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"test_valve": None}}
    )
    set_temperature(hass, ROOM, 21.0)
    set_temperature(hass, RETURN, 30.0)
    hass.states.async_set(WINDOW, STATE_OFF)
    settings = {
        **default_options(system_type),
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
        data={CONF_SYSTEM_TYPE: system_type},
        options={key: value for key, value in settings.items() if value is not None},
        entry_id=entry_id,
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


async def test_aim_offset_moves_the_valve_not_the_target(hass: HomeAssistant) -> None:
    entry = await setup_zone(hass, **{CONF_AIM_OFFSET: 0.15})
    assert entry.runtime_data.controller.config.aim_offset == 0.15
    set_temperature(hass, ROOM, 20.8)  # opens below 21.15 - 0.3
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_ON
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes[ATTR_TEMPERATURE] == 21.0
    assert attributes["aim_offset"] == 0.15
    assert attributes["close_threshold"] == 20.95  # 21.15 - 0.2

    set_temperature(hass, ROOM, 20.9)  # without the offset, it would close here
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_ON
    set_temperature(hass, ROOM, 21.0)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_OFF
    assert hass.states.get(CLIMATE).attributes["reason"] == "coasting"


@pytest.mark.parametrize("system_type", [SYSTEM_UNDERFLOOR, SYSTEM_RADIATOR])
async def test_zone_saved_before_the_aim_offset_aims_at_the_target(
    hass: HomeAssistant, system_type: str
) -> None:
    entry = await setup_zone(hass, system_type=system_type, **{CONF_AIM_OFFSET: None})
    assert entry.runtime_data.controller.config.aim_offset == 0.0
    state = hass.states.get(CLIMATE)
    assert state.attributes["aim_offset"] == 0.0
    assert "aim_offset" in state.state_info["unrecorded_attributes"]


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

    # A radiator zone has no fallback: it stays closed
    for _ in range(3):
        await wait(hass, freezer, timedelta(minutes=20))
        attributes = hass.states.get(CLIMATE).attributes
        assert hass.states.get(VALVE).state == STATE_OFF
        assert attributes["reason"] == "room_sensor_lost"
        assert attributes["fallback"] is None
        assert attributes["fallback_duty"] is None


async def wait(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delta: timedelta
) -> None:
    """Let time pass; the zone takes one control step at its end."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def move_to(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, timestamp: float
) -> None:
    """Move the clock to a moment; the zone takes one control step there."""
    freezer.move_to(dt_util.utc_from_timestamp(timestamp))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_underfloor_zone_keeps_heating_without_room_sensor(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup_zone(hass, system_type=SYSTEM_UNDERFLOOR)
    grid = Grid(HOLD_INTERVAL_S, zone_phase(entry.entry_id, HOLD_INTERVAL_S))
    point = grid.next(dt_util.utcnow().timestamp())
    await move_to(hass, freezer, point + 60)  # a minute after one of its grid points
    hass.states.async_set(ROOM, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["reason"] == "room_sensor_unavailable"

    # After the timeout it holds the share the valve was open lately: 40 % so far.
    # Too late in this interval to open, so the valve stays closed.
    await wait(hass, freezer, timedelta(minutes=31))
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["reason"] == "room_sensor_lost"
    assert attributes["fallback"] == "room_sensor"
    assert attributes["fallback_duty"] == 40
    assert hass.states.get(VALVE).state == STATE_OFF

    # From the next grid point it opens for 40 % of 45 min: 18 min
    await move_to(hass, freezer, point + HOLD_INTERVAL_S)
    assert hass.states.get(VALVE).state == STATE_ON
    assert hass.states.get(CLIMATE).attributes["reason"] == "room_sensor_lost"
    # A valve that misses the command (here it is put back) is asked again
    hass.states.async_set(VALVE, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(VALVE).state == STATE_OFF
    await wait(hass, freezer, timedelta(minutes=1))
    assert hass.states.get(VALVE).state == STATE_ON
    await wait(hass, freezer, timedelta(minutes=16))
    assert hass.states.get(VALVE).state == STATE_ON
    await wait(hass, freezer, timedelta(minutes=1))
    assert hass.states.get(VALVE).state == STATE_OFF

    # The room sensor is back: normal control, and the share is no longer shown
    set_temperature(hass, ROOM, 20.0)
    await hass.async_block_till_done()
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["reason"] == "heating"
    assert attributes["fallback"] is None
    assert attributes["fallback_duty"] is None


@pytest.mark.parametrize("system_type", [SYSTEM_UNDERFLOOR, SYSTEM_RADIATOR])
async def test_room_sensor_check_attributes(
    hass: HomeAssistant, system_type: str
) -> None:
    await setup_zone(hass, system_type=system_type)
    state = hass.states.get(CLIMATE)
    assert state.attributes["fallback"] is None
    assert state.attributes["fallback_duty"] is None
    underfloor = system_type == SYSTEM_UNDERFLOOR
    if underfloor:
        assert state.attributes["room_silent"] is False
        assert state.attributes["room_sensor_silent_hours"] == 6
    else:
        assert "room_silent" not in state.attributes
        assert "room_sensor_silent_hours" not in state.attributes
    # The option is configuration, the rest is recorded
    unrecorded = state.state_info["unrecorded_attributes"]
    assert "room_sensor_silent_hours" in unrecorded
    assert unrecorded.isdisjoint({"fallback", "fallback_duty", "room_silent"})


@pytest.mark.parametrize(
    ("platform", "checked"),
    [
        ("test", True),
        ("template", False),  # a helper writes only when its sources change
        (None, False),  # no registry entry, like a state set through the API
    ],
)
async def test_silent_room_sensor_counts_as_lost(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    platform: str | None,
    checked: bool,
) -> None:
    if platform is not None:
        entity_registry.async_get_or_create(
            "sensor", platform, "room", suggested_object_id="test_room"
        )
    entry = await setup_zone(
        hass, system_type=SYSTEM_UNDERFLOOR, **{CONF_ROOM_SILENT: 1}
    )
    assert hass.states.get(CLIMATE).attributes["room_silent"] is False
    # The last report comes a minute after one of the zone's grid points, so the
    # sensor is found silent mid-interval, when the duty hold doesn't open the valve
    grid = Grid(HOLD_INTERVAL_S, zone_phase(entry.entry_id, HOLD_INTERVAL_S))
    await move_to(hass, freezer, grid.next(dt_util.utcnow().timestamp()) + 60)
    set_temperature(hass, ROOM, 21.0)

    await wait(hass, freezer, timedelta(minutes=59))
    assert hass.states.get(CLIMATE).attributes["room_silent"] is False
    # An hour without a report: lost since that report, so the room sensor timeout
    # has passed, and the zone heats by the last day's average at once
    await wait(hass, freezer, timedelta(minutes=1))
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["room_silent"] is checked
    assert attributes["reason"] == ("room_sensor_lost" if checked else "idle")
    assert attributes["fallback"] == ("room_sensor" if checked else None)
    assert hass.states.get(VALVE).state == STATE_OFF

    # Any report brings it back, also of the same value
    set_temperature(hass, ROOM, 21.0)
    await wait(hass, freezer, timedelta(minutes=1))
    attributes = hass.states.get(CLIMATE).attributes
    assert attributes["room_silent"] is False
    assert attributes["reason"] == "idle"
    assert attributes["fallback"] is None


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


@pytest.mark.parametrize("system_type", [SYSTEM_UNDERFLOOR, SYSTEM_RADIATOR])
async def test_saved_learning_has_the_open_share_of_underfloor_zones(
    hass: HomeAssistant, hass_storage: dict[str, Any], system_type: str
) -> None:
    entry = await setup_zone(hass, system_type=system_type)
    controller = entry.runtime_data.controller
    controller.model.add_cycle(0.5, room_close=20.8, return_close=40.0)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    expected = controller.model.to_dict()
    if system_type == SYSTEM_UNDERFLOOR:
        # Next to the model's values, so older versions still read them
        expected["calibration"] = {"duty": controller.calibration.duty}
    else:
        assert controller.calibration is None
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"] == expected


async def test_loads_learning_saved_by_version_0_3_0(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    entry_id = "01K7SMARTHEAT030ZONE000001"
    key = f"{DOMAIN}.{entry_id}"
    saved = {
        "flat_overshoot": 0.3,
        "beta": 1.8,
        "beta_samples": 4,
        "holding_delta": 4.2,
        "cycles": 6,
        "heat_up_rate": 0.5,
        "last_overshoot": 0.25,
        "last_peak_error": 0.05,
    }
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": saved}
    entry = await setup_zone(hass, system_type=SYSTEM_UNDERFLOOR, entry_id=entry_id)

    controller = entry.runtime_data.controller
    assert controller.model.to_dict() == saved
    assert controller.model.trained
    assert controller.calibration.duty == pytest.approx(0.4)  # not saved yet

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass_storage[key]["data"].keys() == {*saved, "calibration"}


async def test_open_share_alone_is_saved_at_most_every_half_hour(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup_zone(hass, system_type=SYSTEM_UNDERFLOOR)
    key = f"{DOMAIN}.{entry.entry_id}"
    controller = entry.runtime_data.controller

    def saved_duty() -> float | None:
        stored = hass_storage.pop(key, None)
        return stored and stored["data"]["calibration"]["duty"]

    assert saved_duty() is None
    controller.calibration.duty = 0.5  # counts as a change at the next control step
    await wait(hass, freezer, timedelta(minutes=1))
    await wait(hass, freezer, timedelta(seconds=11))  # the store's delay
    assert saved_duty() == pytest.approx(0.5, abs=0.005)

    controller.calibration.duty = 0.6
    await wait(hass, freezer, timedelta(minutes=1))
    await wait(hass, freezer, timedelta(seconds=11))
    assert saved_duty() is None  # saved a minute ago
    await wait(hass, freezer, timedelta(minutes=30))
    await wait(hass, freezer, timedelta(seconds=11))
    assert saved_duty() == pytest.approx(0.6, abs=0.005)  # the valve is closed

    # What the model learns is saved at once
    controller.model.add_cycle(0.5, room_close=20.8, return_close=40.0)
    await wait(hass, freezer, timedelta(minutes=1))
    await wait(hass, freezer, timedelta(seconds=11))
    assert hass_storage[key]["data"]["cycles"] == 1


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
            "aim_offset",
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

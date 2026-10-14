"""Climate entity for a SmartHeat zone."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    PRESET_AWAY,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.weather import (
    ATTR_WEATHER_TEMPERATURE,
    ATTR_WEATHER_TEMPERATURE_UNIT,
    DOMAIN as WEATHER_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    UnitOfTemperature,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import SmartHeatConfigEntry, learning_data, zone_config, zone_strategy
from .const import (
    ATTR_OUTSIDE_SENSOR_ENTITY_ID,
    ATTR_RETURN_SENSOR_ENTITY_ID,
    ATTR_ROOM_SENSOR_ENTITY_ID,
    ATTR_SUPPLY_SENSOR_ENTITY_ID,
    ATTR_SYSTEM_TYPE,
    ATTR_VALVE_ENTITY_ID,
    ATTR_WINDOW_SENSOR_ENTITY_ID,
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
    DEFAULT_TARGET,
    PRESET_FROST,
    SIGNAL_UPDATE,
    SYSTEM_UNDERFLOOR,
)
from .controller import Inputs
from .entity import SmartHeatEntity
from .tickle import TickleController

_LOGGER = logging.getLogger(__name__)

SERVICE_RESET_LEARNING = "reset_learning"
UPDATE_INTERVAL = timedelta(minutes=1)
COMMAND_RETRY_S = 60.0  # resend a command the valve hasn't followed after this long
SAVE_DELAY_S = 10.0
CALIBRATION_SAVE_S = 1800.0  # a calibration that changed alone is saved this seldom
ATTR_COMFORT_TEMPERATURE = "comfort_temperature"
# What tickle pulses show. The recorder skips these: the return target has its own
# sensor, the outdoor temperature its own entity, and pulse times follow a fixed grid.
ATTR_RETURN_THRESHOLD = "return_threshold"
ATTR_NEXT_PULSE = "next_pulse"
ATTR_OUTSIDE_TEMPERATURE = "outside_temperature"
VALVE_STATES = {"open": True, "opening": True, "closed": False, "closing": False}
# Tuning options that are also attributes, under their option keys
TUNING_ATTRIBUTES = (
    CONF_HYSTERESIS,
    CONF_AIM_OFFSET,
    CONF_MIN_OPEN,
    CONF_MIN_CLOSED,
    CONF_COAST_WINDOW,
    CONF_MAX_RETURN,
    CONF_SENSOR_TIMEOUT,
    CONF_ECO_TEMP,
    CONF_AWAY_TEMP,
    CONF_FROST_TEMP,
)
# The same, for the options only underfloor zones have
UNDERFLOOR_TUNING_ATTRIBUTES = (CONF_ROOM_SILENT, CONF_PULSE_INTERVAL, CONF_PULSE_FLUSH)
# Helpers that may write their state only when one of their sources changes, so a
# steady room temperature from them would look like a silent sensor
SILENT_HELPER_PLATFORMS = frozenset(
    {
        "template",
        "min_max",
        "filter",
        "statistics",
        "group",
        "derivative",
        "compensation",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartHeatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the zone's climate entity."""
    async_add_entities([SmartHeatClimate(entry)])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_RESET_LEARNING, None, "async_reset_learning"
    )


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _celsius(value: Any, unit: str | None) -> float | None:
    """Return a temperature in °C, or None if it isn't a number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if unit in (UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
        number = TemperatureConverter.convert(number, unit, UnitOfTemperature.CELSIUS)
    return number


class SmartHeatClimate(SmartHeatEntity, ClimateEntity, RestoreEntity):
    """Thermostat that opens and closes the zone's valve."""

    _attr_name = None
    _attr_translation_key = "zone"
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = [PRESET_NONE, PRESET_ECO, PRESET_AWAY, PRESET_FROST]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    # The zone's configuration only changes with a reload, so the recorder skips it
    _unrecorded_attributes = frozenset(
        {
            ATTR_SYSTEM_TYPE,
            CONF_STRATEGY,
            ATTR_VALVE_ENTITY_ID,
            ATTR_ROOM_SENSOR_ENTITY_ID,
            ATTR_RETURN_SENSOR_ENTITY_ID,
            ATTR_SUPPLY_SENSOR_ENTITY_ID,
            ATTR_WINDOW_SENSOR_ENTITY_ID,
            ATTR_OUTSIDE_SENSOR_ENTITY_ID,
            CONF_INVERT_VALVE,
            *TUNING_ATTRIBUTES,
            *UNDERFLOOR_TUNING_ATTRIBUTES,
            ATTR_RETURN_THRESHOLD,
            ATTR_NEXT_PULSE,
            ATTR_OUTSIDE_TEMPERATURE,
        }
    )

    def __init__(self, entry: SmartHeatConfigEntry) -> None:
        super().__init__(entry, "climate")
        config = zone_config(entry)
        self._store = entry.runtime_data.store
        self._underfloor = config[CONF_SYSTEM_TYPE] == SYSTEM_UNDERFLOOR
        tuning = TUNING_ATTRIBUTES
        if self._underfloor:
            tuning += UNDERFLOOR_TUNING_ATTRIBUTES
        self._valve: str = config[CONF_VALVE]
        self._room_sensor: str = config[CONF_ROOM_SENSOR]
        self._return_sensor: str | None = config.get(CONF_RETURN_SENSOR)
        self._supply_sensor: str | None = config.get(CONF_SUPPLY_SENSOR)
        self._window_sensor: str | None = config.get(CONF_WINDOW_SENSOR)
        # Only underfloor zones have one; only tickle pulses use it
        self._outside_sensor: str | None = (
            config.get(CONF_OUTSIDE_SENSOR) if self._underfloor else None
        )
        self._invert: bool = config[CONF_INVERT_VALVE]
        self._preset_temperatures = {
            PRESET_ECO: config[CONF_ECO_TEMP],
            PRESET_AWAY: config[CONF_AWAY_TEMP],
            PRESET_FROST: config[CONF_FROST_TEMP],
        }
        self._attr_min_temp = config[CONF_MIN_TEMP]
        self._attr_max_temp = config[CONF_MAX_TEMP]
        # Built once: changing the options reloads the zone
        self._config_attributes: dict[str, Any] = {
            ATTR_SYSTEM_TYPE: config[CONF_SYSTEM_TYPE],
            CONF_STRATEGY: zone_strategy(config),
            ATTR_VALVE_ENTITY_ID: self._valve,
            ATTR_ROOM_SENSOR_ENTITY_ID: self._room_sensor,
            ATTR_RETURN_SENSOR_ENTITY_ID: self._return_sensor or None,
            ATTR_SUPPLY_SENSOR_ENTITY_ID: self._supply_sensor or None,
            ATTR_WINDOW_SENSOR_ENTITY_ID: self._window_sensor or None,
            CONF_INVERT_VALVE: self._invert,
            **{key: config[key] for key in tuning},
        }
        if self._underfloor:
            self._config_attributes[ATTR_OUTSIDE_SENSOR_ENTITY_ID] = (
                self._outside_sensor or None
            )
        self._attr_hvac_mode = HVACMode.HEAT
        self._attr_preset_mode = PRESET_NONE
        self._comfort_temperature = DEFAULT_TARGET
        self._room: float | None = None
        self._return: float | None = None
        self._supply: float | None = None
        # Whether the room sensor's reports are checked; known once the entity is added
        self._watch_room_reports = False
        self._lock = asyncio.Lock()
        self._last_command: tuple[bool, float] | None = None
        self._saved_revisions = self._learning_revisions()
        self._saved_at: float | None = None  # when the store last wrote
        self._save_waiting = False  # a write is waiting in the store
        self._removed = False

    @property
    def target_temperature(self) -> float:
        """Return the temperature of the active preset, or the comfort target."""
        return self._preset_temperatures.get(
            self._attr_preset_mode, self._comfort_temperature
        )

    @property
    def current_temperature(self) -> float | None:
        """Return the room temperature."""
        return self._room

    @property
    def hvac_action(self) -> HVACAction:
        """Return heating while the valve is open (frost protection can open it when off)."""
        if self._controller.valve_open:
            return HVACAction.HEATING
        if self._attr_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explain what the controller is doing, and show how the zone is set up."""
        controller = self._controller
        decision = controller.last_decision
        share = decision.fallback_share if decision else None
        attributes: dict[str, Any] = {
            ATTR_COMFORT_TEMPERATURE: self._comfort_temperature,
            "reason": decision.reason if decision else None,
            # The lost sensor the valve works around, and the share of time it opens
            "fallback": decision.fallback if decision else None,
            "fallback_duty": None if share is None else round(100 * share),
            "predicted_overshoot": _round(
                decision.predicted_overshoot if decision else None
            ),
            "close_threshold": _round(decision.close_threshold if decision else None),
            # Without a return sensor the prediction is always the average
            "trained": controller.model.trained and bool(self._return_sensor),
            "coasting": controller.coasting,
            "return_temperature": self._return,
        }
        if isinstance(controller, TickleController):
            next_pulse = decision.next_pulse if decision else None
            attributes.update(
                {
                    # The return temperature that ends a pulse; null while none is used
                    ATTR_RETURN_THRESHOLD: _round(
                        decision.return_threshold if decision else None
                    ),
                    ATTR_NEXT_PULSE: (
                        None
                        if next_pulse is None
                        else dt_util.utc_from_timestamp(next_pulse).isoformat()
                    ),
                    # A reading, or the last one for up to 48 hours
                    ATTR_OUTSIDE_TEMPERATURE: _round(controller.outside_temperature),
                    "calibrating": controller.calibrating,
                }
            )
        if self._underfloor:
            attributes["room_silent"] = controller.room_silent
        if self._supply_sensor:
            attributes["supply_temperature"] = self._supply
            attributes["no_heat_source"] = controller.no_heat_source
        if self._supply_sensor and self._return_sensor:
            # These compare the supply with the return, so they need both sensors
            attributes["supply_return_delta"] = _round(controller.supply_return_delta)
            attributes["loop_saturated"] = controller.loop_saturated
        attributes.update(self._config_attributes)
        return attributes

    async def async_added_to_hass(self) -> None:
        """Restore the last settings and start listening."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._restore(last_state)
        # An underfloor zone checks that its room sensor still reports. Not checked:
        # helpers, which may stay silent while the temperature is steady, and entities
        # without a registry entry, such as states set through the API or YAML sensors
        # without a unique ID
        room = er.async_get(self.hass).async_get(self._room_sensor)
        self._watch_room_reports = (
            self._underfloor
            and room is not None
            and room.platform not in SILENT_HELPER_PLATFORMS
        )

        watched = [
            entity_id
            for entity_id in (
                self._valve,
                self._room_sensor,
                self._return_sensor,
                self._supply_sensor,
                self._window_sensor,
                self._outside_sensor,
            )
            if entity_id
        ]
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, watched, self._async_state_changed
            )
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, UPDATE_INTERVAL)
        )
        self.async_on_remove(async_at_started(self.hass, self._async_started))

    async def async_will_remove_from_hass(self) -> None:
        """Stop controlling once removed."""
        self._removed = True
        await super().async_will_remove_from_hass()

    def _restore(self, last_state: State) -> None:
        if last_state.state in (HVACMode.HEAT, HVACMode.OFF):
            self._attr_hvac_mode = HVACMode(last_state.state)
        comfort = last_state.attributes.get(ATTR_COMFORT_TEMPERATURE)
        if isinstance(comfort, (int, float)):
            self._comfort_temperature = float(comfort)
        if (
            preset := last_state.attributes.get(ATTR_PRESET_MODE)
        ) in self._attr_preset_modes:
            self._attr_preset_mode = preset

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the comfort target; this leaves any preset."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        self._comfort_temperature = float(temperature)
        self._attr_preset_mode = PRESET_NONE
        await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Turn the zone on or off."""
        self._attr_hvac_mode = hvac_mode
        await self._async_control()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Switch to a preset target."""
        self._attr_preset_mode = preset_mode
        await self._async_control()

    async def async_reset_learning(self) -> None:
        """Forget what the zone's control method learned, and save that soon.

        The thermostat forgets its overshoot model. Tickle pulses start their return
        offset afresh from the model's h, and keep the model. The valve's open share
        stays in both.
        """
        self._controller.reset_learning()
        await self._async_control(save_soon=True)

    async def _async_started(self, hass: HomeAssistant) -> None:
        await self._async_control()

    async def _async_tick(self, now: datetime) -> None:
        await self._async_control()

    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        self.hass.async_create_task(self._async_control())

    async def _async_control(self, save_soon: bool = False) -> None:
        """Read the inputs, let the controller decide and move the valve."""
        if self._removed:
            return
        async with self._lock:
            now = dt_util.utcnow().timestamp()
            inputs = self._read_inputs()
            decision = self._controller.update(inputs, now)
            if (
                inputs.valve_open is not None
                and decision.valve_open != inputs.valve_open
            ):
                await self._async_command_valve(decision.valve_open, now)
            self._save_learning(now, save_soon)
        if self._removed:
            return
        self.async_write_ha_state()
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self._entry.entry_id))

    def _read_inputs(self) -> Inputs:
        self._room = self._temperature(self._room_sensor)
        self._return = self._temperature(self._return_sensor)
        self._supply = self._temperature(self._supply_sensor)
        valve_open, valve_changed_at = self._valve_state()
        window = self._window_sensor and self.hass.states.get(self._window_sensor)
        return Inputs(
            room=self._room,
            return_temp=self._return,
            target=self.target_temperature,
            heating_enabled=self._attr_hvac_mode == HVACMode.HEAT,
            supply=self._supply,
            window_open=bool(window) and window.state == STATE_ON,
            valve_open=valve_open,
            valve_changed_at=valve_changed_at,
            room_updated_at=self._room_reported_at(),
            outside=self._outside_temperature(),
            # Tickle pulses calibrate only at the targets a room is held at for long
            calibrate=self._attr_preset_mode in (PRESET_NONE, PRESET_ECO),
        )

    def _room_reported_at(self) -> float | None:
        """Return when the room sensor last reported, if its reports are watched."""
        if not self._watch_room_reports:
            return None
        if (state := self.hass.states.get(self._room_sensor)) is None:
            return None
        # Unlike last_changed, this also moves when the same value is written again.
        # Some integrations, MQTT and deCONZ among them, write a reading only when it
        # changes, so for their sensors it is the time of the last change.
        return state.last_reported.timestamp()

    def _temperature(self, entity_id: str | None) -> float | None:
        """Return a sensor's temperature in °C, or None if it has none."""
        if entity_id is None or (state := self.hass.states.get(entity_id)) is None:
            return None
        return _celsius(state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))

    def _outside_temperature(self) -> float | None:
        """Return the outdoor temperature in °C: a sensor's, or a weather entity's."""
        entity_id = self._outside_sensor
        if entity_id is None or split_entity_id(entity_id)[0] != WEATHER_DOMAIN:
            return self._temperature(entity_id)
        if (state := self.hass.states.get(entity_id)) is None:
            return None
        if state.state == STATE_UNAVAILABLE:
            return None
        # In the unit the weather entity shows, which follows the unit system
        return _celsius(
            state.attributes.get(ATTR_WEATHER_TEMPERATURE),
            state.attributes.get(ATTR_WEATHER_TEMPERATURE_UNIT),
        )

    def _valve_state(self) -> tuple[bool | None, float | None]:
        """Return whether water flows, and since when; None if unknown."""
        if (state := self.hass.states.get(self._valve)) is None:
            return None, None
        if split_entity_id(self._valve)[0] == "valve":
            energized = VALVE_STATES.get(state.state)
        else:
            energized = {STATE_ON: True, STATE_OFF: False}.get(state.state)
        if energized is None:
            return None, None
        return energized != self._invert, state.last_changed.timestamp()

    async def _async_command_valve(self, open_valve: bool, now: float) -> None:
        if self._last_command is not None:
            last_open, sent_at = self._last_command
            if last_open == open_valve and now - sent_at < COMMAND_RETRY_S:
                return  # give the valve time to report its new state
        self._last_command = (open_valve, now)
        domain = split_entity_id(self._valve)[0]
        energize = open_valve != self._invert
        if domain == "valve":
            service = "open_valve" if energize else "close_valve"
        else:
            service = SERVICE_TURN_ON if energize else SERVICE_TURN_OFF
        try:
            await self.hass.services.async_call(
                domain, service, {ATTR_ENTITY_ID: self._valve}, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "%s could not %s %s: %s", self.entity_id, service, self._valve, err
            )

    def _learning_revisions(self) -> tuple[int, int | None]:
        calibration = self._controller.calibration
        return (
            self._controller.model.revision,
            None if calibration is None else calibration.revision,
        )

    def _save_learning(self, now: float, soon: bool = False) -> None:
        """Save soon after the model learns; a calibration change alone can wait.

        A calibration change within half an hour of the last write is written at the
        end of that half hour. Until then the store holds it, and it also writes it
        when Home Assistant stops.
        """
        revisions = self._learning_revisions()
        if revisions == self._saved_revisions:
            return
        calibration_only = not soon and revisions[0] == self._saved_revisions[0]
        self._saved_revisions = revisions
        if calibration_only and self._save_waiting:
            return  # the write waiting in the store takes this change along
        since = None if self._saved_at is None else now - self._saved_at
        if calibration_only and since is not None and 0 <= since < CALIBRATION_SAVE_S:
            # Written at the end of the half hour. Asked for only while no write
            # waits, as the store would put a waiting write off until then too.
            self._delay_save(CALIBRATION_SAVE_S - since)
        else:
            self._delay_save(SAVE_DELAY_S)

    def _delay_save(self, delay: float) -> None:
        self._save_waiting = True
        self._store.async_delay_save(self._data_to_save, delay)

    def _data_to_save(self) -> dict[str, Any]:
        """Return what the zone has learned; the store calls this as it writes."""
        self._save_waiting = False
        self._saved_at = dt_util.utcnow().timestamp()
        return learning_data(self._controller)

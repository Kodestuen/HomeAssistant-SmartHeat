"""SmartHeat: learning on/off valve control for water-based heating zones."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    CONF_AIM_OFFSET,
    CONF_COAST_WINDOW,
    CONF_FROST_TEMP,
    CONF_HYSTERESIS,
    CONF_MAX_RETURN,
    CONF_MIN_CLOSED,
    CONF_MIN_OPEN,
    CONF_ROOM_SENSOR,
    CONF_ROOM_SILENT,
    CONF_SENSOR_TIMEOUT,
    CONF_SYSTEM_TYPE,
    DOMAIN,
    INITIAL_OVERSHOOT,
    STORAGE_VERSION,
    SYSTEM_UNDERFLOOR,
    default_options,
)
from .controller import HOLD_INTERVAL_S, Controller, ControllerConfig, zone_phase
from .learning import OvershootModel, ZoneCalibration

PLATFORMS = [Platform.CLIMATE, Platform.SENSOR]


@dataclass
class SmartHeatData:
    """Runtime data shared by the entities of one zone."""

    controller: Controller
    store: Store[dict[str, Any]]


type SmartHeatConfigEntry = ConfigEntry[SmartHeatData]


def zone_config(entry: ConfigEntry) -> dict[str, Any]:
    """Return the zone settings: defaults, then setup data, then options."""
    return {
        **default_options(entry.data[CONF_SYSTEM_TYPE]),
        **entry.data,
        **entry.options,
    }


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, Any]]:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")


def learning_data(controller: Controller) -> dict[str, Any]:
    """Return what a zone has learned, as it is saved.

    The calibration sits next to the model's values, so a version that doesn't know it
    can still read the model.
    """
    data = controller.model.to_dict()
    if controller.calibration is not None:
        data["calibration"] = controller.calibration.to_dict()
    return data


async def async_setup_entry(hass: HomeAssistant, entry: SmartHeatConfigEntry) -> bool:
    """Set up a SmartHeat zone."""
    config = zone_config(entry)
    store = _store(hass, entry)
    data = await store.async_load() or {}
    model = OvershootModel.from_dict(data, INITIAL_OVERSHOOT[config[CONF_SYSTEM_TYPE]])
    # A floor that loses its room sensor keeps heating by the share its valve was open
    # over the last day. Radiator zones close the valve, as before, and need no share.
    underfloor = config[CONF_SYSTEM_TYPE] == SYSTEM_UNDERFLOOR
    controller = Controller(
        ControllerConfig(
            hysteresis=config[CONF_HYSTERESIS],
            aim_offset=config[CONF_AIM_OFFSET],
            min_open_s=config[CONF_MIN_OPEN] * 60,
            min_closed_s=config[CONF_MIN_CLOSED] * 60,
            coast_window_s=config[CONF_COAST_WINDOW] * 60,
            max_return=config[CONF_MAX_RETURN],
            sensor_timeout_s=config[CONF_SENSOR_TIMEOUT] * 60,
            frost_temp=config[CONF_FROST_TEMP],
            lost_room_hold=underfloor,
            grid_phase_s=zone_phase(entry.entry_id, HOLD_INTERVAL_S),
            room_silent_s=config.get(CONF_ROOM_SILENT, 0) * 3600 or None,
        ),
        model,
        ZoneCalibration.from_dict(data.get("calibration")) if underfloor else None,
    )
    entry.runtime_data = SmartHeatData(controller, store)
    # The zone's device is registered with its entities, so none yet means a new zone
    device_registry = dr.async_get(hass)
    new_zone = not dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if new_zone:
        _async_adopt_room_area(hass, entry, config[CONF_ROOM_SENSOR])
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartHeatConfigEntry) -> bool:
    """Unload a zone, saving what it has learned."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = entry.runtime_data
        await data.store.async_save(learning_data(data.controller))
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the learned data when a zone is removed."""
    await _store(hass, entry).async_remove()


@callback
def _async_adopt_room_area(
    hass: HomeAssistant, entry: ConfigEntry, room_sensor: str
) -> None:
    """Put a new zone's device in the room sensor's area.

    This runs once the platforms are set up, so the entities created with the zone
    keep IDs without the area. Entities added later get the area in their IDs, as on
    any device in an area. It runs only for a new zone and never replaces an area, so
    whatever the user chooses later stays.
    """
    if (room := er.async_get(hass).async_get(room_sensor)) is None:
        return
    device_registry = dr.async_get(hass)
    area_id = room.area_id
    if area_id is None and room.device_id is not None:
        room_device = device_registry.async_get(room.device_id)
        area_id = room_device.area_id if room_device else None
    if area_id is None:
        return
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if device.area_id is None:
            device_registry.async_update_device(device.id, area_id=area_id)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

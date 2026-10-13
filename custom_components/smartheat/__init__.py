"""SmartHeat: learning on/off valve control for water-based heating zones."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_COAST_WINDOW,
    CONF_FROST_TEMP,
    CONF_HYSTERESIS,
    CONF_MAX_RETURN,
    CONF_MIN_CLOSED,
    CONF_MIN_OPEN,
    CONF_SENSOR_TIMEOUT,
    CONF_SYSTEM_TYPE,
    DOMAIN,
    INITIAL_OVERSHOOT,
    STORAGE_VERSION,
    default_options,
)
from .controller import Controller, ControllerConfig
from .learning import OvershootModel

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


async def async_setup_entry(hass: HomeAssistant, entry: SmartHeatConfigEntry) -> bool:
    """Set up a SmartHeat zone."""
    config = zone_config(entry)
    store = _store(hass, entry)
    model = OvershootModel.from_dict(
        await store.async_load(), INITIAL_OVERSHOOT[config[CONF_SYSTEM_TYPE]]
    )
    controller = Controller(
        ControllerConfig(
            hysteresis=config[CONF_HYSTERESIS],
            min_open_s=config[CONF_MIN_OPEN] * 60,
            min_closed_s=config[CONF_MIN_CLOSED] * 60,
            coast_window_s=config[CONF_COAST_WINDOW] * 60,
            max_return=config[CONF_MAX_RETURN],
            sensor_timeout_s=config[CONF_SENSOR_TIMEOUT] * 60,
            frost_temp=config[CONF_FROST_TEMP],
        ),
        model,
    )
    entry.runtime_data = SmartHeatData(controller, store)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartHeatConfigEntry) -> bool:
    """Unload a zone, saving what it has learned."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = entry.runtime_data
        await data.store.async_save(data.controller.model.to_dict())
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the learned data when a zone is removed."""
    await _store(hass, entry).async_remove()


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

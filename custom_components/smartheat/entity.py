"""Base entity for SmartHeat."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from . import SmartHeatConfigEntry
from .const import CONF_SYSTEM_TYPE, DOMAIN, SYSTEM_UNDERFLOOR

MODELS = {SYSTEM_UNDERFLOOR: "Underfloor heating zone"}


class SmartHeatEntity(Entity):
    """An entity belonging to one SmartHeat zone (one device per zone)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: SmartHeatConfigEntry, key: str) -> None:
        self._entry = entry
        self._controller = entry.runtime_data.controller
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="SmartHeat",
            model=MODELS.get(entry.data[CONF_SYSTEM_TYPE], "Radiator zone"),
        )

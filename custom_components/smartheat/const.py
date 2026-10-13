"""Constants for SmartHeat.

Kept free of Home Assistant imports so the control logic can be tested without it.
"""

DOMAIN = "smartheat"
STORAGE_VERSION = 1
SIGNAL_UPDATE = "smartheat_update_{}"

# Entities (chosen in the config flow)
CONF_SYSTEM_TYPE = "system_type"
CONF_VALVE = "valve"
CONF_ROOM_SENSOR = "room_sensor"
CONF_RETURN_SENSOR = "return_sensor"
CONF_SUPPLY_SENSOR = "supply_sensor"
CONF_WINDOW_SENSOR = "window_sensor"

# Tuning (options flow)
CONF_INVERT_VALVE = "invert_valve"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_HYSTERESIS = "hysteresis"
CONF_MIN_OPEN = "min_open_minutes"
CONF_MIN_CLOSED = "min_closed_minutes"
CONF_COAST_WINDOW = "coast_window_minutes"
CONF_MAX_RETURN = "max_return_temp"
CONF_SENSOR_TIMEOUT = "sensor_timeout_minutes"
CONF_FROST_TEMP = "frost_temp"
CONF_ECO_TEMP = "eco_temp"
CONF_AWAY_TEMP = "away_temp"

SYSTEM_UNDERFLOOR = "underfloor"
SYSTEM_RADIATOR = "radiator"
SYSTEM_TYPES = [SYSTEM_UNDERFLOOR, SYSTEM_RADIATOR]

PRESET_FROST = "frost"
DEFAULT_TARGET = 21.0

COMMON_DEFAULTS = {
    CONF_INVERT_VALVE: False,
    CONF_MIN_TEMP: 5.0,
    CONF_MAX_TEMP: 28.0,
    CONF_HYSTERESIS: 0.3,
    CONF_SENSOR_TIMEOUT: 30,
    CONF_FROST_TEMP: 7.0,
    CONF_ECO_TEMP: 19.0,
    CONF_AWAY_TEMP: 16.0,
}

SYSTEM_DEFAULTS = {
    SYSTEM_UNDERFLOOR: {
        CONF_MIN_OPEN: 15,
        CONF_MIN_CLOSED: 15,
        CONF_COAST_WINDOW: 240,
        CONF_MAX_RETURN: 45.0,
    },
    SYSTEM_RADIATOR: {
        CONF_MIN_OPEN: 5,
        CONF_MIN_CLOSED: 5,
        CONF_COAST_WINDOW: 60,
        CONF_MAX_RETURN: 75.0,
    },
}

# Overshoot assumed before the zone has completed any learning cycle (°C)
INITIAL_OVERSHOOT = {
    SYSTEM_UNDERFLOOR: 0.4,
    SYSTEM_RADIATOR: 0.2,
}


def default_options(system_type: str) -> dict:
    """Return the tuning defaults for a system type."""
    return {**COMMON_DEFAULTS, **SYSTEM_DEFAULTS[system_type]}

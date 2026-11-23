"""Constants for Ford EV."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

DOMAIN = "ford_ev"
VERSION = "0.1.1"
FCQ_DOMAIN = "fordconnect_query"
FCQ_URL = "https://github.com/marq24/ha-fordconnect-query"

CONF_DEVICE_ID = "device_id"
CONF_VIN = "vin"
CONF_BATTERY_KWH = "battery_kwh"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_WEATHER_ENTITY = "weather_entity"

CONF_NOTIFY_SERVICES = "notify_services"
CONF_NOTIFY_ENTITIES = "notify_entities"
CONF_DASHBOARD_PATH = "dashboard_path"
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_FIXED = "price_fixed"
CONF_PUBLIC_PRICE_ENTITY = "public_price_entity"
CONF_PUBLIC_PRICE_FIXED = "public_price_fixed"
CONF_SOLAR_POWER_ENTITY = "solar_power_entity"
CONF_SOLAR_PRICE_ENTITY = "solar_price_entity"
CONF_SOLAR_PRICE_FIXED = "solar_price_fixed"
CONF_BASE_LOAD_W = "base_load_w"
CONF_AC_LOSS_PCT = "ac_loss_pct"
CONF_DC_LOSS_PCT = "dc_loss_pct"
CONF_ALERT_DELAY_MIN = "alert_delay_min"
CONF_REMINDER_TIME = "reminder_time"
CONF_NIGHT_START = "night_start"
CONF_NIGHT_END = "night_end"
CONF_WEEKLY_DAY = "weekly_day"
CONF_WEEKLY_TIME = "weekly_time"
CONF_STALE_UNAVAILABLE_H = "stale_unavailable_h"
CONF_STALE_NO_UPDATE_H = "stale_no_update_h"

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DEFAULT_OPTIONS: dict[str, Any] = {
    CONF_NOTIFY_SERVICES: [],
    CONF_NOTIFY_ENTITIES: [],
    CONF_DASHBOARD_PATH: "",
    CONF_PRICE_ENTITY: None,
    CONF_PRICE_FIXED: None,
    CONF_PUBLIC_PRICE_ENTITY: None,
    CONF_PUBLIC_PRICE_FIXED: None,
    CONF_SOLAR_POWER_ENTITY: None,
    CONF_SOLAR_PRICE_ENTITY: None,
    CONF_SOLAR_PRICE_FIXED: None,
    CONF_BASE_LOAD_W: 300,
    CONF_AC_LOSS_PCT: 10,
    CONF_DC_LOSS_PCT: 5,
    CONF_ALERT_DELAY_MIN: 10,
    CONF_REMINDER_TIME: "21:00:00",
    CONF_NIGHT_START: "00:00:00",
    CONF_NIGHT_END: "06:00:00",
    CONF_WEEKLY_DAY: "sun",
    CONF_WEEKLY_TIME: "20:00:00",
    CONF_STALE_UNAVAILABLE_H: 2,
    CONF_STALE_NO_UPDATE_H: 48,
}


def merged_config(entry: ConfigEntry) -> dict[str, Any]:
    """Defaults, overridden by setup data, overridden by options."""
    return {**DEFAULT_OPTIONS, **entry.data, **entry.options}

NOTIFY_KEYS = (
    "charging",
    "unlocked",
    "open",
    "plug_in_reminder",
    "warning_light",
    "data_stale",
    "night_activity",
    "weekly_summary",
    "calibration",
)
NOTIFY_DEFAULT_OFF = frozenset({"calibration"})

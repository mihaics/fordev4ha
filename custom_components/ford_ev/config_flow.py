"""Config and options flows for Ford EV."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ALERT_DELAY_MIN,
    CONF_BATTERY_KWH,
    CONF_DASHBOARD_PATH,
    CONF_DEVICE_ID,
    CONF_NIGHT_END,
    CONF_NIGHT_START,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_SERVICES,
    CONF_PRICE_ENTITY,
    CONF_PRICE_FIXED,
    CONF_REMINDER_TIME,
    CONF_STALE_NO_UPDATE_H,
    CONF_STALE_UNAVAILABLE_H,
    CONF_TEMPERATURE_SENSOR,
    CONF_VIN,
    CONF_WEATHER_ENTITY,
    CONF_WEEKLY_DAY,
    CONF_WEEKLY_TIME,
    DOMAIN,
    FCQ_URL,
    WEEKDAYS,
    merged_config,
)
from .discovery import FcqVehicle, async_discover_vehicles

DEFAULT_WEATHER = "weather.forecast_home"

_BATTERY = selector.NumberSelector(
    selector.NumberSelectorConfig(min=10, max=200, step=0.1, unit_of_measurement="kWh", mode=selector.NumberSelectorMode.BOX)
)
_TEMPERATURE = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="temperature"))
_WEATHER = selector.EntitySelector(selector.EntitySelectorConfig(domain="weather"))
_TIME = selector.TimeSelector()


def _temperature_fields(defaults: dict[str, Any]) -> dict[Any, Any]:
    return {
        vol.Required(CONF_BATTERY_KWH, default=defaults.get(CONF_BATTERY_KWH, vol.UNDEFINED)): _BATTERY,
        vol.Optional(
            CONF_TEMPERATURE_SENSOR, description={"suggested_value": defaults.get(CONF_TEMPERATURE_SENSOR)}
        ): _TEMPERATURE,
        vol.Optional(CONF_WEATHER_ENTITY, description={"suggested_value": defaults.get(CONF_WEATHER_ENTITY)}): _WEATHER,
    }


def _has_temperature_source(user_input: dict[str, Any]) -> bool:
    return bool(user_input.get(CONF_TEMPERATURE_SENSOR) or user_input.get(CONF_WEATHER_ENTITY))


class FordEvConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one FCQ vehicle."""

    VERSION = 1

    def __init__(self) -> None:
        self._vehicles: dict[str, FcqVehicle] = {}
        self._vehicle: FcqVehicle | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the vehicle."""
        vehicles = await async_discover_vehicles(self.hass)
        if not vehicles:
            return self.async_abort(reason="no_vehicles", description_placeholders={"url": FCQ_URL})
        configured = {entry.unique_id for entry in self._async_current_entries(include_ignore=False)}
        self._vehicles = {v.device_id: v for v in vehicles if v.vin not in configured}
        if not self._vehicles:
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            self._vehicle = self._vehicles[user_input[CONF_DEVICE_ID]]
            await self.async_set_unique_id(self._vehicle.vin)
            self._abort_if_unique_id_configured()
            return await self.async_step_settings()

        options = [selector.SelectOptionDict(value=v.device_id, label=v.name) for v in self._vehicles.values()]
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID, default=options[0]["value"]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Battery capacity and temperature sources."""
        assert self._vehicle is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            if _has_temperature_source(user_input):
                return self.async_create_entry(
                    title=self._vehicle.name,
                    data={
                        CONF_DEVICE_ID: self._vehicle.device_id,
                        CONF_VIN: self._vehicle.vin,
                        CONF_BATTERY_KWH: user_input[CONF_BATTERY_KWH],
                        CONF_TEMPERATURE_SENSOR: user_input.get(CONF_TEMPERATURE_SENSOR) or None,
                        CONF_WEATHER_ENTITY: user_input.get(CONF_WEATHER_ENTITY) or None,
                    },
                )
            errors["base"] = "temperature_source_required"

        defaults = dict(user_input or {})
        if not user_input and self.hass.states.get(DEFAULT_WEATHER) is not None:
            defaults[CONF_WEATHER_ENTITY] = DEFAULT_WEATHER
        return self.async_show_form(
            step_id="settings",
            data_schema=vol.Schema(_temperature_fields(defaults)),
            errors=errors,
            description_placeholders={"name": self._vehicle.name},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FordEvOptionsFlow:
        """Options flow."""
        return FordEvOptionsFlow()


class FordEvOptionsFlow(OptionsFlowWithReload):
    """Settings changed rarely: notification targets, price, alert timing, sources."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Single options form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if _has_temperature_source(user_input):
                data = dict(user_input)
                for key in (CONF_TEMPERATURE_SENSOR, CONF_WEATHER_ENTITY, CONF_PRICE_ENTITY, CONF_PRICE_FIXED):
                    data.setdefault(key, None)
                data.setdefault(CONF_DASHBOARD_PATH, "")
                return self.async_create_entry(data=data)
            errors["base"] = "temperature_source_required"

        current = {**merged_config(self.config_entry), **(user_input or {})}
        services = sorted(s for s in self.hass.services.async_services_for_domain("notify") if s != "send_message")
        schema = vol.Schema(
            {
                vol.Optional(CONF_NOTIFY_SERVICES, default=current[CONF_NOTIFY_SERVICES]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=services, multiple=True, custom_value=True)
                ),
                vol.Optional(CONF_NOTIFY_ENTITIES, default=current[CONF_NOTIFY_ENTITIES]): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="notify", multiple=True)
                ),
                vol.Optional(
                    CONF_DASHBOARD_PATH, description={"suggested_value": current[CONF_DASHBOARD_PATH]}
                ): selector.TextSelector(),
                vol.Optional(
                    CONF_PRICE_ENTITY, description={"suggested_value": current[CONF_PRICE_ENTITY]}
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain=["sensor", "input_number"])),
                vol.Optional(
                    CONF_PRICE_FIXED, description={"suggested_value": current[CONF_PRICE_FIXED]}
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=0, max=100, step="any", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_ALERT_DELAY_MIN, default=current[CONF_ALERT_DELAY_MIN]): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=120, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_REMINDER_TIME, default=current[CONF_REMINDER_TIME]): _TIME,
                vol.Required(CONF_NIGHT_START, default=current[CONF_NIGHT_START]): _TIME,
                vol.Required(CONF_NIGHT_END, default=current[CONF_NIGHT_END]): _TIME,
                vol.Required(CONF_WEEKLY_DAY, default=current[CONF_WEEKLY_DAY]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(WEEKDAYS), translation_key="weekday")
                ),
                vol.Required(CONF_WEEKLY_TIME, default=current[CONF_WEEKLY_TIME]): _TIME,
                vol.Required(CONF_STALE_UNAVAILABLE_H, default=current[CONF_STALE_UNAVAILABLE_H]): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=48, step=1, unit_of_measurement="h", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_STALE_NO_UPDATE_H, default=current[CONF_STALE_NO_UPDATE_H]): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=6, max=336, step=1, unit_of_measurement="h", mode=selector.NumberSelectorMode.BOX)
                ),
                **_temperature_fields(current),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

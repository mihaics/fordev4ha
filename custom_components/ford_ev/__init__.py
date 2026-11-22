"""Ford EV for Home Assistant."""

from __future__ import annotations

from pathlib import Path

import voluptuous as vol
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .alerts import AlertManager
from .const import CONF_DEVICE_ID, CONF_VIN, DOMAIN, VERSION, merged_config
from .discovery import async_discover_vehicles, async_get_vehicle
from .engine import FordEvConfigEntry, FordEvEngine, FordEvRuntime

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_RESET_RANGE_MODEL = "reset_range_model"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
STRATEGY_URL = f"/{DOMAIN}/ford-ev-strategy.js"
STRATEGY_FILE = Path(__file__).parent / "frontend" / "ford-ev-strategy.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard strategy and register the integration-wide service."""
    await hass.http.async_register_static_paths([StaticPathConfig(STRATEGY_URL, str(STRATEGY_FILE), False)])
    if "frontend" in hass.config.components:
        add_extra_js_url(hass, f"{STRATEGY_URL}?v={VERSION}")

    async def reset_range_model(call: ServiceCall) -> None:
        loaded = [e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]
        entry_id = call.data.get(ATTR_CONFIG_ENTRY_ID)
        if entry_id:
            entry = next((e for e in loaded if e.entry_id == entry_id), None)
            if entry is None:
                raise ServiceValidationError(f"No loaded Ford EV entry {entry_id}")
        elif len(loaded) == 1:
            entry = loaded[0]
        else:
            raise ServiceValidationError("Several vehicles are set up: choose one with config_entry_id")
        entry.runtime_data.engine.reset_range_model()

    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_RANGE_MODEL,
        reset_range_model,
        schema=vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FordEvConfigEntry) -> bool:
    """Set up one vehicle.

    Order matters: state is loaded before entities are added (so totals never flash to zero),
    and entities and the notifier subscribe before the engine starts (so events raised while
    reconciling a restart are not lost).
    """
    vehicle = await async_get_vehicle(hass, entry.data[CONF_DEVICE_ID])
    if vehicle is None:
        vehicle = next((v for v in await async_discover_vehicles(hass) if v.vin == entry.data[CONF_VIN]), None)
    if vehicle is None:
        raise ConfigEntryNotReady("FordConnect Query vehicle not found")

    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, vehicle.vin)},
        name=f"Ford EV {vehicle.name}",
        model="Ford EV",
        via_device_id=vehicle.device_id,
    )
    engine = FordEvEngine(hass, entry, vehicle)
    await engine.async_load()
    alerts = AlertManager(hass, engine, merged_config(entry))
    entry.runtime_data = FordEvRuntime(engine=engine, alerts=alerts)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await alerts.async_start()
    await engine.async_start()
    alerts.async_evaluate()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FordEvConfigEntry) -> bool:
    """Unload one vehicle."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.alerts.async_stop()
        await entry.runtime_data.engine.async_stop()
    return unloaded

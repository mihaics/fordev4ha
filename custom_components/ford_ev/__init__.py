"""Ford EV for Home Assistant."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .alerts import AlertManager
from .const import CONF_DEVICE_ID, CONF_VIN, merged_config
from .discovery import async_discover_vehicles, async_get_vehicle
from .engine import FordEvConfigEntry, FordEvEngine, FordEvRuntime

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]


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

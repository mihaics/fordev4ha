"""Diagnostics download, with the VIN and notification targets removed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import merged_config
from .engine import FordEvConfigEntry

TO_REDACT = {"vin", "notify_services", "notify_entities", "dashboard_path", "device_id"}
REDACTED = "**REDACTED**"


def _scrub(value: Any, vin: str) -> Any:
    """Replace every string that contains the VIN (entity IDs contain it)."""
    if isinstance(value, dict):
        return {k: _scrub(v, vin) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, vin) for v in value]
    if isinstance(value, str) and vin.lower() in value.lower():
        return REDACTED
    return value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: FordEvConfigEntry) -> dict[str, Any]:
    """Configuration, which FCQ keys were found, and the engine state."""
    engine = entry.runtime_data.engine
    data = {
        "config": merged_config(entry),
        "vehicle_keys": sorted(engine.vehicle.entities),
        "state": engine.state.to_dict(),
    }
    return _scrub(async_redact_data(data, TO_REDACT), engine.vehicle.vin)

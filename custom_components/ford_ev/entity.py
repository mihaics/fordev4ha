"""Base entity for Ford EV."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .engine import FordEvEngine


class FordEvEntity(Entity):
    """An entity of one vehicle, refreshed whenever the engine signals a change."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, engine: FordEvEngine, key: str) -> None:
        self.engine = engine
        vehicle = engine.vehicle
        self._attr_translation_key = key
        self._attr_unique_id = f"{vehicle.vin}_{key}"
        # The device itself is created in async_setup_entry, linked to the FCQ device.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vehicle.vin)})

    async def async_added_to_hass(self) -> None:
        """Follow engine updates."""
        self.async_on_remove(self.engine.async_add_listener(self._handle_engine_update))

    @callback
    def _handle_engine_update(self) -> None:
        self.async_write_ha_state()

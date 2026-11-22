"""Event entities: alerts, charging and reports, for users' own automations."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .alerts import EVENT_TYPES
from .engine import FordEvConfigEntry, FordEvEngine, signal_event
from .entity import FordEvEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: FordEvConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the event entities."""
    engine = entry.runtime_data.engine
    async_add_entities(FordEvEvent(engine, key) for key in EVENT_TYPES)


class FordEvEvent(FordEvEntity, EventEntity):
    """Fires for every event of one group, with the event data as attributes."""

    def __init__(self, engine: FordEvEngine, key: str) -> None:
        super().__init__(engine, key)
        self._key = key
        self._attr_event_types = EVENT_TYPES[key]

    async def async_added_to_hass(self) -> None:
        """Follow engine events (not general updates)."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_event(self.engine.entry.entry_id), self._on_event)
        )

    @callback
    def _on_event(self, entity_key: str, event_type: str, data: dict[str, Any]) -> None:
        if entity_key != self._key:
            return
        self._trigger_event(event_type, data)
        self.async_write_ha_state()

"""Binary sensor: charging session active."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .engine import FordEvConfigEntry
from .entity import FordEvEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: FordEvConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the binary sensor."""
    async_add_entities([ChargingSessionSensor(entry.runtime_data.engine, "charging_session")])


class ChargingSessionSensor(FordEvEntity, BinarySensorEntity):
    """On while a charging session is open."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    @property
    def is_on(self) -> bool:
        """Session open."""
        return self.engine.state.session is not None

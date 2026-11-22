"""Switch entities: climate while driving, and one switch per notification type."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import NOTIFY_KEYS
from .engine import FordEvConfigEntry, FordEvEngine
from .entity import FordEvEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: FordEvConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the switches."""
    engine = entry.runtime_data.engine
    switches = [FordEvSwitch(engine, "climate_on", "climate_on")]
    switches += [FordEvSwitch(engine, f"notify_{key}", f"notify.{key}", EntityCategory.CONFIG) for key in NOTIFY_KEYS]
    async_add_entities(switches)


class FordEvSwitch(FordEvEntity, SwitchEntity):
    """A boolean setting stored by the engine."""

    def __init__(self, engine: FordEvEngine, key: str, setting: str, category: EntityCategory | None = None) -> None:
        super().__init__(engine, key)
        self._setting = setting
        self._attr_entity_category = category

    @property
    def is_on(self) -> bool:
        """Current setting."""
        settings = self.engine.state.settings
        if self._setting.startswith("notify."):
            return settings.notify.get(self._setting.removeprefix("notify."), False)
        return bool(getattr(settings, self._setting))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        self.engine.set_setting(self._setting, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        self.engine.set_setting(self._setting, False)

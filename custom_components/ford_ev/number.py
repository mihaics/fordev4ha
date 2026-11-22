"""Number entities: settings changed day to day."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .engine import FordEvConfigEntry, FordEvEngine
from .entity import FordEvEntity


@dataclass(frozen=True, kw_only=True)
class FordEvNumberDescription(NumberEntityDescription):
    """A number backed by an engine setting."""

    setting: str


NUMBERS = (
    FordEvNumberDescription(
        key="charge_target", setting="charge_target", native_min_value=50, native_max_value=100,
        native_step=5, native_unit_of_measurement=PERCENTAGE, mode=NumberMode.SLIDER,
    ),
    FordEvNumberDescription(
        key="plug_in_reminder_soc", setting="plug_in_reminder_soc", native_min_value=5, native_max_value=95,
        native_step=5, native_unit_of_measurement=PERCENTAGE, mode=NumberMode.SLIDER,
    ),
    FordEvNumberDescription(
        key="climate_setpoint", setting="climate_setpoint", native_min_value=16, native_max_value=28,
        native_step=0.5, native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=NumberDeviceClass.TEMPERATURE, mode=NumberMode.SLIDER,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: FordEvConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the number entities."""
    engine = entry.runtime_data.engine
    async_add_entities(FordEvNumber(engine, description) for description in NUMBERS)


class FordEvNumber(FordEvEntity, NumberEntity):
    """A setting stored by the engine."""

    entity_description: FordEvNumberDescription

    def __init__(self, engine: FordEvEngine, description: FordEvNumberDescription) -> None:
        super().__init__(engine, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        """Current setting."""
        return float(getattr(self.engine.state.settings, self.entity_description.setting))

    async def async_set_native_value(self, value: float) -> None:
        """Store a new value."""
        self.engine.set_setting(self.entity_description.setting, value)

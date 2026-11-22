"""Read Home Assistant states as numbers in the integration's internal units (km, °C)."""

from __future__ import annotations

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfLength,
    UnitOfTemperature,
)
from homeassistant.core import State
from homeassistant.util.unit_conversion import DistanceConverter, TemperatureConverter

_INVALID = (STATE_UNKNOWN, STATE_UNAVAILABLE, "", None)


def _number(value: object) -> float | None:
    if value in _INVALID:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def state_number(state: State | None) -> float | None:
    """Return the numeric state, or None for missing, unknown, unavailable or non-numeric states."""
    if state is None:
        return None
    return _number(state.state)


def to_km(value: float, unit: str | None) -> float:
    """Convert a distance to km. A missing unit means km."""
    if unit is None or unit == UnitOfLength.KILOMETERS:
        return value
    return DistanceConverter.convert(value, unit, UnitOfLength.KILOMETERS)


def to_celsius(value: float, unit: str | None) -> float:
    """Convert a temperature to °C. A missing unit means °C."""
    if unit is None or unit == UnitOfTemperature.CELSIUS:
        return value
    return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)


def state_km(state: State | None) -> float | None:
    """Return a distance state in km."""
    value = state_number(state)
    if value is None:
        return None
    return to_km(value, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))


def state_celsius(state: State | None) -> float | None:
    """Return a temperature state in °C."""
    value = state_number(state)
    if value is None:
        return None
    return to_celsius(value, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))


def weather_celsius(state: State | None) -> float | None:
    """Return the current temperature of a weather entity in °C."""
    if state is None:
        return None
    value = _number(state.attributes.get("temperature"))
    if value is None:
        return None
    return to_celsius(value, state.attributes.get("temperature_unit"))

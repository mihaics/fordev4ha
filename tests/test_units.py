"""Tests for unit conversion helpers."""

import pytest
from homeassistant.core import State

from custom_components.ford_ev.units import (
    state_celsius,
    state_km,
    state_number,
    to_celsius,
    to_km,
    weather_celsius,
)


def test_state_km_converts_miles():
    assert state_km(State("sensor.o", "100", {"unit_of_measurement": "mi"})) == pytest.approx(160.934, abs=1e-3)


def test_state_km_keeps_km():
    assert state_km(State("sensor.o", "326.0", {"unit_of_measurement": "km"})) == 326.0


@pytest.mark.parametrize("raw", ["unknown", "unavailable", "Unsupported", ""])
def test_non_numeric_is_none(raw):
    assert state_number(State("sensor.x", raw)) is None


def test_missing_state_is_none():
    assert state_km(None) is None


def test_state_celsius_from_fahrenheit():
    assert state_celsius(State("sensor.t", "68", {"unit_of_measurement": "°F"})) == pytest.approx(20.0)


def test_weather_celsius_uses_temperature_unit():
    assert weather_celsius(State("weather.h", "sunny", {"temperature": 50, "temperature_unit": "°F"})) == pytest.approx(10.0)


def test_weather_without_temperature_is_none():
    assert weather_celsius(State("weather.h", "sunny", {})) is None


def test_to_km_none_unit_is_km():
    assert to_km(12.5, None) == 12.5


def test_to_celsius_none_unit_is_celsius():
    assert to_celsius(-3.0, None) == -3.0

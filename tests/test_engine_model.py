"""Tests for the range model wiring: temperature, forecast, trips, calibration, seeding."""

from datetime import timedelta

import pytest
from homeassistant.core import SupportsResponse, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ford_ev.engine import signal_event
from custom_components.ford_ev.range_model import RangeModelState
from custom_components.ford_ev.state import EngineState

from .conftest import async_setup_ford_ev


def engine_of(entry):
    return entry.runtime_data.engine


async def advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def capture(hass, entry_id):
    got = []

    @callback
    def _on(key, event_type, data):
        got.append((key, event_type, data))

    async_dispatcher_connect(hass, signal_event(entry_id), _on)
    return got


async def test_first_start_seeds_model(hass, fcq):
    await fcq.set("soc", "80")
    await fcq.set("elveh", "240")
    entry = await async_setup_ford_ev(hass, fcq)
    model = engine_of(entry).state.model
    assert model.a == pytest.approx(1.25)
    assert model.a0 == pytest.approx(1.25)
    assert model.model_energy == pytest.approx(48.0)
    assert model.odo_accounted == 1000


async def _drive(hass, freezer, fcq, minutes, end_odometer, unit_attrs=None):
    await fcq.set("ignitionstatus", "ON")
    await advance(hass, freezer, minutes * 60)
    await fcq.set("odometer", end_odometer, unit_attrs)
    await fcq.set("ignitionstatus", "OFF")


async def test_trip_accounted_two_minutes_after_off(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await _drive(hass, freezer, fcq, 30, "1030")
    await advance(hass, freezer, 119)
    assert engine_of(entry).state.model.cycle_km == 0
    await advance(hass, freezer, 2)
    model = engine_of(entry).state.model
    assert model.cycle_km == pytest.approx(30)
    assert model.avg_speed == pytest.approx(48.0)
    assert engine_of(entry).state.pending_trip_end_ts is None


async def test_trip_with_miles_odometer(hass, fcq, freezer):
    await fcq.set("odometer", "621.371", {"unit_of_measurement": "mi"})
    entry = await async_setup_ford_ev(hass, fcq)
    await _drive(hass, freezer, fcq, 30, "640.0", {"unit_of_measurement": "mi"})
    await advance(hass, freezer, 121)
    assert engine_of(entry).state.model.cycle_km == pytest.approx(30.0, abs=0.1)


async def test_trip_after_unavailable_ignition(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("ignitionstatus", "ON")
    await advance(hass, freezer, 1800)
    await fcq.set("ignitionstatus", "unavailable")
    await fcq.set("odometer", "1030")
    await fcq.set("ignitionstatus", "OFF")
    await advance(hass, freezer, 121)
    assert engine_of(entry).state.model.cycle_km == pytest.approx(30)


async def test_trip_uses_climate_setting(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    engine_of(entry).set_setting("climate_on", False)
    await _drive(hass, freezer, fcq, 30, "1030")
    await advance(hass, freezer, 121)
    assert engine_of(entry).state.model.cycle_x_hvac == 0


async def test_restart_with_pending_trip(hass, fcq, freezer, hass_storage):
    now = dt_util.utcnow().timestamp()
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.model = RangeModelState(model_energy=30.0, odo_accounted=1000.0, trip_start_ts=now - 1860, trip_start_temp=20.0)
    st.pending_trip_end_ts = now - 60
    key = "ford_ev.entry_t"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": st.to_dict()}
    await fcq.set("odometer", "1030")
    entry = await async_setup_ford_ev(hass, fcq, entry_id="entry_t")
    assert engine_of(entry).state.model.cycle_km == pytest.approx(30)
    await advance(hass, freezer, 121)
    assert engine_of(entry).state.model.cycle_km == pytest.approx(30)
    assert engine_of(entry).state.pending_trip_end_ts is None


async def test_temperature_fallback_to_weather(hass, fcq):
    hass.states.async_set("weather.home", "sunny", {"temperature": 50, "temperature_unit": "°F"})
    entry = await async_setup_ford_ev(hass, fcq, data={"temperature_sensor": None, "weather_entity": "weather.home"})
    assert engine_of(entry).current_temp_c() == pytest.approx(10.0)


async def test_temperature_sensor_preferred(hass, fcq):
    hass.states.async_set("weather.home", "sunny", {"temperature": 50, "temperature_unit": "°F"})
    entry = await async_setup_ford_ev(hass, fcq, data={"weather_entity": "weather.home"})
    assert engine_of(entry).current_temp_c() == pytest.approx(20.0)


async def test_forecast_tomorrow(hass, fcq):
    hass.states.async_set("weather.home", "sunny", {"temperature": 5, "temperature_unit": "°C"})
    tomorrow = (dt_util.now() + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    today = dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0)

    async def get_forecasts(call):
        return {
            "weather.home": {
                "forecast": [
                    {"datetime": today.isoformat(), "temperature": 30, "templow": 20},
                    {"datetime": tomorrow.isoformat(), "temperature": 10, "templow": 2},
                ]
            }
        }

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    entry = await async_setup_ford_ev(hass, fcq, data={"weather_entity": "weather.home"})
    assert engine_of(entry).forecast_tomorrow_c == pytest.approx(6.0)


async def test_forecast_none_without_weather(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    assert engine_of(entry).forecast_tomorrow_c is None


async def test_calibration_after_five_minutes_at_full(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await fcq.set("evccstatus", "C")
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": 0.5})
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": 30.5})
    await fcq.set("soc", "100")
    await advance(hass, freezer, 299)
    assert not [e for e in got if e[1] == "calibration"]
    await advance(hass, freezer, 2)
    [cal] = [e for e in got if e[1] == "calibration"]
    assert cal[0] == "report"
    assert cal[2]["charged_kwh"] == pytest.approx(30.0)
    assert cal[2]["soc_start_car"] == 50
    assert cal[2]["capacity_estimate_kwh"] == pytest.approx(60.0)
    assert "summary" in cal[2]
    model = engine_of(entry).state.model
    assert model.model_energy == 60
    assert model.cycle_charged == 0
    assert model.last_calibration is not None


async def test_no_calibration_if_soc_drops(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await fcq.set("evccstatus", "C")
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": 0.5})
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": 5.5})
    await fcq.set("soc", "100")
    await advance(hass, freezer, 120)
    await fcq.set("soc", "99")
    await advance(hass, freezer, 300)
    assert not [e for e in got if e[1] == "calibration"]


async def test_model_energy_set_at_first_soc(hass, fcq):
    await fcq.set("soc", "unavailable")
    entry = await async_setup_ford_ev(hass, fcq)
    assert engine_of(entry).state.model.model_energy is None
    await fcq.set("soc", "50")
    assert engine_of(entry).state.model.model_energy == pytest.approx(30.0)


async def test_reset_range_model(hass, fcq, freezer):
    await fcq.set("soc", "80")
    await fcq.set("elveh", "240")
    entry = await async_setup_ford_ev(hass, fcq)
    eng = engine_of(entry)
    eng.state.model.a = 1.7
    eng.state.model.calibrations = 3
    eng.state.model.avg_speed = 55.0
    eng.reset_range_model()
    assert eng.state.model.a == pytest.approx(1.25)
    assert eng.state.model.calibrations == 0
    assert eng.state.model.avg_speed == 55.0
    assert eng.state.model.model_energy == pytest.approx(48.0)

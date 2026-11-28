"""Tests for prices without a value, session location, solar over a step, charging efficiency,
the model battery's size, the weekly summary's span, days of range and driving-only figures."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.ford_ev.const import DOMAIN
from custom_components.ford_ev.range_model import (
    RangeModelState,
    calibrate,
    capacity_estimate,
    first_cycle_deficit,
    predicted_kwh_per_km,
)
from custom_components.ford_ev.state import EngineState
from custom_components.ford_ev.trips import speed_key, speed_rows, trip_statistics

from .conftest import async_setup_ford_ev
from .test_costs_trips import AC, charge, drive, state
from .test_engine import advance, capture, engine_of

COMPONENT = Path(__file__).parent.parent / "custom_components" / "ford_ev"


def _store(hass_storage, entry_id, st):
    key = f"{DOMAIN}.{entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": st.to_dict()}


# ---------------------------------------------------------------------- prices


async def test_price_entity_without_a_value_uses_its_last_value(hass, fcq):
    hass.states.async_set("input_number.price", "0.5")
    entry = await async_setup_ford_ev(hass, fcq, options={"price_entity": "input_number.price", "ac_loss_pct": 0})
    await charge(fcq, (0.5,))
    hass.states.async_set("input_number.price", "unavailable")
    await hass.async_block_till_done()
    await charge(fcq, (2.5,))
    st = engine_of(entry).state
    assert st.cost == pytest.approx(2.0 * 0.5)
    assert st.last_prices == {"input_number.price": 0.5}


async def test_last_price_survives_a_restart(hass, fcq, hass_storage):
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.battery_value = 15.0
    st.charger_last_reading = 0.5
    st.last_prices = {"input_number.price": 0.4}
    _store(hass_storage, "p", st)
    hass.states.async_set("input_number.price", "unavailable")
    entry = await async_setup_ford_ev(
        hass, fcq, options={"price_entity": "input_number.price", "ac_loss_pct": 0}, entry_id="p"
    )
    await charge(fcq, (2.5,))
    assert engine_of(entry).state.cost == pytest.approx(2.0 * 0.4)


async def test_price_of_a_replaced_entity_is_not_reused(hass, fcq, hass_storage):
    st = EngineState.new()
    st.last_prices = {"input_number.old": 9.0}
    _store(hass_storage, "p", st)
    entry = await async_setup_ford_ev(hass, fcq, options={"price_entity": "input_number.new"}, entry_id="p")
    assert engine_of(entry).price() is None


# ---------------------------------------------------------------------- location


async def test_session_is_billed_where_it_started(hass, fcq):
    entry = await async_setup_ford_ev(
        hass, fcq, options={"price_fixed": 1.0, "public_price_fixed": 3.0, "ac_loss_pct": 0}
    )
    await fcq.set("evccstatus", "C")
    await charge(fcq, (0.5, 1.5))
    await fcq.set("tracker", "not_home")
    await charge(fcq, (2.5,))
    st = engine_of(entry).state
    assert st.cost_home == pytest.approx(2.0)
    assert st.cost_public == 0


@pytest.mark.parametrize("before", ["unavailable", "not_home"])
async def test_position_reported_after_the_plug_still_places_the_session(hass, fcq, before):
    await fcq.set("tracker", before)
    entry = await async_setup_ford_ev(
        hass, fcq, options={"price_fixed": 1.0, "public_price_fixed": 3.0, "ac_loss_pct": 0}
    )
    await fcq.set("evccstatus", "C")
    session = engine_of(entry).state.session
    assert (session.home, session.home_known) == (False, False)
    await fcq.set("tracker", "home")
    assert (session.home, session.home_known) == (True, False)
    await charge(fcq, (0.5, 2.5))
    assert session.home_known
    st = engine_of(entry).state
    assert st.cost_home == pytest.approx(2.0)
    assert st.cost_public == 0


# ---------------------------------------------------------------------- solar


async def test_solar_is_added_up_over_the_step(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "2300", {"unit_of_measurement": "W"})
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.5, "solar_power_entity": "sensor.pv"})
    await charge(fcq, (0.5,))
    freezer.tick(timedelta(minutes=15))
    hass.states.async_set("sensor.pv", "300", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=15))
    await charge(fcq, (2.5,))
    # 2.0 kW above the base load for the first 15 min, nothing after.
    assert engine_of(entry).state.charger_energy_solar == pytest.approx(0.5)


async def test_solar_sensor_without_a_value_counts_as_none(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "2300", {"unit_of_measurement": "W"})
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.5, "solar_power_entity": "sensor.pv"})
    await charge(fcq, (0.5,))
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("sensor.pv", "unavailable")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=30))
    await charge(fcq, (2.5,))
    assert engine_of(entry).state.charger_energy_solar == pytest.approx(1.0)


# ---------------------------------------------------------------------- forecast


async def test_forecast_is_read_when_the_weather_entity_appears(hass, fcq):
    tomorrow = (dt_util.now() + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)

    async def get_forecasts(call):
        if hass.states.get("weather.home") is None:
            raise ValueError("not loaded yet")
        return {"weather.home": {"forecast": [{"datetime": tomorrow.isoformat(), "temperature": 10, "templow": 2}]}}

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)
    entry = await async_setup_ford_ev(hass, fcq, data={"weather_entity": "weather.home"})
    assert engine_of(entry).forecast_tomorrow_c is None
    hass.states.async_set("weather.home", "sunny", {"temperature": 5, "temperature_unit": "°C"})
    await hass.async_block_till_done()
    assert engine_of(entry).forecast_tomorrow_c == pytest.approx(6.0)


# ---------------------------------------------------------------------- charging efficiency


async def _session(hass, freezer, fcq, readings, end_soc):
    await fcq.set("evccstatus", "C")
    await charge(fcq, readings)
    await fcq.set("soc", end_soc)
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)


async def test_session_efficiency(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await _session(hass, freezer, fcq, (0.5, 20.5), "80")
    data = [g for g in got if g[0] == "charging"][-1][2]
    # 30 points of 60 kWh reached the battery, of 20 kWh at the charger.
    assert data["battery_kwh"] == pytest.approx(18.0)
    assert data["efficiency_pct"] == pytest.approx(90.0)
    sensor = state(hass, "charging_efficiency")
    assert float(sensor.state) == pytest.approx(90.0)
    assert sensor.attributes["sessions"] == 1
    assert sensor.attributes["last"] == 90.0


async def test_short_session_has_no_efficiency_and_keeps_the_last_one(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await _session(hass, freezer, fcq, (0.5, 20.5), "80")
    await _session(hass, freezer, fcq, (0.5, 6.5), "89")
    st = engine_of(entry).state
    assert st.last_session["efficiency_pct"] is None
    assert st.eff_sessions == 1
    assert state(hass, "charging_efficiency").attributes["last"] == 90.0


async def test_state_of_charge_jump_is_not_an_efficiency(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    # 10 kWh at the charger and a rise worth 24 kWh: the state of charge was corrected.
    await _session(hass, freezer, fcq, (0.5, 10.5), "90")
    st = engine_of(entry).state
    assert st.last_session["efficiency_pct"] is None
    assert st.eff_sessions == 0
    assert state(hass, "charging_efficiency").state == "unknown"


async def test_session_counted_from_the_state_of_charge_has_no_efficiency(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("evccstatus", "C")
    await fcq.set("soc", "80")
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    st = engine_of(entry).state
    assert st.last_session["estimated_kwh"] == pytest.approx(18.0)
    assert st.last_session["efficiency_pct"] is None
    assert st.eff_sessions == 0


# ---------------------------------------------------------------------- model battery size


def test_capacity_estimate_needs_a_session_from_below_80():
    assert capacity_estimate(31.5, 50) == pytest.approx(63.0)
    assert capacity_estimate(10.0, 85) is None
    assert capacity_estimate(None, 50) is None
    assert capacity_estimate(0.0, 50) is None


def test_upgrade_keeps_the_capacity_estimate_as_the_first_sample():
    data = EngineState.new().to_dict()
    del data["capacity_estimates"]
    data["capacity_estimate"] = 61.0
    assert EngineState.from_dict(data).capacity_estimates == [61.0]


async def test_capacity_estimate_is_the_median_and_does_not_resize_the_model(hass, fcq, freezer, hass_storage):
    st = EngineState.new()
    st.capacity_estimate = 66.0
    st.capacity_estimates = [61.0, 66.0]
    _store(hass_storage, "m", st)
    entry = await async_setup_ford_ev(hass, fcq, entry_id="m")
    engine = engine_of(entry)
    await fcq.set("evccstatus", "C")
    await charge(fcq, (0.5, 32.0))
    await fcq.set("soc", "100")
    await advance(hass, freezer, 301)
    assert engine.state.capacity_estimates == [61.0, 66.0, pytest.approx(63.0)]
    capacity = state(hass, "capacity_estimate")
    assert float(capacity.state) == pytest.approx(63.0)
    assert capacity.attributes["samples"] == 3
    assert capacity.attributes["last"] == 63.0
    # The model battery keeps the configured capacity.
    assert engine.state.model.model_energy == pytest.approx(60.0)
    assert float(state(hass, "model_soc").state) == pytest.approx(100.0)


async def test_no_capacity_estimate_from_a_session_counted_from_the_state_of_charge(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await fcq.set("evccstatus", "C")
    await fcq.set("soc", "100")
    await advance(hass, freezer, 301)
    [cal] = [e for e in got if e[1] == "calibration"]
    assert cal[2]["capacity_estimate_kwh"] is None
    assert engine_of(entry).state.capacity_estimates == []


async def test_no_capacity_estimate_when_such_a_session_already_ended(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("evccstatus", "C")
    await fcq.set("soc", "100")
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 301)
    engine = engine_of(entry)
    assert engine.state.model.last_calibration is not None
    assert engine.state.capacity_estimates == []


# ---------------------------------------------------------------------- first cycle


def test_first_cycle_does_not_count_the_energy_that_filled_the_battery():
    # Set up at 40 % of 60 kWh: 36 kWh of the 86 charged only refill the battery.
    st = RangeModelState(cycle_x_drive=40, cycle_x_hvac=10, cycle_charged=86, cycle_km=300, cycle_deficit=36)
    st2, result = calibrate(st, 60.0, 1000.0, None, None, None)
    assert result.valid
    assert result.used_kwh == pytest.approx(50.0)
    assert result.charged_kwh == pytest.approx(86.0)
    assert result.ratio == pytest.approx(1.0)
    assert st2.cycle_deficit == 0.0


def test_deficit_of_a_cycle_stored_before_it_was_recorded():
    # Began with 24 kWh in a 60 kWh battery; 30 charged and 20 taken out by the model since.
    st = RangeModelState(a=0.8, cycle_x_drive=20, cycle_x_hvac=4, cycle_charged=30, model_energy=34.0)
    assert first_cycle_deficit(st, 60.0) == pytest.approx(36.0)
    assert first_cycle_deficit(RangeModelState(model_energy=34.0, last_calibration=5.0), 60.0) == 0.0


async def test_first_calibration_after_setup_uses_only_the_energy_used(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    engine = engine_of(entry)
    assert engine.state.model.cycle_deficit == pytest.approx(30.0)
    got = capture(hass, entry.entry_id)
    await drive(hass, freezer, fcq, 120, "1200", soc="20")
    await fcq.set("evccstatus", "C")
    await charge(fcq, (0.5, 48.5))
    await fcq.set("soc", "100")
    await advance(hass, freezer, 301)
    [cal] = [e for e in got if e[1] == "calibration"]
    # 48 kWh charged, 30 of them to fill the battery from the 50 % it had at setup.
    assert cal[2]["charged_kwh"] == pytest.approx(48.0)
    assert cal[2]["used_kwh"] == pytest.approx(18.0)
    assert "18.0 kWh used" in cal[2]["summary"]
    assert engine.state.model.cycle_deficit == 0.0


async def test_upgrade_finds_the_deficit_of_the_running_first_cycle(hass, fcq, hass_storage):
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.model = RangeModelState(a=0.8, cycle_x_drive=20, cycle_x_hvac=4, cycle_charged=30, model_energy=34.0)
    data = st.to_dict()
    del data["model"]["cycle_deficit"]
    key = f"{DOMAIN}.u"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}
    entry = await async_setup_ford_ev(hass, fcq, entry_id="u")
    assert engine_of(entry).state.model.cycle_deficit == pytest.approx(36.0)


# ---------------------------------------------------------------------- weekly summary


async def test_weekly_summary_covers_the_time_since_the_last_one(hass, fcq, freezer):
    from .test_alerts import OPTS, PHONE, at_local  # noqa: PLC0415

    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(19, 59, 50, weekday=6))
    entry = await async_setup_ford_ev(hass, fcq, options={**OPTS, "price_fixed": 1.0, "ac_loss_pct": 0})
    await fcq.set("odometer", "1100")
    await advance(hass, freezer, 15)
    assert calls[0].data["message"].startswith("100 km")
    assert engine_of(entry).state.summary_totals["odometer"] == 1100
    # 50 km on Sunday evening belong to the old calendar week, 50 km to the new one.
    await fcq.set("odometer", "1150")
    await advance(hass, freezer, 6 * 86400)
    await fcq.set("odometer", "1200")
    await advance(hass, freezer, 86400)
    summaries = [c.data["message"] for c in calls if c.data["title"] == "Weekly summary"]
    assert summaries[1].startswith("100 km")


async def test_past_charge_before_the_last_summary_is_not_in_the_next_one(hass, fcq, freezer):
    from .test_alerts import OPTS, PHONE, at_local  # noqa: PLC0415

    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(19, 59, 50, weekday=6))
    entry = await async_setup_ford_ev(hass, fcq, options={**OPTS, "price_fixed": 1.0, "ac_loss_pct": 0})
    await advance(hass, freezer, 15)
    engine_of(entry).add_past_charge(dt_util.now() - timedelta(days=2), 10.0, home=True, dc=False)
    await advance(hass, freezer, 7 * 86400)
    summaries = [c.data["message"] for c in calls if c.data["title"] == "Weekly summary"]
    assert "Charged 0.0 kWh" in summaries[1]


# ---------------------------------------------------------------------- days of range, driving only


async def test_days_of_range(hass, fcq, freezer):
    freezer.move_to(dt_util.now().replace(day=10, hour=12, minute=0, second=0, microsecond=0))
    await async_setup_ford_ev(hass, fcq)
    assert state(hass, "days_of_range").state == "unknown"
    await advance(hass, freezer, 4 * 86400)
    await fcq.set("odometer", "1225")
    sensor = state(hass, "days_of_range")
    # 225 km in the 4.5 days since the start of the day tracking began.
    assert sensor.attributes["daily_km"] == pytest.approx(50.0)
    assert float(sensor.state) == pytest.approx(float(state(hass, "model_range").state) / 50.0)
    assert sensor.attributes["days_car"] == pytest.approx(4.0)


async def test_days_of_range_empty_without_driving(hass, fcq, freezer):
    freezer.move_to(dt_util.now().replace(day=10, hour=12, minute=0, second=0, microsecond=0))
    await async_setup_ford_ev(hass, fcq)
    await advance(hass, freezer, 4 * 86400)
    await fcq.set("soc", "49")
    sensor = state(hass, "days_of_range")
    assert sensor.state == "unknown"
    assert sensor.attributes["days_car"] is None


async def test_consumption_and_cost_without_parked_energy(hass, fcq, freezer):
    freezer.move_to(dt_util.now().replace(day=10, hour=12, minute=0, second=0, microsecond=0))
    await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    await fcq.set("soc", "45")  # parked: 3 kWh
    await drive(hass, freezer, fcq, 60, "1100", soc="30")  # 100 km, 9 kWh
    consumption = state(hass, "consumption_month")
    assert float(consumption.state) == pytest.approx(12.0)
    assert consumption.attributes["driving"] == 9.0
    assert consumption.attributes["parked_kwh"] == 3.0
    cost = state(hass, "cost_per_100km_month")
    assert float(cost.state) == pytest.approx(12.0)
    assert cost.attributes["driving_only"] == 9.0
    assert cost.attributes["parked_cost"] == 3.0


async def test_driving_only_cost_is_empty_before_cost_tracking_starts(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    assert state(hass, "cost_per_100km_month").attributes["driving_only"] is None


# ---------------------------------------------------------------------- cost to charge


async def test_cost_to_charge_away_on_ac_uses_the_ac_losses(hass, fcq):
    await fcq.set("tracker", "not_home")
    await async_setup_ford_ev(hass, fcq, options={"public_price_fixed": 2.0})
    # 50 % of 60 kWh; DC losses (5 %) while nothing says otherwise.
    assert float(state(hass, "charge_to_target_cost").state) == pytest.approx(30 * 1.05 * 2.0)
    await fcq.set("evccstatus", "B")
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": 0.0, **AC})
    assert float(state(hass, "charge_to_target_cost").state) == pytest.approx(30 * 1.10 * 2.0)


# ---------------------------------------------------------------------- packaging


def test_strings_and_english_translation_are_the_same():
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    assert strings == english


def test_every_sensor_has_a_name():
    from custom_components.ford_ev.sensor import SENSORS  # noqa: PLC0415

    names = json.loads((COMPONENT / "strings.json").read_text())["entity"]["sensor"]
    assert {d.key for d in SENSORS} <= set(names)


# ---------------------------------------------------------------------- start while charging


async def test_started_while_charging_opens_a_session(hass, fcq, freezer):
    await fcq.set("evccstatus", "C")
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": 3.0, **AC})
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    engine = engine_of(entry)
    assert engine.state.session is not None
    assert engine.state.session.start_soc == 50
    got = capture(hass, entry.entry_id)
    await charge(fcq, (9.0,))
    await fcq.set("soc", "60")
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    data = [g for g in got if g[0] == "charging"][-1][2]
    assert data["kwh"] == pytest.approx(6.0)
    assert data["location"] == "home"


async def test_restart_while_charging_keeps_the_open_session(hass, fcq, hass_storage):
    await fcq.set("evccstatus", "C")
    entry = await async_setup_ford_ev(hass, fcq)
    started = engine_of(entry).state.session.start_ts
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert engine_of(entry).state.session.start_ts == started


# ---------------------------------------------------------------------- trips, speed, sessions, power


def test_speed_bands():
    assert [speed_key(v) for v in (0, 29.9, 30, 75, 130)] == ["0", "0", "30", "60", "90"]
    rows = speed_rows({"30": {"km": 50.0, "kwh": 8.0}, "0": {"km": 5.0, "kwh": 2.0}})
    assert rows == [{"speed": 0, "km": 5, "kwh100": None}, {"speed": 30, "km": 50, "kwh100": 16.0}]


def test_trip_statistics():
    assert trip_statistics([], 0.0) == {"trips": 0}
    now = 10 * 86400.0
    trips = [
        {"end_ts": now - 8 * 86400, "km": 4.0, "kwh": 1.0, "pred": 0.8},
        {"end_ts": now - 86400, "km": 6.0, "kwh": None, "pred": 1.0},
        {"end_ts": now - 3600, "km": 40.0, "kwh": 8.0, "pred": 6.0},
        {"end_ts": now - 60, "km": 50.0, "kwh": 12.0, "pred": 9.0},
    ]
    stats = trip_statistics(trips, now)
    assert stats["trips"] == 4
    assert (stats["trips_7d"], stats["km_7d"]) == (3, 96.0)
    assert (stats["mean_km"], stats["median_km"], stats["longest_km"]) == (25.0, 23.0, 50.0)
    assert stats["short_trip_km_pct"] == 10
    assert (stats["best_kwh100"], stats["worst_kwh100"]) == (20.0, 24.0)
    # The model says 15 kWh for the two long trips that used 20.
    assert (stats["model_error_pct"], stats["model_error_trips"]) == (-25.0, 2)


async def test_trip_log_and_speed_band(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await drive(hass, freezer, fcq, 60, "1040", soc="40")
    await advance(hass, freezer, 600)
    await fcq.set("soc", "39")  # reported late: still the trip's
    st = engine_of(entry).state
    [trip] = st.trips
    assert (trip["km"], trip["speed"], trip["min"]) == (40.0, 40, 60)
    assert trip["kwh"] == pytest.approx(6.6)
    assert trip["pred"] == pytest.approx(st.model.a * st.model.cycle_x_drive + st.model.b * st.model.cycle_x_hvac, abs=1e-3)
    assert st.speed_bins == {"30": {"km": 40.0, "kwh": pytest.approx(6.6), "cost": 0.0}}
    sensor = state(hass, "trips")
    assert sensor.state == "1"
    assert sensor.attributes["by_speed"] == [{"speed": 30, "km": 40, "kwh100": 16.5}]
    assert sensor.attributes["model_error_trips"] == 1


async def test_trip_log_keeps_the_last_fifty(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    for n in range(52):
        await drive(hass, freezer, fcq, 10, str(1001 + n))
    trips = engine_of(entry).state.trips
    assert len(trips) == 50
    assert trips[-1]["km"] == 1.0


async def test_measured_consumption_has_the_model_next_to_it(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await drive(hass, freezer, fcq, 60, "1040", soc="40")
    [row] = state(hass, "consumption_by_temperature").attributes["by_temperature"]
    assert (row["t"], row["kwh100"]) == (20, 15.0)
    m, st = engine_of(entry).state.model, engine_of(entry).state.settings
    per_km = predicted_kwh_per_km(22.5, m.a, m.b, m.avg_speed, st.climate_setpoint, st.climate_on)
    assert row["model"] == round(per_km * 100, 1)


async def test_charging_power_and_session_statistics(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    assert state(hass, "charging_power").state == "unknown"
    await fcq.set("evccstatus", "C")
    await charge(fcq, (0.5,))
    await advance(hass, freezer, 1800)
    await charge(fcq, (4.0,))
    power = state(hass, "charging_power")
    assert float(power.state) == pytest.approx(7.0)
    # 50 % of 60 kWh to the target of 100 %, at 7 kW.
    assert power.attributes["to_target_min"] == round(30 / 7 * 60)
    await fcq.set("soc", "56")
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    assert state(hass, "charging_power").state == "unknown"
    sessions = state(hass, "charging_sessions")
    assert sessions.state == "1"
    assert sessions.attributes["mean_kwh"] == 3.5
    assert sessions.attributes["mean_start_soc"] == 50
    assert sessions.attributes["dc_share_pct"] == 0.0
    assert sessions.attributes["full_cycles"] == 0.1
    assert engine_of(entry).charging_power_kw is None


async def test_dc_session_counts_towards_the_dc_share(hass, fcq, freezer):
    await async_setup_ford_ev(hass, fcq)
    await fcq.set("evccstatus", "C")
    for value in (0.5, 20.5):
        await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": value, "chargingType": "DC_FAST"})
    await fcq.set("evccstatus", "A")
    await fcq.set("elvehcharging", "NOT_PLUGGED_IN", {"chargerEnergyOutput": 20.5})
    await advance(hass, freezer, 121)
    assert state(hass, "charging_sessions").attributes["dc_share_pct"] == 100.0


async def test_idle_energy_and_parked_shares(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("ignitionstatus", "ON")
    await advance(hass, freezer, 600)
    await fcq.set("soc", "49")
    await fcq.set("ignitionstatus", "OFF")
    await advance(hass, freezer, 121)
    st = engine_of(entry).state
    assert st.idle_energy == pytest.approx(0.6)
    attrs = state(hass, "parked_energy").attributes
    assert attrs["idle_kwh"] == 0.6
    assert attrs["share_pct"] == 100.0


async def test_house_power_sensor_replaces_the_base_load(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "3000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.house", "1", {"unit_of_measurement": "kW"})
    entry = await async_setup_ford_ev(
        hass,
        fcq,
        options={"price_fixed": 1.5, "solar_power_entity": "sensor.pv", "house_load_entity": "sensor.house"},
    )
    engine = engine_of(entry)
    assert engine.solar_surplus_kw() == pytest.approx(2.0)
    await charge(fcq, (0.5,))
    freezer.tick(timedelta(minutes=15))
    hass.states.async_set("sensor.house", "3", {"unit_of_measurement": "kW"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=15))
    await charge(fcq, (2.5,))
    assert engine.state.charger_energy_solar == pytest.approx(0.5)
    hass.states.async_set("sensor.house", "unavailable")
    # Without a value the fixed base load (300 W) applies.
    assert engine.solar_surplus_kw() == pytest.approx(2.7)


async def test_cost_shares_and_year_projection(hass, fcq, freezer):
    freezer.move_to(dt_util.now().replace(day=10, hour=12, minute=0, second=0, microsecond=0))
    hass.states.async_set("sensor.pv", "2300", {"unit_of_measurement": "W"})
    await async_setup_ford_ev(
        hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0, "solar_power_entity": "sensor.pv"}
    )
    await charge(fcq, (0.5,))
    await advance(hass, freezer, 1800)
    await charge(fcq, (4.5,))
    attrs = state(hass, "cost_per_100km_month").attributes
    assert attrs["home_share_pct"] == 100.0
    # 2.0 kW of surplus for 30 min, of 4 kWh.
    assert attrs["solar_share_pct"] == 25.0
    await advance(hass, freezer, 4 * 86400)
    await drive(hass, freezer, fcq, 60, "1100", soc="40")
    projection = state(hass, "cost_month_projection").attributes
    daily = state(hass, "days_of_range").attributes["daily_km"]
    assert projection["year_km"] == pytest.approx(daily * 365, abs=40)
    per_100 = float(state(hass, "cost_per_100km_total").state)
    assert projection["year_cost"] == pytest.approx(daily * 365 * per_100 / 100, abs=2)


async def test_weekly_summary_compares_with_the_one_before(hass, fcq, freezer):
    from .test_alerts import OPTS, PHONE, at_local  # noqa: PLC0415

    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(19, 59, 50, weekday=6))
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("odometer", "1100")
    await fcq.set("soc", "35")
    await advance(hass, freezer, 15)
    await advance(hass, freezer, 7 * 86400)
    summaries = [c.data["message"] for c in calls if c.data["title"] == "Weekly summary"]
    assert "Before:" not in summaries[0]
    assert "Before: 100 km, 9.0 kWh/100 km." in summaries[1]


async def test_options_flow_saves_the_house_power_sensor(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"house_load_entity": "sensor.old"})
    for value in ("sensor.house", None):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        user_input = {"battery_kwh": 60, "temperature_sensor": "sensor.outside"}
        if value:
            user_input["house_load_entity"] = value
        await hass.config_entries.options.async_configure(result["flow_id"], user_input)
        await hass.async_block_till_done()
        # Cleared, it is stored as None and does not fall back to the old value.
        assert entry.options["house_load_entity"] == value


# ---------------------------------------------------------------------- calibration at the target, CO2


def test_calibration_below_full_adds_what_the_battery_is_short_of():
    # Began 30 kWh short of full, ends 12 kWh short: 36 charged, 18 used.
    st = RangeModelState(cycle_x_drive=16, cycle_x_hvac=2, cycle_charged=36, cycle_km=100, cycle_deficit=30)
    st2, result = calibrate(st, 60.0, 1000.0, None, None, None, estimate_capacity=False, end_deficit=12.0)
    assert result.used_kwh == pytest.approx(18.0)
    assert result.valid
    assert st2.model_energy == pytest.approx(48.0)
    assert st2.cycle_deficit == pytest.approx(12.0)


@pytest.mark.parametrize(("option", "calibrated"), [(True, True), (False, False)])
async def test_calibration_at_the_charge_target(hass, fcq, freezer, option, calibrated):
    entry = await async_setup_ford_ev(hass, fcq, options={"calibrate_at_target": option})
    engine = engine_of(entry)
    engine.set_setting("charge_target", 80.0)
    got = capture(hass, entry.entry_id)
    await drive(hass, freezer, fcq, 120, "1200", soc="20")
    await _session(hass, freezer, fcq, (0.5, 36.5), "80")
    cals = [e[2] for e in got if e[1] == "calibration"]
    assert bool(cals) is calibrated
    if calibrated:
        assert cals[0]["used_kwh"] == pytest.approx(18.0)
        assert cals[0]["capacity_estimate_kwh"] is None
        assert engine.state.model.model_energy == pytest.approx(48.0)
        assert float(state(hass, "model_soc").state) == pytest.approx(80.0)


async def test_co2_saved(hass, fcq, freezer):
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415

    from .conftest import VIN  # noqa: PLC0415

    entry = await async_setup_ford_ev(
        hass, fcq, options={"ice_consumption": 8.0, "fuel_co2_kg_l": 2.5, "grid_co2_g_kwh": 250, "ac_loss_pct": 0}
    )
    await drive(hass, freezer, fcq, 60, "1100", soc="30")
    # 100 km: 8 l x 2.5 kg against 12 kWh x 0.25 kg.
    assert float(state(hass, "co2_savings").state) == pytest.approx(20.0 - 3.0)
    await hass.config_entries.async_remove(entry.entry_id)
    other = await async_setup_ford_ev(hass, fcq, options={"ice_consumption": 8.0})
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_co2_savings") is None
    assert other.state.name == "LOADED"

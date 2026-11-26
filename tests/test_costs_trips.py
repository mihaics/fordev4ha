"""Tests for the cost of driving, long periods, trips, parked energy and their sensors."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.ford_ev.accounting import consume_value, seed_price
from custom_components.ford_ev.const import DOMAIN
from custom_components.ford_ev.periods import (
    LIFETIME,
    PeriodSnapshot,
    PeriodState,
    period_start,
    period_value,
    roll_periods,
    seed_long_periods,
)
from custom_components.ford_ev.state import EngineState
from custom_components.ford_ev.trips import add_to_bin, bin_key, bin_rows, measured_consumption

from .conftest import VIN, async_setup_ford_ev
from .test_engine import advance, capture, engine_of

AC = {"chargingType": "AC_BASIC"}


def state(hass, key):
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_{key}")
    assert entity_id is not None, key
    return hass.states.get(entity_id)


def _store(hass_storage, entry_id, data):
    key = f"{DOMAIN}.{entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


async def charge(fcq, values):
    for value in values:
        await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": value, **AC})


async def drive(hass, freezer, fcq, minutes, odometer, soc=None):
    await fcq.set("ignitionstatus", "ON")
    await advance(hass, freezer, minutes * 60)
    await fcq.set("odometer", odometer)
    if soc is not None:
        await fcq.set("soc", soc)
    await fcq.set("ignitionstatus", "OFF")
    await advance(hass, freezer, 121)


# ---------------------------------------------------------------------- pure functions


def test_consume_value_takes_the_share_used():
    cost, left = consume_value(60.0, 30.0, 3.0)
    assert (cost, left) == (pytest.approx(6.0), pytest.approx(54.0))


def test_consume_value_never_goes_negative():
    assert consume_value(10.0, 2.0, 5.0) == (pytest.approx(10.0), pytest.approx(0.0))
    assert consume_value(10.0, 0.0, 5.0) == (pytest.approx(10.0), pytest.approx(0.0))
    assert consume_value(0.0, 30.0, 5.0) == (0.0, 0.0)


def test_seed_price_is_the_average_paid_within_the_configured_prices():
    assert seed_price(120.0, 60.0, [1.1, 1.65, 2.625], 1.65) == pytest.approx(2.0)
    assert seed_price(500.0, 50.0, [1.0, 2.0], 1.0) == pytest.approx(2.0)
    assert seed_price(1.0, 50.0, [1.0, 2.0], 1.0) == pytest.approx(1.0)


def test_seed_price_without_history_is_the_home_price():
    assert seed_price(3.0, 2.0, [1.5, 2.5], 1.5) == pytest.approx(1.5)
    assert seed_price(0.0, 0.0, [], None) == 0.0


def test_year_period_and_lifetime_snapshot():
    assert period_start("year", datetime(2026, 11, 22, 12)).isoformat() == "2026-01-01"
    totals = {"odometer": 1000.0, "cost": 5.0}
    st = roll_periods(PeriodState({}, {}), datetime(2026, 12, 30, 9), totals)
    later = {"odometer": 1500.0, "cost": 50.0}
    st = roll_periods(st, datetime(2027, 1, 2, 9), later)
    assert st.previous["year"] == {"odometer": 500.0, "cost": 45.0}
    assert period_value(st, "year", "odometer", later) == 0
    # The lifetime never rolls.
    assert st.snapshots[LIFETIME].start == "2026-12-30"
    assert period_value(st, LIFETIME, "odometer", later) == 500.0


def test_long_periods_start_from_a_month_that_never_rolled():
    month = PeriodSnapshot("2026-11-01", {"odometer": 1000.0, "cost": 0.0})
    st = seed_long_periods(PeriodState({"month": month}, {"week": {"cost": 1.0}}), datetime(2026, 11, 26, 9))
    assert st.snapshots["year"].start == "2026-01-01"
    assert st.snapshots["year"].values == {"odometer": 1000.0, "cost": 0.0}
    assert st.snapshots[LIFETIME].start == "2026-11-01"
    # Later restarts leave them alone.
    st.snapshots[LIFETIME].values["cost"] = 7.0
    assert seed_long_periods(st, datetime(2026, 11, 27, 9)).snapshots[LIFETIME].values["cost"] == 7.0


def test_long_periods_not_seeded_after_a_month_rollover():
    month = PeriodSnapshot("2026-11-01", {"odometer": 900.0})
    st = seed_long_periods(PeriodState({"month": month}, {"month": {"odometer": 300.0}}), datetime(2026, 11, 26, 9))
    assert "year" not in st.snapshots and LIFETIME not in st.snapshots


def test_temperature_bands():
    assert (bin_key(-0.1), bin_key(0.0), bin_key(4.9), bin_key(12.0), bin_key(-7.0)) == ("-5", "0", "0", "10", "-10")
    bins = {}
    add_to_bin(bins, "10", km=30, kwh=6, cost=9)
    add_to_bin(bins, "-5", km=10, kwh=3, cost=5)
    add_to_bin(bins, "5", km=40, kwh=8, cost=12)
    rows = bin_rows(bins)
    assert [r["t"] for r in rows] == [-5, 5, 10]
    assert rows[0]["kwh100"] is None and rows[0]["cost100"] is None  # under 20 km
    assert rows[2] == {"t": 10, "km": 30, "kwh100": 20.0, "cost100": 30.0}
    assert measured_consumption(bins) == pytest.approx(17 / 80 * 100)
    assert measured_consumption({"0": {"km": 5.0, "kwh": 1.0, "cost": 1.0}}) is None


def test_state_round_trip_and_capacity_estimate_from_the_last_calibration():
    st = EngineState.from_dict({"model": {"last_result": {"capacity_estimate_kwh": 56.5}}, "temp_bins": {"5": {"km": 1.0}}})
    assert st.capacity_estimate == 56.5
    assert st.battery_value is None
    again = EngineState.from_dict({**st.to_dict(), "battery_value": 12.0, "capacity_estimate": None})
    assert again.battery_value == 12.0
    assert again.capacity_estimate is None
    assert again.temp_bins == {"5": {"km": 1.0}}


# ---------------------------------------------------------------------- inventory


async def test_fresh_install_values_the_battery_at_the_home_price(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0})
    st = engine_of(entry).state
    # 50 % of 60 kWh at 1.0 plus 10 % AC losses.
    assert st.battery_value == pytest.approx(33.0)
    assert st.driving_cost == 0
    assert float(state(hass, "battery_energy_price").state) == pytest.approx(1.1)
    assert state(hass, "battery_energy_price").attributes["unit_of_measurement"] == f"{hass.config.currency}/kWh"


async def test_driving_takes_value_out_and_charging_puts_it_in(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "public_price_fixed": 3.0, "ac_loss_pct": 0, "dc_loss_pct": 0})
    engine = engine_of(entry)
    await fcq.set("soc", "40")  # 6 kWh at 1.0
    assert engine.state.driving_cost == pytest.approx(6.0)
    assert engine.state.battery_value == pytest.approx(24.0)
    await fcq.set("tracker", "not_home")
    await fcq.set("evccstatus", "C")
    await charge(fcq, (0.5, 12.5))  # 12 kWh at 3.0
    await fcq.set("soc", "60")
    assert engine.state.battery_value == pytest.approx(60.0)
    assert engine.battery_price() == pytest.approx(60.0 / 36.0)
    await fcq.set("evccstatus", "A")
    await fcq.set("soc", "50")  # 6 of 36 kWh
    assert engine.state.driving_cost == pytest.approx(6.0 + 10.0)
    # Everything paid is either used or still in the battery.
    assert engine.state.driving_cost + engine.state.battery_value == pytest.approx(30.0 + 36.0)


async def test_regeneration_does_not_change_the_price(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 2.0, "ac_loss_pct": 0})
    engine = engine_of(entry)
    await fcq.set("soc", "52")  # regeneration, not paid for
    assert engine.battery_price() == pytest.approx(2.0)
    await fcq.set("soc", "48")
    assert engine.state.driving_cost == pytest.approx(2 * 0.6 * 2.0)
    assert engine.battery_price() == pytest.approx(2.0)


async def test_seed_waits_for_the_price_entity(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_entity": "input_number.price", "ac_loss_pct": 0})
    engine = engine_of(entry)
    assert engine.state.battery_value is None
    assert "driving_cost" not in engine.totals()
    assert state(hass, "driving_cost").state == "unknown"
    hass.states.async_set("input_number.price", "1.5")
    await hass.async_block_till_done()
    assert engine.state.battery_value == pytest.approx(30 * 1.5)
    assert float(state(hass, "driving_cost").state) == 0


async def test_seed_uses_the_state_of_charge_before_the_change(hass, fcq, hass_storage):
    st = EngineState.new()
    st.soc.last_soc = 50.0
    _store(hass_storage, "s1", st.to_dict())
    entry = await async_setup_ford_ev(hass, fcq, options={"price_entity": "input_number.price", "ac_loss_pct": 0}, entry_id="s1")
    engine = engine_of(entry)
    await fcq.set("soc", "48")
    assert engine.state.battery_value is None
    hass.states.async_set("input_number.price", "1.0")
    await hass.async_block_till_done()
    # Valued at 48 %, and the 2 % used before are priced once, not taken out again.
    assert engine.state.battery_value == pytest.approx(0.48 * 60)
    assert engine.state.driving_cost == pytest.approx(1.2)


async def test_upgrade_of_an_existing_installation(hass, fcq, freezer, hass_storage):
    """A store written before cost tracking: tracked for some days, the month never rolled."""
    freezer.move_to("2026-11-26 10:00:00+00:00")
    values = {"energy_added": 24.0, "charger_energy": 60.0, "charger_energy_home": 40.0, "charger_energy_solar": 4.0,
              "cost": 120.0, "cost_home": 70.0, "cost_public": 50.0}
    zero = dict.fromkeys(values, 0.0)
    _store(hass_storage, "up", {
        "soc": {"last_soc": 40.0, "regen_credit": 0.0, "energy_used": 20.0, "energy_added": 24.0, "energy_added_home": 0.0},
        "charger_last_reading": 20.0, "charger_energy": 60.0, "charger_energy_home": 40.0, "charger_energy_solar": 4.0,
        "cost": 120.0, "cost_home": 70.0, "cost_public": 50.0, "last_odometer_km": 1100.0,
        "periods": {
            "snapshots": {
                "day": {"start": "2026-11-26", "values": {**values, "energy_used": 18.0, "odometer": 1090.0}},
                "week": {"start": "2026-11-23", "values": {**values, "energy_used": 2.0, "odometer": 1020.0}},
                "month": {"start": "2026-11-01", "values": {**zero, "energy_used": 0.0, "odometer": 1000.0}},
            },
            "previous": {"week": {"energy_used": 2.0, "odometer": 20.0, "cost": 80.0}},
        },
        "model": {"a": 0.8, "odo_accounted": 1100.0, "model_energy": 25.0},
    })
    await fcq.set("soc", "40")
    await fcq.set("odometer", "1100")
    options = {"price_fixed": 1.5, "public_price_fixed": 2.5, "solar_power_entity": "sensor.pv", "solar_price_fixed": 1.0}
    hass.states.async_set("sensor.pv", "0", {"unit_of_measurement": "W"})
    entry = await async_setup_ford_ev(hass, fcq, options=options, entry_id="up")
    engine = engine_of(entry)
    st = engine.state
    price = 2.0  # 120 paid for 60 kWh, inside the configured prices
    assert st.battery_value == pytest.approx(0.40 * 60 * price)
    assert st.driving_cost == pytest.approx(20.0 * price)
    totals, periods = engine.totals(), st.periods
    assert period_value(periods, "month", "driving_cost", totals) == pytest.approx(40.0)
    assert period_value(periods, "week", "driving_cost", totals) == pytest.approx(36.0)
    assert period_value(periods, "year", "odometer", totals) == pytest.approx(100.0)
    assert period_value(periods, LIFETIME, "driving_cost", totals) == pytest.approx(40.0)
    assert periods.previous["week"]["driving_cost"] == pytest.approx(4.0)
    assert periods.snapshots[LIFETIME].start == "2026-11-01"
    for key in ("cost_per_100km_month", "cost_per_100km_year", "cost_per_100km_total"):
        assert float(state(hass, key).state) == pytest.approx(40.0), key
    assert float(state(hass, "cost_per_100km_week").state) == pytest.approx(45.0)
    attrs = state(hass, "cost_per_100km_month").attributes
    assert attrs["km"] == 100 and attrs["charging_cost"] == 120.0 and attrs["charging_cost_public"] == 50.0
    assert attrs["price_per_kwh"] == 2.0
    assert state(hass, "cost_per_100km_week").attributes["previous_period"] == 20.0
    assert float(state(hass, "cost_year").state) == pytest.approx(120.0)
    # The day tracking started is not known, so there is no projection for this month.
    assert state(hass, "cost_month_projection").state == "unknown"

    # A restart changes nothing.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = engine_of(entry).state
    assert again.driving_cost == pytest.approx(40.0)
    assert again.battery_value == pytest.approx(48.0)
    assert period_value(again.periods, LIFETIME, "odometer", engine_of(entry).totals()) == pytest.approx(100.0)


async def test_past_charge_does_not_change_the_cost_of_driving(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0})
    engine = engine_of(entry)
    before = engine.state.battery_value
    when = (dt_util.now() - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    await hass.services.async_call(DOMAIN, "add_charge", {"date": when, "energy_kwh": 10, "location": "home"}, blocking=True)
    assert engine.state.battery_value == before
    assert engine.state.driving_cost == 0
    assert engine.state.cost == pytest.approx(11.0)


# ---------------------------------------------------------------------- trips and parked energy


async def test_trip_record_band_and_cost(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    got = capture(hass, entry.entry_id)
    await drive(hass, freezer, fcq, 30, "1030", soc="40")
    st = engine_of(entry).state
    trip = st.last_trip
    assert trip["km"] == 30 and trip["duration_min"] == 30 and trip["avg_speed"] == 60
    assert trip["kwh"] == pytest.approx(6.0) and trip["cost"] == pytest.approx(6.0)
    assert trip["kwh100"] == 20.0 and trip["cost100"] == 20.0
    assert st.temp_bins == {"20": {"km": pytest.approx(30.0), "kwh": pytest.approx(6.0), "cost": pytest.approx(6.0)}}
    assert st.parked_energy == 0
    event = [g for g in got if g[1] == "trip_finished"][-1][2]
    assert event["km"] == 30 and "bin" not in event
    assert "30 km, 6.0 kWh (20.0 kWh/100 km), 20 °C" in state(hass, "last_trip").state
    assert state(hass, "last_trip").attributes["avg_speed"] == 60
    assert float(state(hass, "consumption_by_temperature").state) == pytest.approx(20.0)
    assert float(state(hass, "cost_per_100km_week").state) == pytest.approx(20.0)
    assert float(state(hass, "cost_per_100km_total").state) == pytest.approx(20.0)


async def test_energy_reported_after_the_trip_belongs_to_it(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    await drive(hass, freezer, fcq, 30, "1030")
    st = engine_of(entry).state
    assert st.last_trip["kwh"] == 0
    await advance(hass, freezer, 600)
    await fcq.set("soc", "45")
    assert st.last_trip["kwh"] == pytest.approx(3.0)
    assert st.last_trip["kwh100"] == 10.0
    assert st.temp_bins["20"]["kwh"] == pytest.approx(3.0)
    assert st.parked_energy == 0


async def test_energy_used_while_parked(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    await drive(hass, freezer, fcq, 30, "1030", soc="45")
    await advance(hass, freezer, 3600)
    await fcq.set("soc", "44")
    st = engine_of(entry).state
    assert st.parked_energy == pytest.approx(0.6)
    assert st.parked_cost == pytest.approx(0.6)
    assert st.last_trip["kwh"] == pytest.approx(3.0)
    assert float(state(hass, "parked_energy").state) == pytest.approx(0.6)
    assert state(hass, "parked_energy").attributes["cost"] == 0.6


async def test_ignition_on_without_driving_counts_as_parked(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    await fcq.set("ignitionstatus", "ON")
    await advance(hass, freezer, 900)
    await fcq.set("soc", "49")
    await fcq.set("ignitionstatus", "OFF")
    await advance(hass, freezer, 121)
    st = engine_of(entry).state
    assert st.last_trip is None
    assert st.parked_energy == pytest.approx(0.6)


async def test_short_stop_is_one_trip(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("ignitionstatus", "ON")
    await advance(hass, freezer, 600)
    await fcq.set("odometer", "1010")
    await fcq.set("ignitionstatus", "OFF")
    await advance(hass, freezer, 60)
    await fcq.set("ignitionstatus", "ON")
    await advance(hass, freezer, 240)
    st = engine_of(entry).state
    # The first leg was not closed by its timer while the car drives on.
    assert st.last_trip is None
    assert st.model.trip_start_ts is not None
    await advance(hass, freezer, 300)
    await fcq.set("odometer", "1020")
    await fcq.set("soc", "46")
    await fcq.set("ignitionstatus", "OFF")
    await advance(hass, freezer, 121)
    assert st.last_trip["km"] == 20
    assert st.last_trip["duration_min"] == 20
    assert st.last_trip["kwh"] == pytest.approx(2.4)


async def test_trip_started_while_stopped_is_still_recorded(hass, fcq, freezer):
    await fcq.set("ignitionstatus", "unavailable")
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("ignitionstatus", "ON")
    assert engine_of(entry).state.trip_energy_start == 0
    await advance(hass, freezer, 600)
    await fcq.set("odometer", "1010")
    await fcq.set("soc", "47")
    await fcq.set("ignitionstatus", "OFF")
    await advance(hass, freezer, 121)
    assert engine_of(entry).state.last_trip["kwh"] == pytest.approx(1.8)


# ---------------------------------------------------------------------- sensors


async def test_new_sensors_present_with_valid_units(hass, fcq):
    hass.states.async_set("sensor.pv", "0", {"unit_of_measurement": "W"})
    await async_setup_ford_ev(
        hass, fcq, options={"price_fixed": 1.0, "ice_consumption": 7.0, "fuel_price_fixed": 8.0, "solar_power_entity": "sensor.pv"}
    )
    currency = hass.config.currency
    for key in ("cost_per_100km_week", "cost_per_100km_month", "cost_per_100km_year", "cost_per_100km_total", "model_cost_per_100km"):
        st = state(hass, key)
        assert st.attributes["unit_of_measurement"] == f"{currency}/100 km", key
        assert st.attributes["state_class"] == "measurement", key
        assert "device_class" not in st.attributes, key
    for key in ("distance_year", "energy_used_year", "charger_energy_year", "cost_year", "consumption_year", "driving_cost",
                "battery_energy_price", "model_cost_per_100km_tomorrow", "cost_month_projection", "charge_to_target_cost",
                "fuel_savings", "solar_savings", "consumption_by_temperature", "last_trip", "parked_energy", "capacity_estimate"):
        assert state(hass, key) is not None, key
    assert state(hass, "driving_cost").attributes["state_class"] == "total"
    assert state(hass, "capacity_estimate").attributes["device_class"] == "energy_storage"
    assert state(hass, "cost_year").attributes["last_reset"].startswith(f"{datetime.now().year}-01-01")


async def test_optional_sensors_absent_without_their_options(hass, fcq):
    await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0})
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{VIN}_fuel_savings") is None
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{VIN}_solar_savings") is None


async def test_model_cost_by_temperature_and_price(hass, fcq):
    hass.states.async_set("sensor.pv", "0", {"unit_of_measurement": "W"})
    await async_setup_ford_ev(
        hass, fcq,
        options={"price_fixed": 1.5, "public_price_fixed": 2.5, "solar_power_entity": "sensor.pv", "solar_price_fixed": 1.0,
                 "ac_loss_pct": 10, "dc_loss_pct": 0},
    )
    st = state(hass, "model_cost_per_100km")
    consumption = float(state(hass, "model_consumption").state)
    assert float(st.state) == pytest.approx(consumption * 1.65)
    assert st.attributes["prices_per_kwh"] == {"home": 1.65, "solar": 1.1, "public": 2.5, "battery": 1.65}
    row = next(r for r in st.attributes["by_temperature"] if r["t"] == 20)
    assert row["home"] == pytest.approx(row["kwh100"] * 1.65, abs=0.1)
    assert row["public"] == pytest.approx(row["kwh100"] * 2.5, abs=0.15)
    cold = next(r for r in st.attributes["by_temperature"] if r["t"] == -10)
    assert cold["home"] > row["home"]


async def test_fuel_and_solar_savings(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "2300", {"unit_of_measurement": "W"})
    await async_setup_ford_ev(
        hass, fcq,
        options={"price_fixed": 1.0, "ac_loss_pct": 0, "ice_consumption": 7.0, "fuel_price_fixed": 8.0,
                 "solar_power_entity": "sensor.pv", "solar_price_fixed": 0.4},
    )
    await drive(hass, freezer, fcq, 30, "1100", soc="30")  # 100 km, 12 kWh at 1.0
    st = state(hass, "fuel_savings")
    assert float(st.state) == pytest.approx(100 * 7 / 100 * 8 - 12)
    assert st.attributes["week"] == pytest.approx(44.0)
    assert st.attributes["fuel_cost_per_100km"] == 56.0
    await charge(fcq, (0.5,))
    freezer.tick(timedelta(minutes=30))
    await charge(fcq, (2.5,))  # 2 kWh, 1 kWh of it solar
    assert float(state(hass, "solar_savings").state) == pytest.approx(1.0 * (1.0 - 0.4))


async def test_cost_to_charge_to_target(hass, fcq):
    await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "public_price_fixed": 2.0, "dc_loss_pct": 5})
    st = state(hass, "charge_to_target_cost")
    assert float(st.state) == pytest.approx(30 * 1.1 * 1.0)
    assert st.attributes["kwh"] == 30.0
    await fcq.set("tracker", "not_home")
    assert float(state(hass, "charge_to_target_cost").state) == pytest.approx(30 * 1.05 * 2.0)


async def test_month_projection(hass, fcq, freezer):
    freezer.move_to("2026-11-10 12:00:00+00:00")
    await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    freezer.move_to("2026-11-20 12:00:00+00:00")
    await fcq.set("odometer", "1100")
    await fcq.set("soc", "40")
    st = state(hass, "cost_month_projection")
    # 6.0 in the tracked days since the 10th, about as many days of the month left.
    start = dt_util.start_of_local_day(dt_util.now().replace(day=10))
    end = dt_util.start_of_local_day(dt_util.now().replace(month=12, day=1))
    factor = 1 + (end - dt_util.now()) / (dt_util.now() - start)
    assert 1.8 < factor < 2.3
    assert float(st.state) == pytest.approx(6.0 * factor)
    assert st.attributes["km"] == round(100 * factor)


async def test_session_summary_has_average_power(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    got = capture(hass, entry.entry_id)
    await fcq.set("evccstatus", "C")
    await charge(fcq, (0.5,))
    await advance(hass, freezer, 3600)
    await charge(fcq, (7.5,))
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    data = [g for g in got if g[0] == "charging"][-1][2]
    assert data["avg_kw"] == pytest.approx(7.0, abs=0.1)
    assert data["price_per_kwh"] == pytest.approx(1.0)
    assert "+7.0 kWh at 7.0 kW" in data["summary"]


async def test_weekly_summary_has_the_cost_of_driving(hass, fcq, freezer):
    from .test_alerts import OPTS, PHONE, at_local  # noqa: PLC0415

    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(19, 59, 50, weekday=6))
    await async_setup_ford_ev(hass, fcq, options={**OPTS, "price_fixed": 1.0, "ac_loss_pct": 0})
    await fcq.set("odometer", "1100")
    await fcq.set("soc", "35")
    await advance(hass, freezer, 15)
    assert "Driving cost 9.00" in calls[0].data["message"]
    assert "(9.00/100 km)" in calls[0].data["message"]


async def test_options_flow_saves_fuel_settings(hass, fcq):
    hass.states.async_set("sensor.outside", "20", {"unit_of_measurement": "°C", "device_class": "temperature"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VIN,
        data={"vin": VIN, "device_id": fcq.device_id, "battery_kwh": 60, "temperature_sensor": "sensor.outside", "weather_entity": None},
        options={"fuel_price_fixed": 9.0},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.ford_ev.async_setup_entry", return_value=True):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(
            r["flow_id"], {"ice_consumption": 6.5, "battery_kwh": 60, "temperature_sensor": "sensor.outside"}
        )
        await hass.async_block_till_done()
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["ice_consumption"] == 6.5
    assert entry.options["fuel_price_fixed"] is None
    assert entry.options["fuel_price_entity"] is None

"""Tests for charging cost: losses, public price, solar share, the add_charge service."""

from datetime import timedelta

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.ford_ev.const import DOMAIN
from custom_components.ford_ev.periods import period_value
from custom_components.ford_ev.state import EngineState

from .conftest import async_setup_ford_ev

AC = {"chargingType": "AC_BASIC"}
DC = {"chargingType": "DC_FAST"}


def engine_of(entry):
    return entry.runtime_data.engine


async def readings(fcq, values, attrs=None, freezer=None, minutes=0):
    for value in values:
        if freezer is not None and minutes:
            freezer.tick(timedelta(minutes=minutes))
        await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": value, **(attrs or AC)})


def test_old_cost_becomes_home_cost():
    st = EngineState.from_dict({"cost": 5.0, "charger_energy_home": 3.0})
    assert st.cost_home == pytest.approx(5.0)
    assert st.cost_public == 0
    assert st.cost == pytest.approx(5.0)


def test_new_fields_survive_round_trip():
    st = EngineState.from_dict({"cost": 7.0, "cost_home": 2.0, "cost_public": 5.0, "charger_energy_solar": 1.5})
    again = EngineState.from_dict(st.to_dict())
    assert (again.cost_home, again.cost_public, again.charger_energy_solar) == (2.0, 5.0, 1.5)


async def test_home_ac_loss_is_billed(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0})
    await readings(fcq, (0.5, 4.5))
    st = engine_of(entry).state
    assert st.charger_energy_home == pytest.approx(4.0)
    assert st.cost_home == pytest.approx(4.4)
    assert st.cost == pytest.approx(4.4)


async def test_loss_options_are_used(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    await readings(fcq, (0.5, 4.5))
    assert engine_of(entry).state.cost == pytest.approx(4.0)


async def test_public_dc_charge_uses_public_price_and_dc_loss(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.5, "public_price_fixed": 2.55})
    await fcq.set("tracker", "not_home")
    await readings(fcq, (0.5, 23.5), DC)
    st = engine_of(entry).state
    assert st.cost_public == pytest.approx(23.0 * 1.05 * 2.55)
    assert st.cost_home == 0
    assert st.cost == pytest.approx(st.cost_public)


async def test_public_price_entity_takes_precedence(hass, fcq):
    hass.states.async_set("input_number.public", "2.0")
    entry = await async_setup_ford_ev(
        hass, fcq, options={"public_price_fixed": 2.55, "public_price_entity": "input_number.public", "dc_loss_pct": 0}
    )
    await fcq.set("tracker", "not_home")
    await readings(fcq, (0.5, 10.5), DC)
    assert engine_of(entry).state.cost_public == pytest.approx(20.0)


async def test_public_without_price_costs_nothing(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0})
    await fcq.set("tracker", "not_home")
    await readings(fcq, (0.5, 2.0))
    assert engine_of(entry).state.cost == 0


async def test_solar_surplus_priced_at_solar_price(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "2300", {"unit_of_measurement": "W"})
    entry = await async_setup_ford_ev(
        hass, fcq, options={"price_fixed": 1.5, "solar_power_entity": "sensor.pv", "solar_price_fixed": 1.0}
    )
    await readings(fcq, (0.5,))
    await readings(fcq, (2.5,), freezer=freezer, minutes=30)
    st = engine_of(entry).state
    # 2.0 kWh reported, 2.2 billed; 2.0 kW surplus over 30 min = 1.0 kWh solar.
    assert st.charger_energy_solar == pytest.approx(1.0)
    assert st.cost_home == pytest.approx(1.2 * 1.5 + 1.0 * 1.0)


async def test_solar_power_in_kw(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "2.3", {"unit_of_measurement": "kW"})
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.5, "solar_power_entity": "sensor.pv"})
    await readings(fcq, (0.5,))
    await readings(fcq, (2.5,), freezer=freezer, minutes=30)
    st = engine_of(entry).state
    assert st.charger_energy_solar == pytest.approx(1.0)
    # No solar price set: solar kWh are free.
    assert st.cost_home == pytest.approx(1.2 * 1.5)


async def test_solar_share_never_exceeds_billed_energy(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "5000", {"unit_of_measurement": "W"})
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.5, "solar_power_entity": "sensor.pv"})
    await readings(fcq, (0.5,))
    await readings(fcq, (1.5,), freezer=freezer, minutes=600)
    st = engine_of(entry).state
    assert st.charger_energy_solar == pytest.approx(1.1)
    assert st.cost_home == 0


async def test_solar_window_starts_at_session_start(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "2300", {"unit_of_measurement": "W"})
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "solar_power_entity": "sensor.pv"})
    await readings(fcq, (10.0,))  # stale value from an earlier session
    freezer.tick(timedelta(days=1))
    await fcq.set("evccstatus", "C")
    freezer.tick(timedelta(minutes=30))
    await readings(fcq, (5.0,))
    # Only the 30 min since the session started count: 2.0 kW x 0.5 h.
    assert engine_of(entry).state.charger_energy_solar == pytest.approx(1.0)


async def test_cost_sensors(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "public_price_fixed": 2.0, "ac_loss_pct": 0})
    await readings(fcq, (0.5, 2.5))
    await fcq.set("tracker", "not_home")
    await readings(fcq, (3.5,))
    assert float(hass.states.get("sensor.ev_charging_cost").state) == pytest.approx(4.0)
    assert float(hass.states.get("sensor.ev_charging_cost_at_home").state) == pytest.approx(2.0)
    assert float(hass.states.get("sensor.ev_public_charging_cost").state) == pytest.approx(2.0)
    assert hass.states.get("sensor.ev_energy_charged_from_solar") is not None
    assert engine_of(entry).state.cost == pytest.approx(4.0)


async def test_public_session_summary_shows_cost(hass, fcq, freezer):
    from .test_engine import advance, capture  # noqa: PLC0415

    entry = await async_setup_ford_ev(hass, fcq, options={"public_price_fixed": 2.0, "dc_loss_pct": 0})
    got = capture(hass, entry.entry_id)
    await fcq.set("tracker", "not_home")
    await fcq.set("evccstatus", "C")
    await readings(fcq, (0.5, 10.5), DC)
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    data = got[-1][2]
    assert data["location"] == "away"
    assert data["cost"] == pytest.approx(20.0)
    assert "20.00" in data["summary"]


# ---------------------------------------------------------------------- add_charge service


async def _call(hass, **data):
    await hass.services.async_call(DOMAIN, "add_charge", data, blocking=True)


async def test_add_home_charge_with_solar_share(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.5, "solar_price_fixed": 1.0})
    when = (dt_util.now() - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    await _call(hass, date=when, energy_kwh=10, location="home", solar_share=0.2)
    st = engine_of(entry).state
    assert st.charger_energy == pytest.approx(10.0)
    assert st.charger_energy_home == pytest.approx(10.0)
    assert st.charger_energy_solar == pytest.approx(2.2)
    assert st.cost_home == pytest.approx(11 * (0.8 * 1.5 + 0.2 * 1.0))
    assert st.cost == pytest.approx(st.cost_home)


async def test_add_public_cost_only(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    when = (dt_util.now() - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    await _call(hass, date=when, energy_kwh=0, location="public", cost=61.33)
    st = engine_of(entry).state
    assert st.cost_public == pytest.approx(61.33)
    assert st.cost == pytest.approx(61.33)
    assert st.charger_energy == 0


async def test_add_public_charge_uses_dc_loss_by_default(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"public_price_fixed": 2.0})
    when = (dt_util.now() - timedelta(hours=3)).replace(tzinfo=None).isoformat()
    await _call(hass, date=when, energy_kwh=10, location="public")
    st = engine_of(entry).state
    assert st.cost_public == pytest.approx(10 * 1.05 * 2.0)
    assert st.charger_energy_home == 0


async def test_add_charge_lands_in_its_period(hass, fcq, freezer):
    freezer.move_to("2026-11-20 20:00:00+00:00")  # Friday, local time zone of the test instance
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0})
    freezer.move_to("2026-11-25 20:00:00+00:00")  # Wednesday of the next week
    await fcq.set("odometer", "1010")  # rolls the periods
    engine = engine_of(entry)
    await _call(hass, date="2026-11-19T10:00:00", energy_kwh=10, location="home")
    totals = engine.totals()
    periods = engine.state.periods
    assert engine.state.cost == pytest.approx(11.0)
    assert period_value(periods, "month", "cost", totals) == pytest.approx(11.0)
    assert period_value(periods, "week", "cost", totals) == pytest.approx(0.0)
    assert period_value(periods, "day", "cost", totals) == pytest.approx(0.0)
    assert periods.previous["week"]["cost"] == pytest.approx(11.0)
    assert periods.previous["week"]["charger_energy_home"] == pytest.approx(10.0)


async def test_add_charge_before_month_only_changes_lifetime(hass, fcq, freezer):
    freezer.move_to("2026-11-25 20:00:00+00:00")
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "ac_loss_pct": 0})
    engine = engine_of(entry)
    await _call(hass, date="2026-10-30T10:00:00", energy_kwh=5, location="home")
    assert engine.state.cost == pytest.approx(5.0)
    assert period_value(engine.state.periods, "month", "cost", engine.totals()) == pytest.approx(0.0)


async def test_add_charge_rejects_future_date(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    when = (dt_util.now() + timedelta(days=1)).replace(tzinfo=None).isoformat()
    with pytest.raises(ServiceValidationError):
        await _call(hass, date=when, energy_kwh=5, location="home")


async def test_solar_window_runs_from_the_last_counter_change(hass, fcq, freezer):
    hass.states.async_set("sensor.pv", "2300", {"unit_of_measurement": "W"})
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0, "solar_power_entity": "sensor.pv"})
    await readings(fcq, (0.5,))
    freezer.tick(timedelta(minutes=20))
    # An update with the same counter value (for example a new SoC) does not restart the window.
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": 0.5, "stateOfCharge": 51, **AC})
    await readings(fcq, (2.5,), freezer=freezer, minutes=10)
    assert engine_of(entry).state.charger_energy_solar == pytest.approx(1.0)

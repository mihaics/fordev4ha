"""Tests for the engine core: storage, accounting, sessions, cost, periods, recovery."""

from datetime import timedelta

import pytest
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ford_ev.engine import signal_event
from custom_components.ford_ev.periods import period_value
from custom_components.ford_ev.state import ChargingSession, EngineState

from .conftest import async_setup_ford_ev


def capture(hass, entry_id):
    got = []

    @callback
    def _on(key, event_type, data):
        got.append((key, event_type, data))

    async_dispatcher_connect(hass, signal_event(entry_id), _on)
    return got


def engine_of(entry):
    return entry.runtime_data.engine


async def advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_soc_drop_counts_used(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("soc", "40")
    assert engine_of(entry).state.soc.energy_used == pytest.approx(6.0)


async def test_charger_energy_and_cost_at_home(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 0.30})
    for reading in (0.5, 2.0, 4.5):
        await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": reading})
    st = engine_of(entry).state
    assert st.charger_energy == pytest.approx(4.0)
    assert st.charger_energy_home == pytest.approx(4.0)
    assert st.cost == pytest.approx(1.20)


async def test_price_entity_takes_precedence(hass, fcq):
    hass.states.async_set("input_number.price", "0.5")
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 0.30, "price_entity": "input_number.price"})
    for reading in (0.5, 2.5):
        await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": reading})
    assert engine_of(entry).state.cost == pytest.approx(1.0)


async def test_charger_away_has_no_cost(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 0.30})
    await fcq.set("tracker", "not_home")
    for reading in (0.5, 2.0):
        await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": reading})
    st = engine_of(entry).state
    assert st.charger_energy == pytest.approx(1.5)
    assert st.charger_energy_home == 0
    assert st.cost == 0


async def _charge(fcq, end_soc, end_reading):
    await fcq.set("evccstatus", "C")
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": 0.5})
    await fcq.set("soc", str(end_soc))
    await fcq.set("elvehcharging", "CHARGING", {"chargerEnergyOutput": end_reading})


async def test_session_complete_event(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await _charge(fcq, 100, 30.5)
    assert engine_of(entry).state.session is not None
    assert got[0][:2] == ("charging", "session_started")
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 119)
    assert len(got) == 1
    await advance(hass, freezer, 2)
    key, event_type, data = got[-1]
    assert (key, event_type) == ("charging", "charging_complete")
    assert data["kwh"] == pytest.approx(30.0)
    assert data["end_soc"] == 100
    assert data["location"] == "home"
    assert "50% -> 100%" in data["summary"]
    st = engine_of(entry).state
    assert st.session is None and st.last_session["kwh"] == pytest.approx(30.0)


async def test_session_stopped_early(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await _charge(fcq, 80, 18.5)
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    assert got[-1][1] == "charging_stopped_early"


async def test_session_cost_in_summary(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 0.25})
    got = capture(hass, entry.entry_id)
    await _charge(fcq, 100, 30.5)
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    assert got[-1][2]["cost"] == pytest.approx(7.5)
    assert "7.50" in got[-1][2]["summary"]


async def test_session_end_cancelled_by_resume(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await _charge(fcq, 70, 12.5)
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 60)
    await fcq.set("evccstatus", "C")
    await advance(hass, freezer, 121)
    assert [e[1] for e in got] == ["session_started"]
    st = engine_of(entry).state
    assert st.session is not None and st.pending_session_end is None


async def test_session_fallback_to_soc_energy(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await fcq.set("evccstatus", "C")
    await fcq.set("soc", "60")
    await fcq.set("evccstatus", "A")
    await advance(hass, freezer, 121)
    st = engine_of(entry).state
    assert st.charger_energy == pytest.approx(6.0)
    assert st.model.cycle_charged == pytest.approx(6.0)
    assert got[-1][1] == "unplugged_while_charging"
    assert got[-1][2]["kwh"] == pytest.approx(6.0)


async def test_session_end_after_unavailable(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await _charge(fcq, 100, 30.5)
    await fcq.set("evccstatus", "unavailable")
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    assert got[-1][1] == "charging_complete"


def _stored(hass_storage, entry_id, state):
    key = f"ford_ev.{entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": state.to_dict()}


def _open_session():
    return ChargingSession(
        start_ts=dt_util.utcnow().timestamp() - 3600,
        start_soc=40.0,
        charger_at_start=0.0,
        added_at_start=0.0,
        cost_at_start=0.0,
        model_soc_at_start=None,
        home=True,
    )


async def test_restart_with_pending_session_end(hass, fcq, hass_storage):
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.session = _open_session()
    st.pending_session_end = {"ts": dt_util.utcnow().timestamp() - 30, "to": "B"}
    _stored(hass_storage, "entry_a", st)
    await fcq.set("evccstatus", "B")
    got = capture(hass, "entry_a")
    entry = await async_setup_ford_ev(hass, fcq, entry_id="entry_a")
    assert [e[1] for e in got] == ["charging_stopped_early"]
    assert engine_of(entry).state.session is None
    assert engine_of(entry).state.pending_session_end is None


async def test_restart_with_open_session_while_unplugged(hass, fcq, hass_storage):
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.session = _open_session()
    _stored(hass_storage, "entry_b", st)
    got = capture(hass, "entry_b")
    entry = await async_setup_ford_ev(hass, fcq, entry_id="entry_b")
    assert [e[1] for e in got] == ["unplugged_while_charging"]
    assert engine_of(entry).state.session is None


async def test_restart_while_still_charging_keeps_session(hass, fcq, hass_storage):
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.session = _open_session()
    _stored(hass_storage, "entry_c", st)
    await fcq.set("evccstatus", "C")
    got = capture(hass, "entry_c")
    entry = await async_setup_ford_ev(hass, fcq, entry_id="entry_c")
    assert got == []
    assert engine_of(entry).state.session is not None


async def test_state_persists_across_reload(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("soc", "40")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert engine_of(entry).state.soc.energy_used == pytest.approx(6.0)
    assert engine_of(entry).state.soc.last_soc == 40


async def test_two_entries_separate_storage(hass, fcq, fcq2):
    e1 = await async_setup_ford_ev(hass, fcq)
    e2 = await async_setup_ford_ev(hass, fcq2)
    await fcq.set("soc", "40")
    assert engine_of(e1).state.soc.energy_used == pytest.approx(6.0)
    assert engine_of(e2).state.soc.energy_used == 0


async def test_midnight_rolls_day(hass, fcq, freezer):
    freezer.move_to(dt_util.start_of_local_day() + timedelta(hours=23, minutes=50))
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("odometer", "1030")
    eng = engine_of(entry)
    assert period_value(eng.state.periods, "day", "odometer", eng.totals()) == 30
    await advance(hass, freezer, 10 * 60 + 6)
    assert eng.state.periods.previous["day"]["odometer"] == 30
    assert period_value(eng.state.periods, "day", "odometer", eng.totals()) == 0


async def test_unavailable_state_keeps_last_valid(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("ignitionstatus", "unavailable")
    assert engine_of(entry).last_valid("ignitionstatus") == "OFF"


async def test_settings_persist(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    engine_of(entry).set_setting("climate_setpoint", 22.0)
    engine_of(entry).set_setting("notify.night_activity", False)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    settings = engine_of(entry).state.settings
    assert settings.climate_setpoint == 22.0
    assert settings.notify["night_activity"] is False
    assert settings.notify["calibration"] is False

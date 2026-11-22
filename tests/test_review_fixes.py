"""Regression tests for restart ordering, the FCQ "Unsupported" transient, and period gaps."""

from datetime import timedelta

import pytest
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.ford_ev.engine import signal_event
from custom_components.ford_ev.periods import period_value
from custom_components.ford_ev.range_model import RangeModelState
from custom_components.ford_ev.state import EngineState

from .conftest import FCQ_KEYS, async_setup_ford_ev, default_states

PHONE = "mobile_app_test_phone"


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


def _store(hass_storage, entry_id, st):
    key = f"ford_ev.{entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": st.to_dict()}


def _remove_fcq_states(hass, fcq):
    for key in FCQ_KEYS:
        hass.states.async_remove(fcq.entity_id(key))


async def _fcq_loads(hass, fcq, overrides):
    states = {**default_states(), **overrides}
    for key, state in states.items():
        attrs = {"frontLeft": "UNLOCKED"} if key == "doorlock" and state == "UNLOCKED" else None
        await fcq.set(key, state, attrs)


async def test_fired_alert_not_repeated_when_fcq_loads_after_restart(hass, fcq, freezer, hass_storage):
    """Alert already fired before restart; car still unlocked; FCQ loads after ford_ev."""
    freezer.move_to(dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0))
    calls = async_mock_service(hass, "notify", PHONE)
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.alerts_armed = {"unlocked": False}
    _store(hass_storage, "e1", st)
    _remove_fcq_states(hass, fcq)
    await async_setup_ford_ev(hass, fcq, options={"notify_services": [PHONE]}, entry_id="e1")
    await _fcq_loads(hass, fcq, {"doorlock": "UNLOCKED"})
    await advance(hass, freezer, 11 * 60)
    assert [c.data["title"] for c in calls] == []  # expected: no repeat


async def test_pending_trip_accounted_when_fcq_loads_after_restart(hass, fcq, freezer, hass_storage):
    now = dt_util.utcnow().timestamp()
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.model = RangeModelState(model_energy=30.0, odo_accounted=1000.0, trip_start_ts=now - 1860, trip_start_temp=20.0)
    st.pending_trip_end_ts = now - 60
    _store(hass_storage, "e2", st)
    _remove_fcq_states(hass, fcq)
    entry = await async_setup_ford_ev(hass, fcq, entry_id="e2")
    await _fcq_loads(hass, fcq, {"odometer": "1030"})
    await advance(hass, freezer, 121)
    assert entry.runtime_data.engine.state.model.cycle_km == pytest.approx(30)  # expected: trip accounted


async def test_open_trip_closed_when_fcq_loads_after_restart(hass, fcq, freezer, hass_storage):
    now = dt_util.utcnow().timestamp()
    st = EngineState.new()
    st.soc.last_soc = 50.0
    st.model = RangeModelState(model_energy=30.0, odo_accounted=1000.0, trip_start_ts=now - 1860, trip_start_temp=20.0)
    _store(hass_storage, "e3", st)
    _remove_fcq_states(hass, fcq)
    entry = await async_setup_ford_ev(hass, fcq, entry_id="e3")
    await _fcq_loads(hass, fcq, {"odometer": "1030", "ignitionstatus": "OFF"})
    await advance(hass, freezer, 121)
    assert entry.runtime_data.engine.state.model.cycle_km == pytest.approx(30)  # expected: trip accounted


async def test_unsupported_between_charging_and_plugged(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await fcq.set("evccstatus", "C")
    await fcq.set("soc", "100")
    await fcq.set("evccstatus", "Unsupported")
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    assert "charging_complete" in [e[1] for e in got]  # expected


async def test_unsupported_between_ignition_on_and_off(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("ignitionstatus", "ON")
    await advance(hass, freezer, 1800)
    await fcq.set("odometer", "1030")
    await fcq.set("ignitionstatus", "Unsupported")
    await fcq.set("ignitionstatus", "OFF")
    await advance(hass, freezer, 121)
    assert entry.runtime_data.engine.state.model.cycle_km == pytest.approx(30)  # expected


async def test_odometer_unavailable_over_midnight_keeps_distance(hass, fcq, freezer):
    freezer.move_to(dt_util.now().replace(hour=22, minute=0, second=0, microsecond=0))
    entry = await async_setup_ford_ev(hass, fcq)
    engine = entry.runtime_data.engine
    await fcq.set("odometer", "1010")  # 10 km driven today before the outage
    await fcq.set("odometer", "unavailable")
    await advance(hass, freezer, 3 * 3600)  # past midnight
    await fcq.set("odometer", "1030")  # 20 km driven after midnight, reported when FCQ returns
    totals = engine.totals()
    assert engine.state.periods.previous.get("day", {}).get("odometer") == pytest.approx(10)  # expected
    assert period_value(engine.state.periods, "day", "odometer", totals) == pytest.approx(20)  # expected


async def test_fired_alert_persisted(hass, fcq, freezer, hass_storage):
    freezer.move_to(dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0))
    async_mock_service(hass, "notify", PHONE)
    entry = await async_setup_ford_ev(hass, fcq, options={"notify_services": [PHONE]}, entry_id="e7")
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 30)  # flush the delayed save from the doorlock change
    await advance(hass, freezer, 571)
    assert entry.runtime_data.engine.state.alerts_armed.get("unlocked") is False
    await advance(hass, freezer, 60)  # any delayed save has flushed by now
    stored = hass_storage["ford_ev.e7"]["data"]["alerts_armed"]
    assert stored.get("unlocked") is False  # expected: persisted


async def test_calibration_without_charger_counter_during_open_session(hass, fcq, freezer):
    """Vehicle without chargerEnergyOutput; car stays in C for a while at 100 %."""
    entry = await async_setup_ford_ev(hass, fcq)
    engine = entry.runtime_data.engine
    got = capture(hass, entry.entry_id)
    await fcq.set("evccstatus", "C")
    await fcq.set("soc", "100")
    await advance(hass, freezer, 301)  # still C (balancing)
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    types = [e[1] for e in got]
    assert "calibration" in types  # expected: this full charge calibrates


async def test_unsupported_ignition_keeps_last_valid(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    await fcq.set("ignitionstatus", "Unsupported")
    assert entry.runtime_data.engine.last_valid("ignitionstatus") == "OFF"


async def test_unsupported_doorlock_does_not_repeat_alert(hass, fcq, freezer):
    freezer.move_to(dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0))
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options={"notify_services": [PHONE]})
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 601)
    await fcq.set("doorlock", "Unsupported")
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 601)
    assert len(calls) == 1


async def test_calibration_after_session_ends_at_full(hass, fcq, freezer):
    """No charger counter; the car leaves C before the 5-minute calibration timer runs out."""
    entry = await async_setup_ford_ev(hass, fcq)
    got = capture(hass, entry.entry_id)
    await fcq.set("evccstatus", "C")
    await fcq.set("soc", "100")
    await advance(hass, freezer, 60)
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    await advance(hass, freezer, 300)
    types = [e[1] for e in got]
    assert types.count("calibration") == 1
    assert [e for e in got if e[1] == "calibration"][0][2]["charged_kwh"] == pytest.approx(30.0)


async def test_period_sensor_shows_previous_period(hass, fcq, freezer):
    from homeassistant.helpers import entity_registry as er

    from custom_components.ford_ev.const import DOMAIN

    from .conftest import VIN

    freezer.move_to(dt_util.start_of_local_day() + timedelta(hours=23, minutes=50))
    await async_setup_ford_ev(hass, fcq)
    await fcq.set("odometer", "1030")
    await advance(hass, freezer, 10 * 60 + 6)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{VIN}_distance_day")
    assert hass.states.get(entity_id).attributes["previous_period"] == pytest.approx(30)

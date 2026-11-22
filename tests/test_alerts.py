"""Tests for alerts, notifications and event entities."""

from datetime import timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from custom_components.ford_ev.const import DOMAIN

from .conftest import VIN, async_setup_ford_ev

PHONE = "mobile_app_test_phone"
OPTS = {"notify_services": [PHONE]}


async def advance(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def titles(calls):
    return [c.data["title"] for c in calls]


def at_local(hour, minute, second=0, weekday=None):
    """Next local datetime at the given time (and weekday, 0 = Monday)."""
    now = dt_util.now()
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if weekday is not None:
        target += timedelta(days=(weekday - target.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7 if weekday is not None else 1)
    return target


@pytest.fixture(autouse=True)
def _wednesday_noon(freezer):
    """Start every test outside the night window and away from the weekly summary."""
    freezer.move_to(at_local(12, 0, weekday=2))


async def test_unlocked_fires_once_after_delay(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 9 * 60)
    assert calls == []
    await advance(hass, freezer, 61)
    assert titles(calls) == ["Car unlocked"]
    assert "frontLeft" in calls[0].data["message"]
    assert "at home" in calls[0].data["message"]
    await advance(hass, freezer, 20 * 60)
    assert len(calls) == 1


async def test_unlocked_needs_ignition_off(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(12, 0))
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("ignitionstatus", "ON")
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 11 * 60)
    assert calls == []


async def test_unlocked_rearms_after_lock(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 601)
    await fcq.set("doorlock", "LOCKED")
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 601)
    assert titles(calls) == ["Car unlocked", "Car unlocked"]


async def test_armed_state_survives_restart(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    entry = await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 601)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await advance(hass, freezer, 1200)
    assert len(calls) == 1


async def test_open_lists_windows(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("windowposition", "Open", {"frontLeft": {"doubleRange": {"upperBound": 10}}, "rearLeft": {"doubleRange": {"upperBound": 0}}})
    await advance(hass, freezer, 601)
    assert titles(calls) == ["Car open"]
    assert "frontLeft" in calls[0].data["message"]
    assert "rearLeft" not in calls[0].data["message"]


async def test_open_lists_doors(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("doorstatus", "Open", {"rearRight": "AJAR"})
    await advance(hass, freezer, 601)
    assert "rearRight" in calls[0].data["message"]
    assert "frontLeft" not in calls[0].data["message"]


async def test_warning_light_on_increase_only(hass, fcq):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("indicators", "1", {"tirePressure": True, "oilLife": False})
    await fcq.set("indicators", "0")
    await fcq.set("indicators", "1", {"tirePressure": True})
    assert titles(calls) == ["Warning light", "Warning light"]
    assert "tirePressure" in calls[0].data["message"]
    assert "oilLife" not in calls[0].data["message"]


async def test_night_activity_window_wraps(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(23, 30))
    await async_setup_ford_ev(hass, fcq, options={**OPTS, "night_start": "22:00:00", "night_end": "06:00:00"})
    await fcq.set("ignitionstatus", "ON")
    assert titles(calls) == ["Night activity"]
    extra = calls[0].data["data"]
    assert extra["priority"] == "high" and extra["channel"] == "Car alert" and extra["ttl"] == 0


async def test_no_night_activity_outside_window(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(12, 0))
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("ignitionstatus", "ON")
    await fcq.set("tracker", "not_home")
    assert calls == []


async def test_night_activity_left_home(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(3, 0))
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("tracker", "not_home")
    assert calls[0].data["message"].startswith("Left home at ")


async def test_plug_in_reminder(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(20, 59, 50))
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("soc", "30")
    await advance(hass, freezer, 15)
    assert titles(calls) == ["Plug-in reminder"]
    assert "30%" in calls[0].data["message"]


async def test_no_plug_in_reminder_when_plugged(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(20, 59, 50))
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("soc", "30")
    await fcq.set("elvehplug", "CONNECTED")
    await advance(hass, freezer, 15)
    assert calls == []


async def test_weekly_summary_sunday(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(19, 59, 50, weekday=6))
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("odometer", "1100")
    await fcq.set("soc", "35")
    await advance(hass, freezer, 15)
    assert titles(calls) == ["Weekly summary"]
    message = calls[0].data["message"]
    assert "100 km" in message and "9.0 kWh used" in message and "9.0 kWh/100 km" in message


async def test_no_weekly_summary_saturday(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    freezer.move_to(at_local(19, 59, 50, weekday=5))
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await advance(hass, freezer, 15)
    assert calls == []


async def test_data_stale_unavailable(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("soc", "unavailable")
    await advance(hass, freezer, 2 * 3600 + 1)
    assert titles(calls) == ["No data from car"]
    assert "unavailable for 2 h" in calls[0].data["message"]


async def test_data_stale_no_update(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    await fcq.set("lastrefresh", (dt_util.utcnow() - timedelta(hours=49)).isoformat())
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await advance(hass, freezer, 601)
    assert titles(calls) == ["No data from car"]
    await advance(hass, freezer, 601)
    assert len(calls) == 1


async def test_switch_off_suppresses_notification_not_event(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    entry = await async_setup_ford_ev(hass, fcq, options=OPTS)
    entry.runtime_data.engine.set_setting("notify.unlocked", False)
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 601)
    assert calls == []
    event_id = er.async_get(hass).async_get_entity_id("event", DOMAIN, f"{VIN}_alert")
    state = hass.states.get(event_id)
    assert state.attributes["event_type"] == "unlocked"
    assert state.attributes["doors"] == ["frontLeft"]


async def test_no_targets_no_calls(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq)
    await fcq.set("indicators", "1")
    assert calls == []


async def test_missing_target_logged_and_others_sent(hass, fcq, caplog):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options={"notify_services": ["mobile_app_gone", PHONE]})
    await fcq.set("indicators", "1")
    assert len(calls) == 1
    assert "mobile_app_gone" in caplog.text


async def test_notify_entity_target(hass, fcq):
    calls = async_mock_service(hass, "notify", "send_message")
    await async_setup_ford_ev(hass, fcq, options={"notify_entities": ["notify.kitchen_display"]})
    await fcq.set("indicators", "1")
    assert calls[0].data["entity_id"] == ["notify.kitchen_display"]
    assert calls[0].data["title"] == "Warning light"


async def test_mobile_app_extras(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    entry = await async_setup_ford_ev(hass, fcq, options={**OPTS, "dashboard_path": "/ford-ev"})
    await fcq.set("doorlock", "UNLOCKED", {"frontLeft": "UNLOCKED"})
    await advance(hass, freezer, 601)
    extra = calls[0].data["data"]
    assert extra["tag"] == f"ford_ev-{entry.entry_id}-unlocked"
    assert extra["channel"] == "Car"
    assert extra["clickAction"] == "/ford-ev"
    assert "priority" not in extra


async def test_charging_complete_notification(hass, fcq, freezer):
    calls = async_mock_service(hass, "notify", PHONE)
    await async_setup_ford_ev(hass, fcq, options=OPTS)
    await fcq.set("evccstatus", "C")
    await fcq.set("soc", "100")
    await fcq.set("evccstatus", "B")
    await advance(hass, freezer, 121)
    assert titles(calls) == ["Charging complete"]
    assert calls[0].data["message"].startswith("100% (target 100%)")


async def test_calibration_notification_off_by_default(hass, fcq):
    calls = async_mock_service(hass, "notify", PHONE)
    entry = await async_setup_ford_ev(hass, fcq, options=OPTS)
    entry.runtime_data.engine.emit("report", "calibration", {"summary": "x"})
    await hass.async_block_till_done()
    assert calls == []
    entry.runtime_data.engine.set_setting("notify.calibration", True)
    entry.runtime_data.engine.emit("report", "calibration", {"summary": "300 km"})
    await hass.async_block_till_done()
    assert titles(calls) == ["Range calibrated"]


def test_notification_texts_are_short():
    from custom_components.ford_ev.alerts import build_notification

    cases = {
        "unlocked": {"location": "home", "doors": ["frontLeft"], "delay_min": 10},
        "open": {"doors": [], "windows": ["rearLeft"], "window_open": True, "delay_min": 10},
        "plug_in_reminder": {"soc": 30.0, "range_km": 120.0},
        "warning_light": {"count": 1, "indicators": []},
        "data_stale": {"reason": "unavailable", "hours": 2, "last_update": None},
        "night_activity": {"reason": "ignition", "state": "ON", "time": "02:13"},
    }
    expected = {
        "unlocked": ("Car unlocked", "Unlocked for 10 min, at home. Doors: frontLeft."),
        "open": ("Car open", "Open for 10 min. Windows: rearLeft."),
        "plug_in_reminder": ("Plug-in reminder", "Battery 30%, 120 km, not plugged in."),
        "warning_light": ("Warning light", "1 active."),
        "data_stale": ("No data from car", "FordConnect Query unavailable for 2 h."),
        "night_activity": ("Night activity", "Ignition ON at 02:13."),
    }
    for event_type, data in cases.items():
        assert build_notification(event_type, data, "EUR") == expected[event_type]

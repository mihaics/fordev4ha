"""Alert conditions and the notifier that turns engine events into notifications."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ALERT_DELAY_MIN,
    CONF_DASHBOARD_PATH,
    CONF_NIGHT_END,
    CONF_NIGHT_START,
    CONF_NOTIFY_ENTITIES,
    CONF_NOTIFY_SERVICES,
    CONF_REMINDER_TIME,
    CONF_STALE_NO_UPDATE_H,
    CONF_STALE_UNAVAILABLE_H,
    CONF_WEEKLY_DAY,
    CONF_WEEKLY_TIME,
    DOMAIN,
    WEEKDAYS,
)
from .engine import FordEvEngine, signal_event
from .notify import async_send
from .periods import period_value
from .units import state_km, state_number

EVENT_TYPES: dict[str, list[str]] = {
    "alert": ["unlocked", "open", "warning_light", "data_stale", "night_activity", "plug_in_reminder"],
    "charging": ["session_started", "charging_complete", "charging_stopped_early", "unplugged_while_charging"],
    "report": ["weekly_summary", "calibration"],
}
NOTIFY_FOR: dict[str, str] = {
    "charging_complete": "charging",
    "charging_stopped_early": "charging",
    "unlocked": "unlocked",
    "open": "open",
    "plug_in_reminder": "plug_in_reminder",
    "warning_light": "warning_light",
    "data_stale": "data_stale",
    "night_activity": "night_activity",
    "weekly_summary": "weekly_summary",
    "calibration": "calibration",
}
STALE_CHECK_INTERVAL = timedelta(minutes=10)
_IGNORED_ATTRS = {"friendly_name", "icon", "unit_of_measurement", "device_class"}
_CLOSED = {"CLOSED", "INVALID", "UNKNOWN", "LOCKED"}
_FALSY = (False, None, 0, "0", "false", "False", "OFF", "off")


class DelayedCondition:
    """Fires once when a condition has held for a delay; re-arms when it clears.

    The armed flag lives in a dict owned by the engine, so it survives restarts.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        delay_s: float,
        predicate: Callable[[], bool],
        on_fire: Callable[[], None],
        armed: dict[str, bool],
    ) -> None:
        self.hass = hass
        self.name = name
        self.delay_s = delay_s
        self.predicate = predicate
        self.on_fire = on_fire
        self.armed = armed
        self._timer: CALLBACK_TYPE | None = None

    @callback
    def evaluate(self) -> None:
        """Re-check the condition after an input changed."""
        if self.predicate():
            if self.armed.get(self.name, True) and self._timer is None:
                self._timer = async_call_later(self.hass, self.delay_s, self._expired)
        else:
            self.cancel()
            self.armed[self.name] = True

    @callback
    def cancel(self) -> None:
        """Stop a running timer."""
        if self._timer is not None:
            self._timer()
            self._timer = None

    @callback
    def _expired(self, _now: datetime) -> None:
        self._timer = None
        if self.predicate() and self.armed.get(self.name, True):
            self.armed[self.name] = False
            self.on_fire()


def _parse_time(value: str) -> time:
    parsed = dt_util.parse_time(value)
    if parsed is None:
        raise ValueError(f"invalid time {value}")
    return parsed


def in_window(now: time, start: time, end: time) -> bool:
    """now is inside [start, end), where the window may wrap past midnight."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _attrs(state: State | None) -> dict[str, Any]:
    if state is None:
        return {}
    return {k: v for k, v in state.attributes.items() if k not in _IGNORED_ATTRS}


class AlertManager:
    """Watches alert conditions for one vehicle, raises events, and sends notifications."""

    def __init__(self, hass: HomeAssistant, engine: FordEvEngine, config: dict[str, Any]) -> None:
        self.hass = hass
        self.engine = engine
        self.config = config
        self._unsubs: list[CALLBACK_TYPE] = []
        armed = engine.state.alerts_armed
        self.delay_min = int(config[CONF_ALERT_DELAY_MIN])
        self.unavailable_h = int(config[CONF_STALE_UNAVAILABLE_H])
        self.no_update_h = int(config[CONF_STALE_NO_UPDATE_H])
        self._conditions = [
            DelayedCondition(hass, "unlocked", self.delay_min * 60, self._is_unlocked, self._fire_unlocked, armed),
            DelayedCondition(hass, "open", self.delay_min * 60, self._is_open, self._fire_open, armed),
            DelayedCondition(
                hass, "data_stale_unavailable", self.unavailable_h * 3600, self._soc_unavailable, self._fire_unavailable, armed
            ),
        ]

    async def async_start(self) -> None:
        """Subscribe to events and inputs. Conditions are evaluated by async_evaluate."""
        self._unsubs.append(
            async_dispatcher_connect(self.hass, signal_event(self.engine.entry.entry_id), self._on_event)
        )
        self._unsubs.append(self.engine.async_add_fcq_listener(self._on_fcq_change))
        reminder = _parse_time(self.config[CONF_REMINDER_TIME])
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_reminder_time, hour=reminder.hour, minute=reminder.minute, second=reminder.second
            )
        )
        weekly = _parse_time(self.config[CONF_WEEKLY_TIME])
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._on_weekly_time, hour=weekly.hour, minute=weekly.minute, second=weekly.second
            )
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._check_no_update, STALE_CHECK_INTERVAL))

    @callback
    def async_evaluate(self) -> None:
        """Evaluate every condition against the current state."""
        for condition in self._conditions:
            condition.evaluate()
        self.engine.save()

    async def async_stop(self) -> None:
        """Unsubscribe and stop timers."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for condition in self._conditions:
            condition.cancel()

    # ------------------------------------------------------------------ conditions

    def _ignition_off(self) -> bool:
        return self.engine.last_valid("ignitionstatus") == "OFF"

    def _is_unlocked(self) -> bool:
        return self._ignition_off() and self.engine.last_valid("doorlock") == "UNLOCKED"

    def _is_open(self) -> bool:
        return self._ignition_off() and (
            self.engine.last_valid("doorstatus") == "Open" or self.engine.last_valid("windowposition") == "Open"
        )

    def _soc_unavailable(self) -> bool:
        state = self.engine.fcq_state("soc")
        return state is not None and state.state == "unavailable"

    @callback
    def _on_fcq_change(self, key: str, prev: str | None, new_state: State) -> None:
        for condition in self._conditions:
            condition.evaluate()
        if key == "indicators":
            self._check_indicators(prev, new_state)
        elif key in ("ignitionstatus", "tracker"):
            self._check_night(key, prev, new_state)
        self.engine.save()

    def _check_indicators(self, prev: str | None, new_state: State) -> None:
        new = state_number(new_state)
        old = state_number(State(new_state.entity_id, prev)) if prev is not None else None
        if new is None or old is None or new <= old:
            return
        active = [k for k, v in _attrs(new_state).items() if v not in _FALSY]
        self.engine.emit("alert", "warning_light", {"count": int(new), "indicators": active})

    def _check_night(self, key: str, prev: str | None, new_state: State) -> None:
        new = new_state.state
        if new in ("unknown", "unavailable"):
            return
        start = _parse_time(self.config[CONF_NIGHT_START])
        end = _parse_time(self.config[CONF_NIGHT_END])
        now = dt_util.now()
        if not in_window(now.time(), start, end):
            return
        if key == "ignitionstatus" and prev == "OFF" and new not in ("OFF", "Unsupported"):
            reason = "ignition"
        elif key == "tracker" and prev == "home" and new != "home":
            reason = "left_home"
        else:
            return
        self.engine.emit("alert", "night_activity", {"reason": reason, "state": new, "time": now.strftime("%H:%M")})

    # ------------------------------------------------------------------ alert events

    def _fire_unlocked(self) -> None:
        doors = [k for k, v in _attrs(self.engine.fcq_state("doorlock")).items() if v == "UNLOCKED"]
        location = "home" if self.engine.is_home() else "away"
        self.engine.emit("alert", "unlocked", {"location": location, "doors": doors, "delay_min": self.delay_min})

    def _fire_open(self) -> None:
        doors = [
            k
            for k, v in _attrs(self.engine.fcq_state("doorstatus")).items()
            if isinstance(v, str) and v.upper() not in _CLOSED
        ]
        windows = [
            k
            for k, v in _attrs(self.engine.fcq_state("windowposition")).items()
            if isinstance(v, dict) and _float((v.get("doubleRange") or {}).get("upperBound")) > 0
        ]
        data = {
            "doors": doors,
            "windows": windows,
            "window_open": self.engine.last_valid("windowposition") == "Open",
            "delay_min": self.delay_min,
        }
        self.engine.emit("alert", "open", data)

    def _fire_unavailable(self) -> None:
        self.engine.emit("alert", "data_stale", {"reason": "unavailable", "hours": self.unavailable_h, "last_update": None})

    @callback
    def _check_no_update(self, _now: datetime) -> None:
        state = self.engine.fcq_state("lastrefresh")
        when = dt_util.parse_datetime(state.state) if state is not None else None
        if when is None:
            return
        armed = self.engine.state.alerts_armed
        stale = dt_util.utcnow() - when > timedelta(hours=self.no_update_h)
        if stale and armed.get("data_stale_no_update", True):
            armed["data_stale_no_update"] = False
            self.engine.emit(
                "alert",
                "data_stale",
                {"reason": "no_update", "hours": self.no_update_h, "last_update": dt_util.as_local(when).isoformat()},
            )
            self.engine.save()
        elif not stale and not armed.get("data_stale_no_update", True):
            armed["data_stale_no_update"] = True
            self.engine.save()

    @callback
    def _on_reminder_time(self, _now: datetime) -> None:
        engine = self.engine
        soc = state_number(engine.fcq_state("soc"))
        if not engine.is_home() or soc is None or engine.is_plugged():
            return
        if soc >= engine.state.settings.plug_in_reminder_soc:
            return
        engine.emit("alert", "plug_in_reminder", {"soc": soc, "range_km": state_km(engine.fcq_state("elveh"))})

    @callback
    def _on_weekly_time(self, now: datetime) -> None:
        if dt_util.as_local(now).weekday() != WEEKDAYS.index(self.config[CONF_WEEKLY_DAY]):
            return
        engine = self.engine
        totals = engine.totals()
        periods = engine.state.periods

        def week(counter: str) -> float:
            return period_value(periods, "week", counter, totals) or 0.0

        km = week("odometer")
        used = week("energy_used")
        engine.emit(
            "report",
            "weekly_summary",
            {
                "km": km,
                "energy_used": used,
                "consumption": used / km * 100 if km >= 10 else None,
                "charger_energy": week("charger_energy"),
                "charger_energy_home": week("charger_energy_home"),
                "cost": week("cost"),
                "soc": state_number(engine.fcq_state("soc")),
            },
        )

    # ------------------------------------------------------------------ notifier

    @callback
    def _on_event(self, _entity_key: str, event_type: str, data: dict[str, Any]) -> None:
        notify_key = NOTIFY_FOR.get(event_type)
        if notify_key is None or not self.engine.state.settings.notify.get(notify_key, False):
            return
        services = list(self.config.get(CONF_NOTIFY_SERVICES) or [])
        entities = list(self.config.get(CONF_NOTIFY_ENTITIES) or [])
        if not services and not entities:
            return
        title, message = build_notification(event_type, data, self.hass.config.currency)
        night = event_type == "night_activity"
        self.engine.entry.async_create_task(
            self.hass,
            async_send(
                self.hass,
                services,
                entities,
                title,
                message,
                tag=f"{DOMAIN}-{self.engine.entry.entry_id}-{event_type}",
                channel="Car alert" if night else "Car",
                high_priority=night,
                click_action=self.config.get(CONF_DASHBOARD_PATH) or None,
            ),
            f"{DOMAIN} notification",
        )


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _num(value: Any, fmt: str = ".0f") -> str:
    return format(value, fmt) if isinstance(value, int | float) else "?"


def build_notification(event_type: str, data: dict[str, Any], currency: str) -> tuple[str, str]:
    """Title and message for an event."""
    if event_type in ("charging_complete", "charging_stopped_early"):
        title = "Charging complete" if event_type == "charging_complete" else "Charging stopped early"
        return title, (
            f"{_num(data.get('end_soc'))}% (target {_num(data.get('target'))}%), "
            f"range {_num(data.get('range_km'))} km. {data.get('summary', '')}"
        )
    if event_type == "unlocked":
        where = "at home" if data.get("location") == "home" else "away from home"
        message = f"Parked {where} and unlocked for {data.get('delay_min')} min."
        if data.get("doors"):
            message += f" Unlocked: {', '.join(data['doors'])}."
        return "Car unlocked", message
    if event_type == "open":
        message = f"Open for {data.get('delay_min')} min."
        if data.get("doors"):
            message += f" Doors: {', '.join(data['doors'])}."
        if data.get("windows"):
            message += f" Windows: {', '.join(data['windows'])}."
        elif data.get("window_open"):
            message += " Windows: open."
        return "Car open while parked", message
    if event_type == "plug_in_reminder":
        return "Plug in the car", (
            f"Battery {_num(data.get('soc'))}%, range {_num(data.get('range_km'))} km, not plugged in."
        )
    if event_type == "warning_light":
        details = ", ".join(data.get("indicators") or []) or "Check the FordPass app for details."
        return "Car warning light", f"{data.get('count')} active. {details}"
    if event_type == "data_stale":
        hours = data.get("hours")
        if data.get("reason") == "unavailable":
            return "Car data stopped", (
                f"Ford integration unavailable for {hours} h - likely an API or login problem "
                "(check the FordConnect Query integration)."
            )
        last = dt_util.parse_datetime(str(data.get("last_update") or ""))
        last_txt = last.strftime("%d.%m %H:%M") if last else "unknown"
        return "Car data stopped", (
            f"No update from the car for {hours} h (last {last_txt}). "
            "Car asleep for a long time, or the integration has stopped."
        )
    if event_type == "night_activity":
        if data.get("reason") == "ignition":
            return "Car activity at night", f"Ignition turned {data.get('state')} at {data.get('time')}."
        return "Car activity at night", f"Car left home at {data.get('time')}."
    if event_type == "weekly_summary":
        message = f"{_num(data.get('km'))} km, {_num(data.get('energy_used'), '.1f')} kWh used"
        if data.get("consumption") is not None:
            message += f" ({_num(data['consumption'], '.1f')} kWh/100 km)"
        message += (
            f". Charged {_num(data.get('charger_energy'), '.1f')} kWh "
            f"({_num(data.get('charger_energy_home'), '.1f')} kWh at home, {_num(data.get('cost'), '.2f')} {currency}). "
            f"Battery now {_num(data.get('soc'))}%."
        )
        return "Car - this week", message
    if event_type == "calibration":
        return "Range model calibrated", str(data.get("summary", ""))
    return "Ford EV", str(data)

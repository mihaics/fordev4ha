"""Per-vehicle engine: follows the FCQ entities, keeps the totals, sessions and range model.

All arithmetic lives in the pure modules (accounting, periods, range_model); this module wires
them to Home Assistant state changes and timers, persists the result, and signals entities.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .accounting import apply_soc_change, charger_increment
from .const import (
    CONF_BATTERY_KWH,
    CONF_PRICE_ENTITY,
    CONF_PRICE_FIXED,
    DOMAIN,
    merged_config,
)
from .discovery import FcqVehicle
from .periods import roll_periods
from .range_model import add_charge, model_soc
from .state import STORAGE_VERSION, ChargingSession, EngineState
from .units import state_km, state_number

_LOGGER = logging.getLogger(__name__)

SESSION_END_DELAY_S = 120
SAVE_DELAY_S = 10
_INVALID = (STATE_UNKNOWN, STATE_UNAVAILABLE)


def signal_update(entry_id: str) -> str:
    """Dispatcher signal sent whenever engine state or an input changed."""
    return f"{DOMAIN}_{entry_id}_update"


def signal_event(entry_id: str) -> str:
    """Dispatcher signal for events: (entity_key, event_type, data)."""
    return f"{DOMAIN}_{entry_id}_event"


def _valid(state: State | None) -> bool:
    return state is not None and state.state not in _INVALID


class FordEvEngine:
    """Engine for one configured vehicle."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, vehicle: FcqVehicle) -> None:
        self.hass = hass
        self.entry = entry
        self.vehicle = vehicle
        self.config = merged_config(entry)
        self.state = EngineState.new()
        self.first_start = False
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._key_of = {entity_id: key for key, entity_id in vehicle.entities.items()}
        self._last_valid: dict[str, str] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._session_end_timer: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------ lifecycle

    @property
    def capacity_kwh(self) -> float:
        """Usable battery capacity."""
        return float(self.config[CONF_BATTERY_KWH])

    async def async_start(self) -> None:
        """Load state, reconcile what happened while stopped, start listening."""
        data = await self._store.async_load()
        if data is None:
            self.first_start = True
            self.state = EngineState.new()
        else:
            self.state = EngineState.from_dict(data)

        for key, entity_id in self.vehicle.entities.items():
            st = self.hass.states.get(entity_id)
            if _valid(st):
                self._last_valid[key] = st.state  # type: ignore[union-attr]
        self._init_baselines()
        self._recover_session()

        self._unsubs.append(
            async_track_state_change_event(self.hass, list(self.vehicle.entities.values()), self._on_fcq_change)
        )
        self._unsubs.append(async_track_time_change(self.hass, self._on_midnight, hour=0, minute=0, second=5))
        self._roll()
        self.save()

    async def async_stop(self) -> None:
        """Stop listening and save."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_session_end_timer()
        await self._store.async_save(self.state.to_dict())

    def save(self) -> None:
        """Save soon."""
        self._store.async_delay_save(self.state.to_dict, SAVE_DELAY_S)

    # ------------------------------------------------------------------ helpers

    def fcq_state(self, key: str) -> State | None:
        """Current state of an FCQ key, if the vehicle has it."""
        entity_id = self.vehicle.entities.get(key)
        return self.hass.states.get(entity_id) if entity_id else None

    def last_valid(self, key: str) -> str | None:
        """Last state of an FCQ key that was not unknown or unavailable."""
        return self._last_valid.get(key)

    def is_home(self) -> bool:
        """The car is in the home zone."""
        return self._last_valid.get("tracker") == "home"

    def is_plugged(self) -> bool:
        """The charging cable is connected."""
        return self._last_valid.get("evccstatus") in ("B", "C") or self._last_valid.get("elvehplug") == "CONNECTED"

    def current_soc(self) -> float | None:
        """Car SoC now, or the last known one."""
        soc = state_number(self.fcq_state("soc"))
        return soc if soc is not None else self.state.soc.last_soc

    def price(self) -> float | None:
        """Electricity price per kWh: the price entity, else the fixed price, else None."""
        entity_id = self.config.get(CONF_PRICE_ENTITY)
        if entity_id:
            value = state_number(self.hass.states.get(entity_id))
            if value is not None:
                return value
        fixed = self.config.get(CONF_PRICE_FIXED)
        return float(fixed) if fixed is not None else None

    def totals(self) -> dict[str, float]:
        """Cumulative counters used for report periods."""
        st = self.state
        totals = {
            "energy_used": st.soc.energy_used,
            "energy_added": st.soc.energy_added,
            "charger_energy": st.charger_energy,
            "charger_energy_home": st.charger_energy_home,
            "cost": st.cost,
        }
        odometer = state_km(self.fcq_state("odometer"))
        if odometer is not None:
            totals["odometer"] = odometer
        return totals

    def set_setting(self, name: str, value: Any) -> None:
        """Change a setting; `notify.<key>` switches one notification type."""
        settings = self.state.settings
        if name.startswith("notify."):
            settings.notify[name.removeprefix("notify.")] = bool(value)
        else:
            setattr(settings, name, value)
        self.notify_listeners()
        self.save()

    def emit(self, entity_key: str, event_type: str, data: dict[str, Any]) -> None:
        """Fire an event for the event entities and the notifier."""
        async_dispatcher_send(self.hass, signal_event(self.entry.entry_id), entity_key, event_type, data)

    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> CALLBACK_TYPE:
        """Call cb whenever engine state or an input changes."""
        return async_dispatcher_connect(self.hass, signal_update(self.entry.entry_id), cb)

    def notify_listeners(self) -> None:
        """Tell entities to refresh."""
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    def _roll(self) -> None:
        self.state.periods = roll_periods(self.state.periods, dt_util.now(), self.totals())

    def _init_baselines(self) -> None:
        if self.state.soc.last_soc is None:
            soc = state_number(self.fcq_state("soc"))
            if soc is not None:
                self.state.soc.last_soc = soc
        if self.state.charger_last_reading is None:
            reading = self._charger_reading(self.fcq_state("elvehcharging"))
            if reading is not None:
                self.state.charger_last_reading = reading

    @staticmethod
    def _charger_reading(state: State | None) -> float | None:
        if not _valid(state):
            return None
        value = state.attributes.get("chargerEnergyOutput")  # type: ignore[union-attr]
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    # ------------------------------------------------------------------ state changes

    @callback
    def _on_fcq_change(self, event: Event[EventStateChangedData]) -> None:
        key = self._key_of.get(event.data["entity_id"])
        new_state = event.data["new_state"]
        if key is None or new_state is None:
            return
        prev = self._last_valid.get(key)
        if _valid(new_state):
            self._last_valid[key] = new_state.state
        self.handle_fcq_change(key, prev, new_state)
        self.notify_listeners()

    def handle_fcq_change(self, key: str, prev: str | None, new_state: State) -> None:
        """React to one FCQ state change. prev is the last valid state before it."""
        if key == "soc":
            soc = state_number(new_state)
            if soc is not None:
                self._on_soc(soc)
        elif key == "elvehcharging":
            reading = self._charger_reading(new_state)
            if reading is not None:
                self._on_charger(reading)
        elif key == "evccstatus" and _valid(new_state):
            self._on_evcc(prev, new_state.state)

    def _on_soc(self, soc: float) -> None:
        self._roll()
        self.state.soc = apply_soc_change(self.state.soc, soc, self.capacity_kwh, self.is_plugged(), self.is_home())
        self.save()

    def _on_charger(self, reading: float) -> None:
        increment = charger_increment(self.state.charger_last_reading, reading)
        self.state.charger_last_reading = reading
        if increment > 0:
            self._add_charger_energy(increment)
        self.save()

    def _add_charger_energy(self, kwh: float) -> None:
        self._roll()
        st = self.state
        st.charger_energy += kwh
        if self.is_home():
            st.charger_energy_home += kwh
            st.cost += kwh * (self.price() or 0.0)
        st.model = add_charge(st.model, kwh, self.capacity_kwh)

    # ------------------------------------------------------------------ charging sessions

    def _on_evcc(self, prev: str | None, new: str) -> None:
        st = self.state
        if prev is None:
            # First valid value since start: reconcile a session left open while stopped.
            self._recover_session()
        if new == "C":
            if st.pending_session_end is not None:
                self._cancel_session_end_timer()
                st.pending_session_end = None
            elif st.session is None:
                self._start_session()
            self.save()
            return
        if st.session is None or st.pending_session_end is not None:
            return
        if (prev == "C" and new in ("A", "B")) or new == "A":
            st.pending_session_end = {"ts": dt_util.utcnow().timestamp(), "to": new}
            self._session_end_timer = async_call_later(self.hass, SESSION_END_DELAY_S, self._on_session_end_timer)
            self.save()

    def _start_session(self) -> None:
        st = self.state
        st.session = ChargingSession(
            start_ts=dt_util.utcnow().timestamp(),
            start_soc=self.current_soc(),
            charger_at_start=st.charger_energy,
            added_at_start=st.soc.energy_added,
            cost_at_start=st.cost,
            model_soc_at_start=model_soc(st.model, self.capacity_kwh),
            home=self.is_home(),
        )
        self.emit("charging", "session_started", {"start_soc": st.session.start_soc, "location": self._location()})

    def _location(self) -> str:
        return "home" if self.is_home() else "away"

    def _cancel_session_end_timer(self) -> None:
        if self._session_end_timer is not None:
            self._session_end_timer()
            self._session_end_timer = None

    @callback
    def _on_session_end_timer(self, _now: datetime) -> None:
        self._session_end_timer = None
        pending = self.state.pending_session_end
        if pending is not None:
            self._finalise_session(pending["to"], pending["ts"])

    def _recover_session(self) -> None:
        st = self.state
        evcc = self._last_valid.get("evccstatus")
        if evcc is None:
            return
        if st.pending_session_end is not None and self._session_end_timer is None:
            if evcc == "C":
                st.pending_session_end = None
            else:
                self._finalise_session(st.pending_session_end["to"], st.pending_session_end["ts"])
        elif st.session is not None and st.pending_session_end is None and evcc != "C":
            self._finalise_session(evcc, dt_util.utcnow().timestamp())

    def _finalise_session(self, to: str, end_ts: float) -> None:
        st = self.state
        session = st.session
        st.pending_session_end = None
        if session is None:
            return
        end_soc = self.current_soc()
        charger_delta = st.charger_energy - session.charger_at_start
        added_delta = st.soc.energy_added - session.added_at_start
        if charger_delta <= 0 and added_delta > 0:
            self._add_charger_energy(added_delta)
            kwh = added_delta
        else:
            kwh = max(charger_delta, 0.0)
        cost = st.cost - session.cost_at_start
        price = self.price()
        target = st.settings.charge_target

        if to == "B":
            complete = end_soc is not None and end_soc >= target - 1
            event_type = "charging_complete" if complete else "charging_stopped_early"
        else:
            event_type = "unplugged_while_charging"

        start = dt_util.as_local(dt_util.utc_from_timestamp(session.start_ts))
        end = dt_util.as_local(dt_util.utc_from_timestamp(end_ts))
        duration_min = max(int((end_ts - session.start_ts) // 60), 0)
        location = "home" if session.home else "away"
        start_soc_txt = f"{session.start_soc:.0f}" if session.start_soc is not None else "?"
        end_soc_txt = f"{end_soc:.0f}" if end_soc is not None else "?"
        summary = (
            f"{start:%d.%m %H:%M}-{end:%H:%M} ({duration_min // 60}h{duration_min % 60:02d}), "
            f"{start_soc_txt}% -> {end_soc_txt}%, +{kwh:.1f} kWh, {location}"
        )
        if price is not None and session.home:
            summary += f", {cost:.2f} {self.hass.config.currency}"

        data = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration_min": duration_min,
            "start_soc": session.start_soc,
            "end_soc": end_soc,
            "kwh": round(kwh, 3),
            "cost": round(cost, 2),
            "location": location,
            "target": target,
            "range_km": state_km(self.fcq_state("elveh")),
            "model_soc_at_start": session.model_soc_at_start,
            "summary": summary,
        }
        st.last_session = data
        st.session = None
        self.emit("charging", event_type, data)
        self.notify_listeners()
        self.save()

    # ------------------------------------------------------------------ timers

    @callback
    def _on_midnight(self, _now: datetime) -> None:
        self._roll()
        self.notify_listeners()
        self.save()


@dataclass
class FordEvRuntime:
    """Objects owned by one config entry."""

    engine: FordEvEngine
    alerts: Any = None


type FordEvConfigEntry = ConfigEntry[FordEvRuntime]

"""Per-vehicle engine: follows the FCQ entities, keeps the totals, sessions and range model.

All arithmetic lives in the pure modules (accounting, periods, range_model); this module wires
them to Home Assistant state changes and timers, persists the result, and signals entities.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
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
    CONF_TEMPERATURE_SENSOR,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    merged_config,
)
from .discovery import FcqVehicle
from .periods import roll_periods
from .range_model import (
    FALLBACK_TEMP_C,
    SOC_FULL,
    RangeModelState,
    TripInput,
    account_trip,
    add_charge,
    calibrate,
    calibration_allowed,
    model_soc,
    seed_drive_factor,
)
from .state import STORAGE_VERSION, ChargingSession, EngineState
from .units import state_celsius, state_km, state_number, to_celsius, weather_celsius

_LOGGER = logging.getLogger(__name__)

SESSION_END_DELAY_S = 120
TRIP_END_DELAY_S = 120
CALIBRATION_DELAY_S = 300
LAST_SESSION_MAX_AGE = timedelta(hours=1)
SAVE_DELAY_S = 10
# "Unsupported" is a transient FCQ value; it must not overwrite the last real value.
_INVALID = (STATE_UNKNOWN, STATE_UNAVAILABLE, "Unsupported")


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
        self._trip_end_timer: CALLBACK_TYPE | None = None
        self._calibration_timer: CALLBACK_TYPE | None = None
        self.forecast_tomorrow_c: float | None = None
        self._loaded = False
        self._fcq_listeners: list[Callable[[str, str | None, State], None]] = []

    # ------------------------------------------------------------------ lifecycle

    @property
    def capacity_kwh(self) -> float:
        """Usable battery capacity."""
        return float(self.config[CONF_BATTERY_KWH])

    async def async_load(self) -> None:
        """Load persisted state. Entities read it, so this runs before they are added."""
        data = await self._store.async_load()
        if data is None:
            self.first_start = True
            self.state = EngineState.new()
        else:
            self.state = EngineState.from_dict(data)
        self._loaded = True

    async def async_start(self) -> None:
        """Reconcile what happened while stopped, then start listening."""
        if not self._loaded:
            await self.async_load()

        for key, entity_id in self.vehicle.entities.items():
            st = self.hass.states.get(entity_id)
            if _valid(st):
                self._last_valid[key] = st.state  # type: ignore[union-attr]
        self._init_baselines()
        if self.first_start:
            self._seed_model()
        self._recover_session()
        self._recover_trip()

        self._unsubs.append(
            async_track_state_change_event(self.hass, list(self.vehicle.entities.values()), self._on_fcq_change)
        )
        temperature_entities = [e for e in (self.config.get(CONF_TEMPERATURE_SENSOR), self.config.get(CONF_WEATHER_ENTITY)) if e]
        if temperature_entities:
            self._unsubs.append(
                async_track_state_change_event(self.hass, temperature_entities, self._on_temperature_change)
            )
        self._unsubs.append(async_track_time_change(self.hass, self._on_midnight, hour=0, minute=0, second=5))
        self._unsubs.append(async_track_time_change(self.hass, self._on_forecast_time, minute=7, second=0))
        soc = state_number(self.fcq_state("soc"))
        if soc is not None and soc >= SOC_FULL:
            self._start_calibration_timer()
        self._roll()
        odometer = state_km(self.fcq_state("odometer"))
        if odometer is not None:
            self.state.last_odometer_km = odometer
            self._roll()
        self.save()
        self.notify_listeners()
        await self.async_refresh_forecast()

    async def async_stop(self) -> None:
        """Stop listening and save."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_session_end_timer()
        for timer in (self._trip_end_timer, self._calibration_timer):
            if timer is not None:
                timer()
        self._trip_end_timer = self._calibration_timer = None
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
        if st.last_odometer_km is not None:
            totals["odometer"] = st.last_odometer_km
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
        for listener in list(self._fcq_listeners):
            listener(key, prev, new_state)
        self.notify_listeners()

    @callback
    def async_add_fcq_listener(self, cb: Callable[[str, str | None, State], None]) -> CALLBACK_TYPE:
        """Call cb(key, last valid state before the change, new state) after each FCQ change."""
        self._fcq_listeners.append(cb)

        def remove() -> None:
            self._fcq_listeners.remove(cb)

        return remove

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
        elif key == "ignitionstatus" and _valid(new_state):
            self._on_ignition(prev, new_state)
        elif key == "odometer":
            odometer = state_km(new_state)
            if odometer is not None:
                self._on_odometer(odometer)

    def _on_odometer(self, odometer_km: float) -> None:
        # Roll first, so distance driven before a missed period boundary stays in that period.
        self._roll()
        self.state.last_odometer_km = odometer_km
        if self.state.pending_trip_end_ts is not None and self._trip_end_timer is None:
            # A trip was waiting for the odometer (FCQ not loaded yet after a restart).
            self._account_trip(self.state.pending_trip_end_ts)
        self.save()

    def _on_soc(self, soc: float) -> None:
        self._roll()
        self.state.soc = apply_soc_change(self.state.soc, soc, self.capacity_kwh, self.is_plugged(), self.is_home())
        if self.state.model.model_energy is None:
            self.state.model = replace(self.state.model, model_energy=soc / 100 * self.capacity_kwh)
        if soc >= SOC_FULL:
            self._start_calibration_timer()
        elif self._calibration_timer is not None:
            self._calibration_timer()
            self._calibration_timer = None
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

    def _apply_session_fallback(self) -> None:
        """Count the open session's SoC rise as charger energy when the charger reported none."""
        st = self.state
        session = st.session
        if session is None:
            return
        reported = st.charger_energy - session.charger_at_start - session.fallback_kwh
        added = st.soc.energy_added - session.added_at_start
        if reported <= 0 and added > session.fallback_kwh:
            extra = added - session.fallback_kwh
            self._add_charger_energy(extra)
            session.fallback_kwh += extra

    def _finalise_session(self, to: str, end_ts: float) -> None:
        st = self.state
        session = st.session
        st.pending_session_end = None
        if session is None:
            return
        end_soc = self.current_soc()
        self._apply_session_fallback()
        kwh = max(st.charger_energy - session.charger_at_start, 0.0)
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

    # ------------------------------------------------------------------ temperature

    def current_temp_c(self) -> float | None:
        """Outside temperature: the sensor, else the weather entity, else None."""
        sensor = self.config.get(CONF_TEMPERATURE_SENSOR)
        if sensor:
            value = state_celsius(self.hass.states.get(sensor))
            if value is not None:
                return value
        weather = self.config.get(CONF_WEATHER_ENTITY)
        if weather:
            return weather_celsius(self.hass.states.get(weather))
        return None

    @callback
    def _on_temperature_change(self, _event: Event[EventStateChangedData]) -> None:
        self.notify_listeners()

    @callback
    def _on_forecast_time(self, _now: datetime) -> None:
        self.entry.async_create_task(self.hass, self.async_refresh_forecast(), "ford_ev forecast")

    async def async_refresh_forecast(self) -> None:
        """Tomorrow's mean temperature from the weather entity's daily forecast."""
        weather = self.config.get(CONF_WEATHER_ENTITY)
        value: float | None = None
        if weather and self.hass.services.has_service("weather", "get_forecasts"):
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"type": "daily"},
                    target={"entity_id": weather},
                    blocking=True,
                    return_response=True,
                )
                value = self._parse_forecast(weather, response)
            except Exception:  # noqa: BLE001 - a missing forecast must never break the engine
                _LOGGER.debug("Forecast for %s unavailable", weather, exc_info=True)
        if value != self.forecast_tomorrow_c:
            self.forecast_tomorrow_c = value
            self.notify_listeners()

    def _parse_forecast(self, weather: str, response: Any) -> float | None:
        forecast = (response or {}).get(weather, {}).get("forecast") or []
        tomorrow = (dt_util.now() + timedelta(days=1)).date()
        state = self.hass.states.get(weather)
        unit = state.attributes.get("temperature_unit") if state else None
        for item in forecast:
            when = dt_util.parse_datetime(str(item.get("datetime", "")))
            if when is None or dt_util.as_local(when).date() != tomorrow:
                continue
            high = item.get("temperature")
            low = item.get("templow")
            if high is None:
                return None
            mean = (float(high) + float(low)) / 2 if low is not None else float(high)
            return to_celsius(mean, unit)
        return None

    # ------------------------------------------------------------------ trips

    def _on_ignition(self, prev: str | None, new_state: State) -> None:
        new = new_state.state
        st = self.state
        if prev == "OFF" and new != "OFF":
            st.model = replace(
                st.model, trip_start_ts=new_state.last_changed.timestamp(), trip_start_temp=self.current_temp_c()
            )
            self.save()
        elif new == "OFF" and (
            prev not in (None, "OFF")
            # First value after a restart: close a trip that was open when Home Assistant stopped.
            or (prev is None and st.model.trip_start_ts is not None and st.pending_trip_end_ts is None)
        ):
            st.pending_trip_end_ts = new_state.last_changed.timestamp()
            if self._trip_end_timer is not None:
                self._trip_end_timer()
            self._trip_end_timer = async_call_later(self.hass, TRIP_END_DELAY_S, self._on_trip_end_timer)
            self.save()

    @callback
    def _on_trip_end_timer(self, _now: datetime) -> None:
        self._trip_end_timer = None
        if self.state.pending_trip_end_ts is not None:
            self._account_trip(self.state.pending_trip_end_ts)

    def _account_trip(self, end_ts: float) -> None:
        """Charge the trip to the model; without an odometer reading it stays pending."""
        st = self.state
        odometer = state_km(self.fcq_state("odometer"))
        if odometer is None:
            st.pending_trip_end_ts = end_ts
            self.save()
            return
        st.pending_trip_end_ts = None
        temp = self.current_temp_c()
        st.model = account_trip(
            st.model,
            TripInput(
                odometer_km=odometer,
                start_ts=st.model.trip_start_ts,
                end_ts=end_ts,
                start_temp_c=st.model.trip_start_temp,
                end_temp_c=temp if temp is not None else FALLBACK_TEMP_C,
                setpoint_c=st.settings.climate_setpoint,
                climate_on=st.settings.climate_on,
            ),
            self.capacity_kwh,
        )
        self.notify_listeners()
        self.save()

    def _recover_trip(self) -> None:
        st = self.state
        if st.pending_trip_end_ts is not None:
            self._account_trip(st.pending_trip_end_ts)
            return
        ignition = self.fcq_state("ignitionstatus")
        if st.model.trip_start_ts is not None and ignition is not None and ignition.state == "OFF":
            self._account_trip(ignition.last_changed.timestamp())

    # ------------------------------------------------------------------ range model

    def _seeded_model(self, avg_speed: float | None = None) -> RangeModelState:
        soc = state_number(self.fcq_state("soc"))
        temp = self.current_temp_c()
        a0 = seed_drive_factor(soc, state_km(self.fcq_state("elveh")), self.capacity_kwh, temp if temp is not None else 20.0)
        model = RangeModelState(
            a=a0,
            a0=a0,
            model_energy=soc / 100 * self.capacity_kwh if soc is not None else None,
            odo_accounted=state_km(self.fcq_state("odometer")),
        )
        if avg_speed is not None:
            model = replace(model, avg_speed=avg_speed)
        return model

    def _seed_model(self) -> None:
        self.state.model = self._seeded_model()

    def reset_range_model(self) -> None:
        """Back to the initial factors; model SoC from the car's SoC. Keeps the average speed."""
        self.state.model = self._seeded_model(avg_speed=self.state.model.avg_speed)
        self.notify_listeners()
        self.save()

    def _start_calibration_timer(self) -> None:
        if self._calibration_timer is None:
            self._calibration_timer = async_call_later(self.hass, CALIBRATION_DELAY_S, self._on_calibration_timer)

    @callback
    def _on_calibration_timer(self, _now: datetime) -> None:
        self._calibration_timer = None
        soc = state_number(self.fcq_state("soc"))
        now_ts = dt_util.utcnow().timestamp()
        if soc is None or soc < SOC_FULL:
            return
        # Vehicles without a charger counter: the open session's energy must be in this cycle.
        self._apply_session_fallback()
        if not calibration_allowed(self.state.model, now_ts):
            self.save()
            return
        session_kwh, start_soc_car, start_soc_model = self._calibration_session()
        self.state.model, result = calibrate(
            self.state.model, self.capacity_kwh, now_ts, session_kwh, start_soc_car, start_soc_model
        )
        factors = (
            f"drive x{result.a:.2f} climate x{result.b:.2f}" if result.valid else f"NOT USED, ratio {result.ratio:.2f}"
        )
        summary = (
            f"{result.km:.0f} km, charged {result.charged_kwh:.1f} kWh, model {result.predicted_kwh:.1f} kWh "
            f"({result.error_pct:+.1f}%) | {factors}"
        )
        self.emit("report", "calibration", {**asdict(result), "summary": summary})
        self.notify_listeners()
        self.save()

    def _calibration_session(self) -> tuple[float | None, float | None, float | None]:
        st = self.state
        if st.session is not None:
            return st.charger_energy - st.session.charger_at_start, st.session.start_soc, st.session.model_soc_at_start
        last = st.last_session
        if last:
            end = dt_util.parse_datetime(str(last.get("end", "")))
            if end is not None and dt_util.utcnow() - end <= LAST_SESSION_MAX_AGE:
                return last.get("kwh"), last.get("start_soc"), last.get("model_soc_at_start")
        return None, None, None

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

"""Sensor entities: reports, charging, the car's own estimate, and the range model."""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfEnergyDistance,
    UnitOfLength,
    UnitOfMass,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .engine import FordEvConfigEntry, FordEvEngine
from .entity import FordEvEntity
from .const import CONF_FUEL_CO2, CONF_GRID_CO2, CONF_SOLAR_POWER_ENTITY
from .periods import LIFETIME, period_value
from .trips import BIN_WIDTH_C, bin_rows, measured_consumption, speed_rows, trip_statistics
from .range_model import BY_TEMPERATURE, climate_kw, drive_kwh, model_soc, predicted_kwh_per_km
from .units import state_km, state_number

# data_age is the only polled sensor.
SCAN_INTERVAL = timedelta(minutes=5)
MIN_CONSUMPTION_KM = 10
CHARGING_STATES = {"A": "unplugged", "B": "plugged", "C": "charging"}


@dataclass(frozen=True, kw_only=True)
class FordEvSensorDescription(SensorEntityDescription):
    """A sensor computed from the engine."""

    value_fn: Callable[[FordEvEngine], StateType | datetime]
    attrs_fn: Callable[[FordEvEngine], dict[str, Any]] | None = None
    period: str | None = None
    currency: bool = False
    # Appended to the currency, for example "/100 km".
    unit_suffix: str = ""
    poll: bool = False
    # The sensor is only created when this returns True.
    exists_fn: Callable[[FordEvEngine], bool] | None = None


# ---------------------------------------------------------------------- value helpers


def _period(period: str, counter: str) -> Callable[[FordEvEngine], float | None]:
    def value(engine: FordEvEngine) -> float | None:
        return period_value(engine.state.periods, period, counter, engine.totals())

    return value


def _consumption(period: str) -> Callable[[FordEvEngine], float | None]:
    def value(engine: FordEvEngine) -> float | None:
        totals = engine.totals()
        km = period_value(engine.state.periods, period, "odometer", totals)
        used = period_value(engine.state.periods, period, "energy_used", totals)
        if km is None or used is None or km < MIN_CONSUMPTION_KM:
            return None
        return used / km * 100

    return value


def _consumption_attrs(period: str) -> Callable[[FordEvEngine], dict[str, Any]]:
    """The same without the energy used while parked."""

    def attrs(engine: FordEvEngine) -> dict[str, Any]:
        totals = engine.totals()
        km = period_value(engine.state.periods, period, "odometer", totals)
        used = period_value(engine.state.periods, period, "energy_used", totals)
        parked = period_value(engine.state.periods, period, "parked_energy", totals)
        driving = max(used - (parked or 0.0), 0.0) if used is not None else None
        return {"driving": _round(_per_100(driving, km), 1), "parked_kwh": _round(parked, 2)}

    return attrs


def _soc_and_range(engine: FordEvEngine) -> tuple[float | None, float | None]:
    return state_number(engine.fcq_state("soc")), state_km(engine.fcq_state("elveh"))


def _car_full_range(engine: FordEvEngine) -> float | None:
    soc, rng = _soc_and_range(engine)
    if soc is None or rng is None or soc <= 5 or rng <= 0:
        return None
    return rng / soc * 100


def _car_km_per_percent(engine: FordEvEngine) -> float | None:
    soc, rng = _soc_and_range(engine)
    if soc is None or rng is None or soc <= 5 or rng <= 0:
        return None
    return rng / soc


def _data_age(engine: FordEvEngine) -> float | None:
    state = engine.fcq_state("lastrefresh")
    when = dt_util.parse_datetime(state.state) if state is not None else None
    if when is None:
        return None
    return max((dt_util.utcnow() - when).total_seconds() / 60, 0.0)


def _kwh_per_km(engine: FordEvEngine, temp: float | None) -> float | None:
    if temp is None:
        return None
    m, s = engine.state.model, engine.state.settings
    return predicted_kwh_per_km(temp, m.a, m.b, m.avg_speed, s.climate_setpoint, s.climate_on)


def _model_consumption(engine: FordEvEngine) -> float | None:
    per_km = _kwh_per_km(engine, engine.current_temp_c())
    return per_km * 100 if per_km is not None else None


def _model_consumption_attrs(engine: FordEvEngine) -> dict[str, Any]:
    temp = engine.current_temp_c()
    m, s = engine.state.model, engine.state.settings
    if temp is None:
        return {"average_speed": round(m.avg_speed)}
    kw = m.b * climate_kw(temp, s.climate_setpoint, s.climate_on)
    return {
        "driving": round(m.a * drive_kwh(100, temp), 1),
        "climate": round(kw / max(m.avg_speed, 5) * 100, 1),
        "climate_kw": round(kw, 2),
        "average_speed": round(m.avg_speed),
    }


def _model_soc(engine: FordEvEngine) -> float | None:
    return model_soc(engine.state.model, engine.capacity_kwh)


def _model_soc_attrs(engine: FordEvEngine) -> dict[str, Any]:
    energy = engine.state.model.model_energy
    car = state_number(engine.fcq_state("soc"))
    if energy is None or car is None:
        return {"difference_vs_car": None}
    return {"difference_vs_car": round(energy / engine.capacity_kwh * 100 - car, 1)}


def _model_range(engine: FordEvEngine) -> float | None:
    per_km = _kwh_per_km(engine, engine.current_temp_c())
    energy = engine.state.model.model_energy
    if per_km is None or per_km <= 0 or energy is None:
        return None
    return max(energy, 0.0) / per_km


def _model_range_attrs(engine: FordEvEngine) -> dict[str, Any]:
    per_km = _kwh_per_km(engine, engine.current_temp_c())
    car = state_number(engine.fcq_state("soc"))
    if per_km is None or per_km <= 0 or car is None:
        return {"with_car_soc": None}
    return {"with_car_soc": round(car / 100 * engine.capacity_kwh / per_km)}


def _full_range_at(engine: FordEvEngine, temp: float | None) -> float | None:
    per_km = _kwh_per_km(engine, temp)
    if per_km is None or per_km <= 0:
        return None
    return engine.capacity_kwh / per_km


def _by_temperature(engine: FordEvEngine) -> dict[str, Any]:
    rows = []
    for temp in BY_TEMPERATURE:
        per_km = _kwh_per_km(engine, temp)
        if per_km is None or per_km <= 0:
            continue
        rows.append({"t": temp, "km": round(engine.capacity_kwh / per_km), "kwh100": round(per_km * 100, 1)})
    return {"by_temperature": rows}


def _last_calibration(engine: FordEvEngine) -> datetime | None:
    ts = engine.state.model.last_calibration
    return dt_util.utc_from_timestamp(ts) if ts is not None else None


def _cycle_attrs(engine: FordEvEngine) -> dict[str, Any]:
    m = engine.state.model
    return {
        "cycle_km": round(m.cycle_km, 1),
        "cycle_charged_kwh": round(m.cycle_charged, 2),
        # Of that, what only refills a battery that was not full when the cycle began.
        "cycle_refill_kwh": round(m.cycle_deficit or 0.0, 2),
        "cycle_model_kwh": round(m.a * m.cycle_x_drive + m.b * m.cycle_x_hvac, 2),
    }


def _last_charge(engine: FordEvEngine) -> str | None:
    last = engine.state.last_session
    return str(last.get("summary", ""))[:255] if last else None


def _charge_end(engine: FordEvEngine) -> datetime | None:
    """When the car expects the running charge to finish; only while charging.

    FCQ derives estimatedEndTime from the car's time to full charge, which the car keeps
    reporting while it is not charging.
    """
    if engine.last_valid("evccstatus") != "C":
        return None
    state = engine.fcq_state("elvehcharging")
    value = state.attributes.get("estimatedEndTime") if state is not None else None
    if isinstance(value, str):
        value = dt_util.parse_datetime(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value


# ---------------------------------------------------------------------- costs


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _per_100(amount: float | None, km: float | None) -> float | None:
    if amount is None or km is None or km < MIN_CONSUMPTION_KM:
        return None
    return amount / km * 100


def _in_period(engine: FordEvEngine, period: str, counter: str) -> float | None:
    return period_value(engine.state.periods, period, counter, engine.totals())


def _cost_per_100km(period: str) -> Callable[[FordEvEngine], float | None]:
    def value(engine: FordEvEngine) -> float | None:
        return _per_100(_in_period(engine, period, "driving_cost"), _in_period(engine, period, "odometer"))

    return value


def _tracked_since(engine: FordEvEngine, period: str) -> str | None:
    """The day tracking started, when the period began before it did."""
    periods = engine.state.periods
    snap, lifetime = periods.snapshots.get(period), periods.snapshots.get(LIFETIME)
    if snap is None or lifetime is None:
        return None
    return lifetime.start if lifetime.start > snap.start else None


def _cost_per_100km_attrs(period: str) -> Callable[[FordEvEngine], dict[str, Any]]:
    def attrs(engine: FordEvEngine) -> dict[str, Any]:
        def get(counter: str) -> float | None:
            return _in_period(engine, period, counter)

        charged, cost = get("charger_energy"), get("cost")
        driving_cost, parked_cost = get("driving_cost"), get("parked_cost")
        driving_only = max(driving_cost - (parked_cost or 0.0), 0.0) if driving_cost is not None else None
        out: dict[str, Any] = {
            "km": _round(get("odometer"), 1),
            "driving_cost": _round(driving_cost, 2),
            # The same per 100 km without the energy used while parked.
            "driving_only": _round(_per_100(driving_only, get("odometer")), 2),
            "parked_cost": _round(parked_cost, 2),
            "charging_cost": _round(cost, 2),
            "charging_cost_home": _round(get("cost_home"), 2),
            "charging_cost_public": _round(get("cost_public"), 2),
            "charged_kwh": _round(charged, 1),
            "solar_kwh": _round(get("charger_energy_solar"), 1),
            "home_share_pct": _share(get("charger_energy_home"), charged),
            # Solar is counted at the meter, losses included, so it is compared with what was billed.
            "solar_share_pct": _share(get("charger_energy_solar"), _billed_home(engine, get("charger_energy_home"))),
            "price_per_kwh": round(cost / charged, 3) if cost is not None and charged else None,
        }
        if period == LIFETIME:
            snapshot = engine.state.periods.snapshots.get(LIFETIME)
            out["since"] = snapshot.start if snapshot else None
        else:
            out["since"] = _tracked_since(engine, period)
            previous = engine.state.periods.previous.get(period, {})
            out["previous_period"] = _round(_per_100(previous.get("driving_cost"), previous.get("odometer")), 2)
        return out

    return attrs


def _share(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole or whole <= 0:
        return None
    return round(min(part / whole, 1.0) * 100, 1)


def _billed_home(engine: FordEvEngine, kwh: float | None) -> float | None:
    return kwh * (1 + engine.loss_pct(False) / 100) if kwh is not None else None


def _battery_price_attrs(engine: FordEvEngine) -> dict[str, Any]:
    return {"battery_value": _round(engine.state.battery_value, 2)}


def _energy_price(engine: FordEvEngine) -> float | None:
    """Price of the energy the car drives on: what is in the battery, else the home price."""
    price = engine.battery_price()
    if price is None:
        price = (engine.scenario_prices() or {}).get("home")
    return price


def _model_cost(engine: FordEvEngine, temp: float | None) -> float | None:
    per_km, price = _kwh_per_km(engine, temp), _energy_price(engine)
    if per_km is None or price is None:
        return None
    return per_km * 100 * price


def _model_cost_attrs(engine: FordEvEngine) -> dict[str, Any]:
    prices = {k: v for k, v in (engine.scenario_prices() or {}).items() if v is not None}
    battery = engine.battery_price()
    if battery is not None:
        prices["battery"] = battery
    rows = []
    for temp in BY_TEMPERATURE:
        per_km = _kwh_per_km(engine, temp)
        if per_km is None or per_km <= 0:
            continue
        row: dict[str, Any] = {"t": temp, "kwh100": round(per_km * 100, 1)}
        row.update({name: round(per_km * 100 * price, 2) for name, price in prices.items()})
        rows.append(row)
    return {"prices_per_kwh": {k: round(v, 3) for k, v in prices.items()}, "by_temperature": rows}


def _last_trip(engine: FordEvEngine) -> str | None:
    last = engine.state.last_trip
    return str(last.get("summary", ""))[:255] if last else None


def _last_trip_attrs(engine: FordEvEngine) -> dict[str, Any]:
    last = engine.state.last_trip or {}
    out = {k: v for k, v in last.items() if k not in ("end_ts", "bin", "speed_bin", "summary")}
    for key, digits in (("kwh", 2), ("cost", 2)):
        if out.get(key) is not None:
            out[key] = round(out[key], digits)
    return out


def _month_tracking_start(engine: FordEvEngine, since: str | None) -> datetime | None:
    """Start of the part of this month that was tracked from the given date on."""
    month = engine.state.periods.snapshots.get("month")
    if month is None:
        return None
    start = date.fromisoformat(month.start)
    if since is not None:
        start = max(start, date.fromisoformat(since))
    return dt_util.start_of_local_day(start)


def _parked_attrs(engine: FordEvEngine) -> dict[str, Any]:
    month_kwh = _in_period(engine, "month", "parked_energy")
    start = _month_tracking_start(engine, engine.state.parked_since)
    days = (dt_util.now() - start).total_seconds() / 86400 if start else 0.0
    per_day = month_kwh / days if month_kwh is not None and days >= 1 else None
    return {
        "cost": round(engine.state.parked_cost, 2),
        "month_kwh": _round(month_kwh, 2),
        "month_cost": _round(_in_period(engine, "month", "parked_cost"), 2),
        "per_day": _round(per_day, 2),
        "percent_per_day": round(per_day / engine.capacity_kwh * 100, 2) if per_day is not None else None,
        "share_pct": _share(engine.state.parked_energy, engine.state.soc.energy_used),
        # Of the total: used with the ignition on and the car not moving.
        "idle_kwh": round(engine.state.idle_energy, 2),
    }


def _charge_to_target(engine: FordEvEngine) -> tuple[float, float] | None:
    """(kWh, cost) to reach the charge target where the car is now."""
    soc = engine.current_soc()
    home = engine.is_home()
    price = engine.price() if home else engine.public_price()
    if soc is None or price is None:
        return None
    kwh = max(engine.state.settings.charge_target - soc, 0.0) / 100 * engine.capacity_kwh
    dc = not home
    if dc and engine.is_plugged():
        # Away from home the charger is assumed to be DC, unless the car says it is AC.
        charging = engine.fcq_state("elvehcharging")
        dc = "AC" not in str((charging.attributes.get("chargingType") if charging else "") or "").upper()
    return kwh, kwh * (1 + engine.loss_pct(dc) / 100) * price


def _charge_to_target_cost(engine: FordEvEngine) -> float | None:
    result = _charge_to_target(engine)
    return result[1] if result else None


def _charge_to_target_attrs(engine: FordEvEngine) -> dict[str, Any]:
    result = _charge_to_target(engine)
    return {"kwh": round(result[0], 1) if result else None, "location": "home" if engine.is_home() else "away"}


def _capacity_estimate(engine: FordEvEngine) -> float | None:
    """Median of the last estimates: one estimate moves with the state of charge it started from."""
    estimates = engine.state.capacity_estimates
    return statistics.median(estimates) if estimates else None


def _capacity_attrs(engine: FordEvEngine) -> dict[str, Any]:
    estimate = _capacity_estimate(engine)
    return {
        "percent_of_configured": round(estimate / engine.capacity_kwh * 100, 1) if estimate else None,
        "last": _round(engine.state.capacity_estimate, 1),
        "samples": len(engine.state.capacity_estimates),
    }


def _measured_by_temperature(engine: FordEvEngine) -> dict[str, Any]:
    """The temperature bands, each with what the model predicts at the middle of the band."""
    rows = bin_rows(engine.state.temp_bins)
    for row in rows:
        per_km = _kwh_per_km(engine, row["t"] + BIN_WIDTH_C / 2)
        row["model"] = round(per_km * 100, 1) if per_km is not None else None
    return {"by_temperature": rows}


def _trips_7d(engine: FordEvEngine) -> int:
    return trip_statistics(engine.state.trips, dt_util.utcnow().timestamp()).get("trips_7d", 0)


def _trip_attrs(engine: FordEvEngine) -> dict[str, Any]:
    stats = trip_statistics(engine.state.trips, dt_util.utcnow().timestamp())
    stats.pop("trips_7d", None)
    return {**stats, "by_speed": speed_rows(engine.state.speed_bins)}


def _charging_power(engine: FordEvEngine) -> float | None:
    return engine.charging_power_kw if engine.state.session is not None else None


def _charging_power_attrs(engine: FordEvEngine) -> dict[str, Any]:
    power = _charging_power(engine)
    soc = engine.current_soc()
    kwh = max(engine.state.settings.charge_target - soc, 0.0) / 100 * engine.capacity_kwh if soc is not None else None
    return {"to_target_min": round(kwh / power * 60) if power and kwh is not None else None}


def _session_attrs(engine: FordEvEngine) -> dict[str, Any]:
    st = engine.state
    return {
        "mean_kwh": round(st.sessions_kwh / st.sessions_count, 1) if st.sessions_count else None,
        "mean_start_soc": round(st.sessions_start_soc_sum / st.sessions_start_soc_count)
        if st.sessions_start_soc_count
        else None,
        "dc_share_pct": _share(st.sessions_dc_kwh, st.sessions_kwh),
        # Battery energy added so far, in full batteries.
        "full_cycles": round(st.soc.energy_added / engine.capacity_kwh, 1),
    }


def _charging_efficiency(engine: FordEvEngine) -> float | None:
    st = engine.state
    return st.eff_battery_kwh / st.eff_charger_kwh * 100 if st.eff_charger_kwh > 0 else None


def _charging_efficiency_attrs(engine: FordEvEngine) -> dict[str, Any]:
    st = engine.state
    return {
        "sessions": st.eff_sessions,
        "last": _round(st.eff_last_pct, 1),
        "charger_kwh": round(st.eff_charger_kwh, 1),
        "battery_kwh": round(st.eff_battery_kwh, 1),
    }


def _daily_km(engine: FordEvEngine) -> float | None:
    """Mean km per day over the tracked part of this month and the previous one.

    None under 3 tracked days, and when the day tracking started is not known.
    """
    periods = engine.state.periods
    month, lifetime = periods.snapshots.get("month"), periods.snapshots.get(LIFETIME)
    km = _in_period(engine, "month", "odometer")
    if month is None or lifetime is None or km is None:
        return None
    month_start, tracked_from = date.fromisoformat(month.start), date.fromisoformat(lifetime.start)
    previous = periods.previous.get("month")
    if previous is None and tracked_from == month_start:
        return None
    start = max(month_start, tracked_from)
    if previous is not None and "odometer" in previous and tracked_from < month_start:
        previous_start = (month_start - timedelta(days=1)).replace(day=1)
        start = max(previous_start, tracked_from)
        km += previous["odometer"]
    days = (dt_util.now() - dt_util.start_of_local_day(start)).total_seconds() / 86400
    return km / days if days >= 3 else None


def _days_of_range(engine: FordEvEngine) -> float | None:
    daily, km = _daily_km(engine), _model_range(engine)
    if daily is None or daily <= 0 or km is None:
        return None
    return km / daily


def _days_of_range_attrs(engine: FordEvEngine) -> dict[str, Any]:
    daily, car = _daily_km(engine), state_km(engine.fcq_state("elveh"))
    return {
        "daily_km": _round(daily, 1),
        "days_car": round(car / daily, 1) if daily and daily > 0 and car is not None else None,
    }


def _month_projection_factor(engine: FordEvEngine) -> float | None:
    """Month so far times this is the whole month. None until 3 days of the month are tracked.

    When the month began before tracking did and the day tracking started is not known, there
    is no projection.
    """
    periods = engine.state.periods
    month, lifetime = periods.snapshots.get("month"), periods.snapshots.get(LIFETIME)
    if month is None or lifetime is None:
        return None
    month_start, tracked_from = date.fromisoformat(month.start), date.fromisoformat(lifetime.start)
    if "month" not in periods.previous and tracked_from == month_start:
        return None
    start = dt_util.start_of_local_day(max(month_start, tracked_from))
    now = dt_util.now()
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    elapsed = (now - start).total_seconds() / 86400
    remaining = max((dt_util.start_of_local_day(next_month) - now).total_seconds() / 86400, 0.0)
    if elapsed < 3:
        return None
    return 1 + remaining / elapsed


def _month_projection(engine: FordEvEngine) -> float | None:
    factor, cost = _month_projection_factor(engine), _in_period(engine, "month", "driving_cost")
    return cost * factor if factor is not None and cost is not None else None


def _month_projection_attrs(engine: FordEvEngine) -> dict[str, Any]:
    factor, km = _month_projection_factor(engine), _in_period(engine, "month", "odometer")
    # A year at the km per day driven lately and the cost per km since the start.
    daily = _daily_km(engine)
    total_km, total_cost = _in_period(engine, LIFETIME, "odometer"), _in_period(engine, LIFETIME, "driving_cost")
    year_km = daily * 365 if daily is not None else None
    per_km = total_cost / total_km if total_cost is not None and total_km and total_km >= MIN_CONSUMPTION_KM else None
    return {
        "km": round(km * factor) if factor is not None and km is not None else None,
        "year_km": round(year_km) if year_km is not None else None,
        "year_cost": round(year_km * per_km) if year_km is not None and per_km is not None else None,
    }


def _fuel_savings_in(engine: FordEvEngine, period: str) -> float | None:
    """What the same distance would have cost in fuel, minus what it cost in electricity."""
    consumption, price = engine.ice_consumption(), engine.fuel_price()
    km, cost = _in_period(engine, period, "odometer"), _in_period(engine, period, "driving_cost")
    if consumption is None or price is None or km is None or cost is None:
        return None
    return km * consumption / 100 * price - cost


def _fuel_savings_attrs(engine: FordEvEngine) -> dict[str, Any]:
    consumption, price = engine.ice_consumption(), engine.fuel_price()
    out: dict[str, Any] = {p: _round(_fuel_savings_in(engine, p), 2) for p in ("week", "month", "year")}
    out["fuel_cost_per_100km"] = round(consumption * price, 2) if consumption is not None and price is not None else None
    return out


def _co2_savings_in(engine: FordEvEngine, period: str) -> float | None:
    """kg of CO2 the same distance would have emitted from fuel, minus those of the electricity used.

    The electricity is the energy used plus the AC charging losses, at the grid figure set in
    the options; the fuel is the comparison car's consumption at the figure per litre set there.
    """
    consumption, fuel, grid = engine.ice_consumption(), engine.config.get(CONF_FUEL_CO2), engine.config.get(CONF_GRID_CO2)
    km, used = _in_period(engine, period, "odometer"), _in_period(engine, period, "energy_used")
    if consumption is None or fuel is None or grid is None or km is None or used is None:
        return None
    return km * consumption / 100 * float(fuel) - used * (1 + engine.loss_pct(False) / 100) * float(grid) / 1000


def _solar_savings_in(engine: FordEvEngine, period: str | None) -> float | None:
    """Solar energy charged, times what it saved per kWh against the home price."""
    price = engine.price()
    kwh = engine.state.charger_energy_solar if period is None else _in_period(engine, period, "charger_energy_solar")
    if price is None or kwh is None:
        return None
    return kwh * max(price - engine.solar_price(), 0.0)


# ---------------------------------------------------------------------- descriptions

_ENERGY = {
    "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    "device_class": SensorDeviceClass.ENERGY,
    "suggested_display_precision": 1,
}
_KM = {
    "native_unit_of_measurement": UnitOfLength.KILOMETERS,
    "device_class": SensorDeviceClass.DISTANCE,
    "suggested_display_precision": 0,
}
_CONSUMPTION = {
    "native_unit_of_measurement": UnitOfEnergyDistance.KILO_WATT_HOUR_PER_100_KM,
    "device_class": SensorDeviceClass.ENERGY_DISTANCE,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 1,
}
_MONEY = {"device_class": SensorDeviceClass.MONETARY, "currency": True, "suggested_display_precision": 2}
# An amount of money that is an estimate or a rate, not a running total.
_AMOUNT = {"currency": True, "state_class": SensorStateClass.MEASUREMENT, "suggested_display_precision": 2}
_PER_100KM = {**_AMOUNT, "unit_suffix": "/100 km"}


def _total(key: str, fn: Callable[[FordEvEngine], float]) -> FordEvSensorDescription:
    return FordEvSensorDescription(key=key, value_fn=fn, state_class=SensorStateClass.TOTAL_INCREASING, **_ENERGY)


def _period_sensor(key: str, period: str, counter: str, kind: dict[str, Any]) -> FordEvSensorDescription:
    def previous(engine: FordEvEngine) -> dict[str, Any]:
        return {"previous_period": engine.state.periods.previous.get(period, {}).get(counter)}

    return FordEvSensorDescription(
        key=key,
        value_fn=_period(period, counter),
        attrs_fn=previous,
        period=period,
        state_class=SensorStateClass.TOTAL,
        **kind,
    )


SENSORS: tuple[FordEvSensorDescription, ...] = (
    _total("energy_used", lambda e: e.state.soc.energy_used),
    _total("energy_added", lambda e: e.state.soc.energy_added),
    _total("charger_energy", lambda e: e.state.charger_energy),
    _total("charger_energy_home", lambda e: e.state.charger_energy_home),
    _total("charger_energy_solar", lambda e: e.state.charger_energy_solar),
    FordEvSensorDescription(
        key="charging_cost", value_fn=lambda e: e.state.cost, state_class=SensorStateClass.TOTAL, **_MONEY
    ),
    FordEvSensorDescription(
        key="charging_cost_home", value_fn=lambda e: e.state.cost_home, state_class=SensorStateClass.TOTAL, **_MONEY
    ),
    FordEvSensorDescription(
        key="charging_cost_public", value_fn=lambda e: e.state.cost_public, state_class=SensorStateClass.TOTAL, **_MONEY
    ),
    _period_sensor("distance_day", "day", "distance", _KM),
    _period_sensor("distance_week", "week", "distance", _KM),
    _period_sensor("distance_month", "month", "distance", _KM),
    _period_sensor("energy_used_week", "week", "energy_used", _ENERGY),
    _period_sensor("energy_used_month", "month", "energy_used", _ENERGY),
    _period_sensor("charger_energy_week", "week", "charger_energy", _ENERGY),
    _period_sensor("charger_energy_month", "month", "charger_energy", _ENERGY),
    _period_sensor("charger_energy_home_week", "week", "charger_energy_home", _ENERGY),
    _period_sensor("charger_energy_home_month", "month", "charger_energy_home", _ENERGY),
    _period_sensor("cost_week", "week", "cost", _MONEY),
    _period_sensor("cost_month", "month", "cost", _MONEY),
    *(
        FordEvSensorDescription(
            key=f"consumption_{period}",
            value_fn=_consumption(period),
            attrs_fn=_consumption_attrs(period),
            **_CONSUMPTION,
        )
        for period in ("week", "month")
    ),
    _period_sensor("distance_year", "year", "distance", _KM),
    _period_sensor("energy_used_year", "year", "energy_used", _ENERGY),
    _period_sensor("charger_energy_year", "year", "charger_energy", _ENERGY),
    _period_sensor("cost_year", "year", "cost", _MONEY),
    FordEvSensorDescription(
        key="consumption_year", value_fn=_consumption("year"), attrs_fn=_consumption_attrs("year"), **_CONSUMPTION
    ),
    FordEvSensorDescription(
        key="driving_cost",
        value_fn=lambda e: e.state.driving_cost if e.state.battery_value is not None else None,
        state_class=SensorStateClass.TOTAL,
        **_MONEY,
    ),
    FordEvSensorDescription(
        key="battery_energy_price",
        value_fn=lambda e: e.battery_price(),
        attrs_fn=_battery_price_attrs,
        **{**_AMOUNT, "unit_suffix": "/kWh", "suggested_display_precision": 3},
    ),
    *(
        FordEvSensorDescription(
            key=f"cost_per_100km_{period}",
            value_fn=_cost_per_100km(period),
            attrs_fn=_cost_per_100km_attrs(period),
            **_PER_100KM,
        )
        for period in ("week", "month", "year", LIFETIME)
    ),
    FordEvSensorDescription(
        key="model_cost_per_100km",
        value_fn=lambda e: _model_cost(e, e.current_temp_c()),
        attrs_fn=_model_cost_attrs,
        **_PER_100KM,
    ),
    FordEvSensorDescription(
        key="model_cost_per_100km_tomorrow",
        value_fn=lambda e: _model_cost(e, e.forecast_tomorrow_c),
        attrs_fn=lambda e: {"temperature": e.forecast_tomorrow_c},
        **{**_PER_100KM, "state_class": None},
    ),
    FordEvSensorDescription(
        key="cost_month_projection", value_fn=_month_projection, attrs_fn=_month_projection_attrs, **_AMOUNT
    ),
    FordEvSensorDescription(
        key="charge_to_target_cost", value_fn=_charge_to_target_cost, attrs_fn=_charge_to_target_attrs, **_AMOUNT
    ),
    FordEvSensorDescription(
        key="fuel_savings",
        value_fn=lambda e: _fuel_savings_in(e, LIFETIME),
        attrs_fn=_fuel_savings_attrs,
        exists_fn=lambda e: e.ice_consumption() is not None,
        **_AMOUNT,
    ),
    FordEvSensorDescription(
        key="co2_savings",
        value_fn=lambda e: _co2_savings_in(e, LIFETIME),
        attrs_fn=lambda e: {p: _round(_co2_savings_in(e, p), 1) for p in ("week", "month", "year")},
        exists_fn=lambda e: (
            e.ice_consumption() is not None
            and e.config.get(CONF_FUEL_CO2) is not None
            and e.config.get(CONF_GRID_CO2) is not None
        ),
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    FordEvSensorDescription(
        key="solar_savings",
        value_fn=lambda e: _solar_savings_in(e, None),
        attrs_fn=lambda e: {p: _round(_solar_savings_in(e, p), 2) for p in ("week", "month", "year")},
        exists_fn=lambda e: bool(e.config.get(CONF_SOLAR_POWER_ENTITY)),
        **_AMOUNT,
    ),
    FordEvSensorDescription(
        key="consumption_by_temperature",
        value_fn=lambda e: measured_consumption(e.state.temp_bins),
        attrs_fn=_measured_by_temperature,
        **_CONSUMPTION,
    ),
    FordEvSensorDescription(key="last_trip", value_fn=_last_trip, attrs_fn=_last_trip_attrs),
    FordEvSensorDescription(
        key="parked_energy",
        value_fn=lambda e: e.state.parked_energy,
        attrs_fn=_parked_attrs,
        state_class=SensorStateClass.TOTAL_INCREASING,
        **_ENERGY,
    ),
    FordEvSensorDescription(
        key="capacity_estimate",
        value_fn=_capacity_estimate,
        attrs_fn=_capacity_attrs,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    FordEvSensorDescription(
        key="charging_efficiency",
        value_fn=_charging_efficiency,
        attrs_fn=_charging_efficiency_attrs,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    FordEvSensorDescription(
        key="trips",
        value_fn=_trips_7d,
        attrs_fn=_trip_attrs,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FordEvSensorDescription(
        key="charging_power",
        value_fn=_charging_power,
        attrs_fn=_charging_power_attrs,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    FordEvSensorDescription(
        key="charging_sessions",
        value_fn=lambda e: e.state.sessions_count,
        attrs_fn=_session_attrs,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FordEvSensorDescription(
        key="days_of_range",
        value_fn=_days_of_range,
        attrs_fn=_days_of_range_attrs,
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    FordEvSensorDescription(
        key="car_full_range", value_fn=_car_full_range, state_class=SensorStateClass.MEASUREMENT, **_KM
    ),
    FordEvSensorDescription(
        key="car_km_per_percent",
        value_fn=_car_km_per_percent,
        native_unit_of_measurement="km/%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    FordEvSensorDescription(
        key="charging_state",
        value_fn=lambda e: CHARGING_STATES.get(e.last_valid("evccstatus") or ""),
        device_class=SensorDeviceClass.ENUM,
        options=list(CHARGING_STATES.values()),
    ),
    FordEvSensorDescription(key="charge_end", value_fn=_charge_end, device_class=SensorDeviceClass.TIMESTAMP),
    FordEvSensorDescription(
        key="data_age",
        value_fn=_data_age,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        poll=True,
    ),
    FordEvSensorDescription(
        key="last_charge", value_fn=_last_charge, attrs_fn=lambda e: dict(e.state.last_session or {})
    ),
    FordEvSensorDescription(
        key="outside_temperature",
        value_fn=lambda e: e.current_temp_c(),
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    FordEvSensorDescription(
        key="model_consumption", value_fn=_model_consumption, attrs_fn=_model_consumption_attrs, **_CONSUMPTION
    ),
    FordEvSensorDescription(
        key="model_soc",
        value_fn=_model_soc,
        attrs_fn=_model_soc_attrs,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    FordEvSensorDescription(
        key="model_range",
        value_fn=_model_range,
        attrs_fn=_model_range_attrs,
        state_class=SensorStateClass.MEASUREMENT,
        **_KM,
    ),
    FordEvSensorDescription(
        key="model_range_full",
        value_fn=lambda e: _full_range_at(e, e.current_temp_c()),
        attrs_fn=_by_temperature,
        state_class=SensorStateClass.MEASUREMENT,
        **_KM,
    ),
    FordEvSensorDescription(
        key="model_range_full_tomorrow",
        value_fn=lambda e: _full_range_at(e, e.forecast_tomorrow_c),
        attrs_fn=lambda e: {"temperature": e.forecast_tomorrow_c},
        **_KM,
    ),
    FordEvSensorDescription(
        key="drive_factor",
        value_fn=lambda e: e.state.model.a,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
    ),
    FordEvSensorDescription(
        key="climate_factor",
        value_fn=lambda e: e.state.model.b,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
    ),
    FordEvSensorDescription(
        key="cycle_error",
        value_fn=lambda e: e.state.model.last_error_pct,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    FordEvSensorDescription(
        key="calibrations",
        value_fn=lambda e: e.state.model.calibrations,
        attrs_fn=_cycle_attrs,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FordEvSensorDescription(
        key="last_calibration",
        value_fn=_last_calibration,
        attrs_fn=lambda e: dict(e.state.model.last_result or {}),
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: FordEvConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the sensors."""
    engine = entry.runtime_data.engine
    async_add_entities(
        FordEvSensor(engine, description)
        for description in SENSORS
        if description.exists_fn is None or description.exists_fn(engine)
    )


class FordEvSensor(FordEvEntity, SensorEntity):
    """A value computed from the engine."""

    entity_description: FordEvSensorDescription

    def __init__(self, engine: FordEvEngine, description: FordEvSensorDescription) -> None:
        super().__init__(engine, description.key)
        self.entity_description = description
        if description.currency:
            self._attr_native_unit_of_measurement = f"{engine.hass.config.currency}{description.unit_suffix}"
        self._attr_should_poll = description.poll

    @property
    def native_value(self) -> StateType | datetime:
        """Current value."""
        return self.entity_description.value_fn(self.engine)

    @property
    def last_reset(self) -> datetime | None:
        """Start of the current period, for period sensors."""
        period = self.entity_description.period
        if period is None:
            return None
        snapshot = self.engine.state.periods.snapshots.get(period)
        if snapshot is None:
            return None
        return dt_util.start_of_local_day(date.fromisoformat(snapshot.start))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra attributes."""
        fn = self.entity_description.attrs_fn
        return fn(self.engine) if fn else None

"""Sensor entities: reports, charging, the car's own estimate, and the range model."""

from __future__ import annotations

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
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .engine import FordEvConfigEntry, FordEvEngine
from .entity import FordEvEntity
from .periods import period_value
from .range_model import BY_TEMPERATURE, climate_kw, drive_kwh, model_soc, predicted_kwh_per_km
from .units import state_km, state_number

# data_age is the only polled sensor.
SCAN_INTERVAL = timedelta(seconds=60)
MIN_CONSUMPTION_KM = 10
CHARGING_STATES = {"A": "unplugged", "B": "plugged", "C": "charging"}


@dataclass(frozen=True, kw_only=True)
class FordEvSensorDescription(SensorEntityDescription):
    """A sensor computed from the engine."""

    value_fn: Callable[[FordEvEngine], StateType | datetime]
    attrs_fn: Callable[[FordEvEngine], dict[str, Any]] | None = None
    period: str | None = None
    currency: bool = False
    poll: bool = False


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
        "cycle_model_kwh": round(m.a * m.cycle_x_drive + m.b * m.cycle_x_hvac, 2),
    }


def _last_charge(engine: FordEvEngine) -> str | None:
    last = engine.state.last_session
    return str(last.get("summary", ""))[:255] if last else None


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
    _period_sensor("distance_day", "day", "odometer", _KM),
    _period_sensor("distance_week", "week", "odometer", _KM),
    _period_sensor("distance_month", "month", "odometer", _KM),
    _period_sensor("energy_used_week", "week", "energy_used", _ENERGY),
    _period_sensor("energy_used_month", "month", "energy_used", _ENERGY),
    _period_sensor("charger_energy_week", "week", "charger_energy", _ENERGY),
    _period_sensor("charger_energy_month", "month", "charger_energy", _ENERGY),
    _period_sensor("charger_energy_home_week", "week", "charger_energy_home", _ENERGY),
    _period_sensor("charger_energy_home_month", "month", "charger_energy_home", _ENERGY),
    _period_sensor("cost_week", "week", "cost", _MONEY),
    _period_sensor("cost_month", "month", "cost", _MONEY),
    FordEvSensorDescription(key="consumption_week", value_fn=_consumption("week"), **_CONSUMPTION),
    FordEvSensorDescription(key="consumption_month", value_fn=_consumption("month"), **_CONSUMPTION),
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
    async_add_entities(FordEvSensor(engine, description) for description in SENSORS)


class FordEvSensor(FordEvEntity, SensorEntity):
    """A value computed from the engine."""

    entity_description: FordEvSensorDescription

    def __init__(self, engine: FordEvEngine, description: FordEvSensorDescription) -> None:
        super().__init__(engine, description.key)
        self.entity_description = description
        if description.currency:
            self._attr_native_unit_of_measurement = engine.hass.config.currency
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

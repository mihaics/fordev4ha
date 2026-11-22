"""Independent range model, recalibrated at every charge to 100 %.

Trip energy = a * drive(km, T) + b * hours * climate(T, setpoint, on). At 100 % the battery is
full again, so the energy charged since the previous 100 % is the energy really used in that
cycle; a and b are refitted from it by ridge least squares with forgetting.

Pure functions, no Home Assistant dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

BASE_KWH_PER_KM = 0.16
REF_TEMP_C = 20
COLD_COEF = 0.01
HEAT_KW_PER_C = 0.12
COOL_KW_PER_C = 0.08
MIN_SPEED = 5
LAMBDA = 0.85
PRIOR_WEIGHT = 100
A_BOUNDS = (0.5, 2.0)
B_BOUNDS = (0.2, 3.0)
SOC_FULL = 99.5
FALLBACK_TEMP_C = 10.0
BY_TEMPERATURE = (-15, -10, -5, 0, 5, 10, 15, 20, 25, 30, 35)

MIN_TRIP_KM = 0.5
MAX_TRIP_HOURS = 12
MAX_TRIP_SPEED = 160
SPEED_BOUNDS = (10.0, 130.0)
MIN_CYCLE_CHARGED_KWH = 1.0
MIN_CALIBRATION_GAP_S = 6 * 3600
MIN_CYCLE_MODEL_KWH = 3.0
RATIO_BOUNDS = (0.5, 2.0)
CAPACITY_ESTIMATE_MAX_START_SOC = 80


def _clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


def drive_kwh(km: float, temp_c: float) -> float:
    """Baseline driving energy, rising 1 % per °C below 20 °C."""
    return km * BASE_KWH_PER_KM * (1 + COLD_COEF * max(0.0, REF_TEMP_C - temp_c))


def climate_kw(temp_c: float, setpoint_c: float, on: bool) -> float:
    """Cabin heating or cooling power."""
    if not on:
        return 0.0
    return HEAT_KW_PER_C * max(0.0, setpoint_c - temp_c) + COOL_KW_PER_C * max(0.0, temp_c - setpoint_c)


def predicted_kwh_per_km(
    temp_c: float, a: float, b: float, avg_speed: float, setpoint_c: float, climate_on: bool
) -> float:
    """Predicted consumption per km at the given conditions."""
    return a * drive_kwh(1, temp_c) + b * climate_kw(temp_c, setpoint_c, climate_on) / max(avg_speed, MIN_SPEED)


@dataclass
class RangeModelState:
    """Learned factors, fit sums, the current cycle and the model battery."""

    a: float = 1.0
    b: float = 1.0
    a0: float = 1.0
    b0: float = 1.0
    s11: float = 0.0
    s12: float = 0.0
    s22: float = 0.0
    s1y: float = 0.0
    s2y: float = 0.0
    calibrations: int = 0
    last_error_pct: float | None = None
    last_calibration: float | None = None
    last_result: dict[str, Any] | None = field(default=None)
    cycle_x_drive: float = 0.0
    cycle_x_hvac: float = 0.0
    cycle_charged: float = 0.0
    cycle_km: float = 0.0
    model_energy: float | None = None
    avg_speed: float = 45.0
    odo_accounted: float | None = None
    trip_start_ts: float | None = None
    trip_start_temp: float | None = None


@dataclass(frozen=True)
class TripInput:
    """One finished trip."""

    odometer_km: float
    start_ts: float | None
    end_ts: float
    start_temp_c: float | None
    end_temp_c: float
    setpoint_c: float
    climate_on: bool


@dataclass(frozen=True)
class CalibrationResult:
    """Outcome of one calibration at 100 %."""

    valid: bool
    ratio: float
    km: float
    charged_kwh: float
    predicted_kwh: float
    error_pct: float
    a: float
    b: float
    capacity_estimate_kwh: float | None
    soc_start_car: float | None
    soc_start_model: float | None
    soc_start_charger: float | None


def account_trip(st: RangeModelState, trip: TripInput, capacity_kwh: float) -> RangeModelState:
    """Charge one trip to the model and to the current cycle."""
    st = replace(st, trip_start_ts=None, trip_start_temp=None)
    if st.odo_accounted is None:
        return replace(st, odo_accounted=trip.odometer_km)
    km = trip.odometer_km - st.odo_accounted
    if km < 0:
        return replace(st, odo_accounted=trip.odometer_km)
    if km < MIN_TRIP_KM:
        return st

    hours_raw = (trip.end_ts - trip.start_ts) / 3600 if trip.start_ts is not None else 0.0
    valid = (
        trip.start_ts is not None
        and 0 < hours_raw < MAX_TRIP_HOURS
        and km / max(hours_raw, 0.01) < MAX_TRIP_SPEED
    )
    hours = hours_raw if valid else km / st.avg_speed
    if valid and trip.start_temp_c is not None:
        temp = (trip.start_temp_c + trip.end_temp_c) / 2
    else:
        temp = trip.end_temp_c

    x1 = drive_kwh(km, temp)
    x2 = hours * climate_kw(temp, trip.setpoint_c, trip.climate_on)
    model_energy = st.model_energy
    if model_energy is not None:
        model_energy = max(model_energy - (st.a * x1 + st.b * x2), -capacity_kwh)

    avg_speed = st.avg_speed
    if valid and hours >= 0.1 and km >= 2:
        avg_speed = _clamp(0.8 * avg_speed + 0.2 * km / hours, SPEED_BOUNDS)

    return replace(
        st,
        odo_accounted=trip.odometer_km,
        model_energy=model_energy,
        cycle_x_drive=st.cycle_x_drive + x1,
        cycle_x_hvac=st.cycle_x_hvac + x2,
        cycle_km=st.cycle_km + km,
        avg_speed=avg_speed,
    )


def add_charge(st: RangeModelState, kwh: float, capacity_kwh: float) -> RangeModelState:
    """Add energy delivered by the charger to the model battery and the current cycle."""
    model_energy = None if st.model_energy is None else min(st.model_energy + kwh, capacity_kwh)
    return replace(st, model_energy=model_energy, cycle_charged=st.cycle_charged + kwh)


def calibration_allowed(st: RangeModelState, now_ts: float) -> bool:
    """Enough charged this cycle, and not calibrated in the last 6 hours."""
    if st.cycle_charged < MIN_CYCLE_CHARGED_KWH:
        return False
    return st.last_calibration is None or now_ts - st.last_calibration >= MIN_CALIBRATION_GAP_S


def calibrate(
    st: RangeModelState,
    capacity_kwh: float,
    now_ts: float,
    session_kwh: float | None,
    session_start_soc_car: float | None,
    session_start_soc_model: float | None,
) -> tuple[RangeModelState, CalibrationResult]:
    """Refit the factors from the finished cycle and reset the model to full."""
    x1, x2, y = st.cycle_x_drive, st.cycle_x_hvac, st.cycle_charged
    pred = st.a * x1 + st.b * x2
    ratio = y / pred if pred > 0 else 0.0
    valid = x1 + x2 >= MIN_CYCLE_MODEL_KWH and RATIO_BOUNDS[0] <= ratio <= RATIO_BOUNDS[1]
    error_pct = _clamp((pred - y) / y * 100, (-100.0, 100.0)) if y > 0 else 0.0

    updates: dict[str, Any] = {}
    a, b = st.a, st.b
    if valid:
        w = PRIOR_WEIGHT
        s11 = LAMBDA * st.s11 + x1 * x1
        s12 = LAMBDA * st.s12 + x1 * x2
        s22 = LAMBDA * st.s22 + x2 * x2
        s1y = LAMBDA * st.s1y + x1 * y
        s2y = LAMBDA * st.s2y + x2 * y
        r1 = s1y + w * st.a0
        r2 = s2y + w * st.b0
        det = (s11 + w) * (s22 + w) - s12 * s12
        a = _clamp((r1 * (s22 + w) - s12 * r2) / det, A_BOUNDS)
        b = _clamp(((s11 + w) * r2 - s12 * r1) / det, B_BOUNDS)
        updates = {
            "s11": s11, "s12": s12, "s22": s22, "s1y": s1y, "s2y": s2y,
            "a": a, "b": b,
            "calibrations": st.calibrations + 1,
            "last_error_pct": error_pct,
        }

    capacity_estimate = None
    soc_start_charger = None
    if session_kwh is not None and session_kwh > 0:
        soc_start_charger = 100 - session_kwh / capacity_kwh * 100
        if session_start_soc_car is not None and session_start_soc_car < CAPACITY_ESTIMATE_MAX_START_SOC:
            capacity_estimate = session_kwh / (100 - session_start_soc_car) * 100

    result = CalibrationResult(
        valid=valid,
        ratio=ratio,
        km=st.cycle_km,
        charged_kwh=y,
        predicted_kwh=pred,
        error_pct=error_pct,
        a=a,
        b=b,
        capacity_estimate_kwh=capacity_estimate,
        soc_start_car=session_start_soc_car,
        soc_start_model=session_start_soc_model,
        soc_start_charger=soc_start_charger,
    )
    new_state = replace(
        st,
        **updates,
        model_energy=capacity_kwh,
        cycle_x_drive=0.0,
        cycle_x_hvac=0.0,
        cycle_charged=0.0,
        cycle_km=0.0,
        last_calibration=now_ts,
        last_result=asdict(result),
    )
    return new_state, result


def seed_drive_factor(soc: float | None, range_km: float | None, capacity_kwh: float, temp_c: float) -> float:
    """Initial drive factor from the car's own range estimate."""
    if soc is None or soc < 20 or range_km is None or range_km <= 0:
        return 1.0
    implied = soc / 100 * capacity_kwh / range_km
    return _clamp(implied / drive_kwh(1, temp_c), A_BOUNDS)


def model_soc(st: RangeModelState, capacity_kwh: float) -> float | None:
    """Model state of charge in %, clamped to 0-100."""
    if st.model_energy is None or capacity_kwh <= 0:
        return None
    return min(max(st.model_energy / capacity_kwh * 100, 0.0), 100.0)

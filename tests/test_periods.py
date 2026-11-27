"""Tests for day/week/month period snapshots."""

from datetime import date, datetime

import pytest

from custom_components.ford_ev.periods import (
    PeriodSnapshot,
    PeriodState,
    add_distance,
    period_start,
    period_value,
    roll_periods,
    set_distance_start,
)

T0 = {"odometer": 1000.0, "energy_used": 10.0}


def test_first_call_creates_zero_periods():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), T0)
    assert period_value(st, "week", "odometer", T0) == 0


def test_value_since_snapshot():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), T0)
    assert period_value(st, "day", "odometer", {**T0, "odometer": 1042.0}) == 42.0


def test_day_rollover_records_previous():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), T0)
    t1 = {"odometer": 1030.0, "energy_used": 14.0}
    st = roll_periods(st, datetime(2026, 11, 19, 0, 0, 5), t1)
    assert st.previous["day"] == {"odometer": 30.0, "energy_used": 4.0}
    assert period_value(st, "day", "odometer", t1) == 0


def test_week_starts_monday():
    assert period_start("week", datetime(2026, 11, 22, 12)) == date(2026, 11, 16)


def test_month_rollover_after_downtime():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 10, 30, 9), T0)
    st = roll_periods(st, datetime(2026, 11, 3, 9), {**T0, "odometer": 1100.0})
    assert st.previous["month"]["odometer"] == 100.0
    assert st.snapshots["month"].start == "2026-11-01"


def test_counter_added_late():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), {"energy_used": 1.0})
    st = roll_periods(st, datetime(2026, 11, 18, 10), {"energy_used": 1.0, "odometer": 500.0})
    assert period_value(st, "day", "odometer", {"energy_used": 1.0, "odometer": 510.0}) == 10.0


def test_missing_counter_value_is_none():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), {"energy_used": 1.0})
    assert period_value(st, "day", "odometer", {"energy_used": 1.0}) is None


def test_same_period_keeps_snapshot():
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), T0)
    st = roll_periods(st, datetime(2026, 11, 18, 23), {**T0, "odometer": 1050.0})
    assert period_value(st, "day", "odometer", {**T0, "odometer": 1050.0}) == 50.0
    assert "day" not in st.previous


# ---------------------------------------------------------------------- distance baselines


def test_add_distance_copies_odometer_baselines():
    st = PeriodState(
        {"month": PeriodSnapshot("2026-11-01", {"odometer": 1200.0, "energy_used": 0.0}), "day": PeriodSnapshot("2026-11-27", {"energy_used": 1.0})},
        {"day": {"odometer": 15.0, "energy_used": 4.0}},
    )
    out = add_distance(st)
    assert out.snapshots["month"].values["distance"] == 1200.0
    assert "distance" not in out.snapshots["day"].values
    assert out.previous["day"]["distance"] == 15.0


def test_add_distance_keeps_a_corrected_baseline():
    st = PeriodState({"year": PeriodSnapshot("2026-01-01", {"odometer": 1200.0, "distance": 10.0})}, {})
    assert add_distance(st).snapshots["year"].values["distance"] == 10.0


def test_set_distance_start_changes_only_distance():
    totals = {"odometer": 1290.0, "distance": 1290.0, "energy_used": 20.0}
    st = roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), {**totals, "odometer": 1200.0, "distance": 1200.0, "energy_used": 0.0})
    st = set_distance_start(st, "year", 10.0)
    assert period_value(st, "year", "distance", totals) == 1280.0
    assert period_value(st, "year", "odometer", totals) == 90.0
    assert period_value(st, "month", "distance", totals) == 90.0


def test_set_distance_start_needs_the_period():
    with pytest.raises(ValueError):
        set_distance_start(PeriodState({}, {}), "month", 10.0)


def test_corrected_baseline_ends_with_its_period():
    start = {"odometer": 1200.0, "distance": 1200.0}
    st = set_distance_start(roll_periods(PeriodState({}, {}), datetime(2026, 11, 18, 9), start), "month", 10.0)
    # Not seen again until February: the whole difference since the correction is one rollover.
    st = roll_periods(st, datetime(2027, 2, 3, 9), {"odometer": 2300.0, "distance": 2300.0})
    assert st.previous["month"] == {"odometer": 1100.0, "distance": 2290.0}
    assert st.snapshots["month"].values["distance"] == 2300.0

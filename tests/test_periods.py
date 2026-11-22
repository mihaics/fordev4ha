"""Tests for day/week/month period snapshots."""

from datetime import date, datetime

from custom_components.ford_ev.periods import PeriodState, period_start, period_value, roll_periods

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

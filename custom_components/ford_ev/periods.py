"""Day/week/month report periods from snapshots of cumulative counters.

Pure functions, no Home Assistant dependency. Weeks start on Monday.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

COUNTERS = ("odometer", "energy_used", "energy_added", "charger_energy", "charger_energy_home", "cost")
PERIODS = ("day", "week", "month")


@dataclass
class PeriodSnapshot:
    """Counter values at the start of a period."""

    start: str  # ISO date
    values: dict[str, float] = field(default_factory=dict)


@dataclass
class PeriodState:
    """Current snapshot and the previous period's totals, per period."""

    snapshots: dict[str, PeriodSnapshot] = field(default_factory=dict)
    previous: dict[str, dict[str, float]] = field(default_factory=dict)


def period_start(period: str, now_local: datetime) -> date:
    """First day of the period containing now_local."""
    today = now_local.date()
    if period == "day":
        return today
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    raise ValueError(f"unknown period {period}")


def roll_periods(st: PeriodState, now_local: datetime, totals: dict[str, float]) -> PeriodState:
    """Start new periods where the calendar moved on, recording the finished period's totals."""
    snapshots = dict(st.snapshots)
    previous = dict(st.previous)
    for period in PERIODS:
        start = period_start(period, now_local).isoformat()
        snap = snapshots.get(period)
        if snap is None:
            snapshots[period] = PeriodSnapshot(start, dict(totals))
            continue
        if snap.start != start:
            previous[period] = {c: totals[c] - snap.values[c] for c in snap.values if c in totals}
            snapshots[period] = PeriodSnapshot(start, dict(totals))
            continue
        missing = {c: v for c, v in totals.items() if c not in snap.values}
        if missing:
            snapshots[period] = PeriodSnapshot(snap.start, {**snap.values, **missing})
    return PeriodState(snapshots, previous)


def period_value(st: PeriodState, period: str, counter: str, totals: dict[str, float]) -> float | None:
    """Amount of a counter since the start of the current period."""
    snap = st.snapshots.get(period)
    if snap is None or counter not in snap.values or counter not in totals:
        return None
    return totals[counter] - snap.values[counter]

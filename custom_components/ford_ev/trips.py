"""Measured consumption per outside temperature and speed band, and statistics of the last trips.

Pure functions, no Home Assistant dependency.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

BIN_WIDTH_C = 5
# A band needs this many km before its consumption means anything: 1 % of charge is several km.
MIN_BIN_KM = 20.0
MIN_TOTAL_KM = 10.0

Bins = dict[str, dict[str, float]]


def bin_key(temp_c: float) -> str:
    """Lower bound of the temperature band, as the key used in storage."""
    return str(int(math.floor(temp_c / BIN_WIDTH_C) * BIN_WIDTH_C))


def add_to_bin(bins: Bins, key: str, km: float = 0.0, kwh: float = 0.0, cost: float = 0.0) -> None:
    """Add a trip, or energy that arrived after it, to a band."""
    band = bins.setdefault(key, {"km": 0.0, "kwh": 0.0, "cost": 0.0})
    band["km"] = band.get("km", 0.0) + km
    band["kwh"] = band.get("kwh", 0.0) + kwh
    band["cost"] = band.get("cost", 0.0) + cost


def bin_rows(bins: Bins) -> list[dict[str, Any]]:
    """One row per band, coldest first; consumption only where enough km were driven."""
    rows = []
    for key in sorted(bins, key=int):
        band = bins[key]
        km = band.get("km", 0.0)
        enough = km >= MIN_BIN_KM
        rows.append(
            {
                "t": int(key),
                "km": round(km),
                "kwh100": round(band.get("kwh", 0.0) / km * 100, 1) if enough else None,
                "cost100": round(band.get("cost", 0.0) / km * 100, 2) if enough else None,
            }
        )
    return rows


def measured_consumption(bins: Bins) -> float | None:
    """kWh/100 km over all bands."""
    km = sum(band.get("km", 0.0) for band in bins.values())
    if km < MIN_TOTAL_KM:
        return None
    return sum(band.get("kwh", 0.0) for band in bins.values()) / km * 100


SPEED_BANDS = (0, 30, 60, 90)
TRIP_LOG_SIZE = 50
SHORT_TRIP_KM = 10.0
# A trip's energy is in whole percent of charge: below this distance it says little about the model.
MIN_MODEL_ERROR_KM = 20.0
WEEK_S = 7 * 86400


def speed_key(speed_kmh: float) -> str:
    """Lower bound of the average-speed band, as the key used in storage."""
    return str(max(band for band in SPEED_BANDS if band <= max(speed_kmh, 0.0)))


def speed_rows(bins: Bins) -> list[dict[str, Any]]:
    """One row per average-speed band, slowest first; consumption only where enough km were driven."""
    rows = []
    for key in sorted(bins, key=int):
        km = bins[key].get("km", 0.0)
        rows.append(
            {
                "speed": int(key),
                "km": round(km),
                "kwh100": round(bins[key].get("kwh", 0.0) / km * 100, 1) if km >= MIN_BIN_KM else None,
            }
        )
    return rows


def trip_statistics(trips: list[dict[str, Any]], now_ts: float) -> dict[str, Any]:
    """Figures over the logged trips (the last TRIP_LOG_SIZE)."""
    if not trips:
        return {"trips": 0}
    kms = [t["km"] for t in trips]
    total = sum(kms)
    week = [t for t in trips if now_ts - t["end_ts"] <= WEEK_S]
    rated = [t["kwh"] / t["km"] * 100 for t in trips if t["km"] >= SHORT_TRIP_KM and t.get("kwh") is not None]
    long = [t for t in trips if t["km"] >= MIN_MODEL_ERROR_KM and t.get("kwh") and t.get("pred") is not None]
    measured = sum(t["kwh"] for t in long)
    return {
        "trips": len(trips),
        "trips_7d": len(week),
        "km_7d": round(sum(t["km"] for t in week), 1),
        "mean_km": round(total / len(trips), 1),
        "median_km": round(statistics.median(kms), 1),
        "longest_km": round(max(kms), 1),
        "short_trip_km_pct": round(sum(k for k in kms if k < SHORT_TRIP_KM) / total * 100) if total > 0 else None,
        "best_kwh100": round(min(rated), 1) if rated else None,
        "worst_kwh100": round(max(rated), 1) if rated else None,
        # Model minus measured over the longer trips: an error figure between full charges.
        "model_error_pct": round((sum(t["pred"] for t in long) - measured) / measured * 100, 1) if measured > 0 else None,
        "model_error_trips": len(long),
    }

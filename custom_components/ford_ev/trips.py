"""Measured consumption per outside temperature band, from finished trips.

Pure functions, no Home Assistant dependency.
"""

from __future__ import annotations

import math
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

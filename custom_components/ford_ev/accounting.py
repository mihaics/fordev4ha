"""Battery-side energy accounting from state of charge, and the charger energy counter.

Pure functions, no Home Assistant dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# A SoC rise of this many points while unplugged is treated as charging, not regeneration.
UNPLUGGED_CHARGE_POINTS = 5.0
REGEN_CREDIT_MAX = 100.0


@dataclass
class SocAccountingState:
    """Cumulative energy derived from SoC changes."""

    last_soc: float | None = None
    regen_credit: float = 0.0
    energy_used: float = 0.0
    energy_added: float = 0.0
    energy_added_home: float = 0.0


def apply_soc_change(
    st: SocAccountingState, new_soc: float, capacity_kwh: float, plugged: bool, home: bool
) -> SocAccountingState:
    """Turn one SoC change into kWh used or added.

    Deltas are taken against the last known SoC, so gaps while the car sleeps are not lost.
    Small rises while unplugged are regeneration: they are credited against the next drop
    instead of being counted as both added and used.
    """
    if st.last_soc is None:
        return replace(st, last_soc=new_soc)

    delta = new_soc - st.last_soc
    kwh_per_point = capacity_kwh / 100

    if delta > 0 and (plugged or delta >= UNPLUGGED_CHARGE_POINTS):
        added = delta * kwh_per_point
        return replace(
            st,
            last_soc=new_soc,
            energy_added=st.energy_added + added,
            energy_added_home=st.energy_added_home + (added if home else 0.0),
            regen_credit=0.0,
        )
    if delta > 0:
        return replace(st, last_soc=new_soc, regen_credit=min(st.regen_credit + delta, REGEN_CREDIT_MAX))
    if delta < 0:
        drop = -delta
        used = max(drop - st.regen_credit, 0.0) * kwh_per_point
        return replace(
            st,
            last_soc=new_soc,
            energy_used=st.energy_used + used,
            regen_credit=max(st.regen_credit - drop, 0.0),
        )
    return replace(st, last_soc=new_soc)


@dataclass(frozen=True)
class ChargeCost:
    """Price of energy the car reports as charged."""

    billed_kwh: float
    solar_kwh: float
    cost: float


def price_charge(
    kwh: float, loss_pct: float, price: float, solar_kwh_available: float = 0.0, solar_price: float = 0.0
) -> ChargeCost:
    """Cost of kwh reported by the car.

    The meter that bills the energy sees more than the car reports (charger and cable losses), so
    the billed energy is kwh plus loss_pct. Solar energy available at the time covers it first, at
    solar_price; the rest is priced at price.
    """
    billed = kwh * (1 + loss_pct / 100)
    solar = min(billed, max(solar_kwh_available, 0.0))
    return ChargeCost(billed, solar, (billed - solar) * price + solar * solar_price)


def charger_increment(last_reading: float | None, reading: float) -> float:
    """kWh to add for a new chargerEnergyOutput reading.

    The value counts kWh within one charging run and restarts near zero when a run restarts;
    its last value persists after unplugging. The first observation is a baseline, so a stale
    value present at install time is never counted.
    """
    if last_reading is None:
        return 0.0
    if reading >= last_reading:
        return reading - last_reading
    return reading

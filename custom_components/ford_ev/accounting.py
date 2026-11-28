"""Battery-side energy accounting from state of charge, and the charger energy counter.

Pure functions, no Home Assistant dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# A SoC rise of this many points while unplugged is treated as charging, not regeneration.
UNPLUGGED_CHARGE_POINTS = 5.0
REGEN_CREDIT_MAX = 100.0
# Charging history needed before its average price is used to value the battery.
SEED_MIN_KWH = 5.0


@dataclass
class SocAccountingState:
    """Cumulative energy derived from SoC changes."""

    last_soc: float | None = None
    regen_credit: float = 0.0
    energy_used: float = 0.0
    energy_added: float = 0.0


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


def consume_value(value: float, energy_before_kwh: float, used_kwh: float) -> tuple[float, float]:
    """Cost of used_kwh taken from a battery that held energy_before_kwh worth value.

    The battery is priced as one pool: every kWh in it costs the same, the average of what was
    paid for the energy charged into it. Returns (cost, remaining value); the value never goes
    below zero.
    """
    if value <= 0 or used_kwh <= 0:
        return 0.0, max(value, 0.0)
    share = 1.0 if energy_before_kwh <= 0 else min(used_kwh / energy_before_kwh, 1.0)
    cost = value * share
    return cost, value - cost


def seed_price(cost: float, charger_kwh: float, scenario_prices: list[float], home_price: float | None) -> float:
    """Price per kWh of the energy in the battery when cost tracking starts.

    The average paid so far, kept between the lowest and highest configured price; without
    enough charging history, the home price; else 0.
    """
    if charger_kwh >= SEED_MIN_KWH and cost > 0:
        price = cost / charger_kwh
        if scenario_prices:
            price = min(max(price, min(scenario_prices)), max(scenario_prices))
        return price
    return home_price or 0.0

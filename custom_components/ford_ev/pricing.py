"""Prices and solar surplus: what a kWh costs at each source, and the solar energy available.

A mixin of the engine. It reads the configured entities and options, and keeps the last value
of each price entity in the engine state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, UnitOfPower
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONF_AC_LOSS_PCT,
    CONF_BASE_LOAD_W,
    CONF_DC_LOSS_PCT,
    CONF_FUEL_PRICE_ENTITY,
    CONF_FUEL_PRICE_FIXED,
    CONF_HOUSE_LOAD_ENTITY,
    CONF_ICE_CONSUMPTION,
    CONF_PRICE_ENTITY,
    CONF_PRICE_FIXED,
    CONF_PUBLIC_PRICE_ENTITY,
    CONF_PUBLIC_PRICE_FIXED,
    CONF_SOLAR_POWER_ENTITY,
    CONF_SOLAR_PRICE_ENTITY,
    CONF_SOLAR_PRICE_FIXED,
)
from .units import state_number

if TYPE_CHECKING:
    from .state import EngineState

# Longest stretch of solar production credited to one charger reading.
SOLAR_WINDOW_MAX_S = 7200
_PRICE_ENTITY_KEYS = (CONF_PRICE_ENTITY, CONF_PUBLIC_PRICE_ENTITY, CONF_SOLAR_PRICE_ENTITY, CONF_FUEL_PRICE_ENTITY)


class PricingMixin:
    """Price and solar helpers of FordEvEngine."""

    hass: HomeAssistant
    config: dict[str, Any]
    state: EngineState
    capacity_kwh: float
    _solar_start_ts: float | None
    _solar_last_ts: float
    _solar_kw: float
    _solar_kwh: float

    def _configured_price(self, entity_key: str, fixed_key: str) -> float | None:
        entity_id = self.config.get(entity_key)
        if entity_id:
            value = state_number(self.hass.states.get(entity_id))
            if value is not None:
                self.state.last_prices[entity_id] = value
                return value
            if entity_id in self.state.last_prices:
                # The entity has no value now: a charge must not be priced at nothing.
                return self.state.last_prices[entity_id]
        fixed = self.config.get(fixed_key)
        return float(fixed) if fixed is not None else None

    def _record_prices(self) -> bool:
        """Remember the value of every price entity. True when one changed."""
        before = dict(self.state.last_prices)
        for entity_key in _PRICE_ENTITY_KEYS:
            self._configured_price(entity_key, "")
        return self.state.last_prices != before

    def price(self) -> float | None:
        """Electricity price per kWh at home.

        The price entity, else its last known value, else the fixed price, else None.
        """
        return self._configured_price(CONF_PRICE_ENTITY, CONF_PRICE_FIXED)

    def public_price(self) -> float | None:
        """Price per kWh away from home: the public price entity, else the fixed one, else None."""
        return self._configured_price(CONF_PUBLIC_PRICE_ENTITY, CONF_PUBLIC_PRICE_FIXED)

    def solar_price(self) -> float:
        """Value of a solar kWh used for charging; 0 when not set."""
        return self._configured_price(CONF_SOLAR_PRICE_ENTITY, CONF_SOLAR_PRICE_FIXED) or 0.0

    def fuel_price(self) -> float | None:
        """Price of a litre of fuel, for the comparison with a combustion car."""
        return self._configured_price(CONF_FUEL_PRICE_ENTITY, CONF_FUEL_PRICE_FIXED)

    def ice_consumption(self) -> float | None:
        """Consumption of the combustion car to compare with, in l/100 km."""
        value = self.config.get(CONF_ICE_CONSUMPTION)
        return float(value) if value else None

    def _has_price(self, entity_key: str, fixed_key: str) -> bool:
        return bool(self.config.get(entity_key)) or self.config.get(fixed_key) is not None

    def scenario_prices(self) -> dict[str, float | None] | None:
        """What a kWh reported by the car costs at each configured source, losses included.

        A source that is not configured is left out. None while a configured price is not
        known yet (its entity has no value after a restart).
        """
        ac, dc = 1 + self.loss_pct(False) / 100, 1 + self.loss_pct(True) / 100
        prices: dict[str, float | None] = {}
        if self._has_price(CONF_PRICE_ENTITY, CONF_PRICE_FIXED):
            price = self.price()
            if price is None:
                return None
            prices["home"] = price * ac
        if self.config.get(CONF_SOLAR_POWER_ENTITY):
            if self._has_price(CONF_SOLAR_PRICE_ENTITY, CONF_SOLAR_PRICE_FIXED):
                price = self._configured_price(CONF_SOLAR_PRICE_ENTITY, CONF_SOLAR_PRICE_FIXED)
                if price is None:
                    return None
                prices["solar"] = price * ac
            else:
                prices["solar"] = 0.0
        if self._has_price(CONF_PUBLIC_PRICE_ENTITY, CONF_PUBLIC_PRICE_FIXED):
            price = self.public_price()
            if price is None:
                return None
            prices["public"] = price * dc
        return prices

    def battery_price(self) -> float | None:
        """Average price per kWh of the energy now in the battery."""
        st = self.state
        if st.battery_value is None or st.soc.last_soc is None:
            return None
        energy = (st.soc.last_soc - st.soc.regen_credit) / 100 * self.capacity_kwh
        return st.battery_value / energy if energy > 0 else None

    def loss_pct(self, dc: bool) -> float:
        """Energy billed above what the car reports, in percent."""
        return float(self.config[CONF_DC_LOSS_PCT] if dc else self.config[CONF_AC_LOSS_PCT])

    def _power_kw(self, entity_id: str | None) -> float | None:
        """A power entity's value in kW, or None without a usable value."""
        state = self.hass.states.get(entity_id) if entity_id else None
        value = state_number(state)
        if value is None:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UnitOfPower.WATT  # type: ignore[union-attr]
        try:
            return PowerConverter.convert(value, unit, UnitOfPower.KILO_WATT)
        except Exception:  # noqa: BLE001 - an unknown unit means no estimate, never a crash
            return None

    def solar_surplus_kw(self) -> float | None:
        """Solar power left after the house's own use, or None without a solar power entity.

        The house's use is the house load entity (without the car), else the fixed base load.
        """
        kw = self._power_kw(self.config.get(CONF_SOLAR_POWER_ENTITY))
        if kw is None:
            return None
        house = self._power_kw(self.config.get(CONF_HOUSE_LOAD_ENTITY))
        if house is None:
            house = float(self.config[CONF_BASE_LOAD_W]) / 1000
        return max(kw - max(house, 0.0), 0.0)

    def _solar_reset(self, now_ts: float) -> None:
        self._solar_start_ts = self._solar_last_ts = now_ts
        self._solar_kwh = 0.0
        self._solar_kw = self.solar_surplus_kw() or 0.0

    def _solar_flush(self, now_ts: float) -> None:
        if self._solar_start_ts is None:
            return
        self._solar_kwh += self._solar_kw * max(now_ts - self._solar_last_ts, 0.0) / 3600
        self._solar_last_ts = now_ts

    @callback
    def _on_solar_change(self, _event: Event[EventStateChangedData]) -> None:
        self._solar_flush(dt_util.utcnow().timestamp())
        # No value means no solar energy, not the last value carried on.
        self._solar_kw = self.solar_surplus_kw() or 0.0

    def _solar_available(self, since: float | None, now_ts: float) -> float:
        """Solar surplus in kWh since `since`, at most SOLAR_WINDOW_MAX_S of it."""
        if since is None or now_ts <= since or not self.config.get(CONF_SOLAR_POWER_ENTITY):
            return 0.0
        elapsed = now_ts - since
        if self._solar_start_ts is None or abs(self._solar_start_ts - since) > 1.0:
            # Not followed over the whole step (a restart): the power now, for the step.
            return (self.solar_surplus_kw() or 0.0) * min(elapsed, SOLAR_WINDOW_MAX_S) / 3600
        self._solar_flush(now_ts)
        return self._solar_kwh * min(1.0, SOLAR_WINDOW_MAX_S / elapsed)

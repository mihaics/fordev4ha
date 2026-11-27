"""Tests for distance baselines: the upgrade migration and the set_period_odometer action."""

import copy

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.ford_ev.const import DOMAIN
from custom_components.ford_ev.periods import LIFETIME, period_value

from .conftest import async_setup_ford_ev
from .test_costs_trips import _store, state
from .test_engine import engine_of


def _km(hass, key):
    return float(state(hass, key).state)


async def _set_odometer(hass, entry_id=None, **data):
    if entry_id:
        data["config_entry_id"] = entry_id
    await hass.services.async_call(DOMAIN, "set_period_odometer", data, blocking=True)
    await hass.async_block_till_done()


def _store_041(hass_storage):
    """A 0.4.1 store: tracked since Wednesday 2026-11-18, the month never rolled, no distance counter."""
    zero = {"energy_used": 0.0, "cost": 0.0, "odometer": 1200.0}
    _store(hass_storage, "up", {
        "last_odometer_km": 1290.0,
        "periods": {
            "snapshots": {
                "day": {"start": "2026-11-27", "values": {"energy_used": 20.0, "cost": 0.0, "odometer": 1286.0}},
                "week": {"start": "2026-11-23", "values": {"energy_used": 2.0, "cost": 0.0, "odometer": 1220.0}},
                "month": {"start": "2026-11-01", "values": dict(zero)},
                "year": {"start": "2026-01-01", "values": dict(zero)},
                LIFETIME: {"start": "2026-11-18", "values": dict(zero)},
            },
            "previous": {"day": {"energy_used": 4.0, "odometer": 15.0}},
        },
        "model": {"odo_accounted": 1290.0},
    })


async def test_upgrade_keeps_distances(hass, fcq, freezer, hass_storage):
    """A 0.4.1 store has no distance counter: the distances stay what they were."""
    freezer.move_to("2026-11-27 10:00:00+00:00")
    await fcq.set("odometer", "1290")
    _store_041(hass_storage)
    entry = await async_setup_ford_ev(hass, fcq, entry_id="up")
    assert _km(hass, "distance_year") == pytest.approx(90.0)
    assert _km(hass, "distance_month") == pytest.approx(90.0)
    assert _km(hass, "distance_day") == pytest.approx(4.0)
    assert state(hass, "distance_day").attributes["previous_period"] == 15.0
    assert engine_of(entry).state.periods.snapshots["year"].values["distance"] == 1200.0


async def test_correction_changes_only_distance(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq, options={"price_fixed": 1.0})
    await fcq.set("soc", "40")
    await fcq.set("odometer", "1090")
    consumption = state(hass, "consumption_year").state
    cost_per_100 = state(hass, "cost_per_100km_year").state
    assert _km(hass, "distance_year") == pytest.approx(90.0)

    await _set_odometer(hass, period="year", odometer=10)

    assert _km(hass, "distance_year") == pytest.approx(1080.0)
    assert _km(hass, "distance_month") == pytest.approx(90.0)
    assert state(hass, "consumption_year").state == consumption
    assert state(hass, "cost_per_100km_year").state == cost_per_100
    assert state(hass, "cost_per_100km_year").attributes["km"] == 90.0

    # Kept across a restart.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert _km(hass, "distance_year") == pytest.approx(1080.0)
    assert period_value(engine_of(entry).state.periods, "year", "odometer", engine_of(entry).totals()) == pytest.approx(90.0)


async def test_rejected_correction_changes_nothing(hass, fcq, freezer):
    freezer.move_to("2026-11-27 10:00:00+00:00")
    entry = await async_setup_ford_ev(hass, fcq)
    engine = engine_of(entry)
    # The month ends while nothing triggers a roll.
    freezer.move_to("2026-12-02 10:00:00+00:00")
    before = copy.deepcopy(engine.state.periods)
    with pytest.raises(ServiceValidationError):
        await _set_odometer(hass, period="month", odometer=1500)
    assert engine.state.periods == before

    # A valid correction applies to the month that is current now, not the expired one.
    await _set_odometer(hass, period="month", odometer=990)
    month = engine.state.periods.snapshots["month"]
    assert month.start == "2026-12-01"
    assert month.values["distance"] == 990.0


async def test_correction_needs_a_known_odometer(hass, fcq):
    await fcq.set("odometer", "unknown")
    await async_setup_ford_ev(hass, fcq)
    with pytest.raises(ServiceValidationError):
        await _set_odometer(hass, period="year", odometer=0)


async def test_correction_with_two_vehicles(hass, fcq, fcq2):
    e1 = await async_setup_ford_ev(hass, fcq)
    e2 = await async_setup_ford_ev(hass, fcq2)
    with pytest.raises(ServiceValidationError):
        await _set_odometer(hass, period="year", odometer=0)
    await _set_odometer(hass, e2.entry_id, period="year", odometer=0)
    assert engine_of(e2).state.periods.snapshots["year"].values["distance"] == 0.0
    assert engine_of(e1).state.periods.snapshots["year"].values["distance"] == 1000.0


async def test_correction_in_miles_on_an_imperial_instance(hass, fcq):
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = await async_setup_ford_ev(hass, fcq)
    await _set_odometer(hass, period="year", odometer=10)
    assert engine_of(entry).state.periods.snapshots["year"].values["distance"] == pytest.approx(16.09344)


async def test_cost_rows_say_since_when_tracking_started_later(hass, fcq, freezer, hass_storage):
    freezer.move_to("2026-11-27 10:00:00+00:00")
    await fcq.set("odometer", "1290")
    _store_041(hass_storage)
    await async_setup_ford_ev(hass, fcq, entry_id="up")
    assert state(hass, "cost_per_100km_month").attributes["since"] == "2026-11-18"
    assert state(hass, "cost_per_100km_year").attributes["since"] == "2026-11-18"
    # The week began after tracking did.
    assert state(hass, "cost_per_100km_week").attributes["since"] is None
    assert state(hass, "cost_per_100km_total").attributes["since"] == "2026-11-18"

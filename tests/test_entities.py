"""Tests for sensor, binary sensor, number and switch entities."""

from datetime import date

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.ford_ev.const import DOMAIN, NOTIFY_KEYS
from custom_components.ford_ev.periods import period_start

from .conftest import VIN, async_setup_ford_ev

SENSOR_KEYS = [
    "energy_used", "energy_added", "charger_energy", "charger_energy_home", "charging_cost",
    "distance_day", "distance_week", "distance_month",
    "energy_used_week", "energy_used_month", "charger_energy_week", "charger_energy_month",
    "charger_energy_home_week", "charger_energy_home_month", "cost_week", "cost_month",
    "consumption_week", "consumption_month", "car_full_range", "car_km_per_percent",
    "charging_state", "data_age", "last_charge", "outside_temperature",
    "model_consumption", "model_soc", "model_range", "model_range_full", "model_range_full_tomorrow",
    "drive_factor", "climate_factor", "cycle_error", "calibrations", "last_calibration",
]


def eid(hass, domain, key):
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{VIN}_{key}")
    assert entity_id is not None, key
    return entity_id


def state(hass, domain, key):
    return hass.states.get(eid(hass, domain, key))


async def test_core_sensors_present(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    for key in SENSOR_KEYS:
        assert state(hass, "sensor", key) is not None, key
    assert state(hass, "sensor", "energy_used").attributes["state_class"] == "total_increasing"
    assert state(hass, "sensor", "charging_cost").attributes["unit_of_measurement"] == hass.config.currency
    assert state(hass, "sensor", "consumption_week").attributes["unit_of_measurement"] == "kWh/100km"


async def test_entity_ids_use_vehicle_name(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    assert eid(hass, "sensor", "energy_used") == "sensor.ford_ev_test_explorer_energy_used"


async def test_counters_update(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    await fcq.set("soc", "40")
    assert float(state(hass, "sensor", "energy_used").state) == pytest.approx(6.0)


async def test_consumption_none_under_10_km(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    await fcq.set("odometer", "1005")
    await fcq.set("soc", "48")
    assert state(hass, "sensor", "consumption_week").state == "unknown"
    assert float(state(hass, "sensor", "distance_week").state) == pytest.approx(5)


async def test_consumption_over_10_km(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    await fcq.set("odometer", "1020")
    await fcq.set("soc", "45")
    assert float(state(hass, "sensor", "consumption_week").state) == pytest.approx(15.0)


async def test_period_sensor_last_reset(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    monday = period_start("week", dt_util.now())
    expected = dt_util.start_of_local_day(date.fromisoformat(monday.isoformat())).isoformat()
    assert state(hass, "sensor", "distance_week").attributes["last_reset"] == expected


async def test_car_full_range(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    assert float(state(hass, "sensor", "car_full_range").state) == pytest.approx(400)
    assert float(state(hass, "sensor", "car_km_per_percent").state) == pytest.approx(4)


async def test_charging_state_enum(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    assert state(hass, "sensor", "charging_state").state == "unplugged"
    await fcq.set("evccstatus", "C")
    assert state(hass, "sensor", "charging_state").state == "charging"
    assert state(hass, "binary_sensor", "charging_session").state == "on"


async def test_model_sensors(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    # Seeded drive factor: 50 % of 60 kWh over 200 km = 0.15 kWh/km -> a = 0.15 / 0.16.
    # 20 °C, climate on at 21 °C, average speed 45: a * 0.16 + 0.12 / 45 kWh/km
    cons = (0.15 + 0.12 / 45) * 100
    assert float(state(hass, "sensor", "model_consumption").state) == pytest.approx(cons, abs=0.01)
    assert float(state(hass, "sensor", "model_soc").state) == pytest.approx(50)
    assert float(state(hass, "sensor", "model_range").state) == pytest.approx(30 / cons * 100, abs=0.5)
    assert float(state(hass, "sensor", "outside_temperature").state) == 20


async def test_model_range_full_by_temperature(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    rows = state(hass, "sensor", "model_range_full").attributes["by_temperature"]
    assert len(rows) == 11
    assert rows[0]["t"] == -15
    assert set(rows[0]) == {"t", "km", "kwh100"}


async def test_model_unknown_without_temperature(hass, fcq):
    hass.states.async_set("sensor.outside", "unavailable")
    await async_setup_ford_ev(hass, fcq)
    assert state(hass, "sensor", "model_consumption").state == "unknown"
    assert state(hass, "sensor", "model_range").state == "unknown"


async def test_number_writes_setting(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": eid(hass, "number", "climate_setpoint"), "value": 22}, blocking=True
    )
    assert entry.runtime_data.engine.state.settings.climate_setpoint == 22
    assert float(state(hass, "number", "climate_setpoint").state) == 22


async def test_switch_writes_notify_setting(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    for key in NOTIFY_KEYS:
        assert state(hass, "switch", f"notify_{key}") is not None
    assert state(hass, "switch", "notify_calibration").state == "off"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": eid(hass, "switch", "notify_night_activity")}, blocking=True
    )
    assert entry.runtime_data.engine.state.settings.notify["night_activity"] is False
    await hass.services.async_call("switch", "turn_off", {"entity_id": eid(hass, "switch", "climate_on")}, blocking=True)
    assert entry.runtime_data.engine.state.settings.climate_on is False


async def test_device_linked_to_fcq(hass, fcq):
    entry = await async_setup_ford_ev(hass, fcq)
    [device] = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert (DOMAIN, VIN) in device.identifiers
    assert device.via_device_id == fcq.device_id
    assert device.name == "Ford EV Test Explorer"


async def test_calibrations_sensor_exposes_current_cycle(hass, fcq, freezer):
    entry = await async_setup_ford_ev(hass, fcq)
    model = entry.runtime_data.engine.state.model
    model.cycle_km, model.cycle_charged, model.cycle_x_drive, model.cycle_x_hvac = 120.0, 5.5, 18.0, 2.0
    entry.runtime_data.engine.notify_listeners()
    await hass.async_block_till_done()
    attrs = state(hass, "sensor", "calibrations").attributes
    assert attrs["cycle_km"] == 120.0
    assert attrs["cycle_charged_kwh"] == 5.5
    assert attrs["cycle_model_kwh"] == pytest.approx(model.a * 18.0 + model.b * 2.0, abs=0.01)

"""Tests for the config and options flows."""

from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.ford_ev.const import DOMAIN

from .conftest import VIN

SETUP = "custom_components.ford_ev.async_setup_entry"


def _outside(hass):
    hass.states.async_set("sensor.outside", "5", {"unit_of_measurement": "°C", "device_class": "temperature"})


async def test_full_flow_creates_entry(hass, fcq):
    _outside(hass)
    with patch(SETUP, return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"device_id": fcq.device_id})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "settings"
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"battery_kwh": 60, "temperature_sensor": "sensor.outside"}
        )
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == "Test Explorer"
    assert r["result"].unique_id == VIN
    assert r["data"]["battery_kwh"] == 60
    assert r["data"]["vin"] == VIN
    assert r["data"]["device_id"] == fcq.device_id
    assert r["data"]["weather_entity"] is None


async def test_abort_without_fcq(hass):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] is FlowResultType.ABORT
    assert r["reason"] == "no_vehicles"


async def test_temperature_source_required(hass, fcq):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"device_id": fcq.device_id})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"battery_kwh": 60})
    assert r["type"] is FlowResultType.FORM
    assert r["errors"] == {"base": "temperature_source_required"}


async def test_weather_entity_alone_is_enough(hass, fcq):
    hass.states.async_set("weather.forecast_home", "sunny", {"temperature": 5, "temperature_unit": "°C"})
    with patch(SETUP, return_value=True):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {"device_id": fcq.device_id})
        r = await hass.config_entries.flow.async_configure(
            r["flow_id"], {"battery_kwh": 52, "weather_entity": "weather.forecast_home"}
        )
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"]["temperature_sensor"] is None


async def test_configured_vehicle_not_offered(hass, fcq):
    MockConfigEntry(domain=DOMAIN, unique_id=VIN, data={"vin": VIN}).add_to_hass(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] is FlowResultType.ABORT
    assert r["reason"] == "already_configured"


async def test_options_flow_saves(hass, fcq):
    _outside(hass)
    async_mock_service(hass, "notify", "mobile_app_test_phone")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VIN,
        data={"vin": VIN, "device_id": fcq.device_id, "battery_kwh": 60, "temperature_sensor": "sensor.outside", "weather_entity": None},
    )
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "init"
        r = await hass.config_entries.options.async_configure(
            r["flow_id"],
            {
                "notify_services": ["mobile_app_test_phone"],
                "price_fixed": 0.25,
                "alert_delay_min": 15,
                "battery_kwh": 60,
                "temperature_sensor": "sensor.outside",
            },
        )
        await hass.async_block_till_done()
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["price_fixed"] == 0.25
    assert entry.options["alert_delay_min"] == 15
    assert entry.options["notify_services"] == ["mobile_app_test_phone"]
    assert entry.options["reminder_time"] == "21:00:00"


async def test_options_flow_requires_temperature_source(hass, fcq):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VIN,
        data={"vin": VIN, "device_id": fcq.device_id, "battery_kwh": 60, "temperature_sensor": None, "weather_entity": None},
    )
    entry.add_to_hass(hass)
    r = await hass.config_entries.options.async_init(entry.entry_id)
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"battery_kwh": 60})
    assert r["errors"] == {"base": "temperature_source_required"}


async def test_options_flow_saves_cost_settings(hass, fcq):
    _outside(hass)
    hass.states.async_set("sensor.pv", "100", {"unit_of_measurement": "W", "device_class": "power"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=VIN,
        data={"vin": VIN, "device_id": fcq.device_id, "battery_kwh": 60, "temperature_sensor": "sensor.outside", "weather_entity": None},
        options={"public_price_fixed": 3.0, "solar_price_fixed": 0.5},
    )
    entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        r = await hass.config_entries.options.async_init(entry.entry_id)
        r = await hass.config_entries.options.async_configure(
            r["flow_id"],
            {
                "public_price_fixed": 2.55,
                "solar_power_entity": "sensor.pv",
                "base_load_w": 400,
                "ac_loss_pct": 9,
                "dc_loss_pct": 4.5,
                "battery_kwh": 60,
                "temperature_sensor": "sensor.outside",
            },
        )
        await hass.async_block_till_done()
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["public_price_fixed"] == 2.55
    assert entry.options["solar_power_entity"] == "sensor.pv"
    assert entry.options["base_load_w"] == 400
    assert (entry.options["ac_loss_pct"], entry.options["dc_loss_pct"]) == (9, 4.5)
    # A cleared optional field is stored as None, not kept from before.
    assert entry.options["solar_price_fixed"] is None

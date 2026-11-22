"""Tests for FordConnect Query vehicle discovery."""

from homeassistant.helpers import entity_registry as er

from custom_components.ford_ev.discovery import async_discover_vehicles, async_get_vehicle

from .conftest import FCQ_DOMAIN, VIN


async def test_discovers_vehicle(hass, fcq):
    [v] = await async_discover_vehicles(hass)
    assert v.vin == VIN
    assert v.name == "Test Explorer"
    assert v.device_id == fcq.device_id
    assert v.entities["soc"] == fcq.entity_id("soc")
    assert (FCQ_DOMAIN, VIN) in v.identifiers


async def test_renamed_entity_still_found(hass, fcq):
    er.async_get(hass).async_update_entity(fcq.entity_id("soc"), new_entity_id="sensor.my_car_battery")
    [v] = await async_discover_vehicles(hass)
    assert v.entities["soc"] == "sensor.my_car_battery"


async def test_incomplete_vehicle_skipped(hass, fcq):
    er.async_get(hass).async_update_entity(fcq.entity_id("odometer"), disabled_by=er.RegistryEntryDisabler.USER)
    assert await async_discover_vehicles(hass) == []


async def test_optional_missing_ok(hass, fcq):
    er.async_get(hass).async_remove(fcq.entity_id("indicators"))
    [v] = await async_discover_vehicles(hass)
    assert "indicators" not in v.entities


async def test_no_fcq(hass):
    assert await async_discover_vehicles(hass) == []


async def test_get_vehicle_by_device(hass, fcq):
    v = await async_get_vehicle(hass, fcq.device_id)
    assert v is not None and v.vin == VIN
    assert await async_get_vehicle(hass, "nope") is None


async def test_vin_device_name_replaced_by_model(hass):
    from .conftest import FakeFcq

    FakeFcq(hass, VIN, f"VIN: {VIN}", model="2027 Explorer EV")
    [v] = await async_discover_vehicles(hass)
    assert v.name == "Explorer EV"


async def test_user_given_name_preferred(hass):
    from homeassistant.helpers import device_registry as dr

    from .conftest import FakeFcq

    fake = FakeFcq(hass, VIN, f"VIN: {VIN}", model="2027 Explorer EV")
    dr.async_get(hass).async_update_device(fake.device_id, name_by_user="Family car")
    [v] = await async_discover_vehicles(hass)
    assert v.name == "Family car"


async def test_vin_device_name_without_model(hass):
    from .conftest import FakeFcq

    FakeFcq(hass, VIN, VIN)
    [v] = await async_discover_vehicles(hass)
    assert v.name == "Car"

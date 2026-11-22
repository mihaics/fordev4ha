"""Tests for the reset service and diagnostics."""

import json

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.ford_ev.const import DOMAIN

from .conftest import VIN, async_setup_ford_ev


async def test_reset_service(hass, fcq):
    await fcq.set("soc", "80")
    await fcq.set("elveh", "240")
    entry = await async_setup_ford_ev(hass, fcq)
    model = entry.runtime_data.engine.state.model
    model.a = 1.8
    model.calibrations = 4
    await hass.services.async_call(DOMAIN, "reset_range_model", {}, blocking=True)
    model = entry.runtime_data.engine.state.model
    assert model.calibrations == 0
    assert model.a == pytest.approx(1.25)


async def test_reset_with_entry_id(hass, fcq, fcq2):
    e1 = await async_setup_ford_ev(hass, fcq)
    e2 = await async_setup_ford_ev(hass, fcq2)
    e2.runtime_data.engine.state.model.calibrations = 2
    e1.runtime_data.engine.state.model.calibrations = 5
    await hass.services.async_call(DOMAIN, "reset_range_model", {"config_entry_id": e2.entry_id}, blocking=True)
    assert e2.runtime_data.engine.state.model.calibrations == 0
    assert e1.runtime_data.engine.state.model.calibrations == 5


async def test_reset_requires_entry_when_two(hass, fcq, fcq2):
    await async_setup_ford_ev(hass, fcq)
    await async_setup_ford_ev(hass, fcq2)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "reset_range_model", {}, blocking=True)


async def test_reset_rejects_unknown_entry(hass, fcq):
    await async_setup_ford_ev(hass, fcq)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "reset_range_model", {"config_entry_id": "nope"}, blocking=True)


async def test_diagnostics_has_no_vin(hass, fcq, hass_client):
    assert await async_setup_component(hass, "diagnostics", {})
    entry = await async_setup_ford_ev(hass, fcq, options={"notify_services": ["mobile_app_test_phone"]})
    await fcq.set("soc", "40")
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = json.dumps(diag).lower()
    assert VIN.lower() not in text
    assert "mobile_app" not in text
    assert diag["state"]["soc"]["energy_used"] == pytest.approx(6.0)
    assert "soc" in diag["vehicle_keys"]

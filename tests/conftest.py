"""Shared test fixtures."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

FCQ_DOMAIN = "fordconnect_query"
VIN = "WF0TESTVIN0000001"
VIN2 = "WF0TESTVIN0000002"

FCQ_KEYS: dict[str, str] = {
    "soc": "sensor",
    "elveh": "sensor",
    "odometer": "sensor",
    "ignitionstatus": "sensor",
    "evccstatus": "sensor",
    "elvehplug": "sensor",
    "elvehcharging": "sensor",
    "doorlock": "sensor",
    "doorstatus": "sensor",
    "windowposition": "sensor",
    "indicators": "sensor",
    "lastrefresh": "sensor",
    "tracker": "device_tracker",
}

DEFAULT_ATTRS: dict[str, dict[str, Any]] = {
    "soc": {"unit_of_measurement": "%"},
    "elveh": {"unit_of_measurement": "km"},
    "odometer": {"unit_of_measurement": "km"},
    "doorlock": {"frontLeft": "LOCKED", "frontRight": "LOCKED", "rearLeft": "LOCKED", "rearRight": "LOCKED"},
    "doorstatus": {"frontLeft": "CLOSED", "frontRight": "CLOSED", "rearLeft": "CLOSED", "rearRight": "CLOSED", "hood": "CLOSED"},
    "windowposition": {},
    "lastrefresh": {"device_class": "timestamp"},
    "tracker": {"latitude": 0.0, "longitude": 0.0, "source_type": "gps"},
}


def default_states() -> dict[str, str]:
    """Initial FCQ states of a parked, unplugged car at home."""
    return {
        "soc": "50",
        "elveh": "200",
        "odometer": "1000",
        "ignitionstatus": "OFF",
        "evccstatus": "A",
        "elvehplug": "DISCONNECTED",
        "elvehcharging": "NOT_PLUGGED_IN",
        "doorlock": "LOCKED",
        "doorstatus": "Closed",
        "windowposition": "Closed",
        "indicators": "0",
        "lastrefresh": dt_util.utcnow().isoformat(),
        "tracker": "home",
    }


class FakeFcq:
    """A FordConnect Query vehicle: config entry, device, registry entities and states."""

    def __init__(self, hass: HomeAssistant, vin: str, name: str) -> None:
        self.hass = hass
        self.vin = vin
        self.name = name
        self.entry = MockConfigEntry(domain=FCQ_DOMAIN, title=name, unique_id=vin)
        self.entry.add_to_hass(hass)
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=self.entry.entry_id, identifiers={(FCQ_DOMAIN, vin)}, name=name
        )
        self.device_id = device.id
        ent_reg = er.async_get(hass)
        for key, domain in FCQ_KEYS.items():
            ent_reg.async_get_or_create(
                domain,
                FCQ_DOMAIN,
                f"fcq_uid_{vin}_{key}",
                suggested_object_id=f"fcq_{vin.lower()}_{key}",
                config_entry=self.entry,
                device_id=self.device_id,
                translation_key=key,
            )
        for key, state in default_states().items():
            self._set(key, state, None)

    def entity_id(self, key: str) -> str:
        """Current entity ID of an FCQ key (follows renames)."""
        entity_id = er.async_get(self.hass).async_get_entity_id(FCQ_KEYS[key], FCQ_DOMAIN, f"fcq_uid_{self.vin}_{key}")
        assert entity_id is not None, key
        return entity_id

    def _set(self, key: str, state: str, attrs: dict[str, Any] | None) -> None:
        self.hass.states.async_set(self.entity_id(key), state, {**DEFAULT_ATTRS.get(key, {}), **(attrs or {})})

    async def set(self, key: str, state: str, attrs: dict[str, Any] | None = None) -> None:
        """Set an FCQ state (default attributes merged with attrs) and let listeners run."""
        self._set(key, state, attrs)
        await self.hass.async_block_till_done()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every test."""
    return


@pytest.fixture
def fcq(hass: HomeAssistant) -> FakeFcq:
    """One FCQ vehicle."""
    return FakeFcq(hass, VIN, "Test Explorer")


@pytest.fixture
def fcq2(hass: HomeAssistant) -> FakeFcq:
    """A second FCQ vehicle."""
    return FakeFcq(hass, VIN2, "Second Car")


async def async_setup_ford_ev(
    hass: HomeAssistant,
    fcq: FakeFcq,
    options: dict[str, Any] | None = None,
    entry_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Add and set up a Ford EV entry for a fake vehicle."""
    from custom_components.ford_ev.const import DOMAIN  # noqa: PLC0415

    if hass.states.get("sensor.outside") is None:
        hass.states.async_set("sensor.outside", "20", {"unit_of_measurement": "°C", "device_class": "temperature"})
    kwargs: dict[str, Any] = {"entry_id": entry_id} if entry_id else {}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=fcq.vin,
        title=fcq.name,
        data={
            "device_id": fcq.device_id,
            "vin": fcq.vin,
            "battery_kwh": 60.0,
            "temperature_sensor": "sensor.outside",
            "weather_entity": None,
            **(data or {}),
        },
        options=options or {},
        **kwargs,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry

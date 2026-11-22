"""Find FordConnect Query vehicles in the entity and device registries.

Entities are matched by platform and translation key, never by entity ID, so vehicles keep
working after the user renames entities.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import FCQ_DOMAIN

REQUIRED_KEYS = ("soc", "odometer", "ignitionstatus", "evccstatus", "tracker")
OPTIONAL_KEYS = (
    "elveh",
    "elvehplug",
    "elvehcharging",
    "doorlock",
    "doorstatus",
    "windowposition",
    "indicators",
    "lastrefresh",
)
_KNOWN_KEYS = frozenset(REQUIRED_KEYS + OPTIONAL_KEYS)
_UID_PREFIX = "fcq_uid_"


@dataclass(frozen=True)
class FcqVehicle:
    """One FCQ vehicle and the entity ID of each FCQ key it has."""

    device_id: str
    vin: str
    name: str
    identifiers: frozenset[tuple[str, str]]
    entities: Mapping[str, str]


def _vin_from_unique_id(unique_id: str, key: str) -> str | None:
    if not unique_id.startswith(_UID_PREFIX) or not unique_id.endswith(f"_{key}"):
        return None
    return unique_id.removeprefix(_UID_PREFIX).removesuffix(f"_{key}") or None


def _vehicles(hass: HomeAssistant) -> list[FcqVehicle]:
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    by_device: dict[str, dict[str, er.RegistryEntry]] = defaultdict(dict)
    for entry in ent_reg.entities.values():
        if entry.platform != FCQ_DOMAIN or entry.disabled_by is not None or entry.device_id is None:
            continue
        key = entry.translation_key
        if key in _KNOWN_KEYS:
            by_device[entry.device_id][key] = entry

    vehicles = []
    for device_id, entries in by_device.items():
        if not all(key in entries for key in REQUIRED_KEYS):
            continue
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        vin = _vin_from_unique_id(entries["soc"].unique_id, "soc")
        if vin is None:
            continue
        vehicles.append(
            FcqVehicle(
                device_id=device_id,
                vin=vin,
                name=device.name_by_user or device.name or vin,
                identifiers=frozenset(device.identifiers),
                entities={key: e.entity_id for key, e in entries.items()},
            )
        )
    return vehicles


async def async_discover_vehicles(hass: HomeAssistant) -> list[FcqVehicle]:
    """All FCQ vehicles that have every required entity enabled."""
    return _vehicles(hass)


async def async_get_vehicle(hass: HomeAssistant, device_id: str) -> FcqVehicle | None:
    """The FCQ vehicle on a device, if it is complete."""
    return next((v for v in _vehicles(hass) if v.device_id == device_id), None)

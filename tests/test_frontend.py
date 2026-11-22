"""Tests for serving and registering the dashboard strategy."""

import hashlib
from pathlib import Path
from unittest.mock import patch

from homeassistant.setup import async_setup_component

from custom_components.ford_ev.const import DOMAIN

STRATEGY_URL = "/ford_ev/ford-ev-strategy.js"
STRATEGY_FILE = Path(__file__).parents[1] / "custom_components" / "ford_ev" / "frontend" / "ford-ev-strategy.js"


async def test_strategy_served(hass, hass_client, fcq):
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_client()
    resp = await client.get(STRATEGY_URL)
    assert resp.status == 200
    assert "ll-strategy-dashboard-ford-ev" in await resp.text()


async def test_module_url_registered_when_frontend_loaded(hass):
    hass.config.components.add("frontend")
    with patch("custom_components.ford_ev.add_extra_js_url") as add:
        assert await async_setup_component(hass, DOMAIN, {})
    digest = hashlib.sha256(STRATEGY_FILE.read_bytes()).hexdigest()[:8]
    add.assert_called_once_with(hass, f"{STRATEGY_URL}?v=0.1.1-{digest}")


async def test_module_url_skipped_without_frontend(hass):
    with patch("custom_components.ford_ev.add_extra_js_url") as add:
        assert await async_setup_component(hass, DOMAIN, {})
    add.assert_not_called()

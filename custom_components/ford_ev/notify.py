"""Send notifications to notify services and notify entities."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_send(
    hass: HomeAssistant,
    services: list[str],
    entities: list[str],
    title: str,
    message: str,
    *,
    tag: str,
    channel: str,
    high_priority: bool,
    click_action: str | None,
) -> None:
    """Send one notification. A failing or missing target is logged and skipped."""
    for service in services:
        if not hass.services.has_service("notify", service):
            _LOGGER.warning("Notification target notify.%s does not exist; skipped", service)
            continue
        data: dict[str, Any] = {"title": title, "message": message}
        if service.startswith("mobile_app_"):
            extra: dict[str, Any] = {"tag": tag, "channel": channel}
            if click_action:
                extra["clickAction"] = click_action
            if high_priority:
                extra["priority"] = "high"
                extra["ttl"] = 0
            data["data"] = extra
        try:
            await hass.services.async_call("notify", service, data, blocking=True)
        except Exception:  # noqa: BLE001 - one broken target must not stop the others
            _LOGGER.warning("Sending to notify.%s failed", service, exc_info=True)
    if entities:
        try:
            await hass.services.async_call(
                "notify", "send_message", {"entity_id": entities, "title": title, "message": message}, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Sending to %s failed", ", ".join(entities), exc_info=True)

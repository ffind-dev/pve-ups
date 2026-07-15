"""Optional notifications: generic webhook.

Notifications are best-effort: a failure to notify must never affect the shutdown
logic, so every send is wrapped and only logged on error.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import logging

import httpx

from .config import Notifications

log = logging.getLogger("pve-usv.notify")


async def notify(notifications: Notifications, subject: str, body: str, payload: dict) -> None:
    """Fire the webhook notification, swallowing all errors."""
    hook = notifications.webhook
    if hook.enabled and hook.url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    hook.url,
                    json={"subject": subject, "body": body, "status": payload},
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("Webhook notification failed: %s", exc)

"""Proxmox VE API client.

Shuts down a node via ``POST /nodes/{node}/status {command: shutdown}`` using an
API token (``user@realm!tokenid``) that only needs the ``Sys.PowerMgmt`` privilege.
Node shutdown lets PVE stop the guests in an orderly fashion according to their own
shutdown configuration, so we do not have to iterate guests ourselves.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import HostConfig

log = logging.getLogger("pve-usv.proxmox")


@dataclass
class TestResult:
    ok: bool
    message: str
    has_power_mgmt: bool = False


def _auth_header(host: HostConfig) -> dict[str, str]:
    secret = host.token_secret.get_secret_value()
    return {"Authorization": f"PVEAPIToken={host.token_id}={secret}"}


def _client(host: HostConfig, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=host.api_url.rstrip("/") + "/api2/json",
        headers=_auth_header(host),
        verify=host.verify_tls,
        timeout=timeout,
    )


async def test_connection(host: HostConfig, timeout: float = 10.0) -> TestResult:
    """Validate URL, token and that the token can manage power on this node."""
    try:
        async with _client(host, timeout) as client:
            # Version endpoint confirms reachability + token validity.
            resp = await client.get("/version")
            if resp.status_code == 401:
                return TestResult(False, "Authentication failed (token invalid?)")
            resp.raise_for_status()

            # Check effective permissions for Sys.PowerMgmt on this node.
            perm = await client.get("/access/permissions")
            has_power = False
            if perm.status_code == 200:
                data = perm.json().get("data", {})
                for path in (f"/nodes/{host.name}", "/nodes", "/"):
                    if data.get(path, {}).get("Sys.PowerMgmt"):
                        has_power = True
                        break

            if not has_power:
                return TestResult(
                    True,
                    "Connection ok, but the 'Sys.PowerMgmt' privilege could not be "
                    "confirmed for this node. A shutdown might be rejected.",
                    has_power_mgmt=False,
                )
            return TestResult(True, "Connection and 'Sys.PowerMgmt' privilege ok.", has_power_mgmt=True)

    except httpx.HTTPStatusError as exc:
        return TestResult(False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        return TestResult(False, f"Connection error: {exc}")


async def shutdown_node(host: HostConfig, timeout: float = 60.0) -> tuple[bool, str]:
    """Issue an orderly node shutdown. Returns (ok, message)."""
    try:
        async with _client(host, timeout) as client:
            resp = await client.post(
                f"/nodes/{host.name}/status", data={"command": "shutdown"}
            )
            if resp.status_code in (200, 201):
                return True, "Shutdown command accepted"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        log.error("Shutdown of %s failed: %s", host.name, exc)
        return False, f"Error: {exc}"

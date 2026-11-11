"""Dispatch between the shutdown target types.

The engine shuts down through this module and never imports a specific client, so
adding a target is: a model in config.py (a member of the ``ShutdownTarget`` union),
a module with ``shutdown_node``/``test_connection``, a branch here, and the matching
i18n keys.

Both entry points are *total*: they never raise, and they never run longer than their
timeout. That second guarantee is what keeps the targets independent — one machine
that accepts a connection and then goes quiet must not delay another machine's
shutdown, nor the poll loop that keeps the battery countdown running.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio

from . import linuxssh, pbs, proxmox
from .config import HostConfig, LinuxHostConfig, PbsHostConfig, PveHostConfig
from .proxmox import NodeVerdict, TestResult

# Headroom over the caller's timeout: the client's own httpx timeout should be what
# actually fires, so its error message survives. This deadline is the backstop for
# everything httpx does not bound (TLS handshakes, redirect chains, event-loop stalls).
DEADLINE_GRACE_S = 5.0


def _unsupported(host: HostConfig) -> str:
    return f"Unsupported shutdown target type: {getattr(host, 'type', '?')}"


async def shutdown(
    host: HostConfig, timeout: float, use_localhost: bool = False
) -> tuple[bool, str]:
    """Shut down one target. Never raises, never exceeds ``timeout`` by more than the grace.

    ``use_localhost`` only means anything to PVE: it addresses the node behind the entry's
    API URL directly instead of by its configured name (see proxmox.shutdown_node). PBS
    ignores the segment entirely and always has, and an SSH target has no node path at
    all, so the flag never reaches either.
    """
    if isinstance(host, PbsHostConfig):
        call = pbs.shutdown_node(host, timeout)
    elif isinstance(host, LinuxHostConfig):
        call = linuxssh.shutdown_node(host, timeout)
    elif isinstance(host, PveHostConfig):
        call = proxmox.shutdown_node(host, timeout, use_localhost=use_localhost)
    else:
        # Unknown type: report a failure instead of guessing a product. Sending a PVE
        # token to something else would be a silent, wrong action on a real machine.
        return False, _unsupported(host)

    try:
        return await asyncio.wait_for(call, timeout=timeout + DEADLINE_GRACE_S)
    except (asyncio.TimeoutError, TimeoutError):
        return False, f"No response within {timeout + DEADLINE_GRACE_S:.0f}s — gave up"


async def verify_node(host: HostConfig, timeout: float = 10.0) -> NodeVerdict:
    """Check a target's configured node name against its API. Never raises, always bounded.

    Only Proxmox VE has a node name that can be wrong. PBS ignores the path segment
    entirely (see PbsHostConfig.api_node) and a Linux target is reached by address, so
    there is nothing to verify and nothing to report — "unverified" is the honest answer, not a defect.
    """
    if not isinstance(host, PveHostConfig):
        return NodeVerdict(state="unverified")
    try:
        return await asyncio.wait_for(
            proxmox.verify_node(host, timeout=timeout), timeout=timeout + DEADLINE_GRACE_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        return NodeVerdict(
            state="unverified",
            detail=f"No response within {timeout + DEADLINE_GRACE_S:.0f}s — gave up",
        )


async def test_connection(host: HostConfig, timeout: float = 10.0) -> TestResult:
    """Verify token + power-management privilege. Never raises, always bounded."""
    if isinstance(host, PbsHostConfig):
        call = pbs.test_connection(host, timeout)
    elif isinstance(host, LinuxHostConfig):
        call = linuxssh.test_connection(host, timeout)
    elif isinstance(host, PveHostConfig):
        call = proxmox.test_connection(host, timeout)
    else:
        return TestResult(False, _unsupported(host))

    try:
        return await asyncio.wait_for(call, timeout=timeout + DEADLINE_GRACE_S)
    except (asyncio.TimeoutError, TimeoutError):
        return TestResult(False, f"No response within {timeout + DEADLINE_GRACE_S:.0f}s — gave up")

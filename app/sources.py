"""Dispatch between the UPS source types.

The engine polls through this module and never imports a specific source, so adding a
way to read a UPS is: a model in config.py (a member of the ``UpsSource`` union), a
module with ``poll``/``probe``, a branch here, and the matching i18n keys.

Every source produces the same ``UpsState``, which is why the trigger logic, the host
policy, the fail-safe rules and their whole test suite apply unchanged.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import nut, ups
from .config import NutConfig, SnmpConfig, UpsBase
from .ups import ProbeResult, UpsState

log = logging.getLogger("pve-usv.sources")

# Headroom over what a source can legitimately spend, exactly like targets.DEADLINE_GRACE_S:
# the source's own finer timeouts are what should normally fire, so their error messages
# survive. This is the backstop for what they do not bound.
POLL_GRACE_S = 5.0


def poll_budget_s(cfg: UpsBase) -> float:
    """Longest a single poll of this source may legitimately take, in seconds.

    Derived from the source's OWN settings rather than from the poll interval. That
    distinction is the whole point: SNMP in "auto" mode may issue two sequential GETs, each
    costing timeout_s x (retries + 1), so ~12 s with the defaults — well past the 8 s
    battery interval and still perfectly healthy. Cutting a poll off at the interval would
    turn working installations into permanently "unreachable" ones, which is an alarm and
    a fail-safe refusal to shut down.

    A profile read one object per request (ups.MibProfile.single_varbind) fits the same
    budget: it pays a round trip per object, and its first timeout ends the poll.
    """
    if isinstance(cfg, SnmpConfig):
        # Two profiles' worth of GETs under "auto", each timeout_s per try plus retries.
        return max(1.0, float(cfg.timeout_s)) * (max(0, int(cfg.retries)) + 1) * 2
    if isinstance(cfg, NutConfig):
        # Login plus the variable list. Each read is bounded individually, but the list
        # loop is not bounded as a whole — a upsd that answers one line just inside the
        # timeout could otherwise hold the poll for the better part of an hour.
        return max(1.0, float(cfg.timeout_s)) * 8
    return 10.0


async def poll(cfg: UpsBase) -> UpsState:
    """Read one UPS. Never raises, and never runs longer than its budget plus the grace.

    The bound matters as much as the totality. The engine polls every source in one
    gather and then does everything else — countdowns, host eligibility, the staged
    shutdown — sequentially behind it, so a single source that accepts a connection and
    then goes quiet used to freeze the whole decision engine, for every other UPS too.
    """
    try:
        return await asyncio.wait_for(
            _poll(cfg), timeout=poll_budget_s(cfg) + POLL_GRACE_S
        )
    except (asyncio.TimeoutError, TimeoutError):
        # Same outcome as any other failed read: unreachable is an alarm, never a shutdown.
        return UpsState(error=f"No answer within {poll_budget_s(cfg) + POLL_GRACE_S:.0f}s")
    except Exception as exc:  # noqa: BLE001 - the poll loop must never see an exception
        log.warning("Poll of %s failed: %s", getattr(cfg, "label", "?"), exc)
        return UpsState(error=str(exc))


async def _poll(cfg: UpsBase) -> UpsState:
    if isinstance(cfg, NutConfig):
        return await nut.poll(cfg)
    if isinstance(cfg, SnmpConfig):
        return await ups.poll(cfg)
    # Unknown type: stay unreachable, which is an alarm and never a shutdown.
    return UpsState(error=f"Unsupported UPS source type: {getattr(cfg, 'type', '?')}")


def poll_hint(cfg: UpsBase, state: UpsState, result: ProbeResult) -> Optional[str]:
    """Advice for when the production poll and the per-object probe disagree, if any."""
    if isinstance(cfg, SnmpConfig):
        return ups.batching_hint(cfg, state, result)
    return None


async def probe(cfg: UpsBase) -> ProbeResult:
    """Per-object diagnosis for the manual test button. Never raises."""
    if isinstance(cfg, NutConfig):
        return await nut.probe(cfg)
    if isinstance(cfg, SnmpConfig):
        return await ups.probe(cfg)
    return ProbeResult(summary=f"Unsupported UPS source type: {getattr(cfg, 'type', '?')}")

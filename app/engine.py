"""Decision engine and background poll loop.

Per-UPS state machine (each configured UPS is evaluated independently)::

    ONLINE  ->  ON_BATTERY  ->  (UPS triggered)

A host is shut down based on *its* feeding UPS devices (``HostConfig.ups_ids``) and
its policy (``ups_policy``): ``"all"`` (redundant PSUs — shut down only when every
feed has triggered, the default) or ``"any"`` (shut down as soon as one feed
triggers). A return to mains on any required feed aborts a not-yet-committed shutdown
(hysteresis, no flapping). SNMP unreachability raises an alarm but never triggers a
shutdown on its own (fail safe); a trigger already fired on fresh data, however, stays
latched while the UPS is unreachable (blind = never downgrade). When ``dry_run`` is set, the engine logs what it
*would* do and latches per host until power returns, instead of actually shutting
hosts down.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import __version__, db, notify, proxmox
from .config import AppConfig, HostConfig, SnmpConfig
from .ups import UpsState, poll

log = logging.getLogger("pve-usv.engine")

ONLINE = "ONLINE"
ON_BATTERY = "ON_BATTERY"
SHUTDOWN_PENDING = "SHUTDOWN_PENDING"
SHUTTING_DOWN = "SHUTTING_DOWN"

# Per-UPS battery timers survive a service restart via this file (next to events.db).
# Without it, a restart during a "blind" outage (on battery, then SNMP lost) would drop
# the running countdown and never shut down — even though the outage was confirmed.
STATE_PATH = db.DB_PATH.parent / "engine-state.json"
STATE_RESTORE_MAX_AGE_H = 24  # discard persisted timers older than this


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _UpsRuntime:
    """Per-UPS runtime state (one instance per configured UPS, keyed by ``SnmpConfig.id``)."""

    state: UpsState = field(default_factory=UpsState)
    on_battery_since: Optional[datetime] = None
    unreachable_count: int = 0
    unreachable_since: Optional[datetime] = None  # wall clock, for the comms-loss opt-in
    alarm_active: bool = False
    last_reachable: Optional[bool] = None  # for connect/disconnect logging
    comm_loss_fired: bool = False  # latch for the optional comms-loss alarm wording
    triggered: bool = False  # this UPS currently demands a shutdown of its hosts
    trigger_reason: Optional[str] = None


class Engine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.started_at = _now()
        self.state = ONLINE

        # Per-UPS runtime, keyed by SnmpConfig.id.
        self.ups_rt: dict[str, _UpsRuntime] = {}
        self._sync_runtimes()

        # Re-arm battery timers + latched triggers from the state file (None = nothing
        # written yet, so the first _evaluate always persists and clears a stale file).
        self._persisted_state: Optional[dict] = None
        self._restore_state()

        # Aggregate shutdown bookkeeping (any host fired this episode).
        self.shutdown_triggered = False
        self.shutdown_reason: Optional[str] = None
        self.triggered_at: Optional[datetime] = None

        # Per-host latch: do not re-fire while still pending. host_states holds the
        # committed (real) shutdown result per host.
        self.host_fired: dict[str, bool] = {}
        self.host_states: dict[str, dict] = {}

        # Daily housekeeping: keep the event log bounded.
        self.last_prune_date = None  # type: ignore[var-annotated]

        # Daily self-test of the Proxmox API credentials.
        self.last_selftest_date = None  # type: ignore[var-annotated]
        self.last_selftest_at: Optional[datetime] = None
        self.last_selftest_ok: Optional[bool] = None

        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # -- runtime/config sync -------------------------------------------------
    def _sync_runtimes(self) -> None:
        """Ensure there is exactly one _UpsRuntime per configured UPS id."""
        ids = {u.id for u in self.cfg.ups if u.id}
        for uid in ids:
            self.ups_rt.setdefault(uid, _UpsRuntime())
        for uid in list(self.ups_rt):
            if uid not in ids:
                del self.ups_rt[uid]

    # -- battery-timer persistence (survives service restarts) ---------------
    def _restore_state(self) -> None:
        """Best-effort: re-arm per-UPS battery timers from the state file at startup."""
        try:
            if not STATE_PATH.exists():
                return
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            entries = data.get("on_battery_since", {})
            triggers = data.get("trigger_reason", {})
            now = _now()
            for uid, ts in entries.items():
                rt = self.ups_rt.get(uid)
                if rt is None or not isinstance(ts, str):
                    continue
                try:
                    since = datetime.fromisoformat(ts)
                except ValueError:
                    continue
                if since.tzinfo is None or since > now:
                    continue
                if now - since > timedelta(hours=STATE_RESTORE_MAX_AGE_H):
                    continue
                rt.on_battery_since = since
                # A latched trigger (e.g. battery low before the restart) is re-armed
                # together with its timer, so the restart cannot demote it back to the
                # remaining countdown.
                reason = triggers.get(uid)
                if isinstance(reason, str) and reason:
                    rt.triggered = True
                    rt.trigger_reason = reason
                self._log_quiet(
                    "On-battery timer restored",
                    f"UPS {uid}: on battery since {ts} (from state file after restart). "
                    + (
                        f"Latched trigger persists: {reason}."
                        if rt.triggered
                        else "The countdown continues."
                    ),
                    db.WARNING,
                )
        except Exception as exc:  # noqa: BLE001 - a broken state file must never block startup
            log.warning("Engine state restore failed: %s", exc)

    def _persist_state(self) -> None:
        """Best-effort: write the per-UPS battery timers + latched triggers on change."""
        current = {
            "on_battery_since": {
                uid: rt.on_battery_since.isoformat()
                for uid, rt in self.ups_rt.items()
                if rt.on_battery_since is not None
            },
            # Only triggers tied to a running battery timer are worth restoring: a pure
            # comms-loss trigger re-arms from unreachable_since, which a restart resets.
            "trigger_reason": {
                uid: rt.trigger_reason
                for uid, rt in self.ups_rt.items()
                if rt.triggered and rt.trigger_reason and rt.on_battery_since is not None
            },
        }
        if current == self._persisted_state:
            return
        try:
            tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
            tmp.write_text(json.dumps(current), encoding="utf-8")
            os.replace(tmp, STATE_PATH)
            self._persisted_state = current
        except Exception as exc:  # noqa: BLE001 - persistence must never affect the loop
            log.warning("Engine state persist failed: %s", exc)

    @property
    def alarm_active(self) -> bool:
        """Aggregate alarm: True if any UPS is in alarm state."""
        return any(rt.alarm_active for rt in self.ups_rt.values())

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="pve-usv-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def update_config(self, cfg: AppConfig) -> None:
        """Apply a new config (from the UI) without restarting the loop."""
        self.cfg = cfg
        self._sync_runtimes()

    def reset(self) -> None:
        """Clear shutdown latches and alarms — used after a dry-run test."""
        self.shutdown_triggered = False
        self.shutdown_reason = None
        self.triggered_at = None
        self.host_fired = {}
        self.host_states = {}
        for rt in self.ups_rt.values():
            rt.alarm_active = False
            rt.comm_loss_fired = False
            rt.triggered = False
            rt.trigger_reason = None
        self._recompute_state()

    # -- main loop -----------------------------------------------------------
    async def _loop(self) -> None:
        log.info("Engine started (dry_run=%s)", self.cfg.dry_run)
        while not self._stop.is_set():
            try:
                self._sync_runtimes()
                # Freeze the UPS list for this iteration: a config save during the awaited
                # polls may swap self.cfg, and zipping against the NEW list would assign
                # results to the wrong UPS ids.
                ups_list = list(self.cfg.ups)
                if ups_list:
                    results = await asyncio.gather(*(poll(u) for u in ups_list))
                    for u, st in zip(ups_list, results):
                        rt = self.ups_rt.get(u.id)
                        if rt is not None:
                            rt.state = st
                await self._evaluate()
                await self._maybe_selftest()
                self._maybe_prune()
            except Exception as exc:  # noqa: BLE001
                log.exception("Engine iteration failed: %s", exc)

            # Keep the fast battery cadence while we believe ANY UPS is on battery — even if
            # one just went blind (unreachable) mid-outage — so the countdown stays responsive.
            on_battery_now = any(
                rt.state.on_battery or rt.on_battery_since is not None
                for rt in self.ups_rt.values()
            )
            interval = (
                self.cfg.thresholds.poll_interval_battery_s
                if on_battery_now
                else self.cfg.thresholds.poll_interval_normal_s
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # -- evaluation ----------------------------------------------------------
    async def _evaluate(self) -> None:
        self._sync_runtimes()

        # Phase A: evaluate every UPS independently.
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is not None:
                await self._evaluate_ups(u, rt)

        # Phase B + C: per-host eligibility and shutdown execution.
        await self._evaluate_hosts()

        self._recompute_state()
        self._persist_state()

    async def _evaluate_ups(self, u: SnmpConfig, rt: _UpsRuntime) -> None:
        """Per-UPS state machine: connectivity logging, fail-safe alarm, battery timer,
        and the resulting ``rt.triggered`` / ``rt.trigger_reason``."""
        th = self.cfg.effective_thresholds(u)
        st = rt.state
        name = u.label

        # --- log every connectivity change (the first poll, None, is silent) -----
        if rt.last_reachable is not None and st.reachable != rt.last_reachable:
            if st.reachable:
                await self._emit(
                    f"{name}: network connection restored",
                    "SNMP response received.",
                    db.INFO,
                )
            else:
                await self._emit(
                    f"{name}: network connection lost",
                    f"No SNMP response ({st.error or 'timeout'}).",
                    db.WARNING,
                )
        rt.last_reachable = st.reachable

        # --- unreachable handling: alarm only (fail safe) -----------------------
        if not st.reachable:
            rt.unreachable_count += 1
            if rt.unreachable_since is None:
                rt.unreachable_since = _now()

            # A trigger fired on fresh data (battery low / charge / runtime) stays latched
            # while blind — the battery only drains further, so never downgrade it to the
            # time countdown. Otherwise recompute so a blind on-battery countdown / armed
            # comms-loss opt-in can still arm this UPS. Cleared only on fresh data again
            # (mains return or a reachable re-evaluation).
            if not rt.triggered:
                rt.trigger_reason = self._ups_trigger_reason(u, rt)
                rt.triggered = rt.trigger_reason is not None

            # A confirmed on-battery outage whose time-based countdown survives a comms loss.
            shutdown_pending_blind = (
                th.keep_shutdown_on_comm_loss
                and th.on_battery_seconds is not None
                and rt.on_battery_since is not None
            )

            if rt.unreachable_count >= th.unreachable_alarm_after_polls and not rt.alarm_active:
                rt.alarm_active = True
                if rt.triggered:
                    await self._emit(
                        f"{name} unreachable — trigger stays latched",
                        f"No SNMP response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). The already fired trigger persists: "
                        f"{rt.trigger_reason}.",
                        db.WARNING,
                    )
                elif shutdown_pending_blind:
                    remaining = self._ups_countdown_remaining_s(u, rt)
                    await self._emit(
                        f"{name} unreachable — on-battery countdown continues",
                        f"No SNMP response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). The on-battery countdown keeps running "
                        f"on the local clock — shutdown when it expires"
                        + (f" (~{remaining} s)." if remaining is not None else "."),
                        db.WARNING,
                    )
                elif th.comm_loss_shutdown_after_min is not None:
                    await self._emit(
                        f"{name} unreachable — shutdown on prolonged loss",
                        f"No SNMP response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). No power outage confirmed, but if the "
                        f"communication loss persists, a shutdown will be triggered after "
                        f"{th.comm_loss_shutdown_after_min} min.",
                        db.WARNING,
                    )
                else:
                    await self._emit(
                        f"{name} unreachable",
                        f"No SNMP response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). NO shutdown will be triggered.",
                        db.WARNING,
                    )

            # Latch the opt-in comms-loss event once it has armed the trigger.
            if (
                th.comm_loss_shutdown_after_min is not None
                and rt.triggered
                and not rt.comm_loss_fired
            ):
                rt.comm_loss_fired = True
            return

        # Reachable again — clear alarm/comms-loss latches.
        rt.alarm_active = False
        rt.comm_loss_fired = False
        rt.unreachable_count = 0
        rt.unreachable_since = None

        # --- power restored -----------------------------------------------------
        if not st.on_battery:
            if rt.on_battery_since is not None or rt.triggered:
                await self._emit(
                    f"{name}: mains power restored",
                    "UPS is back on mains power.",
                    db.INFO,
                )
            rt.on_battery_since = None
            rt.triggered = False
            rt.trigger_reason = None
            return

        # --- on battery ---------------------------------------------------------
        if rt.on_battery_since is None:
            rt.on_battery_since = _now()
            await self._emit(
                f"Power outage — {name} on battery",
                f"Runtime remaining ~{st.runtime_remaining_min} min, "
                f"charge {st.battery_charge_pct}%.",
                db.WARNING,
            )

        rt.trigger_reason = self._ups_trigger_reason(u, rt)
        rt.triggered = rt.trigger_reason is not None

    async def _evaluate_hosts(self) -> None:
        """Per-host eligibility (Phase B) and shutdown execution (Phase C).

        Eligible, not-yet-fired hosts are processed in ``ordered_hosts()`` order, which
        places the appliance's own host last — so "own host last" holds within the
        current batch automatically.
        """
        eligible: list[tuple[HostConfig, str]] = []
        for host in self.cfg.ordered_hosts():
            reason = self._host_trigger_reason(host)
            committed = self.host_states.get(host.name, {}).get("shutdown_state") in (
                "sent",
                "failed",
            )

            if reason is None:
                # No longer eligible (a required feed recovered): release a not-yet-committed
                # (dry-run) latch so the dashboard can recover. A real, sent shutdown stays.
                if self.host_fired.get(host.name) and not committed:
                    self.host_fired[host.name] = False
                    await self._emit(
                        f"Host {host.name}: shutdown aborted",
                        "Feeding UPS device(s) sufficient again — shutdown no longer needed.",
                        db.INFO,
                    )
                continue

            if self.host_fired.get(host.name):
                continue  # already fired this episode
            eligible.append((host, reason))

        for host, reason in eligible:
            await self._fire_host(host, reason)

        # Clear the aggregate once nothing is pending/committed anymore.
        if not any(self.host_fired.values()):
            self.shutdown_triggered = False
            self.shutdown_reason = None
            self.triggered_at = None

    def _host_trigger_reason(self, host: HostConfig) -> Optional[str]:
        """A host is eligible when its feeds satisfy its policy.

        ``all`` (default, redundant PSUs): every feed must have triggered.
        ``any``: at least one feed has triggered.
        Empty ``ups_ids`` falls back to "all configured UPS".
        """
        feed_ids = self.cfg.feed_ids_for(host)
        rts = [(uid, self.ups_rt[uid]) for uid in feed_ids if uid in self.ups_rt]
        if not rts:
            return None
        fired = [(uid, rt) for uid, rt in rts if rt.triggered]
        if host.ups_policy == "any":
            ready = len(fired) >= 1
        else:  # "all"
            ready = len(fired) == len(rts)
        if not ready:
            return None

        def _label(uid: str) -> str:
            u = self.cfg.ups_by_id(uid)
            return u.label if u else uid

        return "; ".join(f"{_label(uid)}: {rt.trigger_reason}" for uid, rt in fired)

    def _ups_elapsed_on_battery(self, rt: _UpsRuntime) -> Optional[int]:
        """Prefer the UPS counter; fall back to our own timer."""
        if rt.state.seconds_on_battery is not None:
            return rt.state.seconds_on_battery
        if rt.on_battery_since is not None:
            return int((_now() - rt.on_battery_since).total_seconds())
        return None

    def _ups_trigger_reason(self, u: SnmpConfig, rt: _UpsRuntime) -> Optional[str]:
        """Whether (and why) a single UPS currently demands a shutdown.

        These thresholds must never fire on mains, so a UPS recharging after an outage is
        not mistaken for a reason to shut down. While unreachable, only the on_battery_seconds
        timer (blind countdown) or the opt-in pure comms-loss can match — the fresh UpsState
        carries no runtime/charge/battery_low data.
        """
        th = self.cfg.effective_thresholds(u)
        st = rt.state

        # Opt-in: a prolonged *pure* comms loss is treated as an outage (independent of battery).
        # Wall clock, not poll count: the loop cadence varies (battery interval), so counting
        # polls would misestimate the elapsed time.
        if (
            not st.reachable
            and th.comm_loss_shutdown_after_min is not None
            and rt.unreachable_since is not None
        ):
            elapsed_min = (_now() - rt.unreachable_since).total_seconds() / 60
            if elapsed_min >= th.comm_loss_shutdown_after_min:
                return (
                    f"SNMP communication lost for ~{int(elapsed_min)} min "
                    f"(threshold {th.comm_loss_shutdown_after_min} min)"
                )

        on_battery = st.on_battery or rt.on_battery_since is not None
        if not on_battery:
            return None

        # Blind (unreachable) while on battery: only the time-based countdown can match, and
        # only when keep_shutdown_on_comm_loss is enabled.
        if not st.reachable:
            if not (th.keep_shutdown_on_comm_loss and th.on_battery_seconds is not None):
                return None
            elapsed = self._ups_elapsed_on_battery(rt)
            if elapsed is not None and elapsed >= th.on_battery_seconds:
                return (
                    f"on battery for {elapsed} s ≥ {th.on_battery_seconds} s "
                    f"(SNMP lost while on battery, countdown kept running)"
                )
            return None

        # Reachable and on battery: the full threshold set.
        if th.on_battery_low and st.battery_low:
            return f"UPS reports '{st.battery_status}'"

        if th.runtime_below_minutes is not None and st.runtime_remaining_min is not None:
            if st.runtime_remaining_min <= th.runtime_below_minutes:
                return (
                    f"runtime remaining {st.runtime_remaining_min} min ≤ {th.runtime_below_minutes} min"
                )

        if th.charge_below_percent is not None and st.battery_charge_pct is not None:
            if st.battery_charge_pct <= th.charge_below_percent:
                return f"charge {st.battery_charge_pct}% ≤ {th.charge_below_percent}%"

        if th.on_battery_seconds is not None:
            elapsed = self._ups_elapsed_on_battery(rt)
            if elapsed is not None and elapsed >= th.on_battery_seconds:
                return f"on battery for {elapsed} s ≥ {th.on_battery_seconds} s"

        return None

    def _ups_countdown_remaining_s(self, u: SnmpConfig, rt: _UpsRuntime) -> Optional[int]:
        th = self.cfg.effective_thresholds(u)
        # Once this UPS has triggered (battery low, charge/runtime threshold, ...), the
        # time-based countdown is moot — hiding it keeps the UI from suggesting the
        # shutdown would wait for it.
        if rt.triggered:
            return None
        if th.on_battery_seconds is None:
            return None
        if not rt.state.on_battery and rt.on_battery_since is None:
            return None
        elapsed = self._ups_elapsed_on_battery(rt)
        if elapsed is None:
            return None
        return max(0, th.on_battery_seconds - elapsed)

    def _ups_comm_loss_remaining_s(self, u: SnmpConfig, rt: _UpsRuntime) -> Optional[int]:
        """Seconds until the opt-in *pure* comms-loss shutdown fires for this UPS, or None."""
        th = self.cfg.effective_thresholds(u)
        if th.comm_loss_shutdown_after_min is None or rt.comm_loss_fired:
            return None
        if rt.state.reachable or rt.unreachable_since is None:
            return None
        elapsed_s = (_now() - rt.unreachable_since).total_seconds()
        return max(0, int(th.comm_loss_shutdown_after_min * 60 - elapsed_s))

    def _recompute_state(self) -> None:
        committed = any(
            st.get("shutdown_state") in ("sent", "failed") for st in self.host_states.values()
        )
        if committed:
            self.state = SHUTTING_DOWN
        elif any(self.host_fired.values()):
            self.state = SHUTDOWN_PENDING
        elif any(
            rt.state.on_battery or rt.on_battery_since is not None
            for rt in self.ups_rt.values()
        ):
            self.state = ON_BATTERY
        else:
            self.state = ONLINE

    async def simulate_shutdown(self) -> str:
        """Log/notify what a shutdown *would* do, without touching the state machine.

        Safe to call at any time, including during a real outage: it never sets a host
        latch and so cannot suppress a genuine shutdown.
        """

        def _desc(h: HostConfig) -> str:
            feeds = [self.cfg.ups_by_id(i) for i in self.cfg.feed_ids_for(h)]
            labels = "+".join(u.label for u in feeds if u) or "(no UPS)"
            policy = "AND" if h.ups_policy == "all" else "OR"
            return f"{h.name} [{labels}, {policy}]"

        hosts = ", ".join(_desc(h) for h in self.cfg.ordered_hosts()) or "(no hosts)"
        msg = f"Test (dry-run): order {hosts}. NOTHING was shut down."
        await self._emit("Test shutdown executed", msg, db.WARNING)
        return msg

    def _maybe_prune(self) -> None:
        """Trim the event log once per day so events.db stays bounded over months."""
        today = datetime.now().date()
        if self.last_prune_date == today:
            return
        self.last_prune_date = today
        try:
            db.prune()
        except Exception as exc:  # noqa: BLE001 - housekeeping must never affect the loop
            log.warning("Event log prune failed: %s", exc)

    # -- daily self-test of the Proxmox API credentials ---------------------
    async def _maybe_selftest(self) -> None:
        """Run the credential self-test once per day at/after the configured hour."""
        cfg = self.cfg
        if not cfg.selftest_enabled or not cfg.hosts:
            return
        now = datetime.now()  # server local time, matches selftest_hour
        if self.last_selftest_date == now.date() or now.hour < cfg.selftest_hour:
            return
        self.last_selftest_date = now.date()
        await self._run_selftest()

    async def _run_selftest(self) -> None:
        """Verify token + Sys.PowerMgmt per host. Success is logged quietly (no notify),
        failure is emitted (notify) so a broken credential is noticed."""
        ok_all = True
        for host in self.cfg.ordered_hosts():
            result = await proxmox.test_connection(host)
            if result.ok and result.has_power_mgmt:
                self._log_quiet(f"Self-test {host.name}: ok", result.message, db.INFO)
            else:
                ok_all = False
                sev = db.WARNING if result.ok else db.CRITICAL
                await self._emit(f"Self-test {host.name}: FAILED", result.message, sev)
        self.last_selftest_ok = ok_all
        self.last_selftest_at = _now()

    def _log_quiet(self, subject: str, body: str, severity: str) -> None:
        """Write an event without firing notifications (for routine successes)."""
        log.info("%s — %s", subject, body)
        try:
            db.log_event(subject, body, severity)
        except Exception as exc:  # noqa: BLE001
            log.warning("Event log write failed: %s", exc)

    # -- shutdown execution --------------------------------------------------
    async def _fire_host(self, host: HostConfig, reason: str) -> None:
        """Shut down a single host (or latch/log it in dry-run)."""
        self.host_fired[host.name] = True
        if not self.shutdown_triggered:
            self.shutdown_triggered = True
            self.triggered_at = _now()
        self.shutdown_reason = f"{host.name}: {reason}"

        if self.cfg.dry_run:
            await self._emit(
                "DRY-RUN: shutdown would be triggered",
                f"Host {host.name} — reason: {reason}. NOTHING will be shut down.",
                db.CRITICAL,
            )
            return

        self.host_states.setdefault(host.name, {})
        ok, msg = await proxmox.shutdown_node(
            host, timeout=self.cfg.thresholds.host_shutdown_timeout_s
        )
        self.host_states[host.name] = {
            "shutdown_state": "sent" if ok else "failed",
            "last_action_at": _now().isoformat(),
            "last_error": None if ok else msg,
            "reachable": ok,
            "this_host": host.this_host,
            "order": host.order,
        }
        await self._emit(
            f"Host {host.name}: shutdown {'sent' if ok else 'FAILED'}",
            f"Reason: {reason}. {msg}",
            db.INFO if ok else db.CRITICAL,
        )

    # -- notifications + event log ------------------------------------------
    async def _emit(self, subject: str, body: str, severity: str) -> None:
        log.log(
            logging.WARNING if severity != db.INFO else logging.INFO,
            "%s — %s",
            subject,
            body,
        )
        try:
            db.log_event(subject, body, severity)
        except Exception as exc:  # noqa: BLE001
            log.warning("Event log write failed: %s", exc)
        await notify.notify(self.cfg.notifications, f"[PVE-UPS] {subject}", body, self.snapshot())

    # -- status snapshot for the REST API -----------------------------------
    def _ups_snapshot(self, u: SnmpConfig, rt: _UpsRuntime) -> dict:
        st = rt.state
        th = self.cfg.effective_thresholds(u)
        return {
            "id": u.id,
            "name": u.label,
            "reachable": st.reachable,
            "manufacturer": st.manufacturer,
            "model": st.model,
            "last_poll": st.last_poll.isoformat() if st.last_poll else None,
            "poll_interval_s": (
                self.cfg.thresholds.poll_interval_battery_s
                if st.on_battery
                else self.cfg.thresholds.poll_interval_normal_s
            ),
            "power_source": st.power_source,
            "battery_status": st.battery_status,
            "runtime_remaining_min": st.runtime_remaining_min,
            "battery_charge_pct": st.battery_charge_pct,
            "seconds_on_battery": self._ups_elapsed_on_battery(rt),
            "triggered": rt.triggered,
            "trigger_reason": rt.trigger_reason,
            "countdown_remaining_s": self._ups_countdown_remaining_s(u, rt),
            "comm_loss_remaining_s": self._ups_comm_loss_remaining_s(u, rt),
            "alarm": rt.alarm_active,
            "error": st.error,
        }

    def _aggregate_countdown_s(self) -> Optional[int]:
        vals = []
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is None:
                continue
            v = self._ups_countdown_remaining_s(u, rt)
            if v is not None:
                vals.append(v)
        return min(vals) if vals else None

    def _aggregate_comm_loss_s(self) -> Optional[int]:
        vals = []
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is None:
                continue
            v = self._ups_comm_loss_remaining_s(u, rt)
            if v is not None:
                vals.append(v)
        return min(vals) if vals else None

    def snapshot(self) -> dict:
        ups_list = []
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is not None:
                ups_list.append(self._ups_snapshot(u, rt))

        hosts = []
        for h in self.cfg.ordered_hosts():
            st = self.host_states.get(h.name, {})
            feed_ids = self.cfg.feed_ids_for(h)
            feeds = []
            for uid in feed_ids:
                u = self.cfg.ups_by_id(uid)
                rt = self.ups_rt.get(uid)
                feeds.append(
                    {
                        "id": uid,
                        "name": u.label if u else uid,
                        "triggered": bool(rt.triggered) if rt else False,
                    }
                )
            hosts.append(
                {
                    "name": h.name,
                    "this_host": h.this_host,
                    "order": h.order,
                    "ups_ids": list(h.ups_ids),
                    "ups_policy": h.ups_policy,
                    "feeds": feeds,
                    "eligible": self._host_trigger_reason(h) is not None,
                    "pending_reason": self._host_trigger_reason(h),
                    "reachable": st.get("reachable"),
                    "shutdown_state": st.get("shutdown_state", "idle"),
                    "last_action_at": st.get("last_action_at"),
                    "last_error": st.get("last_error"),
                }
            )

        return {
            "appliance": {
                "version": __version__,
                "uptime_s": int((_now() - self.started_at).total_seconds()),
                "engine_state": self.state,
                "dry_run": self.cfg.dry_run,
                "config_valid": self.cfg.configured,
                "alarm": self.alarm_active,
                "last_selftest_at": (
                    self.last_selftest_at.isoformat() if self.last_selftest_at else None
                ),
                "last_selftest_ok": self.last_selftest_ok,
            },
            "ups": ups_list,
            "shutdown": {
                "triggered": self.shutdown_triggered,
                "reason": self.shutdown_reason,
                "triggered_at": self.triggered_at.isoformat() if self.triggered_at else None,
                "countdown_remaining_s": self._aggregate_countdown_s(),
                "comm_loss_remaining_s": self._aggregate_comm_loss_s(),
            },
            "hosts": hosts,
        }

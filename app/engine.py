"""Decision engine and background poll loop.

Per-UPS state machine (each configured UPS is evaluated independently)::

    ONLINE  ->  ON_BATTERY  ->  (UPS triggered)

A host is shut down based on *its* feeding UPS devices (``HostConfig.ups_ids``) and
its policy (``ups_policy``): ``"all"`` (redundant PSUs — shut down only when every
feed has triggered, the default) or ``"any"`` (shut down as soon as one feed
triggers). A return to mains on any required feed aborts a not-yet-committed shutdown
(hysteresis, no flapping). An unreachable UPS raises an alarm but never triggers a
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
import socket
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple, Optional

from itertools import groupby

from . import __version__, cluster, db, history, notify, targets
from .config import AppConfig, HostConfig, PveHostConfig, Thresholds, UpsBase
from .sources import poll
from .ups import UpsState

log = logging.getLogger("pve-usv.engine")

ONLINE = "ONLINE"
ON_BATTERY = "ON_BATTERY"
SHUTDOWN_PENDING = "SHUTDOWN_PENDING"
SHUTTING_DOWN = "SHUTTING_DOWN"

# Per-UPS battery timers survive a service restart via this file (next to events.db).
# Without it, a restart during a "blind" outage (on battery, then contact lost) would drop
# the running countdown and never shut down — even though the outage was confirmed.
STATE_PATH = db.DB_PATH.parent / "engine-state.json"
STATE_RESTORE_MAX_AGE_H = 24  # discard persisted timers older than this

# How often one host is asked to shut down before the engine gives up on it for this
# episode. A failure is not proof that the request did not arrive — targets.shutdown()
# reports one when its deadline expires while the node was merely slow, and a busy
# pveproxy answers 503 — so a single attempt used to cost the whole machine: it stayed
# up until the battery died while the dashboard showed the episode as handled. Re-sending
# to a node that is already going down is harmless.
#
# Attempts normally land one poll apart. The exception is the pass _evaluate_hosts runs
# just before the ``this_host`` stage: nothing follows that stage — the appliance loses
# power — so a host that failed earlier in the same sweep would otherwise never see its
# second attempt at all. That one extra stage is the only retry cost shutdown_budget_s()
# accounts for; see its docstring for why the rest are deliberately left out.
MAX_SHUTDOWN_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local_now() -> datetime:
    """Naive *local* wall clock, kept separate from _now().

    The self-test schedule is a wall-clock time the user configures, so it has to follow
    the container's timezone; durations and timestamps stay UTC (_now()). Also the single
    seam the schedule tests patch.
    """
    return datetime.now()


def _hostname() -> str:
    """This appliance's host name, for matching it against the cluster's guest list.

    Total by construction: a name is a convenience here (the guest is normally picked
    explicitly in the settings), so every way of not getting one ends in an empty string
    rather than an exception on the poll loop.
    """
    try:
        name = socket.gethostname()
    except Exception:  # noqa: BLE001
        name = ""
    if not name:
        try:
            name = open("/etc/hostname", encoding="utf-8").read()
        except Exception:  # noqa: BLE001
            name = ""
    return name.strip()


@dataclass
class ShutdownBudget:
    """Worst-case seconds from "trigger fires" to "the last node was told to go".

    A breakdown rather than one number, because the warning that quotes it has to show its
    working: an operator who is told "raise the trigger" needs to see which term is large.
    It used to print an arithmetic of its own that no longer matched the total.
    """

    stages: int = 1
    host_s: int = 0
    cluster_s: int = 0
    inspect_s: int = 0
    notify_s: int = 0

    @property
    def total(self) -> int:
        return self.host_s + self.cluster_s + self.inspect_s + self.notify_s

    def explain(self) -> str:
        """The terms, in the order they run, for the caller to put in parentheses."""
        parts = []
        if self.cluster_s:
            parts.append(f"cluster preparation {self.cluster_s}s")
        if self.inspect_s:
            parts.append(f"cluster inspection {self.inspect_s}s")
        parts.append(f"{self.stages} node stage(s) {self.host_s}s")
        if self.notify_s:
            parts.append(f"notifications {self.notify_s}s")
        return " + ".join(parts)


class ClusterSwitches(NamedTuple):
    """The cluster switches as ONE answer per cluster (see Engine._cluster_switches)."""

    ceph: bool        # Ceph maintenance flags — and with them, always, the guest stop
    disarm: bool      # disarm the HA manager
    unit: bool        # shut the whole cluster down as a unit
    guest_stop: bool  # the cluster-wide guest stop on its own, for a cluster without Ceph

    @property
    def guests(self) -> bool:
        """Whether anything asks for the guest stop: the Ceph switch includes it."""
        return self.ceph or self.guest_stop


@dataclass
class GuestPlan:
    """Whether the cluster-wide guest stop runs on one cluster, and if not, why not.

    Decided in one place (Engine._guest_plan) for the preparation, its dry-run, the
    preview and the self-test: when each of them derived it on its own, the preview
    promised a guest stop the preparation then skipped for want of an HA disarm.
    """

    asked: bool = False       # a switch asks for it (Ceph, or the guest-stop tick)
    requested: bool = False   # ... and this cluster can take it (Ceph present if only Ceph asked)
    block: str = ""           # why a stop that was asked for will not run; "" = it runs
    self_guest: Optional["cluster.GuestInfo"] = None
    source: str = ""          # how self_guest was (not) found, see cluster.find_self_guest
    needs_disarm: bool = False  # HA manages guests here, so HA has to be disarmed first

    @property
    def want(self) -> bool:
        return self.requested and not self.block


# How many events can be emitted one after another between a trigger firing and the last
# shutdown stage, NOT counting the stages themselves: the outage notice, the four
# cluster-preparation events, and the abort or partial-shutdown notice. Engine._emit()
# AWAITS the notification round (deliberately — the appliance powers itself off last, so
# an unsent event is an event nobody receives), and notify.NOTIFY_BUDGET_S bounds one
# round. A dead chat server therefore costs real battery, and the number an operator sizes
# a reserve against has to say so.
#
# Each shutdown stage adds one more round on top of this: every _fire_host() emits its
# own "shutdown sent", and while the hosts inside one stage emit concurrently, the stages
# themselves are sequential. shutdown_budget() therefore adds the stage count — a
# constant alone could not, and understated a four-stage estate by three rounds.
NOTIFY_EVENTS_ON_PATH = 6

# History sampling (#37): one row per UPS a minute on mains, one per poll while on battery,
# and one at every change of state in between. The buffer cap bounds what piles up in
# memory while the writer thread is stuck on a disk that does not answer.
HISTORY_INTERVAL_S = 60
HISTORY_BUFFER_MAX = 20000


def shutdown_budget(cfg: AppConfig) -> ShutdownBudget:
    """The worst case, broken down. Pure, so every caller quotes the same figures.

    It counts what actually runs in SEQUENCE, which is what it used to get wrong: one
    host timeout and one cluster preparation, when both are sequential loops. Hosts go in
    stages — everything sharing an ``order`` at once, the appliance's own host alone at
    the end — so four distinct order values cost four timeouts, not one. An operator
    sizes their battery reserve against this figure, and it was several times too small.

    One retry stage is counted, and only one. _evaluate_hosts() gives hosts that failed
    earlier in the sweep a second attempt immediately before the ``this_host`` stage,
    because no poll follows that stage to carry the retry. It runs concurrently, so it
    costs one more host timeout, and it is worth counting for the same reason the stages
    are: the operator sizes a battery reserve against this number.

    The cluster inspection is counted where a cluster is configured: on a cold start (no
    self-test since the last restart, or a configuration saved just before the outage) the
    preparation reads the cluster from inside the shutdown path. Since 4.1.0 that is one
    read per cluster rather than one per node, which is what makes it a term worth naming
    instead of an unbounded one.

    The cluster term follows the switches, not just the tick: the guest stop is the
    largest single number here and it runs only where Ceph or the guest stop was asked
    for, so it is counted only there. Reading it off ``cluster`` alone charged a plain
    cluster five minutes for a step that cannot happen — and the battery-reserve warning
    below quotes this figure.

    Two things it deliberately does not add. The number of clusters, because a pure
    function over the config cannot know how many distinct clusters those hosts belong to
    (the names come from the API); one is assumed, and a second one doubles the cluster
    term. And the remaining attempts from MAX_SHUTDOWN_ATTEMPTS, because those land one
    poll apart and only after a host has already failed — counting every one of them would
    inflate the normal case threefold to warn about the abnormal one.
    """
    th = cfg.thresholds
    enabled = [h for h in cfg.hosts if h.enabled]
    stages = len({(h.this_host, h.order) for h in enabled}) or 1
    # The pre-this_host retry pass only exists where there is both something to retry and
    # a final stage to run before: a single stage has nothing ahead of it.
    if stages > 1 and any(h.this_host for h in enabled):
        stages += 1
    clustered = any(h.enabled and getattr(h, "cluster", False) for h in cfg.hosts)
    # The guest stop is the elastic half of the preparation and by far the largest term,
    # and it only ever runs where Ceph or the guest stop itself is switched on (see
    # Engine._guest_plan). Counting it for a cluster whose members ticked neither added
    # five minutes to a step that provably cannot happen — enough on a plain three-node
    # cluster to push the total past the default 10-minute trigger and warn a perfectly
    # sized installation that its battery reserve is too short. Still "err on the high
    # side": _cluster_switches() resolves a switch as "any member asked for it", and
    # any(cfg.hosts) is the superset of that.
    guests = any(
        h.enabled
        and (getattr(h, "cluster_ceph", False) or getattr(h, "cluster_guest_stop", False))
        for h in cfg.hosts
    )
    noisy = any(h.enabled and h.url for h in cfg.notifications.webhooks)
    return ShutdownBudget(
        stages=stages,
        # The grace belongs in here: targets.shutdown() bounds a call at the configured
        # timeout PLUS targets.DEADLINE_GRACE_S — the backstop for everything httpx does
        # not bound — and it pays that per stage, not once. Left out, the figure was
        # short by five seconds a stage, and this number exists to be sized against.
        host_s=int(th.host_shutdown_timeout_s + targets.DEADLINE_GRACE_S) * stages,
        cluster_s=(
            int(th.cluster_prep_timeout_s)
            + (int(th.cluster_guest_shutdown_timeout_s) if guests else 0)
            if clustered
            else 0
        ),
        # The grace belongs in here for the same reason it does in host_s: cluster.inspect()
        # is cut off at its budget PLUS cluster.DEADLINE_GRACE_S, and this figure may only
        # ever err high. One inspection's time covers any number of nodes since 4.1.0 —
        # _cold_inspect() runs them concurrently.
        inspect_s=(
            int(cluster.INSPECT_BUDGET_S + cluster.DEADLINE_GRACE_S) if clustered else 0
        ),
        # Plus one round per stage: see NOTIFY_EVENTS_ON_PATH.
        notify_s=(
            int(notify.NOTIFY_BUDGET_S) * (NOTIFY_EVENTS_ON_PATH + stages) if noisy else 0
        ),
    )


def shutdown_budget_s(cfg: AppConfig) -> int:
    """The worst case as one number, for /api/status and the shutdown preview."""
    return shutdown_budget(cfg).total


def _prep_intent(wanted: bool, effective: bool, why_not: str) -> str:
    """One cluster preparation step, as the event log should read.

    Three states, not two: switched off, switched on and going to run, or switched on but
    skipped because this cluster cannot do it. Collapsing the last two into "no" is what
    used to make a cluster without Ceph look like a configuration mistake.
    """
    if not wanted:
        return "off"
    return "yes" if effective else f"skipped ({why_not})"


def selftest_slot(now_local: datetime, hour: int, interval_min: int) -> datetime:
    """Start of the self-test slot ``now_local`` falls into (naive local time).

    The grid is anchored at ``hour``:00 and repeats every ``interval_min`` minutes. Every
    selectable interval divides 1440 evenly, so the grid is the same on every day and
    wraps cleanly across midnight (anchor 23:00, every 2 h -> 23:00, 01:00, 03:00, ...).
    Total by construction: a nonsensical hour or interval degrades to a daily grid rather
    than raising, because this runs inside the poll loop.

    DST is deliberately handled by doing nothing: in spring the slots inside the skipped
    hour simply do not occur that day, in autumn the repeated hour yields the same slot
    value twice and the caller's latch swallows the second run.
    """
    step = interval_min if interval_min and interval_min > 0 else 1440
    anchor = (hour % 24) * 60
    minute_of_day = now_local.hour * 60 + now_local.minute
    elapsed = (minute_of_day - anchor) % step
    return now_local.replace(second=0, microsecond=0) - timedelta(minutes=elapsed)


@dataclass
class _UpsRuntime:
    """Per-UPS runtime state (one instance per configured UPS, keyed by the UPS id)."""

    state: UpsState = field(default_factory=UpsState)
    on_battery_since: Optional[datetime] = None
    # True while this UPS carries state read off DISK that no poll of this process has
    # confirmed yet (see _restore_state). Nothing restored may fire a shutdown while it
    # is set and the device is silent — see _ups_trigger_reason(). Cleared by the first
    # answer, whatever that answer is.
    #
    # The appliance shuts its OWN host down last, so every outage ends with this process
    # being restarted and finding a state file that says "on battery since T0". If the UPS
    # then misses the very first poll after that boot — a switch still converging, an SNMP
    # card still coming up — the blind countdown read hours of elapsed time against a
    # 600 s threshold and shut the freshly booted nodes down again, during normal
    # operation, before the device had said a single word.
    restored_unconfirmed: bool = False
    unreachable_count: int = 0
    unreachable_since: Optional[datetime] = None  # wall clock, for the comms-loss opt-in
    alarm_active: bool = False
    last_reachable: Optional[bool] = None  # for connect/disconnect logging
    # True once a connection LOSS was actually notified (i.e. the unreachable alarm fired).
    # "Connection restored" is only worth a notification if the loss was one too — otherwise
    # a single dropped SNMP packet would produce a lost/restored pair out of nowhere.
    loss_notified: bool = False
    comm_loss_fired: bool = False  # latch for the optional comms-loss alarm wording
    triggered: bool = False  # this UPS currently demands a shutdown of its hosts
    trigger_reason: Optional[str] = None


class Engine:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.started_at = _now()
        self.state = ONLINE

        # Per-UPS runtime, keyed by the UPS id.
        self.ups_rt: dict[str, _UpsRuntime] = {}
        self._sync_runtimes()

        # Self-test scheduling, on two clocks on purpose:
        #   last_selftest_slot: naive LOCAL grid position -> the schedule latch (persisted)
        #   last_selftest_at:   UTC timestamp of the actual run -> REST API only
        # Declared before _restore_state() below, which re-arms the slot from the state
        # file — assigning them afterwards would wipe the restored value.
        self.last_selftest_slot: Optional[datetime] = None
        self.last_selftest_at: Optional[datetime] = None
        self.last_selftest_ok: Optional[bool] = None
        # Date (naive local) on which a successful run was last written to the event log.
        self.last_selftest_ok_logged: Optional[date] = None

        # Re-arm battery timers + latched triggers from the state file (None = nothing
        # written yet, so the first _evaluate always persists and clears a stale file).
        self._persisted_state: Optional[dict] = None
        self._restore_state()

        # Aggregate shutdown bookkeeping (any host fired this episode).
        self.shutdown_triggered = False
        self.shutdown_reason: Optional[str] = None
        self.triggered_at: Optional[datetime] = None

        # Per-host latch: do not re-fire while still pending. host_states holds the
        # committed (real) shutdown result and the last self-test result per host.
        # Both are keyed by HostConfig.key — the host's stable id, never anything the
        # user edits: correcting a node name would otherwise discard the self-test result
        # and the latch, and two entries sharing a type and a name (a duplicated row with
        # the IP not adjusted) would share one latch, leaving the second never fired.
        self.host_fired: dict[str, bool] = {}
        self.host_states: dict[str, dict] = {}
        # Since when every UPS has been back on mains, for the automatic re-arm (see
        # _maybe_rearm). Runtime only: a restart clears the latches it guards anyway.
        self._mains_ok_since: Optional[datetime] = None

        # Cluster awareness, all runtime-only (nothing here belongs in the config):
        # last inspection per cluster name, the "prepared this episode" latch, the
        # outcome of that preparation, and the names already warned about a missing
        # disarm-ha endpoint.
        self.cluster_states: dict[str, "cluster.ClusterInfo"] = {}
        self.cluster_prepared: dict[str, bool] = {}
        # Clusters whose preparation FAILED this episode. Separate from the latch above,
        # because the two answer different questions: "was it attempted" decides whether
        # to attempt it again, "did it work" decides whether the nodes may go down under
        # ``cluster_abort_on_prep_failure``. Deriving the second from the work of a single
        # iteration made the opt-in last exactly one poll.
        self.cluster_prep_failed: dict[str, bool] = {}
        # What the last preparation of this episode actually did, per cluster, for the
        # dashboard and the shutdown preview. Per-episode, so it is cleared with the
        # latches above.
        self.cluster_prep_steps: dict[str, list[str]] = {}
        # Guest facts discovered by the last inspection, per cluster name. A discovered
        # fact like cluster_states, so it survives the episode teardown.
        self.cluster_guest_state: dict[str, dict] = {}
        # Hosts pulled into this episode's shutdown because their cluster goes down as
        # a unit, mapped to the reason that brought them in. They have no trigger reason
        # of their own, so without this they would look "no longer eligible" on the next
        # poll and have their latch released.
        #
        # The reason is kept, not just the membership, because it is what lets a failed
        # attempt be retried. While this was a bare set, a unit host that failed its first
        # shutdown was skipped on every later poll — the retry MAX_SHUTDOWN_ATTEMPTS
        # promises never happened for it, and the event said "the next poll tries again"
        # anyway. _prepare_clusters() cannot re-add it either: the per-episode latch makes
        # it return before _unit_additions() runs.
        self.cluster_unit_hosts: dict[str, str] = {}
        # Hosts whose cluster could not be inspected at outage time. Kept apart from
        # cluster_prep_failed because that one is keyed by cluster NAME, and the name is
        # exactly what an unreachable inspection failed to establish.
        self.cluster_inspect_failed: set[str] = set()
        # Hosts that answered the inspection and turned out NOT to be a cluster member
        # at all. A per-episode latch like the two above and for the same reason: without
        # it the answer is settled but nobody records it, so every poll on which such a
        # host is still due — a retried shutdown — pays for another full inspection of a
        # question that cannot change. Kept apart from cluster_inspect_failed because that
        # one means "we could not look", which is a different verdict with different
        # consequences (abort-on-failure keys on it; this must never hold a node back).
        self.cluster_standalone: set[str] = set()
        self._disarm_unsupported_warned: set[str] = set()
        # Set by the automatic re-arm, consumed by _maybe_selftest: right after a
        # re-arm is exactly when a leftover problem (an expired token, a cluster still
        # prepared, a node that never came back) should surface, rather than hours later
        # at the next scheduled slot.
        self._rearm_selftest_pending = False
        # One inspection per process start, independent of the self-test schedule — see
        # _maybe_cluster_startup_check(). Deliberately not persisted.
        self._cluster_startup_done = False
        self._node_startup_done = False
        # API URLs already warned about. Both the startup node check and the scheduled
        # self-test report them, and on a process start with a due slot both run in the
        # same iteration — the operator would read the identical warning twice.
        self._dup_url_warned: set[str] = set()
        self._stale_feed_warned: set[str] = set()
        self._incomplete_warned: set[str] = set()
        self._value_corrections_warned: set[str] = set()
        self._reserve_warned: set[str] = set()

        # Daily housekeeping: keep the event log bounded.
        self.last_prune_date = None  # type: ignore[var-annotated]

        # History (#37): rows waiting for the writer thread, the writer itself, and per
        # UPS when and in which state the last row was taken.
        self._history_buf: list[tuple] = []
        self._history_task: Optional[asyncio.Task] = None
        self._history_last: dict[str, tuple[float, tuple]] = {}
        self._history_prune_due = False
        self._history_failing = False

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

    def _sync_host_states(self) -> None:
        """Drop per-host bookkeeping for hosts that are no longer configured.

        The sibling of _sync_runtimes, and it was missing. Both of the places that decide
        whether an episode is over read these dicts WHOLESALE: _recompute_state() scans
        every value of host_states, and _evaluate_hosts() ends the episode on
        ``any(self.host_fired.values())``. A host that had fired and was then deleted
        therefore kept the appliance on SHUTTING_DOWN and shutdown_triggered for ever —
        which in turn stands down the self-test, both startup checks and "Restore
        cluster". With the default re-arm that heals itself after five minutes; with
        ``rearm_after_mains_min: null`` it does not.

        Disabled hosts are KEPT: they are still configured, ordered_hosts() simply skips
        them, and dropping their self-test verdict on a temporary untick would lose the
        one record that says whether their credentials still work.
        """
        keys = {h.key for h in self.cfg.hosts}
        for key in list(self.host_states):
            if key not in keys:
                del self.host_states[key]
        for key in list(self.host_fired):
            if key not in keys:
                del self.host_fired[key]
        for key in list(self.cluster_unit_hosts):
            if key not in keys:
                del self.cluster_unit_hosts[key]
        self.cluster_inspect_failed &= keys
        self.cluster_standalone &= keys

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
                # Restored, but HELD until one poll of this process has answered — see
                # _UpsRuntime.restored_unconfirmed and _ups_trigger_reason(). The timer
                # itself is kept in full, so the confirming poll re-arms the whole elapsed
                # time at once and a service restart mid-outage loses nothing.
                rt.restored_unconfirmed = True
                # A latched trigger (e.g. battery low before the restart) is re-armed
                # together with its timer, so the restart cannot demote it back to the
                # remaining countdown — but it is re-armed by that confirming poll, not
                # here — and it needs nothing parked to be re-armed by it, because the
                # restored timer comes back in full: with on_battery_seconds set, that
                # confirming poll re-derives a trigger from an elapsed time already far
                # past the threshold. Setting rt.triggered from the file instead made the
                # latch bypass every freshness check there is: the unreachable branch of
                # _evaluate_ups() deliberately never re-derives a trigger that is already
                # set, so a latch read off disk fired on the first poll after a boot no
                # matter what — or whether — the device answered.
                reason = triggers.get(uid)
                if not isinstance(reason, str):
                    reason = ""
                self._log_quiet(
                    "On-battery timer restored",
                    f"UPS {uid}: on battery since {ts} (from state file after restart). "
                    + (
                        f"The latched trigger ({reason}) is re-armed"
                        if reason
                        else "The countdown continues"
                    )
                    + " as soon as this UPS answers a poll — nothing read from the state "
                    "file shuts anything down on its own.",
                    db.WARNING,
                )
            self._restore_selftest_slot(data.get("selftest_slot"))
            self._restore_selftest_outcome(data.get("selftest_at"), data.get("selftest_ok"))
        except Exception as exc:  # noqa: BLE001 - a broken state file must never block startup
            log.warning("Engine state restore failed: %s", exc)

    def _restore_selftest_slot(self, raw) -> None:
        """Re-arm the self-test latch, so a restart does not re-run the test immediately.

        Written as a naive *local* timestamp (unlike the UTC values above). A value in the
        future can only come from a backwards clock jump (NTP correction, timezone change)
        and is dropped — keeping it would block self-tests until the clock caught up.
        """
        if not isinstance(raw, str):
            return
        try:
            slot = datetime.fromisoformat(raw)
        except ValueError:
            return
        if slot.tzinfo is None and slot <= _local_now() + timedelta(minutes=1):
            self.last_selftest_slot = slot

    def _restore_selftest_outcome(self, raw_at, raw_ok) -> None:
        """Carry the last result over a restart, so /api/status keeps reporting it."""
        if not isinstance(raw_at, str):
            return
        try:
            when = datetime.fromisoformat(raw_at)
        except ValueError:
            return
        if when.tzinfo is None:  # ours is UTC-aware; anything else is not our value
            return
        self.last_selftest_at = when
        self.last_selftest_ok = raw_ok if isinstance(raw_ok, bool) else None

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
        # Naive LOCAL timestamp, unlike everything above — the self-test grid is
        # wall-clock based. Key absent = the test has not run yet on this instance.
        if self.last_selftest_slot is not None:
            current["selftest_slot"] = self.last_selftest_slot.isoformat()
        # The outcome travels with the latch: since a restart no longer re-runs the test,
        # /api/status would otherwise report "never tested" until the next slot.
        if self.last_selftest_at is not None:
            current["selftest_at"] = self.last_selftest_at.isoformat()
            current["selftest_ok"] = self.last_selftest_ok
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

    def _outage_in_progress(self) -> bool:
        """Whether housekeeping has to stand down: a shutdown episode or a real outage.

        One reading, shared by the two startup checks, the scheduled self-test, the manual
        self-test and "Restore cluster" — five copies of the same condition, which is how
        they would drift apart.

        A restored-but-unconfirmed timer deliberately does NOT count. It is set by
        _restore_state() for every UPS the state file mentions, and it stays set until that
        UPS answers a poll — so a device that never answers again used to mean "an outage
        is running" for ever. That is exactly the situation after a real cluster shutdown:
        this appliance powers its own host off last, comes back up, and the UPS management
        card is still down. Everything below then stood down permanently — the cluster
        startup check never ran, so cluster_states stayed empty and the "Restore cluster"
        button stayed hidden; restore_clusters() would have refused it anyway; and the
        node check's warnings about incomplete entries, stale feeds, duplicated API URLs,
        corrected settings and the battery reserve were never written. The one moment the
        operator needs that button is the one moment they could not reach it.

        Nothing is risked by leaving it out: such a timer cannot fire a shutdown either
        (see _ups_trigger_reason), so there is no countdown for a self-test to delay. The
        moment the device answers, this reads "outage" again — or the timer is dropped.
        """
        return self.shutdown_triggered or any(
            rt.state.on_battery
            or (rt.on_battery_since is not None and not rt.restored_unconfirmed)
            for rt in self.ups_rt.values()
        )

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
        """Apply a new config (from the UI) without restarting the loop.

        The self-test latch is deliberately kept: saving settings must not fire a
        credential check, and a changed schedule takes effect at its next slot.

        The node-name check is the exception, and re-armed here. What makes the credential
        test too expensive to repeat on every save — up to 10 s per host — does not apply
        to one GET /nodes, and a node name saved without ever pressing "Test" is exactly
        the mistake that used to stay hidden until an outage.
        """
        self.cfg = cfg
        self._node_startup_done = False
        # Re-armed for the same reason, and it was missing: ticking "this is a cluster
        # member" and saving left the dashboard showing no cluster and hiding the "Restore
        # cluster" button until the next scheduled slot — a day by default — because both
        # are driven by cluster_states, which only the self-test and this startup check
        # fill. One read-only, hard-bounded inspection on the next poll answers it instead.
        self._cluster_startup_done = False
        # A changed host list is a new question: say it again for whatever is wrong now.
        self._dup_url_warned = set()
        self._stale_feed_warned = set()
        self._incomplete_warned = set()
        self._value_corrections_warned = set()
        self._reserve_warned = set()
        self._sync_runtimes()
        # Hosts and webhooks that are gone leave bookkeeping behind that outlives them —
        # a shutdown latch that keeps the episode open, a delivery record a re-created id
        # would inherit. Cleared here, where the new configuration arrives.
        self._sync_host_states()
        notify.forget_deliveries({h.id for h in cfg.notifications.webhooks})

    def _release_shutdown_latches(self) -> None:
        """Un-latch the shutdown episode: the appliance is ready for the next outage.

        Shared by the manual reset and the automatic re-arm, so both leave exactly the
        same state behind.

        Only the shutdown keys are dropped from ``host_states`` — the self-test verdicts
        and the node-name check live in the same dict and answer a different question.
        Clearing the whole dict (which the manual reset used to do) made /api/health
        report "never tested" for every host until the next scheduled slot, hours away.
        """
        self.shutdown_triggered = False
        self.shutdown_reason = None
        self.triggered_at = None
        self.host_fired = {}
        for key in list(self.host_states):
            self._clear_host_shutdown_state(key)
        # Not the discovered cluster facts (those are refreshed by the self-test), only
        # the per-episode latches: a new outage must prepare again, and a preparation
        # that failed last time must not keep holding its nodes back. What was written to
        # the cluster stays written until someone presses "Restore cluster".
        self.cluster_prepared = {}
        self.cluster_prep_failed = {}
        self.cluster_prep_steps = {}
        self.cluster_unit_hosts = {}
        self.cluster_inspect_failed = set()
        self.cluster_standalone = set()
        self._mains_ok_since = None

    def _clear_host_shutdown_state(self, key: str) -> None:
        """Drop one host's per-episode shutdown keys, keeping everything else.

        Only the shutdown keys go — the self-test verdicts and the node-name check live in
        the same dict and answer a different question. Clearing the whole entry (which the
        manual reset used to do) made /api/health report "never tested" for every host
        until the next scheduled slot, hours away.
        """
        st = self.host_states.get(key)
        if not st:
            return
        # ``key_name``, not ``field``: dataclasses.field is imported at module level and a
        # loop variable of that name shadows it for the rest of this scope.
        for key_name in ("shutdown_state", "shutdown_attempts", "last_action_at",
                         "last_error", "reachable", "this_host", "order"):
            st.pop(key_name, None)

    def reset(self) -> None:
        """Clear shutdown latches and alarms — the dashboard's "Reset state" button."""
        self._release_shutdown_latches()
        for rt in self.ups_rt.values():
            rt.alarm_active = False
            rt.loss_notified = False
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
                    # return_exceptions: sources.poll() is total and bounded, but this is
                    # the one gather whose failure would cost the whole iteration — the
                    # countdowns, the host evaluation and the staged shutdown all sit
                    # behind it. A source that manages to raise anyway becomes what every
                    # other failed read becomes: unreachable, i.e. an alarm, never a
                    # shutdown.
                    results = await asyncio.gather(
                        *(poll(u) for u in ups_list), return_exceptions=True
                    )
                    for u, st in zip(ups_list, results):
                        rt = self.ups_rt.get(u.id)
                        if rt is None:
                            continue
                        if isinstance(st, BaseException):
                            log.warning("Poll of %s raised: %s", u.label, st)
                            st = UpsState(error=str(st) or type(st).__name__)
                        rt.state = st
                await self._evaluate()
                await self._maybe_cluster_startup_check()
                await self._maybe_node_startup_check()
                await self._maybe_selftest()
                self._maybe_prune()
            except Exception as exc:  # noqa: BLE001
                log.exception("Engine iteration failed: %s", exc)
            # Last, and outside the block above: every decision of this iteration is made
            # by now, and nothing the history does — or fails to do — can reach it.
            try:
                self._maybe_record_history()
            except Exception as exc:  # noqa: BLE001 - the history never touches the loop
                log.warning("History sampling failed: %s", exc)

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
        self._sync_host_states()

        # Phase A: evaluate every UPS independently.
        for u in self.cfg.ups:
            rt = self.ups_rt.get(u.id)
            if rt is not None:
                await self._evaluate_ups(u, rt)

        # Phase A2: release last episode's latches once mains have proven to be back.
        # Before phase B, so the very same iteration can act on a fresh outage.
        await self._maybe_rearm()

        # Phase B + C: per-host eligibility and shutdown execution.
        await self._evaluate_hosts()

        self._recompute_state()
        self._persist_state()

    async def _evaluate_ups(self, u: UpsBase, rt: _UpsRuntime) -> None:
        """Per-UPS state machine: connectivity logging, fail-safe alarm, battery timer,
        and the resulting ``rt.triggered`` / ``rt.trigger_reason``."""
        th = self.cfg.effective_thresholds(u)
        st = rt.state
        name = u.label

        # --- log every connectivity change (the first poll, None, is silent) -----
        # Both directions are logged, but only NOTIFIED once the loss has proven to be
        # more than a blip: a single dropped SNMP packet used to fire a warning-level
        # webhook here, ahead of (and in addition to) the unreachable alarm below, which
        # is the one that honours ``unreachable_alarm_after_polls``. The event log keeps
        # every transition, so diagnostics stay complete either way.
        if rt.last_reachable is not None and st.reachable != rt.last_reachable:
            if st.reachable:
                subject = f"{name}: network connection restored"
                body = "The UPS is answering again."
                if rt.loss_notified:
                    await self._emit(subject, body, db.INFO)
                else:
                    self._log_quiet(subject, body, db.INFO)
            else:
                self._log_quiet(
                    f"{name}: network connection lost",
                    f"No response from the UPS ({st.error or 'timeout'}).",
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
                # Logged here too, not only on the reachable path. A blind countdown and
                # the comms-loss opt-in fire real shutdowns, and they fire them in exactly
                # the situation where the post-mortem has least to go on: the last poll
                # that answered is the only evidence there is, and nothing was recording
                # it. What readable_raw() shows is that last good reading, which is the
                # point — the elapsed time is measured on our clock either way.
                if rt.triggered:
                    self._log_trigger_evidence(u, rt)

            # A confirmed on-battery outage whose time-based countdown survives a comms loss.
            # "Confirmed" is load-bearing and used to be assumed: a timer read back from
            # the state file has been observed by nobody in this process, and while it is
            # held (see _ups_trigger_reason) this alarm promised a shutdown "when it
            # expires" that was never going to come.
            shutdown_pending_blind = (
                th.keep_shutdown_on_comm_loss
                and th.on_battery_seconds is not None
                and rt.on_battery_since is not None
                and not rt.restored_unconfirmed
            )

            if rt.unreachable_count >= th.unreachable_alarm_after_polls and not rt.alarm_active:
                rt.alarm_active = True
                # This IS the connection-loss notification (see the transition log above):
                # it only fires once the loss has persisted for the configured number of
                # polls, and it licenses the matching "restored" notification later.
                rt.loss_notified = True
                if rt.triggered:
                    await self._emit(
                        f"{name} unreachable — trigger stays latched",
                        f"No response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). The already fired trigger persists: "
                        f"{rt.trigger_reason}.",
                        db.WARNING,
                    )
                elif rt.restored_unconfirmed and rt.on_battery_since is not None:
                    # The state file says an outage was running when this process last
                    # died; this UPS has not answered since, so nothing about that is
                    # confirmed. Its own wording, because the two neighbouring ones are
                    # both wrong here: there is no countdown to expire, and "NO shutdown
                    # will be triggered" would hide a timer that is very much still there
                    # and re-arms in full the moment the device speaks.
                    elapsed = self._ups_elapsed_on_battery(rt)
                    await self._emit(
                        f"{name} unreachable — restored countdown is on hold",
                        f"No response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). A previous run left an on-battery "
                        f"timer behind"
                        + (f" (~{elapsed} s)" if elapsed is not None else "")
                        + ", but no poll of this run has confirmed the outage is still "
                        "going, so nothing is shut down on the strength of it. It re-arms "
                        "in full as soon as this UPS answers — or is dropped if the answer "
                        "is 'on mains'.",
                        db.WARNING,
                    )
                elif shutdown_pending_blind:
                    remaining = self._ups_countdown_remaining_s(u, rt)
                    await self._emit(
                        f"{name} unreachable — on-battery countdown continues",
                        f"No response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). The on-battery countdown keeps running "
                        f"on the local clock — shutdown when it expires"
                        + (f" (~{remaining} s)." if remaining is not None else "."),
                        db.WARNING,
                    )
                elif (
                    th.comm_loss_shutdown_after_min is not None
                    and not st.answered
                    # Same guard as the trigger this text announces (see
                    # _ups_trigger_reason): an entry that cannot be polled at all never
                    # arms the opt-in, so promising a shutdown "if the loss persists"
                    # would be the alarm contradicting the engine. It falls through to the
                    # plain "NO shutdown will be triggered" below, which is the truth —
                    # and the incomplete entry has a CRITICAL event and a dashboard chip
                    # of its own saying what to fix.
                    and not self.cfg.incomplete_ups(u)
                ):
                    await self._emit(
                        f"{name} unreachable — shutdown on prolonged loss",
                        f"No response for {rt.unreachable_count} polls "
                        f"({st.error or 'timeout'}). No power outage confirmed, but if the "
                        f"communication loss persists, a shutdown will be triggered after "
                        f"{th.comm_loss_shutdown_after_min} min.",
                        db.WARNING,
                    )
                elif st.answered:
                    # It is answering; what it says cannot be used. "No response for N
                    # polls" would send the operator to the network, which is the one place
                    # the fault is not — and with the comms-loss opt-in on, the neighbouring
                    # wording would promise a shutdown that deliberately never comes.
                    #
                    # Source-neutral wording: this branch is no longer SNMP-only. A upsd
                    # answering ERR DATA-STALE, or one whose ups.status names neither OL
                    # nor OB, reaches it too — "which objects it implements" would be an
                    # answer to a question a NUT user never asked.
                    await self._emit(
                        f"{name} answers, but cannot be read",
                        f"{rt.unreachable_count} polls in a row were answered without a "
                        f"usable reading ({st.error or 'no usable reading'}). The device is "
                        f"treated as unreachable — an alarm, and NO shutdown, not even "
                        f"through 'shutdown on pure communication loss': the connection is "
                        f"plainly there. Use the UPS card's test button to see what it does "
                        f"deliver.",
                        db.WARNING,
                    )
                else:
                    await self._emit(
                        f"{name} unreachable",
                        f"No response for {rt.unreachable_count} polls "
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

        # Reachable again — clear alarm/comms-loss latches. Runs *after* the transition
        # log above, so clearing loss_notified here cannot swallow the "restored" notice
        # for a loss that was actually reported.
        rt.alarm_active = False
        rt.loss_notified = False
        rt.comm_loss_fired = False
        rt.unreachable_count = 0
        rt.unreachable_since = None

        # The device has spoken: what came off the state file may act from here on. Placed
        # before the two branches below so both inherit it — the mains branch clears the
        # timer, the battery branch re-derives the trigger against the FULL restored
        # elapsed time and so re-arms it on this very poll. A service restart mid-outage
        # therefore loses nothing at all, because there the UPS is reachable by definition;
        # if it were not, there would be no fresh data to act on either way.
        rt.restored_unconfirmed = False

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

        was_triggered = rt.triggered
        rt.trigger_reason = self._ups_trigger_reason(u, rt)
        rt.triggered = rt.trigger_reason is not None
        if rt.triggered and not was_triggered:
            self._log_trigger_evidence(u, rt)

    async def _evaluate_hosts(self) -> None:
        """Per-host eligibility (Phase B) and shutdown execution (Phase C).

        Eligible, not-yet-fired hosts are processed in ``ordered_hosts()`` order, which
        places the appliance's own host last — so "own host last" holds within the
        current batch automatically.
        """
        eligible: list[tuple[HostConfig, str]] = []
        for host in self.cfg.ordered_hosts():
            reason = self._host_trigger_reason(host)
            # "sent" only. A failed attempt is deliberately NOT committed any more: it
            # may still be retried (see _fire_host), and if the outage ends first the
            # latch should come off like any other uncommitted one. Note this is a
            # different question from the one _recompute_state asks, which still counts
            # "failed" as committed — a retry in progress is a shutdown in progress.
            committed = self.host_states.get(host.key, {}).get("shutdown_state") == "sent"

            if reason is None:
                # A host its cluster took down as a unit never had a reason of its own, so
                # "no reason now" says nothing about it. Without this the latch would be
                # released on the very next poll — in dry-run once per host, with a
                # misleading "shutdown aborted", and it could never fire again because the
                # preparation latch is already set.
                unit_reason = self.cluster_unit_hosts.get(host.key)
                # ...but only while its cluster's episode is genuinely still running.
                # Without that second half the branch answered "no reason now" with "then
                # keep going" unconditionally, and a taken-along node whose first attempt
                # had failed was re-sent on every following poll — including after mains
                # had come back. That is a healthy production node powered off during
                # normal operation, which is the one outcome this appliance exists to
                # prevent. When the episode is over the host falls through to the release
                # below instead, where it gets the same verdict as any other node that
                # never went down.
                if unit_reason is not None and self._cluster_episode_live(host):
                    # It does say something about a FAILED attempt, though, and this is
                    # where the retry has to come from. _prepare_clusters() cannot supply
                    # it a second time (its per-episode latch returns before
                    # _unit_additions runs), so while this branch simply skipped, a unit
                    # host got exactly one attempt — whatever MAX_SHUTDOWN_ATTEMPTS and
                    # the "the next poll tries again" event both promised.
                    if self._retry_due(host):
                        eligible.append((host, unit_reason))
                    continue
                # No longer eligible (a required feed recovered): release a not-yet-committed
                # (dry-run) latch so the dashboard can recover. A real, sent shutdown stays.
                #
                # "or failed" is not redundant with the latch, and leaving it out was a
                # hole: a failed attempt that still has retries left keeps host_fired
                # False on purpose (see _fire_host), so keying on the latch alone missed
                # exactly the state the retry created. An outage ending BETWEEN two
                # attempts then left "failed" in host_states for good — _recompute_state
                # counts that as committed, so the appliance sat on SHUTTING_DOWN with
                # mains long back, no abort event was ever written, and _maybe_rearm could
                # not clean up either because nothing was latched any more.
                failed = (
                    self.host_states.get(host.key, {}).get("shutdown_state") == "failed"
                )
                if (self.host_fired.get(host.key) or failed) and not committed:
                    self.host_fired[host.key] = False
                    # A taken-along node loses its membership record here too, and only
                    # here: this is the moment its cluster's episode is over, so there is
                    # nothing left to take it along with. _fire_stage() deliberately keeps
                    # the entry on a raising attempt, which is a different moment.
                    self.cluster_unit_hosts.pop(host.key, None)
                    # Also drop the attempt record. Without this a host that had used up
                    # its retries left "failed" in host_states, which _recompute_state
                    # counts as committed — the dashboard would sit on SHUTTING_DOWN with
                    # the outage long over, and the re-arm that used to clean up never
                    # runs because nothing is latched any more.
                    self._clear_host_shutdown_state(host.key)
                    await self._emit(
                        f"Host {host.name}: shutdown aborted",
                        # Two wordings, because "no longer needed" is false for a machine
                        # that is still running only because every attempt to shut it down
                        # failed. Reading that as a clean withdrawal is how an unfinished
                        # episode gets filed as a finished one.
                        "Feeding UPS device(s) sufficient again — shutdown no longer needed."
                        if not failed
                        else (
                            "Feeding UPS device(s) sufficient again, so the shutdown is no "
                            "longer needed — but note it never succeeded: every attempt "
                            "failed and this machine is still running. Check the host's "
                            "credentials and the earlier events."
                        ),
                        # A withdrawn shutdown is not routine: warning, so it passes the
                        # webhook's default severity filter. One that never succeeded is
                        # not merely non-routine — a machine is still running that was
                        # meant to be off — so it goes out a level higher.
                        db.CRITICAL if failed else db.WARNING,
                    )
                continue

            if self.host_fired.get(host.key):
                continue  # already fired this episode
            eligible.append((host, reason))

        # Prepare each affected cluster exactly once, BEFORE the first node of it goes
        # down. Returns the hosts that may still be shut down (all of them unless the
        # user opted into aborting on a failed preparation).
        #
        # Kept from before that filtering: a cluster held back leaves nothing to fire and
        # nothing latched, which would otherwise read as "the episode is over" at the end
        # of this method — clearing the preparation latch and running the whole failed
        # sequence again on the next poll, every 8 s, with a CRITICAL pair each time.
        had_eligible = bool(eligible)
        if eligible:
            eligible = await self._prepare_clusters(eligible)

        # Fire in stages: hosts sharing an ``order`` go at once, and ``this_host`` forms
        # the final stage on its own. Concurrency within a stage is what keeps one target
        # that stops responding from delaying its peers — and, because every stage is
        # awaited inside the poll loop, from stalling the battery countdown as well.
        # ``eligible`` is already sorted by (this_host, order, name), so the groups are
        # contiguous; targets.shutdown() carries a hard deadline, so no stage can outlast it.
        failed_earlier: list[tuple[HostConfig, str]] = []
        for stage_key, group in groupby(eligible, key=lambda hr: (hr[0].this_host, hr[0].order)):
            staged = list(group)
            # Last chance before the appliance powers itself off. Every stage but this one
            # is followed by another poll in eight seconds, which is where the retry
            # normally happens — but nothing follows ``this_host``: we lose power. So a
            # host that failed in an earlier stage of THIS pass gets its next attempt here
            # rather than in a poll that will never run. That is the case
            # MAX_SHUTDOWN_ATTEMPTS exists for (a 503 from a busy pveproxy), and it was
            # precisely the case the poll-based retry could not reach.
            if stage_key[0] and failed_earlier:
                retry = [(h, r) for h, r in failed_earlier if self._retry_due(h)]
                failed_earlier = []
                if retry:
                    await self._fire_stage(retry)
            failed_earlier += await self._fire_stage(staged)

        # Clear the aggregate once nothing is pending, committed or still due. The last
        # condition matters for a cluster that is being held back: no host of it is
        # latched, yet the outage is very much still running.
        if not any(self.host_fired.values()) and not had_eligible:
            self.shutdown_triggered = False
            self.shutdown_reason = None
            self.triggered_at = None
            # Episode over (mains back, nothing pending): the next outage prepares afresh.
            # The clusters stay prepared on the Proxmox side until someone presses
            # "Restore cluster" — the health check keeps saying so.
            self.cluster_prepared = {}
            self.cluster_prep_failed = {}
            self.cluster_prep_steps = {}
            self.cluster_unit_hosts = {}
            self.cluster_inspect_failed = set()
            self.cluster_standalone = set()

    def _cluster_episode_live(self, host: HostConfig) -> bool:
        """Whether the shutdown episode of this host's cluster is still running.

        Asked only about a node that has no trigger of its own — one its cluster took
        along as a unit. Its own state cannot answer the question: it is "failed", which
        is exactly why a retry is being considered, and nothing about it says whether the
        outage that started all this is still on.

        So the peers answer it. One of them having a *sent* shutdown means the cluster
        really is going down and this node has to follow; one of them still being eligible
        means the outage has not been withdrawn. Anything else means the episode is over,
        and a node that is only "due" because of a cluster that is no longer going anywhere
        must not be shut down — that would be a healthy machine powered off with mains back.
        Deliberately keyed on "sent" rather than "failed": a peer that never went down is
        no reason to take this one down either.

        Without a discovered cluster name there are no peers to ask (which
        _unit_additions() makes unreachable in practice, since it stamps every member), and
        the answer is then "not live": refusing to shut a machine down is the recoverable
        half of being wrong here.
        """
        name = self.host_states.get(host.key, {}).get("cluster_name")
        if not name:
            return False
        for peer in self.cfg.hosts:
            if not peer.enabled or peer.key == host.key:
                continue
            if self.host_states.get(peer.key, {}).get("cluster_name") != name:
                continue
            if self.host_states.get(peer.key, {}).get("shutdown_state") == "sent":
                return True
            if self._host_trigger_reason(peer) is not None:
                return True
        return False

    def _retry_due(self, host: HostConfig) -> bool:
        """Whether this host failed a shutdown and still has attempts left.

        One reading of "may try again", shared by the unit-host branch and the extra pass
        before ``this_host`` — the two used to be the places where a retry either happened
        or silently did not.
        """
        st = self.host_states.get(host.key, {})
        if st.get("shutdown_state") != "failed" or self.host_fired.get(host.key):
            return False
        return int(st.get("shutdown_attempts") or 0) < MAX_SHUTDOWN_ATTEMPTS

    async def _fire_stage(
        self, staged: list[tuple[HostConfig, str]]
    ) -> list[tuple[HostConfig, str]]:
        """Shut down one stage concurrently; return the entries that failed and may retry.

        Split out of _evaluate_hosts so the extra pass before ``this_host`` runs through
        exactly the same code — a second, slightly different copy of the firing logic is
        how the two would drift apart.
        """
        # return_exceptions, because without it one raising host aborts _evaluate()
        # entirely: every LATER stage is skipped — the appliance's own host among them
        # — and _recompute_state()/_persist_state() never run for that iteration.
        # _fire_host is total in practice; this makes it structural.
        outcomes = await asyncio.gather(
            *(self._fire_host(h, r) for h, r in staged), return_exceptions=True
        )
        failed: list[tuple[HostConfig, str]] = []
        for (host, reason), outcome in zip(staged, outcomes):
            if isinstance(outcome, BaseException):
                log.exception("Shutdown of %s raised: %s", host.name, outcome)
                # Nothing may treat this as handled, so the latch comes off — but the
                # attempt is recorded rather than left blank, which is what turns it into
                # a retry. cluster_unit_hosts is deliberately KEPT: for a node the cluster
                # takes down as a unit it is the only record of why it is due at all
                # (_host_trigger_reason returns None for it, and _prepare_clusters cannot
                # offer it again behind the per-episode latch). Dropping it here left such
                # a node silently unshut for the rest of the outage.
                self.host_fired[host.key] = False
                st = self.host_states.setdefault(host.key, {})
                st["shutdown_state"] = "failed"
                st["shutdown_attempts"] = int(st.get("shutdown_attempts") or 0) + 1
                st["last_error"] = f"{type(outcome).__name__}: {outcome}"
                st["last_action_at"] = _now().isoformat()
                if self._retry_due(host):
                    failed.append((host, reason))
                continue
            if not self.host_fired.get(host.key):
                failed.append((host, reason))
        return failed

    async def _maybe_rearm(self) -> None:
        """Release a finished shutdown episode once mains have been back long enough.

        A *sent* shutdown latches its host for good (_evaluate_hosts only releases
        uncommitted ones, so a host that is powering down is never told twice). Nothing
        ever cleared that latch again: the appliance stayed in SHUTTING_DOWN for as long
        as the process lived, a second outage found every host still flagged as fired and
        shut down nothing, and the self-test, the startup checks and the "Restore cluster"
        button — all of which stand down while ``shutdown_triggered`` — never came back.

        The delay is the point: mains returning is not mains staying. A grid that dips
        twice in a minute must not re-arm in between, so every UPS has to be *reachable,
        on mains and no longer triggered* for the whole waiting time. An unreachable UPS
        does not count as mains-ok — the same fail-safe reading as everywhere else: we do
        not know, so we do not act.

        Restoring a prepared cluster stays manual (see restore_clusters); this only makes
        that button reachable again, and says so when a cluster is still carrying the
        preparation.
        """
        minutes = self.cfg.thresholds.rearm_after_mains_min
        latched = self.shutdown_triggered or any(self.host_fired.values())
        if minutes is None or not latched:
            self._mains_ok_since = None
            return

        mains_ok = bool(self.ups_rt) and all(
            rt.state.reachable
            and not rt.state.on_battery
            and rt.on_battery_since is None
            and not rt.triggered
            for rt in self.ups_rt.values()
        )
        if not mains_ok:
            self._mains_ok_since = None
            return
        if self._mains_ok_since is None:
            self._mains_ok_since = _now()
            return
        waited_s = (_now() - self._mains_ok_since).total_seconds()
        if waited_s < minutes * 60:
            return

        pending = [
            name for name, info in self.cluster_states.items() if info.needs_recovery
        ]
        self._release_shutdown_latches()
        self._recompute_state()
        await self._emit(
            "Appliance re-armed",
            f"Mains have been back on every UPS for {minutes} min. The shutdown latches "
            f"are released, so a new outage is handled from scratch."
            + (
                f" Still prepared for shutdown and waiting for 'Restore cluster': "
                f"{', '.join(pending)}."
                if pending
                else ""
            ),
            db.INFO,
        )
        # Not run from here: this is phase A2 of _evaluate(), and a self-test costs up to
        # ten seconds per host — it would delay the eligibility check of the very same
        # iteration. _maybe_selftest() picks the flag up a few lines later in the loop and
        # already owns the guard that refuses to run one during an outage.
        self._rearm_selftest_pending = True

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
        """How long this UPS has been on battery, in seconds — our own clock, or nothing.

        ``UpsState.seconds_on_battery`` is deliberately NOT consulted. It is a duration the
        *device* claims: we never observed it and cannot check it. RFC 1628 defines
        upsSecondsOnBattery only "if the unit is on battery power" and says nothing about
        the value while on mains, so a card that keeps the last transfer's figure instead of
        clearing it breaks no rule — and a number of them do exactly that. Trusting it meant
        the very first poll of a fresh outage already read a value past ``on_battery_seconds``
        and fired the shutdown before any time had actually passed (issue #25). The mirror
        image was just as bad: a card reporting a permanent 0 pinned the elapsed time at zero
        and disabled the time trigger altogether — the only trigger that still works while
        the UPS is unreachable.

        The timer normally survives a restart through the state file (see _restore_state),
        which covers the case that would otherwise argue for the device counter. Where it
        does not — the file is missing, unreadable or older than STATE_RESTORE_MAX_AGE_H —
        the price is deliberate: an appliance that starts cold into a running outage begins
        counting at zero and therefore fires the time trigger late. Runtime, charge and
        battery_low all read fresh data and are unaffected, and they are the ones that
        matter when the battery is actually running out.
        """
        if rt.on_battery_since is not None:
            return int((_now() - rt.on_battery_since).total_seconds())
        return None

    def _ups_elapsed_source(self, rt: _UpsRuntime) -> Optional[str]:
        """Which clock _ups_elapsed_on_battery() answered from, or None for no answer.

        Reported next to the figure so "we measured this" can never be mistaken for a number
        the device handed us. Never the device's own counter, by construction — see above.

        "own-unconfirmed" is the same measurement carrying one caveat: it was read back
        from the state file and no poll of this process has confirmed the outage is still
        running, so it is displayed but cannot fire anything (see _ups_trigger_reason).
        Its own value rather than a silent "own", because the difference is the whole
        question an operator has at that moment — the number is real, the conclusion is not.
        """
        if rt.on_battery_since is None:
            return None
        return "own-unconfirmed" if rt.restored_unconfirmed else "own"

    def _log_trigger_evidence(self, u: UpsBase, rt: _UpsRuntime) -> None:
        """Record the readings that armed this UPS, once, at the moment it happens.

        Without this an unexpected shutdown cannot be reconstructed afterwards: the reason
        line says what the engine concluded, never what it read, and the manual test button
        can only answer about a later and different state. Quiet on purpose — this belongs
        in the event log for the post-mortem, not in every chat channel.
        """
        parts = [f"Reason: {rt.trigger_reason}."]
        elapsed = self._ups_elapsed_on_battery(rt)
        if elapsed is not None:
            parts.append(
                f"Elapsed on battery: {elapsed} s "
                f"(from {self._ups_elapsed_source(rt)} clock)."
            )
        raw = rt.state.readable_raw()
        if raw:
            parts.append(f"Readings: {raw}")
        self._log_quiet(
            f"{u.label} armed the shutdown trigger", " ".join(parts), db.WARNING
        )

    def _ups_trigger_reason(self, u: UpsBase, rt: _UpsRuntime) -> Optional[str]:
        """Whether (and why) a single UPS currently demands a shutdown.

        These thresholds must never fire on mains, so a UPS recharging after an outage is
        not mistaken for a reason to shut down. While unreachable, only the on_battery_seconds
        timer (blind countdown) or the opt-in pure comms-loss can match — the fresh UpsState
        carries no runtime/charge/battery_low data.
        """
        th = self.cfg.effective_thresholds(u)
        st = rt.state

        # Nothing read off disk may shut anything down before the device has answered once.
        # Every branch below that can fire while unreachable does so from state this
        # process never observed — the restored on_battery_since, or an unreachable_since
        # that a restart resets — and the appliance restarts at the END of every outage,
        # because it shuts its own host down last. A UPS that then missed the first poll
        # after the boot (switch still converging, SNMP card still coming up) presented
        # hours of restored elapsed time against a 600 s threshold, and the machines that
        # had just come back up were shut down again during normal operation.
        #
        # Placed above the comms-loss branch so it covers that opt-in too, and phrased
        # against `reachable` rather than the flag alone: a device that has answered has
        # confirmed or refuted the whole restored picture in one poll (see _evaluate_ups),
        # so this can only ever hold back the case where there is no fresh evidence at all.
        if not st.reachable and rt.restored_unconfirmed:
            return None

        # Opt-in: a prolonged *pure* comms loss is treated as an outage (independent of battery).
        # Wall clock, not poll count: the loop cadence varies (battery interval), so counting
        # polls would misestimate the elapsed time.
        #
        # ``not st.answered`` is the load-bearing half and it used to be missing. A source
        # can be unreachable while demonstrably talking to us: an SNMP card that answers
        # but names no usable power source, one that implements none of the profile's
        # objects, a upsd without ups.status. All three are correctly reachable=False —
        # the state machine has nothing to go on — but none of them is a *communication*
        # loss, and this branch shuts the whole estate down on one. Reading them as silence
        # fired a real shutdown during normal operation, minutes after the opt-in's
        # threshold, on a device that had never stopped answering.
        # ``incomplete_ups`` is the third half of the same idea and it was missing. An
        # entry stored without an address is never polled at all — poll() returns "not
        # configured" without touching the network — so it is unreachable and silent by
        # construction, for ever. This release describes exactly that state as fail safe
        # ("a standing refusal to shut down every host it feeds", see
        # _warn_about_incomplete_entries), and with the opt-in on it did the opposite: a
        # configuration mistake, a backup import or a hand-edited config.yaml shut down
        # every host that entry feeds, minutes later, during normal operation and without
        # a single packet having been sent. A trigger needs evidence about the power, and
        # an entry that was never asked has produced none.
        if (
            not st.reachable
            and not st.answered
            and not self.cfg.incomplete_ups(u)
            and th.comm_loss_shutdown_after_min is not None
            and rt.unreachable_since is not None
        ):
            elapsed_min = (_now() - rt.unreachable_since).total_seconds() / 60
            if elapsed_min >= th.comm_loss_shutdown_after_min:
                return (
                    f"communication lost for ~{int(elapsed_min)} min "
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
                    f"(contact lost while on battery, countdown kept running)"
                )
            return None

        # Reachable and on battery: the full threshold set.
        if th.on_battery_low and st.battery_low:
            # Name what the device actually said, not only the normalised word: "low"
            # covers an empty battery and a defective one, and both fire this trigger in
            # the first second of an outage. Told apart here or nowhere.
            detail = f" ({st.battery_status_detail})" if st.battery_status_detail else ""
            return f"UPS reports '{st.battery_status}'{detail}"

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

    def _ups_countdown_remaining_s(self, u: UpsBase, rt: _UpsRuntime) -> Optional[int]:
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
        # Held for the same reason the trigger is (see _ups_trigger_reason): a restored
        # timer that no poll has confirmed cannot fire, so counting it down to zero on the
        # dashboard would promise a shutdown that is not going to happen.
        if not rt.state.reachable and rt.restored_unconfirmed:
            return None
        elapsed = self._ups_elapsed_on_battery(rt)
        if elapsed is None:
            return None
        return max(0, th.on_battery_seconds - elapsed)

    def _ups_comm_loss_remaining_s(self, u: UpsBase, rt: _UpsRuntime) -> Optional[int]:
        """Seconds until the opt-in *pure* comms-loss shutdown fires for this UPS, or None."""
        th = self.cfg.effective_thresholds(u)
        if th.comm_loss_shutdown_after_min is None or rt.comm_loss_fired:
            return None
        if rt.state.reachable or rt.unreachable_since is None:
            return None
        # Same gate as the trigger it counts down to (see _ups_trigger_reason): a device
        # that answers but cannot be read is not a communication loss, so there is nothing
        # to count down to. Showing a countdown for a shutdown that will never fire is the
        # dashboard telling the operator the opposite of what the engine will do.
        if rt.state.answered:
            return None
        # And the same for an entry that is never polled at all: the trigger refuses it
        # (see _ups_trigger_reason), so a countdown here would be the card promising the
        # one thing the engine has decided not to do. The "incomplete" chip next to it is
        # what that card should be saying instead.
        if self.cfg.incomplete_ups(u):
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
            kind = str(getattr(h, "type", "pve")).upper()
            return f"{h.name} ({kind}) [{labels}, {policy}]"

        hosts = ", ".join(_desc(h) for h in self.cfg.ordered_hosts()) or "(no hosts)"
        msg = f"Test (dry-run): order {hosts}."
        extra = self._cluster_preview()
        if extra:
            msg += " " + extra
        msg += " NOTHING was shut down."
        await self._emit("Test shutdown executed", msg, db.WARNING)
        return msg

    def _cluster_preview(self) -> str:
        """What the cluster preparation would do, in one line per cluster.

        Built entirely from the last inspection — no I/O. This runs behind a POST the
        operator clicks, and a preview that talks to five nodes would be neither instant
        nor free of side effects.
        """
        if not self.cluster_hosts():
            return ""
        if not self.cluster_states:
            return "No cluster has been inspected yet - run the self-test first."

        own_hostname = _hostname()
        lines = []
        for name, info in self.cluster_states.items():
            members = self._cluster_members(info, name)
            sw = self._cluster_switches(members)
            want_ceph = sw.ceph and not info.ceph_unavailable
            want_disarm = sw.disarm and not info.disarm_unavailable
            steps = []
            if want_disarm:
                steps.append("HA disarm")
            # The same decision the preparation makes, HA precondition included.
            plan = self._guest_plan(info, sw)
            if plan.want:
                # Not named ``targets``: that is the module this engine shuts down
                # through, and a local of the same name shadows it for the whole function.
                stop_list = cluster.stop_targets(info.guests, plan.self_guest, own_hostname)
                steps.append(
                    f"stop {len(stop_list)} of {len(info.running_guests)} running guests"
                    + (f" (sparing {plan.self_guest.label})" if plan.self_guest else "")
                )
            elif plan.requested:
                steps.append(f"guest stop skipped ({plan.block})")
            if want_ceph:
                steps.append(f"Ceph flags {','.join(cluster.CEPH_MAINTENANCE_FLAGS)}")
            if not steps:
                steps.append("nothing (no Ceph, no HA disarm, no guest stop)")
            order = ", ".join(
                h.name + ("*" if h.name in info.mon_nodes else "")
                for h in self.cfg.ordered_hosts()
                if self.host_states.get(h.key, {}).get("cluster_name") == name
            )
            lines.append(
                f"Cluster {name}: " + " -> ".join(steps)
                + (f" -> nodes {order}" if order else "")
                + (" (* carries a Ceph MON and should go last)"
                   if any(h.name in info.mon_nodes for h in members) else "")
            )
        # Once, at the end. The figure is estate-wide (see shutdown_budget), so repeating
        # it on every cluster line read as a per-cluster cost — three clusters looked like
        # three times the budget they actually share.
        return " ".join(lines) + f" Up to {shutdown_budget_s(self.cfg)}s in total."

    def _maybe_prune(self) -> None:
        """Trim the event log once per day so events.db stays bounded over months."""
        today = _local_now().date()
        if self.last_prune_date == today:
            return
        self.last_prune_date = today
        # The history's own prune rides along with the next write (see _history_kick).
        self._history_prune_due = True
        try:
            db.prune()
        except Exception as exc:  # noqa: BLE001 - housekeeping must never affect the loop
            log.warning("Event log prune failed: %s", exc)

    # -- history (#37) ---------------------------------------------------------
    def _maybe_record_history(self) -> None:
        """Queue this iteration's history rows and hand them to the writer thread.

        Never blocks and never waits: the rows go to a background thread through
        _history_kick(), so a slow or hanging disk (an SD card, a full volume) costs the
        history, not the poll loop. A row is taken when a UPS's state changes, on every
        poll while it is on battery (an outage is what the chart is for), and otherwise
        once a minute.

        "On battery" is the engine's own reading — the UPS says so, or an outage timer is
        running — the same one the fast poll cadence and the countdown use, so a UPS that
        goes silent mid-outage still counts as part of it.
        """
        hc = self.cfg.history
        if hc.enabled:
            now = _now().timestamp()
            for u in self.cfg.ups:
                rt = self.ups_rt.get(u.id)
                if rt is None:
                    continue
                st = rt.state
                if st.last_poll is None and st.error is None:
                    continue  # never polled yet: nothing to record
                on_battery = bool(st.on_battery or rt.on_battery_since is not None)
                state = (st.power_source or "unknown") if st.reachable else "unreachable"
                key = (state, on_battery, bool(rt.triggered))
                last = self._history_last.get(u.id)
                if not (
                    last is None
                    or last[1] != key
                    or on_battery
                    or now - last[0] >= HISTORY_INTERVAL_S
                ):
                    continue
                self._history_last[u.id] = (now, key)
                self._history_buf.append((
                    u.id, int(now), state, int(on_battery), int(bool(rt.triggered)),
                    st.runtime_remaining_min if st.reachable else None,
                    st.battery_charge_pct if st.reachable else None,
                    st.load_pct if st.reachable else None,
                ))
        else:
            # Off: nothing is recorded, and switching it back on starts afresh rather than
            # comparing against a state from before the pause.
            self._history_buf.clear()
            self._history_last.clear()
        # The prune runs either way: switching the recording off keeps what was recorded
        # only until it ages out.
        self._history_kick(hc.retention_days)

    def _history_kick(self, retention_days: int) -> None:
        """Start the writer thread for whatever is queued, unless it is still busy."""
        if self._history_task is not None and not self._history_task.done():
            overflow = len(self._history_buf) - HISTORY_BUFFER_MAX
            if overflow > 0:
                del self._history_buf[:overflow]  # the oldest go first
            return
        rows, self._history_buf = self._history_buf, []
        prune = retention_days if self._history_prune_due else None
        self._history_prune_due = False
        if not rows and prune is None:
            return
        self._history_task = asyncio.create_task(
            self._history_write(rows, prune), name="pve-usv-history"
        )

    async def _history_write(self, rows: list[tuple], prune_days: Optional[int]) -> None:
        try:
            await asyncio.to_thread(history.write, rows, prune_days)
        except Exception as exc:  # noqa: BLE001 - a broken history must not spam the log
            if not self._history_failing:
                log.warning("Writing the history failed (rows are dropped): %s", exc)
            self._history_failing = True
        else:
            if self._history_failing:
                log.info("Writing the history works again.")
            self._history_failing = False

    # -- scheduled self-test of the shutdown targets' credentials -----------
    async def _maybe_cluster_startup_check(self) -> None:
        """Inspect the configured clusters once per process start.

        The self-test latch survives a restart (see _restore_selftest_slot), so after the
        appliance itself has been shut down and comes back, the next scheduled run may be
        a whole interval away — a day, by default. That is exactly the window in which the
        cluster is still disarmed with the maintenance flags set: without this, the
        dashboard would show no cluster at all and the "Restore cluster" button would stay
        hidden, because both are driven by cluster_states. Since restoring is deliberately
        manual, that button is the only prompt the operator gets.

        Read-only, concurrent and hard-bounded, so it costs one poll iteration at most.
        Skipped during an outage for the same reason as the self-test: the countdown has
        to stay responsive, and the preparation reads the state it needs itself.
        """
        if self._cluster_startup_done:
            return
        if self._outage_in_progress():
            return  # no latch: try again on the next iteration, once mains are back
        if not self.cluster_hosts():
            self._cluster_startup_done = True
            return
        self._cluster_startup_done = True
        try:
            self.cluster_states = await self._inspect_clusters()
        except Exception as exc:  # noqa: BLE001 - never break the loop over a health read
            log.warning("Cluster startup inspection failed: %s", exc)
            return
        for name, info in self.cluster_states.items():
            if info.needs_recovery:
                # Emitted, not quiet: this is the state a previous outage left behind, and
                # nobody is watching the dashboard at the moment the appliance boots.
                await self._emit(
                    f"Cluster {name}: still prepared for shutdown",
                    "Left over from an earlier outage: "
                    + (
                        f"Ceph flags {', '.join(info.ceph_flags_set)}"
                        if info.ceph_flags_set
                        else ""
                    )
                    + ("; " if info.ceph_flags_set and info.ha_disarmed else "")
                    + (f"HA is {info.ha_armed_state}" if info.ha_disarmed else "")
                    + ". Use 'Restore cluster' once every node is back up.",
                    db.WARNING,
                )

    async def _maybe_node_startup_check(self) -> None:
        """Verify every PVE target's node name once per process start.

        Without this, the answer to "what happens after an update to a config whose node
        name is wrong?" would be "nothing, for up to a day": last_selftest_slot is
        persisted and survives a restart (see _restore_selftest_slot), so the next
        scheduled run may be a whole interval away. The cluster check has had a startup
        override for the same reason; the credential check has none.

        Deliberately NOT the full credential test: that costs up to 10 s per host, while
        this is one GET /nodes per host, read-only and concurrent. It also covers the
        config that arrived by backup import or by hand, which never passed the wizard.
        """
        if self._node_startup_done:
            return
        if self._outage_in_progress():
            return  # no latch: try again on the next iteration, once mains are back
        hosts = [
            h for h in self.cfg.hosts if isinstance(h, PveHostConfig) and h.enabled
        ]
        self._node_startup_done = True
        # First, and outside the node check entirely. These three are pure config
        # questions and have nothing to do with node names, but they used to sit at the
        # BOTTOM of this method, behind two returns: an estate of Proxmox Backup Servers
        # has no PveHostConfig at all, so "if not hosts: return" skipped them — including
        # the stale-feed warning, which the appliance's own comments call the one failure
        # that is otherwise completely silent. It then waited for the next scheduled
        # self-test, up to a day away, instead of the poll after the save that caused it.
        await self._warn_about_duplicate_api_urls()
        await self._warn_about_stale_feeds()
        await self._warn_about_incomplete_entries()
        await self._warn_about_corrected_values()
        await self._warn_about_battery_reserve()
        if not hosts:
            return
        # return_exceptions, for the same reason _fire_stage carries it: targets.verify_node
        # is total in practice, and this makes it structural. Without it one host that
        # manages to raise takes the whole round with it, and every other host loses its
        # verdict over a fault that was not theirs.
        try:
            verdicts = await asyncio.gather(
                *(targets.verify_node(h) for h in hosts), return_exceptions=True
            )
        except Exception as exc:  # noqa: BLE001 - never break the loop over a health read
            log.warning("Node name startup check failed: %s", exc)
            return
        for host, verdict in zip(hosts, verdicts):
            if isinstance(verdict, BaseException):
                log.warning("Node check of %s raised: %s", host.name, verdict)
                # "unverified" is the honest answer to a check that did not happen — and
                # _node_name_ok() reads it as "no verdict", never as a broken target.
                verdict = targets.NodeVerdict(
                    state="unverified",
                    detail=f"The node name could not be verified: {verdict}",
                )
            self.host_states.setdefault(host.key, {})["node_state"] = verdict.state
            if verdict.state in ("ok", "unverified"):
                continue
            # Critical only where the name still decides where the shutdown lands; with
            # its own API URL the entry is addressed as /nodes/localhost and the name is
            # a label. Wrong either way, and worth saying either way.
            load_bearing = not self.cfg.api_url_is_unique(host)
            await self._emit(
                f"Host {host.name}: node name does not match this API",
                verdict.detail
                + (
                    " This entry shares its API URL with another one, so the name is what "
                    "addresses the node — the shutdown will not reach it."
                    if load_bearing
                    else " The shutdown itself is unaffected (it addresses the node behind "
                    "this API URL directly), but every event and dashboard entry carries "
                    "this name. Correct it on the host card."
                ),
                db.CRITICAL if load_bearing else db.WARNING,
            )

    async def _maybe_selftest(self) -> None:
        """Run the credential self-test once per scheduled slot (see selftest_slot())."""
        cfg = self.cfg
        if not cfg.selftest_enabled or not cfg.hosts:
            return
        # Never spend the poll budget on credential checks during an outage: every host
        # costs up to 10 s and the battery countdown has to stay responsive. No latch is
        # set, so the test runs at the next slot once mains are back.
        if self._outage_in_progress():
            return

        # A re-arm asked for one run, off-schedule. Cleared only when it actually runs,
        # so an outage arriving right after the re-arm postpones it instead of losing it.
        # force_log because "everything is fine again" is the answer being looked for, and
        # a silent success would be indistinguishable from no check at all.
        if self._rearm_selftest_pending:
            self._rearm_selftest_pending = False
            await self._run_selftest(force_log=True)
            return

        slot = selftest_slot(_local_now(), cfg.selftest_hour, cfg.selftest_interval_min)
        # ">" rather than "!=": lowering the start hour at runtime moves the slot
        # backwards, which must not fire an extra run.
        if self.last_selftest_slot is not None and slot <= self.last_selftest_slot:
            return
        # Latch and persist *before* running: a crash halfway through the test must not
        # turn into a restart loop that hammers the Proxmox API.
        self.last_selftest_slot = slot
        self._persist_state()
        await self._run_selftest()

    async def run_selftest_now(self) -> tuple[bool, str]:
        """Run the scheduled checks immediately, on explicit request.

        Needed because saving settings deliberately does NOT fire a self-test (see
        update_config) and the next slot may be hours away: after changing a token or
        ticking the cluster option there has to be a way to see the result now.

        Refused during an outage for the same reason _maybe_selftest skips then — every
        host costs up to 10 s and the battery countdown has to stay responsive.
        """
        if self._outage_in_progress():
            return False, "Not while a power outage is in progress."
        if not self.cfg.hosts:
            return False, "No hosts configured."
        # An explicit check should report the full current picture, so the "say it once"
        # latch for a missing disarm-ha endpoint does not apply here.
        self._disarm_unsupported_warned.clear()
        await self._run_selftest(force_log=True)
        hosts = self.cfg.ordered_hosts()
        ok = sum(
            1
            for h in hosts
            if self.host_states.get(h.key, {}).get("credentials_ok")
            and self.host_states.get(h.key, {}).get("power_mgmt_ok")
        )
        msg = f"{ok} of {len(hosts)} hosts ok"
        if self.cluster_states:
            msg += f", {len(self.cluster_states)} cluster(s) checked"
        return ok == len(hosts), msg + ". See the event log for details."

    def _node_name_ok(self, host: HostConfig, node_state: str) -> bool:
        """Whether a node-name verdict should count against this host.

        The severity follows whether the name is load-bearing at all. With one API URL per
        entry the shutdown addresses /nodes/localhost and a wrong name is only a
        misleading label — reported, but not a broken target. Where entries share a URL,
        PVE's proxying is the only thing telling them apart, the name IS the path, and the
        same verdict means the shutdown will not land.
        """
        if node_state in ("ok", "unverified"):
            return True
        return self.cfg.api_url_is_unique(host)

    async def _warn_about_corrected_values(self) -> None:
        """Report settings that were out of range and had to be pulled back.

        config._repair() corrects rather than refuses, because load_config() is called
        from the lifespan without a guard: rejecting a stored value would mean the service
        no longer starts, which is a far worse failure than the value. Correcting silently
        would be worse again — the operator would be running thresholds nobody chose — so
        the loudness lives here. Once per distinct correction and configuration.
        """
        for line in self.cfg.value_corrections():
            if line in self._value_corrections_warned:
                continue
            self._value_corrections_warned.add(line)
            await self._emit(
                "Setting out of range — corrected",
                f"{line}. Check it under Settings; the appliance is running with the "
                f"value named here, not with the one that was stored.",
                db.WARNING,
            )

    async def _warn_about_battery_reserve(self) -> None:
        """Warn when the shutdown takes longer than the runtime its trigger waits for.

        Warned about, never acted on: lowering someone's trigger for them is not this
        appliance's decision.

        It used to live inside _check_self_guest(), which _check_clusters() only calls for
        a Ceph cluster — so the majority of installations (standalone nodes, Backup Servers,
        clusters without Ceph) never saw the one warning that says "your trigger fires later
        than your shutdown lasts". The terms differ per estate; the question does not, since
        the stages, the host timeout and the notification round apply everywhere.

        Once per distinct wording and configuration, the same rule as the warnings next
        door: repeating an unchanged one every self-test trains the operator to skip the feed.
        """
        # The SMALLEST effective trigger in the estate, not the global one alone. The
        # threshold is overridable per UPS (see AppConfig.effective_thresholds), and an
        # override is precisely where a reserve too short for the shutdown gets set — a
        # global 30 min said nothing about the one device configured to fire at 2.
        candidates: list[tuple[int, str]] = []
        for u in self.cfg.ups:
            value = self.cfg.effective_thresholds(u).runtime_below_minutes
            if value is None:
                continue
            # Named only where it really is this device's own value; naming the UPS for an
            # inherited number would send the operator to the wrong field.
            own = u.overrides.runtime_below_minutes is not None
            candidates.append((int(value), u.label if own else ""))
        if not self.cfg.ups and self.cfg.thresholds.runtime_below_minutes is not None:
            # No UPS configured yet: the question is still about the estate, and the
            # wizard is exactly where this is worth reading.
            candidates.append((int(self.cfg.thresholds.runtime_below_minutes), ""))
        if not candidates:
            return
        reserve, source = min(candidates, key=lambda c: c[0])
        budget = shutdown_budget(self.cfg)
        if reserve * 60 >= budget.total:
            return
        body = (
            f"The shutdown trigger fires at {reserve} min ({reserve * 60}s) of estimated "
            f"runtime"
            + (f" (set on {source})" if source else "")
            + f", but shutting this estate down can take up to {budget.total}s "
            f"({budget.explain()}). Raise the trigger, shorten the timeouts, or give fewer "
            f"hosts distinct shutdown orders."
        )
        if body in self._reserve_warned:
            return
        self._reserve_warned.add(body)
        await self._emit("Battery reserve is shorter than the shutdown", body, db.WARNING)

    async def _warn_about_stale_feeds(self) -> None:
        """Name hosts pointing at UPS devices that are not configured any more.

        The one failure in this appliance that is completely silent: feed_ids_for() drops
        unknown ids, _host_trigger_reason() returns None on an empty feed list, and the
        host is then never eligible — no event, no alarm, no failed test. It looks exactly
        like a host waiting for an outage that has not come.

        Deliberately not left to the scheduled self-test alone. That one stands down for
        the whole duration of an outage (and rightly so), which is precisely when this
        would matter; both callers here run on the poll after a configuration is saved,
        which is when the operator actually caused it. Said once per host and config, the
        same rule as the duplicate-URL warning next door.
        """
        for host in self.cfg.hosts:
            if not host.enabled:
                continue
            stale = self.cfg.stale_feed_ids(host)
            if not stale or host.key in self._stale_feed_warned:
                continue
            self._stale_feed_warned.add(host.key)
            remaining = self.cfg.feed_ids_for(host)
            if remaining:
                tail = (
                    f"It still has {len(remaining)} of {len(host.ups_ids)} feeds, so with "
                    f"policy '{host.ups_policy}' it now acts on those alone — the "
                    f"redundancy this entry was configured for is gone."
                )
            else:
                tail = (
                    "It has no feeding UPS left at all, which means this host will NEVER "
                    "be shut down. Re-assign it under Settings -> Hosts."
                )
            await self._emit(
                f"Host {host.name}: unknown UPS assignment",
                f"Assigned to {', '.join(stale)}, which no longer exists. {tail}",
                db.CRITICAL,
            )

    async def _warn_about_incomplete_entries(self) -> None:
        """Name entries that are stored but cannot do their job (see incomplete_entries()).

        The server-side half of the browser's ``incompleteCards()``, and it is needed
        because the two paths where this actually happens never pass a form: a backup
        import and a hand-edited config.yaml. An enabled host without an API URL or a
        token is stored, renders complete on the dashboard, and fails during the outage —
        while targets.verify_node() answers "unverified" for an entry it cannot even
        address, which _node_name_ok() correctly reads as *no verdict* rather than as a
        fault. Until this existed, the first real signal was the next scheduled self-test.

        CRITICAL for the two that break the shutdown, WARNING for a webhook (an alarm that
        cannot be delivered is not a machine that stays up). Said once per distinct finding
        and configuration, the same rule as the warnings next door.
        """
        for kind, label, missing in self.cfg.incomplete_entries():
            line = f"{kind} {label}: {missing}"
            if line in self._incomplete_warned:
                continue
            self._incomplete_warned.add(line)
            if kind == "Webhook":
                tail = (
                    "Nothing is sent to this target. Complete it or switch it off under "
                    "Settings -> Notifications."
                )
                sev = db.WARNING
            elif kind == "UPS":
                tail = (
                    "This device is never polled, so it counts as permanently unreachable "
                    "— which is an alarm and, being fail safe, a standing refusal to shut "
                    "down every host it feeds. Complete it under Settings -> UPS."
                )
                sev = db.CRITICAL
            else:
                tail = (
                    "The shutdown cannot reach this host and will only fail during an "
                    "outage. Complete it, or untick 'active', under Settings -> Hosts."
                )
                sev = db.CRITICAL
            await self._emit(
                f"{kind} {label}: incomplete configuration",
                f"Stored with {missing}. {tail}",
                sev,
            )

    async def _warn_about_duplicate_api_urls(self) -> None:
        """One warning per self-test run for URLs serving more than one enabled entry.

        Nearly always a copy-paste slip, and the state in which the node name is back in
        the shutdown path — so it is worth saying even though the UI flags it while
        editing: a config restored from a backup or edited by hand never passes the UI.

        Said once per URL and config: the startup check and the self-test both call this,
        and repeating an unchanged warning every run trains the operator to skip the feed.
        """
        for url in self.cfg.duplicate_api_urls():
            if url in self._dup_url_warned:
                continue
            self._dup_url_warned.add(url)
            names = [
                h.name for h in self.cfg.hosts
                if h.enabled and h.api_url.strip().rstrip("/").lower() == url
            ]
            await self._emit(
                "Several hosts share one API URL",
                f"{url} is configured for {', '.join(names)}. Every node needs its own "
                f"API URL — one that is already powered off cannot forward the shutdown "
                f"for the others. Until this is fixed, those entries are addressed by "
                f"their configured node name, so that name has to be exactly right.",
                db.WARNING,
            )

    async def _run_selftest(self, force_log: bool = False) -> None:
        """Verify token + power-management privilege per host. Success is logged quietly
        (no notify), failure is emitted (notify) so a broken credential is noticed.

        ``force_log`` bypasses the daily throttle on the quiet "ok" lines — an explicitly
        requested run must show its result, not stay silent because today's line is
        already in the log."""
        hosts = self.cfg.ordered_hosts()
        # Concurrently: sequentially, five unreachable hosts would stall the poll loop for
        # 5 x 10 s. gather preserves the order, so the events stay in host order.
        # return_exceptions like the shutdown stages: targets.test_connection is total in
        # practice, and this makes it structural. One host that raises would otherwise
        # abort the whole round — every other host keeps a stale verdict, no failure event
        # is written, and the exception travels up into _loop()'s catch-all, which costs
        # this iteration its housekeeping too.
        results = await asyncio.gather(
            *(targets.test_connection(h) for h in hosts), return_exceptions=True
        )
        results = [
            r
            if not isinstance(r, BaseException)
            else targets.TestResult(False, f"Connection error: {r}")
            for r in results
        ]
        today = _local_now().date()
        # At a 15-minute cadence a quiet "ok" per host and run would be ~100 events per
        # host per day and drown the 48 h event feed. Write one per day, plus whenever the
        # test recovers from a failure. Computed once, before the flags below are updated,
        # and shared with the cluster check so both follow the same cadence.
        log_daily = (
            force_log
            or self.last_selftest_ok is False
            or self.last_selftest_ok_logged != today
        )
        # ...and the opt-out on top of it, for an estate whose operator is the only one who
        # ever touches the tokens and reads the daily green line as noise. Deliberately only
        # the routine "ok" lines: the check itself keeps running at its configured interval,
        # so a credential that breaks is still caught within the hour rather than at the
        # outage. Two green lines are never routine and survive the switch:
        #   force_log — an explicitly requested run. Pressing "Test now" and being told
        #               nothing is indistinguishable from a broken button.
        #   a recovery (last_selftest_ok is False) — it closes out a failure that WAS
        #               reported, so suppressing it would leave the log claiming the
        #               credential is still broken.
        # log_daily, NOT this, is what the dry-run notice below hangs on — that one is a
        # standing misconfiguration, not a report of success, and silencing the good news
        # must not silence it too.
        log_ok = (
            force_log
            or self.last_selftest_ok is False
            or (log_daily and self.cfg.selftest_log_ok)
        )
        ok_all = True
        for host, result in zip(hosts, results):
            # Keep the outcome per host: it is what /api/status and /api/health report,
            # so a credential that broke months before an outage is visible on the
            # dashboard instead of only in the event log.
            host_ok = result.ok and result.has_power_mgmt and self._node_name_ok(
                host, result.node_state
            )
            self.host_states.setdefault(host.key, {}).update(
                {
                    "credentials_ok": result.ok,
                    "power_mgmt_ok": result.has_power_mgmt,
                    "node_state": result.node_state,
                    "last_test_at": _now().isoformat(),
                    "last_test_error": None if host_ok else result.message,
                }
            )
            if host_ok:
                if log_ok:
                    self._log_quiet(f"Self-test {host.name}: ok", result.message, db.INFO)
                else:
                    log.info("Self-test %s: ok — %s", host.name, result.message)
            else:
                ok_all = False
                sev = db.WARNING if result.ok else db.CRITICAL
                await self._emit(f"Self-test {host.name}: FAILED", result.message, sev)
        if ok_all:
            self.last_selftest_ok_logged = today
        self.last_selftest_ok = ok_all
        self.last_selftest_at = _now()
        # The one thing a green self-test does not say. Every host can be reachable, every
        # token valid and every privilege in place while the master switch means none of it
        # is ever used — and dry_run defaults to on, so this is the state a half-finished
        # commissioning leaves behind. Quiet and on the same daily cadence as the "ok"
        # lines (log_daily), because it is a standing condition, not an incident — but on
        # log_daily rather than log_ok, so switching the green lines off does not also
        # switch off the one line that says the appliance will not actually do anything.
        if self.cfg.dry_run and self.cfg.configured and log_daily:
            self._log_quiet(
                "Dry-run is on — nothing will be shut down",
                "The credential check above says the shutdown targets are ready, but the "
                "master safety switch is still set: the engine only logs what it would do. "
                "Switch dry-run off under Settings once the configuration is tested.",
                db.WARNING,
            )
        await self._warn_about_duplicate_api_urls()
        await self._warn_about_stale_feeds()
        await self._warn_about_incomplete_entries()
        await self._warn_about_corrected_values()
        await self._warn_about_battery_reserve()
        # Cluster health rides along with the credential check: same schedule, and it is
        # suspended during an outage for the same reason (see _maybe_selftest).
        await self._check_clusters(log_ok=log_ok)

    async def _prepare_clusters(
        self, eligible: list[tuple[HostConfig, str]]
    ) -> list[tuple[HostConfig, str]]:
        """Run the cluster preparation once per cluster, before any of its nodes go down.

        Returns the hosts that may proceed. Ordinarily that is all of them: a failed
        preparation leaves the HA manager armed, and an armed LRM still stops the guests
        itself — degraded, but the machines come down. Only when the user explicitly opted
        into ``cluster_abort_on_prep_failure`` are that cluster's hosts held back, because
        the alternative there is losing power uncontrolled.
        """
        th = self.cfg.thresholds
        # Freeze the candidate list per iteration, like the UPS list in _loop(): a config
        # save during the awaits below can swap self.cfg underneath us.
        candidates = [
            (host, reason)
            for host, reason in eligible
            if isinstance(host, PveHostConfig) and host.cluster
        ]
        if not candidates:
            return eligible

        ap = self.cfg.appliance
        own_hostname = _hostname()
        # Hosts pulled in because their cluster goes down as a unit. Collected here and
        # merged once at the end, so the canonical ordering is rebuilt exactly once.
        extra: list[tuple[HostConfig, str]] = []
        cold = await self._cold_inspect(candidates)

        for host, reason in candidates:
            # Already established this episode that its cluster cannot be reached. The
            # latch is what keeps this from repeating: with abort-on-failure the host is
            # filtered out below, so it never fires, stays eligible, and comes back here
            # on the next poll — every eight seconds on battery, each round paying for a
            # full inspection and sending another CRITICAL. That is the same knot 4.0.0
            # untied for the preparation itself; this branch had been left out of it.
            # Cleared with the other per-episode latches, so a re-arm inspects afresh.
            if host.key in self.cluster_inspect_failed or host.key in self.cluster_standalone:
                continue
            # The stamp, or a cluster inspected earlier in THIS loop that names this node.
            # Only _inspect_clusters() writes the stamp, and that one stands down for the
            # whole duration of an outage — so on a cold start every candidate used to fall
            # through to its own inspect() below. Shared with _cold_inspect() so the
            # pre-pass reads exactly the set this loop will ask for.
            name, info = self._known_cluster(host)
            # The name may still be unknown when no self-test has run yet — read it now,
            # from the node we are about to shut down, and KEEP that reading: it already
            # carries the feature detection the writes below depend on. Re-fetching from
            # cluster_states instead would throw it away and fall back to guessing.
            if info is None:
                # From the concurrent pre-pass above, never a fresh sequential read: a
                # cluster whose API is down answers nothing, so nothing gets stamped, and
                # every remaining candidate used to pay its own inspect() — three nodes
                # meant three times inspect()'s full budget, spent before the first
                # machine (cluster or not) was asked to shut down.
                discovered = cold.get(host.key)
                if discovered is None:
                    continue
                if not discovered.reachable or not discovered.is_cluster:
                    if not discovered.reachable:
                        # Silent before, and that silence defeated the opt-in: no event,
                        # no cluster_prep_failed entry, and — because the name was never
                        # established — nothing for the abort filter to key on. Someone
                        # who ticked "abort on failure" got their nodes powered off with
                        # HA still armed and no Ceph flags, and the log said nothing.
                        self.cluster_inspect_failed.add(host.key)
                        await self._emit(
                            f"Cluster of {host.name}: not reachable for preparation",
                            "The cluster could not be inspected, so HA was not disarmed "
                            "and no Ceph flags were set. "
                            + (
                                "'Abort on failure' is on, so this node is held back."
                                if th.cluster_abort_on_prep_failure
                                else "The node is shut down anyway, unprepared."
                            ),
                            db.CRITICAL,
                        )
                    else:
                        # Reachable, and settled: this host is not a cluster member at
                        # all. Latched for the episode because the answer cannot change
                        # within it, and without the latch every poll on which this host
                        # is still due — a retried shutdown — paid for another full
                        # inspection (up to INSPECT_BUDGET_S plus the grace) off the
                        # battery, for a question already answered. Said once as well:
                        # the "this host belongs to a cluster" tick does nothing here,
                        # and nothing else in the product would ever mention it.
                        self.cluster_standalone.add(host.key)
                        self._log_quiet(
                            f"{host.name}: cluster preparation does not apply",
                            "This host is marked as a cluster member, but its API "
                            "reports a standalone node — no cluster record, so there is "
                            "no HA manager to disarm and no Ceph flags to set. It is "
                            "shut down normally. Untick 'this host belongs to a cluster' "
                            "on its card to say so.",
                            db.INFO,
                        )
                    continue
                name = discovered.name or host.name
                self.cluster_states.setdefault(name, discovered)
                info = self.cluster_states[name]
                # Stamped on every member, not only on the node that happened to answer.
                # _cluster_members() matches against the node list this reading just
                # brought back, which is what saves the other candidates their own
                # inspect() — and it is also the entry the abort-on-failure filter below
                # keys on, so a held-back cluster now covers all of its nodes on a cold
                # start too.
                for member in self._cluster_members(info, name):
                    self.host_states.setdefault(member.key, {})["cluster_name"] = name
                self.host_states.setdefault(host.key, {})["cluster_name"] = name

            if self.cluster_prepared.get(name):
                continue
            self.cluster_prepared[name] = True  # latch first: never prepare twice

            # Who else belongs to this cluster, and how many of them asked for this on
            # their own. Both numbers go into the announcement: a preparation that runs
            # on a partial outage is the one case where the event log has to say so.
            members = self._cluster_members(info, name)
            due_keys = {h.key for h, _ in eligible} | {
                k for k, fired in self.host_fired.items() if fired
            }
            due = [m for m in members if m.key in due_keys]

            # Ask for nothing this cluster cannot do. Ceph flags need Ceph, disarm-ha
            # needs PVE 9.2+ — and both are told apart from "could not read" (see
            # ClusterInfo.ceph_unavailable / .disarm_unavailable), so a denied read still
            # attempts the write instead of silently skipping it. Without this, a cluster
            # without Ceph reported a FAILED preparation on every single outage, and with
            # abort-on-failure it held every node back over a component that is not there.
            #
            # What is WANTED comes from the cluster's members, not from the node that
            # happened to trigger first (see _cluster_switches): the preview and the
            # self-test have always read it that way, and reading it differently here is
            # what made them promise steps the preparation then skipped.
            sw = self._cluster_switches(members)
            want_ceph = sw.ceph and not info.ceph_unavailable
            want_disarm = sw.disarm and not info.disarm_unavailable

            # The cluster-wide guest stop: always with Ceph, where it is not an option but
            # the first step of the official procedure (letting each node stop its own
            # guests as it powers off drops the pool below min_size, and the guests still
            # running on the survivors then block on IO and never finish shutting down),
            # and on its own switch without Ceph (#43), where it trades the configured
            # startup order for a shutdown that fits the battery. See _guest_plan().
            plan = self._guest_plan(info, sw)
            self_guest = plan.self_guest
            guest_needs_disarm = plan.needs_disarm
            guest_block = plan.block
            want_guests = plan.want

            def _guest_clause() -> str:
                return _prep_intent(plan.asked, want_guests, guest_block)

            if self.cfg.dry_run:
                await self._emit(
                    f"DRY-RUN: cluster {name} would be prepared",
                    f"HA disarm: "
                    f"{_prep_intent(sw.disarm, want_disarm, 'needs PVE 9.2+')}; "
                    f"guest shutdown: {_guest_clause()}"
                    + (
                        f" ({len(cluster.stop_targets(info.guests, self_guest, own_hostname))} "
                        f"guests"
                        + (f", sparing {self_guest.label}" if self_guest else "")
                        + ")"
                        if want_guests
                        else ""
                    )
                    + f"; Ceph flags: {_prep_intent(sw.ceph, want_ceph, 'no Ceph')}"
                    + self._unit_clause(host, members, due)
                    + ". NOTHING is changed.",
                    db.WARNING,
                )
                extra += self._unit_additions(host, name, members, eligible, extra, reason)
                continue

            if not want_ceph and not want_disarm and not want_guests:
                # Quiet, not a failure: the operator asked for features this cluster does
                # not have. Saying it once per outage beats a CRITICAL that reads as if
                # the shutdown went wrong.
                self._log_quiet(
                    f"Cluster {name}: nothing to prepare",
                    f"HA disarm: "
                    f"{_prep_intent(sw.disarm, False, 'needs PVE 9.2+')}; "
                    f"guest shutdown: {_guest_clause()}; "
                    f"Ceph flags: {_prep_intent(sw.ceph, False, 'no Ceph')}. "
                    f"The nodes are shut down normally.",
                    db.INFO,
                )
                # A guest stop that was asked for and cannot run is not "nothing": without
                # Ceph it may be the only step ticked, and its refusal must still be said.
                if plan.requested and guest_block:
                    await self._report_guest_skip(name, plan, want_ceph)
                extra += self._unit_additions(host, name, members, eligible, extra, reason)
                continue

            # Said BEFORE the work, not only after it: the preparation is the one step
            # that deliberately delays the shutdown, for up to cluster_prep_timeout_s.
            # Without this line the event log jumped straight from "power outage" to a
            # result a minute later, and nothing said that the nodes were waiting on
            # purpose. WARNING like the outage and the shutdown itself, so the chain is
            # complete in the webhook too; once per cluster and episode (the latch above
            # is already set), so it cannot repeat every eight seconds.
            budget = th.cluster_prep_timeout_s + (
                th.cluster_guest_shutdown_timeout_s if want_guests else 0
            )
            await self._emit(
                f"Cluster {name}: preparing for shutdown",
                f"HA disarm: "
                f"{_prep_intent(sw.disarm, want_disarm, 'needs PVE 9.2+')}; "
                f"guest shutdown: {_guest_clause()}"
                + (
                    f" ({len(cluster.stop_targets(info.guests, self_guest, own_hostname))} "
                    f"guests"
                    + (f", sparing {self_guest.label}" if self_guest else "")
                    + ")"
                    if want_guests
                    else ""
                )
                + f"; Ceph flags: {_prep_intent(sw.ceph, want_ceph, 'no Ceph')}."
                + self._unit_clause(host, members, due)
                + f" The nodes of this cluster wait up to {budget}s for this"
                + (
                    " and are held back entirely if it fails ('abort on failure')."
                    if th.cluster_abort_on_prep_failure
                    else ", then shut down whether it worked or not."
                ),
                db.WARNING,
            )

            # Said before the work, so it is not buried in a result line a minute later.
            if plan.requested and guest_block:
                await self._report_guest_skip(name, plan, want_ceph)

            result = await cluster.prepare(
                host,
                want_ceph=want_ceph,
                want_disarm=want_disarm,
                want_guests=want_guests,
                guests=info.guests,
                self_guest=self_guest,
                guest_needs_disarm=guest_needs_disarm,
                hostname=own_hostname,
                timeout=th.cluster_prep_timeout_s,
                guest_timeout=th.cluster_guest_shutdown_timeout_s,
                force_after_s=th.cluster_guest_force_after_s,
            )
            # Latched, not re-derived per iteration: the preparation runs once per
            # episode, so this is the only moment the outcome is known — and the only
            # moment these two events may be written.
            self.cluster_prep_failed[name] = not result.ok
            self.cluster_prep_steps[name] = list(result.steps)
            await self._emit(
                f"Cluster {name}: preparation {'done' if result.ok else 'FAILED'}",
                result.message,
                db.WARNING if result.ok else db.CRITICAL,
            )
            if not result.ok and th.cluster_abort_on_prep_failure:
                await self._emit(
                    f"Cluster {name}: shutdown aborted",
                    "The cluster preparation failed and 'abort on failure' is enabled, "
                    "so no node of this cluster will be shut down.",
                    db.CRITICAL,
                )

            extra += self._unit_additions(host, name, members, eligible, extra, reason)
            # The state the field test ran into: the preparation is cluster-wide — HA
            # disarmed, with the guest stop every guest stopped — while only the nodes
            # whose own UPS triggered go down. The rest stand there without guests,
            # without HA and with the maintenance flags set, and on a hyper-converged
            # cluster their storage is gone too once the monitors follow. Said out loud
            # rather than left to be discovered afterwards.
            if not sw.unit and len(due) < len(members):
                left = ["HA disarmed"]
                if want_guests:
                    left.append("every guest stopped")
                if want_ceph:
                    left.append("the Ceph maintenance flags set")
                await self._emit(
                    f"Cluster {name}: only part of the cluster is shut down",
                    f"{len(due)} of {len(members)} nodes triggered, and 'shut the whole "
                    f"cluster down' is off, so "
                    f"{', '.join(m.name for m in members if m not in due)} keep running — "
                    f"with "
                    + (", ".join(left[:-1]) + " and " + left[-1] if len(left) > 1
                       else left[0])
                    + ". Switch the option on, or feed every node of this cluster from "
                    "UPS devices that fail together.",
                    db.CRITICAL,
                )

        if extra:
            # Re-sorted through ordered_hosts() rather than appended: _evaluate_hosts
            # groups by (this_host, order) and relies on that sort making the groups
            # contiguous, so tacking the additions onto the end would split a stage in two
            # and could let the appliance's own host go before its peers.
            merged = {h.key: (h, r) for h, r in eligible + extra}
            eligible = [
                merged[h.key] for h in self.cfg.ordered_hosts() if h.key in merged
            ]

        # From the latch, not from the work of this iteration: the preparation happens
        # once per episode, while this filter has to hold on every poll that follows.
        # Reading it off the current round instead let the hosts through on the very next
        # one, silently undoing the opt-in the operator chose.
        if not th.cluster_abort_on_prep_failure:
            return eligible
        blocked = {name for name, failed in self.cluster_prep_failed.items() if failed}
        if not blocked and not self.cluster_inspect_failed:
            return eligible
        return [
            (host, reason)
            for host, reason in eligible
            if self.host_states.get(host.key, {}).get("cluster_name") not in blocked
            and host.key not in self.cluster_inspect_failed
        ]

    async def _report_guest_skip(self, name: str, plan: GuestPlan, want_ceph: bool) -> None:
        """Say why a cluster-wide guest stop that was asked for will not run.

        CRITICAL with Ceph, because there each reason means the cluster is about to lose
        power with its guests still writing to Ceph — the exact failure the guest stop
        exists to prevent. Without Ceph a WARNING: the nodes then stop their own guests as
        they power off, slower but intact. "no Ceph" (only Ceph asked, none here) never
        gets here — plan.requested is False then, the step simply does not apply.
        """
        await self._emit(
            f"Cluster {name}: guest shutdown skipped",
            {
                "guest list unreadable": (
                    "The cluster's guest list could not be read (the token needs "
                    "VM.Audit), so it is unknown what would have to stop. Note that this "
                    "endpoint filters by privilege instead of refusing, so an empty "
                    "answer is never taken to mean 'no guests'."
                ),
                "needs HA disarm (PVE 9.2+)": (
                    "HA manages guests on this cluster and cannot be disarmed (disarm-ha "
                    "needs Proxmox VE 9.2 or newer, or the switch is off), so the HA "
                    "manager would restart the guests as fast as they are stopped."
                ),
            }.get(
                plan.block,
                "This appliance's own guest could not be identified "
                f"({plan.source}), and stopping every guest would have stopped this "
                "appliance in the middle of the outage. Pick it under Settings -> "
                "Appliance.",
            )
            + (
                " The nodes are shut down without it, which is what earlier releases did."
                if want_ceph
                else " The nodes are shut down without it; each one stops its own guests "
                "as it powers off, in their configured order."
            ),
            db.CRITICAL if want_ceph else db.WARNING,
        )

    def _known_cluster(
        self, host: HostConfig
    ) -> tuple[str, Optional["cluster.ClusterInfo"]]:
        """This host's cluster as far as anything already knows it: (name, info).

        Two ways in, and both matter. The ``cluster_name`` stamp is written by
        _inspect_clusters() and by the preparation's own cold path; the node list of a
        cluster inspected earlier in the same pass names members that were never stamped.
        ``info`` is None exactly when somebody still has to read the cluster — which is the
        one question _cold_inspect() and _prepare_clusters() must answer identically, or
        the pre-pass inspects the wrong set and the loop falls through with nothing.
        """
        name = self.host_states.get(host.key, {}).get("cluster_name") or ""
        if not name:
            name = next(
                (n for n, i in self.cluster_states.items() if host.name in i.nodes), ""
            )
        return name, (self.cluster_states.get(name) if name else None)

    async def _cold_inspect(
        self, candidates: list[tuple[HostConfig, str]]
    ) -> dict[str, "cluster.ClusterInfo"]:
        """Read the cluster for every candidate that still has none — one read where one
        read is enough, concurrently where it is not.

        The cold start: only _inspect_clusters() writes the ``cluster_name`` stamp, and it
        stands down for the whole duration of an outage — so an outage arriving before the
        first self-test (a restart, a configuration saved moments earlier) leaves the
        preparation to read the cluster from inside the shutdown path.

        One reading normally answers for the whole cluster, because it brings back the node
        list and the caller stamps every member from it. That is the 4.1.0 behaviour and it
        is kept exactly: the first candidate is read alone, and whatever its answer covers
        is not read again.

        What it does not cover was the hole. A cluster whose API is down names nobody, so
        every remaining candidate fell through to its own sequential read of up to
        INSPECT_BUDGET_S plus the grace — three nodes meant close to a minute, spent before
        the first machine was asked to shut down, cluster member or not. Those go together
        now, so the rest of the phase costs one reading's time however many are left. Two
        genuinely distinct clusters take the same path, for the same reason.

        Read-only and total: cluster.inspect() never raises and never outlasts its budget,
        and _inspect_one() makes that structural rather than a promise — which is why the
        gather below needs no return_exceptions of its own.
        """
        pending = [
            host
            for host, _reason in candidates
            if host.key not in self.cluster_inspect_failed
            and host.key not in self.cluster_standalone
            and self._known_cluster(host)[1] is None
        ]
        if not pending:
            return {}
        out: dict[str, "cluster.ClusterInfo"] = {}
        first, rest = pending[0], pending[1:]
        out[first.key] = await self._inspect_one(first)
        # Everything the first reading names is answered by it — the caller stamps those
        # members and never asks again. Only what it leaves open is read, and then at once.
        covered = set(out[first.key].nodes)
        rest = [h for h in rest if h.name not in covered]
        if rest:
            results = await asyncio.gather(*(self._inspect_one(h) for h in rest))
            out.update(zip((h.key for h in rest), results))
        return out

    async def _inspect_one(self, host: PveHostConfig) -> "cluster.ClusterInfo":
        """One read-only inspection that cannot raise, for the cold-start pre-pass.

        cluster.inspect() is total and bounded already; this makes the guarantee structural
        at the one call site that is on the shutdown path with no other net under it.
        """
        try:
            return await cluster.inspect(host)
        except Exception as exc:  # noqa: BLE001 - never break the shutdown over a read
            log.warning("Cluster inspection of %s raised: %s", host.name, exc)
            # Unreachable is the honest reading of an inspection that did not happen, and
            # it is the one the caller already handles: a critical event and, with "abort
            # on failure", the node held back.
            return cluster.ClusterInfo(reachable=False, error=str(exc))

    def _cluster_members(self, info: "cluster.ClusterInfo", name: str) -> list[PveHostConfig]:
        """Every enabled PVE host that asked for cluster handling and belongs to this one.

        Two ways to match, because a shutdown can happen before any self-test has run:
        the discovered cluster name in host_states, or the node list the API just gave us.
        The second is the same verbatim name comparison cluster.node_coverage() makes —
        the shutdown call uses the name literally, so anything looser would be a lie.

        ``h.cluster`` is part of the question, and leaving it out was a hole. This list is
        what _unit_additions() shuts down as a unit, so a node that merely appears in the
        API's node list — with the cluster switch deliberately NOT ticked, its own UPS
        perfectly happy — was powered off by another node's outage. It was also the half
        that silenced the warning meant to catch exactly that: _check_cluster_feeds() did
        filter on ``h.cluster`` and returned early below two members, so the one
        configuration in which the action over-reached was the one in which nothing said
        so. One membership rule now, shared by the preparation, the unit additions, the
        preview and the health checks.

        cluster.node_coverage() is deliberately NOT affected: it counts over every enabled
        PVE target, because its question is "who will nobody shut down", not "who takes
        part in the preparation".
        """
        nodes = {n for n in info.nodes if n}
        found: dict[str, PveHostConfig] = {}
        for h in self.cfg.hosts:
            if not isinstance(h, PveHostConfig) or not h.enabled or not h.cluster:
                continue
            known = self.host_states.get(h.key, {}).get("cluster_name")
            if (name and known == name) or h.name in nodes:
                found[h.key] = h
        return list(found.values())

    @staticmethod
    def _cluster_switches(members: list[PveHostConfig]) -> ClusterSwitches:
        """The cluster switches as ONE answer per cluster.

        They are edited per host card because that is where a host is configured, but
        every one of them describes something cluster-wide: Ceph maintenance flags, the
        HA manager's arm state, whether the cluster goes down as a unit, and the guest
        stop. Nothing stops them from being ticked differently on two nodes of the same
        cluster.

        Answered in one place because the answer used to depend on who was asking. The
        preparation read them off the candidate that happened to trigger first, while the
        shutdown preview and the scheduled health check both read ``any(member)`` — so
        with the switches set unevenly, the self-test promised Ceph flags and an HA disarm
        that the real preparation then skipped, and which of the two was right depended on
        which node's UPS failed. ``any`` is the safe reading of the three: preparing a
        cluster nobody asked to prepare costs a maintenance flag, skipping one that was
        asked for costs the storage.

        _check_cluster_feeds() reports a cluster whose members disagree, so this never
        silently papers over a configuration mistake.
        """
        return ClusterSwitches(
            ceph=any(h.cluster_ceph for h in members),
            disarm=any(h.cluster_ha_disarm for h in members),
            unit=any(h.cluster_shutdown_all for h in members),
            guest_stop=any(h.cluster_guest_stop for h in members),
        )

    def _guest_plan(self, info: "cluster.ClusterInfo", sw: ClusterSwitches) -> GuestPlan:
        """Whether this cluster's guests are stopped cluster-wide first, and if not, why.

        Pure — no I/O, built from one inspection — and the one place the question is
        answered, so the preparation, its dry-run, the preview and the self-test cannot
        disagree about it.
        """
        ap = self.cfg.appliance
        want_ceph = sw.ceph and not info.ceph_unavailable
        want_disarm = sw.disarm and not info.disarm_unavailable
        # Ceph asks for it only where there is Ceph; the guest-stop tick asks regardless.
        requested = want_ceph or sw.guest_stop
        self_guest, source = cluster.find_self_guest(
            info.guests, ap.self_vmid, ap.self_node, _hostname()
        )
        # Stopping guests while the HA manager is live and holding resources only feeds
        # it work. With zero HA resources there is nobody to restart them, so the disarm
        # stops being a precondition.
        needs_disarm = info.ha_resources
        if not requested:
            block = "no Ceph" if sw.ceph else ""
        elif info.guests_unreadable:
            block = "guest list unreadable"
        elif needs_disarm and not want_disarm:
            block = "needs HA disarm (PVE 9.2+)"
        elif self_guest is None and not ap.self_external:
            block = f"own guest not identified ({source})"
        else:
            block = ""
        return GuestPlan(
            asked=sw.guests, requested=requested, block=block, self_guest=self_guest,
            source=source, needs_disarm=needs_disarm,
        )

    def _unit_clause(
        self, host: PveHostConfig, members: list[PveHostConfig], due: list[PveHostConfig]
    ) -> str:
        """The "n of m nodes" half-sentence for the preparation events."""
        if len(members) <= 1:
            return ""
        want_all = self._cluster_switches(members).unit
        part = f" {len(due)} of {len(members)} nodes triggered"
        if len(due) == len(members):
            return part + "."
        return part + (
            "; the rest are shut down with them."
            if want_all
            else "; the rest keep running ('shut the whole cluster down' is off)."
        )

    def _unit_additions(
        self, host: PveHostConfig, name: str, members: list[PveHostConfig],
        eligible: list[tuple[HostConfig, str]], extra: list[tuple[HostConfig, str]],
        reason: str,
    ) -> list[tuple[HostConfig, str]]:
        """Nodes of this cluster that go down with it although nothing triggered for them.

        The preparation is cluster-wide by nature — it disarms HA and, with Ceph or the
        guest stop, stops every guest in the cluster. Shutting down only the nodes whose own UPS triggered
        leaves the others without guests, without HA and (once the monitors follow)
        without storage. This is what keeps the two halves in step.
        """
        if not self._cluster_switches(members).unit:
            return []
        taken = {h.key for h, _ in eligible} | {h.key for h, _ in extra}
        out = []
        for member in members:
            if member.key in taken or self.host_fired.get(member.key):
                continue
            # Kept, not just noted: this is the reason _evaluate_hosts hands back to
            # _fire_host when a first attempt failed. Written once and used in both
            # places, so a retry cannot end up in the event log worded differently from
            # the attempt it repeats.
            unit_reason = f"cluster {name} is shut down as a unit ({reason})"
            self.cluster_unit_hosts[member.key] = unit_reason
            # Record the membership we just established. On a cold start these hosts were
            # matched through the API's node list, not through host_states — and the
            # abort-on-failure filter below keys on exactly that entry, so without this a
            # held-back cluster would still shut its taken-along nodes down.
            self.host_states.setdefault(member.key, {})["cluster_name"] = name
            out.append((member, unit_reason))
        return out

    # -- cluster awareness (read-only part) ----------------------------------
    def cluster_hosts(self) -> list[PveHostConfig]:
        """Enabled PVE hosts with cluster preparation switched on."""
        return [
            h
            for h in self.cfg.hosts
            if isinstance(h, PveHostConfig) and h.enabled and h.cluster
        ]

    async def _inspect_clusters(self) -> dict[str, cluster.ClusterInfo]:
        """Inspect each configured cluster once, via its first reachable member.

        Grouping is by the cluster NAME the API reports, not by anything configured:
        two separate clusters stay two groups, and a host that turns out to be
        standalone drops out on its own. Members are probed concurrently — five
        unreachable ones would otherwise stall the poll loop for 5 x 10 s.
        """
        hosts = self.cluster_hosts()
        if not hosts:
            return {}
        ap = self.cfg.appliance
        own_hostname = _hostname()
        infos = await asyncio.gather(
            *(
                cluster.inspect(
                    h, self_vmid=ap.self_vmid, self_node=ap.self_node,
                    hostname=own_hostname,
                )
                for h in hosts
            )
        )

        found: dict[str, cluster.ClusterInfo] = {}
        for host, info in zip(hosts, infos):
            self.host_states.setdefault(host.key, {}).update(
                {
                    "cluster_name": info.name,
                    "cluster_is_cluster": info.is_cluster,
                    "cluster_error": info.error,
                }
            )
            if not info.reachable or not info.is_cluster:
                continue
            # First reachable member of a cluster wins; the rest are the same cluster.
            found.setdefault(info.name or host.name, info)
        self.cluster_guest_state = {
            name: {
                "total": len(info.guests),
                "running": len(info.running_guests),
                "readable": info.guests_read,
                "self_vmid": info.self_guest.vmid if info.self_guest else None,
                "self_label": info.self_guest.label if info.self_guest else "",
                "self_node": info.self_guest.node if info.self_guest else "",
                "self_source": info.self_guest_source,
                "self_on_ceph": info.self_guest_on_ceph,
                "mon_nodes": list(info.mon_nodes),
            }
            for name, info in found.items()
        }
        return found

    async def restore_clusters(self) -> tuple[bool, list[dict]]:
        """Undo the shutdown preparation on every cluster that still carries it.

        Driven by the dashboard button, never by a timer. Each cluster is restored
        through its first reachable member, and the state is re-read afterwards so the
        dashboard and the health warnings agree immediately instead of at the next
        self-test.

        Returns (allowed, results). Refused during an outage: the button becomes visible
        the moment the preparation lands, which is mid-shutdown — and arming HA while the
        nodes are powering off would undo the preparation at the one moment it is doing
        its job. The confirmation dialog alone is too soft a guard for that.
        """
        if self._outage_in_progress():
            return False, []

        infos = await self._inspect_clusters()
        results: list[dict] = []
        for name, info in infos.items():
            if not info.needs_recovery:
                continue
            host = next(
                (
                    h
                    for h in self.cluster_hosts()
                    if self.host_states.get(h.key, {}).get("cluster_name") == name
                ),
                None,
            )
            if host is None:
                continue
            # The same budget the preparation gets, and never less than the module
            # default: arming HA back is exactly as slow as disarming it, and this runs
            # with mains back, so there is no battery to spend.
            result = await cluster.restore(
                host,
                timeout=max(60.0, float(self.cfg.thresholds.cluster_prep_timeout_s)),
            )
            await self._emit(
                f"Cluster {name}: restore {'done' if result.ok else 'FAILED'}",
                result.message,
                db.WARNING if result.ok else db.CRITICAL,
            )
            results.append({"cluster": name, "ok": result.ok, "message": result.message})
            self.cluster_prepared.pop(name, None)
            self.cluster_prep_failed.pop(name, None)
        if results:
            # Re-read so /api/status stops showing the warning the moment it is fixed.
            self.cluster_states = await self._inspect_clusters()
        return True, results

    async def _check_clusters(self, log_ok: bool = False) -> None:
        """Health checks per cluster, run as part of the scheduled self-test.

        Everything here is a WARNING at most: these are problems for the *next* outage,
        not reasons to call a running appliance broken. They go through _emit()/
        _log_quiet() so they land on the dashboard, /api/status and the webhooks without
        needing a channel of their own.

        ``log_ok`` writes a quiet "checked, all good" line per healthy cluster. Warnings
        alone would leave "checked and fine" indistinguishable from "never checked", so
        the self-test says what it found — throttled by the caller to the same daily
        cadence as the per-host "ok" lines.
        """
        try:
            infos = await self._inspect_clusters()
        except Exception as exc:  # noqa: BLE001 - a health check must never break the loop
            log.warning("Cluster inspection failed: %s", exc)
            return
        self.cluster_states = {name: info for name, info in infos.items()}

        for name, info in infos.items():
            warned = 0
            # Through the same membership rule and the same switch reading the
            # preparation uses, so "the self-test says Ceph flags will be set" and "the
            # preparation sets Ceph flags" cannot disagree. This used to match on the
            # host_states stamp alone, which the preparation's cold path fills from the
            # API's node list instead.
            sw = self._cluster_switches(self._cluster_members(info, name))
            wants_ceph, wants_disarm = sw.ceph, sw.disarm
            # Requested, not merely asked: a Ceph tick on a cluster without Ceph stops no
            # guest, so it must not demand the guest privileges either.
            wants_guests = self._guest_plan(info, sw).requested

            missing = cluster.missing_privileges(
                info, wants_ceph, wants_disarm, want_guests=wants_guests
            )
            if missing:
                warned += 1
                await self._emit(
                    f"Cluster {name}: token privileges incomplete",
                    f"Missing: {', '.join(missing)}. The cluster preparation before a "
                    f"shutdown will not work until they are granted. Sys.* are read on "
                    f"'/', the VM ones also count on '/vms'.",
                    db.WARNING,
                )

            # How the nodes are fed decides whether a partial outage is possible at
            # all — and that question stands whether or not Ceph is involved.
            warned += await self._check_cluster_feeds(name, info, wants_guests)

            # And the mirror image of that question: a node the preparation reaches but
            # the shutdown never takes along.
            warned += await self._check_cluster_optout(name, info, wants_guests, wants_ceph)

            # --- the appliance's own guest ------------------------------------
            # Everything below only matters once the guest stop is in play — with Ceph,
            # or switched on by itself. Otherwise none of it applies and none of it is said.
            if wants_guests:
                warned += await self._check_self_guest(name, info, wants_ceph)

            # Asked for a feature this cluster does not have. Only a warning — the
            # shutdown itself is unaffected (the step is skipped, see _prepare_clusters)
            # — but the tick is dead weight that also asks for a privilege nobody needs.
            if wants_ceph and info.ceph_unavailable:
                warned += 1
                await self._emit(
                    f"Cluster {name}: Ceph flags enabled, but no Ceph found",
                    "'Set Ceph maintenance flags' is ticked, yet this cluster does not "
                    "run Ceph. The step is skipped before every shutdown. Untick it — "
                    f"then {cluster.PRIV_MODIFY} is not needed on '/' either.",
                    db.WARNING,
                )

            if not info.quorate:
                warned += 1
                await self._emit(
                    f"Cluster {name}: no quorum",
                    "The cluster has no quorum. Cluster-wide preparation before a "
                    "shutdown cannot be carried out in this state.",
                    db.WARNING,
                )

            # Counted over every enabled PVE target, NOT only the cluster-ticked ones:
            # the tick governs the preparation (which runs once per cluster anyway), while
            # this warning is about nodes that nobody will shut down. Counting the ticks
            # instead accused a perfectly complete setup of leaving nodes running.
            pve = [h for h in self.cfg.hosts if isinstance(h, PveHostConfig)]
            cov = cluster.node_coverage(
                info.nodes,
                [h.name for h in pve if h.enabled],
                [h.name for h in pve if not h.enabled],
            )
            if info.nodes and cov.missing:
                warned += 1
                await self._emit(
                    f"Cluster {name}: not every node is a configured target",
                    cluster.coverage_report(info.nodes, cov),
                    db.WARNING,
                )

            # Left over from an earlier outage — these two are why the check exists.
            if info.ceph_flags_set:
                warned += 1
                await self._emit(
                    f"Cluster {name}: Ceph maintenance flags still set",
                    f"{', '.join(info.ceph_flags_set)} are still active, so Ceph is not "
                    f"rebalancing or recovering. Use 'Restore cluster' once the cluster "
                    f"is fully back.",
                    db.WARNING,
                )
            # The arm state alone: a disarmed stack means no fencing cluster-wide, even
            # with zero HA guests — and it stays disarmed until someone re-arms it.
            if info.ha_disarmed:
                warned += 1
                await self._emit(
                    f"Cluster {name}: HA is still disarmed",
                    "The HA manager is disarmed, so a real node failure would not be "
                    "handled. Use 'Restore cluster' to arm it again.",
                    db.WARNING,
                )

            # Either an armed state is reported or HA manages guests — otherwise there is
            # nothing to disarm and the version limitation is not worth a word.
            if (
                wants_disarm
                and (info.ha_resources or info.ha_present)
                and info.disarm_unavailable
            ):
                # Once per appliance run: repeating a version limitation every self-test
                # would train the operator to ignore the feed.
                # Counted either way: the cluster is not "all good" just because the
                # limitation was already reported on an earlier run.
                warned += 1
                if name not in self._disarm_unsupported_warned:
                    self._disarm_unsupported_warned.add(name)
                    await self._emit(
                        f"Cluster {name}: HA disarm not available",
                        "Arming/disarming HA needs Proxmox VE 9.2 or newer. Ceph flags "
                        "are still set as configured. As an alternative, set the "
                        "datacenter option shutdown_policy=freeze, which keeps HA "
                        "services from being recovered onto nodes that are shutting down.",
                        db.WARNING,
                    )

            # Independent of the version: whenever HA will NOT be disarmed, the
            # datacenter's shutdown policy decides what happens to the services.
            will_disarm = wants_disarm and not info.disarm_unavailable
            if info.ha_resources and not will_disarm and info.shutdown_policy in (
                "conditional", "migrate", "failover", ""
            ):
                policy = info.shutdown_policy or "conditional (default)"
                extra = (
                    " With 'migrate' the LRM even delays the shutdown until everything "
                    "has been migrated away — the opposite of what a draining battery "
                    "needs."
                    if info.shutdown_policy == "migrate"
                    else ""
                )
                warned += 1
                await self._emit(
                    f"Cluster {name}: shutdown_policy is '{policy}'",
                    f"HA will not be disarmed, so on power-off the HA services are "
                    f"recovered onto nodes that are shutting down themselves."
                    f"{extra} Recommended: shutdown_policy=freeze.",
                    db.WARNING,
                )

            # Nothing to complain about: say so, or "checked and fine" is indistinguishable
            # from "never checked" in the event feed. Throttled by the caller (log_ok) to
            # the same daily cadence as the per-host "ok" lines.
            if not warned and log_ok:
                self._log_quiet(
                    f"Cluster {name}: ok",
                    f"{len(info.nodes)} nodes ({info.nodes_online} online), quorum ok, "
                    f"Ceph {'flags clean' if info.ceph_configured else 'not configured'}, "
                    f"HA {info.ha_armed_state or 'stack not reporting a state'} "
                    f"({info.ha_services} HA guests)"
                    f"{'' if info.disarm_supported else ', disarm-ha not available'}.",
                    db.INFO,
                )

    async def _check_cluster_feeds(
        self, name: str, info: "cluster.ClusterInfo", wants_guests: bool
    ) -> int:
        """Warn when the nodes of one cluster can be triggered independently.

        Pure configuration, no I/O: two nodes fed by different UPS devices mean one of
        them can become due while the other does not. What follows from that depends on
        the "as a unit" switch, and both outcomes are worth knowing in advance — which is
        why this is said at self-test time rather than discovered during an outage.
        """
        members = self._cluster_members(info, name)
        if len(members) < 2:
            return 0
        # Uneven switches first, because that question does not depend on the feeds and
        # the answer to it changes what the paragraph below even means. All three are
        # cluster-wide in effect while being edited per host card, so the appliance reads
        # each of them as "any member asked for it" (see _cluster_switches) — which is the
        # safe reading, not the obvious one, and therefore worth saying out loud.
        warned = await self._check_cluster_switch_agreement(name, members)

        feeds = {h.key: frozenset(self.cfg.feed_ids_for(h)) for h in members}
        if len(set(feeds.values())) < 2:
            return warned

        unit = [h.cluster_shutdown_all for h in members]
        if all(unit):
            body = (
                "One UPS device failing is therefore enough to shut down the whole "
                "cluster, including the nodes that still have power - that is what "
                "'shut the whole cluster down as a unit' does, and on a hyper-converged "
                "cluster it is the right answer."
            )
        elif not any(unit):
            body = (
                "A single UPS device failing therefore shuts down only the nodes it "
                "feeds, while the preparation has already disarmed HA"
                + (" and stopped every guest in the cluster" if wants_guests else "")
                + " for all of them. Switch 'shut the whole cluster down as a unit' on, "
                "or feed every node from UPS devices that fail together."
            )
        else:
            # The disagreement itself is reported by _check_cluster_switch_agreement
            # above; here it only decides how to describe the feeds.
            body = (
                "'Shut the whole cluster down as a unit' is also set on some nodes of "
                "this cluster and not on others. The appliance goes with 'as a unit' "
                "because any node asked for it, but set it the same way everywhere."
            )
        await self._emit(
            f"Cluster {name}: its nodes are fed by different UPS devices",
            ", ".join(
                f"{h.name}: " + (", ".join(
                    (self.cfg.ups_by_id(u).label if self.cfg.ups_by_id(u) else u)
                    for u in sorted(feeds[h.key])
                ) or "all UPS devices")
                for h in members
            )
            + ". " + body
            + " Note also that a UPS which goes unreachable instead of reporting battery "
            "(a management switch on the failing UPS) never triggers at all - see "
            "'Shutdown on pure communication loss after (min)'.",
            db.WARNING,
        )
        return warned + 1

    async def _check_cluster_switch_agreement(
        self, name: str, members: list[PveHostConfig]
    ) -> int:
        """Warn when one cluster's nodes disagree about the cluster switches.

        They are edited per host card but every one of them acts on the whole cluster, so
        "ticked on pve01, not on pve02" is not two settings but one contradiction. The
        appliance resolves it with ``any`` (see _cluster_switches) — deliberately, because
        preparing a cluster nobody asked to prepare costs a maintenance flag while
        skipping one that was asked for costs the storage — but resolving it silently
        would leave an operator reading their own configuration wrongly.

        The guest stop is compared by its effective value — ticked, or included by the
        Ceph switch — so a node with Ceph and another with only the guest stop agree.
        """
        if len(members) < 2:
            return 0
        warned = 0
        for label, is_on, effect in (
            ("Set Ceph maintenance flags", lambda h: h.cluster_ceph,
             "the flags are set and every guest in the cluster is stopped first"),
            ("Disarm HA", lambda h: h.cluster_ha_disarm,
             "the HA manager is disarmed for the whole cluster"),
            ("Shut the whole cluster down as a unit", lambda h: h.cluster_shutdown_all,
             "every node of the cluster is shut down as soon as one is due"),
            ("Stop all guests cluster-wide first",
             lambda h: h.cluster_ceph or h.cluster_guest_stop,
             "every guest in the cluster is stopped before the first node goes down"),
        ):
            on = [h.name for h in members if is_on(h)]
            off = [h.name for h in members if not is_on(h)]
            if not on or not off:
                continue
            warned += 1
            await self._emit(
                f"Cluster {name}: '{label}' is not set the same on every node",
                f"On: {', '.join(on)}. Off: {', '.join(off)}. This switch acts on the "
                f"whole cluster, so it cannot be half on: {effect}, because at least one "
                f"node asked for it. Set it the same way on every node of this cluster so "
                f"the configuration says what happens.",
                db.WARNING,
            )
        return warned

    async def _check_cluster_optout(
        self, name: str, info: "cluster.ClusterInfo", wants_guests: bool, wants_ceph: bool
    ) -> int:
        """Warn about a node the preparation acts on but the shutdown leaves behind.

        The counterpart of the membership rule _cluster_members() enforces, and it has to
        exist because that rule is right: a node whose "cluster member" box is deliberately
        unticked must not be powered off by another node's outage. What the tick does NOT
        opt it out of is the preparation, which is cluster-wide by nature — the HA manager
        is disarmed for the whole cluster, and with the guest stop every guest in it is
        stopped, including the ones running on that node.

        So it is left standing without HA, without its guests and, on a hyper-converged
        cluster, without storage once the monitors follow — and nothing said so. Every
        neighbouring check looks past it by construction: _unit_additions() and the
        "only part of the cluster is shut down" event count over ``members`` (ticked
        only), _check_cluster_switch_agreement() compares within ``members``, and
        cluster.node_coverage() sees a perfectly well configured, enabled target and
        counts it as covered.

        A warning, never an action: which of the two ticks is the wrong one is the
        operator's call, and both answers are legitimate.
        """
        members = self._cluster_members(info, name)
        if not members:
            return 0
        nodes = {n for n in info.nodes if n}
        member_names = {m.name for m in members}
        outsiders = [
            h.name
            for h in self.cfg.hosts
            if isinstance(h, PveHostConfig)
            and h.enabled
            and not h.cluster
            and h.name in nodes
            and h.name not in member_names
        ]
        if not outsiders:
            return 0
        await self._emit(
            # Deliberately not worded "not every node ...": the coverage warning
            # above already opens that way, and two events whose subjects differ only
            # after the sixth word are two events nobody tells apart in a feed.
            f"Cluster {name}: some nodes are left out of the cluster handling",
            f"{', '.join(outsiders)} "
            + ("is" if len(outsiders) == 1 else "are")
            + f" configured and active here, and the API reports "
            + ("it" if len(outsiders) == 1 else "them")
            + f" as a member of cluster {name}, but 'this host belongs to a cluster' is "
            f"not ticked on "
            + ("its card" if len(outsiders) == 1 else "their cards")
            + ". The preparation another node triggers still acts on the whole cluster: "
            "HA is disarmed cluster-wide"
            + (", and every guest in the cluster is stopped — including the ones running "
               "there" if wants_guests else "")
            + ". The shutdown, however, stops at the ticked nodes, so "
            + ("that machine keeps" if len(outsiders) == 1 else "those machines keep")
            + " running without HA"
            + (", without guests" if wants_guests else "")
            + (" and with the Ceph maintenance flags set" if wants_ceph else "")
            + ". Tick the option there as well, or untick it on the other nodes of this "
            "cluster.",
            db.WARNING,
        )
        return 1

    async def _check_self_guest(
        self, name: str, info: "cluster.ClusterInfo", wants_ceph: bool = True
    ) -> int:
        """Warnings around the cluster-wide guest stop. Returns how many were emitted.

        Split out of _check_clusters because it is a self-contained question — "can this
        appliance stop the other guests without stopping itself, and will it survive
        doing so" — and because all of it is pointless without the guest stop. The two
        Ceph-only hints at the end (storage check, MON order) need the Ceph switch too.
        """
        ap = self.cfg.appliance
        warned = 0

        if info.guests_unreadable:
            warned += 1
            await self._emit(
                f"Cluster {name}: the guest list cannot be read",
                "Stopping the guests before the nodes needs VM.Audit and VM.PowerMgmt "
                "(on '/' or '/vms'). Note that /cluster/resources filters by privilege "
                "instead of refusing, so an empty answer is never read as 'no guests'.",
                db.WARNING,
            )
            return warned

        guest, source = cluster.find_self_guest(
            info.guests, ap.self_vmid, ap.self_node, _hostname()
        )
        if guest is None and not ap.self_external:
            warned += 1
            reason = {
                "missing": (
                    f"The selected guest {ap.self_vmid} does not exist in this cluster "
                    f"(renumbered, or it belongs to a different one)."
                ),
                "ambiguous": (
                    f"Several guests are named '{_hostname()}', so the hostname cannot "
                    f"identify this one."
                ),
            }.get(source, "No guest has been selected and none carries this hostname.")
            await self._emit(
                f"Cluster {name}: this appliance's own guest is unknown",
                f"{reason} Until it is selected under Settings -> Appliance the "
                f"cluster-wide guest shutdown is skipped, because stopping every guest "
                f"would stop this appliance in the middle of the outage. Tick 'not a "
                f"guest of this cluster' if it really runs elsewhere.",
                db.WARNING,
            )
        elif guest is not None:
            # Storage first: this is the deployment mistake that only shows up when it is
            # far too late to fix, and the appliance is the one guest that has to outlive
            # the cluster it is shutting down.
            if info.self_guest_on_ceph:
                warned += 1
                await self._emit(
                    f"Cluster {name}: this appliance runs on Ceph storage",
                    f"{guest.label} uses "
                    f"{', '.join(s for s in info.self_guest_storages if s in info.ceph_storages)}"
                    f", which is Ceph-backed. Once the OSDs drop below min_size its own "
                    f"IO blocks, so it can no longer shut anything down - the appliance "
                    f"would freeze halfway through the outage. Move it to local storage "
                    f"(local-lvm/local-zfs).",
                    db.WARNING,
                )
            # A guest that resolved but sits on a node nobody shuts down last is the same
            # mistake as an unset this_host, only harder to see.
            owner = next(
                (h for h in self.cfg.hosts if h.enabled and h.name == guest.node), None
            )
            if owner is not None and not owner.this_host:
                warned += 1
                await self._emit(
                    f"Cluster {name}: this appliance's node is not marked 'this host'",
                    f"{guest.label} runs on {guest.node}, but that host is not marked as "
                    f"the one carrying this appliance, so it is not forced to shut down "
                    f"last. Selecting the guest under Settings -> Appliance sets this "
                    f"automatically.",
                    db.WARNING,
                )

        advisory = cluster.advisory_privileges(info) if wants_ceph else []
        if advisory:
            self._log_quiet(
                f"Cluster {name}: storage check unavailable",
                f"Without {', '.join(advisory)} it cannot be checked whether this "
                f"appliance's own guest lives on Ceph. Everything else works.",
                db.INFO,
            )

        if not wants_ceph:
            return warned

        # MON ordering: advice, never enforcement (see cluster.mon_order_report).
        order = [
            h.name
            for h in self.cfg.ordered_hosts()
            if self.host_states.get(h.key, {}).get("cluster_name") == name
        ]
        this_node = next(
            (h.name for h in self.cfg.hosts if h.enabled and h.this_host), ""
        )
        report = cluster.mon_order_report(order, info.mon_nodes, this_node)
        if report:
            warned += 1
            await self._emit(f"Cluster {name}: MON nodes are not shut down last",
                             report, db.WARNING)

        return warned

    def _log_quiet(self, subject: str, body: str, severity: str) -> None:
        """Write an event without firing notifications (for routine successes)."""
        log.info("%s — %s", subject, body)
        try:
            db.log_event(subject, body, severity)
        except Exception as exc:  # noqa: BLE001
            log.warning("Event log write failed: %s", exc)

    # -- shutdown execution --------------------------------------------------
    async def _fire_host(self, host: HostConfig, reason: str) -> None:
        """Shut down a single host (or latch/log it in dry-run).

        Runs concurrently with its stage peers (see _evaluate_hosts), so it must not
        assume it is alone: state is merged into host_states, never replaced.
        """
        if not self.shutdown_triggered:
            self.shutdown_triggered = True
            self.triggered_at = _now()
        self.shutdown_reason = f"{host.name}: {reason}"

        if self.cfg.dry_run:
            self.host_fired[host.key] = True
            await self._emit(
                "DRY-RUN: shutdown would be triggered",
                f"Host {host.name} — reason: {reason}. NOTHING will be shut down.",
                db.CRITICAL,
            )
            return

        ok, msg = await targets.shutdown(
            host,
            timeout=self.cfg.thresholds.host_shutdown_timeout_s,
            # Only where this entry owns its API URL is "the node behind it" unambiguous.
            # Sharing one URL across entries makes PVE's proxying the only thing telling
            # them apart, so there the configured name has to stay in the path.
            use_localhost=self.cfg.api_url_is_unique(host),
        )
        # Latch on success; on failure only once the attempts are used up. The latch used
        # to be set before the call, so one transient failure took the machine out of the
        # episode for good.
        state = self.host_states.setdefault(host.key, {})
        attempts = int(state.get("shutdown_attempts") or 0) + 1
        done = ok or attempts >= MAX_SHUTDOWN_ATTEMPTS
        self.host_fired[host.key] = done
        # Merge, so the last self-test result stays visible next to the shutdown result.
        state.update(
            {
                "shutdown_state": "sent" if ok else "failed",
                "shutdown_attempts": attempts,
                "last_action_at": _now().isoformat(),
                "last_error": None if ok else msg,
                "reachable": ok,
                "this_host": host.this_host,
                "order": host.order,
            }
        )
        if ok:
            subject = f"Host {host.name}: shutdown sent"
            body = f"Reason: {reason}. {msg}"
            severity = db.WARNING  # not routine either (see "shutdown aborted" above)
        elif not done:
            subject = f"Host {host.name}: shutdown failed — retrying"
            body = (
                f"Reason: {reason}. {msg} Attempt {attempts} of {MAX_SHUTDOWN_ATTEMPTS}; "
                f"the next poll tries again."
            )
            severity = db.WARNING
        else:
            subject = f"Host {host.name}: shutdown FAILED"
            body = (
                f"Reason: {reason}. {msg} Gave up after {attempts} attempts — this "
                f"machine is still running."
            )
            severity = db.CRITICAL
        await self._emit(subject, body, severity)

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
        await notify.notify(
            self.cfg.notifications, f"[PVE-UPS] {subject}", body, self.snapshot(), severity
        )

    # -- status snapshot for the REST API -----------------------------------
    def _ups_snapshot(self, u: UpsBase, rt: _UpsRuntime) -> dict:
        st = rt.state
        # "On battery" in the same sense the rest of the engine uses it: the device says so,
        # or we watched it go and have not seen it come back (which covers a blind outage).
        _on_battery = st.on_battery or rt.on_battery_since is not None
        return {
            "id": u.id,
            "name": u.label,
            "type": getattr(u, "type", "snmp"),
            # Which MIB the last poll actually read ("" until the first answer, and for
            # sources that have no MIB). Diagnostics only — "auto" is otherwise invisible.
            "mib": st.mib,
            "reachable": st.reachable,
            "manufacturer": st.manufacturer,
            "model": st.model,
            "last_poll": st.last_poll.isoformat() if st.last_poll else None,
            # From _on_battery, not from st.on_battery: the loop picks its cadence the
            # same way (see _loop), so reading only what the device says reported the
            # 30 s normal interval throughout a blind outage — the one situation in which
            # someone is actually watching this number — while polling ran at 8 s.
            "poll_interval_s": (
                self.cfg.thresholds.poll_interval_battery_s
                if _on_battery
                else self.cfg.thresholds.poll_interval_normal_s
            ),
            "power_source": st.power_source,
            "battery_status": st.battery_status,
            "runtime_remaining_min": st.runtime_remaining_min,
            "battery_charge_pct": st.battery_charge_pct,
            "load_pct": st.load_pct,
            # Both only while the UPS really is on battery. This used to be passed through
            # whatever the state, so a card holding on to the last transfer's counter read
            # as a multi-day outage on a UPS sitting happily on mains.
            "seconds_on_battery": (
                self._ups_elapsed_on_battery(rt) if _on_battery else None
            ),
            "elapsed_source": self._ups_elapsed_source(rt) if _on_battery else None,
            "triggered": rt.triggered,
            "trigger_reason": rt.trigger_reason,
            "countdown_remaining_s": self._ups_countdown_remaining_s(u, rt),
            "comm_loss_remaining_s": self._ups_comm_loss_remaining_s(u, rt),
            "alarm": rt.alarm_active,
            # What keeps this entry from being polled at all, or None. Told apart from
            # "error" on purpose: that one is why the last read failed, this one is why
            # there was never a read to fail. Without it the two are indistinguishable on
            # the dashboard — both render as a device that is simply not answering.
            "incomplete": self.cfg.incomplete_ups(u) or None,
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
            st = self.host_states.get(h.key, {})
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
                    # getattr default: a config that predates the type field still renders.
                    "id": h.key,
                    "type": getattr(h, "type", "pve"),
                    "this_host": h.this_host,
                    # Cluster membership as configured, plus the name discovered from the
                    # API (None until a cluster has been inspected). The UI shows it on the
                    # collapsed host card, the way "this host" shows its star.
                    "cluster": bool(getattr(h, "cluster", False)),
                    "cluster_name": st.get("cluster_name") or None,
                    "order": h.order,
                    "ups_ids": list(h.ups_ids),
                    "ups_policy": h.ups_policy,
                    "feeds": feeds,
                    # Ids this host names that no longer exist. Live, because the event
                    # is written once per config while this stays true until it is fixed.
                    "stale_ups_ids": self.cfg.stale_feed_ids(h),
                    # What this entry is missing to be usable at all, or None. Live for
                    # the same reason, and next to the chip above because it is the same
                    # class of fault: configured, rendered complete, and unable to shut
                    # anything down when it is asked to.
                    "incomplete": self.cfg.incomplete_host(h) or None,
                    "eligible": self._host_trigger_reason(h) is not None,
                    "pending_reason": self._host_trigger_reason(h),
                    "reachable": st.get("reachable"),
                    "shutdown_state": st.get("shutdown_state", "idle"),
                    "last_action_at": st.get("last_action_at"),
                    "last_error": st.get("last_error"),
                    # Last self-test outcome (None until the first run).
                    "credentials_ok": st.get("credentials_ok"),
                    "power_mgmt_ok": st.get("power_mgmt_ok"),
                    # Member of proxmox.NODE_STATES; None until anything checked. "ok"
                    # and "unverified" are both fine, the rest is a misnamed entry.
                    "node_state": st.get("node_state"),
                    "last_test_at": st.get("last_test_at"),
                    "last_test_error": st.get("last_test_error"),
                }
            )

        return {
            "appliance": {
                "version": __version__,
                "uptime_s": int((_now() - self.started_at).total_seconds()),
                "engine_state": self.state,
                "dry_run": self.cfg.dry_run,
                # Whether the History tab exists at all (#37).
                "history_enabled": self.cfg.history.enabled,
                "config_valid": self.cfg.configured,
                "alarm": self.alarm_active,
                "last_selftest_at": (
                    self.last_selftest_at.isoformat() if self.last_selftest_at else None
                ),
                "last_selftest_ok": self.last_selftest_ok,
                # Naive LOCAL time (no offset), unlike last_selftest_at above: it is a
                # position on the wall-clock grid, not a UTC instant.
                "next_selftest_at": (
                    (
                        self.last_selftest_slot
                        + timedelta(minutes=self.cfg.selftest_interval_min)
                    ).isoformat()
                    if self.cfg.selftest_enabled and self.last_selftest_slot
                    else None
                ),
                # Which guest this appliance runs in, as far as the last inspection could
                # tell. Everything is None until one has run — the UI must show "not
                # established", never a confident "not on Ceph".
                "self_guest": self._self_guest_snapshot(),
                "shutdown_budget_s": shutdown_budget_s(self.cfg),
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
            "clusters": self.cluster_snapshot(),
            # How the notification targets themselves are doing. The shutdown credentials
            # have had a self-test and a chip for releases; these had a journald line.
            "webhooks": self._webhook_snapshot(),
        }

    def _webhook_snapshot(self) -> list[dict]:
        """Per-webhook delivery state. Everything is None until a target has been tried."""
        seen = notify.delivery_state()
        return [
            {
                "id": h.id,
                "name": h.label,
                "enabled": h.enabled,
                "last_delivery_ok": seen.get(h.id, {}).get("ok"),
                "last_delivery_at": seen.get(h.id, {}).get("at"),
                "last_delivery_error": seen.get(h.id, {}).get("error"),
            }
            for h in self.cfg.notifications.webhooks
        ]

    def _self_guest_snapshot(self) -> dict:
        """The appliance's own guest as the last inspection saw it, across all clusters.

        Only one guest can be ours, so the first cluster that resolved one wins; the
        configured values are echoed either way so the settings page can show what it is
        looking for even before anything has been inspected.
        """
        ap = self.cfg.appliance
        out = {
            "vmid": ap.self_vmid,
            "node": ap.self_node,
            "external": ap.self_external,
            "label": "",
            "source": "none",
            "on_ceph": None,
            "hostname": _hostname(),
        }
        for state in self.cluster_guest_state.values():
            if state.get("self_vmid") is None and state.get("self_source") == "none":
                continue
            out.update(
                {
                    "vmid": state.get("self_vmid", ap.self_vmid),
                    "node": state.get("self_node") or ap.self_node,
                    "label": state.get("self_label", ""),
                    "source": state.get("self_source", "none"),
                    "on_ceph": state.get("self_on_ceph"),
                }
            )
            if state.get("self_vmid") is not None:
                break
        return out

    def cluster_snapshot(self) -> list[dict]:
        """Per-cluster monitoring information from the last inspection.

        Purely informational, like the per-host self-test results: it never influences
        the engine state or the health endpoint's HTTP code. Empty until the first
        self-test has run, or when no host has cluster preparation enabled.
        """
        out = []
        for name, info in self.cluster_states.items():
            plan = self._guest_plan(
                info, self._cluster_switches(self._cluster_members(info, name))
            )
            out.append(
                {
                    "name": name,
                    "quorate": info.quorate,
                    "nodes": list(info.nodes),
                    "nodes_online": info.nodes_online,
                    "ceph_configured": info.ceph_configured,
                    "ceph_flags_set": info.ceph_flags_set,
                    "ha_services": info.ha_services,
                    "ha_armed_state": info.ha_armed_state,
                    "disarm_supported": info.disarm_supported,
                    "shutdown_policy": info.shutdown_policy,
                    "needs_recovery": info.needs_recovery,
                    "prepared": bool(self.cluster_prepared.get(name)),
                    "guests_total": len(info.guests),
                    "guests_running": len(info.running_guests),
                    "guests_readable": info.guests_read,
                    "mon_nodes": list(info.mon_nodes),
                    "self_guest_vmid": info.self_guest.vmid if info.self_guest else None,
                    "self_guest_on_ceph": info.self_guest_on_ceph,
                    # The guest stop will run here (Ceph, or its own switch) — and if so,
                    # whether it is held up by an unknown own guest. The dashboard says
                    # that where the operator looks, not only in the event log.
                    "guest_stop": plan.requested,
                    "self_guest_missing": plan.block.startswith("own guest"),
                    "prep_steps": list(self.cluster_prep_steps.get(name, [])),
                }
            )
        return out

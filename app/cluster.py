"""Proxmox VE cluster awareness: Ceph maintenance flags and HA arm/disarm.

Shutting a whole cluster down needs three things the per-node shutdown cannot do:

* On a hyper-converged cluster every guest has to stop BEFORE the first node loses
  power. Letting each node stop its own guests as it goes down looks tidy but hangs:
  with ``size=3/min_size=2`` the pool falls below ``min_size`` once the second node's
  OSDs are gone, the guests still running on the survivor block on IO, their shutdown
  never completes and that node never powers off. The official procedure
  (``pveceph_shutdown``) therefore stops all Ceph clients first,
* Ceph must be told not to start healing while the OSDs disappear one by one
  (``noout,nobackfill,norecover,norebalance``), and
* the HA manager must be disarmed, or it recovers services onto nodes that are
  themselves being powered off. With ``resource-mode=ignore`` the guests leave HA
  tracking and ``pve-guests`` shuts them down in their configured order — that
  handover is exactly what ``PVE::HA::Config::vm_is_ha_managed`` implements by
  returning false while disarmed in that mode. ``freeze`` does NOT do this: the
  guests stay HA-managed, ``pve-guests`` skips them and the disarmed LRM no longer
  stops them, so they would be killed by the power-off. Hence ignore, hard-wired.

Every entry point here is *total*, exactly like app/targets.py: it never raises and
never runs longer than its timeout. This runs on the poll loop while the battery is
draining, so a cluster that accepts a connection and then goes quiet must not be able
to stall the shutdown of anything else.

Order matters and is not the order of the switches: HA disarm, then the guests, then
the flags. Disarming last would let the HA manager restart every guest as fast as it is
stopped; setting the flags first would leave them on if the guest stop then fails.

Availability differs per feature, so they are checked separately rather than gated on
a version number: the Ceph flags work on PVE 8.x, while ``disarm-ha`` arrived with
PVE 9.2. Detection is done on the endpoint index (works with backports too), never by
comparing ``GET /version``.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .config import PveHostConfig
from .proxmox import CONNECT_TIMEOUT_S, _auth_header

log = logging.getLogger("pve-usv.cluster")

# Same backstop as app/targets.py: httpx's own timeout should be what fires, this
# catches everything it does not bound.
DEADLINE_GRACE_S = 5.0

# The flags that stop Ceph from healing while the cluster powers down. Confirmed by the
# reporter of the feature request as the manual equivalent of `ceph osd set <flag>`.
CEPH_MAINTENANCE_FLAGS = ("noout", "nobackfill", "norecover", "norebalance")

# Privileges the cluster features need, on "/" (see the manual). Kept here so the
# self-test can name the missing one instead of reporting a bare 403.
PRIV_AUDIT = "Sys.Audit"  # read cluster/ceph/HA status
PRIV_MODIFY = "Sys.Modify"  # set/clear Ceph flags
PRIV_CONSOLE = "Sys.Console"  # arm/disarm HA — a heavy privilege, hence its own switch
# Guest handling. Accepted on "/" or on "/vms": a token may legitimately hold VM.PowerMgmt
# only on /vms, and demanding it on "/" would send the operator off to widen it needlessly.
PRIV_VM_AUDIT = "VM.Audit"  # list the cluster's guests
PRIV_VM_POWER = "VM.PowerMgmt"  # shut the guests down before the nodes go
# Advisory only (see advisory_privileges): without it the Ceph-backed-storage check for
# this appliance's own guest cannot run, which is a missing hint, not a broken setup.
PRIV_DS_AUDIT = "Datastore.Audit"

# How many guest calls may be in flight at once. Sequential is hopeless — a connect
# timeout of ten seconds times forty guests — and unbounded opens a socket per guest at
# the worst possible moment. Not a user setting: there is no way to choose it well.
_GUEST_CONCURRENCY = 8

# Storage plugin types that are Ceph. The appliance's own guest must not live on one of
# these: it is the one guest that has to outlive the cluster it is shutting down.
CEPH_PLUGINS = ("rbd", "cephfs")

# ``armed-state`` values of the HA fencing entry (GET /cluster/ha/status/current).
ARMED = "armed"
DISARMED = "disarmed"
DISARMING = "disarming"


# Closed set of per-endpoint probe outcomes, mirroring ups.PROBE_STATUSES. The UI labels
# each one via the i18n key "cprobe.st.<status>", so a new member here needs a matching
# key in both dictionaries (tests/test_i18n.py enforces that).
CLUSTER_PROBE_STATUSES = (
    "ok",       # answered and understood
    "denied",   # 403 — the token lacks the privilege
    "absent",   # answered, but the feature is not there (no Ceph, no disarm-ha, standalone)
    "error",    # anything else
    # A read that failed ("denied" or "error") for a feature none of the ticked options
    # uses. Assigned afterwards by mark_unneeded(), never by a read itself: whether a
    # privilege matters depends on the host's options, which inspect() does not see.
    "unneeded",
)

# Which option a read serves (ClusterProbeEntry.feature). Reads without one feed what every
# cluster member needs (Sys.Audit: membership, quorum, HA state) and always count.
FEATURE_CEPH = "ceph"
FEATURE_GUESTS = "guests"


@dataclass
class ClusterProbeEntry:
    """Outcome of reading one endpoint during the host test.

    Same idea as ups.ProbeEntry: a one-line summary is not enough when something is wrong
    on real hardware — the operator needs to see what each call actually answered.
    """

    name: str                     # the endpoint path, e.g. "/cluster/ha/status/current"
    status: str                   # member of CLUSTER_PROBE_STATUSES
    value: str = ""               # short human-readable summary of what was read
    error: Optional[str] = None
    feature: str = ""             # FEATURE_* this read serves; "" = always needed


@dataclass
class GuestInfo:
    """One VM or container as /cluster/resources reports it."""

    vmid: int
    node: str
    kind: str = ""            # "qemu" | "lxc"
    name: str = ""
    status: str = ""          # "running" | "stopped"
    template: bool = False
    hastate: str = ""

    @property
    def sid(self) -> str:
        """The id /cluster/resources uses, e.g. "lxc/950"."""
        return f"{self.kind}/{self.vmid}"

    @property
    def path(self) -> str:
        """API path prefix of this guest, e.g. "/nodes/pve01/lxc/950"."""
        return f"/nodes/{self.node}/{self.kind}/{self.vmid}"

    @property
    def label(self) -> str:
        """Short human form for the event log: "CT 950 'pve-usv' on pve01"."""
        kind = "CT" if self.kind == "lxc" else "VM"
        name = f" '{self.name}'" if self.name else ""
        return f"{kind} {self.vmid}{name} on {self.node}"


@dataclass
class ClusterInfo:
    """Everything one read-only inspection of a cluster member found.

    Deliberately tolerant: each field falls back to a harmless default when the
    corresponding call fails or the privilege is missing, and ``errors`` collects what
    could not be read. A partial answer is still useful — knowing the Ceph flags is
    worth having even when the HA endpoint is out of reach.
    """

    reachable: bool = False
    error: Optional[str] = None

    is_cluster: bool = False
    name: str = ""
    quorate: bool = True
    nodes: list[str] = field(default_factory=list)
    nodes_online: int = 0
    # The node that answered THIS api_url, as it names itself. The only node name here
    # that is not a guess, which is what makes it safe to offer as a suggestion.
    local_node: str = ""

    ceph_configured: bool = False
    ceph_flags: dict[str, bool] = field(default_factory=dict)
    # Why Ceph looks absent. A cluster without Ceph errors here just like one we may not
    # read, so this is what tells the two apart.
    ceph_error: Optional[str] = None

    # Number of guests HA actually manages. Deliberately NOT the same thing as the armed
    # state below: "HA manages resources" and "the HA stack is armed" are independent, and
    # conflating them hid a disarmed stack on a cluster with no HA guests.
    ha_services: int = 0
    ha_armed_state: str = ""
    disarm_supported: bool = False
    # Whether the HA endpoint index was actually read. Without it ``disarm_supported`` is
    # False for two very different reasons — "this release has no disarm-ha" and "we never
    # got an answer" — and only the first one may be treated as a settled absence.
    ha_index_read: bool = False
    shutdown_policy: str = ""

    # Which nodes carry a Ceph MON, from the monmap in /cluster/ceph/status. Used only to
    # advise on the shutdown order (MON nodes last) — never to reorder anything.
    mon_nodes: list[str] = field(default_factory=list)

    # Every guest of the cluster, and whether that list is trustworthy. The second half is
    # not pedantry: /cluster/resources is permission-FILTERED, so a token without VM.Audit
    # gets HTTP 200 and an empty list. Treating that as "no guests to stop" would report
    # success while forty of them keep writing to Ceph, which is the single most dangerous
    # failure this module can have.
    guests: list[GuestInfo] = field(default_factory=list)
    guests_read: bool = False
    guests_error: Optional[str] = None

    # Storage names backed by Ceph (plugintype rbd/cephfs), for the check below.
    ceph_storages: list[str] = field(default_factory=list)
    storages_read: bool = False

    # This appliance's own guest — the one that must NOT be stopped — and where it lives.
    self_guest: Optional["GuestInfo"] = None
    self_guest_source: str = "none"  # config | hostname | none | ambiguous | missing
    self_guest_storages: list[str] = field(default_factory=list)
    # None means "not established" (config unread, no Datastore.Audit); only True is a
    # reason to warn, and only False may unlock anything.
    self_guest_on_ceph: Optional[bool] = None

    can_audit: bool = False
    can_modify: bool = False
    can_console: bool = False
    can_vm_audit: bool = False
    can_vm_power: bool = False
    can_ds_audit: bool = False

    errors: list[str] = field(default_factory=list)
    # One entry per endpoint queried, for the test button's diagnostics panel.
    probe: list["ClusterProbeEntry"] = field(default_factory=list)

    @property
    def ceph_flags_set(self) -> list[str]:
        """Maintenance flags currently active (the ones a re-arm would clear)."""
        return [f for f in CEPH_MAINTENANCE_FLAGS if self.ceph_flags.get(f)]

    @property
    def ha_disarmed(self) -> bool:
        return self.ha_armed_state in (DISARMED, DISARMING)

    @property
    def ha_resources(self) -> bool:
        """HA currently manages at least one guest."""
        return self.ha_services > 0

    @property
    def ha_present(self) -> bool:
        """The HA stack reports an arm state at all (no fencing entry before PVE 9.2)."""
        return bool(self.ha_armed_state)

    @property
    def ceph_unavailable(self) -> bool:
        """Ceph is settled as *absent*, as opposed to merely unread.

        A denied read looks exactly like a missing Ceph on the wire, so the two are told
        apart by ``ceph_error``: only a non-403 failure means "there is no Ceph here". This
        is what keeps the Ceph flags from being attempted — and their privilege from being
        demanded — on a cluster that simply does not run Ceph.
        """
        if self.ceph_configured:
            return False
        return bool(self.ceph_error) and "not permitted" not in self.ceph_error

    @property
    def disarm_unavailable(self) -> bool:
        """``disarm-ha`` is settled as absent (PVE < 9.2), not just unread.

        Mirror image of ``ceph_unavailable``: concluded only from an index we actually got.
        """
        return self.ha_index_read and not self.disarm_supported

    @property
    def running_guests(self) -> list["GuestInfo"]:
        """Guests that are actually running (templates are not)."""
        return [g for g in self.guests if g.status == "running" and not g.template]

    @property
    def guests_unreadable(self) -> bool:
        """The guest list could not be established — as opposed to being empty.

        Same doctrine as ceph_unavailable: absent and unread are different answers, and
        here the difference decides whether a cluster-wide stop is attempted at all.
        """
        return not self.guests_read

    @property
    def hyperconverged(self) -> bool:
        """Ceph runs on the cluster's own nodes (there is a monmap naming them)."""
        return self.ceph_configured and bool(self.mon_nodes)

    @property
    def needs_recovery(self) -> bool:
        """True when something is left over from a previous shutdown preparation.

        The arm state alone decides, never the resource count: a disarmed stack means no
        fencing cluster-wide, whether or not a guest happens to be HA-managed right now.
        """
        return bool(self.ceph_flags_set) or self.ha_disarmed


def _client(host: PveHostConfig, timeout: float) -> httpx.AsyncClient:
    """Same shape as proxmox._client — cluster calls use the host's existing token."""
    return httpx.AsyncClient(
        base_url=host.api_url.rstrip("/") + "/api2/json",
        headers=_auth_header(host),
        verify=host.verify_tls,
        timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT_S)),
    )


async def _get(client: httpx.AsyncClient, path: str) -> tuple[Optional[object], Optional[str]]:
    """GET one endpoint, returning (data, error). Never raises.

    A 403 is reported as a missing privilege rather than a failure: the caller decides
    whether that feature was wanted at all.
    """
    try:
        resp = await client.get(path)
        if resp.status_code == 403:
            return None, f"{path}: not permitted (missing privilege)"
        if resp.status_code >= 400:
            return None, f"{path}: HTTP {resp.status_code}"
        return resp.json().get("data"), None
    except Exception as exc:  # noqa: BLE001 - a read must never break the caller
        return None, f"{path}: {exc}"


def _record(info: ClusterInfo, path: str, value: str = "", err: Optional[str] = None,
            absent: bool = False, feature: str = "") -> None:
    """Add one diagnostics entry, classifying the outcome into the closed status set."""
    if err:
        status = "denied" if "not permitted" in err else "error"
    else:
        status = "absent" if absent else "ok"
    info.probe.append(
        ClusterProbeEntry(name=path, status=status, value=value, error=err, feature=feature)
    )


def mark_unneeded(info: ClusterInfo, *, want_ceph: bool, want_guests: bool) -> None:
    """Downgrade failed reads that no ticked option depends on to "unneeded".

    The reads run regardless of the options, and a token scoped to exactly what the
    ticked options need is the recommended setup — without this, a correctly built token
    turned the diagnostics panel orange over a privilege nothing uses (#39). Only "denied"
    and "error" move: an answered read stays what it is.
    """
    wanted = {FEATURE_CEPH: want_ceph, FEATURE_GUESTS: want_guests}
    for entry in info.probe:
        if (
            entry.status in ("denied", "error")
            and entry.feature
            and not wanted.get(entry.feature, True)
        ):
            entry.status = "unneeded"


def _priv_on(data: dict, priv: str, *paths: str) -> bool:
    """Whether the token holds ``priv`` on any of ``paths``.

    The Sys.* privileges are only ever read on "/", but the guest and storage ones are
    routinely granted on "/vms" or "/storage" instead. Insisting on "/" for those would
    report a perfectly good token as broken.
    """
    for path in paths:
        entry = data.get(path)
        if isinstance(entry, dict) and entry.get(priv):
            return True
    return False


async def _read_permissions(client: httpx.AsyncClient, info: ClusterInfo) -> None:
    """Which of the cluster privileges the token actually holds."""
    path = "/access/permissions"
    data, err = await _get(client, path)
    if err or not isinstance(data, dict):
        if err:
            info.errors.append(err)
        _record(info, path, err=err or "unexpected answer")
        return
    root = data.get("/", {}) or {}
    info.can_audit = bool(root.get(PRIV_AUDIT))
    info.can_modify = bool(root.get(PRIV_MODIFY))
    info.can_console = bool(root.get(PRIV_CONSOLE))
    info.can_vm_audit = _priv_on(data, PRIV_VM_AUDIT, "/", "/vms")
    info.can_vm_power = _priv_on(data, PRIV_VM_POWER, "/", "/vms")
    info.can_ds_audit = _priv_on(data, PRIV_DS_AUDIT, "/", "/storage")
    held = [
        name
        for name, ok in (
            (PRIV_AUDIT, info.can_audit),
            (PRIV_MODIFY, info.can_modify),
            (PRIV_CONSOLE, info.can_console),
            (PRIV_VM_AUDIT, info.can_vm_audit),
            (PRIV_VM_POWER, info.can_vm_power),
            (PRIV_DS_AUDIT, info.can_ds_audit),
        )
        if ok
    ]
    _record(info, path, ", ".join(held) or "none of the cluster privileges")


async def _read_cluster_status(client: httpx.AsyncClient, info: ClusterInfo) -> None:
    """Cluster name, membership and quorum.

    A standalone node answers with a single fake entry (``nodeid=0``) and no
    ``type=cluster`` record — that absence is how standalone is detected.
    """
    path = "/cluster/status"
    data, err = await _get(client, path)
    if err or not isinstance(data, list):
        if err:
            info.errors.append(err)
        _record(info, path, err=err or "unexpected answer")
        return
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "cluster":
            info.is_cluster = True
            info.name = str(entry.get("name") or "")
            info.quorate = bool(entry.get("quorate", 1))
        elif entry.get("type") == "node":
            node = str(entry.get("name") or "")
            info.nodes.append(node)
            if entry.get("local"):
                info.local_node = node
            if entry.get("online"):
                info.nodes_online += 1
    if info.is_cluster:
        _record(
            info,
            path,
            f"cluster '{info.name}', {len(info.nodes)} nodes, "
            f"{info.nodes_online} online, "
            f"{'quorate' if info.quorate else 'NO QUORUM'}",
        )
    else:
        # A standalone node answers with a single fake entry and no "cluster" record.
        _record(info, path, "standalone node (no cluster record)", absent=True)


async def _read_ceph(client: httpx.AsyncClient, info: ClusterInfo) -> None:
    """Whether Ceph is configured, and the current state of the maintenance flags."""
    status, err = await _get(client, "/cluster/ceph/status")
    if err:
        # A cluster without Ceph fails here exactly like one we may not read, so this is
        # not reported as a problem on its own — but it IS recorded, because otherwise
        # "no Ceph" and "could not ask" look identical to the operator.
        _record(info, "/cluster/ceph/status", "not available", absent=("not permitted" not in err),
                err=err if "not permitted" in err else None, feature=FEATURE_CEPH)
    else:
        health = ""
        if isinstance(status, dict):
            h = status.get("health")
            health = str(h.get("status") if isinstance(h, dict) else h or "")
            # The monmap comes with the status we already asked for, so knowing which
            # nodes must go down last costs no extra request.
            monmap = status.get("monmap")
            if isinstance(monmap, dict):
                for mon in monmap.get("mons") or []:
                    if isinstance(mon, dict) and mon.get("name"):
                        info.mon_nodes.append(str(mon["name"]))
        _record(
            info,
            "/cluster/ceph/status",
            (health or "answered")
            + (f", MONs: {', '.join(info.mon_nodes)}" if info.mon_nodes else ""),
            feature=FEATURE_CEPH,
        )

    path = "/cluster/ceph/flags"
    data, err = await _get(client, path)
    if err or not isinstance(data, list):
        info.ceph_error = err
        # Only a denied read is actionable; anything else is almost always "no Ceph here".
        _record(info, path, "" if err and "not permitted" in err else "no Ceph configured",
                err=err if err and "not permitted" in err else None,
                absent=not (err and "not permitted" in err), feature=FEATURE_CEPH)
        return
    info.ceph_configured = True
    for entry in data:
        if isinstance(entry, dict) and entry.get("name"):
            info.ceph_flags[str(entry["name"])] = bool(entry.get("value"))
    _record(
        info,
        path,
        ", ".join(
            f"{f}={'ON' if info.ceph_flags.get(f) else 'off'}" for f in CEPH_MAINTENANCE_FLAGS
        ),
        feature=FEATURE_CEPH,
    )


async def _read_guests(client: httpx.AsyncClient, info: ClusterInfo) -> None:
    """Every VM and container of the cluster, in one call.

    ``guests_read`` is what the write path keys on, and it is set conservatively: this
    endpoint filters by privilege rather than refusing, so a token without VM.Audit gets
    HTTP 200 and an empty list. "Empty" is therefore only believed when the token
    demonstrably holds VM.Audit; otherwise the list stays unread and the guest stop is
    skipped with a reason instead of silently doing nothing.
    """
    path = "/cluster/resources?type=vm"
    data, err = await _get(client, path)
    if err or not isinstance(data, list):
        info.guests_error = err or "unexpected answer"
        info.errors.append(info.guests_error)
        _record(info, path, err=info.guests_error, feature=FEATURE_GUESTS)
        return
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("vmid"):
            continue
        info.guests.append(
            GuestInfo(
                vmid=int(entry["vmid"]),
                node=str(entry.get("node") or ""),
                kind=str(entry.get("type") or ""),
                name=str(entry.get("name") or ""),
                status=str(entry.get("status") or ""),
                template=bool(entry.get("template")),
                hastate=str(entry.get("hastate") or ""),
            )
        )
    if not info.guests and not info.can_vm_audit:
        # Worded as a refusal so it is classified "denied" like a 403 — which is what it
        # is, only delivered as a filtered 200.
        info.guests_error = "not permitted to list guests (VM.Audit missing)"
        _record(info, path, err=info.guests_error, feature=FEATURE_GUESTS)
        return
    info.guests_read = True
    running = info.running_guests
    _record(
        info,
        path,
        f"{len(info.guests)} guests, {len(running)} running "
        f"({sum(1 for g in running if g.kind == 'qemu')} VM, "
        f"{sum(1 for g in running if g.kind == 'lxc')} CT)",
        feature=FEATURE_GUESTS,
    )


async def _read_storages(client: httpx.AsyncClient, info: ClusterInfo) -> None:
    """Which storages are backed by Ceph, so the appliance's own guest can be checked."""
    path = "/cluster/resources?type=storage"
    data, err = await _get(client, path)
    if err or not isinstance(data, list):
        # Only feeds the "appliance on Ceph storage" check, hence the Ceph feature.
        _record(info, path, err=err or "unexpected answer", feature=FEATURE_CEPH)
        return
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("storage") or "")
        if name and name not in seen and str(entry.get("plugintype") or "") in CEPH_PLUGINS:
            seen.add(name)
            info.ceph_storages.append(name)
    info.storages_read = True
    _record(
        info, path,
        f"Ceph-backed: {', '.join(info.ceph_storages)}" if info.ceph_storages
        else "no Ceph-backed storage",
        feature=FEATURE_CEPH,
    )


async def _read_self_guest(
    client: httpx.AsyncClient, info: ClusterInfo, vmid: Optional[int], node: str,
    hostname: str,
) -> None:
    """Resolve this appliance's own guest and find out whether it sits on Ceph.

    Both halves matter during an outage: the guest must be spared by the cluster-wide
    stop, and it must not depend on the very storage it is about to freeze — once the
    OSDs drop below min_size its own IO blocks and it can no longer shut anything down.
    """
    info.self_guest, info.self_guest_source = find_self_guest(
        info.guests, vmid, node, hostname
    )
    guest = info.self_guest
    if guest is None:
        _record(
            info, "appliance guest",
            {
                "missing": f"configured guest {vmid} not found in this cluster",
                "ambiguous": f"several guests are named '{hostname}'",
            }.get(info.self_guest_source, "not selected"),
            absent=True,
            feature=FEATURE_GUESTS,
        )
        return
    path = f"{guest.path}/config"
    data, err = await _get(client, path)
    if err or not isinstance(data, dict):
        _record(info, path, err=err or "unexpected answer", feature=FEATURE_GUESTS)
        return
    info.self_guest_storages = storages_of_config(data)
    ceph = [s for s in info.self_guest_storages if s in info.ceph_storages]
    # Only concluded when the storage list itself was readable; otherwise "not on Ceph"
    # would be indistinguishable from "we could not look".
    if info.storages_read:
        info.self_guest_on_ceph = bool(ceph)
    _record(
        info, path,
        f"{guest.label}, storage: {', '.join(info.self_guest_storages) or 'none found'}"
        + (f" — ON CEPH: {', '.join(ceph)}" if ceph else ""),
        feature=FEATURE_GUESTS,
    )


async def _read_ha(client: httpx.AsyncClient, info: ClusterInfo) -> None:
    """HA resources, the fencing entry's ``armed-state`` and whether disarm-ha exists.

    Feature detection reads the endpoint INDEX rather than comparing versions: that also
    catches backports, and it costs no extra privilege (the index is ``user => 'all'``).
    """
    index, err = await _get(client, "/cluster/ha/status")
    if isinstance(index, list):
        info.ha_index_read = True
        info.disarm_supported = any(
            isinstance(e, dict) and e.get("name") == "disarm-ha" for e in index
        )
        _record(
            info,
            "/cluster/ha/status",
            "disarm-ha available" if info.disarm_supported
            else "disarm-ha absent (needs PVE 9.2)",
            absent=not info.disarm_supported,
        )
    else:
        if err:
            info.errors.append(err)
        _record(info, "/cluster/ha/status", err=err or "unexpected answer")

    path = "/cluster/ha/status/current"
    data, err = await _get(client, path)
    if err or not isinstance(data, list):
        if err:
            info.errors.append(err)
        _record(info, path, err=err or "unexpected answer")
        return
    for entry in data:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        if etype == "fencing":
            # The dash spelling is what the API returns; accept the underscore too so a
            # future rename does not silently read as "unknown".
            info.ha_armed_state = str(
                entry.get("armed-state") or entry.get("armed_state") or ""
            )
        elif etype == "service":
            # Counted, not flagged: the number says whether HA manages anything, while the
            # arm state above says whether the stack is live. They are independent.
            info.ha_services += 1
    _record(
        info,
        path,
        f"armed-state={info.ha_armed_state or 'not reported'}, "
        f"{info.ha_services} HA services",
    )


async def _read_shutdown_policy(client: httpx.AsyncClient, info: ClusterInfo) -> None:
    """datacenter.cfg's ``ha: shutdown_policy`` — the default is ``conditional``.

    It matters because with HA left armed, ``conditional`` recovers services onto nodes
    that are themselves shutting down, and ``migrate`` makes the LRM actively *delay* the
    shutdown while the battery drains.
    """
    path = "/cluster/options"
    data, err = await _get(client, path)
    if err or not isinstance(data, dict):
        _record(info, path, err=err or "unexpected answer")
        return
    ha = data.get("ha")
    if isinstance(ha, dict):
        info.shutdown_policy = str(ha.get("shutdown_policy") or "")
    elif isinstance(ha, str) and "shutdown_policy=" in ha:
        # datacenter.cfg property strings come back as "shutdown_policy=freeze".
        for part in ha.split(","):
            key, _, value = part.partition("=")
            if key.strip() == "shutdown_policy":
                info.shutdown_policy = value.strip()
    _record(
        info,
        path,
        f"shutdown_policy={info.shutdown_policy or 'conditional (default, not set)'}",
    )


async def _inspect(
    host: PveHostConfig, timeout: float, self_vmid: Optional[int], self_node: str,
    hostname: str,
) -> ClusterInfo:
    info = ClusterInfo()
    try:
        async with _client(host, timeout) as client:
            await _read_permissions(client, info)
            await _read_cluster_status(client, info)
            info.reachable = True
            if not info.is_cluster:
                return info  # standalone: nothing else applies
            await _read_ceph(client, info)
            await _read_guests(client, info)
            await _read_storages(client, info)
            await _read_self_guest(client, info, self_vmid, self_node, hostname)
            await _read_ha(client, info)
            await _read_shutdown_policy(client, info)
    except Exception as exc:  # noqa: BLE001
        info.reachable = False
        info.error = f"Connection error: {exc}"
    return info


# The default budget for one inspection. A module constant rather than a bare default
# argument because engine.shutdown_budget_s() has to add it up: on a cold start the
# preparation inspects the cluster from inside the shutdown path, and an operator sizing a
# battery reserve should not have to guess what that costs.
INSPECT_BUDGET_S = 12.0


async def inspect(
    host: PveHostConfig, timeout: float = INSPECT_BUDGET_S, *,
    self_vmid: Optional[int] = None, self_node: str = "", hostname: str = "",
) -> ClusterInfo:
    """Read-only inspection of the cluster this host belongs to.

    Never raises, never exceeds ``timeout`` by more than the grace — the same contract
    as targets.shutdown(), for the same reason. The default is a little above the ten
    seconds elsewhere because this is nine requests, not six.
    """
    try:
        return await asyncio.wait_for(
            _inspect(host, timeout, self_vmid, self_node, hostname),
            timeout=timeout + DEADLINE_GRACE_S,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return ClusterInfo(
            reachable=False,
            error=f"No response within {timeout + DEADLINE_GRACE_S:.0f}s — gave up",
        )


# --- writing operations ------------------------------------------------------
# How long to keep re-reading a state until it matches, and how often. Both write paths
# verify by GET rather than trusting the response: the bulk PUT on /cluster/ceph/flags is
# asynchronous (it only hands back a worker UPID) and disarming passes through an
# intermediate "disarming" state.
_VERIFY_INTERVAL_S = 1.0
# How long to wait for the Ceph flags to show up before falling back to the per-flag
# endpoint. Short on purpose: the fallback is cheap, the remaining budget is not. It is a
# CEILING on the Ceph share, never a fixed cost — see _ceph_budget().
_CEPH_VERIFY_BUDGET_S = 6.0
# Fraction of what is left that Ceph may spend on ONE verification when a disarm still has
# to happen. The disarm is the slow half (every LRM has to release its watchdog, in rounds
# of ten seconds), so it gets everything Ceph does not need.
_CEPH_SHARE = 1 / 3
# Left unspent so the answer comes from here — with its steps — instead of from the outer
# wait_for as a bare "gave up".
_MARGIN_S = 1.0
# How often the guest stop re-reads /cluster/resources. Far slower than the one-second
# cadence above on purpose: polling a cluster-wide listing every second for five minutes
# would be three hundred requests at the worst possible moment.
_GUEST_POLL_INTERVAL_S = 5.0
# Share of the guest budget spent waiting for a graceful stop before anything is killed.
_GUEST_GRACE_SHARE = 0.7
# Kept back from the guest phase so the Ceph step that runs AFTER it still has both of its
# verification passes. The guest stop is the elastic part of the sequence; the flags are
# not, and a flag step that runs out of budget reports a failed preparation for work it
# almost certainly completed — which, with "abort on failure", holds back a whole cluster.
_CEPH_RESERVE_S = _CEPH_VERIFY_BUDGET_S * 2 + _MARGIN_S


def _ceph_reserve(overall_deadline: float) -> float:
    """What to keep back from the guest phase for the Ceph step behind it.

    A share, never the flat ceiling: on a cluster configured with small budgets — five
    seconds each, which the tests use and nothing forbids — a fixed thirteen seconds would
    put the guest deadline in the past and force-stop every guest instantly. Same shape as
    _ceph_budget() for exactly the same reason.
    """
    return min(_CEPH_RESERVE_S, _remaining(overall_deadline) * _CEPH_SHARE)


def _remaining(deadline: float) -> float:
    """Seconds left of the caller's budget, never negative."""
    return max(0.0, deadline - asyncio.get_running_loop().time())


def _ceph_budget(deadline: float, want_disarm: bool) -> float:
    """One Ceph verification's share of what is left.

    Ceph keeps a small slice on purpose: giving up here is what triggers the fallback to
    the synchronous per-flag endpoint, so a budget that swallowed the whole deadline would
    never reach it — and would leave nothing for the disarm either.
    """
    left = _remaining(deadline)
    if want_disarm:
        left *= _CEPH_SHARE
    return min(_CEPH_VERIFY_BUDGET_S, left)


def _disarm_budget(deadline: float, want_ceph: bool) -> float:
    """What the disarm may wait, keeping the flags' slice back.

    The disarm now runs FIRST (it has to: otherwise the HA manager restarts the guests as
    fast as they are stopped), so it is the step that has to leave something behind. What
    it keeps back is exactly the slice _ceph_budget would have taken — a proportional
    share, not the flat ceiling: subtracting six seconds from a five-second budget would
    leave the disarm nothing at all.
    """
    left = _remaining(deadline) - _MARGIN_S
    if want_ceph:
        left -= _ceph_budget(deadline, want_disarm=True)
    return max(0.0, left)


@dataclass
class ActionResult:
    """Outcome of one write operation. Never an exception — the caller keeps going."""

    ok: bool
    message: str
    steps: list[str] = field(default_factory=list)


async def _put(client: httpx.AsyncClient, path: str, **kw) -> Optional[str]:
    """PUT one endpoint; returns an error string or None. Never raises."""
    try:
        resp = await client.put(path, **kw)
        if resp.status_code == 403:
            return f"{path}: not permitted (missing privilege)"
        if resp.status_code >= 400:
            return f"{path}: HTTP {resp.status_code}"
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{path}: {exc}"


async def _post(client: httpx.AsyncClient, path: str, **kw) -> Optional[str]:
    """POST one endpoint; returns an error string or None. Never raises."""
    try:
        resp = await client.post(path, **kw)
        if resp.status_code == 403:
            return f"{path}: not permitted (missing privilege)"
        if resp.status_code >= 400:
            return f"{path}: HTTP {resp.status_code}"
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{path}: {exc}"


async def _read_ceph_flags(client: httpx.AsyncClient) -> dict[str, bool]:
    data, _ = await _get(client, "/cluster/ceph/flags")
    if not isinstance(data, list):
        return {}
    return {
        str(e["name"]): bool(e.get("value"))
        for e in data
        if isinstance(e, dict) and e.get("name")
    }


async def _set_ceph_flags(
    client: httpx.AsyncClient, value: bool, steps: list[str], deadline: float
) -> bool:
    """Set or clear the maintenance flags and VERIFY the result by reading them back.

    Takes the caller's absolute ``deadline``, not a pre-computed budget: each of the two
    verification passes then measures its own share off what is actually left. Handing the
    same number to both — which is what happened while this took a ``budget_s`` — promised
    a "freshly measured share" in the comment and delivered twice the ceiling in the code.

    The bulk PUT is asynchronous — its answer is a worker UPID and says nothing about
    whether the flags took effect — so success is only ever concluded from a GET. If the
    bulk call does not get there, each flag is set individually; that per-flag endpoint is
    synchronous, and it is also the older of the two, which is what makes this the normal
    path on PVE releases that lack the bulk variant.
    """
    def reached(flags: dict[str, bool]) -> bool:
        return all(flags.get(f) == value for f in CEPH_MAINTENANCE_FLAGS)

    err = await _put(
        client, "/cluster/ceph/flags", data={f: int(value) for f in CEPH_MAINTENANCE_FLAGS}
    )
    if err:
        steps.append(f"bulk flag update rejected ({err}), falling back to single flags")
    elif await _verify(
        reached, lambda: _read_ceph_flags(client), _ceph_budget(deadline, False)
    ):
        steps.append(f"Ceph flags {'set' if value else 'cleared'}: "
                     f"{', '.join(CEPH_MAINTENANCE_FLAGS)}")
        return True

    # Fallback: one synchronous PUT per flag. This endpoint is the older of the two, so
    # on releases without the bulk variant it is simply the normal path.
    for flag in CEPH_MAINTENANCE_FLAGS:
        err = await _put(client, f"/cluster/ceph/flags/{flag}", data={"value": int(value)})
        if err:
            steps.append(f"{flag}: {err}")
    if await _verify(
        reached, lambda: _read_ceph_flags(client), _ceph_budget(deadline, False)
    ):
        steps.append(f"Ceph flags {'set' if value else 'cleared'} individually")
        return True

    current = await _read_ceph_flags(client)
    steps.append(
        "Ceph flags could not be verified; currently set: "
        + (", ".join(f for f in CEPH_MAINTENANCE_FLAGS if current.get(f)) or "none")
    )
    return False


async def _verify(matches, read, budget_s: float, interval_s: Optional[float] = None) -> bool:
    """Poll ``read()`` until ``matches()`` holds, for at most ``budget_s``.

    It needs its own budget, not just the caller's overall deadline: giving up here is
    what lets the Ceph path fall back to the synchronous per-flag endpoint. Waiting on
    the outer deadline instead would consume the whole budget and never reach it.

    ``interval_s`` is a parameter because the two callers watch very different things:
    a flag flips in about a second, a cluster full of guests takes minutes.
    """
    # Resolved here, not as a default argument: a default is bound at import time, which
    # would quietly pin the module constant and make it unpatchable.
    interval_s = _VERIFY_INTERVAL_S if interval_s is None else interval_s
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    while True:
        left = deadline - loop.time()
        try:
            # Bounded by what is left, not by the client's timeout. Without this the very
            # first read is unbounded and every later one too, so an endpoint that stops
            # answering spends CONNECT_TIMEOUT_S per attempt — a 2 s budget could cost 12 s
            # and eat the share the caller carved out for the step after this one.
            if matches(await asyncio.wait_for(read(), timeout=max(0.5, left))):
                return True
        except Exception as exc:  # noqa: BLE001
            log.debug("Verification read failed: %s", exc)
        left = deadline - loop.time()
        if left <= 0:
            return False
        await asyncio.sleep(min(interval_s, left))
        # Checked again *before* the next read, not only after it: an endpoint that stopped
        # answering blocks for a full CONNECT_TIMEOUT_S, which would spend the share the
        # caller carved out for the step after this one.
        if loop.time() >= deadline:
            return False


async def _read_running_vmids(
    client: httpx.AsyncClient, timeout: float = CONNECT_TIMEOUT_S
) -> Optional[set[int]]:
    """VMIDs currently reported as running, or None when the list could not be read.

    None rather than an empty set, and the distinction decides whether a shutdown is
    called successful: ``down()`` in _shutdown_guests concludes "every guest has stopped"
    from an empty answer, so an unreadable list used to be indistinguishable from a
    cluster that had finished stopping — the preparation then reported success on no
    evidence at all, in the one module whose whole doctrine is that a write is confirmed
    by a read or not at all.

    ``timeout`` is the caller's REMAINING budget, not the client's: three of these reads
    sit between the grace period and the return, and at CONNECT_TIMEOUT_S each they could
    overrun prepare()'s outer bound and cost the Ceph step behind them its reserve.
    """
    try:
        data, _ = await asyncio.wait_for(
            _get(client, "/cluster/resources?type=vm"), timeout=max(0.5, timeout)
        )
    except Exception:  # noqa: BLE001 - a read that did not happen is "unknown", not "none"
        return None
    if not isinstance(data, list):
        return None
    return {
        int(e["vmid"])
        for e in data
        if isinstance(e, dict) and e.get("vmid") and e.get("status") == "running"
    }


def _name_list(guests: list[GuestInfo], limit: int = 5) -> str:
    """Comma-separated guest labels, capped so one bad cluster cannot flood the log."""
    shown = ", ".join(g.label for g in guests[:limit])
    rest = len(guests) - limit
    return shown + (f" and {rest} more" if rest > 0 else "")


async def _shutdown_guests(
    client: httpx.AsyncClient, targets: list[GuestInfo], steps: list[str], *,
    deadline: float, force_after_s: Optional[int],
) -> tuple[bool, list[GuestInfo]]:
    """Stop every guest in ``targets`` and VERIFY they are down. Never raises.

    Same doctrine as _set_ceph_flags: the shutdown call answers with a worker UPID and
    says nothing about the outcome, so success is only ever concluded from a re-read of
    /cluster/resources.

    ``force_after_s`` is handed to Proxmox as the call's own timeout together with
    ``forceStop``, so the kill still happens if we lose the network right after asking —
    and it is ALSO enforced from here, because that delegated kill is invisible to us.
    None disables both: a guest that ignores the request then fails the step by name.
    """
    if not targets:
        return True, []

    sem = asyncio.Semaphore(_GUEST_CONCURRENCY)
    rejected: list[str] = []

    async def ask(guest: GuestInfo) -> None:
        data: dict[str, object] = {}
        if force_after_s is not None:
            data = {"timeout": int(force_after_s), "forceStop": 1}
        async with sem:
            # The same guard the force round below carries, and it belongs here just as
            # much: this round runs BEFORE ``grace`` is computed, so time spent in it is
            # taken straight off the graceful wait.
            if _remaining(deadline) <= 0:
                return
            err = await _post(client, f"{guest.path}/status/shutdown", data=data)
        if err:
            rejected.append(f"{guest.label}: {err}")

    # Bounded as a round, not only per request. Each _post is limited by the client's
    # timeout alone, so a control plane that accepts connections and then goes quiet cost
    # CONNECT_TIMEOUT_S per batch of _GUEST_CONCURRENCY — on a large cluster more than the
    # whole guest budget, spent before a single guest had been given a chance to stop.
    # ``grace`` below is measured from what is left, so that collapsed it to zero and
    # force-stopped every guest at once instead of asking it: exactly the failure the
    # deadline rework removed one step earlier, at the HA disarm.
    try:
        await asyncio.wait_for(
            asyncio.gather(*(ask(g) for g in targets)),
            timeout=max(0.5, _remaining(deadline) - _MARGIN_S),
        )
    except (asyncio.TimeoutError, TimeoutError):
        steps.append("shutdown requests were cut short by the guest deadline")
    steps.append(
        f"shutdown requested for {len(targets)} guests"
        + (f" ({len(rejected)} rejected)" if rejected else "")
    )
    for line in rejected[:5]:
        steps.append(line)
    if len(rejected) > 5:
        steps.append(f"and {len(rejected) - 5} more rejections")

    wanted = {g.vmid for g in targets}
    by_id = {g.vmid: g for g in targets}

    def down(running: Optional[set[int]]) -> bool:
        # An unreadable list is not a stopped cluster. See _read_running_vmids.
        return running is not None and not (wanted & running)

    async def still_running() -> list[GuestInfo]:
        """The targets we cannot see stopped, bounded by what is left of the deadline.

        An unreadable list yields every target rather than none: we asked all of them to
        stop and can confirm none of them, so "all of them may still be up" is the only
        reading that does not invent evidence. Force-stopping a guest that is already down
        costs one rejected request, which is logged; skipping one that is still up costs
        the cluster its orderly shutdown.
        """
        running = await _read_running_vmids(client, _remaining(deadline))
        if running is None:
            steps.append(
                "the guest list could not be re-read, so it is unknown which guests are "
                "still running - treating all of them as running"
            )
            return list(targets)
        return [by_id[v] for v in sorted(wanted & running)]

    # Wait at most what the setting promises, and never more than the share of the budget
    # that still leaves room to kill the stragglers. Waiting the full share regardless
    # would make "force-stop after 120 s" a statement only Proxmox's own timeout honours,
    # while this loop sat there for three minutes.
    grace = max(0.0, (_remaining(deadline) - _MARGIN_S) * _GUEST_GRACE_SHARE)
    if force_after_s is not None:
        grace = min(grace, float(force_after_s))
    if await _verify(
        down, lambda: _read_running_vmids(client, _remaining(deadline)),
        grace, _GUEST_POLL_INTERVAL_S,
    ):
        steps.append(f"all {len(targets)} guests stopped")
        return True, []

    left = await still_running()
    if force_after_s is None:
        steps.append(f"still running, and force-stop is disabled: {_name_list(left)}")
        return False, left

    steps.append(f"force-stopping {len(left)} guests that did not stop: {_name_list(left)}")

    # Concurrently and under the same semaphore as the graceful round above, not one after
    # another: a sequential loop was bounded only by the client's timeout, so a control
    # plane that had stopped answering spent CONNECT_TIMEOUT_S per straggler. Twenty of
    # them outlasted prepare()'s outer wait_for, and the whole preparation then came back
    # as a bare "gave up" - taking the Ceph step, whose reserve exists precisely for this,
    # with it.
    async def kill(guest: GuestInfo) -> None:
        async with sem:
            if _remaining(deadline) <= 0:
                return
            err = await _post(client, f"{guest.path}/status/stop")
        if err:
            steps.append(f"{guest.label}: {err}")

    try:
        await asyncio.wait_for(
            asyncio.gather(*(kill(g) for g in left)),
            timeout=max(0.5, _remaining(deadline) - _MARGIN_S),
        )
    except (asyncio.TimeoutError, TimeoutError):
        steps.append("force-stop requests were cut short by the preparation deadline")

    if await _verify(
        down, lambda: _read_running_vmids(client, _remaining(deadline)),
        max(0.0, _remaining(deadline) - _MARGIN_S), _GUEST_POLL_INTERVAL_S,
    ):
        steps.append("all guests stopped (some had to be forced)")
        return True, []

    survivors = await still_running()
    steps.append(f"guests still running after force-stop: {_name_list(survivors)}")
    return False, survivors


def stop_targets(
    guests: list[GuestInfo], self_guest: Optional[GuestInfo], hostname: str = ""
) -> list[GuestInfo]:
    """The guests a cluster-wide stop may touch. Pure, so the preview and the write agree.

    Excluded: this appliance's own guest, templates, and anything not running. The
    hostname comparison on top is cheap insurance against a wrong selection — if a guest
    carries our own hostname it is not stopped even when a different one was picked.
    """
    spare = {self_guest.vmid} if self_guest else set()
    label = (hostname or "").strip().split(".", 1)[0].casefold()
    return [
        g
        for g in guests
        if g.status == "running"
        and not g.template
        and g.vmid not in spare
        and not (label and g.name.strip().casefold() == label)
    ]


async def _read_armed_state(client: httpx.AsyncClient) -> str:
    data, _ = await _get(client, "/cluster/ha/status/current")
    if not isinstance(data, list):
        return ""
    for entry in data:
        if isinstance(entry, dict) and entry.get("type") == "fencing":
            return str(entry.get("armed-state") or entry.get("armed_state") or "")
    return ""


async def _prepare(
    host: PveHostConfig, want_ceph: bool, want_disarm: bool, want_guests: bool,
    guests: list[GuestInfo], self_guest: Optional[GuestInfo], guest_needs_disarm: bool,
    hostname: str, force_after_s: Optional[int], steps: list[str],
    deadline: float, overall_deadline: float, guest_timeout: float,
) -> ActionResult:
    """The three preparation steps, in the only order that works.

    HA disarm, then the guests, then the Ceph flags. Disarming after the guests would let
    the HA manager restart them as fast as they are stopped; setting the flags before the
    guests would leave them on if the stop then fails, and they are useless while clients
    are still writing anyway.
    """
    ok = True
    loop = asyncio.get_running_loop()
    disarmed = False
    async with _client(host, CONNECT_TIMEOUT_S) as client:
        if want_disarm:
            # A cluster stays prepared until someone presses "Restore cluster", so a
            # second outage can arrive at an already disarmed stack. Reading before
            # writing keeps that case out of the write path entirely.
            seen = [await _read_armed_state(client)]
            if seen[-1] == DISARMED:
                steps.append("HA already disarmed")
                disarmed = True
            else:
                # resource-mode is hard-wired to "ignore" and is not configurable. Under
                # "ignore" the guests leave HA tracking, which makes vm_is_ha_managed()
                # report false, which in turn is what lets pve-guests shut them down in
                # their configured order. Under "freeze" they stay HA-managed: pve-guests
                # skips them and the disarmed LRM no longer stops them, so they would be
                # killed by the power-off. That is a bug, not an option.
                err = await _post(
                    client, "/cluster/ha/status/disarm-ha",
                    data={"resource-mode": "ignore"},
                )
                if err:
                    steps.append(f"HA disarm rejected ({err})")
                    ok = False
                else:
                    # Verify rather than trust the POST: the transition passes through
                    # "disarming", and only "disarmed" means every LRM has released its
                    # watchdog and the handover to pve-guests is actually in effect.
                    # Shutting down while still "disarming" is the one state in which
                    # nobody stops the guests. This is the slow step -- every LRM answers
                    # in rounds of ten seconds -- so it gets everything the flags below
                    # do not need.
                    started = loop.time()

                    def reached(state: str) -> bool:
                        seen.append(state)
                        return state == DISARMED

                    if await _verify(
                        reached,
                        lambda: _read_armed_state(client),
                        _disarm_budget(deadline, want_ceph),
                    ):
                        steps.append("HA disarmed (resource-mode=ignore)")
                        disarmed = True
                    else:
                        # Name the state it stopped at and how long it waited: "not in
                        # time" alone left the operator unable to tell a stack that was
                        # still disarming (give it more budget) from one that never moved
                        # (a real fault) -- they read identically in the event log.
                        steps.append(
                            f"HA disarm was accepted but stopped at "
                            f"'{seen[-1] or 'no state reported'}' after "
                            f"{loop.time() - started:.0f}s - raise the cluster "
                            f"preparation timeout if it was still disarming"
                        )
                        ok = False

        if want_guests:
            # The one gate that can only be decided here, after the write: stopping guests
            # while the HA manager is live and holding resources just feeds it work. The
            # statically decidable cases (no disarm-ha on this release, the switch turned
            # off) are caught and reported by the engine before we are even called.
            if guest_needs_disarm and not disarmed:
                steps.append(
                    "guest shutdown skipped - HA is still armed and manages resources, "
                    "so the HA manager would restart the guests as fast as they stop"
                )
            else:
                targets = stop_targets(guests, self_guest, hostname)
                if not targets:
                    steps.append("no guests to stop")
                else:
                    spared = f", sparing {self_guest.label}" if self_guest else ""
                    steps.append(f"stopping {len(targets)} guests{spared}")
                    # Measured from HERE, not from the start of prepare(). The disarm
                    # runs first and legitimately takes tens of seconds, and it used to
                    # spend those out of the guests' budget: a disarm longer than
                    # guest_timeout left grace = 0, which force-stopped every guest at
                    # once instead of asking it to shut down. Capped against the overall
                    # deadline so the Ceph step keeps its reserve and the sequence stays
                    # inside prepare()'s outer bound — otherwise a guest stop that used
                    # exactly its configured time would be cut off as a bare "gave up".
                    guest_deadline = min(
                        loop.time() + guest_timeout,
                        overall_deadline - (_ceph_reserve(overall_deadline)
                                            if want_ceph else 0.0),
                    )
                    stopped, _left = await _shutdown_guests(
                        client, targets, steps,
                        deadline=guest_deadline, force_after_s=force_after_s,
                    )
                    if not stopped:
                        ok = False

        if want_ceph:
            # Set even when the guest step failed: the flags cost nothing, still stop the
            # rebalancing, and leaving them off would give the worst of both worlds.
            if not await _set_ceph_flags(client, True, steps, overall_deadline):
                ok = False
    return ActionResult(ok, "; ".join(steps) or "nothing to do", steps)


async def prepare(
    host: PveHostConfig, *, want_ceph: bool = False, want_disarm: bool = False,
    want_guests: bool = False, guests: Optional[list[GuestInfo]] = None,
    self_guest: Optional[GuestInfo] = None, guest_needs_disarm: bool = True,
    hostname: str = "", timeout: float, guest_timeout: float = 0.0,
    force_after_s: Optional[int] = None,
) -> ActionResult:
    """Prepare one cluster for shutdown. Never raises, never exceeds its budgets.

    Two budgets, not one. ``timeout`` covers the control-plane work -- the HA disarm and
    the Ceph flags -- and is handed down so the disarm really waits it out rather than
    being cut off by an outer guillotine. ``guest_timeout`` covers the cluster-wide guest
    stop alone. They are separate because they measure different things: the first against
    CRM/LRM rounds of ten seconds, which barely vary, the second against how long this
    particular estate takes to stop. Sharing one number would mean the guests eating the
    disarm's budget on every config that never touched the setting.

    ``steps`` is owned here rather than by ``_prepare`` so that a timeout -- which cancels
    it mid-write -- can still report what had been done. "Gave up" alone would leave the
    operator unable to tell a cluster that was never touched from one that is half
    prepared, which is exactly the state they need to know about afterwards.
    """
    steps: list[str] = []
    loop = asyncio.get_running_loop()
    total = timeout + (guest_timeout if want_guests else 0.0)
    # Two deadlines, and they answer different questions. ``deadline`` is the control
    # plane's: it is what the HA disarm waits out, measured in CRM/LRM rounds.
    # ``overall_deadline`` is the whole sequence's, and it is what the guest phase and the
    # Ceph step measure against — the Ceph step runs last and used to budget itself off
    # the control-plane deadline, which a long guest stop had exhausted long before.
    deadline = loop.time() + timeout
    overall_deadline = loop.time() + total
    try:
        return await asyncio.wait_for(
            _prepare(
                host, want_ceph, want_disarm, want_guests, guests or [], self_guest,
                guest_needs_disarm, hostname, force_after_s, steps, deadline,
                overall_deadline, guest_timeout,
            ),
            timeout=total,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return ActionResult(
            False,
            f"Cluster preparation exceeded {total:.0f}s - gave up"
            + (f" (done so far: {'; '.join(steps)})" if steps else ""),
            steps,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Cluster preparation failed: {exc}", steps)


async def _restore(host: PveHostConfig, steps: list[str], deadline: float) -> ActionResult:
    ok = True
    async with _client(host, CONNECT_TIMEOUT_S) as client:
        state = await _read_armed_state(client)
        if state and state != ARMED:
            err = await _post(client, "/cluster/ha/status/arm-ha")
            if err:
                steps.append(f"HA arm rejected ({err})")
                ok = False
            # The mirror of the disarm and just as slow, so it gets the same share of the
            # budget — what is left, minus the slice the Ceph step below still needs.
            elif await _verify(
                lambda s: s == ARMED,
                lambda: _read_armed_state(client),
                max(0.0, _remaining(deadline) * (1 - _CEPH_SHARE) - _MARGIN_S),
            ):
                steps.append("HA armed again")
            else:
                steps.append("HA arm was accepted but did not reach 'armed' in time")
                ok = False

        flags = await _read_ceph_flags(client)
        if any(flags.get(f) for f in CEPH_MAINTENANCE_FLAGS):
            if not await _set_ceph_flags(client, False, steps, deadline):
                ok = False
    return ActionResult(ok, "; ".join(steps) or "nothing to restore", steps)


async def restore(host: PveHostConfig, timeout: float = 60.0) -> ActionResult:
    """Undo the preparation: arm HA again and clear the maintenance flags.

    Only ever runs on an explicit request (the dashboard button) — never automatically.
    Bringing HA back while nodes are still booting is a decision for an operator who can
    see the cluster, not for a timer.

    ``steps`` is owned here rather than by ``_restore`` for the same reason as in
    prepare(): a timeout cancels it mid-write, and a half-finished restore is precisely
    what the operator has to hear about.
    """
    steps: list[str] = []
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        return await asyncio.wait_for(_restore(host, steps, deadline), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return ActionResult(
            False,
            f"Cluster restore exceeded {timeout:.0f}s — gave up"
            + (f" (done so far: {'; '.join(steps)})" if steps else ""),
            steps,
        )
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Cluster restore failed: {exc}", steps)


# What each privilege buys, appended to its name in the reports below. "403" tells an
# operator nothing, and a bare "Sys.Console" only marginally more — the purpose is what
# lets them decide whether to grant it at all, which matters most for Sys.Console.
PRIV_PURPOSE = {
    PRIV_AUDIT: "cluster status",
    PRIV_MODIFY: "Ceph maintenance flags",
    PRIV_CONSOLE: "HA disarm",
    PRIV_VM_AUDIT: "list the cluster's guests",
    PRIV_VM_POWER: "stop the guests before the shutdown",
    PRIV_DS_AUDIT: "detect Ceph-backed storage",
}


def missing_privileges(
    info: ClusterInfo, want_ceph: bool, want_disarm: bool, want_guests: bool = False
) -> list[str]:
    """Privileges the token still needs for the requested features, named with their use.

    Least privilege in both directions: a feature that is switched off needs nothing, and
    neither does one this cluster cannot do at all. Demanding Sys.Modify on a cluster
    without Ceph — or Sys.Console on a release without disarm-ha — would send the operator
    off to widen a token for something that will never run.

    An *unread* state is not an absent one: while the read was merely denied, the
    privilege is still reported, so a token that lacks everything is told about everything
    instead of one round at a time.
    """
    missing = []
    if not info.can_audit:
        missing.append(PRIV_AUDIT)
    if want_ceph and not info.ceph_unavailable and not info.can_modify:
        missing.append(PRIV_MODIFY)
    if want_disarm and not info.disarm_unavailable and not info.can_console:
        missing.append(PRIV_CONSOLE)
    if want_guests:
        # No "unavailable" carve-out for these two: every cluster has the endpoints, so a
        # missing privilege is always the reason and always actionable.
        if not info.can_vm_audit:
            missing.append(PRIV_VM_AUDIT)
        if not info.can_vm_power:
            missing.append(PRIV_VM_POWER)
    return [f"{p} ({PRIV_PURPOSE[p]})" for p in missing]


def advisory_privileges(info: ClusterInfo) -> list[str]:
    """Privileges that only buy a better diagnosis, never a working preparation.

    Kept out of missing_privileges on purpose: that list means "this will not work", and
    reporting a merely missing hint there would send an operator to widen a token over a
    setup that is entirely fine.
    """
    if info.can_ds_audit:
        return []
    return [f"{PRIV_DS_AUDIT} ({PRIV_PURPOSE[PRIV_DS_AUDIT]})"]


# --- node name matching ------------------------------------------------------
# The configured node name is used verbatim in POST /nodes/{node}/status, so it has to be
# the node's real name. That makes a mismatch a shutdown that fails during the outage —
# and makes silently accepting a near miss the worst possible kindness.


def _norm(name: str) -> str:
    """Loose form of a node name — for EXPLAINING a mismatch, never for matching.

    Case and a domain suffix are exactly what people get wrong, and pointing at the
    probable node ("'PVE01' likely means 'pve01'") is worth a lot. Matching on this form
    instead would hide a shutdown that really does fail: PVE resolves the path segment
    literally.
    """
    return name.strip().split(".", 1)[0].casefold()


@dataclass
class NodeCoverage:
    """Which cluster nodes have a configured shutdown target, and what the rest are."""

    covered: list[str] = field(default_factory=list)   # nodes with an enabled target
    missing: list[str] = field(default_factory=list)   # nodes nobody will shut down
    unmatched: list[str] = field(default_factory=list)  # entry names matching no node
    # (entry name, the node it probably meant) — the actionable half of ``unmatched``.
    near: list[tuple[str, str]] = field(default_factory=list)
    # Disabled entries that WOULD match. The warning is right either way, but "misnamed"
    # and "deliberately switched off" call for completely different actions.
    disabled: list[str] = field(default_factory=list)


def node_coverage(
    nodes: list[str], configured: list[str], disabled: Optional[list[str]] = None
) -> NodeCoverage:
    """Compare configured target names against the node names the API reported.

    Pure and total — no I/O, no exceptions — so both the scheduled health check and the
    interactive host test can describe the same comparison in the same words.
    """
    known = {n for n in nodes if n}
    entries = {c for c in configured if c}
    cov = NodeCoverage(
        covered=[n for n in nodes if n in entries],
        missing=[n for n in nodes if n not in entries],
        unmatched=[c for c in configured if c and c not in known],
        disabled=[d for d in (disabled or []) if d in known],
    )
    loose = {_norm(n): n for n in nodes if n}
    for name in cov.unmatched:
        candidate = loose.get(_norm(name))
        if candidate:
            cov.near.append((name, candidate))
    return cov


def coverage_report(nodes: list[str], cov: NodeCoverage) -> str:
    """The mismatch in words, naming BOTH sides of the comparison.

    Reporting only the counts ("2 nodes, 0 of them configured") states the conclusion and
    withholds every fact needed to act on it: the operator cannot see which names were
    compared, and the usual cause — a case difference or a domain suffix — is invisible
    precisely because it looks right to a human eye.
    """
    parts = [
        f"The cluster has {len(nodes)} nodes: {', '.join(nodes)}. "
        f"Configured here: {', '.join(cov.covered) or 'none'}."
    ]
    if cov.missing:
        parts.append(f"Not covered, they will keep running: {', '.join(cov.missing)}.")
    if cov.unmatched:
        parts.append(
            "Configured entries matching no node: "
            + ", ".join(f"'{n}'" for n in cov.unmatched)
            + "."
        )
        if cov.near:
            parts.append(
                "; ".join(f"'{entry}' likely means '{node}'" for entry, node in cov.near)
                + "."
            )
        # Said whenever an entry matches nothing, not only on a near miss: this is the
        # rule that was broken, and without it the list above reads as arbitrary.
        parts.append(
            "The name is used verbatim in the shutdown call /nodes/<name>/status, so it "
            "must match the node name exactly — lower case, without a domain suffix."
        )
    if cov.disabled:
        parts.append(
            ", ".join(f"'{d}'" for d in cov.disabled)
            + (" would match, but that entry is disabled."
               if len(cov.disabled) == 1
               else " would match, but those entries are disabled.")
        )
    return " ".join(parts)


# --- the appliance's own guest -----------------------------------------------
# The one guest a cluster-wide stop must never touch. Getting this wrong means the
# appliance shuts itself down in the middle of an outage, so the rules are deliberately
# strict rather than helpful.


def find_self_guest(
    guests: list[GuestInfo], vmid: Optional[int], node: str, hostname: str
) -> tuple[Optional[GuestInfo], str]:
    """Resolve this appliance's guest. Returns (guest, source). Pure and total.

    An explicitly selected vmid is used, and if it is not there the answer is "missing" —
    NOT a fall back to the hostname. Guessing after an explicit choice is how a renumbered
    or migrated appliance ends up stopping itself: the selection said which guest to
    spare, and that guest is not here, so nothing may be assumed about the rest.

    Without a selection the hostname is matched against the guest names, preferring a
    container because that is what the installer creates. Anything less than exactly one
    match stays unresolved — "ambiguous" and "none" are different problems and get
    different advice.
    """
    if vmid is not None:
        for g in guests:
            if g.vmid == vmid and (not node or g.node == node):
                return g, "config"
        return None, "missing"

    label = (hostname or "").strip().split(".", 1)[0].casefold()
    if not label:
        return None, "none"
    hits = [g for g in guests if g.name.strip().casefold() == label]
    if not hits:
        return None, "none"
    if len(hits) > 1:
        containers = [g for g in hits if g.kind == "lxc"]
        if len(containers) != 1:
            return None, "ambiguous"
        hits = containers
    return hits[0], "hostname"


# Guest config keys that carry a "<storage>:<volume>" reference. unusedN counts too: a
# detached disk still pins the appliance to that storage.
_VOLUME_KEY_RE = re.compile(
    r"^(rootfs|mp\d+|scsi\d+|virtio\d+|ide\d+|sata\d+|unused\d+|efidisk\d+|tpmstate\d+)$"
)


def storages_of_config(config: dict) -> list[str]:
    """Storage names referenced by a guest config. Pure, order-preserving, deduplicated.

    Only named storages count. A path-style volume ("/dev/…", "/mnt/…") has no storage in
    front of the colon and is skipped rather than guessed at.
    """
    found: list[str] = []
    for key, value in config.items():
        if not isinstance(value, str) or not _VOLUME_KEY_RE.match(str(key)):
            continue
        head = value.split(",", 1)[0]
        name, sep, _volume = head.partition(":")
        name = name.strip()
        if sep and name and not name.startswith("/") and name not in found:
            found.append(name)
    return found


# --- MON ordering ------------------------------------------------------------
# The official procedure powers down nodes without a MON first. This is only ever
# reported, never enforced: ``order`` is explicit, visible configuration, and silently
# re-sorting it at outage time would make the order shown on the dashboard a lie. MON
# names are also not guaranteed to equal node names, so an automatic reorder would be
# built on a mapping that can be wrong.


def mon_ordering_ok(ordered_node_names: list[str], mon_nodes: list[str]) -> bool:
    """True when no MON node is shut down before a node without one."""
    mons = {m for m in mon_nodes if m}
    seen_mon = False
    for name in ordered_node_names:
        if name in mons:
            seen_mon = True
        elif seen_mon:
            return False
    return True


def mon_order_report(
    ordered_node_names: list[str], mon_nodes: list[str], this_host_node: str = ""
) -> str:
    """The ordering problem in words, or an empty string when there is none."""
    mons = [m for m in mon_nodes if m in set(ordered_node_names)]
    if not mons or mon_ordering_ok(ordered_node_names, mon_nodes):
        return ""
    monset = set(mons)
    early = [n for n in ordered_node_names if n in monset]
    late = [n for n in ordered_node_names if n not in monset]
    parts = [
        "Ceph monitors run on " + ", ".join(mons) + ", but the configured shutdown order "
        "is " + ", ".join(ordered_node_names) + ". Proxmox recommends powering down the "
        "nodes without a monitor first, so raise 'order' on " + ", ".join(early)
        + " above " + ", ".join(late) + "."
    ]
    if this_host_node and this_host_node in monset:
        parts.append(
            f"Note that {this_host_node} carries this appliance and is forced last "
            f"regardless of its order, which happens to be right here."
        )
    elif this_host_node and this_host_node in set(ordered_node_names):
        # Saying "raise its order" about a node the sort key overrides would be advice
        # that cannot work, so name the rule instead.
        parts.append(
            f"{this_host_node} carries this appliance and is always shut down last, "
            f"whatever its order says."
        )
    return " ".join(parts)

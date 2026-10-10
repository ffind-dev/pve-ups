"""Configuration model and persistence.

The entire appliance is configured through a *single* YAML file, written exclusively
by the web UI. No hand-editing of config files anywhere — that is the whole point of
this project. Secrets live in the same file, which is created with 0600 permissions.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import os
import secrets
import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Optional, Union

import yaml
from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)

# Location can be overridden for tests / local runs.
CONFIG_PATH = Path(os.environ.get("PVE_USV_CONFIG", "/etc/pve-usv/config.yaml"))

# Selectable self-test intervals in minutes. Every one of them divides 1440 evenly, so the
# schedule forms an exact daily grid anchored at ``selftest_hour`` (see engine.selftest_slot).
# The <select> in app/web/index.html must offer exactly these values.
SELFTEST_INTERVALS = (15, 30, 60, 120, 180, 360, 720, 1440)


class UpsSourceType(str, Enum):
    """How a UPS is read. The value is the ``type`` discriminator in the config.

    Every member needs a matching ``src.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    snmp = "snmp"  # UPS with an SNMP network card, RFC 1628
    nut = "nut"  # any Network UPS Tools server (upsd) over TCP


class SnmpVersion(str, Enum):
    v1 = "v1"
    v2c = "v2c"
    v3 = "v3"


class SnmpMib(str, Enum):
    """Which UPS MIB the SNMP poller reads (see app/ups.py: PROFILES).

    Every member needs a matching ``mib.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    auto = "auto"  # read RFC 1628, switch to a vendor MIB when the device answers it
    rfc1628 = "rfc1628"  # the standard UPS-MIB only
    apc = "apc"  # APC PowerNet-MIB (1.3.6.1.4.1.318) only
    cyberpower = "cyberpower"  # CyberPower CPS-MIB (1.3.6.1.4.1.3808) only


class SnmpAuthProto(str, Enum):
    none = "none"
    md5 = "md5"
    sha = "sha"
    sha256 = "sha256"
    sha512 = "sha512"


class SnmpPrivProto(str, Enum):
    none = "none"
    des = "des"
    aes = "aes"
    aes256 = "aes256"


# Ranges outside which a number is not a setting but a fault. Applied by _repair() below.
#
# CORRECTED, never rejected. That is the same rule _clamp_selftest_hour() already follows,
# and here it is load-bearing: load_config() does not catch ValidationError and app.main
# calls it straight from the lifespan, so a stored value outside these ranges would stop
# the service from starting at all. An appliance that does not come up protects nothing;
# a corrected one that says so loudly does. The correction is always to the field's own
# default rather than to the nearest bound — "poll every 1 s" and "shut down at 100 %
# charge" are technically in range and still not what anyone meant.
def _repair(model, field: str, lo=None, hi=None) -> None:
    """Pull one field back to its default if it left the sane range. Records the change."""
    value = getattr(model, field, None)
    if value is None:  # "off" / "inherit" — never a fault
        return
    if (lo is None or value >= lo) and (hi is None or value <= hi):
        return
    default = type(model).model_fields[field].get_default(call_default_factory=True)
    setattr(model, field, default)
    model._corrections.append(f"{field}={value!r} is out of range, using {default!r}")


class UpsThresholdOverride(BaseModel):
    """Optional per-UPS override of the global trigger thresholds.

    Every field defaults to ``None`` meaning "inherit the global value". Only the
    trigger-relevant thresholds are overridable; the loop cadence
    (poll intervals, ``unreachable_alarm_after_polls``) stays global.
    """

    on_battery_seconds: Optional[int] = None
    runtime_below_minutes: Optional[int] = None
    charge_below_percent: Optional[int] = None
    on_battery_low: Optional[bool] = None
    comm_loss_shutdown_after_min: Optional[int] = None
    keep_shutdown_on_comm_loss: Optional[bool] = None

    _corrections: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _repair_ranges(self) -> "UpsThresholdOverride":
        # The same bounds as Thresholds below, at BOTH ends. An override is exactly where
        # a slipped digit goes unnoticed — it is edited on one card among several and
        # nothing else in the interface shows the resulting number — so leaving the
        # ceilings out here made the correction the release advertises apply to every
        # threshold except the ones set per device. Correcting to the field default
        # (None = "inherit the global value") is the safe direction: the estate-wide
        # setting takes over, and value_corrections() names what happened.
        _repair(self, "on_battery_seconds", lo=1, hi=86400)
        _repair(self, "runtime_below_minutes", lo=1, hi=1440)
        _repair(self, "charge_below_percent", lo=0, hi=100)
        _repair(self, "comm_loss_shutdown_after_min", lo=1, hi=1440)
        return self


class UpsBase(BaseModel):
    """Fields every UPS source shares, regardless of how it is read.

    The engine, the thresholds and the host↔UPS mapping only ever touch these — a new
    source type therefore inherits the whole trigger/policy/fail-safe machinery.
    """

    # Identity (multi-UPS): ``id`` is a stable slug referenced by hosts, ``name`` is the
    # human label shown in the UI. ``id`` is auto-filled on save if left empty.
    id: str = ""
    name: str = ""

    # Optional per-UPS threshold override (None fields inherit the global thresholds).
    overrides: UpsThresholdOverride = UpsThresholdOverride()

    @property
    def configured(self) -> bool:
        """True once the source has enough information to be polled."""
        return False

    @property
    def label(self) -> str:
        """Display label, falling back to id and a per-type hint when no name is set."""
        return self.name or self.id or self._fallback_label() or "UPS"

    def _fallback_label(self) -> str:
        return ""

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        """Secret field names -> default value, for the API's masked-secret reconcile."""
        return {}


class SnmpConfig(UpsBase):
    # Plain string literal (not the enum) so a YAML/JSON "snmp" validates directly and
    # round-trips through yaml.safe_dump untouched. UpsSourceType stays the single list
    # of valid values; tests/test_i18n.py keeps both in sync.
    type: Literal["snmp"] = "snmp"

    host: str = ""
    port: int = 161
    version: SnmpVersion = SnmpVersion.v2c
    timeout_s: float = 3.0
    retries: int = 1

    # Which MIB to read. "auto" needs no migration: a config written before 3.3.0 has no
    # ``mib`` key and picks up this default, which is exactly what an existing APC device
    # wants. See app/ups.py for the profiles and how "auto" resolves.
    mib: SnmpMib = SnmpMib.auto

    # v1/v2c
    community: SecretStr = SecretStr("public")

    # v3
    v3_user: str = ""
    v3_auth_proto: SnmpAuthProto = SnmpAuthProto.sha
    v3_auth_pass: SecretStr = SecretStr("")
    v3_priv_proto: SnmpPrivProto = SnmpPrivProto.aes
    v3_priv_pass: SecretStr = SecretStr("")

    @property
    def configured(self) -> bool:
        return bool(self.host)

    def _fallback_label(self) -> str:
        return self.host

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        return {"community": "public", "v3_auth_pass": "", "v3_priv_pass": ""}

    _corrections: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _repair_ranges(self) -> "SnmpConfig":
        _repair(self, "port", lo=1, hi=65535)
        # A non-positive timeout or a negative retry count goes straight into pysnmp.
        _repair(self, "timeout_s", lo=0.5)
        _repair(self, "retries", lo=0, hi=10)
        return self


class NutConfig(UpsBase):
    """A UPS read from a Network UPS Tools server (``upsd``) over TCP.

    We are a read-only client: PVE-UPS never runs ``upsmon`` and never hands the
    shutdown decision to NUT. Any existing upsd works — a NAS with its built-in UPS
    server, a Raspberry Pi, or the driver bundled with this appliance.
    """

    type: Literal["nut"] = "nut"

    host: str = ""  # upsd host (127.0.0.1 for a locally attached UPS)
    port: int = 3493
    ups_name: str = ""  # section name in upsd's ups.conf, e.g. "ups"
    # Credentials are optional: reading variables is anonymous on a default upsd.
    username: str = ""
    password: SecretStr = SecretStr("")
    timeout_s: float = 3.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.ups_name)

    def _fallback_label(self) -> str:
        return f"{self.ups_name}@{self.host}" if self.host else self.ups_name

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        return {"password": ""}

    _corrections: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _repair_ranges(self) -> "NutConfig":
        _repair(self, "port", lo=1, hi=65535)
        _repair(self, "timeout_s", lo=0.5)
        return self


# Discriminated union of every supported source. Adding a type means: a model here, a
# branch in app/sources.py, an entry in UpsSourceType and the matching i18n keys.
UpsSource = Annotated[Union[SnmpConfig, NutConfig], Field(discriminator="type")]

UPS_SOURCE_MODELS: dict[str, type[UpsBase]] = {
    "snmp": SnmpConfig,
    "nut": NutConfig,
}


class ShutdownMethod(str, Enum):
    api_token = "api_token"


class HostType(str, Enum):
    """Which Proxmox product a shutdown target is. The value is the ``type``
    discriminator in the config.

    Every member needs a matching ``htype.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    pve = "pve"  # Proxmox VE node
    pbs = "pbs"  # Proxmox Backup Server


class HostConfig(BaseModel):
    """Fields every shutdown target shares, regardless of which product it runs.

    The engine, the host↔UPS mapping and the whole trigger machinery only ever touch
    these — a new target type therefore inherits the eligibility, ordering and
    fail-safe logic unchanged. What differs per product (auth header, ACL path,
    privilege name, node path) lives in the client module behind app/targets.py.
    """

    # Identity, mirroring UpsBase and WebhookConfig: ``id`` is a stable slug (auto-filled
    # on save), ``name`` the label. Both are needed because ``name`` is edited — it is the
    # Proxmox node name, and correcting a typo in it must not read as "a different host".
    id: str = ""
    name: str  # Proxmox node name, e.g. "pve01"; a free label for products that ignore it
    api_url: str  # e.g. "https://10.0.0.10:8006"
    method: ShutdownMethod = ShutdownMethod.api_token
    # API token: user@realm!tokenid + secret
    token_id: str = ""  # "ups@pve!shutdown"
    token_secret: SecretStr = SecretStr("")
    verify_tls: bool = False  # PVE ships self-signed certs by default
    this_host: bool = False  # the host carrying this appliance -> shut down last
    order: int = 0  # ascending; this_host is forced last regardless
    enabled: bool = True

    # Multi-UPS: which UPS devices feed this host (by UPS id). Empty = depends
    # on ALL configured UPS (conservative fallback). ``ups_policy`` decides how the
    # feeds combine: "all" = shut down only when every feed has triggered (redundant
    # PSUs, default), "any" = shut down as soon as one feed triggers (split, non-
    # redundant load).
    ups_ids: list[str] = Field(default_factory=list)
    ups_policy: Literal["all", "any"] = "all"

    @property
    def key(self) -> str:
        """Stable runtime key (shutdown latches, per-host state, secret reconcile).

        The id, precisely because it does not change when the entry does. It used to be
        "<type>:<name>", which tied identity to an editable field: correcting a node name
        discarded the stored token secret and the self-test result, and two entries sharing
        a type and a name — a duplicated row with the IP not adjusted — shared one shutdown
        latch, so only the first of them was ever fired.

        Falls back to the old form while the id is still empty (a config loaded but not yet
        through assign_host_ids), because this key must never be blank.
        """
        return self.id or f"{getattr(self, 'type', 'pve')}:{self.name}"

    @property
    def api_node(self) -> str:
        """The ``{node}`` path segment of the shutdown call."""
        return self.name

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        """Secret field names -> default value, for the API's masked-secret reconcile."""
        return {"token_secret": ""}


class PveHostConfig(HostConfig):
    """A Proxmox VE node (the original, and still the default target)."""

    # Plain string literal (not the enum) for the same reason as SnmpConfig.type: a
    # YAML/JSON "pve" validates directly and round-trips through yaml.safe_dump
    # untouched. HostType stays the single list of valid values.
    type: Literal["pve"] = "pve"

    # --- cluster preparation (PBS has no cluster, hence only here) -------------
    # Opt-in: writing to a cluster's Ceph and HA state is never done unasked. Members of
    # one cluster are grouped at runtime by the cluster name discovered from the API,
    # not from configuration.
    cluster: bool = False
    # Whether the whole cluster goes down as soon as ONE of its nodes is due.
    #
    # Default on, because the preparation is already cluster-wide: it disarms HA and — with
    # the Ceph option or the guest stop — stops every guest in the cluster. Shutting down only the nodes whose
    # own UPS happened to trigger then leaves the rest standing with no guests, HA disarmed
    # and the maintenance flags set. On a hyper-converged cluster that is worse than it
    # sounds: take two of three monitors down and Ceph has no quorum, so the survivors have
    # no working storage either. Matching the shutdown to the preparation is the only
    # coherent option; the alternative is two different notions of "the cluster".
    #
    # Turn it off for a plain cluster where HA should recover the guests of a single failing
    # node onto the others — there a partial shutdown is a perfectly good outcome.
    cluster_shutdown_all: bool = True
    # Split because the privileges differ sharply: the Ceph flags need Sys.Modify, while
    # arming/disarming HA needs Sys.Console — effectively shell access to the nodes. Who
    # only wants the Ceph part must not be forced to hand out Sys.Console.
    #
    # Off by default, unlike the disarm below: Ceph is the exception rather than the rule
    # (ZFS replication, NFS/iSCSI), and on a cluster without it every single attempt would
    # fail. Writing into someone's storage layer is opted into, never inherited from
    # ticking "this is a cluster member".
    #
    # This one switch covers the WHOLE hyper-converged procedure, not just the flags:
    # stopping every guest cluster-wide first, then setting the flags (see
    # cluster._prepare). With Ceph the guest stop is not optional but the first step of
    # the official procedure, so this switch always includes it — whatever
    # cluster_guest_stop says. A Ceph tick that could be had without the guest stop would
    # be a trap: the cluster hangs during the power cut.
    cluster_ceph: bool = False
    # The same cluster-wide guest stop for a cluster WITHOUT Ceph (#43): every guest is
    # asked to shut down at once, in parallel, before the first node goes down — instead
    # of each node working through its own guests in reverse startup order as it powers
    # off, which with many order groups can take longer than the battery lasts. Opt-in
    # and off by default: it deliberately ignores the configured startup order, and
    # guests that do not stop in time are forced off (cluster_guest_force_after_s).
    # Effective value: cluster_ceph or cluster_guest_stop (see Engine._cluster_switches).
    cluster_guest_stop: bool = False
    # Kept even where the endpoint is missing (PVE < 9.2): the value is checked against
    # runtime feature detection, so an upgrade to 9.2 activates it without the user
    # having to notice and re-tick anything.
    cluster_ha_disarm: bool = True


class PbsHostConfig(HostConfig):
    """A Proxmox Backup Server instance (default API port 8007)."""

    type: Literal["pbs"] = "pbs"

    @property
    def api_node(self) -> str:
        # PBS ignores the {node} path segment — its router matches any value and the
        # handler does not even take it — and its own web UI calls /nodes/localhost/…
        # So ``name`` is a free label here and the path stays constant, which removes
        # the "wrong node name" guessing that made PBS look unsupported.
        return "localhost"


# Discriminated union of every supported shutdown target. Adding a type means: a model
# here, a branch in app/targets.py, an entry in HostType and the matching i18n keys.
ShutdownTarget = Annotated[Union[PveHostConfig, PbsHostConfig], Field(discriminator="type")]

TARGET_MODELS: dict[str, type[HostConfig]] = {
    "pve": PveHostConfig,
    "pbs": PbsHostConfig,
}


class ApplianceConfig(BaseModel):
    """Where this appliance itself runs, so a cluster-wide guest shutdown skips it.

    One appliance runs in one guest, so this is a single top-level block rather than a
    per-host field: with three cluster members the same fact would otherwise be entered
    three times and could be entered inconsistently. It is not a per-*cluster* setting
    either — there is no cluster config object by design (members are grouped at runtime
    by the name the API reports).

    ``self_vmid`` is picked from a list in the UI (POST /api/cluster/guests), never typed:
    a mistyped id means the appliance shuts *itself* down in the middle of an outage.
    ``self_node`` is written along with that choice and disambiguates two guests sharing
    a name. Both empty means "not picked yet" — the guest stop then refuses rather than
    guessing (see engine._prepare_clusters).
    """

    self_vmid: Optional[int] = None
    self_node: str = ""
    # Explicit opt-out for an appliance that genuinely is not a guest of a managed
    # cluster (Docker on a NAS, bare metal). Deliberately NOT inferred from
    # PVE_USV_DEPLOYMENT=docker: a Docker container can perfectly well run inside a VM
    # of the very cluster being shut down.
    self_external: bool = False


class Thresholds(BaseModel):
    """Shutdown triggers. Any condition that is met (and enabled) fires the shutdown."""

    on_battery_seconds: Optional[int] = 600  # on battery longer than this
    runtime_below_minutes: Optional[int] = 10  # estimated runtime under this
    charge_below_percent: Optional[int] = 30  # battery charge under this
    on_battery_low: bool = True  # UPS reports batteryLow/Depleted

    poll_interval_normal_s: int = 30
    poll_interval_battery_s: int = 8

    # If the UPS is unreachable, do NOT shut down (fail safe, not fail shutdown).
    # We only raise an alarm. This is intentional and stays the default.
    unreachable_alarm_after_polls: int = 3

    # OPT-IN override of the fail-safe: if set, a *pure* communication loss (SNMP
    # unreachable) for this many minutes triggers a shutdown anyway. None = off
    # (recommended default); only set this when a comms loss must be treated like
    # an outage. The power state is unknown while unreachable — use with care.
    comm_loss_shutdown_after_min: Optional[int] = None

    # If communication is lost *while already on battery* (a confirmed outage, e.g. a
    # switch between us and the UPS just lost power), do NOT abort the shutdown: keep the
    # on_battery_seconds countdown running on our own clock and fire when it elapses. A
    # pure comms loss on mains stays fail safe (alarm only). Only effective when
    # on_battery_seconds is set (default 600 s) — runtime/charge are unreadable while blind.
    keep_shutdown_on_comm_loss: bool = True

    # Seconds to wait for a guest/node shutdown to be accepted before moving on.
    host_shutdown_timeout_s: int = 60

    # How long mains have to be back — every UPS reachable, on mains and no longer
    # triggered — before the appliance releases the shutdown latches by itself and is
    # ready for the next outage. Without it the latches only ever came off through the
    # manual "Reset state" button or a restart of the service, so a second outage found
    # every host still flagged as fired and shut down nothing. The delay exists because
    # mains coming back is not the same as mains staying: a grid that flickers would
    # otherwise re-arm between two dips. None = never, i.e. manual only (the pre-4.0
    # behaviour). Restoring a prepared cluster stays manual either way.
    rearm_after_mains_min: Optional[int] = 5

    # Total budget for the cluster preparation (Ceph flags + HA disarm) per cluster.
    # It runs while the battery drains, so it is a hard ceiling, not a per-call timeout —
    # but it IS the budget the HA disarm waits out (see cluster._prepare), so a value
    # under half a minute mostly buys a failed verification: CRM and every LRM work in
    # rounds of ten seconds, and the disarm is only done once the last watchdog is
    # released. The nodes of that cluster wait for this, so it is time off the battery.
    cluster_prep_timeout_s: int = 60

    # What to do when the preparation fails or times out. Default: shut down anyway.
    # A failed disarm leaves the LRM armed, and an armed LRM still stops the guests
    # itself — degraded (HA churn on the other nodes) but safe. Aborting instead would
    # mean letting the cluster lose power uncontrolled when the battery runs out.
    cluster_abort_on_prep_failure: bool = False

    # Budget for the cluster-wide guest shutdown, which runs between the HA disarm and
    # the Ceph flags. Deliberately its own number and NOT a share of the one above: that
    # one is documented as the time the HA disarm gets and is measured in CRM/LRM rounds
    # of ten seconds (a control-plane property, essentially constant), while this one is
    # measured against how long *this* estate takes to stop and scales with the number of
    # guests. Folding them together would silently reinterpret an existing setting and
    # start starving the disarm.
    cluster_guest_shutdown_timeout_s: int = 300

    # After how many seconds a guest that ignores the shutdown request is stopped hard.
    # None = never force, i.e. a hung guest makes the preparation fail and says so. The
    # value is also handed to Proxmox as the shutdown call's own timeout, so the kill
    # still happens if we lose the network right after asking.
    cluster_guest_force_after_s: Optional[int] = 120

    _corrections: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _repair_ranges(self) -> "Thresholds":
        # Bounded at BOTH ends. The lower bounds are the older half and the obvious one: a
        # poll interval <= 0 makes asyncio.wait_for() in the engine loop return
        # immediately, which is a hot loop hammering the UPS on a core of its own.
        #
        # The upper bounds close the mirror image, which is the same class of fault and
        # just as reachable by a slipped digit. Every timeout here is awaited inside the
        # poll loop, so "6000" where "60" was meant does not misconfigure a detail — it
        # parks the decision engine for 100 minutes per shutdown stage while the battery
        # drains. The ceilings are set well above any sane value, so they only ever catch
        # a typo; anything a real estate needs still passes untouched.
        _repair(self, "poll_interval_normal_s", lo=1, hi=3600)
        _repair(self, "poll_interval_battery_s", lo=1, hi=300)
        _repair(self, "on_battery_seconds", lo=1, hi=86400)
        _repair(self, "runtime_below_minutes", lo=1, hi=1440)
        _repair(self, "charge_below_percent", lo=0, hi=100)
        _repair(self, "unreachable_alarm_after_polls", lo=1, hi=1000)
        _repair(self, "comm_loss_shutdown_after_min", lo=1, hi=1440)
        _repair(self, "host_shutdown_timeout_s", lo=1, hi=900)
        _repair(self, "rearm_after_mains_min", lo=0, hi=1440)
        _repair(self, "cluster_prep_timeout_s", lo=5, hi=1800)
        _repair(self, "cluster_guest_shutdown_timeout_s", lo=5, hi=3600)
        _repair(self, "cluster_guest_force_after_s", lo=1, hi=3600)
        return self


class WebhookFormat(str, Enum):
    """Shape of the payload the webhook POSTs (see app/notify.py: FORMATTERS).

    Every member needs matching ``whfmt.<value>`` and ``whfmt.<value>Help`` i18n keys in
    en.js *and* de.js (enforced by tests/test_i18n.py).
    """

    json = "json"  # {"subject", "body", "severity", "status"} — the full status snapshot
    teams = "teams"  # Microsoft Teams adaptive card (Workflows / incoming webhook)
    text = "text"  # human-readable plain text, sent as text/plain
    slack = "slack"  # Slack incoming webhook (attachment with a colour bar)
    discord = "discord"  # Discord webhook (embed with fields)
    ntfy = "ntfy"  # ntfy.sh: plain body plus Title/Priority/Tags headers
    custom = "custom"  # user-defined body with {{placeholder}} substitution


class WebhookLevel(str, Enum):
    """Lowest event severity that still fires the webhook. Values match app/db.py.

    Every member needs a matching ``whlvl.<value>`` i18n key in en.js *and* de.js
    (enforced by tests/test_i18n.py).
    """

    info = "info"  # everything, including routine notices ("mains power restored")
    warning = "warning"  # default: warnings and critical events
    critical = "critical"  # only what needs an immediate reaction


# --- extra webhook headers --------------------------------------------------
# An RFC 7230 field name, and a value restricted to printable ASCII plus tab. Anything
# outside these is dropped rather than escaped: the only reason a CR or LF reaches here is
# a paste accident or an injection attempt, and neither deserves a best-effort rescue.
#
# Narrower than the RFC on purpose: obs-text (\x80-\xff) is legal on the wire, but httpx
# encodes header values as ASCII and raises. Accepting a byte we cannot send would store a
# webhook that saves cleanly and then fails every delivery with a codec error — exactly the
# silently dead notification path the rest of this module is built against.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HEADER_VALUE_RE = re.compile(r"^[\t\x20-\x7e]*$")

# Headers httpx owns. Letting the user set these does not configure the request, it
# corrupts it — a hand-written Content-Length desynchronises the body, and Host/Connection/
# Transfer-Encoding belong to the connection rather than the message.
_HEADER_DENYLIST = frozenset({"host", "content-length", "transfer-encoding", "connection"})

MAX_EXTRA_HEADERS = 20
MAX_HEADER_NAME_LEN = 128
MAX_HEADER_VALUE_LEN = 1024


def parse_extra_headers(value) -> dict[str, str]:
    """Normalise the ``extra_headers`` field to a clean name -> value mapping.

    Accepts both shapes that reach it: a mapping (config.yaml, a backup import) and the
    UI's textarea, which sends one ``Name: Value`` line per header. Follows the same rule
    as every other validator in this file — it **never raises**. A malformed line is
    dropped, because a webhook header in the wrong shape is fixable from the UI while a
    refused config import is not.
    """
    if isinstance(value, str):
        items = []
        for line in value.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue  # a comment or a line that simply is not a header
            name, _, val = line.partition(":")
            items.append((name, val))
    elif isinstance(value, dict):
        items = list(value.items())
    else:
        return {}

    out: dict[str, str] = {}
    for name, val in items:
        if len(out) >= MAX_EXTRA_HEADERS:
            break
        name = str(name).strip()
        val = str("" if val is None else val).strip()
        if not name or len(name) > MAX_HEADER_NAME_LEN or len(val) > MAX_HEADER_VALUE_LEN:
            continue
        if not _HEADER_NAME_RE.match(name) or not _HEADER_VALUE_RE.match(val):
            continue
        if name.lower() in _HEADER_DENYLIST:
            continue
        out[name] = val
    return out


class WebhookConfig(BaseModel):
    # Identity, mirroring UpsBase: ``id`` is a stable slug (auto-filled on save), ``name``
    # the label shown in the UI. Several webhooks may point at different chat systems.
    id: str = ""
    name: str = ""

    enabled: bool = False
    url: str = ""
    # POSTs a rendered notification on each notable event that passes ``min_severity``.
    format: WebhookFormat = WebhookFormat.json
    min_severity: WebhookLevel = WebhookLevel.warning

    # ``custom`` format only: the body to send, with {{placeholder}} substitution (see
    # app/notify.py: _render_custom). Deliberately NOT an expression language — plain
    # replacement covers the target systems without putting a template engine on the
    # notification path.
    template: str = ""
    content_type: str = "application/json"

    # Optional single auth header (e.g. "Authorization: Bearer …" for ntfy, or an API-key
    # header). One *named* header rather than a free-form map, because this is the field
    # that carries a secret: a fixed name keeps the value on the proven masked-secret
    # path below. Everything else the target wants goes in ``extra_headers``, which is
    # free-form precisely because nothing in it is confidential.
    auth_header_name: str = ""
    auth_header_value: SecretStr = SecretStr("")

    # Additional headers the target insists on, beyond the one above. Nextcloud Talk is the
    # case that asked for it (#34): it needs "OCS-APIRequest: true" and an Accept header
    # alongside the Basic auth, and neither of those two is a secret.
    #
    # Deliberately NOT on the masked path, and that is the division of labour: the one
    # named header above stays the place for credentials — masked in /api/config, carried
    # across a save by _reconcile_webhook_secrets — while these are stored and shown in
    # plain text. The UI help says so; putting a token here would weaken the field next
    # door rather than extend it. notify.send_webhook() applies the auth header last for
    # the same reason, so an entry here can never shadow it.
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("extra_headers", mode="before")
    @classmethod
    def _normalise_extra_headers(cls, value):
        """Drop anything malformed; never reject the import (see parse_extra_headers)."""
        return parse_extra_headers(value)

    @property
    def label(self) -> str:
        """Display label, falling back to the id and finally the format."""
        return self.name or self.id or getattr(self.format, "value", self.format) or "Webhook"

    @classmethod
    def secret_fields(cls) -> dict[str, str]:
        """Secret field names -> default value, for the API's masked-secret reconcile."""
        return {"auth_header_value": ""}

    @field_validator("format", mode="before")
    @classmethod
    def _normalise_format(cls, value):
        """Fall back to the plain JSON payload instead of rejecting an unknown format.

        Same rule as the self-test validators below: a backup from another version must
        still import — a webhook in the wrong shape is fixable, a refused import is not.
        """
        try:
            return WebhookFormat(value)
        except (ValueError, TypeError):
            return WebhookFormat.json

    @field_validator("min_severity", mode="before")
    @classmethod
    def _normalise_min_severity(cls, value):
        """Unknown level -> the default; never reject the import."""
        try:
            return WebhookLevel(value)
        except (ValueError, TypeError):
            return WebhookLevel.warning


class Notifications(BaseModel):
    # Legacy configs (< 3.0.0) may still contain a ``smtp`` block; Pydantic ignores
    # unknown keys, so it is dropped silently on load and gone after the next save.
    webhooks: list[WebhookConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_webhook_list(cls, data):
        """Turn the pre-4.0 single ``webhook: {...}`` block into a one-element list.

        Same rule as the UPS/host migrations: an existing config.yaml, a backup import and
        the form POST of a stale, cached app.js all pass through here, so none of them may
        be refused over a key whose shape simply changed. A malformed block is dropped
        rather than rejected — losing one webhook is fixable, a refused import is not.
        """
        if not isinstance(data, dict):
            return data
        legacy = data.pop("webhook", None)
        if legacy is not None and not data.get("webhooks"):
            if isinstance(legacy, BaseModel):
                legacy = legacy.model_dump()
            if isinstance(legacy, dict):
                legacy = dict(legacy)
                legacy.setdefault("id", "webhook1")
                data["webhooks"] = [legacy]
        return data


class HistoryConfig(BaseModel):
    """The per-UPS history behind the History tab (#37): runtime, load, charge and power
    source over time, kept in its own SQLite file next to the event log (app/history.py).

    Switching it off stops both the recording and the tab — for whoever would rather not
    spend the disk space or the write load (an SD card, say). What was recorded stays until
    it ages out or is cleared.
    """

    # On for a new installation. An existing configuration without this section gets it
    # OFF instead (see migrate_history_default): an update must not start writing to a disk
    # nobody sized for it.
    enabled: bool = True
    # How long samples are kept. Free in days; about 1,440 rows per UPS and day, i.e. a
    # few MB per UPS and quarter.
    retention_days: int = 90

    _corrections: list[str] = PrivateAttr(default_factory=list)

    @model_validator(mode="after")
    def _repair_ranges(self) -> "HistoryConfig":
        _repair(self, "retention_days", lo=1, hi=365)
        return self


def migrate_history_default(data):
    """Give a configuration written before the history existed ``history.enabled: false``.

    Applied where an EXISTING configuration comes in — load_config() and the backup import —
    and nowhere else: a fresh installation has no file, gets AppConfig() and with it the
    default (on). Kept out of a model validator on purpose, so building an AppConfig in code
    keeps the documented default instead of depending on which keys happened to be passed.
    """
    if isinstance(data, dict) and "history" not in data:
        data["history"] = {"enabled": False}
    return data


class AppConfig(BaseModel):
    # Marks whether the setup wizard has been completed at least once.
    configured: bool = False

    # Master safety switch: when True the engine only logs, never shuts anything down.
    dry_run: bool = True

    ups: list[UpsSource] = Field(default_factory=list)
    hosts: list[ShutdownTarget] = Field(default_factory=list)
    thresholds: Thresholds = Thresholds()
    notifications: Notifications = Notifications()
    # Where the appliance itself runs. A config written before this existed validates to
    # the defaults, so no migration validator is needed.
    appliance: ApplianceConfig = ApplianceConfig()
    # Per-UPS history (#37). See HistoryConfig for why an older config gets it off.
    history: HistoryConfig = HistoryConfig()

    # Scheduled self-test: verify the API token + power-management privilege still work,
    # so a broken/expired credential is caught long before a real outage needs it.
    selftest_enabled: bool = True
    selftest_hour: int = 9  # anchor: hour of day (0-23, server local time)
    selftest_interval_min: int = 1440  # repeat every N minutes from the anchor; 1440 = daily
    # Whether a PASSING self-test writes its quiet line to the event log. Off means the log
    # only ever mentions the check when it fails — the answer to "the daily ok line is noise
    # in a small estate" that does NOT reduce how often the credentials are verified, which
    # is the whole point of the check (a token can break any day, firewall rules included).
    # Never touches the failure path, journald, or an explicitly requested run; see
    # engine._run_selftest(), where this narrows ``log_ok`` but deliberately not the
    # standing dry-run notice that shares the same daily cadence.
    selftest_log_ok: bool = True

    # Optional NTP server pushed to the container's systemd-timesyncd (empty = leave
    # the system default untouched). Applied by the privileged deploy agent.
    ntp_server: str = ""

    # Optional IANA timezone (e.g. "Europe/Berlin") applied to the container by the
    # privileged deploy agent (empty = leave the system default, usually UTC, untouched).
    # Matters because the self-test schedule is interpreted in the container's local time.
    timezone: str = ""

    # Web UI auth. Read-only endpoints (/api/status, /api/health) are NOT protected.
    ui_password_hash: str = ""
    session_secret: str = Field(default_factory=lambda: secrets.token_hex(32))

    @model_validator(mode="before")
    @classmethod
    def _migrate_ups_list(cls, data):
        """Bring older UPS schemas up to the current ``ups: [{type: ..., ...}]`` shape.

        Two steps, both required for an existing ``config.yaml`` to keep working:

        * pre-2.0 had a single ``snmp: {...}`` block and hosts without ``ups_ids`` — it
          is wrapped into one UPS ``id="ups1"`` that every host is pointed at;
        * pre-3.2 UPS entries have no ``type`` at all, and the discriminated union would
          reject them. SNMP was the only source back then, so that is the default.

        An import must never be refused (same rule as the self-test validators below).
        """
        if not isinstance(data, dict):
            return data
        legacy = data.pop("snmp", None)
        if legacy is not None and not data.get("ups"):
            if not isinstance(legacy, dict):  # a SnmpConfig instance
                legacy = legacy.model_dump() if isinstance(legacy, BaseModel) else None
            if isinstance(legacy, dict):
                legacy = dict(legacy)
                legacy.setdefault("id", "ups1")
                legacy.setdefault("name", "UPS")
                data["ups"] = [legacy]
                for host in data.get("hosts", []) or []:
                    if isinstance(host, dict) and not host.get("ups_ids"):
                        host["ups_ids"] = ["ups1"]
        ups = data.get("ups")
        if isinstance(ups, list):
            data["ups"] = [
                {**u, "type": u.get("type") or "snmp"} if isinstance(u, dict) else u for u in ups
            ]
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_host_list(cls, data):
        """Give pre-3.5 hosts the ``type`` the discriminated union needs.

        Proxmox VE was the only shutdown target back then, so a host without a type is
        a PVE node. This runs for every entry point that builds a config — the stored
        config.yaml, a backup import, and the form POST of a stale, cached app.js — so
        none of them can be refused over a key that simply did not exist yet.

        Only a missing/empty type is filled in. An unknown one is left alone and the
        union rejects it: mistaking one product for another would send the wrong auth
        header to a real machine, which is worse than a clear error.
        """
        if not isinstance(data, dict):
            return data
        hosts = data.get("hosts")
        if isinstance(hosts, list):
            data["hosts"] = [
                {**h, "type": h.get("type") or "pve"} if isinstance(h, dict) else h for h in hosts
            ]
        return data

    @field_validator("selftest_interval_min", mode="before")
    @classmethod
    def _normalise_selftest_interval(cls, value):
        """Snap an unsupported interval to the daily default instead of rejecting it.

        A backup from another version or a hand-edited YAML must still import: a
        self-test running on the wrong cadence is cosmetic, a refused import is not.
        """
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return 1440
        return minutes if minutes in SELFTEST_INTERVALS else 1440

    @field_validator("selftest_hour", mode="before")
    @classmethod
    def _clamp_selftest_hour(cls, value):
        """Clamp to 0-23 — an out-of-range hour must not break the import either."""
        try:
            return min(23, max(0, int(value)))
        except (TypeError, ValueError):
            return 9

    def effective_thresholds(self, ups: UpsBase) -> Thresholds:
        """Global thresholds with this UPS's non-None overrides applied."""
        ov = ups.overrides
        merged = self.thresholds.model_copy()
        for field in (
            "on_battery_seconds",
            "runtime_below_minutes",
            "charge_below_percent",
            "on_battery_low",
            "comm_loss_shutdown_after_min",
            "keep_shutdown_on_comm_loss",
        ):
            val = getattr(ov, field)
            if val is not None:
                setattr(merged, field, val)
        return merged

    def ups_by_id(self, ups_id: str) -> Optional[UpsBase]:
        for u in self.ups:
            if u.id == ups_id:
                return u
        return None

    def feed_ids_for(self, host: HostConfig) -> list[str]:
        """UPS ids feeding a host; empty ups_ids means "all configured UPS"."""
        if host.ups_ids:
            return [i for i in host.ups_ids if self.ups_by_id(i) is not None]
        return [u.id for u in self.ups]

    def value_corrections(self) -> list[str]:
        """Every out-of-range number this config carried, as "where: what happened".

        Collected rather than raised (see _repair): the engine reports these once at
        startup and after every save, so a corrected value is loud without a config that
        cannot be loaded at all.
        """
        out = [f"thresholds: {c}" for c in self.thresholds._corrections]
        out += [f"history: {c}" for c in self.history._corrections]
        for u in self.ups:
            out += [f"UPS {u.label}: {c}" for c in u._corrections]
            out += [f"UPS {u.label} override: {c}" for c in u.overrides._corrections]
        return out

    def stale_feed_ids(self, host: HostConfig) -> list[str]:
        """UPS ids this host names that no longer exist.

        Pure, like duplicate_api_urls(), so the self-test, the startup check and the API
        all describe the same problem in the same words.

        It matters because feed_ids_for() drops them silently. A host whose ids have ALL
        gone stale ends up with no feeds at all, and engine._host_trigger_reason() then
        never fires for it — that machine is simply never shut down, with nothing anywhere
        saying so. Losing only some of them is quieter still: with ups_policy "all" the
        redundancy requirement is satisfied by whatever happens to be left, so a host that
        was configured to wait for both of its feeds starts acting on one.
        """
        return [i for i in host.ups_ids if self.ups_by_id(i) is None]

    def incomplete_entries(self) -> list[tuple[str, str, str]]:
        """Enabled entries stored in a state in which they cannot do their job.

        ``(kind, label, what is missing)`` per finding, in the order the UI shows the
        cards. Pure, like stale_feed_ids() and duplicate_api_urls() next door, so the
        startup check, the self-test and the API all describe the same problem in the
        same words.

        It exists because the browser's own check (``incompleteCards()`` in app.js) is not
        reachable from the two paths that matter most: ``POST /api/config/import`` and a
        hand-edited ``config.yaml`` never pass a form. An enabled host stored without an
        API URL or a token looks complete on the dashboard and fails during the outage —
        and nothing else catches it in time, because targets.verify_node() answers
        "unverified" for an entry it cannot even address, which _node_name_ok() reads as
        *no verdict* rather than as a fault. The first real signal was the next scheduled
        self-test, up to a day away.

        Enabled entries only, exactly as in the UI: a disabled entry shuts nothing down
        (ordered_hosts() skips it), so none of its fields can fail during an outage, and
        an installation upgrading from a release that stored such entries happily must not
        be told its configuration is broken.
        """
        out: list[tuple[str, str, str]] = []
        for u in self.ups:
            missing = self.incomplete_ups(u)
            if missing:
                out.append(("UPS", u.label, missing))
        for h in self.hosts:
            missing = self.incomplete_host(h)
            if missing:
                out.append(("Host", h.name or h.id, missing))
        for w in self.notifications.webhooks:
            if w.enabled and not w.url.strip():
                out.append(("Webhook", w.label, "no URL"))
        return out

    @staticmethod
    def incomplete_ups(ups: UpsBase) -> str:
        """What keeps this UPS from being polled at all, or "" when nothing does.

        ``UpsBase.configured`` IS this question, per source type — an SNMP entry needs an
        address, a NUT entry an address and the section name from ups.conf. Asked through
        the property rather than restated here, so a new source type inherits the check by
        declaring what "pollable" means for it.
        """
        return "" if ups.configured else "no address to poll"

    @staticmethod
    def incomplete_host(host: HostConfig) -> str:
        """What keeps a shutdown from reaching this host, or "" when nothing does.

        Disabled entries always answer "": they shut nothing down (ordered_hosts() skips
        them), so none of their fields can fail during an outage.

        Its own method so ``snapshot()`` can ask about one host directly and get the same
        answer, in the same words, that the event log will use.
        """
        if not host.enabled:
            return ""
        missing = []
        if not host.api_url.strip():
            missing.append("no API URL")
        if not host.token_id.strip():
            missing.append("no API token ID")
        if not host.token_secret.get_secret_value():
            missing.append("no API token secret")
        return ", ".join(missing)

    def ordered_hosts(self) -> list[HostConfig]:
        """Enabled hosts in shutdown order; the appliance's own host always last."""
        active = [h for h in self.hosts if h.enabled]
        return sorted(active, key=lambda h: (h.this_host, h.order, h.name))

    def duplicate_api_urls(self) -> list[str]:
        """API URLs shared by more than one enabled target, normalised.

        Almost always a copy-paste slip (entry duplicated, IP not adjusted), and the one
        state in which the node name still decides where a shutdown lands — see
        api_url_is_unique(). Across all types on purpose: two Backup Servers on one URL is
        the same mistake as two PVE nodes.
        """
        seen: dict[str, int] = {}
        for host in self.hosts:
            if host.enabled:
                key = _norm_api_url(host.api_url)
                seen[key] = seen.get(key, 0) + 1
        return sorted(url for url, count in seen.items() if url and count > 1)

    def api_url_is_unique(self, host: HostConfig) -> bool:
        """Whether this entry is the only enabled target behind its API URL.

        This is what makes "the node behind this URL" unambiguous, and therefore what
        decides whether the shutdown may address /nodes/localhost instead of a configured
        name (see app/proxmox.py). Where a URL serves several entries, PVE's proxying is
        the only thing that tells them apart, so the name has to stay in the path.
        """
        return _norm_api_url(host.api_url) not in self.duplicate_api_urls()


def _norm_api_url(url: str) -> str:
    """Compare form of an API URL — case and a trailing slash are not a difference."""
    return (url or "").strip().rstrip("/").lower()


def _slugify(text: str, fallback: str = "ups") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or fallback


def _assign_unique_ids(items, base_for) -> None:
    """Give every entry a unique, stable id (in place).

    Two jobs, and the second one used to be missing. Filling an EMPTY id is the obvious
    one. Splitting a DUPLICATE is the one that matters: the id is the runtime key — the
    engine latches shutdowns on HostConfig.key (= the id) and keys one _UpsRuntime per
    UPS id — so two entries sharing an id share a latch. Two hosts then get one shutdown
    between them, and two UPS devices overwrite each other's reading in the same poll.
    Nothing else in the system deduplicates, and an imported or hand-edited file never
    passes the UI that would have kept them apart.

    The FIRST holder of an id keeps it, always. Anything else would move a stored secret
    (main._merge_config matches on the id) or a live latch to a different entry.
    """
    taken = {x.id for x in items if x.id}
    used: set[str] = set()
    for i, x in enumerate(items, start=1):
        if x.id and x.id not in used:
            used.add(x.id)
            continue
        base = x.id or base_for(x, i)
        candidate = base
        n = 2
        while candidate in taken or candidate in used or not candidate:
            candidate = f"{base}-{n}"
            n += 1
        x.id = candidate
        taken.add(candidate)
        used.add(candidate)


def assign_ups_ids(ups: list[UpsBase]) -> None:
    """Fill empty UPS ids and split duplicates with stable slugs (in place)."""
    _assign_unique_ids(ups, lambda u, i: _slugify(u.name) if u.name else f"ups{i}")


def assign_host_ids(hosts: list[HostConfig]) -> None:
    """Same job as assign_ups_ids, and needed for the same reason: the id is what the
    UI's per-card secret reconcile matches on, and what the engine latches shutdowns
    against — so it has to survive renaming, reordering, and two entries carrying the
    same name."""
    _assign_unique_ids(hosts, lambda h, i: _slugify(h.name, f"host{i}") if h.name
                       else f"host{i}")


def assign_webhook_ids(hooks: list[WebhookConfig]) -> None:
    """Same job again: the id is what the per-card secret reconcile matches on."""
    _assign_unique_ids(hooks, lambda h, i: _slugify(h.name) if h.name else f"webhook{i}")


def _to_serialisable(cfg: AppConfig) -> dict:
    """Dump the model to plain types: reveal SecretStr, unwrap Enums (so YAML works)."""

    def reveal(obj):
        if isinstance(obj, SecretStr):
            return obj.get_secret_value()
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, dict):
            return {k: reveal(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [reveal(v) for v in obj]
        return obj

    return reveal(cfg.model_dump(mode="python"))


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    """Load config from disk, or return defaults if it does not exist yet."""
    if not path.exists():
        return AppConfig()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    # An existing file is an existing installation: no history section means it predates
    # the history, and an update leaves it off until someone switches it on.
    cfg = AppConfig.model_validate(migrate_history_default(data))
    # A config written before hosts had ids gets them here, so the runtime keys and the
    # ids the UI sends back are id-based from the first request on. Nothing is written to
    # disk until the next save, which is enough: the assignment is deterministic.
    #
    # UPS ids for the same reason, and because an empty one is worse than a wrong one: the
    # engine keys its per-UPS runtime on the id, so a hand-written entry without one would
    # be polled and its answer then dropped — the device would simply never trigger.
    assign_ups_ids(cfg.ups)
    assign_host_ids(cfg.hosts)
    # Webhooks for the third time, and it was the one missing here. The id keys the
    # per-target delivery state (notify.DELIVERY) and the masked-secret reconcile, so two
    # entries without one — a hand-written file, a backup from a foreign version — share
    # a single record: each overwrites the other's "last delivery", and the settings page
    # offers one of them the other's auth header.
    assign_webhook_ids(cfg.notifications.webhooks)
    return cfg


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    """Persist config to a single YAML file with 0600 permissions, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = _to_serialisable(cfg)
    # Created 0600 from the start. chmod *after* writing, which is what this did, left a
    # file containing every plaintext secret at the default 0644 for the length of the
    # write. Deliberately NOT O_EXCL: a .tmp left behind by an earlier crash would then
    # block every future save for good.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # The mode argument above only applies when open() CREATES the file, so the crash
    # leftover the missing O_EXCL deliberately allows would be reused with whatever
    # permissions it already had — 0644, if it predates this code. fchmod on the open
    # descriptor covers both cases and cannot be raced through the path.
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):  # noqa: BLE001 - not on every platform (Windows dev box)
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
        fh.flush()
        # On an appliance whose whole purpose is surviving a power cut, "the rename landed
        # but the bytes never did" is the one outcome that must not be possible.
        os.fsync(fh.fileno())
    os.replace(tmp, path)

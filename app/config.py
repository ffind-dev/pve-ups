"""Configuration model and persistence.

The entire appliance is configured through a *single* YAML file, written exclusively
by the web UI. No hand-editing of multiple config files (that is the whole point of
this project vs. NUT). Secrets live in the same file, which is created with 0600
permissions.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import os
import secrets
import re
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator

# Location can be overridden for tests / local runs.
CONFIG_PATH = Path(os.environ.get("PVE_USV_CONFIG", "/etc/pve-usv/config.yaml"))


class SnmpVersion(str, Enum):
    v1 = "v1"
    v2c = "v2c"
    v3 = "v3"


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


class SnmpConfig(BaseModel):
    # Identity (multi-UPS): ``id`` is a stable slug referenced by hosts, ``name`` is the
    # human label shown in the UI. ``id`` is auto-filled on save if left empty.
    id: str = ""
    name: str = ""

    host: str = ""
    port: int = 161
    version: SnmpVersion = SnmpVersion.v2c
    timeout_s: float = 3.0
    retries: int = 1

    # v1/v2c
    community: SecretStr = SecretStr("public")

    # v3
    v3_user: str = ""
    v3_auth_proto: SnmpAuthProto = SnmpAuthProto.sha
    v3_auth_pass: SecretStr = SecretStr("")
    v3_priv_proto: SnmpPrivProto = SnmpPrivProto.aes
    v3_priv_pass: SecretStr = SecretStr("")

    # Optional per-UPS threshold override (None fields inherit the global thresholds).
    overrides: UpsThresholdOverride = UpsThresholdOverride()

    @property
    def configured(self) -> bool:
        return bool(self.host)

    @property
    def label(self) -> str:
        """Display label, falling back to id/host when no name is set."""
        return self.name or self.id or self.host or "UPS"


class ShutdownMethod(str, Enum):
    api_token = "api_token"


class HostConfig(BaseModel):
    name: str  # Proxmox node name, e.g. "pve01"
    api_url: str  # e.g. "https://10.0.0.10:8006"
    method: ShutdownMethod = ShutdownMethod.api_token
    # API token: user@realm!tokenid + secret
    token_id: str = ""  # "ups@pve!shutdown"
    token_secret: SecretStr = SecretStr("")
    verify_tls: bool = False  # PVE ships self-signed certs by default
    this_host: bool = False  # the host carrying this appliance -> shut down last
    order: int = 0  # ascending; this_host is forced last regardless
    enabled: bool = True

    # Multi-UPS: which UPS devices feed this host (by SnmpConfig.id). Empty = depends
    # on ALL configured UPS (conservative fallback). ``ups_policy`` decides how the
    # feeds combine: "all" = shut down only when every feed has triggered (redundant
    # PSUs, default), "any" = shut down as soon as one feed triggers (split, non-
    # redundant load).
    ups_ids: list[str] = Field(default_factory=list)
    ups_policy: Literal["all", "any"] = "all"


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


class WebhookConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    # POSTs the /api/status payload as JSON on each notable event.


class Notifications(BaseModel):
    # Legacy configs (< 3.0.0) may still contain a ``smtp`` block; Pydantic ignores
    # unknown keys, so it is dropped silently on load and gone after the next save.
    webhook: WebhookConfig = WebhookConfig()


class AppConfig(BaseModel):
    # Marks whether the setup wizard has been completed at least once.
    configured: bool = False

    # Master safety switch: when True the engine only logs, never shuts anything down.
    dry_run: bool = True

    ups: list[SnmpConfig] = Field(default_factory=list)
    hosts: list[HostConfig] = Field(default_factory=list)
    thresholds: Thresholds = Thresholds()
    notifications: Notifications = Notifications()

    # Daily self-test: verify the Proxmox API token + Sys.PowerMgmt still work, so a
    # broken/expired credential is caught long before a real outage needs it.
    selftest_enabled: bool = True
    selftest_hour: int = 9  # hour of day (0-23, server local time) to run the test

    # Optional NTP server pushed to the container's systemd-timesyncd (empty = leave
    # the system default untouched). Applied by the privileged deploy agent.
    ntp_server: str = ""

    # Optional IANA timezone (e.g. "Europe/Berlin") applied to the container by the
    # privileged deploy agent (empty = leave the system default, usually UTC, untouched).
    # Matters because selftest_hour is interpreted in the container's local time.
    timezone: str = ""

    # Web UI auth. Read-only endpoints (/api/status, /api/health) are NOT protected.
    ui_password_hash: str = ""
    session_secret: str = Field(default_factory=lambda: secrets.token_hex(32))

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_snmp(cls, data):
        """Migrate the pre-2.0 single-UPS schema (``snmp: {...}``) to ``ups: [...]``.

        Old config files have one ``snmp`` block and hosts without ``ups_ids``. We wrap
        that block into a single UPS ``id="ups1"`` and point every host at it, so an
        existing ``config.yaml`` keeps working unchanged across the 2.0 upgrade.
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
        return data

    def effective_thresholds(self, ups: SnmpConfig) -> Thresholds:
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

    def ups_by_id(self, ups_id: str) -> Optional[SnmpConfig]:
        for u in self.ups:
            if u.id == ups_id:
                return u
        return None

    def feed_ids_for(self, host: HostConfig) -> list[str]:
        """UPS ids feeding a host; empty ups_ids means "all configured UPS"."""
        if host.ups_ids:
            return [i for i in host.ups_ids if self.ups_by_id(i) is not None]
        return [u.id for u in self.ups]

    def ordered_hosts(self) -> list[HostConfig]:
        """Enabled hosts in shutdown order; the appliance's own host always last."""
        active = [h for h in self.hosts if h.enabled]
        return sorted(active, key=lambda h: (h.this_host, h.order, h.name))


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "ups"


def assign_ups_ids(ups: list[SnmpConfig]) -> None:
    """Fill empty UPS ids with stable, collision-free slugs (in place)."""
    taken = {u.id for u in ups if u.id}
    for i, u in enumerate(ups, start=1):
        if u.id:
            continue
        base = _slugify(u.name) if u.name else f"ups{i}"
        candidate = base
        n = 2
        while candidate in taken or candidate == "":
            candidate = f"{base}-{n}"
            n += 1
        u.id = candidate
        taken.add(candidate)


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
    return AppConfig.model_validate(data)


def save_config(cfg: AppConfig, path: Path = CONFIG_PATH) -> None:
    """Persist config to a single YAML file with 0600 permissions, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = _to_serialisable(cfg)
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

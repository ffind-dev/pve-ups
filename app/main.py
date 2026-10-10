"""FastAPI application: REST status (public) + config wizard (authenticated).

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tarfile
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from passlib.context import CryptContext
from pydantic import BaseModel

from . import __version__, db
from .config import (
    TARGET_MODELS,
    UPS_SOURCE_MODELS,
    ApplianceConfig,
    AppConfig,
    HostConfig,
    PveHostConfig,
    SnmpConfig,
    UpsBase,
    WebhookConfig,
    _to_serialisable,
    assign_host_ids,
    assign_ups_ids,
    assign_webhook_ids,
    load_config,
    migrate_history_default,
    save_config,
)
from .engine import Engine, _hostname
# ``cluster`` and ``proxmox`` are PVE-specific on purpose: both back wizard checks
# that only exist for a Proxmox VE node. Shutdowns still go through ``targets``.
from . import cluster, history, notify, proxmox, sources, targets

log = logging.getLogger("pve-usv")

WEB_DIR = Path(__file__).parent / "web"
SECRET_PLACEHOLDER = "**********"  # pydantic SecretStr json mask; means "unchanged"
SESSION_COOKIE = "pve_usv_session"
SESSION_MAX_AGE = 8 * 3600

# Deployment mode: "lxc" (default, privileged agent handles NTP/timezone/updates) or
# "docker" (no agent present; those features are disabled and surfaced to the UI).
DEPLOYMENT = os.environ.get("PVE_USV_DEPLOYMENT", "lxc").strip().lower()
IS_DOCKER = DEPLOYMENT == "docker"

# State dir layout (shared with the privileged deploy agent, see deploy/pve-usv-agent.*).
STATE_DIR = db.DB_PATH.parent
AGENT_DIR = STATE_DIR / "agent"
AGENT_QUEUE = AGENT_DIR / "queue"
AGENT_RESULT = AGENT_DIR / "result.json"
AGENT_SEEN = AGENT_DIR / "result.seen"  # job_id of the last result already logged
AGENT_LAST_JOB = AGENT_DIR / "last_job"  # job_id of the most recent upload (for the UI)
AGENT_LOG = AGENT_DIR / "agent.log"
UPDATE_DIR = STATE_DIR / "updates"
AGENT_TIMER_UNIT = Path("/etc/systemd/system/pve-usv-agent.timer")

pwd_ctx = CryptContext(schemes=["argon2"], deprecated="auto")

# Single global engine instance, created on startup.
engine: Optional[Engine] = None


# --- session helpers --------------------------------------------------------
def _serializer(cfg: AppConfig) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(cfg.session_secret, salt="pve-usv-session")


def _is_authenticated(request: Request, cfg: AppConfig) -> bool:
    # Bootstrap: before a password is set the wizard is open so it can be set.
    if not cfg.ui_password_hash:
        return True
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        _serializer(cfg).loads(token, max_age=SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False


def require_auth(request: Request):
    assert engine is not None
    if not _is_authenticated(request, engine.cfg):
        raise HTTPException(status_code=401, detail="Authentication required")


# --- privileged deploy agent (update + NTP) ---------------------------------
def _enqueue_agent(action: str, **fields) -> str:
    """Drop a job for the root agent into the queue dir (the app stays unprivileged).

    The temp file is written OUTSIDE the watched queue dir and then atomically moved in.
    Writing the .tmp inside queue/ would already make the dir non-empty, so the systemd
    ``pve-usv-agent.path`` unit (DirectoryNotEmpty) could fire on the .tmp — which has no
    ``*.json`` match — and then never re-fire for the real file, silently dropping the job.
    Returns the job id so callers can correlate the result.
    """
    AGENT_QUEUE.mkdir(parents=True, exist_ok=True)
    job_id = f"{time.time_ns()}-{action}"
    req = {"job_id": job_id, "action": action, "ts": datetime.now(timezone.utc).isoformat(), **fields}
    tmp = AGENT_DIR / f".{job_id}.json.tmp"  # sibling of queue/, same filesystem
    tmp.write_text(json.dumps(req), encoding="utf-8")
    os.replace(tmp, AGENT_QUEUE / f"{job_id}.json")
    return job_id


def _agent_drainer_active() -> Optional[bool]:
    """Is the queue-drainer (pve-usv-agent.timer) running? Best-effort, never raises.

    Detects the one-time bootstrap gap where a box was updated INTO the first version that
    ships the timer by an OLD agent that never installed it: then queued jobs are only picked
    up by the fragile inotify ``.path`` unit and can hang silently. The UI uses this to show a
    recovery hint instead of a perpetual "in queue" message.

    Returns True/False on Linux, or None when undeterminable (e.g. the Windows dev box).
    """
    try:
        # Read-only query; allowed under the service hardening (no extra privilege needed).
        out = subprocess.run(
            ["systemctl", "is-active", "pve-usv-agent.timer"],
            capture_output=True, text=True, timeout=2,
        )
        state = out.stdout.strip()
        if state in ("active", "inactive", "failed", "activating", "deactivating"):
            return state == "active"
    except Exception:
        pass
    # Fallback: the unit file is missing exactly in the bootstrap case (old agent never
    # installed it). Presence alone can't prove it's enabled, but absence is a clear signal.
    try:
        return AGENT_TIMER_UNIT.exists()
    except Exception:
        return None


def _archive_names(path: Path) -> Optional[list[str]]:
    """Member names of an uploaded archive, or None if it is not a readable archive.

    Detection is by CONTENT, never by file name: Safari unpacks .tar.gz on download, so
    the very same release asset arrives as a plain .tar and an extension check would
    reject a perfectly good package (see the update card's accept list).
    """
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                return z.namelist()
        # tarfile.open() transparently handles gzip and uncompressed tar alike.
        with tarfile.open(path) as t:
            return t.getnames()
    except Exception as exc:  # noqa: BLE001 - an unreadable archive is a rejection, not a crash
        log.warning("Could not read archive %s: %s", path, exc)
        return None


def _read_member(path: Path, name: str) -> Optional[str]:
    """Read one member of an archive as text, by content-detected format."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                return z.read(name).decode("utf-8", "replace")
        with tarfile.open(path) as t:
            fh = t.extractfile(name)
            return fh.read().decode("utf-8", "replace") if fh else None
    except Exception as exc:  # noqa: BLE001 - best effort only
        log.warning("Could not read %s from %s: %s", name, path, exc)
        return None


def _inspect_package(path: Path) -> tuple[Optional[str], Optional[str]]:
    """Validate an uploaded update package and read its version.

    Returns ``(version, error)``. ``error`` is a user-facing English message when the
    file is not a usable package; the caller must then discard it. Validating here —
    rather than leaving it to the privileged agent — means a broken or unrelated upload
    never reaches the root path at all.
    """
    names = _archive_names(path)
    if names is None:
        return None, (
            "Not a readable .tar.gz/.tar/.zip archive. If the file came from Safari, "
            "the download may have been unpacked or truncated — re-download it with "
            "automatic unarchiving turned off."
        )

    # The package must look like this project: pyproject.toml next to an app/ package.
    # Both are what the agent later looks for, so checking them here fails fast.
    init_members = [n for n in names if n.endswith("app/__init__.py")]
    has_pyproject = any(n == "pyproject.toml" or n.endswith("/pyproject.toml") for n in names)
    if not init_members or not has_pyproject:
        return None, (
            "This archive does not look like a PVE-UPS release package "
            "(pyproject.toml and app/__init__.py are missing)."
        )

    version = None
    data = _read_member(path, min(init_members, key=len))
    if data:
        m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", data)
        if m:
            version = m.group(1)
    return version, None


def _ingest_agent_result() -> Optional[dict]:
    """Read the agent's last result and log it to the event log exactly once.

    Idempotent across restarts via the ``result.seen`` marker (the agent restarts the app
    after an update, so this also runs on the next startup and surfaces the outcome).
    """
    if not AGENT_RESULT.exists():
        return None
    try:
        result = json.loads(AGENT_RESULT.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read agent result: %s", exc)
        return None

    job_id = result.get("job_id")
    seen = AGENT_SEEN.read_text(encoding="utf-8").strip() if AGENT_SEEN.exists() else None
    if job_id and job_id != seen:
        ok = bool(result.get("ok"))
        vb, va = result.get("version_before"), result.get("version_after")
        change = f" ({vb} → {va})" if (vb or va) else ""
        db.log_event(
            "Update applied" if ok else "Update FAILED",
            f"{result.get('message', '')}{change}",
            db.INFO if ok else db.CRITICAL,
        )
        try:
            AGENT_SEEN.write_text(job_id, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not write agent seen-marker: %s", exc)
    return result


# --- lifespan ---------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    db.init_db()
    cfg = load_config()
    engine = Engine(cfg)
    engine.start()
    log.info("PVE-UPS %s started", __version__)
    # If we just restarted because of an applied update, surface its outcome now.
    try:
        _ingest_agent_result()
    except Exception as exc:  # noqa: BLE001
        log.warning("Ingesting agent result at startup failed: %s", exc)
    try:
        yield
    finally:
        if engine:
            await engine.stop()


app = FastAPI(title="PVE-UPS", version=__version__, lifespan=lifespan)


# --- public (read-only) endpoints ------------------------------------------
@app.get("/api/status")
async def api_status():
    """Full snapshot plus the event log of the last 48 h, so an external monitor can
    react to warnings/errors from this single (public, secret-free) endpoint."""
    assert engine is not None
    snap = engine.snapshot()
    try:
        snap["events"] = db.events_since(hours=48)
        snap["events_summary"] = db.severity_counts_since(hours=48)
    except Exception as exc:  # noqa: BLE001 - status must stay available even if the log read fails
        log.warning("Reading events for /api/status failed: %s", exc)
        snap["events"] = []
        snap["events_summary"] = {db.INFO: 0, db.WARNING: 0, db.CRITICAL: 0}
    return snap


@app.get("/api/health")
async def api_health():
    assert engine is not None
    snap = engine.snapshot()
    ok = engine._task is not None and not engine._task.done()
    ups_list = snap["ups"]
    host_list = snap["hosts"]
    # A shutdown target counts as ok once the self-test confirmed both its credentials
    # and its power-management privilege; hosts never tested yet count as neither.
    hosts_ok = sum(1 for h in host_list if h["credentials_ok"] and h["power_mgmt_ok"])
    # Node names that provably do not match the API behind them. Reported separately from
    # hosts_ok because the two differ in consequence: with one API URL per entry the
    # shutdown addresses the node directly and a wrong name is only a misleading label.
    hosts_node_ok = sum(
        1 for h in host_list if h.get("node_state") in (None, "ok", "unverified")
    )
    tested = [h for h in host_list if h["last_test_at"]]
    # Enabled targets only: a disabled webhook sends nothing, so it can never be "not ok".
    webhooks = [w for w in snap.get("webhooks", []) if w["enabled"]]
    payload = {
        "status": "ok" if ok else "degraded",
        "version": __version__,
        "engine_state": snap["appliance"]["engine_state"],
        # Monitoring information, under the same rule as hosts_ok/webhooks_ok below: it
        # never moves ``status`` or the HTTP code. Reported at all because an appliance
        # left in dry-run is the quietest failure there is — fully configured, self-test
        # green, "ok" here, and it shuts nothing down. The endpoint an external monitor
        # actually polls should be able to say so.
        "dry_run": snap["appliance"]["dry_run"],
        # True only when every configured UPS is reachable (all() of empty list is True).
        "ups_reachable": all(u["reachable"] for u in ups_list),
        "ups_reachable_count": sum(1 for u in ups_list if u["reachable"]),
        "ups_total": len(ups_list),
        # Shutdown targets (PVE + PBS). Reported for monitoring, but deliberately not
        # part of ``status``/the HTTP code: an expired token is a problem for the next
        # outage, not a reason to declare a running appliance down.
        "hosts_total": len(host_list),
        "hosts_ok": hosts_ok,
        "hosts_selftest_ok": (hosts_ok == len(host_list)) if tested else None,
        "hosts_node_ok": hosts_node_ok,
        "hosts_selftest_at": max((h["last_test_at"] for h in tested), default=None),
        # Notification targets, same caveat again. 4.1.0 made a webhook that stopped
        # working a first-class signal — an event, a dashboard row, a note on its card —
        # but the endpoint an external monitor actually polls knew nothing about it. A
        # target counts as ok until a send has provably failed, so one that has never been
        # tried does not read as broken.
        "webhooks_total": len(webhooks),
        "webhooks_ok": sum(1 for w in webhooks if w["last_delivery_ok"] is not False),
        # Cluster state, for the same reason and with the same caveat as hosts_ok above:
        # monitoring information only, never part of ``status`` or the HTTP code. Empty
        # unless at least one host has cluster preparation enabled.
        "clusters": [
            {
                "name": c["name"],
                "quorate": c["quorate"],
                "ha_armed": not c["ha_armed_state"] or c["ha_armed_state"] == "armed",
                "guests_running": c.get("guests_running"),
                "self_guest_on_ceph": c.get("self_guest_on_ceph"),
                "ceph_flags_clean": not c["ceph_flags_set"],
                "needs_recovery": c["needs_recovery"],
            }
            for c in snap.get("clusters", [])
        ],
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


# --- auth -------------------------------------------------------------------
class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
async def api_login(body: LoginBody, response: Response):
    assert engine is not None
    cfg = engine.cfg
    if not cfg.ui_password_hash:
        raise HTTPException(status_code=400, detail="No password set — run the setup first.")
    if not pwd_ctx.verify(body.password, cfg.ui_password_hash):
        raise HTTPException(status_code=401, detail="Wrong password")
    token = _serializer(cfg).dumps("ok")
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, httponly=True, samesite="lax"
    )
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/session")
async def api_session(request: Request):
    assert engine is not None
    return {
        "authenticated": _is_authenticated(request, engine.cfg),
        "password_set": bool(engine.cfg.ui_password_hash),
        "configured": engine.cfg.configured,
        "deployment": DEPLOYMENT,
    }


class PasswordBody(BaseModel):
    new_password: str
    current_password: Optional[str] = None


@app.post("/api/password")
async def api_password(body: PasswordBody, request: Request):
    assert engine is not None
    cfg = engine.cfg
    # If a password already exists, require the current one.
    if cfg.ui_password_hash:
        if not body.current_password or not pwd_ctx.verify(
            body.current_password, cfg.ui_password_hash
        ):
            raise HTTPException(status_code=401, detail="Current password is wrong")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password too short (min. 6 characters)")
    cfg.ui_password_hash = pwd_ctx.hash(body.new_password)
    save_config(cfg)
    engine.update_config(cfg)
    return {"ok": True}


# --- config (authenticated) -------------------------------------------------
def _sanitized_config(cfg: AppConfig) -> dict:
    data = cfg.model_dump(mode="json")  # SecretStr -> "**********"
    data.pop("session_secret", None)
    data.pop("ui_password_hash", None)
    return data


def _reconcile_secret(incoming, existing: str) -> str:
    if incoming in ("", None, SECRET_PLACEHOLDER):
        return existing
    return incoming


def _ups_model(ups_entry: dict) -> type[UpsBase]:
    """Model class for a submitted UPS entry; unknown/absent type means the legacy SNMP one."""
    return UPS_SOURCE_MODELS.get(str(ups_entry.get("type") or "snmp"), SnmpConfig)


def _reconcile_ups_secrets(ups_entry: dict, old: Optional[UpsBase]) -> None:
    """Carry over unchanged (masked) per-UPS secrets, in place.

    Which fields those are comes from the source model itself, so a new source type only
    has to declare ``secret_fields()`` to be handled correctly here.
    """
    model = _ups_model(ups_entry)
    for fld, default in model.secret_fields().items():
        # Only reuse the stored secret when the entry still is the same source type —
        # switching a UPS from SNMP to NUT must not inherit anything.
        keep = old is not None and isinstance(old, model)
        old_secret = getattr(old, fld).get_secret_value() if keep else default
        ups_entry[fld] = _reconcile_secret(ups_entry.get(fld), old_secret)


def _host_model(host_entry: dict) -> type[HostConfig]:
    """Model class for a submitted host entry; unknown/absent type means a PVE node."""
    return TARGET_MODELS.get(str(host_entry.get("type") or "pve"), PveHostConfig)


def _legacy_host_key(host_entry: dict) -> str:
    """The pre-id key of a submitted entry: type + node name."""
    return f"{host_entry.get('type') or 'pve'}:{host_entry.get('name')}"


def _find_host(host_entry: dict, by_id: dict, by_legacy: dict) -> Optional[HostConfig]:
    """The stored host a submitted entry refers to — by id, else by type + node name.

    The fallback is not cosmetic. The first save after this version arrives carries no ids
    at all (a cached older app.js, or a config whose ids were only just assigned on load),
    and matching on the id alone would drop every secret exactly once — the very bug the
    id was introduced to fix.
    """
    entry_id = str(host_entry.get("id") or "")
    if entry_id and entry_id in by_id:
        return by_id[entry_id]
    return by_legacy.get(_legacy_host_key(host_entry))


def _host_lookups(hosts: list) -> tuple[dict, dict]:
    """(by id, by legacy type:name) for the stored hosts."""
    return (
        {h.id: h for h in hosts if h.id},
        {f"{getattr(h, 'type', 'pve')}:{h.name}": h for h in hosts},
    )


def _reconcile_host_secrets(host_entry: dict, old: Optional[HostConfig]) -> None:
    """Carry over unchanged (masked) per-host secrets, in place."""
    model = _host_model(host_entry)
    for fld, default in model.secret_fields().items():
        # Only reuse the stored secret while the entry is still the same target type —
        # repointing a host from PVE to PBS must not inherit the old token.
        keep = old is not None and isinstance(old, model)
        old_secret = getattr(old, fld).get_secret_value() if keep else default
        host_entry[fld] = _reconcile_secret(host_entry.get(fld), old_secret)


def _reconcile_webhook_secrets(hook: dict, old: Optional[WebhookConfig]) -> None:
    """Carry a webhook's unchanged (masked) auth header over, matched by webhook id.

    The sibling of _reconcile_ups_secrets/_reconcile_host_secrets, and it exists because
    /api/test/webhook did not have one: the UI sends the placeholder whenever the field is
    left blank, so pressing "Test" on a saved webhook with an auth header sent the literal
    "**********" as the header value and reported a 401 for a configuration that works.
    """
    for fld, default in WebhookConfig.secret_fields().items():
        old_secret = getattr(old, fld).get_secret_value() if old is not None else default
        hook[fld] = _reconcile_secret(hook.get(fld), old_secret)


def _merge_config(incoming: dict, existing: AppConfig) -> AppConfig:
    """Build a new config, carrying over unchanged (masked) secrets."""
    data = dict(incoming)

    # Per-UPS secrets, matched by stable UPS id.
    existing_ups = {u.id: u for u in existing.ups}
    for ups_entry in data.get("ups", []) or []:
        _reconcile_ups_secrets(ups_entry, existing_ups.get(ups_entry.get("id")))
    data.pop("snmp", None)  # legacy key never accepted from the form

    # Host token secrets, matched by stable host id (with the pre-id fallback).
    by_id, by_legacy = _host_lookups(existing.hosts)
    for host in data.get("hosts", []) or []:
        _reconcile_host_secrets(host, _find_host(host, by_id, by_legacy))

    # Webhook auth-header secrets, matched by stable webhook id.
    existing_hooks = {h.id: h for h in existing.notifications.webhooks}
    for hook in (data.get("notifications") or {}).get("webhooks", []) or []:
        if not isinstance(hook, dict):
            continue
        _reconcile_webhook_secrets(hook, existing_hooks.get(hook.get("id")))

    # A form that does not know the history section (a cached app.js from before it
    # existed) keeps what is stored, instead of switching it to the default.
    data.setdefault("history", existing.history.model_dump())

    # Never overwrite auth/session material from the config form.
    data["ui_password_hash"] = existing.ui_password_hash
    data["session_secret"] = existing.session_secret

    cfg = AppConfig.model_validate(data)
    assign_ups_ids(cfg.ups)  # safety net: fill any still-empty ids with stable slugs
    assign_host_ids(cfg.hosts)
    assign_webhook_ids(cfg.notifications.webhooks)
    return cfg


@app.get("/api/config", dependencies=[Depends(require_auth)])
async def api_get_config():
    assert engine is not None
    return _sanitized_config(engine.cfg)


@app.post("/api/config", dependencies=[Depends(require_auth)])
async def api_set_config(incoming: dict):
    assert engine is not None
    old_ntp = engine.cfg.ntp_server
    old_tz = engine.cfg.timezone
    try:
        new_cfg = _merge_config(incoming, engine.cfg)
    except Exception as exc:  # noqa: BLE001 - validation error -> 400
        raise HTTPException(status_code=400, detail=f"Invalid configuration: {exc}")
    new_cfg.configured = True
    save_config(new_cfg)
    engine.update_config(new_cfg)
    db.log_event("Configuration saved", "", db.INFO)
    # Apply changed system settings (NTP / timezone) via the privileged agent (needs root).
    # No agent exists in Docker deployments; the values persist but nothing is enqueued.
    if not IS_DOCKER:
        # Guarded: the configuration is already saved and applied at this point, so an
        # unwritable queue must not answer with a 500 that reads as "nothing was saved".
        try:
            if new_cfg.ntp_server and new_cfg.ntp_server != old_ntp:
                _enqueue_agent("set-ntp", server=new_cfg.ntp_server)
            if new_cfg.timezone and new_cfg.timezone != old_tz:
                _enqueue_agent("set-timezone", tz=new_cfg.timezone)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not queue a system job for the agent: %s", exc)
            db.log_event(
                "System settings not applied",
                f"The configuration was saved, but NTP/timezone could not be handed to "
                f"the privileged agent: {exc}",
                db.WARNING,
            )
    return _sanitized_config(new_cfg)


# --- config export / import (full backup incl. plaintext secrets) -----------
def _exportable_config(cfg: AppConfig) -> dict:
    """Full config with revealed secrets, minus this instance's auth/session material."""
    data = _to_serialisable(cfg)
    data.pop("session_secret", None)
    data.pop("ui_password_hash", None)
    return data


@app.get("/api/config/export", dependencies=[Depends(require_auth)])
async def api_config_export():
    assert engine is not None
    data = _exportable_config(engine.cfg)
    first_ups = engine.cfg.ups[0].label if engine.cfg.ups else ""
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", first_ups).strip("-") or "appliance"
    stamp = datetime.now().strftime("%Y%m%d")
    filename = f"pve-usv-config-{host}-{stamp}.json"
    db.log_event("Configuration exported", "Backup including secrets downloaded.", db.INFO)
    return JSONResponse(
        data,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/config/import", dependencies=[Depends(require_auth)])
async def api_config_import(incoming: dict):
    assert engine is not None
    data = dict(incoming)
    # Keep the running instance's own auth/session material (not part of the backup).
    data["ui_password_hash"] = engine.cfg.ui_password_hash
    data["session_secret"] = engine.cfg.session_secret
    try:
        # A backup from before the history existed is an older installation coming back:
        # it gets the history off, exactly like an update (see migrate_history_default).
        new_cfg = AppConfig.model_validate(migrate_history_default(data))
    except Exception as exc:  # noqa: BLE001 - validation error -> 400
        raise HTTPException(status_code=400, detail=f"Invalid import file: {exc}")
    assign_ups_ids(new_cfg.ups)  # backups from <2.0 migrate to a single UPS; ensure ids
    assign_host_ids(new_cfg.hosts)  # backups from before 4.0.0 carry no host ids
    # And webhooks, which this path used to leave alone: the id keys the delivery state
    # and the masked-secret reconcile, so an import carrying two of them without ids left
    # both sharing one record.
    assign_webhook_ids(new_cfg.notifications.webhooks)
    new_cfg.configured = True
    save_config(new_cfg)
    engine.update_config(new_cfg)
    db.log_event("Configuration imported", "Settings taken over from file.", db.WARNING)
    # No agent exists in Docker deployments; the values persist but nothing is enqueued.
    # Guarded like the save path: the import has already been applied by now.
    if not IS_DOCKER:
        try:
            if new_cfg.ntp_server:
                _enqueue_agent("set-ntp", server=new_cfg.ntp_server)
            if new_cfg.timezone:
                _enqueue_agent("set-timezone", tz=new_cfg.timezone)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not queue a system job for the agent: %s", exc)
    return _sanitized_config(new_cfg)


# --- tests / actions (authenticated) ---------------------------------------
@app.post("/api/test/ups", dependencies=[Depends(require_auth)])
async def api_test_ups(incoming: dict):
    """One-shot poll of the submitted UPS settings (secrets reconciled by UPS id).

    Runs the production poll *and* a per-object probe: the poll proves the path the engine
    actually uses works (an SNMPv1 multi-object GET can fail even when every object is
    readable on its own), the probe says which object is to blame and which triggers the
    device cannot feed at all.
    """
    assert engine is not None
    incoming = dict(incoming)
    existing_ups = {u.id: u for u in engine.cfg.ups}
    _reconcile_ups_secrets(incoming, existing_ups.get(incoming.get("id")))
    try:
        cfg = _ups_model(incoming).model_validate(incoming)
    except Exception as exc:  # noqa: BLE001 - validation error -> 400
        raise HTTPException(status_code=400, detail=f"Invalid UPS settings: {exc}")
    state = await sources.poll(cfg)
    diag = await sources.probe(cfg)
    # Only the two together show an agent that chokes on multi-object requests (#40).
    hint = sources.poll_hint(cfg, state, diag)
    if hint:
        diag.summary = f"{diag.summary} {hint}".strip()
    return {
        "reachable": state.reachable,
        "power_source": state.power_source,
        "battery_status": state.battery_status,
        "runtime_remaining_min": state.runtime_remaining_min,
        "battery_charge_pct": state.battery_charge_pct,
        "error": state.error,
        "manufacturer": state.manufacturer,
        "model": state.model,
        "probe": {
            "reachable": diag.reachable,
            "summary": diag.summary,
            "ok_count": diag.ok_count,
            "total": diag.total,
            "mib": diag.mib,
            "entries": [asdict(e) for e in diag.entries],
            "missing_triggers": diag.missing_triggers,
        },
    }


@app.post("/api/test/snmp", dependencies=[Depends(require_auth)])
async def api_test_snmp(incoming: dict):
    """Kept for compatibility with 3.1.x; /api/test/ups supersedes it."""
    return await api_test_ups(incoming)


def _configured_node_names(host: PveHostConfig) -> list[str]:
    """Node names of all enabled PVE targets, including the one being tested.

    The tested entry is usually still unsaved when "Test" is pressed, so taking the
    stored config alone would report the node the user is configuring right now as
    uncovered.
    """
    stored = [
        h.name
        for h in (engine.cfg.hosts if engine else [])
        if isinstance(h, PveHostConfig) and h.enabled and h.key != host.key
    ]
    return stored + [host.name]


async def _check_node_name(
    host: PveHostConfig, known: proxmox.NodeList
) -> tuple[dict, str]:
    """Work out what to OFFER for a node name the API does not recognise.

    The verdict itself comes from proxmox.verify_node() via the credential test, which is
    what the self-test uses too — this only adds what a form can do and a background check
    cannot: propose a value. Hence the message here is the suggestion alone; repeating the
    diagnosis would say the same thing twice in one line.

    ``match`` comes from that same verdict rather than from "the name is in the list":
    on a cluster every member is in the list, so a name belonging to a *different* node
    than the one this API URL answers for used to count as a match and nothing was ever
    offered. Where the listing marks the local node, that node is the offer.

    A suggestion is offered only where it is not a guess — an empty field, a name that
    names another member of this cluster, or one differing from a real node in case or
    domain suffix alone. A filled-in field with no near match gets none: picking one of
    several nodes could name the wrong machine.
    """
    verdict = await proxmox.verify_node(host, known=known)
    data = {
        "readable": known.readable,
        "nodes": list(known.nodes),
        "match": verdict.state == "ok",
        "suggestion": None,
        "reason": None,
    }
    if not known.readable or data["match"]:
        return data, ""

    if verdict.state == "proxied" and known.local:
        # Not a guess: the API itself said which node answers here.
        data.update(suggestion=known.local, reason="local")
        return data, ""

    if not host.name:
        if known.local:
            data.update(suggestion=known.local, reason="local")
        elif len(known.nodes) == 1:
            data.update(suggestion=known.nodes[0], reason="only")
        return data, (
            f"No node name entered. This API reports: {', '.join(known.nodes)}."
        )

    near = dict(cluster.node_coverage(known.nodes, [host.name]).near).get(host.name)
    if not near:
        return data, ""
    # No sentence: this endpoint only ever answers the wizard, which renders the
    # suggestion as the clickable "Use <node>?" right after the message. Saying it in
    # words as well would put the same offer on the line twice.
    data.update(suggestion=near, reason="near")
    return data, ""


async def _check_host_cluster(
    host: PveHostConfig,
) -> tuple[dict, str, proxmox.NodeList]:
    """Verify the cluster side of a host's token and describe what was found.

    Returns the structured findings, one English sentence to append to the test message
    (API texts are English throughout, see the language policy), and the node names this
    inspection saw — the caller checks the configured name against them rather than
    asking the API a second time. Reuses cluster.inspect(), which is total and
    hard-bounded, so this cannot hang the request.

    A shorter timeout than the default: this is interactive, and the credential test
    before it may already have spent up to 10 s.
    """
    ap = engine.cfg.appliance if engine is not None else ApplianceConfig()
    info = await cluster.inspect(
        host, timeout=8.0, self_vmid=ap.self_vmid, self_node=ap.self_node,
        hostname=_hostname(),
    )
    # The guest privileges follow the guest stop: its own tick, or the Ceph tick, which
    # includes it where there is Ceph — the same reading as Engine._guest_plan().
    guest_stop = (
        (host.cluster_ceph and not info.ceph_unavailable) or host.cluster_guest_stop
    )
    missing = cluster.missing_privileges(
        info, want_ceph=host.cluster_ceph, want_disarm=host.cluster_ha_disarm,
        want_guests=guest_stop,
    )
    # Same options, same verdict in the diagnostics panel: a read only an unticked option
    # needs must not unfold it in orange (#39).
    cluster.mark_unneeded(info, want_ceph=host.cluster_ceph, want_guests=guest_stop)
    # /cluster/status names every member and marks the one that answered, so a cluster
    # member never needs the extra /nodes round trip.
    known = proxmox.NodeList(
        readable=bool(info.nodes), nodes=list(info.nodes), local=info.local_node or None
    )
    data = {
        "checked": True,
        "reachable": info.reachable,
        "is_cluster": info.is_cluster,
        "name": info.name,
        "nodes": len(info.nodes),
        "nodes_online": info.nodes_online,
        "quorate": info.quorate,
        "ceph_configured": info.ceph_configured,
        "ha_services": info.ha_services,
        "ha_armed_state": info.ha_armed_state,
        "ha_disarmed": info.ha_disarmed,
        "disarm_supported": info.disarm_supported,
        "missing_privileges": missing,
        # Missing but not fatal — kept apart so a hint never reads as a broken setup.
        "advisory_privileges": cluster.advisory_privileges(info),
        "guests_total": len(info.guests),
        "guests_running": len(info.running_guests),
        "guests_readable": info.guests_read,
        "mon_nodes": list(info.mon_nodes),
        "self_guest": (
            {
                "vmid": info.self_guest.vmid,
                "node": info.self_guest.node,
                "name": info.self_guest.name,
                "type": info.self_guest.kind,
                "label": info.self_guest.label,
            }
            if info.self_guest
            else None
        ),
        "self_guest_source": info.self_guest_source,
        "self_guest_on_ceph": info.self_guest_on_ceph,
        # Per-endpoint findings for the diagnostics panel, like the UPS test's probe.
        "entries": [asdict(e) for e in info.probe],
    }

    if not info.reachable:
        return data, f"Cluster check failed: {info.error or 'no answer'}.", known
    if not info.is_cluster and not info.can_audit:
        # Without Sys.Audit /cluster/status is refused, and "no cluster record" then means
        # "not allowed to look" — reporting it as "standalone" would send the user hunting
        # for the wrong problem.
        return data, (
            "Cluster status cannot be read: the token lacks Sys.Audit on '/', so it is "
            "unknown whether this node belongs to a cluster. See the manual for the "
            "pveum commands."
        ), known
    if not info.is_cluster:
        return data, (
            "This node is not part of a cluster — the cluster preparation will be "
            "skipped. Untick the cluster option, or point this entry at a cluster member."
        ), known

    parts = []
    if missing:
        # Named individually: "403" tells an operator nothing, the privilege name points
        # straight at the pveum commands in the manual.
        parts.append(
            f"Cluster '{info.name}' found, but the token is missing "
            f"{', '.join(missing)} on '/' — see the manual for the pveum commands."
        )
    else:
        ceph = "detected"
        if not info.ceph_configured:
            # Only a denied read is actionable; everything else means "no Ceph here".
            ceph = "not configured" if info.ceph_unavailable else (
                "status not readable (missing privilege)"
            )
        parts.append(
            f"Cluster '{info.name}': {len(info.nodes)} nodes "
            f"({info.nodes_online} online), "
            f"Ceph {ceph}, "
            f"HA {info.ha_armed_state or 'stack not reporting a state'} "
            f"({info.ha_services} HA guests)."
        )
    # A tick for a feature this cluster does not have. Worth saying here rather than only
    # in the scheduled self-test: this is the moment the operator is configuring it.
    if host.cluster_ceph and info.ceph_unavailable:
        parts.append(
            "The Ceph option is ticked, but this cluster does not run Ceph — "
            f"the step is skipped before every shutdown. Untick it; {cluster.PRIV_MODIFY} "
            "is then not needed on '/' either."
        )
    if guest_stop:
        # This is where the operator finds out what the cluster-wide guest stop will
        # actually do — while it can still be changed, rather than during the outage.
        parts.extend(_guest_stop_notes(host, info))
    # Reported regardless of whether HA manages any guest: a disarmed stack means no
    # fencing cluster-wide, and it stays that way until someone arms it again.
    if info.ha_disarmed:
        parts.append(
            "HA is currently disarmed — no fencing takes place. Use 'Restore cluster' "
            "on the dashboard once the cluster is fully back."
        )
    if (
        host.cluster_ha_disarm
        and (info.ha_resources or info.ha_present)
        and info.disarm_unavailable
    ):
        parts.append(
            "HA disarm needs Proxmox VE 9.2 or newer and will be skipped here; the Ceph "
            "flags still work."
        )
    if not info.quorate:
        parts.append("The cluster currently has no quorum.")
    # Nodes nobody would shut down. Said here and not only in the scheduled self-test for
    # the same reason as the Ceph note above: this is the moment it can still be fixed.
    # The entry being tested is usually unsaved, so it is added to the stored ones.
    cov = cluster.node_coverage(info.nodes, _configured_node_names(host))
    if cov.missing:
        parts.append(f"Not covered by any entry: {', '.join(cov.missing)}.")
    if host.cluster_ceph and info.mon_nodes:
        parts.append(
            f"Ceph monitors run on {', '.join(info.mon_nodes)} — those nodes should have "
            f"the highest 'order' so they are powered down last."
        )
    return data, " ".join(parts), known


def _guest_stop_notes(host: PveHostConfig, info: cluster.ClusterInfo) -> list[str]:
    """What the cluster-wide guest stop would do, or why it will not run."""
    if info.guests_unreadable:
        return [
            "The cluster's guest list cannot be read, so the guests cannot be stopped "
            f"before the nodes go down. Grant {cluster.PRIV_VM_AUDIT} and "
            f"{cluster.PRIV_VM_POWER} (on '/' or '/vms'). This endpoint filters by "
            "privilege instead of refusing, so an empty answer is never read as "
            "'no guests'."
        ]
    # The same precondition the preparation applies: with HA holding guests and no way to
    # disarm it, the HA manager would restart them as fast as they are stopped.
    if info.ha_resources and (not host.cluster_ha_disarm or info.disarm_unavailable):
        return [
            "HA manages guests on this cluster and cannot be disarmed "
            + ("(the HA disarm option is off)" if not host.cluster_ha_disarm
               else "(disarm-ha needs Proxmox VE 9.2 or newer)")
            + ", so the guests are NOT stopped cluster-wide before the shutdown — the HA "
            "manager would restart them as fast as they are stopped."
        ]
    ap = engine.cfg.appliance if engine is not None else ApplianceConfig()
    guest = info.self_guest
    notes = []
    if guest is None and not ap.self_external:
        notes.append(
            {
                "missing": f"The selected guest {ap.self_vmid} does not exist here.",
                "ambiguous": "Several guests carry this appliance's hostname.",
            }.get(info.self_guest_source, "This appliance's own guest is not selected.")
            + " Until it is picked under Settings -> Appliance the guests are NOT stopped "
            "before the shutdown, because stopping all of them would stop this appliance "
            "too."
        )
        return notes
    targets = cluster.stop_targets(info.guests, guest, _hostname())
    notes.append(
        f"Before the nodes go down, {len(targets)} of {len(info.running_guests)} running "
        f"guests would be stopped"
        + (f", sparing {guest.label}." if guest else " (this appliance runs elsewhere).")
    )
    if info.self_guest_on_ceph:
        on = [s for s in info.self_guest_storages if s in info.ceph_storages]
        notes.append(
            f"{guest.label} lives on Ceph storage ({', '.join(on)}). Move it to local "
            f"storage: once the OSDs drop below min_size its own IO blocks and it can no "
            f"longer shut anything down."
        )
    elif info.self_guest_on_ceph is None and guest is not None and info.ceph_configured:
        notes.append(
            f"Whether {guest.label} sits on Ceph storage could not be checked "
            f"({cluster.PRIV_DS_AUDIT} missing) — everything else works."
        )
    return notes


@app.post("/api/test/host", dependencies=[Depends(require_auth)])
async def api_test_host(incoming: dict):
    assert engine is not None
    # Reconcile this single host's secret against the stored one (by id, then legacy).
    # Felt here first: accepting the node name this very endpoint suggests changes the
    # name, and matching on it would answer "Authentication failed" for a valid token.
    incoming = dict(incoming)
    _reconcile_host_secrets(incoming, _find_host(incoming, *_host_lookups(engine.cfg.hosts)))
    try:
        host = _host_model(incoming).model_validate(incoming)
    except Exception as exc:  # noqa: BLE001 - validation error -> 400
        raise HTTPException(status_code=400, detail=f"Invalid host settings: {exc}")
    result = await targets.test_connection(host)
    message, cluster_info, node_check = result.message, None, None
    # PVE only, and only once the credentials work: an unreachable API cannot answer these
    # questions either, so asking would add its own timeout to the wait and append a
    # second, redundant error to the message.
    if result.ok and isinstance(host, PveHostConfig):
        # With the cluster switch on, the cluster features need three privileges beyond
        # Sys.PowerMgmt — without this check the user would not find out until the next
        # scheduled self-test, or the next real outage. It also names every member, which
        # saves the /nodes round trip below.
        if host.cluster:
            cluster_info, extra, known = await _check_host_cluster(host)
            message = f"{message} {extra}"
        else:
            known = await proxmox.list_nodes(host)
        # The node name is the one setting nothing else validates: test_connection() never
        # touches it, so a wrong one passes every check here and fails during the outage.
        node_check, extra = await _check_node_name(host, known)
        if extra:
            message = f"{message} {extra}"
    return {
        "ok": result.ok,
        "message": message,
        "has_power_mgmt": result.has_power_mgmt,
        # Member of proxmox.NODE_STATES, "unverified" for PBS and for anything unreadable.
        # The sentence is already in ``message``; this is what lets the UI label it with
        # the same chip the dashboard uses instead of hiding a warning in prose.
        "node_state": result.node_state,
        # None when the host is not a cluster member (or not PVE) — a new field only.
        "cluster": cluster_info,
        # None for non-PVE targets; "readable": false means the API would not name its
        # nodes, which is explicitly NOT a verdict on the configured name.
        "node_check": node_check,
    }


@app.post("/api/test/webhook", dependencies=[Depends(require_auth)])
async def api_test_webhook(incoming: dict):
    """Send one sample notification with the submitted (still unsaved) webhook settings.

    Neither ``enabled`` nor the severity filter apply here: the user asked for this send
    explicitly, and the point is to verify URL and format *before* saving.
    """
    assert engine is not None
    # Reconcile first, exactly like /api/test/ups and /api/test/host do.
    incoming = dict(incoming)
    existing = {h.id: h for h in engine.cfg.notifications.webhooks}
    _reconcile_webhook_secrets(incoming, existing.get(incoming.get("id")))
    try:
        hook = WebhookConfig.model_validate(incoming)
    except Exception as exc:  # noqa: BLE001 - validation error -> 400
        raise HTTPException(status_code=400, detail=f"Invalid webhook settings: {exc}")
    if not hook.url:
        raise HTTPException(status_code=400, detail="No webhook URL configured.")
    try:
        result = await notify.send_webhook(
            hook,
            "[PVE-UPS] Test notification",
            "Test message from the PVE-UPS web interface — the webhook is working.",
            db.INFO,
            engine.snapshot(),
        )
    except Exception as exc:  # noqa: BLE001 - report the failure, do not raise
        return {"ok": False, "message": str(exc) or exc.__class__.__name__}
    return {"ok": True, "message": result}


@app.post("/api/test/shutdown", dependencies=[Depends(require_auth)])
async def api_test_shutdown():
    """Log a dry-run shutdown without touching the live state machine (always safe)."""
    assert engine is not None
    msg = await engine.simulate_shutdown()
    return {"ok": True, "message": msg}


@app.post("/api/selftest/run", dependencies=[Depends(require_auth)])
async def api_selftest_run():
    """Run the credential and cluster checks now instead of waiting for the next slot.

    Saving settings deliberately does not fire a self-test, so without this a changed
    token or a freshly ticked cluster option would only be verified hours later.
    """
    assert engine is not None
    ok, message = await engine.run_selftest_now()
    return {"ok": ok, "message": message}


@app.post("/api/cluster/guests", dependencies=[Depends(require_auth)])
async def api_cluster_guests(incoming: dict):
    """List the cluster's guests, so the appliance's own can be PICKED rather than typed.

    A wrong vmid here means the appliance shuts itself down in the middle of an outage,
    which is precisely the class of mistake a free-text field invites. Hence a list.

    It takes a full host payload like /api/test/host — including the masked-secret
    reconcile — so the picker works from an unsaved host card too, and it is deliberately
    a separate endpoint: choosing the guest must not require running a credential test
    first.
    """
    assert engine is not None
    incoming = dict(incoming)
    _reconcile_host_secrets(incoming, _find_host(incoming, *_host_lookups(engine.cfg.hosts)))
    try:
        host = _host_model(incoming).model_validate(incoming)
    except Exception as exc:  # noqa: BLE001 - validation error -> 400
        raise HTTPException(status_code=400, detail=f"Invalid host settings: {exc}")
    if not isinstance(host, PveHostConfig):
        return {"ok": False, "message": "Only Proxmox VE hosts have a guest list.",
                "guests": []}

    ap = engine.cfg.appliance
    info = await cluster.inspect(
        host, timeout=8.0, self_vmid=ap.self_vmid, self_node=ap.self_node,
        hostname=_hostname(),
    )
    if not info.reachable:
        return {"ok": False, "message": info.error or "no answer", "guests": []}
    if info.guests_unreadable:
        # Named rather than returned as an empty list: /cluster/resources filters by
        # privilege instead of refusing, so "no guests" and "may not look" arrive
        # identically on the wire and must not read identically here.
        return {
            "ok": False,
            "message": f"The guest list could not be read - the token needs "
                       f"{cluster.PRIV_VM_AUDIT} (on '/' or '/vms').",
            "guests": [],
        }
    return {
        "ok": True,
        "message": f"{len(info.guests)} guests in cluster '{info.name or host.name}'.",
        "guests": [
            {
                "vmid": g.vmid,
                "name": g.name,
                "node": g.node,
                "type": g.kind,
                "status": g.status,
                "label": g.label,
            }
            for g in sorted(info.guests, key=lambda g: g.vmid)
        ],
        # What the hostname match found, as a suggestion for the picker. Only ever a
        # pre-selection: the stored value is whatever the operator confirms.
        "detected": (
            {"vmid": info.self_guest.vmid, "node": info.self_guest.node,
             "label": info.self_guest.label, "source": info.self_guest_source}
            if info.self_guest
            else {"vmid": None, "node": "", "label": "",
                  "source": info.self_guest_source}
        ),
        "ceph_storages": list(info.ceph_storages),
    }


@app.post("/api/cluster/restore", dependencies=[Depends(require_auth)])
async def api_cluster_restore():
    """Arm the HA manager again and clear the Ceph maintenance flags, per cluster.

    Explicit and manual on purpose: there is no automatic re-arm. Bringing HA back while
    nodes are still booting is a judgement call for someone who can see the cluster.
    """
    assert engine is not None
    allowed, results = await engine.restore_clusters()
    if not allowed:
        # Refused, not queued: arming HA while the nodes are powering off would undo the
        # preparation at exactly the moment it is doing its job.
        return {
            "ok": False,
            "message": "Not while a power outage is in progress.",
            "results": [],
        }
    if not results:
        return {"ok": True, "message": "No cluster needs restoring.", "results": []}
    ok = all(r["ok"] for r in results)
    return {
        "ok": ok,
        "message": "; ".join(f"{r['cluster']}: {r['message']}" for r in results),
        "results": results,
    }


@app.post("/api/reset", dependencies=[Depends(require_auth)])
async def api_reset():
    assert engine is not None
    engine.reset()
    db.log_event("State reset", "", db.INFO)
    return {"ok": True}


@app.get("/api/events", dependencies=[Depends(require_auth)])
async def api_events(limit: int = Query(100, ge=1, le=1000)):
    # Bounded: SQLite reads a negative LIMIT as "no limit", so ?limit=-1 serialised the
    # whole table out of a synchronous read on the event loop.
    return db.recent_events(limit)


@app.delete("/api/events", dependencies=[Depends(require_auth)])
async def api_events_clear():
    removed = db.clear_events()
    db.log_event("Event log cleared", f"{removed} entries removed.", db.INFO)
    return {"ok": True, "removed": removed}


# --- history (#37) -----------------------------------------------------------
# Plain ``def`` handlers on purpose: FastAPI runs them in its thread pool, so the SQLite
# reads (up to a quarter of a year of samples) never run on the event loop the engine
# polls on.
def _history_ups() -> list[tuple[str, str]]:
    assert engine is not None
    return [(u.id, u.label) for u in engine.cfg.ups]


@app.get("/api/history", dependencies=[Depends(require_auth)])
def api_history(
    range_key: str = Query("24h", alias="range"),
    start: Optional[int] = Query(None, alias="from"),
    end: Optional[int] = Query(None, alias="to"),
):
    assert engine is not None
    cfg = engine.cfg
    if not cfg.history.enabled:
        return {"enabled": False}
    meta = history.info()
    s, e = history.resolve_range(
        range_key, start, end, cfg.history.retention_days, oldest=meta["oldest"]
    )
    out = history.query(_history_ups(), s, e)
    # The runtime trigger each UPS actually uses, so the chart can draw how close an
    # outage came to it.
    by_id = {u.id: u for u in cfg.ups}
    for entry in out["ups"]:
        u = by_id.get(entry["id"])
        entry["threshold_runtime_min"] = (
            cfg.effective_thresholds(u).runtime_below_minutes if u is not None else None
        )
    try:
        events = db.events_between(
            datetime.fromtimestamp(s, timezone.utc),
            datetime.fromtimestamp(e, timezone.utc),
            limit=history.MAX_EVENTS,
        )
    except Exception as exc:  # noqa: BLE001 - markers are a nice-to-have
        log.warning("Reading events for the history failed: %s", exc)
        events = []
    out["events"] = [
        {
            "ts": int(datetime.fromisoformat(ev["ts"]).timestamp()),
            "severity": ev["severity"],
            "event": ev["event"],
            "detail": ev["detail"],
        }
        for ev in events
    ]
    out.update(
        enabled=True, retention_days=cfg.history.retention_days, oldest=meta["oldest"]
    )
    return out


@app.get("/api/history.csv", dependencies=[Depends(require_auth)])
def api_history_csv(
    range_key: str = Query("24h", alias="range"),
    start: Optional[int] = Query(None, alias="from"),
    end: Optional[int] = Query(None, alias="to"),
):
    assert engine is not None
    cfg = engine.cfg
    if not cfg.history.enabled:
        raise HTTPException(status_code=404, detail="History is switched off")
    s, e = history.resolve_range(
        range_key, start, end, cfg.history.retention_days, oldest=history.info()["oldest"]
    )
    body = history.csv_export(_history_ups(), s, e)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="pve-ups-history-{stamp}.csv"'},
    )


@app.get("/api/history/info", dependencies=[Depends(require_auth)])
def api_history_info():
    try:
        return history.info()
    except Exception as exc:  # noqa: BLE001 - the settings page must still open
        log.warning("Reading the history size failed: %s", exc)
        return {"size_bytes": None, "rows": None, "oldest": None}


@app.delete("/api/history", dependencies=[Depends(require_auth)])
def api_history_clear():
    removed = history.clear()
    db.log_event("History cleared", f"{removed} samples removed.", db.INFO)
    return {"ok": True, "removed": removed}


# --- updater (manual upload, applied by the privileged agent) ---------------
def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/update/status", dependencies=[Depends(require_auth)])
async def api_update_status():
    if IS_DOCKER:
        # No privileged agent exists in a Docker deployment; there is no queue/log to
        # report. The frontend shows "docker pull" guidance instead of this panel.
        return {
            "version": __version__,
            "deployment": "docker",
            "result": None,
            "last_job": None,
            "pending": [],
            "log_tail": None,
            "agent_drainer": None,
        }
    # Ingesting here makes the outcome show up in the event log even if the user never
    # left the settings page open during the restart.
    result = _ingest_agent_result()
    pending = sorted(p.name for p in AGENT_QUEUE.glob("*.json")) if AGENT_QUEUE.exists() else []
    last_job = (_read_text(AGENT_LAST_JOB) or "").strip() or None
    log_tail = None
    raw = _read_text(AGENT_LOG)
    if raw:
        log_tail = "\n".join(raw.splitlines()[-40:])
    return {
        "version": __version__,
        "deployment": "lxc",
        "result": result,
        "last_job": last_job,
        "pending": pending,
        "log_tail": log_tail,
        "agent_drainer": _agent_drainer_active(),
    }


@app.post("/api/update/upload", dependencies=[Depends(require_auth)])
async def api_update_upload(file: UploadFile = File(...)):
    if IS_DOCKER:
        raise HTTPException(
            status_code=501,
            detail="In-app updates are not supported in Docker deployments. "
            "Pull a new image tag and recreate the container.",
        )
    name = file.filename or ""
    # ".tar" and ".gz" are accepted because Safari unpacks .tar.gz on download; the real
    # decision is made on the archive's CONTENT below, so the extension is only a cheap
    # first filter against obvious mistakes.
    if not name.endswith((".tar.gz", ".tgz", ".tar", ".gz", ".zip")):
        raise HTTPException(
            status_code=400, detail="Only .tar.gz/.tgz/.tar/.gz/.zip archives are accepted"
        )
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitise the name to a basename; the agent only looks in UPDATE_DIR.
    safe = Path(name).name
    target = UPDATE_DIR / safe
    size = 0
    with target.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            fh.write(chunk)

    # Validate BEFORE queueing: the agent applies the job as root, so a package that is
    # unreadable or simply not this project must be rejected while it is still ours.
    pkg_version, error = _inspect_package(target)
    if error:
        try:
            target.unlink()
        except Exception as exc:  # noqa: BLE001 - the rejection stands either way
            log.warning("Could not remove rejected upload %s: %s", target, exc)
        raise HTTPException(status_code=400, detail=error)

    same_version = bool(pkg_version) and pkg_version == __version__
    job_id = _enqueue_agent("update", package=str(target))
    try:
        AGENT_LAST_JOB.write_text(job_id, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record last update job: %s", exc)
    db.log_event(
        "Update uploaded",
        f"Package {safe} ({size // 1024} KiB), package version {pkg_version or 'unknown'}; "
        f"running {__version__}. Applied by the system agent (job {job_id}).",
        db.WARNING,
    )
    return {
        "ok": True,
        "job_id": job_id,
        "package": safe,
        "package_version": pkg_version,
        "running_version": __version__,
        "same_version": same_version,
    }


# --- static UI --------------------------------------------------------------
# Cache busting without a build step. Browsers may cache a response that carries no
# Cache-Control heuristically (roughly 10% of its age), so after an update the old
# app.js/style.css can be served for days without a single request reaching us. The
# fix has two halves: index.html itself is never stored, and every asset it references
# gets a "?v=<stamp>" that changes whenever the file does, so the stamped URL can then
# be cached forever. The stamp comes from the file's mtime/size, NOT from __version__:
# the update agent copies with shutil.copy2 (mtimes survive the tarball), it also works
# while developing without a version bump, and it leaves unchanged files in the cache.
_ASSET_REF_RE = re.compile(r'((?:src|href)=")(/[^"?#]+\.(?:js|css))(")')



def _asset_stamp(url_path: str) -> str:
    """Short cache-busting token for a web asset; falls back to the app version."""
    try:
        st = (WEB_DIR / url_path.lstrip("/")).stat()
    except OSError:
        return __version__
    return f"{st.st_mtime_ns:x}{st.st_size:x}"


def _render_index() -> str:
    """index.html with a "?v=" stamp on every local .js/.css reference.

    Rendered per request on purpose: it is a handful of stat() calls on a document that
    is fetched once per page load, and nothing can then go stale behind a cache of ours.
    """
    return _ASSET_REF_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}?v={_asset_stamp(m.group(2))}{m.group(3)}",
        (WEB_DIR / "index.html").read_text(encoding="utf-8"),
    )


class _StaticWithCacheHeaders(StaticFiles):
    """Static files with explicit caching rules (never leave it to the heuristic)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            stamped = b"v=" in scope.get("query_string", b"")
            resp.headers["cache-control"] = (
                "public, max-age=31536000, immutable" if stamped
                # No stamp (manual.html, a hand-typed asset URL): revalidate every time.
                # Starlette answers with an ETag, so an unchanged file costs a 304.
                else "no-cache"
            )
        return resp


@app.get("/")
async def index():
    # no-store, not no-cache: this response has no ETag to revalidate against, and it
    # is the document that carries the current asset stamps — it must never be reused.
    return HTMLResponse(_render_index(), headers={"Cache-Control": "no-store"})


app.mount("/", _StaticWithCacheHeaders(directory=WEB_DIR), name="web")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    run()

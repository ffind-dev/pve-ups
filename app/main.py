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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from passlib.context import CryptContext
from pydantic import BaseModel

from . import __version__, db
from .config import (
    AppConfig,
    HostConfig,
    SnmpConfig,
    _to_serialisable,
    assign_ups_ids,
    load_config,
    save_config,
)
from .engine import Engine
from . import proxmox, ups

log = logging.getLogger("pve-usv")

WEB_DIR = Path(__file__).parent / "web"
SECRET_PLACEHOLDER = "**********"  # pydantic SecretStr json mask; means "unchanged"
SESSION_COOKIE = "pve_usv_session"
SESSION_MAX_AGE = 8 * 3600

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


def _read_package_version(path: Path) -> Optional[str]:
    """Best-effort: read ``__version__`` from app/__init__.py inside the uploaded archive."""
    try:
        data: Optional[str] = None
        if path.name.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                names = [n for n in z.namelist() if n.endswith("app/__init__.py")]
                if names:
                    data = z.read(min(names, key=len)).decode("utf-8", "replace")
        else:
            with tarfile.open(path) as t:
                members = [m for m in t.getmembers() if m.name.endswith("app/__init__.py")]
                if members:
                    fh = t.extractfile(min(members, key=lambda m: len(m.name)))
                    data = fh.read().decode("utf-8", "replace") if fh else None
        if data:
            m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", data)
            if m:
                return m.group(1)
    except Exception as exc:  # noqa: BLE001 - best effort only
        log.warning("Could not read package version from %s: %s", path, exc)
    return None


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
    payload = {
        "status": "ok" if ok else "degraded",
        "version": __version__,
        "engine_state": snap["appliance"]["engine_state"],
        # True only when every configured UPS is reachable (all() of empty list is True).
        "ups_reachable": all(u["reachable"] for u in ups_list),
        "ups_reachable_count": sum(1 for u in ups_list if u["reachable"]),
        "ups_total": len(ups_list),
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


def _reconcile_ups_secrets(ups_entry: dict, old: Optional[SnmpConfig]) -> None:
    """Carry over unchanged (masked) per-UPS secrets, in place."""
    defaults = {"community": "public", "v3_auth_pass": "", "v3_priv_pass": ""}
    for fld, default in defaults.items():
        old_secret = getattr(old, fld).get_secret_value() if old else default
        ups_entry[fld] = _reconcile_secret(ups_entry.get(fld), old_secret)


def _merge_config(incoming: dict, existing: AppConfig) -> AppConfig:
    """Build a new config, carrying over unchanged (masked) secrets."""
    data = dict(incoming)

    # Per-UPS SNMP secrets, matched by stable UPS id.
    existing_ups = {u.id: u for u in existing.ups}
    for ups_entry in data.get("ups", []) or []:
        _reconcile_ups_secrets(ups_entry, existing_ups.get(ups_entry.get("id")))
    data.pop("snmp", None)  # legacy key never accepted from the form

    # Host token secrets matched by node name
    existing_hosts = {h.name: h for h in existing.hosts}
    for host in data.get("hosts", []):
        old = existing_hosts.get(host.get("name"))
        old_secret = old.token_secret.get_secret_value() if old else ""
        host["token_secret"] = _reconcile_secret(host.get("token_secret"), old_secret)

    # Never overwrite auth/session material from the config form.
    data["ui_password_hash"] = existing.ui_password_hash
    data["session_secret"] = existing.session_secret

    cfg = AppConfig.model_validate(data)
    assign_ups_ids(cfg.ups)  # safety net: fill any still-empty UPS ids with stable slugs
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
    if new_cfg.ntp_server and new_cfg.ntp_server != old_ntp:
        _enqueue_agent("set-ntp", server=new_cfg.ntp_server)
    if new_cfg.timezone and new_cfg.timezone != old_tz:
        _enqueue_agent("set-timezone", tz=new_cfg.timezone)
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
    first_ups = engine.cfg.ups[0].host if engine.cfg.ups else ""
    host = first_ups or "appliance"
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
        new_cfg = AppConfig.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - validation error -> 400
        raise HTTPException(status_code=400, detail=f"Invalid import file: {exc}")
    assign_ups_ids(new_cfg.ups)  # backups from <2.0 migrate to a single UPS; ensure ids
    new_cfg.configured = True
    save_config(new_cfg)
    engine.update_config(new_cfg)
    db.log_event("Configuration imported", "Settings taken over from file.", db.WARNING)
    if new_cfg.ntp_server:
        _enqueue_agent("set-ntp", server=new_cfg.ntp_server)
    if new_cfg.timezone:
        _enqueue_agent("set-timezone", tz=new_cfg.timezone)
    return _sanitized_config(new_cfg)


# --- tests / actions (authenticated) ---------------------------------------
@app.post("/api/test/snmp", dependencies=[Depends(require_auth)])
async def api_test_snmp(incoming: dict):
    """One-shot SNMP poll with the submitted settings (secrets reconciled by UPS id)."""
    assert engine is not None
    incoming = dict(incoming)
    existing_ups = {u.id: u for u in engine.cfg.ups}
    _reconcile_ups_secrets(incoming, existing_ups.get(incoming.get("id")))
    state = await ups.poll(SnmpConfig.model_validate(incoming))
    return {
        "reachable": state.reachable,
        "power_source": state.power_source,
        "battery_status": state.battery_status,
        "runtime_remaining_min": state.runtime_remaining_min,
        "battery_charge_pct": state.battery_charge_pct,
        "error": state.error,
    }


@app.post("/api/test/host", dependencies=[Depends(require_auth)])
async def api_test_host(incoming: dict):
    assert engine is not None
    # Reconcile this single host's secret against the stored one (by name).
    existing_hosts = {h.name: h for h in engine.cfg.hosts}
    old = existing_hosts.get(incoming.get("name"))
    old_secret = old.token_secret.get_secret_value() if old else ""
    incoming = dict(incoming)
    incoming["token_secret"] = _reconcile_secret(incoming.get("token_secret"), old_secret)
    host = HostConfig.model_validate(incoming)
    result = await proxmox.test_connection(host)
    return {"ok": result.ok, "message": result.message, "has_power_mgmt": result.has_power_mgmt}


@app.post("/api/test/shutdown", dependencies=[Depends(require_auth)])
async def api_test_shutdown():
    """Log a dry-run shutdown without touching the live state machine (always safe)."""
    assert engine is not None
    msg = await engine.simulate_shutdown()
    return {"ok": True, "message": msg}


@app.post("/api/reset", dependencies=[Depends(require_auth)])
async def api_reset():
    assert engine is not None
    engine.reset()
    db.log_event("State reset", "", db.INFO)
    return {"ok": True}


@app.get("/api/events", dependencies=[Depends(require_auth)])
async def api_events(limit: int = 100):
    return db.recent_events(limit)


@app.delete("/api/events", dependencies=[Depends(require_auth)])
async def api_events_clear():
    removed = db.clear_events()
    db.log_event("Event log cleared", f"{removed} entries removed.", db.INFO)
    return {"ok": True, "removed": removed}


# --- updater (manual upload, applied by the privileged agent) ---------------
def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/update/status", dependencies=[Depends(require_auth)])
async def api_update_status():
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
        "result": result,
        "last_job": last_job,
        "pending": pending,
        "log_tail": log_tail,
        "agent_drainer": _agent_drainer_active(),
    }


@app.post("/api/update/upload", dependencies=[Depends(require_auth)])
async def api_update_upload(file: UploadFile = File(...)):
    name = file.filename or ""
    if not name.endswith((".tar.gz", ".tgz", ".zip")):
        raise HTTPException(status_code=400, detail="Only .tar.gz/.tgz/.zip allowed")
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitise the name to a basename; the agent only looks in UPDATE_DIR.
    safe = Path(name).name
    target = UPDATE_DIR / safe
    size = 0
    with target.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            fh.write(chunk)

    pkg_version = _read_package_version(target)
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
@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    run()

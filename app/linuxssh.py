"""SSH client for plain Linux shutdown targets (no Proxmox API on the machine).

The appliance holds ONE ed25519 key of its own, generated on first use in its state
directory. On the target, app/remote/linux-target-setup.sh installs that key for a
dedicated account, locked to a forced command that understands exactly two requests:

    pve-ups test      -> PVE-UPS-REMOTE 1 ok power=yes|no host=<hostname>
    pve-ups shutdown  -> PVE-UPS-REMOTE 1 accepted

"power=yes" means the account's single sudo rule — run the root-owned power-off script —
is in place: the SSH counterpart of the Sys.PowerMgmt check on a Proxmox token. The
shutdown request detaches the power-off on the target (systemd-run) and answers at once,
so the reply arrives before the network goes away.

What is deliberately NOT used: the process's ~/.ssh/config, ssh-agent and known_hosts.
The appliance's behaviour must follow from its own configuration, not from whatever an
operator once put in a home directory. Host keys are pinned per entry instead
(LinuxHostConfig.ssh_host_key), on the first successful test.

Both entry points never raise, like the API clients behind app/targets.py.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from importlib import resources
from pathlib import Path
from typing import Optional

import asyncssh

from . import db
from .config import LinuxHostConfig
from .proxmox import CONNECT_TIMEOUT_S, TestResult

log = logging.getLogger("pve-usv.linuxssh")

KEY_PATH = Path(
    os.environ.get("PVE_USV_SSH_KEY", str(db.DB_PATH.parent / "ssh" / "id_ed25519"))
)
KEY_TYPE = "ssh-ed25519"

CMD_TEST = "pve-ups test"
CMD_SHUTDOWN = "pve-ups shutdown"
REPLY_PREFIX = "PVE-UPS-REMOTE 1 "

# Marker in the setup script that the UI download replaces with the public key.
SETUP_SCRIPT_KEY_MARKER = "__PVE_UPS_PUBKEY__"

_key_lock = threading.Lock()
_key_cache: dict[Path, asyncssh.SSHKey] = {}


def appliance_key(path: Optional[Path] = None) -> asyncssh.SSHKey:
    """The appliance's private key, generated (0600, in a 0700 directory) on first use.

    Raises OSError when the state directory is not writable — the callers below turn that
    into a failed result like any other connection error.
    """
    path = Path(path or KEY_PATH)
    with _key_lock:
        cached = _key_cache.get(path)
        if cached is not None:
            return cached
        if path.exists():
            key = asyncssh.read_private_key(str(path))
        else:
            key = asyncssh.generate_private_key(KEY_TYPE, comment=_key_comment())
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            tmp = path.with_suffix(".tmp")
            # O_EXCL + mode at creation: the private half is never world-readable, not
            # even for the moment between write and chmod.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key.export_private_key("openssh"))
            os.replace(tmp, path)
            log.info("Generated the appliance SSH key %s", path)
        _key_cache[path] = key
        return key


def _key_comment() -> str:
    try:
        return f"pve-ups@{socket.gethostname()}"
    except OSError:  # pragma: no cover - gethostname essentially never fails
        return "pve-ups"


def public_key_line(path: Optional[Path] = None) -> str:
    """The public key as one authorized_keys line ("ssh-ed25519 AAAA… pve-ups@host")."""
    key = appliance_key(path)
    return key.export_public_key("openssh").decode().strip()


def setup_script(path: Optional[Path] = None) -> str:
    """The target-side setup script with this appliance's public key filled in."""
    text = (resources.files("app") / "remote" / "linux-target-setup.sh").read_text()
    return text.replace(SETUP_SCRIPT_KEY_MARKER, public_key_line(path), 1)


def _key_fields(line: str) -> str:
    """"<algorithm> <base64>" of a key line — comments are not part of the identity."""
    return " ".join((line or "").split()[:2])


def _fingerprint(line: str) -> str:
    try:
        return asyncssh.import_public_key(_key_fields(line)).get_fingerprint("sha256")
    except (asyncssh.KeyImportError, ValueError):
        return "?"


def _known_hosts(host: LinuxHostConfig):
    """None (accept and report) until a key is pinned; then only that key."""
    if not host.ssh_host_key:
        return None
    return ([asyncssh.import_public_key(_key_fields(host.ssh_host_key))], [], [])


async def _run(host: LinuxHostConfig, command: str, timeout: float) -> tuple[str, str]:
    """Run one request on the target; returns (reply after the prefix, server host key)."""
    conn = await asyncssh.connect(
        host.ssh_host,
        port=host.ssh_port,
        username=host.ssh_user,
        client_keys=[appliance_key()],
        known_hosts=_known_hosts(host),
        config=None,
        agent_path=None,
        preferred_auth="publickey",
        connect_timeout=min(timeout, CONNECT_TIMEOUT_S),
        login_timeout=timeout,
    )
    try:
        server_key = conn.get_server_host_key()
        host_key = (
            _key_fields(server_key.export_public_key("openssh").decode()) if server_key else ""
        )
        result = await conn.run(command, check=False, timeout=timeout)
    finally:
        conn.close()
    out = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode()
    for line in out.splitlines():
        if line.startswith(REPLY_PREFIX):
            return line[len(REPLY_PREFIX):].strip(), host_key
    got = (out.strip() or str(result.stderr or "").strip() or "no output")[:160]
    raise _NotTheRemoteScript(got)


class _NotTheRemoteScript(Exception):
    """The server answered, but not with the forced command's reply line."""


def _error_text(host: LinuxHostConfig, exc: BaseException) -> str:
    where = f"{host.ssh_user}@{host.ssh_host}:{host.ssh_port}"
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return (
            f"Host key of {where} does not match the pinned one "
            f"({_fingerprint(host.ssh_host_key)}). Reinstalled machine, or not the one "
            f"configured? Clear the host key on the card and test again to re-pin it."
        )
    if isinstance(exc, asyncssh.PermissionDenied):
        return (
            f"{where} refused the appliance key. Run the setup script on the server "
            f"(it installs the key for '{host.ssh_user}'), then test again."
        )
    if isinstance(exc, _NotTheRemoteScript):
        return (
            f"{where} accepted the key but did not answer like the PVE-UPS remote script "
            f"(got: {exc}). The key must be installed by the setup script, which locks it "
            f"to the forced command."
        )
    # Before OSError: asyncssh's own TimeoutError derives from both.
    if isinstance(exc, TimeoutError):
        return f"No answer from {where} in time"
    if isinstance(exc, asyncssh.ConnectionLost):
        return f"{where} closed the connection before answering"
    if isinstance(exc, OSError):
        return f"Cannot connect to {where}: {exc.strerror or exc}"
    return f"SSH error with {where}: {exc}"


async def test_connection(host: LinuxHostConfig, timeout: float = 10.0) -> TestResult:
    """Reach the target, check the forced command and its sudo rule, report the host key."""
    try:
        reply, host_key = await _run(host, CMD_TEST, timeout)
    except Exception as exc:  # noqa: BLE001 - never raises, see module docstring
        return TestResult(False, _error_text(host, exc))

    words = reply.split()
    status = words[0] if words else ""
    fields = dict(w.split("=", 1) for w in words[1:] if "=" in w)
    if status != "ok":
        return TestResult(False, f"The remote script answered '{reply}'", host_key=host_key)

    remote_name = fields.get("host", "?")
    pinned = bool(host.ssh_host_key)
    key_note = (
        "Host key matches the pinned one."
        if pinned
        else f"Host key {_fingerprint(host_key)} recorded — it is pinned when you save."
    )
    if fields.get("power") != "yes":
        return TestResult(
            True,
            f"Connection to '{remote_name}' ok, but the sudo rule for the power-off script "
            f"is missing (/etc/sudoers.d/pve-ups). Re-run the setup script. {key_note}",
            has_power_mgmt=False,
            host_key=host_key,
        )
    return TestResult(
        True,
        f"Connection to '{remote_name}' and power-off permission ok. {key_note}",
        has_power_mgmt=True,
        host_key=host_key,
    )


async def shutdown_node(host: LinuxHostConfig, timeout: float = 60.0) -> tuple[bool, str]:
    """Ask the target to power off. Returns (ok, message)."""
    try:
        reply, _ = await _run(host, CMD_SHUTDOWN, timeout)
    except Exception as exc:  # noqa: BLE001
        msg = _error_text(host, exc)
        log.error("Shutdown of %s failed: %s", host.name, msg)
        return False, msg
    if reply.split()[:1] == ["accepted"]:
        return True, "Shutdown command accepted"
    return False, f"The remote script answered '{reply}'"

"""Linux (SSH) shutdown target: model, config round trip and the client against a real
in-process SSH server that plays the forced command from app/remote/linux-target-setup.sh."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
from pathlib import Path

import asyncssh
import pytest

from app import linuxssh, targets
from app.config import (
    AppConfig,
    LinuxHostConfig,
    PveHostConfig,
    load_config,
    save_config,
)


@pytest.fixture
def key_path(tmp_path, monkeypatch):
    path = tmp_path / "state" / "ssh" / "id_ed25519"
    monkeypatch.setattr(linuxssh, "KEY_PATH", path)
    linuxssh._key_cache.clear()
    yield path
    linuxssh._key_cache.clear()


class FakeRemote:
    """An SSH server that accepts only the appliance key and answers like the script."""

    def __init__(self, client_key: asyncssh.SSHKey, power: bool = True, reply: str = ""):
        self.client_key = client_key
        self.power = power
        self.reply = reply  # overrides the scripted answer when set
        self.commands: list[str] = []
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.server = None
        self.port = 0

    def host_key_line(self) -> str:
        return self.host_key.export_public_key("openssh").decode().strip()

    async def __aenter__(self):
        remote = self

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def public_key_auth_supported(self):
                return True

            def validate_public_key(self, username, key):
                return username == "upsshutdown" and (
                    key.public_data == remote.client_key.public_data
                )

        async def handle(process: asyncssh.SSHServerProcess):
            cmd = process.command or ""
            remote.commands.append(cmd)
            if remote.reply:
                process.stdout.write(remote.reply + "\n")
            elif cmd == "pve-ups test":
                p = "yes" if remote.power else "no"
                process.stdout.write(f"PVE-UPS-REMOTE 1 ok power={p} host=gpu01\n")
            elif cmd == "pve-ups shutdown":
                process.stdout.write("PVE-UPS-REMOTE 1 accepted\n")
            else:
                process.stdout.write("PVE-UPS-REMOTE 1 error unknown-command\n")
            process.exit(0)

        self.server = await asyncssh.create_server(
            Server, "127.0.0.1", 0, server_host_keys=[self.host_key], process_factory=handle
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    def target(self, **kw) -> LinuxHostConfig:
        data = dict(name="gpu01", ssh_host="127.0.0.1", ssh_port=self.port)
        data.update(kw)
        return LinuxHostConfig(**data)


# --- key handling ---------------------------------------------------------------------


def test_appliance_key_is_generated_private_and_stable(key_path):
    line = linuxssh.public_key_line()
    assert line.startswith("ssh-ed25519 ")
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(key_path.parent).st_mode) == 0o700
    linuxssh._key_cache.clear()  # a restart reads the same key back
    assert linuxssh.public_key_line() == line


def test_setup_script_carries_the_public_key(key_path):
    script = linuxssh.setup_script()
    assert linuxssh.SETUP_SCRIPT_KEY_MARKER not in script
    assert f"PUBKEY='{linuxssh.public_key_line()}'" in script


def test_setup_script_is_valid_posix_sh(key_path, tmp_path):
    path = tmp_path / "setup.sh"
    path.write_text(linuxssh.setup_script())
    subprocess.run(["sh", "-n", str(path)], check=True)


# --- model ----------------------------------------------------------------------------


def test_linux_model_needs_no_api_fields():
    host = LinuxHostConfig(name="gpu01", ssh_host=" 10.0.0.31 ")
    assert host.ssh_host == "10.0.0.31"
    assert host.address == "ssh://10.0.0.31:22"
    assert AppConfig.incomplete_host(host) == ""
    assert AppConfig.incomplete_host(LinuxHostConfig(name="x")) == "no SSH address"
    assert LinuxHostConfig.secret_fields() == {}


def test_config_roundtrip_with_linux_target(tmp_path):
    cfg = AppConfig(
        hosts=[
            PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006", token_id="a",
                          token_secret="s"),
            LinuxHostConfig(name="gpu01", ssh_host="10.0.0.31", ssh_port=2222,
                            ssh_host_key="ssh-ed25519 AAAAC3Nz", order=0),
        ]
    )
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    restored = load_config(path)
    assert [h.type for h in restored.hosts] == ["pve", "linux"]
    gpu = restored.hosts[1]
    assert isinstance(gpu, LinuxHostConfig)
    assert (gpu.ssh_host, gpu.ssh_port, gpu.ssh_host_key) == ("10.0.0.31", 2222,
                                                             "ssh-ed25519 AAAAC3Nz")


def test_duplicate_ssh_targets_are_flagged():
    cfg = AppConfig(hosts=[
        LinuxHostConfig(name="a", ssh_host="10.0.0.31"),
        LinuxHostConfig(name="b", ssh_host="10.0.0.31"),
        LinuxHostConfig(name="c", ssh_host="10.0.0.32"),
    ])
    assert cfg.duplicate_api_urls() == ["ssh://10.0.0.31:22"]


# --- client against a live SSH server ----------------------------------------------------


async def test_test_connection_reports_power_and_host_key(key_path):
    async with FakeRemote(linuxssh.appliance_key()) as remote:
        result = await targets.test_connection(remote.target())
    assert result.ok and result.has_power_mgmt
    assert "gpu01" in result.message
    assert result.host_key == " ".join(remote.host_key_line().split()[:2])
    assert remote.commands == ["pve-ups test"]


async def test_missing_sudo_rule_is_not_power(key_path):
    async with FakeRemote(linuxssh.appliance_key(), power=False) as remote:
        result = await targets.test_connection(remote.target())
    assert result.ok and not result.has_power_mgmt
    assert "sudo" in result.message


async def test_shutdown_is_accepted(key_path):
    async with FakeRemote(linuxssh.appliance_key()) as remote:
        ok, msg = await targets.shutdown(remote.target(), timeout=10)
    assert ok, msg
    assert remote.commands == ["pve-ups shutdown"]


async def test_pinned_host_key_is_enforced(key_path):
    async with FakeRemote(linuxssh.appliance_key()) as remote:
        good = remote.target(ssh_host_key=remote.host_key_line())
        assert (await targets.test_connection(good)).ok
        other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
        ok, msg = await targets.shutdown(remote.target(ssh_host_key=other), timeout=10)
    assert not ok
    assert "does not match the pinned" in msg
    assert remote.commands == ["pve-ups test"]  # the impostor never got the command


async def test_foreign_key_is_refused(key_path):
    stranger = asyncssh.generate_private_key("ssh-ed25519")
    async with FakeRemote(stranger) as remote:
        result = await targets.test_connection(remote.target())
    assert not result.ok
    assert "refused the appliance key" in result.message


async def test_key_without_forced_command_is_reported(key_path):
    async with FakeRemote(linuxssh.appliance_key(), reply="bash: pve-ups: not found") as remote:
        result = await targets.test_connection(remote.target())
    assert not result.ok
    assert "forced command" in result.message


async def test_unreachable_target_fails_within_its_timeout(key_path):
    # A listening socket that never speaks SSH: the classic "port open, nothing answers".
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        host = LinuxHostConfig(name="mute", ssh_host="127.0.0.1", ssh_port=port)
        loop = asyncio.get_running_loop()
        started = loop.time()
        ok, msg = await targets.shutdown(host, timeout=1.0)
        assert not ok
        assert loop.time() - started < 1.0 + targets.DEADLINE_GRACE_S
    finally:
        server.close()


async def test_closed_port_fails_cleanly(key_path):
    host = LinuxHostConfig(name="off", ssh_host="127.0.0.1", ssh_port=1)
    result = await targets.test_connection(host, timeout=3)
    assert not result.ok
    assert "Cannot connect" in result.message or "No answer" in result.message


def test_setup_script_path_is_packaged():
    assert Path(linuxssh.__file__).parent.joinpath("remote", "linux-target-setup.sh").is_file()

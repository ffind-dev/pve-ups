"""Unit tests for config persistence and the engine trigger logic.

Run with:  pytest
These tests need no UPS hardware and no network.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import (
    AppConfig,
    HostConfig,
    SnmpConfig,
    SnmpVersion,
    Thresholds,
    UpsThresholdOverride,
    load_config,
    save_config,
)
from app.engine import ON_BATTERY, ONLINE, SHUTDOWN_PENDING, SHUTTING_DOWN, Engine
from app.ups import UpsState


@pytest.fixture(autouse=True)
def _isolated_engine_state(tmp_path, monkeypatch):
    """Point the engine's battery-timer state file at a per-test path, so tests never
    read/write a real (or another test's) engine-state.json."""
    from app import engine as engine_mod
    monkeypatch.setattr(engine_mod, "STATE_PATH", tmp_path / "engine-state.json")


# --- config round-trip ------------------------------------------------------
def test_config_roundtrip_keeps_secrets(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(
        ups=[SnmpConfig(id="ups1", name="USV A", host="10.0.0.9",
                        version=SnmpVersion.v2c, community="topsecret")],
        hosts=[HostConfig(name="pve01", api_url="https://10.0.0.10:8006",
                          token_id="ups@pve!shutdown", token_secret="uuid-secret",
                          this_host=True, ups_ids=["ups1"])],
    )
    save_config(cfg, path)
    loaded = load_config(path)

    assert loaded.ups[0].host == "10.0.0.9"
    assert loaded.ups[0].community.get_secret_value() == "topsecret"
    assert loaded.ups[0].version == SnmpVersion.v2c
    assert loaded.hosts[0].token_secret.get_secret_value() == "uuid-secret"
    assert loaded.hosts[0].ups_ids == ["ups1"]
    # File must be owner-only.
    import os, stat
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if os.name != "nt":
        assert mode == 0o600


def test_config_roundtrip_multi_ups_and_overrides(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(
        ups=[
            SnmpConfig(id="a", name="A", host="10.0.0.1", community="ca"),
            SnmpConfig(id="b", name="B", host="10.0.0.2", community="cb",
                       overrides=UpsThresholdOverride(runtime_below_minutes=2)),
        ],
        hosts=[HostConfig(name="pve01", api_url="x", ups_ids=["a", "b"], ups_policy="all")],
    )
    save_config(cfg, path)
    loaded = load_config(path)
    assert [u.id for u in loaded.ups] == ["a", "b"]
    assert loaded.ups[0].community.get_secret_value() == "ca"
    assert loaded.ups[1].community.get_secret_value() == "cb"
    assert loaded.ups[1].overrides.runtime_below_minutes == 2
    # effective thresholds: per-UPS override applied, global value inherited otherwise
    assert loaded.effective_thresholds(loaded.ups[1]).runtime_below_minutes == 2
    assert loaded.effective_thresholds(loaded.ups[0]).runtime_below_minutes == \
        loaded.thresholds.runtime_below_minutes


def test_config_migrates_single_snmp_to_ups_list():
    # An old (pre-2.0) config dict with a single `snmp` block migrates to `ups: [...]`.
    old = {
        "snmp": {"host": "10.0.0.9", "community": "sec", "version": "v2c"},
        "hosts": [{"name": "pve01", "api_url": "https://x:8006"}],
    }
    cfg = AppConfig.model_validate(old)
    assert len(cfg.ups) == 1
    assert cfg.ups[0].id == "ups1"
    assert cfg.ups[0].host == "10.0.0.9"
    assert cfg.ups[0].community.get_secret_value() == "sec"
    # the host now depends on the migrated UPS
    assert cfg.hosts[0].ups_ids == ["ups1"]


def test_config_ignores_legacy_smtp_key(tmp_path):
    # Pre-3.0 configs carry a `notifications.smtp` block; it must load without error
    # and disappear from the file on the next save (e-mail was removed in 3.0.0).
    import yaml
    path = tmp_path / "config.yaml"
    old = {
        "notifications": {
            "smtp": {"enabled": True, "server": "mail.example", "recipients": ["a@b"]},
            "webhook": {"enabled": True, "url": "https://hook.example/x"},
        },
    }
    path.write_text(yaml.safe_dump(old), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.notifications.webhook.enabled is True
    assert cfg.notifications.webhook.url == "https://hook.example/x"
    save_config(cfg, path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "smtp" not in raw["notifications"]
    assert raw["notifications"]["webhook"]["url"] == "https://hook.example/x"


def test_default_config_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert cfg.configured is False
    assert cfg.dry_run is True  # safe default


def test_merge_config_reconciles_ups_secrets_by_id():
    from app import main
    existing = AppConfig(ups=[SnmpConfig(id="a", host="10.0.0.1", community="keep")])
    incoming = {
        "ups": [{"id": "a", "host": "10.0.0.1", "community": main.SECRET_PLACEHOLDER}],
        "hosts": [],
    }
    merged = main._merge_config(incoming, existing)
    assert merged.ups[0].community.get_secret_value() == "keep"  # placeholder kept old secret


# --- ordered_hosts: own host last ------------------------------------------
def test_ordered_hosts_puts_this_host_last():
    cfg = AppConfig(hosts=[
        HostConfig(name="self", api_url="x", this_host=True, order=0),
        HostConfig(name="a", api_url="x", order=5),
        HostConfig(name="b", api_url="x", order=1),
    ])
    order = [h.name for h in cfg.ordered_hosts()]
    assert order == ["b", "a", "self"]


# --- per-UPS trigger logic --------------------------------------------------
def _ups_engine(th: Thresholds) -> Engine:
    """Engine with a single UPS 'u' (no hosts) for testing the per-UPS trigger decision."""
    return Engine(AppConfig(ups=[SnmpConfig(id="u", host="10.0.0.9")], thresholds=th))


def _reason(eng: Engine, uid: str = "u"):
    return eng._ups_trigger_reason(eng.cfg.ups_by_id(uid), eng.ups_rt[uid])


def test_trigger_on_low_runtime():
    eng = _ups_engine(Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                                 charge_below_percent=None, on_battery_low=False))
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery", runtime_remaining_min=3)
    assert _reason(eng) is not None


def test_trigger_on_low_charge():
    eng = _ups_engine(Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                                 charge_below_percent=30, on_battery_low=False))
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery", battery_charge_pct=20)
    assert _reason(eng) is not None


def test_trigger_on_battery_low_flag():
    eng = _ups_engine(Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                                 charge_below_percent=None, on_battery_low=True))
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery", battery_status="low")
    assert _reason(eng) is not None


def test_trigger_on_battery_seconds_uses_own_timer():
    eng = _ups_engine(Thresholds(on_battery_seconds=120, runtime_below_minutes=None,
                                 charge_below_percent=None, on_battery_low=False))
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")  # no UPS counter
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=130)
    assert _reason(eng) is not None


def test_no_trigger_when_healthy():
    eng = _ups_engine(Thresholds())
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     runtime_remaining_min=60, battery_charge_pct=100)
    assert _reason(eng) is None


# --- multi-UPS host policy (AND/OR) -----------------------------------------
def _multi_engine(policy: str, *, dry_run=True, ups_ids=("a", "b")) -> Engine:
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                    charge_below_percent=None, on_battery_low=False)
    cfg = AppConfig(
        dry_run=dry_run,
        ups=[SnmpConfig(id="a", name="A", host="10.0.0.1"),
             SnmpConfig(id="b", name="B", host="10.0.0.2")],
        hosts=[HostConfig(name="pve01", api_url="x", ups_ids=list(ups_ids), ups_policy=policy)],
        thresholds=th,
    )
    return Engine(cfg)


def _on_battery_low_runtime(eng, uid):
    eng.ups_rt[uid].state = UpsState(reachable=True, power_source="battery", runtime_remaining_min=3)


def _on_mains(eng, uid):
    eng.ups_rt[uid].state = UpsState(reachable=True, power_source="mains", runtime_remaining_min=60)


@pytest.mark.asyncio
async def test_and_policy_waits_for_all_feeds():
    # Redundant host: only one of two UPS critical -> NO shutdown.
    eng = _multi_engine("all")
    _on_battery_low_runtime(eng, "a")
    _on_mains(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve01") in (None, False)
    assert eng.shutdown_triggered is False
    # now the second UPS also goes critical -> shutdown fires
    _on_battery_low_runtime(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve01") is True
    assert eng.shutdown_triggered is True


@pytest.mark.asyncio
async def test_or_policy_fires_on_first_feed():
    eng = _multi_engine("any")
    _on_battery_low_runtime(eng, "a")
    _on_mains(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve01") is True
    assert eng.shutdown_triggered is True


@pytest.mark.asyncio
async def test_single_ups_host_behaves_like_before():
    # Regression: a host fed by exactly one UPS shuts down when that UPS triggers.
    eng = _multi_engine("all", ups_ids=("a",))
    _on_battery_low_runtime(eng, "a")
    _on_mains(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve01") is True


@pytest.mark.asyncio
async def test_and_policy_abort_when_feed_recovers():
    # A dry-run latched host is released when a required feed returns to mains.
    eng = _multi_engine("all")
    _on_battery_low_runtime(eng, "a")
    _on_battery_low_runtime(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve01") is True
    _on_mains(eng, "a")  # one feed recovers
    await eng._evaluate()
    assert eng.host_fired.get("pve01") is False  # latch released (abort)
    assert eng.shutdown_triggered is False


@pytest.mark.asyncio
async def test_eligible_hosts_shut_down_this_host_last(monkeypatch):
    from app import proxmox
    order: list[str] = []

    async def fake_shutdown(host, timeout=60):
        order.append(host.name)
        return True, "ok"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                    charge_below_percent=None, on_battery_low=False)
    cfg = AppConfig(
        dry_run=False,
        ups=[SnmpConfig(id="a", host="10.0.0.1")],
        hosts=[
            HostConfig(name="self", api_url="x", this_host=True, ups_ids=["a"]),
            HostConfig(name="other", api_url="x", order=1, ups_ids=["a"]),
        ],
        thresholds=th,
    )
    eng = Engine(cfg)
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="battery", runtime_remaining_min=3)
    await eng._evaluate()
    assert order == ["other", "self"]  # appliance host last


@pytest.mark.asyncio
async def test_per_ups_override_changes_only_that_ups():
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                    charge_below_percent=None, on_battery_low=False)
    cfg = AppConfig(
        dry_run=True,
        ups=[SnmpConfig(id="a", host="10.0.0.1",
                        overrides=UpsThresholdOverride(runtime_below_minutes=2)),
             SnmpConfig(id="b", host="10.0.0.2")],
        hosts=[HostConfig(name="ha", api_url="x", ups_ids=["a"]),
               HostConfig(name="hb", api_url="x", ups_ids=["b"])],
        thresholds=th,
    )
    eng = Engine(cfg)
    # runtime 3 min: below global (5) but above the per-UPS override (2) for UPS a
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="battery", runtime_remaining_min=3)
    eng.ups_rt["b"].state = UpsState(reachable=True, power_source="battery", runtime_remaining_min=3)
    await eng._evaluate()
    assert eng.host_fired.get("ha") in (None, False)  # a's stricter threshold not met
    assert eng.host_fired.get("hb") is True            # b uses global 5 -> met


# --- snapshot ---------------------------------------------------------------
def test_snapshot_ups_is_list_with_feeds():
    eng = _multi_engine("all")
    snap = eng.snapshot()
    assert isinstance(snap["ups"], list)
    assert {u["id"] for u in snap["ups"]} == {"a", "b"}
    host = snap["hosts"][0]
    assert host["ups_policy"] == "all"
    assert {f["id"] for f in host["feeds"]} == {"a", "b"}
    assert host["eligible"] is False


# --- new in v1.2.0 ----------------------------------------------------------
def test_config_roundtrip_new_fields(tmp_path):
    path = tmp_path / "c.yaml"
    cfg = AppConfig(ntp_server="pool.ntp.org", timezone="Europe/Berlin",
                    selftest_enabled=False, selftest_hour=3,
                    thresholds=Thresholds(comm_loss_shutdown_after_min=15))
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.ntp_server == "pool.ntp.org"
    assert loaded.timezone == "Europe/Berlin"
    assert loaded.selftest_enabled is False
    assert loaded.selftest_hour == 3
    assert loaded.thresholds.comm_loss_shutdown_after_min == 15


@pytest.mark.asyncio
async def test_no_shutdown_on_mains_even_with_low_charge():
    # Item 9: a low charge while on mains (UPS recharging) must never shut down.
    eng = _ups_engine(Thresholds(charge_below_percent=30, on_battery_seconds=None,
                                 runtime_below_minutes=None, on_battery_low=False))
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains", battery_charge_pct=10)
    await eng._evaluate()
    assert eng.shutdown_triggered is False
    assert eng.state == ONLINE


@pytest.mark.asyncio
async def test_unreachable_raises_alarm_not_shutdown():
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=1))
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()
    assert eng.alarm_active is True
    assert eng.state != SHUTTING_DOWN
    assert eng.shutdown_triggered is False


@pytest.mark.asyncio
async def test_dry_run_latches_and_does_not_shutdown():
    th = Thresholds(on_battery_seconds=1, runtime_below_minutes=None,
                    charge_below_percent=None, on_battery_low=False)
    cfg = AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    hosts=[HostConfig(name="pve01", api_url="x", ups_ids=["u"])],
                    thresholds=th)
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery", seconds_on_battery=10)
    await eng._evaluate()  # enters ON_BATTERY + fires dry-run
    assert eng.shutdown_triggered is True
    assert eng.host_fired.get("pve01") is True
    assert eng.state == SHUTDOWN_PENDING
    # No real host shutdown recorded because nothing was actually shut down.
    assert eng.host_states == {}


@pytest.mark.asyncio
async def test_comm_loss_does_not_shutdown_by_default():
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=1,
                                 comm_loss_shutdown_after_min=None, poll_interval_normal_s=30))
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    for _ in range(10):
        await eng._evaluate()
    assert eng.shutdown_triggered is False


@pytest.mark.asyncio
async def test_comm_loss_shutdown_when_configured():
    th = Thresholds(unreachable_alarm_after_polls=1, comm_loss_shutdown_after_min=1,
                    poll_interval_normal_s=30)
    cfg = AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    hosts=[HostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()  # arms the wall-clock timer; ~0 s elapsed
    assert eng.shutdown_triggered is False
    # Wall clock, not poll count: backdate the loss beyond the 1-min threshold.
    eng.ups_rt["u"].unreachable_since -= timedelta(seconds=70)
    await eng._evaluate()
    assert eng.shutdown_triggered is True
    assert eng.ups_rt["u"].comm_loss_fired is True


# --- comms loss WHILE ON BATTERY: do not abort the running countdown --------
def _comm_loss_battery_engine(**th_kw) -> Engine:
    th = Thresholds(on_battery_seconds=120, runtime_below_minutes=None,
                    charge_below_percent=None, on_battery_low=False,
                    unreachable_alarm_after_polls=1, **th_kw)
    # dry_run so the (real) shutdown path needs no Proxmox; a host depends on the UPS so a
    # shutdown can actually fire.
    cfg = AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    hosts=[HostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")  # comms dropped
    return eng


@pytest.mark.asyncio
async def test_comm_loss_on_battery_continues_countdown_and_fires():
    eng = _comm_loss_battery_engine()
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=130)
    await eng._evaluate()
    assert eng.shutdown_triggered is True


@pytest.mark.asyncio
async def test_comm_loss_on_battery_waits_until_countdown_elapses():
    eng = _comm_loss_battery_engine()
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=30)
    await eng._evaluate()
    assert eng.shutdown_triggered is False  # not due yet
    assert eng.alarm_active is True


@pytest.mark.asyncio
async def test_comm_loss_on_battery_alarm_does_not_claim_no_shutdown():
    eng = _comm_loss_battery_engine()
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=30)
    events: list[tuple[str, str]] = []

    async def rec(subject, body, severity):
        events.append((subject, body))

    eng._emit = rec  # type: ignore[assignment]
    await eng._evaluate()
    assert eng.shutdown_triggered is False
    assert any("countdown continues" in s for s, _ in events)
    assert all("NO shutdown" not in b for _, b in events)


@pytest.mark.asyncio
async def test_pure_comm_loss_alarm_still_says_no_shutdown():
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=1))
    events: list[tuple[str, str]] = []

    async def rec(subject, body, severity):
        events.append((subject, body))

    eng._emit = rec  # type: ignore[assignment]
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()
    assert any("NO shutdown" in b for _, b in events)
    assert eng._aggregate_comm_loss_s() is None  # opt-in not armed


@pytest.mark.asyncio
async def test_comm_loss_optin_alarm_announces_pending_shutdown():
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=1,
                                 comm_loss_shutdown_after_min=5, poll_interval_normal_s=30))
    events: list[tuple[str, str]] = []

    async def rec(subject, body, severity):
        events.append((subject, body))

    eng._emit = rec  # type: ignore[assignment]
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()  # first poll: alarm only, threshold (5 min) not yet reached
    assert eng.shutdown_triggered is False
    assert any("prolonged loss" in s for s, _ in events)
    assert all("NO shutdown" not in b for _, b in events)
    assert eng._aggregate_comm_loss_s() is not None


@pytest.mark.asyncio
async def test_comm_loss_on_battery_respects_option_off():
    eng = _comm_loss_battery_engine(keep_shutdown_on_comm_loss=False)
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=130)
    await eng._evaluate()
    assert eng.shutdown_triggered is False  # opted out -> stays fail-safe


# --- an already fired trigger is never downgraded ----------------------------
@pytest.mark.asyncio
async def test_immediate_trigger_fires_during_running_countdown():
    # A running on_battery_seconds countdown must not delay battery low & Co.
    th = Thresholds(on_battery_seconds=600, runtime_below_minutes=None,
                    charge_below_percent=30, on_battery_low=True)
    cfg = AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    hosts=[HostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_charge_pct=80)
    await eng._evaluate()
    assert eng.shutdown_triggered is False  # countdown running, nothing critical yet
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_status="low", battery_charge_pct=80)
    await eng._evaluate()
    assert eng.shutdown_triggered is True
    assert "reports" in (eng.ups_rt["u"].trigger_reason or "")  # battery-low reason, not timer


@pytest.mark.asyncio
async def test_countdown_hidden_once_ups_triggered():
    # Once a UPS has triggered (battery low & Co.), the time-based countdown is moot and
    # must vanish from the status (UPS card, banner) — it previously kept ticking and
    # suggested the shutdown would wait for it.
    th = Thresholds(on_battery_seconds=600, runtime_below_minutes=None,
                    charge_below_percent=None, on_battery_low=True)
    cfg = AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    hosts=[HostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_charge_pct=80)
    await eng._evaluate()
    snap = eng.snapshot()  # countdown running, nothing critical yet
    assert snap["ups"][0]["countdown_remaining_s"] is not None
    assert snap["shutdown"]["countdown_remaining_s"] is not None

    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_status="depleted", battery_charge_pct=80)
    await eng._evaluate()
    snap = eng.snapshot()
    assert eng.ups_rt["u"].triggered is True
    assert snap["ups"][0]["countdown_remaining_s"] is None
    assert snap["shutdown"]["countdown_remaining_s"] is None


def _latched_trigger_engine() -> Engine:
    """Charge threshold only, no time countdown, blind countdown opted out — the harshest
    setup: before the latch, a comms loss dropped the fired trigger entirely."""
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                    charge_below_percent=30, on_battery_low=False,
                    unreachable_alarm_after_polls=1, keep_shutdown_on_comm_loss=False)
    return Engine(AppConfig(ups=[SnmpConfig(id="u", host="10.0.0.9")], thresholds=th))


@pytest.mark.asyncio
async def test_fired_trigger_stays_latched_on_comm_loss():
    eng = _latched_trigger_engine()
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_charge_pct=20)
    await eng._evaluate()
    reason = eng.ups_rt["u"].trigger_reason
    assert eng.ups_rt["u"].triggered is True and reason

    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()
    assert eng.ups_rt["u"].triggered is True  # blind = never downgrade
    assert eng.ups_rt["u"].trigger_reason == reason


@pytest.mark.asyncio
async def test_latched_trigger_clears_on_mains_return():
    eng = _latched_trigger_engine()
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_charge_pct=20)
    await eng._evaluate()
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()
    assert eng.ups_rt["u"].triggered is True

    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     battery_charge_pct=20)
    await eng._evaluate()
    assert eng.ups_rt["u"].triggered is False  # confirmed mains return releases the latch


@pytest.mark.asyncio
async def test_latched_trigger_persists_across_restart():
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                    charge_below_percent=30, on_battery_low=False,
                    unreachable_alarm_after_polls=1)
    cfg = AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    hosts=[HostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
    eng1 = Engine(cfg)
    eng1.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                      battery_charge_pct=20)
    await eng1._evaluate()
    reason = eng1.ups_rt["u"].trigger_reason
    assert reason

    # "Restart": the latch is re-armed together with the battery timer.
    eng2 = Engine(cfg)
    assert eng2.ups_rt["u"].triggered is True
    assert eng2.ups_rt["u"].trigger_reason == reason


def test_config_roundtrip_keep_shutdown_on_comm_loss(tmp_path):
    assert Thresholds().keep_shutdown_on_comm_loss is True  # default on
    path = tmp_path / "c.yaml"
    save_config(AppConfig(thresholds=Thresholds(keep_shutdown_on_comm_loss=False)), path)
    assert load_config(path).thresholds.keep_shutdown_on_comm_loss is False


@pytest.mark.asyncio
async def test_network_transitions_are_logged():
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=99))
    events: list[str] = []

    async def rec(subject, body, severity):
        events.append(subject)

    eng._emit = rec  # type: ignore[assignment]
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    await eng._evaluate()  # first poll: no transition (last is None)
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()  # -> lost
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    await eng._evaluate()  # -> restored
    assert any("connection lost" in s for s in events)
    assert any("connection restored" in s for s in events)


# --- SNMP engine lifecycle (v1.8.3): no file-descriptor leak ----------------
def test_close_engine_handles_both_apis_and_never_raises():
    from app import ups

    # pysnmp 7.x style: engine.close_dispatcher()
    class Seven:
        def __init__(self):
            self.closed = False

        def close_dispatcher(self):
            self.closed = True

    seven = Seven()
    ups._close_engine(seven)
    assert seven.closed is True

    # pysnmp 6.x style: engine.transportDispatcher.closeDispatcher()
    class Dispatcher:
        def __init__(self):
            self.closed = False

        def closeDispatcher(self):
            self.closed = True

    class Six:
        def __init__(self):
            self.transportDispatcher = Dispatcher()

    six = Six()
    ups._close_engine(six)
    assert six.transportDispatcher.closed is True

    # None and a raising closer must both be swallowed (the poller may never crash).
    class Boom:
        def close_dispatcher(self):
            raise RuntimeError("nope")

    ups._close_engine(None)
    ups._close_engine(Boom())  # must not raise


@pytest.mark.asyncio
async def test_poll_closes_engine_even_when_unreachable(monkeypatch):
    """Every poll must release its SnmpEngine — especially on the common timeout path,
    which would otherwise leak a UDP socket per poll and exhaust the fd limit."""
    from app import ups
    from app.config import SnmpConfig

    closed: list = []
    monkeypatch.setattr(ups, "_close_engine", lambda eng: closed.append(eng))

    # Point at a port with no SNMP responder so the poll fails fast (reachable=False).
    cfg = SnmpConfig(host="127.0.0.1", port=1, timeout_s=0.1, retries=0)
    state = await ups.poll(cfg)

    assert state.reachable is False  # nothing answered
    assert len(closed) == 1  # the engine was handed to the closer exactly once
    assert closed[0] is not None


def test_clear_events(tmp_path):
    from app import db
    path = tmp_path / "events.db"
    db.init_db(path)
    db.log_event("a", "x", db.INFO, path)
    db.log_event("b", "y", db.WARNING, path)
    assert len(db.recent_events(path=path)) == 2
    assert db.clear_events(path) == 2
    assert db.recent_events(path=path) == []


def test_events_since_filters_window_and_counts(tmp_path):
    from app import db
    path = tmp_path / "events.db"
    db.init_db(path)
    db.log_event("recent", "", db.WARNING, path)
    # Inject an event older than 48 h directly (log_event always uses 'now').
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with db._connect(path) as conn:
        conn.execute(
            "INSERT INTO events (ts, severity, event, detail) VALUES (?, ?, ?, ?)",
            (old_ts, db.CRITICAL, "old", ""),
        )
        conn.commit()

    names = [e["event"] for e in db.events_since(48, path=path)]
    assert "recent" in names and "old" not in names

    counts = db.severity_counts_since(48, path=path)
    assert counts[db.WARNING] == 1
    assert counts[db.CRITICAL] == 0  # the 72 h-old critical is outside the window


# --- updater reliability (v1.5.0) -------------------------------------------
def test_enqueue_agent_writes_final_file_atomically(tmp_path, monkeypatch):
    import json
    from app import main

    agent_dir = tmp_path / "agent"
    queue = agent_dir / "queue"
    monkeypatch.setattr(main, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(main, "AGENT_QUEUE", queue)

    job_id = main._enqueue_agent("update", package="/x/p.tgz")

    files = list(queue.iterdir())
    # Exactly the final job file; no leftover .tmp inside the watched queue dir.
    assert [f.name for f in files] == [f"{job_id}.json"]
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["job_id"] == job_id
    assert data["action"] == "update"
    assert data["package"] == "/x/p.tgz"


def test_agent_drainer_active_never_raises(monkeypatch):
    from app import main

    # systemctl absent / non-Linux dev box: must degrade gracefully, never raise.
    def boom(*a, **k):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(main.subprocess, "run", boom)
    # Force the unit-file fallback to a known answer.
    monkeypatch.setattr(main, "AGENT_TIMER_UNIT", main.Path("/no/such/timer"))
    assert main._agent_drainer_active() is False  # missing unit file -> not active


def test_read_package_version_from_tar_and_zip(tmp_path):
    import io
    import tarfile
    import zipfile

    from app import main

    init = b'__version__ = "9.9.9"\n'

    tgz = tmp_path / "pkg.tar.gz"
    with tarfile.open(tgz, "w:gz") as t:  # with a prefix dir, like git archive produces
        info = tarfile.TarInfo("pve-usv/app/__init__.py")
        info.size = len(init)
        t.addfile(info, io.BytesIO(init))
    assert main._read_package_version(tgz) == "9.9.9"

    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("app/__init__.py", init.decode())
    assert main._read_package_version(z) == "9.9.9"


# --- SNMP v1 -----------------------------------------------------------------
def test_snmp_v1_roundtrip_and_message_processing_model(tmp_path):
    from app.ups import _auth_data

    path = tmp_path / "c.yaml"
    save_config(AppConfig(ups=[SnmpConfig(id="u", host="10.0.0.9",
                                          version=SnmpVersion.v1)]), path)
    loaded = load_config(path)
    assert loaded.ups[0].version == SnmpVersion.v1

    # v1 must go on the wire as model 0, v2c as model 1. pysnmp 6.x exposes the
    # attribute as `mpModel`, 7.x as `message_processing_model`.
    def mp(auth):
        for attr in ("mpModel", "message_processing_model"):
            v = getattr(auth, attr, None)
            if v is not None:
                return v
        raise AssertionError("no message processing model attribute found")

    assert mp(_auth_data(loaded.ups[0])) == 0
    assert mp(_auth_data(SnmpConfig(host="x", version=SnmpVersion.v2c))) == 1


# --- battery timer survives a service restart --------------------------------
@pytest.mark.asyncio
async def test_battery_timer_persists_and_restores_across_restart():
    from app import engine as engine_mod

    th = Thresholds(on_battery_seconds=600, runtime_below_minutes=None,
                    charge_below_percent=None, on_battery_low=False,
                    unreachable_alarm_after_polls=1)
    cfg = AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    hosts=[HostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)

    eng1 = Engine(cfg)
    eng1.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")
    await eng1._evaluate()
    since = eng1.ups_rt["u"].on_battery_since
    assert since is not None
    assert engine_mod.STATE_PATH.exists()  # timer was persisted

    # "Restart": a fresh engine restores the timer, and the blind countdown (UPS now
    # unreachable) keeps running and fires once it elapses.
    eng2 = Engine(cfg)
    assert eng2.ups_rt["u"].on_battery_since == since
    eng2.ups_rt["u"].on_battery_since = since - timedelta(seconds=700)
    eng2.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng2._evaluate()
    assert eng2.shutdown_triggered is True


@pytest.mark.asyncio
async def test_battery_timer_state_file_cleared_on_mains():
    from app import engine as engine_mod
    import json as _json

    cfg = AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")])
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")
    await eng._evaluate()
    assert _json.loads(engine_mod.STATE_PATH.read_text())["on_battery_since"]

    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    await eng._evaluate()
    assert _json.loads(engine_mod.STATE_PATH.read_text())["on_battery_since"] == {}


def test_stale_battery_timer_is_not_restored():
    from app import engine as engine_mod
    import json as _json

    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    engine_mod.STATE_PATH.write_text(
        _json.dumps({"on_battery_since": {"u": old}}), encoding="utf-8")
    eng = Engine(AppConfig(ups=[SnmpConfig(id="u", host="10.0.0.9")]))
    assert eng.ups_rt["u"].on_battery_since is None  # older than 24 h -> discarded


def test_ingest_agent_result_logs_exactly_once(tmp_path, monkeypatch):
    import json

    from app import main

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    result = agent_dir / "result.json"
    seen = agent_dir / "result.seen"
    monkeypatch.setattr(main, "AGENT_RESULT", result)
    monkeypatch.setattr(main, "AGENT_SEEN", seen)

    events: list = []
    monkeypatch.setattr(main.db, "log_event", lambda *a, **k: events.append(a))

    result.write_text(json.dumps({
        "job_id": "J1", "ok": True, "message": "m",
        "version_before": "1.4.0", "version_after": "1.5.0",
    }), encoding="utf-8")

    main._ingest_agent_result()
    main._ingest_agent_result()  # second call must be a no-op (seen marker)

    assert len(events) == 1
    assert seen.read_text(encoding="utf-8") == "J1"

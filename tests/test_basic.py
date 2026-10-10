"""Unit tests for config persistence and the engine trigger logic.

Run with:  pytest
These tests need no UPS hardware and no network.
"""

import os
from datetime import datetime, timedelta, timezone
from itertools import groupby

import pytest

from app.config import (
    AppConfig,
    Notifications,
    NutConfig,
    PbsHostConfig,
    PveHostConfig,
    SnmpConfig,
    SnmpMib,
    SnmpVersion,
    Thresholds,
    UpsThresholdOverride,
    WebhookConfig,
    load_config,
    save_config,
)
from app import db
from app.engine import ON_BATTERY, ONLINE, SHUTDOWN_PENDING, SHUTTING_DOWN, Engine
from app.ups import UpsState


@pytest.fixture(autouse=True)
def _isolated_engine_state(tmp_path, monkeypatch):
    """Point the engine's battery-timer state file at a per-test path, so tests never
    read/write a real (or another test's) engine-state.json."""
    from app import engine as engine_mod
    from app import history as history_mod
    monkeypatch.setattr(engine_mod, "STATE_PATH", tmp_path / "engine-state.json")
    # Same for the history (#37): a test that runs the loop must not create a real
    # /var/lib/pve-usv/history.db.
    monkeypatch.setattr(history_mod, "HISTORY_PATH", tmp_path / "history.db")


# --- config round-trip ------------------------------------------------------
def test_config_roundtrip_keeps_secrets(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(
        ups=[SnmpConfig(id="ups1", name="USV A", host="10.0.0.9",
                        version=SnmpVersion.v2c, community="topsecret")],
        hosts=[PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006",
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
        hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["a", "b"], ups_policy="all")],
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


def test_config_defaults_untyped_ups_entries_to_snmp(tmp_path):
    """Pre-3.2 configs have no ``type``; the discriminated union would reject them."""
    import yaml

    path = tmp_path / "config.yaml"
    old = {
        "ups": [{"id": "a", "name": "A", "host": "10.0.0.1", "community": "sec"}],
        "hosts": [{"name": "pve01", "api_url": "https://x:8006", "ups_ids": ["a"]}],
    }
    path.write_text(yaml.safe_dump(old), encoding="utf-8")
    cfg = load_config(path)
    assert isinstance(cfg.ups[0], SnmpConfig)
    assert cfg.ups[0].type == "snmp"
    assert cfg.ups[0].community.get_secret_value() == "sec"
    # ... and the type is written back explicitly on the next save.
    save_config(cfg, path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["ups"][0]["type"] == "snmp"


def test_config_defaults_untyped_hosts_to_pve(tmp_path):
    """Pre-3.5 hosts have no ``type``; PVE was the only shutdown target back then."""
    import yaml

    path = tmp_path / "config.yaml"
    old = {"hosts": [{"name": "pve01", "api_url": "https://x:8006", "token_id": "u!t"}]}
    path.write_text(yaml.safe_dump(old), encoding="utf-8")
    cfg = load_config(path)
    assert isinstance(cfg.hosts[0], PveHostConfig)
    assert cfg.hosts[0].type == "pve"
    # ... and the type is written back explicitly on the next save.
    save_config(cfg, path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["hosts"][0]["type"] == "pve"


def test_pre_3_5_config_yaml_loads_unchanged(tmp_path):
    """A full 3.4.0 config must survive the update field for field.

    The host type is the only thing that may appear; nothing else about an existing
    installation is allowed to shift when the appliance is updated underneath it.
    """
    import yaml

    path = tmp_path / "config.yaml"
    old = {
        "configured": True,
        "dry_run": False,
        "ups": [{"id": "ups1", "type": "snmp", "name": "USV A", "host": "10.0.0.9",
                 "version": "v2c", "community": "snmp-secret", "mib": "apc"}],
        "hosts": [
            {"name": "pve01", "api_url": "https://10.0.0.10:8006", "method": "api_token",
             "token_id": "ups@pve!shutdown", "token_secret": "secret-1",
             "verify_tls": False, "this_host": False, "order": 2, "enabled": True,
             "ups_ids": ["ups1"], "ups_policy": "all"},
            {"name": "pve02", "api_url": "https://10.0.0.11:8006", "method": "api_token",
             "token_id": "ups@pve!shutdown", "token_secret": "secret-2",
             "verify_tls": True, "this_host": True, "order": 0, "enabled": False,
             "ups_ids": [], "ups_policy": "any"},
        ],
        "thresholds": {"runtime_below_minutes": 7, "host_shutdown_timeout_s": 45},
        "selftest_enabled": True, "selftest_hour": 3, "selftest_interval_min": 360,
    }
    path.write_text(yaml.safe_dump(old), encoding="utf-8")

    cfg = load_config(path)
    assert cfg.configured is True and cfg.dry_run is False
    assert cfg.thresholds.runtime_below_minutes == 7
    assert cfg.thresholds.host_shutdown_timeout_s == 45
    assert cfg.selftest_hour == 3 and cfg.selftest_interval_min == 360
    assert cfg.ups[0].community.get_secret_value() == "snmp-secret"
    for old_host, host in zip(old["hosts"], cfg.hosts):
        assert host.type == "pve"
        assert host.token_secret.get_secret_value() == old_host["token_secret"]
        for field in ("name", "api_url", "token_id", "verify_tls", "this_host",
                      "order", "enabled", "ups_ids", "ups_policy"):
            assert getattr(host, field) == old_host[field], field
    # The node path a PVE shutdown uses is still the node name.
    assert cfg.hosts[0].api_node == "pve01"


def test_pre_2_0_snmp_config_still_migrates_with_typed_hosts(tmp_path):
    """The oldest schema (single ``snmp`` block, hosts without ups_ids *and* without
    type) has to pass both migrations at once."""
    import yaml

    path = tmp_path / "config.yaml"
    old = {
        "snmp": {"host": "10.0.0.9", "community": "sec"},
        "hosts": [{"name": "pve01", "api_url": "https://x:8006"}],
    }
    path.write_text(yaml.safe_dump(old), encoding="utf-8")

    cfg = load_config(path)
    assert isinstance(cfg.ups[0], SnmpConfig) and cfg.ups[0].id == "ups1"
    assert isinstance(cfg.hosts[0], PveHostConfig)
    assert cfg.hosts[0].ups_ids == ["ups1"]


def test_config_rejects_an_unknown_host_type(tmp_path):
    """Better a clear error than silently treating an unknown product as PVE and
    firing a PVE token at it."""
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(
        {"hosts": [{"name": "x", "api_url": "https://x:8007", "type": "pmg"}]}
    ), encoding="utf-8")
    with pytest.raises(Exception):
        load_config(path)


def test_config_roundtrip_mixes_pve_and_pbs_targets(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(hosts=[
        PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006", token_secret="a"),
        PbsHostConfig(name="Backup-Server", api_url="https://10.0.0.20:8007",
                      token_id="ups@pbs!shutdown", token_secret="b", order=1),
    ])
    save_config(cfg, path)
    loaded = load_config(path)

    assert isinstance(loaded.hosts[0], PveHostConfig)
    assert isinstance(loaded.hosts[1], PbsHostConfig)
    assert loaded.hosts[1].token_secret.get_secret_value() == "b"
    # PBS ignores the {node} segment, so the free label never reaches the API path.
    assert loaded.hosts[1].api_node == "localhost"
    assert loaded.hosts[0].api_node == "pve01"


def test_host_key_separates_targets_of_the_same_name():
    """Runtime latches are keyed by the stable id, never by anything the user edits."""
    from app.config import assign_host_ids

    pve = PveHostConfig(name="backup", api_url="x")
    pbs = PbsHostConfig(name="backup", api_url="x")
    # Until ids are assigned, the pre-id form keeps the key non-empty and distinct.
    assert pve.key != pbs.key
    assert pve.key == "pve:backup" and pbs.key == "pbs:backup"

    assign_host_ids([pve, pbs])
    assert pve.key == pve.id and pbs.key == pbs.id
    assert pve.key != pbs.key
    # And the key survives what the pre-id scheme could not: a rename.
    before = pve.key
    pve.name = "backup-01"
    assert pve.key == before


def test_two_entries_with_the_same_name_do_not_share_a_shutdown_latch():
    """A duplicated row with the IP not adjusted used to collide: both entries produced
    the key "pve:pve01", so the second was treated as already fired and stayed up."""
    from app.config import assign_host_ids

    a = PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006")
    b = PveHostConfig(name="pve01", api_url="https://10.0.0.11:8006")
    assign_host_ids([a, b])
    assert a.key != b.key


def test_assign_host_ids_is_stable_collision_free_and_not_ups_flavoured():
    from app.config import assign_host_ids

    hosts = [
        PveHostConfig(id="kept", name="pve01", api_url="x"),
        PveHostConfig(name="pve01", api_url="x"),   # same name, needs its own id
        PveHostConfig(name="!!!", api_url="x"),     # slugifies to nothing
        PbsHostConfig(name="", api_url="x"),        # no name at all
    ]
    assign_host_ids(hosts)
    ids = [h.id for h in hosts]

    assert ids[0] == "kept"                 # an existing id is never rewritten
    assert ids[1] == "pve01"
    assert len(set(ids)) == len(ids)        # collision-free
    # _slugify's default fallback is "ups"; a host must not inherit it.
    assert "ups" not in ids




def test_config_defaults_the_mib_of_pre_3_3_entries_to_auto(tmp_path):
    """A config written before MIB profiles must pick up "auto", so an existing APC UPS
    starts using its vendor MIB after an update without anyone touching the wizard."""
    import yaml

    path = tmp_path / "config.yaml"
    old = {"ups": [{"id": "a", "type": "snmp", "host": "10.0.0.1"}]}
    path.write_text(yaml.safe_dump(old), encoding="utf-8")

    cfg = load_config(path)
    assert cfg.ups[0].mib == SnmpMib.auto

    save_config(cfg, path)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["ups"][0]["mib"] == "auto"


def test_config_roundtrip_keeps_an_explicit_mib(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(ups=[SnmpConfig(id="a", host="10.0.0.1", mib=SnmpMib.apc)])
    save_config(cfg, path)

    assert load_config(path).ups[0].mib == SnmpMib.apc


def test_config_roundtrip_mixes_snmp_and_nut_sources(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig(
        ups=[
            SnmpConfig(id="a", name="Card", host="10.0.0.1", community="ca"),
            NutConfig(id="b", name="USB", host="10.0.0.2", ups_name="ups",
                      username="monitor", password="np",
                      overrides=UpsThresholdOverride(charge_below_percent=25)),
        ],
        hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["a", "b"], ups_policy="any")],
    )
    save_config(cfg, path)
    loaded = load_config(path)

    assert [type(u).__name__ for u in loaded.ups] == ["SnmpConfig", "NutConfig"]
    assert loaded.ups[0].community.get_secret_value() == "ca"
    assert loaded.ups[1].password.get_secret_value() == "np"
    assert loaded.ups[1].port == 3493
    assert loaded.ups[1].configured is True
    # Per-UPS overrides work the same regardless of the source type.
    assert loaded.effective_thresholds(loaded.ups[1]).charge_below_percent == 25


def test_nut_source_is_unconfigured_without_a_ups_name():
    assert NutConfig(host="10.0.0.2").configured is False
    assert NutConfig(ups_name="ups").configured is False
    assert NutConfig(host="10.0.0.2", ups_name="ups").configured is True
    # Label falls back to something recognisable in events when no name is set.
    assert NutConfig(host="10.0.0.2", ups_name="ups").label == "ups@10.0.0.2"


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
    # The single pre-4.0 webhook block is migrated into the one-element list.
    assert len(cfg.notifications.webhooks) == 1
    assert cfg.notifications.webhooks[0].enabled is True
    assert cfg.notifications.webhooks[0].url == "https://hook.example/x"
    save_config(cfg, path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "smtp" not in raw["notifications"]
    # ... and the file is rewritten in the current shape: a list, no legacy key left.
    assert "webhook" not in raw["notifications"]
    assert raw["notifications"]["webhooks"][0]["url"] == "https://hook.example/x"
    # A pre-3.4 webhook block has neither key; both must appear with their defaults, and
    # as plain strings (an Enum would not survive yaml.safe_dump).
    assert raw["notifications"]["webhooks"][0]["format"] == "json"
    assert raw["notifications"]["webhooks"][0]["min_severity"] == "warning"
    # The migration also gives the entry the id the per-webhook secret reconcile needs.
    assert raw["notifications"]["webhooks"][0]["id"]


# --- out-of-range numbers are corrected, never rejected ------------------------------
def test_a_stored_config_with_impossible_numbers_still_loads(tmp_path):
    """The load path has no guard: rejecting would mean the service never starts again.

    load_config() is called straight from the FastAPI lifespan and does not catch
    ValidationError, so a Field(ge=...) here would turn one bad stored number into an
    appliance that no longer comes up — a far worse failure than the number itself.
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        """
thresholds:
  poll_interval_battery_s: -1
  poll_interval_normal_s: 0
  charge_below_percent: 150
  host_shutdown_timeout_s: -30
ups:
  - id: u
    type: snmp
    host: 10.0.0.9
    port: 0
    timeout_s: -3
""",
        encoding="utf-8",
    )
    cfg = load_config(path)            # must not raise

    assert cfg.thresholds.poll_interval_battery_s == 8      # back to the default
    assert cfg.thresholds.poll_interval_normal_s == 30
    assert cfg.thresholds.charge_below_percent == 30
    assert cfg.thresholds.host_shutdown_timeout_s == 60
    assert cfg.ups[0].port == 161
    assert cfg.ups[0].timeout_s == 3.0
    # And it is loud about every one of them.
    lines = " ".join(cfg.value_corrections())
    for field in ("poll_interval_battery_s", "charge_below_percent", "port", "timeout_s"):
        assert field in lines


def test_a_per_ups_override_is_bounded_at_both_ends_like_the_global_thresholds():
    """The overrides used to be bounded only from below, while Thresholds was bounded at
    both ends — so the correction this release advertises applied to every threshold
    except the ones actually set per device, which is exactly where a slipped digit hides:
    one card among several, and nothing else in the interface shows the number again."""
    from app.config import UpsThresholdOverride

    ov = UpsThresholdOverride(on_battery_seconds=999999, runtime_below_minutes=99999,
                              comm_loss_shutdown_after_min=99999)
    # Back to the field default, which for an override means "inherit the global value" —
    # the safe direction: the estate-wide setting takes over rather than a number nobody
    # chose staying in force.
    assert ov.on_battery_seconds is None
    assert ov.runtime_below_minutes is None
    assert ov.comm_loss_shutdown_after_min is None
    assert len(ov._corrections) == 3

    # And it reaches the report the engine reads, in the same words as a global one.
    cfg = AppConfig(ups=[SnmpConfig(
        id="u", name="Rack A", host="10.0.0.9",
        overrides=UpsThresholdOverride(on_battery_seconds=999999))])
    lines = " ".join(cfg.value_corrections())
    assert "override" in lines and "on_battery_seconds" in lines

    # A value a real estate might use still passes untouched.
    sane = UpsThresholdOverride(on_battery_seconds=900, runtime_below_minutes=15)
    assert sane.on_battery_seconds == 900 and sane.runtime_below_minutes == 15
    assert sane._corrections == []


def test_a_healthy_config_reports_no_corrections():
    assert AppConfig().value_corrections() == []
    assert AppConfig(thresholds=Thresholds(charge_below_percent=0)).value_corrections() == []


def test_none_means_off_and_is_never_corrected():
    """Optional thresholds are switched off with None — that is not an out-of-range value."""
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                    charge_below_percent=None, comm_loss_shutdown_after_min=None)
    assert th.on_battery_seconds is None
    assert th._corrections == []


def test_corrections_never_reach_the_saved_file(tmp_path):
    """The bookkeeping is a private attribute: it must not turn into a config key."""
    path = tmp_path / "config.yaml"
    save_config(AppConfig(thresholds=Thresholds(poll_interval_battery_s=-1)), path)
    text = path.read_text(encoding="utf-8")
    assert "_corrections" not in text
    assert "poll_interval_battery_s: 8" in text


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


def test_merge_config_reconciles_webhook_auth_header_by_id():
    """A masked auth header must survive a save, or the token is lost on every edit."""
    from app import main
    from app.config import Notifications, WebhookConfig

    existing = AppConfig(notifications=Notifications(webhooks=[
        WebhookConfig(id="w1", url="https://ntfy/x", auth_header_name="Authorization",
                      auth_header_value="Bearer keep-me"),
    ]))
    incoming = {
        "ups": [], "hosts": [],
        "notifications": {"webhooks": [{
            "id": "w1", "url": "https://ntfy/x", "auth_header_name": "Authorization",
            "auth_header_value": main.SECRET_PLACEHOLDER,
        }]},
    }
    merged = main._merge_config(incoming, existing)
    assert merged.notifications.webhooks[0].auth_header_value.get_secret_value() == "Bearer keep-me"

    # A genuinely new value replaces it, and a brand-new webhook starts empty.
    incoming["notifications"]["webhooks"][0]["auth_header_value"] = "Bearer new"
    incoming["notifications"]["webhooks"].append({"url": "https://other/x"})
    merged = main._merge_config(incoming, existing)
    assert merged.notifications.webhooks[0].auth_header_value.get_secret_value() == "Bearer new"
    assert merged.notifications.webhooks[1].auth_header_value.get_secret_value() == ""
    # ... and the new entry is given an id, which is what the reconcile matches on.
    assert merged.notifications.webhooks[1].id


def test_webhook_secrets_never_leave_through_the_api():
    """The sanitized config must mask the auth header, like every other secret."""
    from app import main
    from app.config import Notifications, WebhookConfig

    cfg = AppConfig(notifications=Notifications(webhooks=[
        WebhookConfig(id="w1", url="https://ntfy/x", auth_header_value="Bearer top-secret"),
    ]))
    data = main._sanitized_config(cfg)
    assert data["notifications"]["webhooks"][0]["auth_header_value"] == main.SECRET_PLACEHOLDER
    assert "top-secret" not in str(data)


def test_merge_config_reconciles_nut_password_and_ignores_the_other_type():
    """Each source type declares its own secrets; switching type must not inherit any."""
    from app import main

    existing = AppConfig(
        ups=[
            NutConfig(id="n", host="10.0.0.2", ups_name="ups", password="keepme"),
            SnmpConfig(id="s", host="10.0.0.1", community="oldcomm"),
        ]
    )
    incoming = {
        "ups": [
            {"id": "n", "type": "nut", "host": "10.0.0.2", "ups_name": "ups",
             "password": main.SECRET_PLACEHOLDER},
            # Same id, but now a NUT source: the stored SNMP community is irrelevant and
            # the empty password must fall back to the default, not to anything stored.
            {"id": "s", "type": "nut", "host": "10.0.0.3", "ups_name": "ups", "password": ""},
        ],
        "hosts": [],
    }
    merged = main._merge_config(incoming, existing)
    assert merged.ups[0].password.get_secret_value() == "keepme"
    assert merged.ups[1].password.get_secret_value() == ""


# --- ordered_hosts: own host last ------------------------------------------
def test_ordered_hosts_puts_this_host_last():
    cfg = AppConfig(hosts=[
        PveHostConfig(name="self", api_url="x", this_host=True, order=0),
        PveHostConfig(name="a", api_url="x", order=5),
        PveHostConfig(name="b", api_url="x", order=1),
    ])
    order = [h.name for h in cfg.ordered_hosts()]
    assert order == ["b", "a", "self"]


def test_shutdown_order_is_ascending_and_this_host_overrides_it():
    """The contract the wizard's sequence preview visualises: 0 goes first, equal
    numbers form one stage, and "this host" is last whatever number it carries."""
    cfg = AppConfig(hosts=[
        PveHostConfig(name="late", api_url="x", order=9),
        PbsHostConfig(name="early-b", api_url="x", order=0),
        PveHostConfig(name="early-a", api_url="x", order=0),
        # Lowest possible number, yet it must still end up last.
        PveHostConfig(name="appliance", api_url="x", order=0, this_host=True),
        PveHostConfig(name="skipped", api_url="x", order=1, enabled=False),
    ])
    hosts = cfg.ordered_hosts()

    assert [h.name for h in hosts] == ["early-a", "early-b", "late", "appliance"]
    # Same (this_host, order) = one stage; the engine fires those concurrently.
    stages = [
        [h.name for h in group]
        for _, group in groupby(hosts, key=lambda h: (h.this_host, h.order))
    ]
    assert stages == [["early-a", "early-b"], ["late"], ["appliance"]]


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


# --- issue #25: the device's own "time on battery" counter is not evidence -----------
# Two failure directions, one rule. The counter is a duration the device *claims*; RFC 1628
# defines upsSecondsOnBattery only while on battery and says nothing about the value on
# mains, so a card that keeps the last transfer's figure breaks no rule. Believing it fired
# shutdowns in the first poll of an outage; believing a permanent 0 disabled the time
# trigger entirely. Our own measurement wins wherever we have one.
def test_an_inherited_device_counter_does_not_fire_the_shutdown_instantly():
    """Issue #25: a fresh outage must not inherit the previous one's length."""
    eng = _ups_engine(Thresholds(on_battery_seconds=600, runtime_below_minutes=None,
                                 charge_below_percent=None, on_battery_low=False))
    # The card reports three days on battery at the very moment it switches to battery.
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     seconds_on_battery=259200)
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc)  # we just saw it happen
    assert _reason(eng) is None
    # ... and once *our* clock passes the threshold, it does fire.
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=601)
    assert _reason(eng) is not None


def test_a_counter_stuck_at_zero_does_not_disable_the_time_trigger():
    """The mirror image: RFC 1628 mandates 0 on mains, some cards report it always."""
    eng = _ups_engine(Thresholds(on_battery_seconds=120, runtime_below_minutes=None,
                                 charge_below_percent=None, on_battery_low=False))
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     seconds_on_battery=0)
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=130)
    assert _reason(eng) is not None


@pytest.mark.asyncio
async def test_a_cold_start_into_a_running_outage_counts_from_zero():
    """No timer of our own means we start at zero — never at the number the device claims.

    Driven through _evaluate(), not through the helper: _evaluate_ups() sets
    on_battery_since on the very poll that first sees the battery, so a test calling
    _ups_elapsed_on_battery() directly can assert a branch the engine never reaches.
    """
    eng = _ups_engine(Thresholds(on_battery_seconds=120, runtime_below_minutes=None,
                                 charge_below_percent=None, on_battery_low=False))
    # The card claims five minutes on battery; our state file was unusable, so we have
    # no measurement at all. Its claim is past the 120 s threshold — and irrelevant.
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     seconds_on_battery=300)
    await eng._evaluate()
    rt = eng.ups_rt["u"]
    assert rt.on_battery_since is not None          # our clock started with this poll
    assert eng._ups_elapsed_on_battery(rt) < 5      # from zero, not from the card's 300
    assert eng._ups_elapsed_source(rt) == "own"
    assert not rt.triggered
    assert eng.shutdown_triggered is False


# --- the evidence behind a trigger ---------------------------------------------------
# A reason line says what the engine concluded, never what it read, and the test button can
# only answer about a later and different state. Without this an unexpected shutdown cannot
# be reconstructed at all.
def test_the_evidence_line_leads_with_the_values_a_post_mortem_starts_from():
    st = UpsState(raw={"device.serial": "abc", "battery.charge": "12",
                       "ups.status": "OB LB", "driver.version": "2.8"})
    line = st.readable_raw()
    assert line.startswith("ups.status=OB LB, battery.charge=12")
    assert "device.serial=abc" in line


def test_the_evidence_line_is_bounded():
    """SNMP contributes a handful of OIDs; NUT hands back its whole LIST VAR answer, which
    is fifty to ninety variables. That body reaches SQLite, the event table and the public
    status endpoint, so it cannot be unbounded."""
    st = UpsState(raw={f"var.{i:02d}": "x" * 20 for i in range(90)})
    line = st.readable_raw()
    assert len(line) <= 1000
    assert "more)" in line
    # An empty reading stays empty rather than becoming a stray "… (+0 more)".
    assert UpsState().readable_raw() == ""


def test_the_evidence_line_never_carries_a_secret():
    """The evidence line is written into the event log, and /api/status serves 48 h of it
    without authentication. NUT hands back its whole LIST VAR answer, and upsd publishes
    each driver's configuration as driver.parameter.<name> -- which for a number of drivers
    means a plaintext password or an SNMPv3 passphrase. Same rule, same reason as
    notify.safe_error() masking a webhook URL, on the evidence path instead."""
    from app.ups import SECRET_MASK

    st = UpsState(raw={
        "ups.status": "OB LB",
        "driver.parameter.password": "hunter2",
        "driver.parameter.authPassword": "hunter3",
        "driver.parameter.community": "private",
        "driver.parameter.port": "/dev/ttyUSB0",
    })
    line = st.readable_raw()
    for secret in ("hunter2", "hunter3", "private"):
        assert secret not in line
    # Masked, not dropped: that the driver carries one is itself diagnostic information,
    # and everything harmless stays readable.
    assert f"driver.parameter.password={SECRET_MASK}" in line
    assert "driver.parameter.port=/dev/ttyUSB0" in line


def test_the_mask_does_not_swallow_the_bypass_readings():
    """"bypass" contains "pass", and NUT publishes input.bypass.* as standard variables.

    A bare "pass" in the key pattern therefore masked input.bypass.voltage, .frequency,
    .current and .realpower — mains readings, in the one line that exists to record what
    the device said when it fired a shutdown. Every spelling of an actual credential still
    goes: erring towards masking is free here, erring the other way publishes one.
    """
    from app.ups import SECRET_MASK, redact_raw

    safe = redact_raw({
        "input.bypass.voltage": "230",
        "input.bypass.frequency": "50.0",
        "output.bypass.realpower": "180",
        "driver.parameter.password": "hunter2",
        "driver.parameter.privPassword": "hunter3",
        "driver.parameter.community": "private",
    })
    assert safe["input.bypass.voltage"] == "230"
    assert safe["input.bypass.frequency"] == "50.0"
    assert safe["output.bypass.realpower"] == "180"
    for key in ("driver.parameter.password", "driver.parameter.privPassword",
                "driver.parameter.community"):
        assert safe[key] == SECRET_MASK


def test_nut_never_stores_a_secret_in_the_first_place():
    """Redacted at the source as well, so every consumer of UpsState.raw inherits it --
    the evidence line is only the path that reaches a public endpoint today."""
    from app import nut
    from app.ups import SECRET_MASK

    st = UpsState()
    nut._apply_variables(st, {
        "ups.status": "OL",
        "battery.charge": "100",
        "driver.parameter.password": "hunter2",
    })
    assert st.raw["driver.parameter.password"] == SECRET_MASK
    assert "hunter2" not in repr(st.raw)
    # The mapping itself is unaffected: it reads standardised ups./battery. variables.
    assert st.battery_charge_pct == 100
    assert st.power_source == "mains"


@pytest.mark.asyncio
async def test_a_trigger_records_the_readings_that_caused_it():
    eng = _ups_engine(Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                                 charge_below_percent=None, on_battery_low=True))
    logged: list[tuple[str, str]] = []
    eng._log_quiet = lambda s, b, sev: logged.append((s, b))  # type: ignore[assignment]
    _notify_recorder(eng)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_status="low",
                                     battery_status_detail="upsBasicBatteryStatus=3",
                                     battery_charge_pct=11,
                                     raw={"1.3.6.1.2.1.33.1.2.4": "11"})

    await eng._evaluate()

    body = next(b for s, b in logged if "armed the shutdown trigger" in s)
    assert "upsBasicBatteryStatus=3" in body        # what the device actually said
    assert "from own clock" in body                 # never "device"
    assert "Readings:" in body

    # Once, not on every poll of the same outage.
    logged.clear()
    await eng._evaluate()
    assert not [s for s, _ in logged if "armed the shutdown trigger" in s]


def test_the_elapsed_source_is_own_or_nothing():
    """The device counter is never a source, whatever it says."""
    eng = _ups_engine(Thresholds(on_battery_seconds=120))
    for claimed in (300, 0, -1):
        eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                         seconds_on_battery=claimed)
        eng.ups_rt["u"].on_battery_since = None
        assert eng._ups_elapsed_on_battery(eng.ups_rt["u"]) is None
        assert eng._ups_elapsed_source(eng.ups_rt["u"]) is None


def test_seconds_on_battery_is_not_reported_while_the_ups_is_on_mains():
    """Issue #25's second symptom: "on battery for days" on a UPS sitting on mains."""
    eng = _ups_engine(Thresholds())
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     seconds_on_battery=259200)
    snap = eng._ups_snapshot(eng.cfg.ups_by_id("u"), eng.ups_rt["u"])
    assert snap["seconds_on_battery"] is None
    assert snap["elapsed_source"] is None


def test_the_snapshot_names_which_clock_the_elapsed_time_came_from():
    eng = _ups_engine(Thresholds())
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     seconds_on_battery=259200)
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=42)
    snap = eng._ups_snapshot(eng.cfg.ups_by_id("u"), eng.ups_rt["u"])
    assert snap["elapsed_source"] == "own"
    assert 40 <= snap["seconds_on_battery"] <= 45  # ours, not the card's three days


def test_no_trigger_when_healthy():
    eng = _ups_engine(Thresholds())
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     runtime_remaining_min=60, battery_charge_pct=100)
    assert _reason(eng) is None


@pytest.mark.asyncio
async def test_nut_source_runs_through_the_same_trigger_logic():
    """The engine must not care how a UPS is read — same thresholds, same latching."""
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                    charge_below_percent=25, on_battery_low=False)
    cfg = AppConfig(ups=[NutConfig(id="u", name="USB UPS", host="127.0.0.1", ups_name="ups")],
                    thresholds=th)
    eng = Engine(cfg)

    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_charge_pct=20)
    await eng._evaluate()
    assert eng.ups_rt["u"].triggered
    assert "charge 20%" in eng.ups_rt["u"].trigger_reason

    # Snapshot tells the UI which source this is, and mains return clears the trigger.
    assert eng.snapshot()["ups"][0]["type"] == "nut"
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     battery_charge_pct=90)
    await eng._evaluate()
    assert not eng.ups_rt["u"].triggered


@pytest.mark.asyncio
async def test_unreachable_nut_source_alarms_but_never_shuts_down():
    """Fail safe is a property of the engine, so it holds for every source type."""
    cfg = AppConfig(
        ups=[NutConfig(id="u", name="USB UPS", host="127.0.0.1", ups_name="ups")],
        hosts=[PveHostConfig(name="pve01", api_url="https://x:8006", ups_ids=["u"])],
        thresholds=Thresholds(unreachable_alarm_after_polls=1),
        dry_run=False,
    )
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=False, error="upsd has stale data")
    await eng._evaluate()
    assert eng.ups_rt["u"].alarm_active
    assert not eng.ups_rt["u"].triggered
    assert not eng.shutdown_triggered


# --- multi-UPS host policy (AND/OR) -----------------------------------------
def _multi_engine(policy: str, *, dry_run=True, ups_ids=("a", "b")) -> Engine:
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                    charge_below_percent=None, on_battery_low=False)
    cfg = AppConfig(
        dry_run=dry_run,
        ups=[SnmpConfig(id="a", name="A", host="10.0.0.1"),
             SnmpConfig(id="b", name="B", host="10.0.0.2")],
        hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=list(ups_ids), ups_policy=policy)],
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
    assert eng.host_fired.get("pve:pve01") in (None, False)
    assert eng.shutdown_triggered is False
    # now the second UPS also goes critical -> shutdown fires
    _on_battery_low_runtime(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve:pve01") is True
    assert eng.shutdown_triggered is True


@pytest.mark.asyncio
async def test_or_policy_fires_on_first_feed():
    eng = _multi_engine("any")
    _on_battery_low_runtime(eng, "a")
    _on_mains(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve:pve01") is True
    assert eng.shutdown_triggered is True


@pytest.mark.asyncio
async def test_single_ups_host_behaves_like_before():
    # Regression: a host fed by exactly one UPS shuts down when that UPS triggers.
    eng = _multi_engine("all", ups_ids=("a",))
    _on_battery_low_runtime(eng, "a")
    _on_mains(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve:pve01") is True


@pytest.mark.asyncio
async def test_and_policy_abort_when_feed_recovers():
    # A dry-run latched host is released when a required feed returns to mains.
    eng = _multi_engine("all")
    _on_battery_low_runtime(eng, "a")
    _on_battery_low_runtime(eng, "b")
    await eng._evaluate()
    assert eng.host_fired.get("pve:pve01") is True
    _on_mains(eng, "a")  # one feed recovers
    await eng._evaluate()
    assert eng.host_fired.get("pve:pve01") is False  # latch released (abort)
    assert eng.shutdown_triggered is False


# --- re-arming after an episode ---------------------------------------------
def _fired_engine(monkeypatch, **th):
    """An engine that has really shut its host down (not a dry run)."""
    from app import proxmox

    sent: list[str] = []

    async def fake_shutdown(host, timeout=60, **kw):
        sent.append(host.name)
        return True, "ok"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    cfg = AppConfig(
        dry_run=False,
        ups=[SnmpConfig(id="a", name="A", host="10.0.0.1")],
        hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["a"])],
        thresholds=Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                              charge_below_percent=None, on_battery_low=False, **th),
    )
    eng = Engine(cfg)
    _notify_recorder(eng)
    return eng, sent


@pytest.mark.asyncio
async def test_the_appliance_re_arms_once_mains_have_been_back(monkeypatch):
    """A sent shutdown latches its host for good — nothing ever cleared that again, so
    the appliance stayed in SHUTTING_DOWN, the self-test and the "Restore cluster" button
    stood down for ever, and a second outage shut down nothing at all."""
    eng, sent = _fired_engine(monkeypatch)
    eng.host_states["pve:pve01"] = {"credentials_ok": True, "last_test_at": "yesterday"}

    _on_battery_low_runtime(eng, "a")
    await eng._evaluate()
    assert sent == ["pve01"] and eng.state == SHUTTING_DOWN

    # Mains are back, but not for long enough yet: a grid that dips twice in a minute
    # must not re-arm in between.
    _on_mains(eng, "a")
    await eng._evaluate()
    assert eng.shutdown_triggered is True and eng.state == SHUTTING_DOWN

    eng._mains_ok_since = eng._mains_ok_since - timedelta(minutes=6)
    await eng._evaluate()

    assert eng.host_fired == {} and eng.shutdown_triggered is False
    assert eng.state == ONLINE
    # The self-test verdict lives in the same dict and answers a different question.
    assert eng.host_states["pve:pve01"]["credentials_ok"] is True
    assert "shutdown_state" not in eng.host_states["pve:pve01"]

    # And the whole point: the next outage is handled like the first.
    _on_battery_low_runtime(eng, "a")
    await eng._evaluate()
    assert sent == ["pve01", "pve01"]


@pytest.mark.asyncio
async def test_an_unreachable_ups_does_not_count_as_mains_back(monkeypatch):
    """Fail safe in the other direction too: we do not know, so we do not re-arm."""
    eng, _ = _fired_engine(monkeypatch)

    _on_battery_low_runtime(eng, "a")
    await eng._evaluate()

    eng.ups_rt["a"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()
    assert eng._mains_ok_since is None
    assert eng.shutdown_triggered is True


@pytest.mark.asyncio
async def test_re_arming_can_be_switched_off(monkeypatch):
    """None keeps the pre-4.0 behaviour: the latches come off by hand, or not at all."""
    eng, _ = _fired_engine(monkeypatch, rearm_after_mains_min=None)

    _on_battery_low_runtime(eng, "a")
    await eng._evaluate()
    _on_mains(eng, "a")
    await eng._evaluate()

    assert eng._mains_ok_since is None       # not even counting
    assert eng.host_fired.get("pve:pve01") is True
    assert eng.state == SHUTTING_DOWN

    # The manual reset still works, and now keeps the self-test results.
    eng.host_states["pve:pve01"]["credentials_ok"] = True
    eng.reset()
    assert eng.host_fired == {} and eng.state == ONLINE
    assert eng.host_states["pve:pve01"] == {"credentials_ok": True}


@pytest.mark.asyncio
async def test_eligible_hosts_shut_down_this_host_last(monkeypatch):
    from app import proxmox
    order: list[str] = []

    async def fake_shutdown(host, timeout=60, **kw):
        order.append(host.name)
        return True, "ok"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                    charge_below_percent=None, on_battery_low=False)
    cfg = AppConfig(
        dry_run=False,
        ups=[SnmpConfig(id="a", host="10.0.0.1")],
        hosts=[
            PveHostConfig(name="self", api_url="x", this_host=True, ups_ids=["a"]),
            PveHostConfig(name="other", api_url="x", order=1, ups_ids=["a"]),
        ],
        thresholds=th,
    )
    eng = Engine(cfg)
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="battery", runtime_remaining_min=3)
    await eng._evaluate()
    assert order == ["other", "self"]  # appliance host last


# --- a failed shutdown is retried, but not forever ----------------------------------
def _one_host_on_battery(**host_kw):
    """Engine with one UPS already past its runtime threshold and one PVE host on it."""
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                    charge_below_percent=None, on_battery_low=False)
    cfg = AppConfig(dry_run=False, ups=[SnmpConfig(id="a", host="10.0.0.1")],
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["a"],
                                         **host_kw)],
                    thresholds=th)
    eng = Engine(cfg)
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="battery",
                                     runtime_remaining_min=3)
    return eng


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_on_the_next_poll(monkeypatch):
    """One 503 must not cost the machine: it used to latch the host for the episode."""
    from app import proxmox
    calls: list[str] = []

    async def fake_shutdown(host, timeout=60, **kw):
        calls.append(host.name)
        return len(calls) > 1, "HTTP 503" if len(calls) == 1 else "accepted"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    eng = _one_host_on_battery()
    key = eng.cfg.hosts[0].key

    await eng._evaluate()
    assert eng.host_states[key]["shutdown_state"] == "failed"
    assert eng.host_fired.get(key) is False        # not latched — try again
    # The dashboard must still say a shutdown is under way while we retry.
    assert eng.state == SHUTTING_DOWN

    await eng._evaluate()
    assert calls == ["pve01", "pve01"]
    assert eng.host_states[key]["shutdown_state"] == "sent"
    assert eng.host_fired.get(key) is True


@pytest.mark.asyncio
async def test_a_dead_host_is_given_up_on_after_the_capped_attempts(monkeypatch):
    from app import proxmox
    calls: list[str] = []

    async def fake_shutdown(host, timeout=60, **kw):
        calls.append(host.name)
        return False, "no route to host"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    eng = _one_host_on_battery()
    key = eng.cfg.hosts[0].key

    for _ in range(6):
        await eng._evaluate()

    from app.engine import MAX_SHUTDOWN_ATTEMPTS
    assert len(calls) == MAX_SHUTDOWN_ATTEMPTS   # and then quiet, not every 8 s forever
    assert eng.host_fired.get(key) is True
    assert eng.host_states[key]["shutdown_attempts"] == MAX_SHUTDOWN_ATTEMPTS


@pytest.mark.asyncio
async def test_a_sent_shutdown_is_never_sent_twice(monkeypatch):
    """The other direction: success still latches on the first attempt."""
    from app import proxmox
    calls: list[str] = []

    async def fake_shutdown(host, timeout=60, **kw):
        calls.append(host.name)
        return True, "accepted"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    eng = _one_host_on_battery()
    for _ in range(4):
        await eng._evaluate()
    assert calls == ["pve01"]


@pytest.mark.asyncio
async def test_a_failed_host_releases_its_latch_when_mains_return(monkeypatch):
    """It never went down, so "aborted" is the honest word once the outage ends."""
    from app import proxmox

    async def fake_shutdown(host, timeout=60, **kw):
        return False, "HTTP 503"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    eng = _one_host_on_battery()
    key = eng.cfg.hosts[0].key
    for _ in range(4):
        await eng._evaluate()
    assert eng.host_fired.get(key) is True        # gave up

    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="mains",
                                     runtime_remaining_min=60)
    await eng._evaluate()
    assert eng.host_fired.get(key) is False
    assert eng.state == ONLINE


@pytest.mark.asyncio
async def test_a_host_that_never_went_down_is_not_filed_as_merely_aborted(monkeypatch):
    """"No longer needed" is true for a withdrawn shutdown and false for a failed one.

    Reading the second as the first is how an unfinished episode gets closed: the machine
    is still running, and the only trace was a routine-sounding line."""
    from app import proxmox

    async def fake_shutdown(host, timeout=60, **kw):
        return False, "HTTP 503"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    eng = _one_host_on_battery()
    for _ in range(4):
        await eng._evaluate()

    events: list[tuple[str, str, str]] = []

    async def rec(subject, body, severity):
        events.append((subject, body, severity))

    eng._emit = rec  # type: ignore[assignment]
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="mains",
                                     runtime_remaining_min=60)
    await eng._evaluate()

    aborted = [e for e in events if "shutdown aborted" in e[0]]
    assert aborted, "the latch release still has to say something"
    assert "still running" in aborted[0][1]
    assert aborted[0][2] == "critical"   # not the routine wording, not the routine level


@pytest.mark.asyncio
async def test_mains_returning_between_two_attempts_still_closes_the_episode(monkeypatch):
    """The gap the retry opened: a failure with attempts LEFT keeps host_fired False.

    The release branch used to key on that latch alone, so an outage ending between two
    attempts left "failed" sitting in host_states for good. _recompute_state counts that
    as committed, so the appliance reported SHUTTING_DOWN with mains long back, wrote no
    abort event at all, and _maybe_rearm could not clean up because nothing was latched.
    Only "Reset state" or a restart got out of it."""
    from app import proxmox

    async def fake_shutdown(host, timeout=60, **kw):
        return False, "HTTP 503"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    eng = _one_host_on_battery()
    key = eng.cfg.hosts[0].key

    await eng._evaluate()                       # exactly ONE failed attempt
    assert eng.host_fired.get(key) is False     # retries left, so not latched
    assert eng.host_states[key]["shutdown_state"] == "failed"

    events = _notify_recorder(eng)
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="mains",
                                     runtime_remaining_min=60)
    await eng._evaluate()

    assert eng.state == ONLINE
    assert "shutdown_state" not in eng.host_states[key]
    assert eng.shutdown_triggered is False
    # _notify_recorder records (subject, severity, body).
    aborted = [e for e in events if "shutdown aborted" in e[0]]
    assert aborted, "the machine is still running - that has to be said"
    assert "still running" in aborted[0][2]
    assert aborted[0][1] == "critical"


@pytest.mark.asyncio
async def test_a_deleted_host_does_not_keep_the_episode_open(monkeypatch):
    """host_states and host_fired are read wholesale, so a removed host used to linger.

    _recompute_state scans every value and _evaluate_hosts ends the episode on
    ``any(host_fired.values())`` - a host that had fired and was then deleted therefore
    pinned SHUTTING_DOWN and shutdown_triggered, which stands down the self-test, both
    startup checks and "Restore cluster"."""
    from app import proxmox

    async def fake_shutdown(host, timeout=60, **kw):
        return True, "accepted"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    eng = _one_host_on_battery()
    key = eng.cfg.hosts[0].key
    await eng._evaluate()
    assert eng.host_fired.get(key) is True
    assert eng.state == SHUTTING_DOWN

    # The host is removed while the appliance is still latched on it.
    new_cfg = eng.cfg.model_copy(update={"hosts": []})
    eng.update_config(new_cfg)
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="mains",
                                     runtime_remaining_min=60)
    await eng._evaluate()

    assert key not in eng.host_states and key not in eng.host_fired
    assert eng.shutdown_triggered is False
    assert eng.state == ONLINE


def test_a_deleted_webhook_does_not_hand_its_failure_to_a_new_one():
    """The delivery record is keyed by the webhook id, and an id can come back."""
    from app import notify as notify_mod
    from app.config import Notifications, WebhookConfig

    cfg = AppConfig(notifications=Notifications(webhooks=[
        WebhookConfig(id="webhook1", enabled=True, url="https://old")]))
    eng = Engine(cfg)
    notify_mod.DELIVERY["webhook1"] = {"ok": False, "at": "now", "error": "HTTP 401"}

    # The old target is deleted and a new one happens to be given the same id.
    eng.update_config(AppConfig(notifications=Notifications(webhooks=[])))
    assert "webhook1" not in notify_mod.DELIVERY

    eng.update_config(AppConfig(notifications=Notifications(webhooks=[
        WebhookConfig(id="webhook1", enabled=True, url="https://new")])))
    assert eng._webhook_snapshot()[0]["last_delivery_ok"] is None


@pytest.mark.asyncio
async def test_the_reserve_warning_sees_a_threshold_set_on_one_ups():
    """The trigger is overridable per UPS, and an override is where a short reserve lands.

    Reading only the global value said nothing about the one device configured to fire at
    two minutes while the estate takes longer than that to shut down."""
    from app.config import UpsThresholdOverride

    cfg = AppConfig(
        ups=[SnmpConfig(id="a", name="Rack A", host="10.0.0.1"),
             SnmpConfig(id="b", name="Rack B", host="10.0.0.2",
                        overrides=UpsThresholdOverride(runtime_below_minutes=1))],
        hosts=[PveHostConfig(name="a", api_url="x", order=0),
               PveHostConfig(name="b", api_url="x2", order=1)],
        thresholds=Thresholds(runtime_below_minutes=30, host_shutdown_timeout_s=60),
    )
    eng = Engine(cfg)
    events = _notify_recorder(eng)

    await eng._warn_about_battery_reserve()

    body = next((b for s, _, b in events if "battery reserve" in s.lower()), "")
    assert body, "the override is the whole point of the warning"
    assert "1 min" in body
    assert "Rack B" in body, "name the field the operator has to go and change"


@pytest.mark.asyncio
async def test_an_earlier_stage_is_retried_before_the_appliance_powers_itself_off(
    monkeypatch,
):
    """The retry is normally carried by the next poll — but no poll follows ``this_host``.

    Without a pass before that final stage, the one case MAX_SHUTDOWN_ATTEMPTS exists for
    (a 503 from a busy pveproxy) is exactly the case it could never reach: the appliance
    shut itself down eight seconds before the retry was due."""
    from app import proxmox
    calls: list[str] = []

    async def fake_shutdown(host, timeout=60, **kw):
        calls.append(host.name)
        # "other" fails once, then works; the appliance's own host always works.
        if host.name == "other":
            return calls.count("other") > 1, "HTTP 503"
        return True, "accepted"

    monkeypatch.setattr(proxmox, "shutdown_node", fake_shutdown)
    cfg = AppConfig(
        dry_run=False,
        ups=[SnmpConfig(id="a", host="10.0.0.1")],
        hosts=[
            PveHostConfig(name="other", api_url="x", order=1, ups_ids=["a"]),
            PveHostConfig(name="self", api_url="x", this_host=True, ups_ids=["a"]),
        ],
        thresholds=Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                              charge_below_percent=None, on_battery_low=False),
    )
    eng = Engine(cfg)
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="battery",
                                     runtime_remaining_min=3)

    await eng._evaluate()

    # The retry happens in the SAME sweep, and still before the appliance's own host.
    assert calls == ["other", "other", "self"]
    assert eng.host_fired[cfg.hosts[0].key] is True
    assert eng.host_states[cfg.hosts[0].key]["shutdown_state"] == "sent"


# --- one sick source must not freeze the engine -------------------------------------
def test_the_poll_budget_leaves_room_for_a_legitimate_slow_read():
    """The bound is a backstop, not a tightening of the source's own timeouts.

    SNMP in "auto" mode may issue two sequential GETs, each costing
    timeout_s x (retries + 1) — about 12 s with the defaults, against an 8 s battery
    interval, and perfectly healthy. Cutting the poll off at the interval would report a
    working UPS as unreachable, which is an alarm and a refusal to shut down.
    """
    from app.sources import poll_budget_s
    assert poll_budget_s(SnmpConfig(id="u", host="h")) >= 12
    assert poll_budget_s(NutConfig(id="n", host="h", ups_name="ups")) >= 8


@pytest.mark.asyncio
async def test_a_hanging_source_is_cut_off_rather_than_stalling_the_loop(monkeypatch):
    """Everything else — countdowns, eligibility, the staged shutdown — waits behind this."""
    import asyncio as _asyncio
    from app import sources

    monkeypatch.setattr(sources, "POLL_GRACE_S", 0.05)
    monkeypatch.setattr(sources, "poll_budget_s", lambda cfg: 0.05)

    async def hang(cfg):
        await _asyncio.sleep(30)

    monkeypatch.setattr(sources.ups, "poll", hang)
    state = await sources.poll(SnmpConfig(id="u", host="10.0.0.9"))

    assert state.reachable is False          # unreachable = alarm, never a shutdown
    assert "No answer within" in (state.error or "")


@pytest.mark.asyncio
async def test_a_source_that_raises_becomes_unreachable_not_an_exception(monkeypatch):
    from app import sources

    async def boom(cfg):
        raise RuntimeError("driver exploded")

    monkeypatch.setattr(sources.ups, "poll", boom)
    state = await sources.poll(SnmpConfig(id="u", host="10.0.0.9"))
    assert state.reachable is False
    assert "driver exploded" in (state.error or "")


@pytest.mark.asyncio
async def test_a_raising_stage_does_not_cost_the_stages_behind_it(monkeypatch):
    """Including the last one, which is the appliance's own host.

    Without return_exceptions the throw propagates out of _evaluate(), so every later
    stage is skipped and _recompute_state()/_persist_state() never run for that poll.
    """
    fired: list[str] = []
    eng = Engine(AppConfig(
        dry_run=False,
        ups=[SnmpConfig(id="a", host="10.0.0.1")],
        hosts=[
            PveHostConfig(name="other", api_url="x", order=1, ups_ids=["a"]),
            PveHostConfig(name="self", api_url="x", this_host=True, ups_ids=["a"]),
        ],
        thresholds=Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                              charge_below_percent=None, on_battery_low=False),
    ))
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="battery",
                                     runtime_remaining_min=3)

    async def fire(host, reason):
        if host.name == "other":
            raise RuntimeError("something in the first stage broke")
        fired.append(host.name)

    eng._fire_host = fire  # type: ignore[assignment]
    await eng._evaluate()

    assert fired == ["self"], "the appliance's own host must still be told to go"
    # The host that threw was never recorded as handled, so the next poll picks it up.
    assert eng.host_fired.get(eng.cfg.hosts[0].key) is False


@pytest.mark.asyncio
async def test_per_ups_override_changes_only_that_ups():
    th = Thresholds(on_battery_seconds=None, runtime_below_minutes=5,
                    charge_below_percent=None, on_battery_low=False)
    cfg = AppConfig(
        dry_run=True,
        ups=[SnmpConfig(id="a", host="10.0.0.1",
                        overrides=UpsThresholdOverride(runtime_below_minutes=2)),
             SnmpConfig(id="b", host="10.0.0.2")],
        hosts=[PveHostConfig(name="ha", api_url="x", ups_ids=["a"]),
               PveHostConfig(name="hb", api_url="x", ups_ids=["b"])],
        thresholds=th,
    )
    eng = Engine(cfg)
    # runtime 3 min: below global (5) but above the per-UPS override (2) for UPS a
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="battery", runtime_remaining_min=3)
    eng.ups_rt["b"].state = UpsState(reachable=True, power_source="battery", runtime_remaining_min=3)
    await eng._evaluate()
    assert eng.host_fired.get("pve:ha") in (None, False)  # a's stricter threshold not met
    assert eng.host_fired.get("pve:hb") is True            # b uses global 5 -> met


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
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["u"])],
                    thresholds=th)
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")
    # Our own clock, not a device counter: the engine measures the outage itself now
    # (see _ups_elapsed_on_battery), so the outage starts in the past rather than the UPS
    # being handed a "10 s on battery" it could just as well have inherited.
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=10)
    await eng._evaluate()  # enters ON_BATTERY + fires dry-run
    assert eng.shutdown_triggered is True
    assert eng.host_fired.get("pve:pve01") is True
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
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
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
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
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


# --- connection-loss debounce (issue #17) ------------------------------------
def _notify_recorder(eng):
    """Record everything _emit() would notify about (the event log is not affected)."""
    events: list[tuple[str, str, str]] = []

    async def rec(subject, body, severity):
        events.append((subject, severity, body))

    eng._emit = rec  # type: ignore[assignment]
    return events


async def _poll_reachability(eng, *reachable_states):
    for ok in reachable_states:
        eng.ups_rt["u"].state = (
            UpsState(reachable=True, power_source="mains", battery_charge_pct=100)
            if ok
            else UpsState(reachable=False, error="timeout")
        )
        await eng._evaluate()


@pytest.mark.asyncio
async def test_single_dropped_poll_does_not_notify():
    """A blip must stay silent: ok, fail, ok produces no notification at all.

    This is issue #17 — a single lost SNMP packet used to fire a warning-level webhook
    immediately, ahead of the alarm threshold that was supposed to govern it.
    """
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=3))
    events = _notify_recorder(eng)
    await _poll_reachability(eng, True, False, True)
    assert events == []


@pytest.mark.asyncio
async def test_sustained_loss_notifies_exactly_once():
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=3))
    events = _notify_recorder(eng)
    await _poll_reachability(eng, True, False, False, False)
    from app import db

    assert len(events) == 1
    subject, severity, _ = events[0]
    assert "unreachable" in subject and severity == db.WARNING

    # Staying unreachable must not repeat it, and the recovery is reported exactly once.
    await _poll_reachability(eng, False, False)
    assert len(events) == 1
    await _poll_reachability(eng, True)
    assert len(events) == 2
    assert "restored" in events[1][0]


@pytest.mark.asyncio
async def test_recovery_is_silent_when_the_loss_was_never_notified():
    """No "restored" out of nowhere: it is only notified if a loss was notified too."""
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=5))
    events = _notify_recorder(eng)
    await _poll_reachability(eng, True, False, False, True)
    assert events == []


@pytest.mark.asyncio
async def test_debounce_never_delays_a_shutdown():
    """Regression guard: the debounce covers connectivity notices only.

    A trigger that fires while unreachable must still latch on the very same poll — the
    battery countdown may not wait for an alarm threshold.
    """
    th = Thresholds(unreachable_alarm_after_polls=99,  # alarm would never fire
                    on_battery_seconds=1, runtime_below_minutes=None,
                    charge_below_percent=None, on_battery_low=False,
                    keep_shutdown_on_comm_loss=True)
    cfg = AppConfig(dry_run=False, ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["u"])],
                    thresholds=th)
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")
    eng.ups_rt["u"].on_battery_since = datetime.now(timezone.utc) - timedelta(seconds=30)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.key)

    eng._fire_host = fake_fire  # type: ignore[assignment]
    # Communication drops on the same poll the countdown expires on.
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()
    assert fired == ["pve:pve01"], "a blind, latched trigger must fire without waiting"


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
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
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
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
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
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)
    eng1 = Engine(cfg)
    eng1.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                      battery_charge_pct=20)
    await eng1._evaluate()
    reason = eng1.ups_rt["u"].trigger_reason
    assert reason

    # "Restart": the timer comes back, but the latch is HELD until a poll of this process
    # answers. Restoring rt.triggered straight from the file made the latch bypass every
    # freshness check there is — the unreachable branch never re-derives a trigger that is
    # already set, so a latch read off disk fired on the first poll after a boot whatever
    # the device said, or whether it said anything at all.
    eng2 = Engine(cfg)
    assert eng2.ups_rt["u"].restored_unconfirmed is True
    assert eng2.ups_rt["u"].triggered is False
    eng2.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng2._evaluate()
    assert eng2.ups_rt["u"].triggered is False
    assert eng2.shutdown_triggered is False

    # The confirming poll re-arms it from fresh data, with the same reason.
    eng2.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                      battery_charge_pct=20)
    await eng2._evaluate()
    assert eng2.ups_rt["u"].triggered is True
    assert eng2.ups_rt["u"].trigger_reason == reason


def test_config_roundtrip_keep_shutdown_on_comm_loss(tmp_path):
    assert Thresholds().keep_shutdown_on_comm_loss is True  # default on
    path = tmp_path / "c.yaml"
    save_config(AppConfig(thresholds=Thresholds(keep_shutdown_on_comm_loss=False)), path)
    assert load_config(path).thresholds.keep_shutdown_on_comm_loss is False


@pytest.mark.asyncio
async def test_network_transitions_are_logged():
    """Every transition reaches the EVENT LOG, even the ones that are not notified.

    Since the issue #17 fix a short loss is debounced out of the notification path, so
    diagnostics depend on the quiet log keeping the full picture. Both directions must
    appear there regardless of the alarm threshold.
    """
    eng = _ups_engine(Thresholds(unreachable_alarm_after_polls=99))
    logged: list[str] = []
    notified: list[str] = []

    async def rec_emit(subject, body, severity):
        notified.append(subject)

    def rec_quiet(subject, body, severity):
        logged.append(subject)

    eng._emit = rec_emit  # type: ignore[assignment]
    eng._log_quiet = rec_quiet  # type: ignore[assignment]
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    await eng._evaluate()  # first poll: no transition (last is None)
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()  # -> lost
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    await eng._evaluate()  # -> restored
    assert any("connection lost" in s for s in logged)
    assert any("connection restored" in s for s in logged)
    # ... and below the alarm threshold none of it is worth a notification.
    assert notified == []


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


def _write_pkg_tar(path, mode, prefix="pve-usv/"):
    """A minimal release package, the way `git archive` produces it (with a prefix dir)."""
    import io
    import tarfile

    members = {
        f"{prefix}pyproject.toml": b"[project]\n",
        f"{prefix}app/__init__.py": b'__version__ = "9.9.9"\n',
    }
    with tarfile.open(path, mode) as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return path


def test_inspect_package_accepts_targz_plain_tar_and_zip(tmp_path):
    """Format detection is by content, not by name.

    Safari unpacks .tar.gz on download (issue #24), so the very same release asset can
    arrive as a plain .tar — it must be accepted just like the compressed original.
    """
    import zipfile

    from app import main

    assert main._inspect_package(_write_pkg_tar(tmp_path / "pkg.tar.gz", "w:gz")) == ("9.9.9", None)
    assert main._inspect_package(_write_pkg_tar(tmp_path / "pkg.tar", "w")) == ("9.9.9", None)
    # A .tar.gz name on plain tar content (and vice versa) must not confuse the check.
    assert main._inspect_package(_write_pkg_tar(tmp_path / "lying.tar.gz", "w")) == ("9.9.9", None)

    z = tmp_path / "pkg.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("pyproject.toml", "[project]\n")
        zf.writestr("app/__init__.py", '__version__ = "9.9.9"\n')
    assert main._inspect_package(z) == ("9.9.9", None)


def test_inspect_package_rejects_unreadable_and_foreign_archives(tmp_path):
    """A bad upload must be caught here, before it is handed to the privileged agent."""
    import io
    import tarfile

    from app import main

    junk = tmp_path / "broken.tar.gz"
    junk.write_bytes(b"this is not an archive")
    version, error = main._inspect_package(junk)
    assert version is None and error and "readable" in error

    foreign = tmp_path / "foreign.tar.gz"
    with tarfile.open(foreign, "w:gz") as t:
        data = b"hello\n"
        info = tarfile.TarInfo("README.md")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    version, error = main._inspect_package(foreign)
    assert version is None and error and "release package" in error

    # Readable and ours, but without a parsable version: still accepted (the agent copes),
    # because an unreadable version is cosmetic while a refused update is not.
    import io as _io

    noversion = tmp_path / "noversion.tar.gz"
    with tarfile.open(noversion, "w:gz") as t:
        for name, data in (("pyproject.toml", b"[project]\n"), ("app/__init__.py", b"# empty\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, _io.BytesIO(data))
    assert main._inspect_package(noversion) == (None, None)


# --- Docker deployment mode (v3.1.0) -----------------------------------------
async def test_update_upload_disabled_in_docker_mode(monkeypatch):
    """There is no privileged agent in a Docker image; the upload endpoint must refuse
    loudly (501) instead of silently enqueueing a job nothing will ever drain."""
    from app import main

    monkeypatch.setattr(main, "IS_DOCKER", True)
    with pytest.raises(main.HTTPException) as exc:
        await main.api_update_upload(file=None)
    assert exc.value.status_code == 501


async def test_update_status_reports_docker_mode_without_agent_state(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "IS_DOCKER", True)
    result = await main.api_update_status()
    assert result["deployment"] == "docker"
    assert result["pending"] == []
    assert result["agent_drainer"] is None


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
                    hosts=[PveHostConfig(name="pve01", api_url="x", ups_ids=["u"])], thresholds=th)

    eng1 = Engine(cfg)
    eng1.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")
    await eng1._evaluate()
    since = eng1.ups_rt["u"].on_battery_since
    assert since is not None
    assert engine_mod.STATE_PATH.exists()  # timer was persisted

    # "Restart": a fresh engine restores the timer, but HOLDS it until one poll of this
    # process has answered — nothing read off disk fires on its own (see
    # _UpsRuntime.restored_unconfirmed).
    eng2 = Engine(cfg)
    assert eng2.ups_rt["u"].on_battery_since == since
    assert eng2.ups_rt["u"].restored_unconfirmed is True
    eng2.ups_rt["u"].on_battery_since = since - timedelta(seconds=700)
    eng2.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng2._evaluate()
    assert eng2.shutdown_triggered is False

    # One answer confirming the outage is still running releases it, and the blind
    # countdown then fires on the FULL restored elapsed time — the restart costs nothing.
    eng2.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")
    await eng2._evaluate()
    assert eng2.ups_rt["u"].restored_unconfirmed is False
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


@pytest.mark.asyncio
async def test_a_restored_timer_does_not_shut_down_a_silent_ups():
    """The reported failure: healthy hosts shut down again right after an outage.

    The appliance shuts its OWN host down last, so every outage ends with this process
    being restarted — and finding a state file that says "on battery since T0". If the UPS
    then misses the very first poll after that boot (a switch still converging, an SNMP
    card still coming up), the blind countdown read hours of restored elapsed time against
    the default 600 s threshold and shut the machines that had just come back up down
    again, during normal operation, before the device had said a single word.

    Defaults throughout, because that is the point: keep_shutdown_on_comm_loss is on and
    on_battery_seconds is 600 out of the box.
    """
    from app import engine as engine_mod
    import json as _json

    cfg = AppConfig(
        dry_run=False,
        ups=[SnmpConfig(id="u", host="10.0.0.9")],
        hosts=[PveHostConfig(id="h", name="pve01", api_url="https://10.0.0.10:8006",
                             token_id="ups@pve!s", token_secret="sec", ups_ids=["u"])],
    )
    assert cfg.thresholds.keep_shutdown_on_comm_loss is True
    assert cfg.thresholds.on_battery_seconds == 600

    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    engine_mod.STATE_PATH.write_text(
        _json.dumps({"on_battery_since": {"u": since},
                     "trigger_reason": {"u": "charge 20% <= 30%"}}),
        encoding="utf-8",
    )

    eng = Engine(cfg)
    assert eng.ups_rt["u"].on_battery_since is not None  # the timer itself is kept
    assert eng.ups_rt["u"].restored_unconfirmed is True
    assert eng.ups_rt["u"].triggered is False            # ...but nothing is armed by it

    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    assert eng._ups_trigger_reason(cfg.ups[0], eng.ups_rt["u"]) is None
    assert eng._host_trigger_reason(cfg.hosts[0]) is None
    # And no countdown is ticked toward a shutdown that cannot happen.
    assert eng._ups_countdown_remaining_s(cfg.ups[0], eng.ups_rt["u"]) is None

    fired: list = []
    eng._fire_host = lambda h, r: fired.append(h.name)  # type: ignore[assignment]
    await eng._evaluate()
    assert fired == []
    assert eng.shutdown_triggered is False


@pytest.mark.asyncio
async def test_a_held_timer_does_not_stand_the_housekeeping_down_for_ever():
    """The other half of the hold, and the one that locks the operator out.

    Five places stand down "while an outage is running": both startup checks, the
    scheduled self-test, the manual one and "Restore cluster". A restored timer sets
    on_battery_since and keeps it until the UPS answers — so a UPS that never answers
    again meant "an outage is running" for ever.

    That is exactly the state after a real cluster shutdown: the appliance powers its own
    host off last, comes back, and the UPS management card is still down. The cluster
    startup check then never ran, so cluster_states stayed empty and the "Restore cluster"
    button stayed hidden — and the button was the only thing the operator needed.

    Nothing is risked by letting them run: a held timer cannot fire a shutdown either, so
    there is no countdown for a self-test to delay.
    """
    from app import engine as engine_mod
    import json as _json

    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    engine_mod.STATE_PATH.write_text(
        _json.dumps({"on_battery_since": {"u": since}}), encoding="utf-8")
    eng = Engine(AppConfig(
        ups=[SnmpConfig(id="u", name="Rack A", host="10.0.0.9")],
        hosts=[PveHostConfig(id="h", name="pve01", api_url="https://10.0.0.10:8006",
                             token_id="ups@pve!s", token_secret="sec", ups_ids=["u"])]))
    rt = eng.ups_rt["u"]
    rt.state = UpsState(reachable=False, error="timeout")
    assert rt.restored_unconfirmed is True and rt.on_battery_since is not None

    # Unconfirmed: the housekeeping runs, and so does the button behind restore_clusters().
    assert eng._outage_in_progress() is False

    # A confirmed outage still stands everything down — that half is the whole point.
    rt.restored_unconfirmed = False
    assert eng._outage_in_progress() is True
    rt.state = UpsState(reachable=True, power_source="battery")
    assert eng._outage_in_progress() is True

    # As does a shutdown episode, whatever the UPS is saying.
    eng.ups_rt["u"] = engine_mod._UpsRuntime()
    eng.shutdown_triggered = True
    assert eng._outage_in_progress() is True


@pytest.mark.asyncio
async def test_the_unreachable_alarm_does_not_promise_a_held_countdown():
    """The alarm text has three neighbouring cases and the held one is none of them.

    "The countdown keeps running, shutdown when it expires" is a promise that will not be
    kept while the timer is held; "NO shutdown will be triggered" would hide a timer that
    is still there and re-arms in full the moment the device speaks.
    """
    from app import engine as engine_mod
    import json as _json

    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    engine_mod.STATE_PATH.write_text(
        _json.dumps({"on_battery_since": {"u": since}}), encoding="utf-8")
    eng = Engine(AppConfig(
        ups=[SnmpConfig(id="u", name="Rack A", host="10.0.0.9")],
        thresholds=Thresholds(unreachable_alarm_after_polls=1)))
    events = _notify_recorder(eng)

    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")
    await eng._evaluate()

    alarms = [(s, b) for s, _sev, b in events if "unreachable" in s]
    assert len(alarms) == 1
    subject, body = alarms[0]
    assert "on hold" in subject
    assert "shutdown when it expires" not in body
    assert "NO shutdown will be triggered" not in body
    assert "re-arms in full" in body


@pytest.mark.asyncio
async def test_a_restored_timer_is_released_by_mains_and_by_battery():
    """The two answers that end the hold, and what each of them means.

    Mains: the outage is over, everything restored is dropped. Battery: it is still
    running, and the trigger is derived from the FULL restored elapsed time on that very
    poll — so a service restart mid-outage, where the UPS is reachable by definition,
    costs nothing at all.
    """
    from app import engine as engine_mod
    import json as _json

    def _engine():
        since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        engine_mod.STATE_PATH.write_text(
            _json.dumps({"on_battery_since": {"u": since}}), encoding="utf-8")
        return Engine(AppConfig(dry_run=True, ups=[SnmpConfig(id="u", host="10.0.0.9")]))

    on_mains = _engine()
    on_mains.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    await on_mains._evaluate()
    assert on_mains.ups_rt["u"].restored_unconfirmed is False
    assert on_mains.ups_rt["u"].on_battery_since is None
    assert on_mains.ups_rt["u"].triggered is False

    still_out = _engine()
    still_out.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")
    await still_out._evaluate()
    assert still_out.ups_rt["u"].restored_unconfirmed is False
    # The restored three hours are honoured in full, not restarted from zero.
    assert still_out._ups_elapsed_on_battery(still_out.ups_rt["u"]) > 3 * 3600 - 60
    assert still_out.ups_rt["u"].triggered is True


def test_the_elapsed_source_says_when_it_is_unconfirmed():
    """/api/status has to distinguish "we measured this" from "we measured this and have
    not been able to check it since" — the number is real either way, the conclusion is
    not."""
    from app import engine as engine_mod
    import json as _json

    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    engine_mod.STATE_PATH.write_text(
        _json.dumps({"on_battery_since": {"u": since}}), encoding="utf-8")
    eng = Engine(AppConfig(ups=[SnmpConfig(id="u", host="10.0.0.9")]))
    eng.ups_rt["u"].state = UpsState(reachable=False, error="timeout")

    snap = eng.snapshot()["ups"][0]
    assert snap["elapsed_source"] == "own-unconfirmed"
    assert snap["seconds_on_battery"] is not None
    assert snap["countdown_remaining_s"] is None


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


# --- SNMP probe diagnostics (v3.1.0) ---------------------------------------
def test_probe_names_cover_every_queried_oid():
    """Every OID a profile queries must be nameable, or the probe would print bare OIDs."""
    from app import ups

    for profile in ups.PROFILES.values():
        assert set(profile.oids) <= set(ups._OBJECTS), profile.id
        assert all(ups._object_name(oid) != oid for oid in profile.oids), profile.id


def test_probe_entry_classifies_missing_objects():
    """v2c/v3 report a missing object as a per-varbind sentinel, not as an error."""
    from pysnmp.proto import rfc1905

    from app import ups

    # A list, not a dict: all three sentinels are Null values and compare equal, so as
    # dict keys they would collapse into one entry.
    cases = [
        (rfc1905.noSuchObject, "noSuchObject"),
        (rfc1905.noSuchInstance, "noSuchInstance"),
        (rfc1905.endOfMibView, "endOfMibView"),
    ]
    for sentinel, expected in cases:
        entry = ups._probe_entry(
            ups.OID_CHARGE_REMAINING, None, 0, 0, [(ups.OID_CHARGE_REMAINING, sentinel)]
        )
        assert entry.status == expected
        assert entry.name == "upsEstimatedChargeRemaining"


def test_probe_entry_maps_snmpv1_no_such_name():
    """errorStatus 2 is how SNMPv1 says 'no such object' - not a transport failure."""
    from pysnmp.proto import rfc1905

    from app import ups

    entry = ups._probe_entry(ups.OID_MINUTES_REMAINING, None, rfc1905.errorStatus.clone(2), 1, [])
    assert entry.status == "noSuchName"
    assert entry.error is None

    other = ups._probe_entry(ups.OID_MINUTES_REMAINING, None, rfc1905.errorStatus.clone(5), 3, [])
    assert other.status == "error"
    assert "index 3" in other.error


def test_probe_entry_reports_transport_errors():
    from app import ups

    entry = ups._probe_entry(ups.OID_OUTPUT_SOURCE, "No SNMP response received", 0, 0, [])
    assert entry.status == "error"
    assert entry.error == "No SNMP response received"


def test_probe_entry_interprets_enum_values():
    from pysnmp.proto.rfc1902 import Integer, OctetString

    from app import ups

    src = ups._probe_entry(
        ups.OID_OUTPUT_SOURCE, None, 0, 0, [(ups.OID_OUTPUT_SOURCE, Integer(5))]
    )
    assert src.status == "ok"
    assert src.value == "battery (5)"

    bat = ups._probe_entry(
        ups.OID_BATTERY_STATUS, None, 0, 0, [(ups.OID_BATTERY_STATUS, Integer(3))]
    )
    assert bat.value == "low (3)"

    pct = ups._probe_entry(
        ups.OID_CHARGE_REMAINING, None, 0, 0, [(ups.OID_CHARGE_REMAINING, Integer(42))]
    )
    assert pct.value == "42"

    name = ups._probe_entry(
        ups.OID_IDENT_MODEL, None, 0, 0, [(ups.OID_IDENT_MODEL, OctetString(" Smart-UPS "))]
    )
    assert name.value == "Smart-UPS"


def test_probe_entry_never_raises_on_a_hostile_value():
    from app import ups

    class Hostile:
        def prettyPrint(self):
            raise RuntimeError("boom")

        def __str__(self):
            raise RuntimeError("boom")

    entry = ups._probe_entry(ups.OID_IDENT_MODEL, None, 0, 0, [(ups.OID_IDENT_MODEL, Hostile())])
    assert entry.status == "error"
    assert "boom" in entry.error


def test_probe_entry_only_emits_declared_statuses():
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    responses = [
        ("timeout", 0, 0, []),
        (None, rfc1905.errorStatus.clone(2), 1, []),
        (None, rfc1905.errorStatus.clone(5), 1, []),
        (None, 0, 0, []),
        (None, 0, 0, [(ups.OID_OUTPUT_SOURCE, Integer(3))]),
        (None, 0, 0, [(ups.OID_OUTPUT_SOURCE, rfc1905.noSuchObject)]),
    ]
    for response in responses:
        entry = ups._probe_entry(ups.OID_OUTPUT_SOURCE, *response)
        assert entry.status in ups.PROBE_STATUSES


@pytest.mark.asyncio
async def test_probe_of_unconfigured_ups_skips_everything():
    from app import ups

    result = await ups.probe(SnmpConfig())
    assert result.reachable is False
    # Nothing was sent, so only the profile that would have been asked first is listed.
    assert result.total == len(ups.DEFAULT_PROFILE.objects)
    assert [e.status for e in result.entries] == ["skipped"] * result.total
    assert "not configured" in result.summary


@pytest.mark.asyncio
async def test_probe_closes_engine_exactly_once_and_stops_early(monkeypatch):
    """One SnmpEngine for all single-OID GETs (no fd leak), and no seven-timeout wait."""
    from app import ups

    closed: list = []
    monkeypatch.setattr(ups, "_close_engine", lambda eng: closed.append(eng))

    cfg = SnmpConfig(host="127.0.0.1", port=1, timeout_s=0.1, retries=0)
    result = await ups.probe(cfg)

    assert len(closed) == 1
    assert closed[0] is not None
    assert result.reachable is False
    assert result.ok_count == 0
    # "auto" walks every profile, so the entry list covers all of them.
    assert len(result.entries) == sum(len(p.objects) for p in ups.PROFILES.values())
    assert result.entries[0].status == "error"
    # Everything after the first failure is reported without waiting for its own timeout.
    assert all(e.status == "skipped" for e in result.entries[1:])


@pytest.mark.asyncio
async def test_probe_summary_names_the_target_but_never_the_secret(monkeypatch):
    from app import ups

    monkeypatch.setattr(ups, "_close_engine", lambda eng: None)
    cfg = SnmpConfig(host="127.0.0.1", port=1, timeout_s=0.1, retries=0, community="s3cr3t")
    result = await ups.probe(cfg)

    assert "127.0.0.1:1" in result.summary
    assert "s3cr3t" not in result.summary


def test_probe_summary_flags_the_snmpv1_multi_get_trap():
    from app import ups

    cfg = SnmpConfig(host="10.0.0.5", port=161, version=SnmpVersion.v1)
    result = ups.ProbeResult(total=2, ok_count=1)
    result.entries = [
        ups.ProbeEntry(
            oid=ups.OID_OUTPUT_SOURCE, name="upsOutputSource", status="ok", value="mains (3)"
        ),
        ups.ProbeEntry(
            oid=ups.OID_MINUTES_REMAINING,
            name="upsEstimatedMinutesRemaining",
            status="noSuchName",
        ),
    ]
    summary = ups._probe_summary(cfg, result, None, ups.RFC1628)

    assert "upsEstimatedMinutesRemaining" in summary
    assert "v2c" in summary


# --- MIB profiles (v3.3.0) -------------------------------------------------
def test_every_profile_is_structurally_sound():
    """A profile must fill real UpsState fields and be able to feed every trigger."""
    from dataclasses import fields

    from app import ups

    state_fields = {f.name for f in fields(ups.UpsState)}
    seen: dict[str, str] = {}
    # Table columns read at a FIXED index. Everything else must be a ".0" scalar: the
    # poller does a plain GET and never walks, so an OID without an instance would simply
    # come back empty. New entries here are a deliberate decision, not an oversight.
    fixed_index_oids = {
        ups.OID_OUTPUT_LOAD: "upsOutputPercentLoad on output line 1",
    }
    for profile in ups.PROFILES.values():
        assert profile.id in {m.value for m in SnmpMib}, profile.id
        assert profile.anchor in profile.oids, profile.id
        for obj in profile.objects:
            assert obj.oid.endswith(".0") or obj.oid in fixed_index_oids, obj.name
            # OIDs must be globally unique or the flat _OBJECTS registry would lose one.
            assert seen.setdefault(obj.oid, profile.id) == profile.id, obj.oid
            assert obj.field in state_fields, obj.name
            assert obj.trigger is None or obj.trigger in ups.PROBE_TRIGGERS, obj.name
            if obj.kind == ups.KIND_ENUM:
                assert obj.enum, obj.name
        # A profile that cannot feed a threshold would leave it silently dead.
        assert set(profile.trigger_oids.values()) == set(ups.PROBE_TRIGGERS), profile.id


def test_output_load_is_read_by_both_profiles_but_triggers_nothing():
    """Issue #20: the load is informational — it must never gate a shutdown condition."""
    from app import ups

    for profile in ups.PROFILES.values():
        loads = [o for o in profile.objects if o.field == "load_pct"]
        assert len(loads) == 1, profile.id
        assert loads[0].kind == ups.KIND_PCT
        # No trigger: a device that omits the load must not make the wizard warn about
        # an unavailable shutdown condition.
        assert loads[0].trigger is None
    assert "load_pct" not in ups.PROBE_TRIGGERS


def test_load_reaches_the_status_snapshot():
    from app.engine import Engine
    from app.ups import UpsState

    eng = Engine(AppConfig(ups=[SnmpConfig(id="u", host="10.0.0.9")]))
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains", load_pct=37)
    snap = eng.snapshot()
    assert snap["ups"][0]["load_pct"] == 37

    # A device that does not report it shows nothing rather than a wrong zero.
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    assert eng.snapshot()["ups"][0]["load_pct"] is None


def test_apc_timeticks_are_converted_to_seconds_and_minutes():
    """TimeTicks are hundredths of a second - the classic vendor-MIB misreading."""
    from pysnmp.proto.rfc1902 import Integer, OctetString, TimeTicks

    from app import ups

    state = ups.UpsState()
    ups._map_state(state, ups.APC, {
        ups.OID_APC_MODEL: OctetString(" Smart-UPS 1500 "),
        ups.OID_APC_OUTPUT_STATUS: Integer(3),
        ups.OID_APC_BATTERY_STATUS: Integer(3),
        ups.OID_APC_TIME_ON_BATTERY: TimeTicks(9500),
        ups.OID_APC_RUNTIME: TimeTicks(114000),
        ups.OID_APC_CAPACITY: Integer(22),
    })

    assert state.mib == "apc"
    assert state.manufacturer == "APC"        # PowerNet has no manufacturer object
    assert state.model == "Smart-UPS 1500"
    assert state.power_source == "battery" and state.on_battery is True
    assert state.battery_status == "low" and state.battery_low is True
    assert state.seconds_on_battery == 95     # 9500 / 100
    assert state.runtime_remaining_min == 19  # 114000 / 6000
    assert state.battery_charge_pct == 22
    assert state.reachable is True


def test_apc_rounds_the_runtime_down():
    """9.9 minutes must not clear a "below 10 minutes" threshold by rounding up."""
    from pysnmp.proto.rfc1902 import TimeTicks

    from app import ups

    state = ups.UpsState()
    ups._map_state(state, ups.APC, {ups.OID_APC_RUNTIME: TimeTicks(59400)})  # 9.9 min
    assert state.runtime_remaining_min == 9


def test_apc_self_test_is_not_reported_as_an_outage():
    """onBatteryTest(15) runs on battery but is a self-test - this app schedules them."""
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    state = ups.UpsState()
    ups._map_state(state, ups.APC, {ups.OID_APC_OUTPUT_STATUS: Integer(15)})
    assert state.power_source == "mains"
    assert state.on_battery is False


def test_map_state_ignores_objects_the_device_does_not_implement():
    """A missing object keeps the field's default instead of poisoning it with a sentinel."""
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    state = ups.UpsState()
    ups._map_state(state, ups.RFC1628, {
        ups.OID_OUTPUT_SOURCE: Integer(3),
        ups.OID_CHARGE_REMAINING: rfc1905.noSuchObject,
    })
    assert state.power_source == "mains"
    assert state.battery_charge_pct is None
    assert state.reachable is True


def test_a_device_answering_no_object_of_the_mib_counts_as_unreachable():
    """Pinning a UPS to the wrong MIB must not look healthy.

    The agent replies, so the GET "succeeds" - but every object is missing. Reporting that
    as reachable would show a card with no values, no alarm and no trigger that can ever
    fire, which is exactly the fail-dangerous case the fail-safe rules exist to prevent.
    """
    from pysnmp.proto import rfc1905

    from app import ups

    state = ups.UpsState()
    ups._map_state(state, ups.APC, {oid: rfc1905.noSuchInstance for oid in ups.APC.oids})

    assert state.reachable is False
    assert state.on_battery is False        # unreachable is an alarm, never a shutdown
    assert "implements none of the APC PowerNet objects" in state.error


def test_a_reading_without_a_usable_power_source_counts_as_unreachable():
    """Other objects answer, the one that decides the state machine does not.

    ``on_battery`` is ``power_source == "battery"``, so an unresolved source reads as
    MAINS — the one fail-dangerous default in the engine. app/nut.py has always refused
    this (no ups.status -> unreachable); the SNMP path did not.
    """
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    # RFC 1628: upsOutputSource simply absent, the charge answers perfectly well.
    absent = ups.UpsState()
    ups._map_state(absent, ups.RFC1628, {
        ups.OID_OUTPUT_SOURCE: rfc1905.noSuchObject,
        ups.OID_CHARGE_REMAINING: Integer(55),
    })
    assert absent.reachable is False and absent.on_battery is False
    assert "upsOutputSource" in absent.error

    # APC: the object answers, with unknown(1) — mapped to "unknown", not to a source.
    unknown = ups.UpsState()
    ups._map_state(unknown, ups.APC, {
        ups.OID_APC_OUTPUT_STATUS: Integer(1),
        ups.OID_APC_CAPACITY: Integer(42),
    })
    assert unknown.reachable is False and unknown.on_battery is False
    assert "upsBasicOutputStatus" in unknown.error

    # And the normal case is untouched: a source that resolves stays reachable.
    ok = ups.UpsState()
    ups._map_state(ok, ups.APC, {ups.OID_APC_OUTPUT_STATUS: Integer(3)})
    assert ok.reachable is True and ok.on_battery is True


@pytest.mark.asyncio
async def test_an_unresolved_power_source_does_not_end_a_running_outage():
    """The engine consequence of the reading above, which is where it hurt.

    Mid-outage such a poll used to take the mains branch: the on-battery timer was
    cleared, a latched trigger dropped and the event log said "mains power restored" —
    on a UPS that had reported nothing of the sort.
    """
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    cfg = AppConfig(
        dry_run=True,
        ups=[SnmpConfig(id="a", name="UPS-A", host="10.0.0.9")],
        hosts=[PveHostConfig(name="pve01", api_url="https://x:8006", ups_ids=["a"])],
        thresholds=Thresholds(on_battery_seconds=600, runtime_below_minutes=None,
                              charge_below_percent=None, on_battery_low=True,
                              unreachable_alarm_after_polls=1),
    )
    eng = Engine(cfg)
    rt = eng.ups_rt["a"]

    rt.state = UpsState(reachable=True, power_source="battery", battery_status="normal")
    await eng._evaluate()
    assert eng.state == ON_BATTERY and rt.on_battery_since is not None
    started = rt.on_battery_since

    blind = ups.UpsState()
    ups._map_state(blind, ups.APC, {ups.OID_APC_OUTPUT_STATUS: Integer(1)})
    rt.state = blind
    await eng._evaluate()

    # The outage is still on, the timer never restarted, and it is an alarm.
    assert eng.state == ON_BATTERY
    assert rt.on_battery_since == started
    assert rt.alarm_active is True


def test_an_unusable_reading_is_still_an_answer():
    """``reachable`` and ``answered`` are two questions, and only one of them is "silent".

    Both branches that end in reachable=False here were reached by an agent that replied:
    one named no usable power source, the other implemented none of the profile's objects.
    Neither is a communication loss — which matters because a communication loss is a
    shutdown trigger (see the opt-in below).
    """
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    no_source = ups.UpsState()
    ups._map_state(no_source, ups.APC, {ups.OID_APC_OUTPUT_STATUS: Integer(1)})
    assert no_source.reachable is False and no_source.answered is True

    nothing = ups.UpsState()
    ups._map_state(nothing, ups.APC,
                   {oid: rfc1905.noSuchInstance for oid in ups.APC.oids})
    assert nothing.reachable is False and nothing.answered is True

    # A device that never spoke: the poller returns a bare state, and that one IS silence.
    assert ups.UpsState(error="timeout").answered is False


def test_rfc1628_other_is_read_as_no_verdict_not_as_mains():
    """upsOutputSource other(1) is the MIB's own "none of the below" — i.e. unknown.

    Mapped to a source it was simply "not battery", so a card falling back to it mid-outage
    cleared the timer and logged "mains power restored". The APC enum keeps its own
    "other" for rebooting(8), which is a definite state.
    """
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    state = ups.UpsState()
    ups._map_state(state, ups.RFC1628, {
        ups.OID_OUTPUT_SOURCE: Integer(1),
        ups.OID_CHARGE_REMAINING: Integer(88),
    })
    assert state.power_source == "unknown"
    assert state.reachable is False and state.on_battery is False
    assert ups._APC_OUTPUT_STATUS[8] == "other"  # unchanged, deliberately


@pytest.mark.asyncio
async def test_a_device_that_answers_never_triggers_the_pure_comms_loss_shutdown():
    """The opt-in shuts the estate down on SILENCE, and an answer is not silence.

    A card that replies but names no usable power source is reachable=False — correctly,
    the state machine has nothing to go on. Reading that as a communication loss fired a
    real shutdown during normal operation, minutes after the threshold, on a device that
    had never stopped talking.
    """
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    cfg = AppConfig(
        dry_run=True,
        ups=[SnmpConfig(id="a", name="UPS-A", host="10.0.0.9")],
        hosts=[PveHostConfig(name="pve01", api_url="https://x:8006", ups_ids=["a"])],
        thresholds=Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                              charge_below_percent=None, on_battery_low=True,
                              unreachable_alarm_after_polls=1,
                              comm_loss_shutdown_after_min=1),
    )
    eng = Engine(cfg)
    rt = eng.ups_rt["a"]

    answering = ups.UpsState()
    ups._map_state(answering, ups.APC, {ups.OID_APC_OUTPUT_STATUS: Integer(1)})
    rt.state = answering
    await eng._evaluate()
    # Long past the threshold on the wall clock the opt-in measures.
    rt.unreachable_since = datetime.now(timezone.utc) - timedelta(minutes=5)
    await eng._evaluate()

    assert rt.triggered is False and rt.trigger_reason is None
    assert eng.shutdown_triggered is False
    # And no countdown is advertised for a shutdown that deliberately never comes.
    assert eng._ups_comm_loss_remaining_s(cfg.ups[0], rt) is None
    assert rt.alarm_active is True  # still an alarm, that half is unchanged

    # A device that really is silent still arms it — the opt-in itself is untouched.
    rt.state = UpsState(error="timeout")
    rt.unreachable_since = datetime.now(timezone.utc) - timedelta(minutes=5)
    await eng._evaluate()
    assert rt.triggered is True and "communication lost" in rt.trigger_reason


@pytest.mark.asyncio
async def test_a_nut_status_naming_no_power_source_does_not_end_a_running_outage():
    """The NUT half of the reading above — the two sources have to agree.

    A missing ``ups.status`` was always refused, but a status that is *present* and names
    neither OL nor OB (an empty value, CHRG, ALARM, a driver's intermediate state during a
    transfer) left power_source "unknown" while staying reachable. ``on_battery`` is
    ``power_source == "battery"``, so that fell into the MAINS branch: mid-outage it
    cleared the timer, dropped a latched trigger and logged "mains power restored".
    """
    from app import nut

    cfg = AppConfig(
        dry_run=True,
        ups=[NutConfig(id="a", name="UPS-A", host="10.0.0.9", ups_name="ups")],
        hosts=[PveHostConfig(name="pve01", api_url="https://x:8006", ups_ids=["a"])],
        thresholds=Thresholds(on_battery_seconds=600, runtime_below_minutes=None,
                              charge_below_percent=None, on_battery_low=True,
                              unreachable_alarm_after_polls=1),
    )
    eng = Engine(cfg)
    rt = eng.ups_rt["a"]

    rt.state = UpsState(reachable=True, power_source="battery", battery_status="normal")
    await eng._evaluate()
    assert eng.state == ON_BATTERY and rt.on_battery_since is not None
    started = rt.on_battery_since

    blind = UpsState()
    nut._apply_variables(blind, {"ups.status": "ALARM", "battery.charge": "44"})
    assert blind.reachable is False and blind.answered is True
    rt.state = blind
    await eng._evaluate()

    assert eng.state == ON_BATTERY
    assert rt.on_battery_since == started
    assert rt.alarm_active is True


@pytest.mark.asyncio
async def test_a_nut_server_that_answers_err_never_triggers_the_comms_loss_shutdown():
    """upsd replying ERR DATA-STALE is talking to us; the opt-in fires on silence only.

    The NUT sibling of the SNMP case above. A wedged driver, a wrong password or a
    mistyped ups_name reaches the poller as a protocol error — an answer — and reading it
    as a communication loss shut the whole estate down during normal operation.
    """
    from app import nut

    cfg = AppConfig(
        dry_run=True,
        ups=[NutConfig(id="a", name="UPS-A", host="10.0.0.9", ups_name="ups")],
        hosts=[PveHostConfig(name="pve01", api_url="https://x:8006", ups_ids=["a"])],
        thresholds=Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                              charge_below_percent=None, on_battery_low=True,
                              unreachable_alarm_after_polls=1,
                              comm_loss_shutdown_after_min=1),
    )
    eng = Engine(cfg)
    rt = eng.ups_rt["a"]

    stale = UpsState(error=str(nut._NutError("DATA-STALE")), answered=True)
    rt.state = stale
    await eng._evaluate()
    rt.unreachable_since = datetime.now(timezone.utc) - timedelta(minutes=5)
    await eng._evaluate()

    assert rt.triggered is False and rt.trigger_reason is None
    assert eng.shutdown_triggered is False
    assert eng._ups_comm_loss_remaining_s(cfg.ups[0], rt) is None
    assert rt.alarm_active is True  # still an alarm


@pytest.mark.asyncio
async def test_an_unpollable_ups_never_triggers_the_comms_loss_shutdown():
    """An entry with no address is silent by construction — and has produced no evidence.

    poll() returns "not configured" without touching the network, so such a UPS is
    unreachable and silent for ever. The appliance calls exactly that state fail safe ("a
    standing refusal to shut down every host it feeds"), and with the opt-in on it did the
    opposite: a configuration mistake, a backup import or a hand-edited config.yaml shut
    down every host the entry feeds, minutes later, during normal operation, without a
    single packet having been sent.
    """
    cfg = AppConfig(
        dry_run=True,
        ups=[SnmpConfig(id="a", name="UPS-A", host="")],  # never pollable
        hosts=[PveHostConfig(name="pve01", api_url="https://x:8006", ups_ids=["a"])],
        thresholds=Thresholds(on_battery_seconds=None, runtime_below_minutes=None,
                              charge_below_percent=None, on_battery_low=True,
                              unreachable_alarm_after_polls=1,
                              comm_loss_shutdown_after_min=1),
    )
    assert cfg.incomplete_ups(cfg.ups[0])  # the premise of this test
    eng = Engine(cfg)
    rt = eng.ups_rt["a"]

    rt.state = UpsState(error="SNMP not configured")
    await eng._evaluate()
    rt.unreachable_since = datetime.now(timezone.utc) - timedelta(minutes=5)
    await eng._evaluate()

    assert rt.triggered is False and rt.trigger_reason is None
    assert eng.shutdown_triggered is False
    assert eng._ups_comm_loss_remaining_s(cfg.ups[0], rt) is None
    assert rt.alarm_active is True

    # Completing the entry restores the opt-in: this guard is about the missing address,
    # not about switching the feature off.
    cfg.ups[0].host = "10.0.0.9"
    rt.unreachable_since = datetime.now(timezone.utc) - timedelta(minutes=5)
    await eng._evaluate()
    assert rt.triggered is True and "communication lost" in rt.trigger_reason


def test_probe_entry_interprets_apc_values():
    from pysnmp.proto.rfc1902 import Integer, TimeTicks

    from app import ups

    src = ups._probe_entry(
        ups.OID_APC_OUTPUT_STATUS, None, 0, 0, [(ups.OID_APC_OUTPUT_STATUS, Integer(3))]
    )
    assert src.name == "upsBasicOutputStatus"
    assert src.value == "battery (3)"

    # The converted value is shown, the raw one stays in .raw - a unit bug is visible.
    runtime = ups._probe_entry(
        ups.OID_APC_RUNTIME, None, 0, 0, [(ups.OID_APC_RUNTIME, TimeTicks(114000))]
    )
    assert runtime.value == "19 min"
    assert "114000" in runtime.raw


def _script_snmp_get(monkeypatch, responses):
    """Replace pysnmp's GET with a scripted response list. Returns the per-call varbind
    counts, which is enough to tell a union GET from a plain one."""
    import pysnmp.hlapi.asyncio as hlapi

    from app import ups

    asked: list[int] = []

    async def fake_get(engine, auth, transport, context, *objects):
        asked.append(len(objects))
        return responses[len(asked) - 1]

    for name in ("getCmd", "get_cmd"):
        if hasattr(hlapi, name):
            monkeypatch.setattr(hlapi, name, fake_get)
    monkeypatch.setattr(ups, "_close_engine", lambda eng: None)
    return asked


@pytest.mark.asyncio
async def test_auto_switches_to_the_vendor_mib_when_its_anchor_answers(monkeypatch):
    """v2c: the standard GET carries the vendor anchor, so detection costs no round trip."""
    from pysnmp.proto.rfc1902 import Integer, TimeTicks

    from app import ups

    responses = [
        # RFC 1628 objects + the APC anchor: the card answers the anchor, so it speaks APC.
        (None, 0, 0, [(ups.OID_APC_OUTPUT_STATUS, Integer(2))]),
        (None, 0, 0, [
            (ups.OID_APC_OUTPUT_STATUS, Integer(3)),
            (ups.OID_APC_RUNTIME, TimeTicks(114000)),
            (ups.OID_APC_CAPACITY, Integer(80)),
        ]),
    ]
    asked = _script_snmp_get(monkeypatch, responses)

    state = await ups.poll(SnmpConfig(host="127.0.0.1", mib=SnmpMib.auto))

    # One anchor per vendor profile rides along with the standard GET.
    assert asked == [len(ups.RFC1628.objects) + len(ups.PROFILES) - 1, len(ups.APC.objects)]
    assert state.mib == "apc"
    assert state.runtime_remaining_min == 19
    assert state.reachable is True


@pytest.mark.asyncio
async def test_auto_stays_on_the_standard_when_no_vendor_anchor_answers(monkeypatch):
    """An RFC 1628 device must cost exactly one GET, as it did before MIB profiles."""
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    responses = [(None, 0, 0, [
        (ups.OID_OUTPUT_SOURCE, Integer(3)),
        (ups.OID_MINUTES_REMAINING, Integer(42)),
        (ups.OID_APC_OUTPUT_STATUS, rfc1905.noSuchObject),
    ])]
    asked = _script_snmp_get(monkeypatch, responses)

    state = await ups.poll(SnmpConfig(host="127.0.0.1", mib=SnmpMib.auto))

    assert asked == [len(ups.RFC1628.objects) + len(ups.PROFILES) - 1]
    assert state.mib == "rfc1628"
    assert state.power_source == "mains"
    assert state.runtime_remaining_min == 42


@pytest.mark.asyncio
async def test_explicit_mib_asks_for_that_profile_only(monkeypatch):
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    responses = [(None, 0, 0, [(ups.OID_APC_OUTPUT_STATUS, Integer(3))])]
    asked = _script_snmp_get(monkeypatch, responses)

    state = await ups.poll(SnmpConfig(host="127.0.0.1", mib=SnmpMib.apc))

    assert asked == [len(ups.APC.objects)]  # no anchor appended, no detection
    assert state.mib == "apc"


@pytest.mark.asyncio
async def test_auto_falls_back_to_the_vendor_mib_under_snmpv1(monkeypatch):
    """v1 aborts a GET over one missing object - which is how an APC NMC1 answers."""
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    responses = [
        (None, rfc1905.errorStatus.clone(2), 1, []),        # noSuchName on RFC 1628
        (None, 0, 0, [(ups.OID_APC_OUTPUT_STATUS, Integer(3))]),
    ]
    asked = _script_snmp_get(monkeypatch, responses)

    cfg = SnmpConfig(host="127.0.0.1", version=SnmpVersion.v1, mib=SnmpMib.auto)
    state = await ups.poll(cfg)

    # The anchor is never appended under v1 - it would break every non-vendor device.
    assert asked == [len(ups.RFC1628.objects), len(ups.APC.objects)]
    assert state.mib == "apc"
    assert state.power_source == "battery"


@pytest.mark.asyncio
async def test_auto_does_not_spend_a_second_timeout_on_an_unreachable_ups(monkeypatch):
    """A timeout means "not reachable", not "wrong MIB". Two timeouts per poll would
    exceed the 8 s on-battery interval and delay the outage response."""
    from app import ups

    for version in (SnmpVersion.v1, SnmpVersion.v2c):
        responses = [("No SNMP response received before timeout", 0, 0, [])] * 2
        asked = _script_snmp_get(monkeypatch, responses)

        state = await ups.poll(SnmpConfig(host="127.0.0.1", version=version, mib=SnmpMib.auto))

        assert len(asked) == 1, version
        assert state.reachable is False
        assert "timeout" in state.error


# --- CyberPower: read one object per request (#40) ---------------------------
def _snmp_agent(monkeypatch, answer):
    """Replace pysnmp's GET with a function of the requested OIDs. Returns the OID list of
    every request, so a test can tell one multi-object GET from several single ones."""
    import pysnmp.hlapi.asyncio as hlapi

    from app import ups

    asked: list[list[str]] = []

    async def fake_get(engine, auth, transport, context, *objects):
        oids = list(objects)
        asked.append(oids)
        return answer(oids)

    for name in ("getCmd", "get_cmd"):
        if hasattr(hlapi, name):
            monkeypatch.setattr(hlapi, name, fake_get)
    # Pass the OID strings straight through, so the fake sees what was asked for without
    # resolving pysnmp objects (which needs a MIB view this fake does not have).
    monkeypatch.setattr(hlapi, "ObjectIdentity", lambda oid: oid)
    monkeypatch.setattr(hlapi, "ObjectType", lambda ident: ident)
    monkeypatch.setattr(ups, "_close_engine", lambda eng: None)
    return asked


def _cyberpower_card(on_battery=True, refuse=(), multi_times_out=True):
    """A CyberPower card as reported in #40: CPS-MIB only, and silent (a timeout, not an
    error) on any request that carries more than one object."""
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer, OctetString, TimeTicks

    from app import ups

    values = {
        ups.OID_CPS_MODEL: OctetString("OR1500ELCDRM1U"),
        ups.OID_CPS_OUTPUT_STATUS: Integer(3 if on_battery else 2),
        ups.OID_CPS_BATTERY_STATUS: Integer(3 if on_battery else 2),
        ups.OID_CPS_TIME_ON_BATTERY: TimeTicks(42000),
        ups.OID_CPS_RUNTIME: TimeTicks(24000),
        ups.OID_CPS_CAPACITY: Integer(18),
        ups.OID_CPS_LOAD: Integer(31),
    }

    def answer(oids):
        if len(oids) > 1 and multi_times_out:
            return ("No SNMP response received before timeout", 0, 0, [])
        oid = oids[0]
        if oid in refuse or oid not in values:
            return (None, rfc1905.errorStatus.clone(2), 1, [])  # v1 noSuchName
        return (None, 0, 0, [(oid, values[oid])])

    return answer


@pytest.mark.asyncio
async def test_cyberpower_is_read_one_object_per_request(monkeypatch):
    from app import ups

    asked = _snmp_agent(monkeypatch, _cyberpower_card())

    state = await ups.poll(SnmpConfig(host="127.0.0.1", version=SnmpVersion.v1,
                                      mib=SnmpMib.cyberpower))

    assert [len(a) for a in asked] == [1] * len(ups.CYBERPOWER.objects)
    assert state.reachable is True and state.mib == "cyberpower"
    assert state.manufacturer == "CyberPower" and state.model == "OR1500ELCDRM1U"
    assert state.power_source == "battery" and state.battery_status == "low"
    # TimeTicks, hundredths of a second: 24000 -> 4 min, 42000 -> 420 s.
    assert state.runtime_remaining_min == 4
    assert state.seconds_on_battery == 420
    assert state.battery_charge_pct == 18 and state.load_pct == 31


@pytest.mark.asyncio
async def test_cyberpower_single_reads_stop_at_the_first_timeout(monkeypatch):
    """A dead card must cost one timeout, exactly like the multi-object GET it replaces —
    not one per object, which would blow the poll budget."""
    from app import ups

    asked = _snmp_agent(
        monkeypatch, lambda oids: ("No SNMP response received before timeout", 0, 0, []))

    state = await ups.poll(SnmpConfig(host="127.0.0.1", mib=SnmpMib.cyberpower))

    assert len(asked) == 1
    assert state.reachable is False and state.answered is False
    assert "timeout" in state.error


@pytest.mark.asyncio
async def test_cyberpower_object_refused_on_its_own_is_just_missing(monkeypatch):
    """One GET per object also ends v1's all-or-nothing: a refused load reading leaves
    every other value, and the UPS, intact."""
    from app import ups

    _snmp_agent(monkeypatch, _cyberpower_card(refuse=(ups.OID_CPS_LOAD,)))

    state = await ups.poll(SnmpConfig(host="127.0.0.1", version=SnmpVersion.v1,
                                      mib=SnmpMib.cyberpower))

    assert state.reachable is True
    assert state.load_pct is None
    assert state.runtime_remaining_min == 4


@pytest.mark.asyncio
async def test_cyberpower_without_its_source_object_is_unreachable_not_mains(monkeypatch):
    """The fail-safe rule holds on the new path: no output status, no verdict."""
    from app import ups

    _snmp_agent(monkeypatch, _cyberpower_card(refuse=(ups.OID_CPS_OUTPUT_STATUS,)))

    state = await ups.poll(SnmpConfig(host="127.0.0.1", mib=SnmpMib.cyberpower))

    assert state.reachable is False and state.answered is True
    assert state.power_source == "unknown"


@pytest.mark.asyncio
async def test_auto_detects_cyberpower_by_its_anchor_under_v2c(monkeypatch):
    """A card that does answer multi-object requests is found by "auto" like APC, and
    then read one object at a time all the same."""
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    card = _cyberpower_card(on_battery=False, multi_times_out=False)

    def answer(oids):
        if len(oids) > 1:  # the standard GET with the vendor anchors appended
            return (None, 0, 0, [
                (o, Integer(2) if o == ups.OID_CPS_OUTPUT_STATUS else rfc1905.noSuchObject)
                for o in oids])
        return card(oids)

    asked = _snmp_agent(monkeypatch, answer)

    state = await ups.poll(SnmpConfig(host="127.0.0.1", mib=SnmpMib.auto))

    assert ups.OID_CPS_OUTPUT_STATUS in asked[0]
    assert [len(a) for a in asked[1:]] == [1] * len(ups.CYBERPOWER.objects)
    assert state.mib == "cyberpower" and state.power_source == "mains"


@pytest.mark.asyncio
async def test_auto_falls_back_to_cyberpower_under_snmpv1(monkeypatch):
    """v1 "auto": RFC 1628 and APC are refused as a whole, CyberPower is then read one
    object per request rather than as the multi-object GET this card would not answer."""
    from pysnmp.proto import rfc1905

    from app import ups

    card = _cyberpower_card()

    def answer(oids):
        if len(oids) > 1 and (set(oids) <= set(ups.RFC1628.oids) or set(oids) <= set(ups.APC.oids)):
            return (None, rfc1905.errorStatus.clone(2), 1, [])
        return card(oids)

    asked = _snmp_agent(monkeypatch, answer)

    state = await ups.poll(SnmpConfig(host="127.0.0.1", version=SnmpVersion.v1,
                                      mib=SnmpMib.auto))

    assert [len(a) for a in asked] == (
        [len(ups.RFC1628.objects), len(ups.APC.objects)] + [1] * len(ups.CYBERPOWER.objects))
    assert state.mib == "cyberpower" and state.power_source == "battery"


def _nonconforming_v2c_card(speaks_rfc=True, speaks_cps=True):
    """Modelled on a real CyberPower RMCARD (OR600ERM1U) over v2c: a missing object is
    answered with v1's noSuchName for the WHOLE request, never with the v2c sentinel."""
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    known = {}
    if speaks_rfc:
        known.update({o: Integer(3 if o == ups.OID_OUTPUT_SOURCE else 26)
                      for o in ups.RFC1628.oids})
    if speaks_cps:
        known.update({o: Integer(2 if o == ups.OID_CPS_OUTPUT_STATUS else 37)
                      for o in ups.CYBERPOWER.oids})

    def answer(oids):
        for i, oid in enumerate(oids, start=1):
            if oid not in known:
                return (None, rfc1905.errorStatus.clone(2), i, [])
        return (None, 0, 0, [(o, known[o]) for o in oids])

    return answer


@pytest.mark.asyncio
async def test_auto_survives_a_v2c_agent_that_refuses_a_missing_anchor(monkeypatch):
    """Found on real hardware: the APC anchor appended to the standard GET made "auto"
    fail on every poll of a CyberPower card that answers everything else."""
    from app import ups

    asked = _snmp_agent(monkeypatch, _nonconforming_v2c_card())

    state = await ups.poll(SnmpConfig(host="127.0.0.1", mib=SnmpMib.auto))

    assert state.reachable is True and state.mib == "cyberpower"
    assert state.power_source == "mains"
    # The refused union, then each vendor anchor on its own, then CyberPower per object.
    vendors = len(ups.PROFILES) - 1
    assert [len(a) for a in asked] == (
        [len(ups.RFC1628.objects) + vendors] + [1] * vendors
        + [1] * len(ups.CYBERPOWER.objects))


@pytest.mark.asyncio
async def test_auto_reads_the_standard_when_no_anchor_answers_on_its_own(monkeypatch):
    from app import ups

    asked = _snmp_agent(monkeypatch, _nonconforming_v2c_card(speaks_cps=False))

    state = await ups.poll(SnmpConfig(host="127.0.0.1", mib=SnmpMib.auto))

    assert state.reachable is True and state.mib == "rfc1628"
    assert asked[-1] == ups.RFC1628.oids


@pytest.mark.asyncio
async def test_v1_fallback_keeps_the_original_refusal_when_no_vendor_answers(monkeypatch):
    """A per-object profile is never refused as a whole; that must not make it "win"
    with nothing in it, nor crash the error message."""
    from pysnmp.proto import rfc1905

    from app import ups

    _snmp_agent(monkeypatch, lambda oids: (None, rfc1905.errorStatus.clone(2), 1, []))

    state = await ups.poll(SnmpConfig(host="127.0.0.1", version=SnmpVersion.v1,
                                      mib=SnmpMib.auto))

    assert state.reachable is False and state.answered is True
    assert "noSuchName" in state.error


def test_cyberpower_output_status_mapping():
    from app import ups

    enum = ups._CPS_OUTPUT_STATUS
    assert enum[2] == "mains" and enum[3] == "battery"
    # Boost, ECO, buck and overload are all still on mains — none may start the timer.
    assert {enum[4], enum[8], enum[10], enum[11]} == {"mains"}
    assert enum[9] == "bypass" and enum[1] == "unknown"


def _probe_result(mib, reachable=True):
    from app.ups import ProbeResult

    return ProbeResult(reachable=reachable, mib=mib, summary="probe says hi.")


def test_batching_hint_tells_auto_users_to_pin_cyberpower():
    """#40: poll silent, probe answering on CyberPower -> say what to do about it."""
    from app import ups

    silent = ups.UpsState(error="No SNMP response received before timeout")
    hint = ups.batching_hint(SnmpConfig(host="h", mib=SnmpMib.auto), silent,
                             _probe_result("cyberpower"))

    assert hint and "'CyberPower'" in hint and "explicitly" in hint


def test_batching_hint_stays_quiet_when_it_has_nothing_to_add():
    from app import ups

    silent = ups.UpsState(error="timeout")
    answered = ups.UpsState(answered=True)
    auto = SnmpConfig(host="h", mib=SnmpMib.auto)

    # The poll answered: nothing to explain.
    assert ups.batching_hint(auto, answered, _probe_result("cyberpower")) is None
    # Nothing answered the probe either: plain unreachable, the summary says so already.
    assert ups.batching_hint(auto, silent, _probe_result("rfc1628", reachable=False)) is None
    # Already pinned to the single-object MIB: the timeout is something else.
    assert ups.batching_hint(SnmpConfig(host="h", mib=SnmpMib.cyberpower), silent,
                             _probe_result("cyberpower")) is None
    # A multi-object MIB that answers singly: worth a report, not a setting to change.
    other = ups.batching_hint(auto, silent, _probe_result("rfc1628"))
    assert other and "report" in other


@pytest.mark.asyncio
async def test_ups_test_endpoint_appends_the_batching_hint(_import_target, monkeypatch):
    main, _ = _import_target
    from app import ups

    _snmp_agent(monkeypatch, _cyberpower_card())

    result = await main.api_test_ups({"type": "snmp", "host": "127.0.0.1",
                                      "version": "v1", "mib": "auto"})

    assert result["reachable"] is False
    assert result["probe"]["mib"] == "cyberpower"
    assert "explicitly" in result["probe"]["summary"]
    assert ups.CYBERPOWER.label in result["probe"]["summary"]


@pytest.mark.asyncio
async def test_probe_counts_only_the_resolved_profile(monkeypatch):
    """An APC card answers 6 of 13 probed objects - reporting that as a defect would make
    the wizard cry wolf on a perfectly healthy device."""
    from pysnmp.proto import rfc1905
    from pysnmp.proto.rfc1902 import Integer

    from app import ups

    def answer(oid):
        if oid in ups.APC.oids:
            return (None, 0, 0, [(oid, Integer(3 if oid == ups.OID_APC_OUTPUT_STATUS else 5))])
        return (None, 0, 0, [(oid, rfc1905.noSuchObject)])

    responses = [answer(oid) for p in ups.PROFILES.values() for oid in p.oids]
    asked = _script_snmp_get(monkeypatch, responses)

    result = await ups.probe(SnmpConfig(host="127.0.0.1", mib=SnmpMib.auto))

    assert len(asked) == sum(len(p.objects) for p in ups.PROFILES.values())
    assert result.mib == "apc"
    assert result.reachable is True
    assert (result.ok_count, result.total) == (len(ups.APC.objects), len(ups.APC.objects))
    assert result.missing_triggers == []
    # The resolved MIB is listed first; the standard's objects stay on for diagnosis.
    assert [e.oid for e in result.entries[:len(ups.APC.objects)]] == ups.APC.oids
    assert all(e.status == "noSuchObject" for e in result.entries[len(ups.APC.objects):])
    assert "does not implement RFC 1628" in result.summary


@pytest.mark.asyncio
async def test_api_test_ups_returns_probe_details_without_secrets(monkeypatch):
    """The test endpoint carries the per-object diagnosis and never echoes credentials."""
    import json

    from app import main, ups

    cfg = AppConfig(ups=[SnmpConfig(id="u1", host="10.0.0.5", community="s3cr3t")])
    monkeypatch.setattr(main, "engine", Engine(cfg))

    async def fake_poll(_cfg):
        return UpsState(reachable=True, power_source="mains", battery_status="normal")

    async def fake_probe(_cfg):
        result = ups.ProbeResult(
            reachable=True, summary="All 1 objects answered.", ok_count=1, total=1
        )
        result.entries = [
            ups.ProbeEntry(
                oid=ups.OID_OUTPUT_SOURCE,
                name="upsOutputSource",
                status="ok",
                value="mains (3)",
                raw="3",
            )
        ]
        result.missing_triggers = ["runtime"]
        return result

    monkeypatch.setattr(main.sources, "poll", fake_poll)
    monkeypatch.setattr(main.sources, "probe", fake_probe)

    # Masked community -> reconciled from the stored UPS, so the stored secret is in play.
    body = await main.api_test_ups(
        {"id": "u1", "host": "10.0.0.5", "community": main.SECRET_PLACEHOLDER}
    )

    assert body["reachable"] is True
    assert body["probe"]["entries"][0]["name"] == "upsOutputSource"
    assert body["probe"]["ok_count"] == 1
    assert body["probe"]["missing_triggers"] == ["runtime"]
    assert "s3cr3t" not in json.dumps(body)

    # /api/test/snmp stays as a 3.1.x-compatible alias for the same behaviour.
    legacy = await main.api_test_snmp({"id": "u1", "host": "10.0.0.5"})
    assert legacy["reachable"] is True


@pytest.mark.asyncio
async def test_api_test_ups_dispatches_on_the_source_type(monkeypatch):
    """A NUT entry must be validated as NutConfig, not silently as an SNMP one."""
    from app import main

    monkeypatch.setattr(main, "engine", Engine(AppConfig()))
    seen = {}

    async def fake_poll(cfg):
        seen["cls"] = type(cfg).__name__
        seen["ups_name"] = getattr(cfg, "ups_name", None)
        return UpsState(reachable=True, power_source="battery", battery_status="normal")

    async def fake_probe(_cfg):
        from app import ups as ups_mod

        return ups_mod.ProbeResult(reachable=True, summary="ok", ok_count=5, total=5)

    monkeypatch.setattr(main.sources, "poll", fake_poll)
    monkeypatch.setattr(main.sources, "probe", fake_probe)

    body = await main.api_test_ups(
        {"id": "n1", "type": "nut", "host": "10.0.0.7", "port": 3493, "ups_name": "myups"}
    )
    assert seen == {"cls": "NutConfig", "ups_name": "myups"}
    assert body["power_source"] == "battery"


# --- self-test schedule (v3.1.0) -------------------------------------------
def test_selftest_slot_is_anchored_at_the_start_hour():
    from app.engine import selftest_slot

    # 09:00 + every 6 h -> 09:00, 15:00, 21:00, 03:00
    assert selftest_slot(datetime(2026, 7, 25, 15, 7), 9, 360) == datetime(2026, 7, 25, 15, 0)
    assert selftest_slot(datetime(2026, 7, 25, 9, 0), 9, 360) == datetime(2026, 7, 25, 9, 0)


def test_selftest_slot_wraps_across_midnight():
    from app.engine import selftest_slot

    # 03:00 belongs to today's grid, not to tomorrow's.
    assert selftest_slot(datetime(2026, 7, 25, 3, 30), 9, 360) == datetime(2026, 7, 25, 3, 0)
    # Anchor 23:00, every 2 h: 00:30 falls into yesterday's 23:00 slot.
    assert selftest_slot(datetime(2026, 7, 25, 0, 30), 23, 120) == datetime(2026, 7, 24, 23, 0)


def test_selftest_slot_daily_default_matches_the_old_behaviour():
    from app.engine import selftest_slot

    assert selftest_slot(datetime(2026, 7, 25, 8, 59), 9, 1440) == datetime(2026, 7, 24, 9, 0)
    assert selftest_slot(datetime(2026, 7, 25, 9, 0), 9, 1440) == datetime(2026, 7, 25, 9, 0)
    assert selftest_slot(datetime(2026, 7, 25, 23, 59), 9, 1440) == datetime(2026, 7, 25, 9, 0)


def test_selftest_slot_grid_is_exact_for_every_supported_interval():
    from app.config import SELFTEST_INTERVALS
    from app.engine import selftest_slot

    for interval in SELFTEST_INTERVALS:
        day = datetime(2026, 7, 25)
        slots = [selftest_slot(day + timedelta(minutes=m), 9, interval) for m in range(0, 1440, 5)]
        # Compare times of day: a day's worth of samples spans one grid period more than
        # the grid itself (the 00:00 sample belongs to yesterday's last slot).
        assert len({(s.hour, s.minute) for s in slots}) == 1440 // interval, interval
        assert slots == sorted(slots), interval  # monotonically non-decreasing
        for slot in slots:
            assert (slot.hour * 60 + slot.minute - 9 * 60) % interval == 0, (interval, slot)


def test_selftest_slot_tolerates_broken_values():
    from app.engine import selftest_slot

    assert selftest_slot(datetime(2026, 7, 25, 10, 0), 99, 0) is not None
    assert selftest_slot(datetime(2026, 7, 25, 10, 0), 9, None) is not None
    assert selftest_slot(datetime(2026, 7, 25, 10, 0), -5, -60) is not None


def _selftest_engine(monkeypatch, now, **cfg_kwargs):
    """Engine with one host, a patched local clock and a counting _run_selftest."""
    from app import engine as engine_mod

    cfg = AppConfig(
        hosts=[PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006")], **cfg_kwargs
    )
    clock = {"now": now}
    monkeypatch.setattr(engine_mod, "_local_now", lambda: clock["now"])
    eng = Engine(cfg)
    runs: list = []

    async def fake_run():
        runs.append(clock["now"])

    eng._run_selftest = fake_run  # type: ignore[method-assign]
    return eng, clock, runs


@pytest.mark.asyncio
async def test_selftest_runs_once_per_slot(monkeypatch):
    eng, clock, runs = _selftest_engine(
        monkeypatch, datetime(2026, 7, 25, 10, 0), selftest_hour=9, selftest_interval_min=15
    )

    await eng._maybe_selftest()
    await eng._maybe_selftest()  # same slot -> no second run
    assert len(runs) == 1

    clock["now"] = datetime(2026, 7, 25, 10, 14)
    await eng._maybe_selftest()
    assert len(runs) == 1

    clock["now"] = datetime(2026, 7, 25, 10, 15)
    await eng._maybe_selftest()
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_selftest_daily_default_runs_once_a_day(monkeypatch):
    eng, clock, runs = _selftest_engine(
        monkeypatch, datetime(2026, 7, 25, 9, 30), selftest_hour=9, selftest_interval_min=1440
    )

    await eng._maybe_selftest()
    clock["now"] = datetime(2026, 7, 25, 20, 0)
    await eng._maybe_selftest()
    assert len(runs) == 1

    clock["now"] = datetime(2026, 7, 26, 9, 30)
    await eng._maybe_selftest()
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_selftest_is_skipped_while_on_battery(monkeypatch):
    eng, clock, runs = _selftest_engine(
        monkeypatch,
        datetime(2026, 7, 25, 10, 0),
        selftest_hour=9,
        selftest_interval_min=15,
        ups=[SnmpConfig(id="u1", host="10.0.0.5")],
    )
    eng.ups_rt["u1"].on_battery_since = datetime(2026, 7, 25, 9, 55, tzinfo=timezone.utc)

    await eng._maybe_selftest()
    assert runs == []
    # No latch was taken, so the test runs as soon as mains are back.
    assert eng.last_selftest_slot is None
    eng.ups_rt["u1"].on_battery_since = None
    await eng._maybe_selftest()
    assert len(runs) == 1


@pytest.mark.asyncio
async def test_selftest_never_runs_when_disabled_or_without_hosts(monkeypatch):
    from app import engine as engine_mod

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))

    configs = [
        AppConfig(
            hosts=[PveHostConfig(name="pve01", api_url="https://x:8006")], selftest_enabled=False
        ),
        AppConfig(selftest_enabled=True),  # no hosts
    ]
    for cfg in configs:
        eng = Engine(cfg)
        runs: list = []

        async def fake_run():
            runs.append(1)

        eng._run_selftest = fake_run  # type: ignore[method-assign]
        await eng._maybe_selftest()
        assert runs == []


@pytest.mark.asyncio
async def test_selftest_slot_survives_a_restart(monkeypatch):
    """Without persistence a 15-minute cadence would re-test on every service restart."""
    import json as _json

    from app import engine as engine_mod

    eng, clock, runs = _selftest_engine(
        monkeypatch, datetime(2026, 7, 25, 10, 0), selftest_hour=9, selftest_interval_min=15
    )
    await eng._maybe_selftest()
    assert len(runs) == 1

    written = _json.loads(engine_mod.STATE_PATH.read_text(encoding="utf-8"))
    assert written["selftest_slot"] == "2026-07-25T10:00:00"

    # Fresh engine, same wall clock -> latch restored, no repeat run.
    restarted = Engine(eng.cfg)
    restarted_runs: list = []

    async def fake_run():
        restarted_runs.append(1)

    restarted._run_selftest = fake_run  # type: ignore[method-assign]
    assert restarted.last_selftest_slot == datetime(2026, 7, 25, 10, 0)
    await restarted._maybe_selftest()
    assert restarted_runs == []


@pytest.mark.asyncio
async def test_selftest_outcome_survives_a_restart(monkeypatch):
    """Since a restart no longer re-runs the test, /api/status must keep the last result."""
    from app import engine as engine_mod
    from app.proxmox import TestResult

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))
    cfg = AppConfig(hosts=[PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006")])
    eng = Engine(cfg)

    async def fake_test(host, *a, **k):
        return TestResult(True, "ok", has_power_mgmt=True)

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    monkeypatch.setattr(eng, "_log_quiet", lambda *a: None)
    await eng._maybe_selftest()
    eng._persist_state()  # the loop persists again on its next _evaluate

    restarted = Engine(cfg)
    assert restarted.last_selftest_ok is True
    assert restarted.last_selftest_at == eng.last_selftest_at
    assert restarted.snapshot()["appliance"]["last_selftest_at"] is not None


def test_state_file_from_an_older_version_still_loads():
    """A state file written before the self-test slot existed must not break startup."""
    import json as _json

    from app import engine as engine_mod

    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    engine_mod.STATE_PATH.write_text(
        _json.dumps({"on_battery_since": {"u1": since}}), encoding="utf-8"
    )
    eng = Engine(AppConfig(ups=[SnmpConfig(id="u1", host="10.0.0.5")]))

    assert eng.last_selftest_slot is None
    assert eng.ups_rt["u1"].on_battery_since is not None


def test_selftest_slot_in_the_future_is_discarded(monkeypatch):
    """A backwards clock jump must not block self-tests until the clock catches up."""
    import json as _json

    from app import engine as engine_mod

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))
    engine_mod.STATE_PATH.write_text(
        _json.dumps({"selftest_slot": "2026-07-25T13:00:00"}), encoding="utf-8"
    )
    assert Engine(AppConfig()).last_selftest_slot is None


@pytest.mark.asyncio
async def test_run_selftest_queries_hosts_concurrently_and_damps_success_events(monkeypatch):
    """Sequential checks would stall the loop; one 'ok' event per run would flood the log."""
    import asyncio

    from app import engine as engine_mod
    from app.proxmox import TestResult

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))
    cfg = AppConfig(
        hosts=[
            PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006"),
            PveHostConfig(name="pve02", api_url="https://10.0.0.11:8006"),
        ]
    )
    eng = Engine(cfg)

    in_flight = {"now": 0, "max": 0}

    async def fake_test(host, *a, **k):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01)
        in_flight["now"] -= 1
        return TestResult(True, "ok", has_power_mgmt=True)

    logged: list = []
    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    monkeypatch.setattr(eng, "_log_quiet", lambda s, b, sev: logged.append(s))

    await eng._run_selftest()
    assert in_flight["max"] == 2  # both hosts were in flight at the same time
    assert len(logged) == 2  # first run of the day: one quiet event per host
    assert eng.last_selftest_ok is True

    await eng._run_selftest()
    assert len(logged) == 2  # same day, still ok -> no further event-log noise


@pytest.mark.asyncio
async def test_one_raising_host_does_not_take_the_self_test_round_with_it(monkeypatch):
    """targets.test_connection() is total in practice; the round makes that structural.

    Without return_exceptions one host that manages to raise aborts the whole gather: every
    other host keeps a stale verdict, no failure event is written for any of them, and the
    exception travels up into _loop()'s catch-all, which costs the iteration its
    housekeeping as well. _fire_stage() already guards the shutdown stages this way."""
    from app import db, engine as engine_mod
    from app.proxmox import TestResult

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))
    eng = Engine(AppConfig(hosts=[
        PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006"),
        PveHostConfig(name="pve02", api_url="https://10.0.0.11:8006"),
    ]))

    async def fake_test(host, *a, **k):
        if host.name == "pve01":
            raise RuntimeError("boom")
        return TestResult(True, "ok", has_power_mgmt=True)

    emitted: list = []

    async def fake_emit(subject, body, severity):
        emitted.append((subject, severity, body))

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    monkeypatch.setattr(eng, "_emit", fake_emit)
    monkeypatch.setattr(eng, "_log_quiet", lambda *a: None)

    await eng._run_selftest()

    # The healthy host keeps its verdict...
    key2 = eng.cfg.hosts[1].key
    assert eng.host_states[key2]["credentials_ok"] is True
    # ...and the raising one becomes a reported failure, not a silent gap.
    key1 = eng.cfg.hosts[0].key
    assert eng.host_states[key1]["credentials_ok"] is False
    assert "boom" in eng.host_states[key1]["last_test_error"]
    assert ("Self-test pve01: FAILED", db.CRITICAL) in [(s, sev) for s, sev, _ in emitted]
    assert eng.last_selftest_ok is False


@pytest.mark.asyncio
async def test_run_selftest_always_reports_failures(monkeypatch):
    from app import engine as engine_mod
    from app.proxmox import TestResult

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))
    eng = Engine(AppConfig(hosts=[PveHostConfig(
        name="pve01", api_url="https://10.0.0.10:8006",
        token_id="ups@pve!s", token_secret="sec")]))

    async def fake_test(host, *a, **k):
        return TestResult(False, "Authentication failed (token invalid?)")

    emitted: list = []

    async def fake_emit(subject, body, severity):
        emitted.append((subject, severity))

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    monkeypatch.setattr(eng, "_emit", fake_emit)

    await eng._run_selftest()
    await eng._run_selftest()  # failures are never damped

    assert len(emitted) == 2
    assert emitted[0][0] == "Self-test pve01: FAILED"
    assert eng.last_selftest_ok is False


def test_config_roundtrip_selftest_interval(tmp_path):
    path = tmp_path / "c.yaml"
    save_config(AppConfig(selftest_hour=3, selftest_interval_min=360), path)
    assert load_config(path).selftest_interval_min == 360


def test_selftest_fields_are_import_forgiving():
    """A backup from another version must import, not 400."""
    assert AppConfig.model_validate({"selftest_interval_min": 45}).selftest_interval_min == 1440
    assert AppConfig.model_validate({"selftest_interval_min": None}).selftest_interval_min == 1440
    assert AppConfig.model_validate({"selftest_interval_min": "360"}).selftest_interval_min == 360
    assert AppConfig.model_validate({"selftest_hour": 99}).selftest_hour == 23
    assert AppConfig.model_validate({"selftest_hour": -1}).selftest_hour == 0
    assert AppConfig.model_validate({"selftest_hour": "abc"}).selftest_hour == 9
    assert AppConfig().selftest_interval_min == 1440  # default = previous behaviour


# --- config export / import round-trip -------------------------------------
def _full_config() -> AppConfig:
    """A config with every notable field populated, for round-trip coverage."""
    from app.config import Notifications, WebhookConfig, WebhookFormat, WebhookLevel

    return AppConfig(
        configured=True,
        dry_run=False,
        ups=[
            SnmpConfig(id="a", name="UPS A", host="10.0.0.1", community="comm-a",
                       mib=SnmpMib.apc),
            SnmpConfig(
                id="b", name="UPS B", host="10.0.0.2", port=1161, version=SnmpVersion.v3,
                v3_user="mon", v3_auth_pass="auth-b", v3_priv_pass="priv-b",
                overrides=UpsThresholdOverride(runtime_below_minutes=2, on_battery_low=True),
            ),
        ],
        hosts=[
            PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006",
                       token_id="ups@pve!sd", token_secret="tok-1", ups_ids=["a"]),
            PbsHostConfig(name="Backup-Server", api_url="https://10.0.0.20:8007",
                       token_id="ups@pbs!sd", token_secret="tok-pbs", ups_ids=["b"],
                       order=1),
            PveHostConfig(name="pve02", api_url="https://10.0.0.11:8006",
                       token_id="ups@pve!sd", token_secret="tok-2",
                       ups_ids=["a", "b"], ups_policy="any", this_host=True),
        ],
        thresholds=Thresholds(runtime_below_minutes=7, comm_loss_shutdown_after_min=15),
        notifications=Notifications(webhook=WebhookConfig(
            enabled=True, url="https://hook/x",
            format=WebhookFormat.teams, min_severity=WebhookLevel.critical,
        )),
        selftest_enabled=True,
        selftest_hour=3,
        selftest_interval_min=360,
        ntp_server="pool.ntp.org",
        timezone="Europe/Berlin",
    )


def test_export_reveals_secrets_but_drops_auth_material():
    import json

    from app import main

    data = main._exportable_config(_full_config())

    assert data["ups"][0]["community"] == "comm-a"
    assert data["ups"][1]["v3_auth_pass"] == "auth-b"
    assert data["hosts"][0]["token_secret"] == "tok-1"
    assert main.SECRET_PLACEHOLDER not in json.dumps(data)  # a backup must be restorable
    assert "session_secret" not in data and "ui_password_hash" not in data


def test_sanitized_config_masks_secrets_for_the_ui():
    import json

    from app import main

    data = main._sanitized_config(_full_config())

    assert data["ups"][0]["community"] == main.SECRET_PLACEHOLDER
    assert "comm-a" not in json.dumps(data)
    assert "tok-1" not in json.dumps(data)
    assert "session_secret" not in data and "ui_password_hash" not in data


@pytest.fixture
def _import_target(monkeypatch, tmp_path):
    """A running engine whose imports are captured instead of touching the system."""
    from app import main

    saved: list = []
    running = AppConfig(ui_password_hash="hash-of-the-running-instance",
                        session_secret="session-of-the-running-instance")
    monkeypatch.setattr(main, "engine", Engine(running))
    monkeypatch.setattr(main, "save_config", lambda cfg, *a, **k: saved.append(cfg))
    monkeypatch.setattr(main, "IS_DOCKER", True)  # no privileged agent to enqueue jobs to
    monkeypatch.setattr(main.db, "log_event", lambda *a, **k: None)
    return main, saved


@pytest.mark.asyncio
async def test_export_import_round_trip_preserves_every_field(_import_target):
    main, saved = _import_target
    original = _full_config()

    await main.api_config_import(main._exportable_config(original))
    restored = main.engine.cfg

    assert [u.id for u in restored.ups] == ["a", "b"]
    assert restored.ups[0].community.get_secret_value() == "comm-a"
    assert restored.ups[1].v3_auth_pass.get_secret_value() == "auth-b"
    assert restored.ups[1].v3_priv_pass.get_secret_value() == "priv-b"
    assert restored.ups[1].port == 1161
    assert restored.ups[1].overrides.runtime_below_minutes == 2
    assert restored.ups[1].overrides.on_battery_low is True
    assert [h.name for h in restored.hosts] == ["pve01", "Backup-Server", "pve02"]
    assert [h.type for h in restored.hosts] == ["pve", "pbs", "pve"]
    assert isinstance(restored.hosts[1], PbsHostConfig)
    assert restored.hosts[1].token_secret.get_secret_value() == "tok-pbs"
    assert restored.hosts[2].token_secret.get_secret_value() == "tok-2"
    assert restored.hosts[2].ups_policy == "any"
    assert restored.hosts[2].this_host is True
    assert restored.thresholds.runtime_below_minutes == 7
    assert restored.thresholds.comm_loss_shutdown_after_min == 15
    assert restored.notifications.webhooks[0].url == "https://hook/x"
    assert restored.notifications.webhooks[0].format.value == "teams"
    assert restored.notifications.webhooks[0].min_severity.value == "critical"
    assert restored.dry_run is False
    assert restored.ntp_server == "pool.ntp.org"
    assert restored.timezone == "Europe/Berlin"
    assert restored.selftest_hour == 3
    assert restored.selftest_interval_min == 360

    # The running instance keeps its own login and session signing key.
    assert restored.ui_password_hash == "hash-of-the-running-instance"
    assert restored.session_secret == "session-of-the-running-instance"
    assert saved and saved[0].configured is True


@pytest.mark.asyncio
async def test_import_of_an_older_backup_survives_unknown_values(_import_target):
    """A backup written by another version must import, not fail with 400."""
    main, _ = _import_target

    result = await main.api_config_import(
        {"selftest_interval_min": 45, "selftest_hour": 42, "unknown_future_key": True}
    )

    assert result["selftest_interval_min"] == 1440  # snapped to the daily default
    assert result["selftest_hour"] == 23


@pytest.mark.asyncio
async def test_import_of_a_pre_2x_backup_migrates_the_single_ups(_import_target):
    main, _ = _import_target

    await main.api_config_import({
        "snmp": {"host": "10.0.0.9", "community": "sec", "version": "v2c"},
        "hosts": [{"name": "pve01", "api_url": "https://x:8006"}],
    })
    restored = main.engine.cfg

    assert [u.id for u in restored.ups] == ["ups1"]
    assert restored.ups[0].community.get_secret_value() == "sec"
    assert restored.hosts[0].ups_ids == ["ups1"]


@pytest.mark.asyncio
async def test_import_rejects_a_broken_payload(_import_target):
    from fastapi import HTTPException

    main, saved = _import_target

    with pytest.raises(HTTPException) as excinfo:
        await main.api_config_import({"ups": "not-a-list"})

    assert excinfo.value.status_code == 400
    assert saved == []  # nothing was written


def test_buildconfig_sends_every_system_field():
    """Guard against the silent-reset trap: the server rebuilds the config from this
    payload alone, so a field the UI omits falls back to its default on every save."""
    import re
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[1] / "app" / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    body = re.search(r"function buildConfig\(\)\s*\{(.*?)\n\}", app_js, re.DOTALL)
    assert body, "buildConfig() not found in app.js"

    for field in ("selftest_enabled", "selftest_hour", "selftest_interval_min",
                  "ntp_server", "timezone", "dry_run"):
        assert field in body.group(1), f"buildConfig() does not send {field}"


def test_hostfromrow_sends_every_host_field():
    """Same trap one level down: hostFromRow() builds each host entry, so a field it
    omits silently reverts on every save — the target type above all."""
    import re
    from pathlib import Path

    app_js = (Path(__file__).resolve().parents[1] / "app" / "web" / "app.js").read_text(
        encoding="utf-8"
    )
    body = re.search(r"function hostFromRow\(tr\)\s*\{(.*?)\n\}", app_js, re.DOTALL)
    assert body, "hostFromRow() not found in app.js"

    for field in PveHostConfig.model_fields:
        assert field in body.group(1), f"hostFromRow() does not send {field}"


# --- SNMPv3 privacy ciphers -------------------------------------------------
def test_snmpv3_privacy_ciphers_are_available():
    """pysnmp 7.x ships no ciphers of its own — it needs the `cryptography` package.

    Without it every authPriv poll fails with "Ciphering services not available" while
    authNoPriv keeps working, which is exactly what a user reported. The flags below are
    what pysnmp sets when its import of `cryptography` failed.
    """
    # Import through the public entry point first: pulling a priv module in on its own
    # trips a circular import inside pysnmp.
    import pysnmp.hlapi.asyncio  # noqa: F401
    from pysnmp.proto.secmod.rfc3414.priv import des
    from pysnmp.proto.secmod.rfc3826.priv import aes

    assert des.PysnmpCryptoError is False, "SNMPv3 DES privacy unavailable"
    # AES-192/256 inherit encrypt_data from this module, so one check covers all three.
    assert aes.PysnmpCryptoError is False, "SNMPv3 AES privacy unavailable"


def test_cryptography_still_exposes_the_apis_pysnmp_uses():
    """Guard the two symbols pysnmp reaches for — the import flags above would not notice.

    A missing package trips PysnmpCryptoError, but an API that merely *moved* still
    imports and only blows up while encrypting, i.e. on the user's box. cryptography 49
    already deprecates primitives.ciphers.modes.CFB (used by pysnmp's AES) and announces
    its removal; this test fails the day it happens instead of the privacy silently
    breaking again. See the version cap in pyproject.toml.
    """
    from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
    from cryptography.hazmat.primitives.ciphers import modes

    assert hasattr(modes, "CFB"), "pysnmp AES needs primitives.ciphers.modes.CFB"
    assert hasattr(
        decrepit_algorithms, "TripleDES"
    ), "pysnmp DES needs decrepit.ciphers.algorithms.TripleDES"


def test_cryptography_is_a_declared_dependency():
    """Guard the declaration itself: the venv may have it, a fresh install must too."""
    from pathlib import Path

    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    # Cut at the closing bracket on its own line — extras like uvicorn[standard] contain
    # brackets, so splitting on a bare "]" would truncate the list.
    block = pyproject.split("dependencies = [", 1)[1].split("\n]", 1)[0]
    assert "cryptography" in block, "cryptography missing from [project].dependencies"


def test_privacy_unavailable_only_applies_to_v3_with_privacy(monkeypatch):
    import builtins

    from app import ups

    real_import = builtins.__import__

    def no_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("simulated: cryptography not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_crypto)

    from app.config import SnmpAuthProto, SnmpPrivProto

    def cfg(**kw):
        base = dict(host="10.0.0.5", version=SnmpVersion.v3, v3_user="mon",
                    v3_auth_proto=SnmpAuthProto.md5, v3_auth_pass="a")
        base.update(kw)
        return SnmpConfig(**base)

    # v1/v2c never encrypt.
    assert ups._privacy_unavailable(SnmpConfig(host="10.0.0.5", version=SnmpVersion.v2c)) is False
    # v3 authNoPriv works without ciphers — the workaround we point users at.
    assert ups._privacy_unavailable(cfg(v3_priv_proto=SnmpPrivProto.none)) is False
    # v3 authPriv cannot work.
    for proto in (SnmpPrivProto.des, SnmpPrivProto.aes, SnmpPrivProto.aes256):
        assert ups._privacy_unavailable(cfg(v3_priv_proto=proto)) is True, proto


def test_privacy_unavailable_is_false_when_ciphers_are_installed():
    from app import ups
    from app.config import SnmpAuthProto, SnmpPrivProto

    cfg = SnmpConfig(host="10.0.0.5", version=SnmpVersion.v3, v3_user="mon",
                     v3_auth_proto=SnmpAuthProto.md5, v3_auth_pass="a",
                     v3_priv_proto=SnmpPrivProto.des, v3_priv_pass="p")
    assert ups._privacy_unavailable(cfg) is False


@pytest.mark.asyncio
async def test_poll_reports_missing_ciphers_instead_of_unreachable(monkeypatch):
    """The failure is local — nothing is sent — so it must not read like a network fault."""
    from app import ups
    from app.config import SnmpAuthProto, SnmpPrivProto

    monkeypatch.setattr(ups, "_privacy_unavailable", lambda cfg: True)
    sent: list = []
    monkeypatch.setattr(ups, "_auth_data", lambda cfg: sent.append(cfg))

    cfg = SnmpConfig(host="10.0.0.5", version=SnmpVersion.v3, v3_user="mon",
                     v3_auth_proto=SnmpAuthProto.md5, v3_auth_pass="a",
                     v3_priv_proto=SnmpPrivProto.des, v3_priv_pass="p")
    state = await ups.poll(cfg)

    assert state.reachable is False  # fail-safe unchanged: alarm, never a shutdown
    assert state.error == ups.PRIVACY_MISSING
    assert "cryptography" in state.error and "authNoPriv" in state.error
    assert sent == []  # bailed out before building the request


@pytest.mark.asyncio
async def test_probe_reports_missing_ciphers_without_firewall_advice(monkeypatch):
    from app import ups
    from app.config import SnmpAuthProto, SnmpPrivProto

    monkeypatch.setattr(ups, "_privacy_unavailable", lambda cfg: True)

    cfg = SnmpConfig(host="10.0.0.5", version=SnmpVersion.v3, v3_user="mon",
                     v3_auth_proto=SnmpAuthProto.md5, v3_auth_pass="a",
                     v3_priv_proto=SnmpPrivProto.des, v3_priv_pass="p")
    result = await ups.probe(cfg)

    assert result.reachable is False
    assert result.summary == ups.PRIVACY_MISSING
    assert "firewall" not in result.summary.lower()
    assert [e.status for e in result.entries] == ["skipped"] * result.total


# --- webhook notifications: formats, severity filter, sending ---------------
def _hook(**kw):
    from app.config import WebhookConfig

    return WebhookConfig(**{"enabled": True, "url": "https://hook/x", **kw})


_SNAPSHOT = {
    "appliance": {"version": "9.9.9", "dry_run": False},
    "ups": [{"id": "a", "name": "UPS A", "reachable": True, "power_source": "battery",
             "battery_charge_pct": 84, "runtime_remaining_min": 12},
            {"id": "b", "name": "UPS B", "reachable": False}],
    "hosts": [{"name": "pve01", "eligible": True}, {"name": "pve02", "shutdown_state": "sent"}],
}


def test_json_payload_stays_backwards_compatible():
    from app import notify

    kwargs = notify._render_json("[PVE-UPS] Subject", "Body.", "warning", _SNAPSHOT)

    # subject/body/status are the 3.0-3.3 contract; severity is additive.
    assert kwargs["json"] == {"subject": "[PVE-UPS] Subject", "body": "Body.",
                              "severity": "warning", "status": _SNAPSHOT}
    assert "headers" not in kwargs  # httpx sets application/json itself


def test_teams_payload_is_an_adaptive_card_in_the_message_envelope():
    from app import notify

    kwargs = notify._render_teams("[PVE-UPS] Power outage", "Runtime 12 min.", "critical",
                                  _SNAPSHOT)
    msg = kwargs["json"]
    attachment = msg["attachments"][0]
    card = attachment["content"]

    assert msg["type"] == "message"
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["body"][0]["text"] == "[PVE-UPS] Power outage"
    assert card["body"][0]["color"] == "Attention"  # critical -> red
    assert card["body"][1]["text"] == "Runtime 12 min."
    facts = {f["title"]: f["value"] for f in card["body"][2]["facts"]}
    assert "UPS A: battery, 84 %, 12 min" in facts["UPS"]
    assert "UPS B: unreachable" in facts["UPS"]
    assert facts["Mode"] == "ARMED"
    assert facts["Appliance"] == "PVE-UPS 9.9.9"


def test_teams_card_colour_follows_the_severity():
    from app import notify

    def colour(severity):
        card = notify._render_teams("s", "b", severity, _SNAPSHOT)["json"]["attachments"][0]
        return card["content"]["body"][0]["color"]

    assert [colour(s) for s in ("info", "warning", "critical")] == ["Good", "Warning", "Attention"]


def test_text_payload_is_plain_text_with_the_severity_up_front():
    from app import notify

    kwargs = notify._render_text("[PVE-UPS] Power outage", "Runtime 12 min.", "warning",
                                 _SNAPSHOT)
    lines = kwargs["content"].decode("utf-8").splitlines()

    assert kwargs["headers"]["Content-Type"] == "text/plain; charset=utf-8"
    assert lines[0] == "[WARNING] [PVE-UPS] Power outage"
    assert lines[1] == "Runtime 12 min."
    assert any(line.startswith("UPS: UPS A: battery") for line in lines)
    assert any(line == "Mode: ARMED" for line in lines)


def test_formatters_survive_a_malformed_snapshot():
    # The engine may hand over anything; a notification must degrade, never raise.
    from app import notify

    for payload in ({}, {"ups": "nonsense"}, {"hosts": [None]}, {"appliance": None}):
        for render in notify.FORMATTERS.values():
            render("subject", "body", "warning", payload)


@pytest.mark.asyncio
async def test_severity_filter_sends_from_the_configured_level_upwards(monkeypatch):
    from app import notify
    from app.config import Notifications

    sent: list = []

    async def record(hook, subject, body, severity, payload):
        sent.append(severity)
        return "HTTP 200"

    monkeypatch.setattr(notify, "send_webhook", record)

    async def levels(min_severity):
        sent.clear()
        cfg = Notifications(webhook=_hook(min_severity=min_severity))
        for severity in ("info", "warning", "critical"):
            await notify.notify(cfg, "s", "b", {}, severity)
        return list(sent)

    assert await levels("warning") == ["warning", "critical"]  # the default
    assert await levels("info") == ["info", "warning", "critical"]
    assert await levels("critical") == ["critical"]


@pytest.mark.asyncio
async def test_notify_stays_silent_when_disabled_or_without_a_url(monkeypatch):
    from app import notify
    from app.config import Notifications

    sent: list = []

    async def record(*a, **k):
        sent.append(a)
        return "HTTP 200"

    monkeypatch.setattr(notify, "send_webhook", record)

    await notify.notify(Notifications(webhook=_hook(enabled=False)), "s", "b", {}, "critical")
    await notify.notify(Notifications(webhook=_hook(url="")), "s", "b", {}, "critical")

    assert sent == []


@pytest.mark.asyncio
async def test_notify_accepts_plain_strings_for_the_enum_settings(monkeypatch):
    # Plain attribute assignment skips pydantic validation, so the fields can hold bare
    # strings — and an AttributeError here would surface on the engine's poll loop.
    from app import notify
    from app.config import Notifications

    calls = _fake_httpx(monkeypatch)  # defined below; resolved at call time
    cfg = Notifications(webhook=_hook())
    cfg.webhooks[0].min_severity = "info"
    cfg.webhooks[0].format = "text"

    await notify.notify(cfg, "s", "b", {}, "info")

    assert calls, "the filter rejected a bare-string level"
    assert calls[0][1]["headers"]["Content-Type"] == "text/plain; charset=utf-8"


@pytest.mark.asyncio
async def test_notify_swallows_a_failing_send(monkeypatch):
    # The shutdown logic must never be affected by an unreachable notification target.
    from app import notify
    from app.config import Notifications

    async def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(notify, "send_webhook", boom)
    await notify.notify(Notifications(webhook=_hook()), "s", "b", {}, "critical")


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_httpx(monkeypatch, status=200):
    """Capture the POST instead of performing it; returns the recorded calls."""
    from app import notify

    calls: list = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return _FakeResponse(status)

    monkeypatch.setattr(notify.httpx, "AsyncClient", _FakeClient)
    return calls


@pytest.mark.asyncio
async def test_send_webhook_posts_in_the_configured_format(monkeypatch):
    from app import notify

    calls = _fake_httpx(monkeypatch)

    result = await notify.send_webhook(_hook(format="text"), "s", "b", "warning", _SNAPSHOT)

    url, kwargs = calls[0]
    assert url == "https://hook/x"
    assert kwargs["headers"]["Content-Type"] == "text/plain; charset=utf-8"
    assert result == "HTTP 200"


@pytest.mark.asyncio
async def test_slack_and_discord_carry_subject_body_and_severity(monkeypatch):
    from app import notify

    calls = _fake_httpx(monkeypatch)
    await notify.send_webhook(_hook(format="slack"), "Subj", "Body.", "critical", _SNAPSHOT)
    payload = calls[0][1]["json"]
    # Slack needs the fallback text for push notifications, and the colour carries severity.
    assert "Subj" in payload["text"]
    assert payload["attachments"][0]["color"] == notify._SLACK_COLOR["critical"]
    assert payload["attachments"][0]["text"] == "Body."

    calls.clear()
    await notify.send_webhook(_hook(format="discord"), "Subj", "Body.", "warning", _SNAPSHOT)
    embed = calls[0][1]["json"]["embeds"][0]
    assert embed["title"] == "Subj" and embed["description"] == "Body."
    assert embed["color"] == notify._DISCORD_COLOR["warning"]
    assert len(embed["fields"]) <= 25  # Discord rejects more


@pytest.mark.asyncio
async def test_ntfy_puts_the_metadata_in_headers(monkeypatch):
    from app import notify

    calls = _fake_httpx(monkeypatch)
    await notify.send_webhook(_hook(format="ntfy"), "Subj", "Body.", "critical", _SNAPSHOT)
    headers = calls[0][1]["headers"]
    assert headers["Title"] == "Subj"
    assert headers["Priority"] == "urgent"
    assert headers["Tags"]
    # Header values must be latin-1 encodable or httpx refuses to send them.
    for value in headers.values():
        value.encode("latin-1")
    # The subject is the Title, so it must not be repeated in the body.
    assert b"Body." in calls[0][1]["content"]


@pytest.mark.asyncio
async def test_ntfy_title_survives_a_non_ascii_subject(monkeypatch):
    from app import notify

    calls = _fake_httpx(monkeypatch)
    await notify.send_webhook(_hook(format="ntfy"), "USV Störung", "b", "warning", _SNAPSHOT)
    calls[0][1]["headers"]["Title"].encode("latin-1")  # must not raise


@pytest.mark.asyncio
async def test_custom_template_substitutes_and_escapes_for_json(monkeypatch):
    from app import notify

    calls = _fake_httpx(monkeypatch)
    hook = _hook(
        format="custom",
        template='{"msg": "{{subject}}", "lvl": "{{severity_upper}}", "all": {{facts_json}}}',
        content_type="application/json",
    )
    # A quote in the subject must not be able to break the JSON body.
    await notify.send_webhook(hook, 'UPS "A" lost', "b", "warning", _SNAPSHOT)
    import json

    body = json.loads(calls[0][1]["content"].decode("utf-8"))  # must parse
    assert body["msg"] == 'UPS "A" lost'
    assert body["lvl"] == "WARNING"
    assert isinstance(body["all"], dict)
    assert calls[0][1]["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_custom_template_leaves_plain_text_unescaped(monkeypatch):
    from app import notify

    calls = _fake_httpx(monkeypatch)
    hook = _hook(format="custom", template="{{subject}}", content_type="text/plain")
    await notify.send_webhook(hook, 'a "quoted" subject', "b", "warning", _SNAPSHOT)
    assert calls[0][1]["content"].decode("utf-8") == 'a "quoted" subject'


@pytest.mark.asyncio
async def test_custom_without_a_template_falls_back_instead_of_sending_nothing(monkeypatch):
    from app import notify

    calls = _fake_httpx(monkeypatch)
    await notify.send_webhook(_hook(format="custom"), "Subj", "b", "warning", _SNAPSHOT)
    assert b"Subj" in calls[0][1]["content"]


@pytest.mark.asyncio
async def test_auth_header_is_attached_to_any_format(monkeypatch):
    from app import notify

    calls = _fake_httpx(monkeypatch)
    hook = _hook(format="ntfy", auth_header_name="Authorization",
                 auth_header_value="Bearer tk_secret")
    await notify.send_webhook(hook, "s", "b", "warning", _SNAPSHOT)
    assert calls[0][1]["headers"]["Authorization"] == "Bearer tk_secret"

    # A name without a value must not produce an empty header.
    calls.clear()
    await notify.send_webhook(_hook(format="ntfy", auth_header_name="X-Api-Key"),
                              "s", "b", "warning", _SNAPSHOT)
    assert "X-Api-Key" not in calls[0][1]["headers"]


@pytest.mark.asyncio
async def test_extra_headers_ride_along_with_every_format(monkeypatch):
    """#34: a target may need more headers than the one authenticating one."""
    from app import notify

    calls = _fake_httpx(monkeypatch)
    # Nextcloud Talk, the case that asked for this: Basic auth plus two plain headers.
    hook = _hook(format="custom", template='{"message":"x"}',
                 auth_header_name="Authorization", auth_header_value="Basic dXNlcjpwdw==",
                 extra_headers={"OCS-APIRequest": "true", "Accept": "application/json"})
    await notify.send_webhook(hook, "s", "b", "warning", _SNAPSHOT)
    headers = calls[0][1]["headers"]
    assert headers["OCS-APIRequest"] == "true"
    assert headers["Accept"] == "application/json"
    assert headers["Authorization"] == "Basic dXNlcjpwdw=="

    # A json-payload format has no headers of its own; the extras must still arrive.
    calls.clear()
    await notify.send_webhook(_hook(format="json", extra_headers={"X-Token": "abc"}),
                              "s", "b", "warning", _SNAPSHOT)
    assert calls[0][1]["headers"] == {"X-Token": "abc"}


@pytest.mark.asyncio
async def test_extra_headers_beat_the_format_but_never_the_auth_header(monkeypatch):
    """The precedence that makes the plain-text field safe next to the masked one.

    An extra header may override what the formatter chose (a target wanting a different
    Content-Type has no other way to say so), but must not be able to shadow the auth
    header — that is the only one of the three carrying a secret.
    """
    from app import notify

    calls = _fake_httpx(monkeypatch)
    hook = _hook(format="ntfy", auth_header_name="Authorization",
                 auth_header_value="Bearer tk_secret",
                 extra_headers={"Content-Type": "application/json",
                                "Authorization": "Bearer spoofed"})
    await notify.send_webhook(hook, "s", "b", "warning", _SNAPSHOT)
    headers = calls[0][1]["headers"]
    assert headers["Content-Type"] == "application/json"   # formatter overridden
    assert headers["Authorization"] == "Bearer tk_secret"  # secret still wins
    assert headers["Priority"]  # ntfy's own untouched headers are still there


def test_extra_headers_are_parsed_from_lines_and_never_reject_the_config():
    """The UI sends a textarea, config.yaml holds a mapping; both land in one clean dict.

    Follows the rule every validator in config.py follows: drop what is malformed, never
    raise — a header in the wrong shape is fixable from the UI, a refused import is not.
    """
    from app.config import WebhookConfig, parse_extra_headers

    parsed = parse_extra_headers(
        "OCS-APIRequest: true\n"
        "  Accept : application/json  \n"   # whitespace around both halves
        "\n"                                 # blank
        "# a comment\n"
        "no-colon-here\n"                    # not a header at all
        ": novalue\n"                        # empty name
        "X-Bad Name: x\n"                    # space is not allowed in a field name
        "Content-Length: 99\n"               # httpx owns this one
        "Host: elsewhere\n"
    )
    assert parsed == {"OCS-APIRequest": "true", "Accept": "application/json"}

    # A mapping (config.yaml, a backup import) goes through the same filter, and so does
    # anything else that may arrive — a list, a number, None — without raising.
    assert parse_extra_headers({"X-A": "1", "Bad Name": "2"}) == {"X-A": "1"}
    assert parse_extra_headers(None) == {} and parse_extra_headers(7) == {}

    # Header splitting, by both routes it could arrive. From the textarea a pasted CRLF is
    # simply a line break, so the smuggled header becomes an ordinary second header that is
    # validated on its own terms — nothing is smuggled INTO a value. Through the mapping
    # there is no line break to split on, so the CR/LF would have to survive inside the
    # value itself, and that is what the value pattern refuses.
    assert parse_extra_headers("X-Inject: a\r\nEvil: b") == {"X-Inject": "a", "Evil": "b"}
    assert parse_extra_headers({"X-Inject": "a\r\nEvil: b"}) == {}

    # Non-ASCII is refused rather than stored, even though RFC 7230 obs-text allows it:
    # httpx encodes header values as ASCII, so keeping one would save a webhook that
    # then fails every delivery with a codec error instead of at the point of entry.
    assert parse_extra_headers("X-Note: Café") == {}

    # And through the model, which is how both of the above actually get there.
    assert WebhookConfig(extra_headers="X-A: 1").extra_headers == {"X-A": "1"}
    assert WebhookConfig(extra_headers=["nonsense"]).extra_headers == {}


def test_extra_headers_are_capped():
    """Bounded like every other free-form field: a paste accident is not a config."""
    from app.config import MAX_EXTRA_HEADERS, parse_extra_headers

    many = "\n".join(f"X-H{i}: v" for i in range(MAX_EXTRA_HEADERS + 10))
    assert len(parse_extra_headers(many)) == MAX_EXTRA_HEADERS
    assert parse_extra_headers(f"X-Long: {'v' * 5000}") == {}


def test_a_disabled_webhook_without_a_url_survives_a_save():
    """#35: a card switched off is a draft, not something to refuse the whole save over.

    It used to be dropped on save, so the UI had to block the save to stop it vanishing in
    silence — and the way out was to invent a URL for a webhook that was switched off.
    """
    from app import main
    from app.config import Notifications, WebhookConfig

    existing = AppConfig(notifications=Notifications(webhooks=[
        WebhookConfig(id="w1", name="Talk", enabled=False, url="",
                      auth_header_name="Authorization", auth_header_value="Basic keep"),
    ]))
    incoming = {
        "ups": [], "hosts": [],
        "notifications": {"webhooks": [{
            "id": "w1", "name": "Talk", "enabled": False, "url": "",
            "auth_header_name": "Authorization",
            "auth_header_value": main.SECRET_PLACEHOLDER,
        }]},
    }
    merged = main._merge_config(incoming, existing)
    hook = merged.notifications.webhooks[0]
    assert hook.id == "w1" and hook.name == "Talk" and hook.url == ""
    # The secret the card never showed is still there, matched by the id as always.
    assert hook.auth_header_value.get_secret_value() == "Basic keep"


@pytest.mark.asyncio
async def test_a_webhook_without_a_url_is_never_posted_to(monkeypatch):
    """The other half of #35: keeping the draft must not make it send anywhere."""
    from app import notify
    from app.config import Notifications

    calls = _fake_httpx(monkeypatch)
    cfg = Notifications(webhooks=[
        _hook(id="draft", url="", enabled=True),   # enabled but nowhere to go
        _hook(id="real", url="https://real/x"),
    ])
    await notify.notify(cfg, "Subj", "Body", _SNAPSHOT, "warning")
    assert [url for url, _ in calls] == ["https://real/x"]


@pytest.mark.asyncio
async def test_every_enabled_webhook_gets_the_notification(monkeypatch):
    from app import notify
    from app.config import Notifications

    calls = _fake_httpx(monkeypatch)
    cfg = Notifications(webhooks=[
        _hook(id="a", url="https://a/x"),
        _hook(id="b", url="https://b/x", format="text"),
        _hook(id="c", url="https://c/x", enabled=False),          # disabled
        _hook(id="d", url="https://d/x", min_severity="critical"),  # filtered out
    ])
    await notify.notify(cfg, "s", "b", _SNAPSHOT, "warning")
    assert sorted(url for url, _ in calls) == ["https://a/x", "https://b/x"]


@pytest.mark.asyncio
async def test_one_failing_webhook_does_not_stop_the_others(monkeypatch):
    """A target that hangs or errors must not cost the other targets their notification —
    nor bring down the poll loop this runs on."""
    from app import notify
    from app.config import Notifications

    sent: list[str] = []

    async def flaky(hook, subject, body, severity, payload):
        if hook.url == "https://bad/x":
            raise RuntimeError("connection reset")
        sent.append(hook.url)
        return "HTTP 200"

    monkeypatch.setattr(notify, "send_webhook", flaky)
    cfg = Notifications(webhooks=[
        _hook(id="bad", url="https://bad/x"),
        _hook(id="good", url="https://good/x"),
    ])
    await notify.notify(cfg, "s", "b", _SNAPSHOT, "warning")
    assert sent == ["https://good/x"]


@pytest.mark.asyncio
async def test_send_webhook_reports_an_http_error(monkeypatch):
    from app import notify

    _fake_httpx(monkeypatch, status=404)

    with pytest.raises(RuntimeError, match="404"):
        await notify.send_webhook(_hook(), "s", "b", "warning", _SNAPSHOT)


@pytest.mark.asyncio
async def test_test_endpoint_sends_regardless_of_the_severity_filter(_import_target, monkeypatch):
    main, _ = _import_target
    sent: list = []

    async def record(hook, subject, body, severity, payload):
        sent.append((hook.format.value, subject, severity))
        return "HTTP 202"

    monkeypatch.setattr(main.notify, "send_webhook", record)

    # enabled=False and "critical only" would both mute the event path — not this one.
    result = await main.api_test_webhook(
        {"enabled": False, "url": "https://hook/x", "format": "teams", "min_severity": "critical"}
    )

    assert result == {"ok": True, "message": "HTTP 202"}
    assert sent == [("teams", "[PVE-UPS] Test notification", "info")]


@pytest.mark.asyncio
async def test_the_test_endpoint_uses_the_stored_secret_not_the_mask(
        _import_target, monkeypatch):
    """Pressing "Test" on a saved webhook used to send the literal placeholder.

    The UI sends "**********" whenever the auth field is left blank — the same convention
    as every other secret — but this was the one test endpoint that never reconciled it,
    so a working configuration answered 401.
    """
    from app.config import WebhookConfig, Notifications

    main, _ = _import_target
    main.engine.cfg.notifications = Notifications(webhooks=[WebhookConfig(
        id="webhook1", url="https://hook/x", auth_header_name="X-Token",
        auth_header_value="the-real-token")])
    seen: list = []

    async def record(hook, subject, body, severity, payload):
        seen.append(hook.auth_header_value.get_secret_value())
        return "HTTP 202"

    monkeypatch.setattr(main.notify, "send_webhook", record)

    await main.api_test_webhook({
        "id": "webhook1", "url": "https://hook/x",
        "auth_header_name": "X-Token", "auth_header_value": main.SECRET_PLACEHOLDER,
    })

    assert seen == ["the-real-token"]


@pytest.mark.asyncio
async def test_a_failing_webhook_is_written_to_the_event_log_once(monkeypatch):
    """It used to reach journald and nowhere else — no event, no health field, no UI."""
    from app import db, notify
    from app.config import Notifications, WebhookConfig

    logged: list[tuple] = []
    # Recorded rather than written: db.log_event() binds its path default at import, and
    # the point of the test is *that* it is called, once, and never through the engine's
    # _emit (which would notify about the failing webhook, for ever).
    monkeypatch.setattr(notify.db, "log_event",
                        lambda ev, detail="", sev=db.INFO: logged.append((ev, detail)))
    monkeypatch.setattr(notify, "DELIVERY", {})

    async def boom(hook, subject, body, severity, payload):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(notify, "send_webhook", boom)
    hooks = Notifications(webhooks=[WebhookConfig(id="w1", name="Teams", enabled=True,
                                                  url="https://hook/x")])

    for _ in range(3):        # an outage emits steadily; the log must not be flooded
        await notify.notify(hooks, "s", "b", {}, db.WARNING)

    assert len(logged) == 1
    assert "Teams" in logged[0][0] and "401 Unauthorized" in logged[0][1]
    assert notify.delivery_state()["w1"]["ok"] is False


@pytest.mark.asyncio
async def test_a_recovered_webhook_can_be_reported_again(monkeypatch):
    from app import db, notify
    from app.config import Notifications, WebhookConfig

    logged: list[tuple] = []
    monkeypatch.setattr(notify.db, "log_event",
                        lambda ev, detail="", sev=db.INFO: logged.append((ev, detail)))
    monkeypatch.setattr(notify, "DELIVERY", {})
    hooks = Notifications(webhooks=[WebhookConfig(id="w1", enabled=True,
                                                  url="https://hook/x")])
    outcome = {"fail": True}

    async def flaky(hook, subject, body, severity, payload):
        if outcome["fail"]:
            raise RuntimeError("boom")
        return "HTTP 200"

    monkeypatch.setattr(notify, "send_webhook", flaky)

    await notify.notify(hooks, "s", "b", {}, db.WARNING)
    outcome["fail"] = False
    await notify.notify(hooks, "s", "b", {}, db.WARNING)
    assert notify.delivery_state()["w1"]["ok"] is True
    outcome["fail"] = True
    await notify.notify(hooks, "s", "b", {}, db.WARNING)

    assert len(logged) == 2, "a fresh run of failures is worth saying again"


@pytest.mark.asyncio
async def test_a_failed_delivery_never_publishes_the_webhook_url(monkeypatch):
    """The URL IS the credential for Slack, Discord, Teams and ntfy — and /api/status,
    which carries both the delivery state and the event log, is deliberately public.

    httpx spells a status error as "... for url '<the whole thing>'", so storing the raw
    exception text published the secret to anyone who could reach the appliance. Worse,
    the snapshot travels on as the ``status`` field of every notification, so target A's
    URL would have been POSTed to target B."""
    from app import db, notify
    from app.config import Notifications, WebhookConfig
    import httpx

    secret_url = "https://hooks.slack.com/services/T00/B00/sUpErSeCrEt"
    logged: list[tuple] = []
    monkeypatch.setattr(notify.db, "log_event",
                        lambda ev, detail="", sev=db.INFO: logged.append((ev, detail)))
    monkeypatch.setattr(notify, "DELIVERY", {})

    async def unauthorized(hook, subject, body, severity, payload):
        request = httpx.Request("POST", secret_url)
        raise httpx.HTTPStatusError(
            "boom", request=request, response=httpx.Response(401, request=request)
        )

    monkeypatch.setattr(notify, "send_webhook", unauthorized)
    hooks = Notifications(webhooks=[WebhookConfig(id="w1", enabled=True, url=secret_url)])

    await notify.notify(hooks, "s", "b", {}, db.WARNING)

    state = notify.delivery_state()["w1"]
    assert state["ok"] is False
    # Still diagnostic — the status code is the whole answer here — but nothing secret.
    assert "401" in state["error"]
    assert "sUpErSeCrEt" not in state["error"] and "://" not in state["error"]
    # The event log is served through /api/status too, so it gets the same treatment.
    assert "sUpErSeCrEt" not in logged[0][1]


def test_the_error_sanitiser_keeps_a_url_out_of_every_shape_of_failure():
    """Pure, so the rule can be checked without a network: nothing that reaches the
    public snapshot may carry a URL, whatever the exception turned out to be."""
    import httpx
    from app.notify import safe_error

    url = "https://discord.com/api/webhooks/123/tOkEn"
    request = httpx.Request("POST", url)
    assert safe_error(httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(404, request=request))) == "HTTP 404 Not Found"
    # Transport failures degrade to the class name — httpx puts the host in the message.
    assert safe_error(httpx.ConnectError("failed to connect to " + url)) == "ConnectError"
    # Anything unforeseen keeps its text, minus any URL in it.
    masked = safe_error(RuntimeError(f"template blew up on {url} again"))
    assert "tOkEn" not in masked and "<url>" in masked and "template blew up" in masked


@pytest.mark.asyncio
async def test_one_silent_webhook_cannot_hold_up_the_shutdown(monkeypatch):
    """notify() is awaited from Engine._emit(), which is awaited from the poll loop —
    including between two shutdown stages. Per-target httpx timeouts do not bound the
    round, so "best effort" had to be made to mean "bounded" as well."""
    import asyncio as _asyncio
    from app import db, notify
    from app.config import Notifications, WebhookConfig

    monkeypatch.setattr(notify.db, "log_event", lambda *a, **kw: None)
    monkeypatch.setattr(notify, "DELIVERY", {})
    monkeypatch.setattr(notify, "NOTIFY_BUDGET_S", 0.05)

    async def never_answers(hook, subject, body, severity, payload):
        await _asyncio.sleep(30)
        return "HTTP 200"

    async def answers(hook, subject, body, severity, payload):
        return "HTTP 200"

    async def dispatch(hook, *a, **kw):
        return await (never_answers if hook.id == "dead" else answers)(hook, *a, **kw)

    monkeypatch.setattr(notify, "send_webhook", dispatch)
    hooks = Notifications(webhooks=[
        WebhookConfig(id="dead", enabled=True, url="https://nowhere/x"),
        WebhookConfig(id="live", enabled=True, url="https://somewhere/x"),
    ])

    await _asyncio.wait_for(notify.notify(hooks, "s", "b", {}, db.WARNING), timeout=5)

    state = notify.delivery_state()
    assert state["dead"]["ok"] is False
    # The healthy target keeps its own outcome: the ceiling is per send, so it is not
    # filed under the timeout of the one next to it.
    assert state["live"]["ok"] is True


def test_two_entries_sharing_an_id_are_split_on_load(tmp_path):
    """Duplicate ids used to pass straight through, so two hosts shared one latch."""
    path = tmp_path / "config.yaml"
    save_config(AppConfig(hosts=[
        PveHostConfig(id="host1", name="a", api_url="https://a:8006"),
        PveHostConfig(id="host1", name="b", api_url="https://b:8006"),
    ], ups=[
        SnmpConfig(id="u", name="A", host="10.0.0.1"),
        SnmpConfig(id="u", name="B", host="10.0.0.2"),
    ]), path)
    cfg = load_config(path)

    assert [h.id for h in cfg.hosts] == ["host1", "host1-2"]   # the first keeps its id
    assert len({h.key for h in cfg.hosts}) == 2                # ... so latches differ
    assert [u.id for u in cfg.ups] == ["u", "u-2"]


@pytest.mark.skipif(os.name != "posix", reason="Windows does not honour POSIX modes")
def test_config_is_never_world_readable_even_mid_write(tmp_path):
    """chmod after the write left every plaintext secret at 0644 for its duration."""
    import stat as _stat

    import os as _os

    path = tmp_path / "config.yaml"
    save_config(AppConfig(ups=[SnmpConfig(id="u", host="h", community="s3cret")]), path)
    mode = _stat.S_IMODE(_os.stat(path).st_mode)
    assert mode & 0o077 == 0, oct(mode)


def test_saving_works_with_a_leftover_temp_file(tmp_path):
    """O_EXCL here would have made one crashed save block every future one."""
    path = tmp_path / "config.yaml"
    (tmp_path / "config.yaml.tmp").write_text("junk from an earlier crash", encoding="utf-8")
    save_config(AppConfig(), path)
    assert load_config(path).thresholds.poll_interval_normal_s == 30


@pytest.mark.skipif(os.name != "posix", reason="Windows does not honour POSIX modes")
def test_a_leftover_temp_file_cannot_smuggle_its_old_permissions_back(tmp_path):
    """The other half of the leftover case, and the one that was missing.

    open()'s mode argument only applies when it CREATES the file, so reusing a .tmp left
    behind by a crash — which is deliberately allowed — wrote every plaintext secret into
    whatever permissions that file already carried."""
    import stat as _stat

    import os as _os

    path = tmp_path / "config.yaml"
    leftover = tmp_path / "config.yaml.tmp"
    leftover.write_text("junk", encoding="utf-8")
    _os.chmod(leftover, 0o644)

    save_config(AppConfig(ups=[SnmpConfig(id="u", host="h", community="s3cret")]), path)

    assert _stat.S_IMODE(_os.stat(path).st_mode) & 0o077 == 0


def test_a_slipped_digit_upwards_is_corrected_like_one_downwards(tmp_path):
    """Every timeout here is awaited inside the poll loop, so "6000" where "60" was meant
    parks the decision engine for 100 minutes per stage while the battery drains. That is
    the same class of fault as the negative values, and it was unbounded."""
    path = tmp_path / "config.yaml"
    path.write_text(
        """
thresholds:
  host_shutdown_timeout_s: 6000
  poll_interval_battery_s: 9999
  cluster_prep_timeout_s: 100000
""",
        encoding="utf-8",
    )
    cfg = load_config(path)                                   # must not raise

    assert cfg.thresholds.host_shutdown_timeout_s == 60       # back to the default
    assert cfg.thresholds.poll_interval_battery_s == 8
    assert cfg.thresholds.cluster_prep_timeout_s == 60
    lines = " ".join(cfg.value_corrections())
    assert "host_shutdown_timeout_s" in lines and "poll_interval_battery_s" in lines

    # A large but sane estate is untouched: the ceilings only ever catch a typo.
    sane = Thresholds(host_shutdown_timeout_s=600, cluster_guest_shutdown_timeout_s=1800)
    assert sane.host_shutdown_timeout_s == 600
    assert sane._corrections == []


def test_webhooks_without_an_id_are_given_one_on_load(tmp_path):
    """Third of three, and the one that was skipped: the id keys the per-target delivery
    state and the masked-secret reconcile, so two entries without one shared a record —
    each overwriting the other's "last delivery"."""
    path = tmp_path / "config.yaml"
    path.write_text(
        """
notifications:
  webhooks:
    - url: https://a/hook
      enabled: true
    - url: https://b/hook
      enabled: true
""",
        encoding="utf-8",
    )
    hooks = load_config(path).notifications.webhooks

    assert all(h.id for h in hooks)
    assert len({h.id for h in hooks}) == 2


@pytest.mark.asyncio
async def test_test_endpoint_reports_a_failure_instead_of_raising(_import_target, monkeypatch):
    from fastapi import HTTPException

    main, _ = _import_target

    async def boom(*a, **k):
        raise RuntimeError("name or service not known")

    monkeypatch.setattr(main.notify, "send_webhook", boom)

    result = await main.api_test_webhook({"url": "https://hook/x"})
    assert result["ok"] is False and "service not known" in result["message"]

    with pytest.raises(HTTPException) as exc:  # nothing to send to
        await main.api_test_webhook({"url": ""})
    assert exc.value.status_code == 400


def test_unknown_webhook_values_snap_to_the_defaults():
    # A backup from another version must import; a webhook in the wrong shape is fixable.
    from app.config import WebhookConfig

    hook = WebhookConfig.model_validate({"url": "https://hook/x", "format": "smoke-signal",
                                         "min_severity": "verbose"})

    assert hook.format.value == "json"
    assert hook.min_severity.value == "warning"


# --- Proxmox Backup Server client ------------------------------------------
class _FakeApiClient:
    """Records requests and replays canned responses; patched over the client's httpx."""

    def __init__(self, routes, calls):
        self._routes, self._calls = routes, calls

    def __call__(self, *a, **kw):
        self._calls.append(("client", kw))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        self._calls.append(("GET", url, kw))
        return self._routes.get(url, _FakeJson(404, {}))

    async def post(self, url, **kw):
        self._calls.append(("POST", url, kw))
        return self._routes.get(url, _FakeJson(404, {}))


class _FakeJson:
    def __init__(self, status_code, payload):
        self.status_code, self._payload, self.text = status_code, payload, ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_pbs(monkeypatch, permissions):
    """Patch app.pbs's httpx with a server answering /version and /access/permissions."""
    from app import pbs

    calls: list = []
    routes = {
        "/version": _FakeJson(200, {"data": {"version": "4.2.5"}}),
        "/access/permissions": _FakeJson(200, {"data": permissions}),
        "/nodes/localhost/status": _FakeJson(200, {"data": None}),
    }
    monkeypatch.setattr(pbs.httpx, "AsyncClient", _FakeApiClient(routes, calls))
    return calls


@pytest.mark.asyncio
async def test_pbs_auth_header_uses_the_pbs_scheme_and_a_colon(monkeypatch):
    """The exact separator is what issues #9/#16/#19 tripped over: PBS wants
    ``PBSAPIToken=<id>:<secret>``, not the ``PVEAPIToken=<id>=<secret>`` of PVE."""
    from app import pbs

    calls = _fake_pbs(monkeypatch, {"/system/status": {"Sys.PowerManagement": 1}})
    host = PbsHostConfig(name="Backup-Server", api_url="https://10.0.0.20:8007/",
                         token_id="ups@pbs!shutdown", token_secret="uuid-secret")

    await pbs.test_connection(host)

    kwargs = next(c[1] for c in calls if c[0] == "client")
    assert kwargs["headers"]["Authorization"] == "PBSAPIToken=ups@pbs!shutdown:uuid-secret"
    assert kwargs["base_url"] == "https://10.0.0.20:8007/api2/json"


@pytest.mark.asyncio
async def test_pbs_test_connection_accepts_the_privilege_on_system_status(monkeypatch):
    from app import pbs

    _fake_pbs(monkeypatch, {"/system/status": {"Sys.PowerManagement": 1}})
    result = await pbs.test_connection(
        PbsHostConfig(name="b", api_url="https://x:8007", token_id="u!t"))

    assert result.ok is True and result.has_power_mgmt is True


@pytest.mark.asyncio
async def test_pbs_test_connection_accepts_the_privilege_inherited_from_root(monkeypatch):
    from app import pbs

    _fake_pbs(monkeypatch, {"/": {"Sys.PowerManagement": 1}})
    result = await pbs.test_connection(
        PbsHostConfig(name="b", api_url="https://x:8007", token_id="u!t"))

    assert result.ok is True and result.has_power_mgmt is True


@pytest.mark.asyncio
async def test_pbs_test_connection_warns_without_the_privilege(monkeypatch):
    """Reachable with a valid token but no power rights: ok, yet flagged — the engine
    turns that into a warning rather than a critical event."""
    from app import pbs

    _fake_pbs(monkeypatch, {"/system/status": {"Sys.Audit": 1}})
    result = await pbs.test_connection(
        PbsHostConfig(name="b", api_url="https://x:8007", token_id="u!t"))

    assert result.ok is True and result.has_power_mgmt is False
    assert "Sys.PowerManagement" in result.message and "Admin" in result.message


@pytest.mark.asyncio
async def test_pbs_shutdown_posts_to_the_localhost_node(monkeypatch):
    """PBS ignores the {node} segment, so the free-form name must never reach the path."""
    from app import pbs

    calls = _fake_pbs(monkeypatch, {})
    ok, msg = await pbs.shutdown_node(
        PbsHostConfig(name="Server Backup (DC1)", api_url="https://x:8007", token_id="u!t"))

    assert ok is True
    post = next(c for c in calls if c[0] == "POST")
    assert post[1] == "/nodes/localhost/status"
    assert post[2]["data"] == {"command": "shutdown"}


# --- shutdown target dispatch ----------------------------------------------
@pytest.mark.asyncio
async def test_targets_dispatch_picks_the_client_per_type(monkeypatch):
    from app import targets

    seen = []

    async def fake_pve(host, timeout, **kw):
        seen.append("pve")
        return True, "pve"

    async def fake_pbs(host, timeout, **kw):
        seen.append("pbs")
        return True, "pbs"

    monkeypatch.setattr(targets.proxmox, "shutdown_node", fake_pve)
    monkeypatch.setattr(targets.pbs, "shutdown_node", fake_pbs)

    await targets.shutdown(PveHostConfig(name="a", api_url="x"), 5)
    await targets.shutdown(PbsHostConfig(name="b", api_url="x"), 5)
    assert seen == ["pve", "pbs"]


@pytest.mark.asyncio
async def test_targets_dispatch_reports_an_unknown_type_instead_of_guessing():
    from app import targets
    from app.config import HostConfig

    ok, msg = await targets.shutdown(HostConfig(name="a", api_url="x"), 5)
    assert ok is False and "Unsupported shutdown target type" in msg

    result = await targets.test_connection(HostConfig(name="a", api_url="x"))
    assert result.ok is False and "Unsupported shutdown target type" in result.message


@pytest.mark.asyncio
async def test_target_calls_give_up_instead_of_hanging(monkeypatch):
    """A machine that accepts the connection and then goes quiet must not tie up the
    loop: the deadline is enforced here, not left to httpx."""
    import asyncio

    from app import targets

    async def never_answers(host, timeout, **kw):
        await asyncio.sleep(60)
        return True, "too late"

    monkeypatch.setattr(targets, "DEADLINE_GRACE_S", 0.05)
    monkeypatch.setattr(targets.proxmox, "shutdown_node", never_answers)
    ok, msg = await targets.shutdown(PveHostConfig(name="a", api_url="x"), 0.01)
    assert ok is False and "No response within" in msg

    monkeypatch.setattr(targets.proxmox, "test_connection", never_answers)
    result = await targets.test_connection(PveHostConfig(name="a", api_url="x"), 0.01)
    assert result.ok is False and "No response within" in result.message


# --- staged shutdown: one hanging target must not delay its peers -----------
def _staged_engine(**pbs_kwargs):
    cfg = AppConfig(
        dry_run=False,
        ups=[SnmpConfig(id="u", name="U", host="10.0.0.1")],
        hosts=[
            PveHostConfig(name="pve01", api_url="x", ups_ids=["u"]),
            PbsHostConfig(name="pbs01", api_url="x", ups_ids=["u"], **pbs_kwargs),
            PveHostConfig(name="self", api_url="x", ups_ids=["u"], this_host=True),
        ],
        thresholds=Thresholds(runtime_below_minutes=5),
    )
    eng = Engine(cfg)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     runtime_remaining_min=1)
    return eng


@pytest.mark.asyncio
async def test_a_hanging_target_does_not_delay_its_stage_peers(monkeypatch):
    """The PBS entry stops responding; the PVE host on the same stage must still get its
    command straight away, and it must not have to wait for the hanging one to finish."""
    import asyncio

    from app import engine as engine_mod

    started, finished, release = [], [], asyncio.Event()

    async def fake_shutdown(host, timeout, **kw):
        started.append(host.name)
        if host.type == "pbs":
            await release.wait()
        finished.append(host.name)
        return True, "ok"

    monkeypatch.setattr(engine_mod.targets, "shutdown", fake_shutdown)
    eng = _staged_engine()

    task = asyncio.create_task(eng._evaluate())
    for _ in range(200):  # let the loop reach the first stage
        if finished:
            break
        await asyncio.sleep(0.005)

    # pve01 and pbs01 share order 0 -> same stage, so both are under way at once (their
    # relative start order inside a stage is deliberately not guaranteed). pve01 is served
    # to completion while pbs01 hangs; sequentially it would have had to wait for pbs01.
    assert sorted(started) == ["pbs01", "pve01"]
    assert finished == ["pve01"]
    assert "self" not in started  # the last stage still waits for the first to drain

    release.set()
    await task
    assert finished[-1] == "self"


@pytest.mark.asyncio
async def test_stage_order_is_kept_across_different_order_values(monkeypatch):
    from app import engine as engine_mod

    fired = []

    async def fake_shutdown(host, timeout, **kw):
        fired.append(host.name)
        return True, "ok"

    monkeypatch.setattr(engine_mod.targets, "shutdown", fake_shutdown)
    eng = _staged_engine(order=5)

    await eng._evaluate()

    assert fired == ["pve01", "pbs01", "self"]
    assert eng.host_states["pbs:pbs01"]["shutdown_state"] == "sent"


# --- self-test results + health --------------------------------------------
@pytest.mark.asyncio
async def test_selftest_records_its_outcome_per_host(monkeypatch):
    from app import engine as engine_mod
    from app.proxmox import TestResult

    eng = Engine(AppConfig(hosts=[
        PveHostConfig(name="pve01", api_url="https://x:8006"),
        PbsHostConfig(name="pbs01", api_url="https://x:8007"),
    ]))

    async def fake_test(host, *a, **k):
        if host.type == "pbs":
            return TestResult(True, "no power rights", has_power_mgmt=False)
        return TestResult(True, "ok", has_power_mgmt=True)

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    await eng._run_selftest()

    snap = {h["name"]: h for h in eng.snapshot()["hosts"]}
    assert snap["pve01"]["credentials_ok"] is True
    assert snap["pve01"]["power_mgmt_ok"] is True
    assert snap["pve01"]["last_test_error"] is None
    assert snap["pbs01"]["power_mgmt_ok"] is False
    assert snap["pbs01"]["last_test_error"] == "no power rights"
    assert snap["pbs01"]["type"] == "pbs"


@pytest.mark.asyncio
async def test_a_shutdown_does_not_erase_the_selftest_result(monkeypatch):
    from app import engine as engine_mod
    from app.proxmox import TestResult

    async def fake_test(host, *a, **k):
        return TestResult(True, "ok", has_power_mgmt=True)

    async def fake_shutdown(host, timeout, **kw):
        return True, "sent"

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    monkeypatch.setattr(engine_mod.targets, "shutdown", fake_shutdown)

    eng = _staged_engine()
    await eng._run_selftest()
    await eng._evaluate()

    state = eng.host_states["pbs:pbs01"]
    assert state["shutdown_state"] == "sent"
    assert state["power_mgmt_ok"] is True  # survived the shutdown write


@pytest.mark.asyncio
async def test_health_reports_the_notification_targets(_import_target, monkeypatch):
    """The shutdown credentials have had a health field for releases; the notification
    ones became a first-class signal in 4.1.0 — an event, a dashboard row, a note on the
    card — but the endpoint an external monitor actually polls knew nothing about it."""
    import json

    from app import notify
    from app.config import Notifications, WebhookConfig

    main, _ = _import_target
    main.engine = Engine(AppConfig(notifications=Notifications(webhooks=[
        WebhookConfig(id="w1", name="chat", enabled=True, url="https://example.invalid/a"),
        WebhookConfig(id="w2", name="off", enabled=False, url="https://example.invalid/b"),
    ])))

    before = json.loads((await main.api_health()).body)
    # Enabled targets only, and one that has never been tried is not broken.
    assert before["webhooks_total"] == 1
    assert before["webhooks_ok"] == 1

    notify.DELIVERY.clear()
    try:
        notify._record_delivery(main.engine.cfg.notifications.webhooks[0],
                                RuntimeError("nope"))
        after = json.loads((await main.api_health()).body)
    finally:
        notify.DELIVERY.clear()

    assert after["webhooks_ok"] == 0
    # Monitoring information, like hosts_ok: an unreachable chat server is not a reason to
    # declare a running appliance down.
    assert after["status"] == before["status"]


@pytest.mark.asyncio
async def test_health_reports_the_shutdown_targets(_import_target, monkeypatch):
    import json

    from app import engine as engine_mod
    from app.proxmox import TestResult

    main, _ = _import_target
    main.engine = Engine(AppConfig(hosts=[
        PveHostConfig(name="pve01", api_url="https://x:8006"),
        PbsHostConfig(name="pbs01", api_url="https://x:8007"),
    ]))

    before = json.loads((await main.api_health()).body)
    assert before["hosts_total"] == 2
    assert before["hosts_ok"] == 0
    # Never tested is not the same as failing.
    assert before["hosts_selftest_ok"] is None
    assert before["hosts_selftest_at"] is None

    async def fake_test(host, *a, **k):
        return TestResult(True, "ok", has_power_mgmt=host.type == "pve")

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    await main.engine._run_selftest()

    after = json.loads((await main.api_health()).body)
    assert after["hosts_ok"] == 1
    assert after["hosts_selftest_ok"] is False
    assert after["hosts_selftest_at"]
    # A broken token is reported, but it must not move status/HTTP code by itself:
    # existing uptime monitors keep watching the engine, not the credentials.
    assert after["status"] == before["status"]


# --- host secret reconcile --------------------------------------------------
def test_merge_config_reconciles_host_secrets_by_type_and_name():
    from app import main

    existing = AppConfig(hosts=[
        PveHostConfig(name="node", api_url="x", token_secret="pve-secret"),
        PbsHostConfig(name="node", api_url="x", token_secret="pbs-secret"),
    ])
    incoming = {"hosts": [
        {"name": "node", "type": "pve", "api_url": "x", "token_secret": "**********"},
        {"name": "node", "type": "pbs", "api_url": "x", "token_secret": "**********"},
    ]}

    merged = main._merge_config(incoming, existing)
    # Same name, different product: the secrets must not cross over.
    assert merged.hosts[0].token_secret.get_secret_value() == "pve-secret"
    assert merged.hosts[1].token_secret.get_secret_value() == "pbs-secret"


def test_renaming_a_host_keeps_its_token_secret():
    """The reported bug: the node name is an EDITED field, so identifying hosts by it made
    a correction look like a different host — and the masked placeholder then resolved to
    an empty secret. Sharpened by the host test now suggesting the correct node name."""
    from app import main

    existing = AppConfig(hosts=[
        PveHostConfig(id="pve01", name="PVE01", api_url="x", token_secret="kept")])
    incoming = {"hosts": [
        {"id": "pve01", "name": "pve01", "type": "pve", "api_url": "x",
         "token_secret": "**********"}]}

    merged = main._merge_config(incoming, existing)
    assert merged.hosts[0].name == "pve01"
    assert merged.hosts[0].token_secret.get_secret_value() == "kept"


def test_renaming_a_host_keeps_its_secret_for_the_test_button_too():
    """Where the user meets it first: accept the suggested node name, press Test, and a
    valid token would have been reported as "Authentication failed"."""
    from app import main

    hosts = [PveHostConfig(id="pve01", name="PVE01", api_url="x", token_secret="kept")]
    incoming = {"id": "pve01", "name": "pve01", "type": "pve", "api_url": "x",
                "token_secret": "**********"}

    main._reconcile_host_secrets(incoming, main._find_host(incoming, *main._host_lookups(hosts)))
    assert incoming["token_secret"] == "kept"


def test_a_payload_without_ids_still_finds_its_stored_secret():
    """The upgrade path: the first save after this version arrives carries no ids (a
    cached older app.js). Without the fallback it would drop every secret exactly once —
    the very bug the id was introduced to fix."""
    from app import main

    existing = AppConfig(hosts=[
        PveHostConfig(id="pve01", name="pve01", api_url="x", token_secret="kept")])
    incoming = {"hosts": [
        {"name": "pve01", "type": "pve", "api_url": "x", "token_secret": "**********"}]}

    merged = main._merge_config(incoming, existing)
    assert merged.hosts[0].token_secret.get_secret_value() == "kept"
    assert merged.hosts[0].id == "pve01"  # and it gains the id on the way through


def test_a_stored_config_without_ids_gains_them_on_load(tmp_path):
    import yaml

    from app.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"hosts": [
        {"name": "pve01", "type": "pve", "api_url": "x"},
        {"name": "pve01", "type": "pbs", "api_url": "y"},
    ]}), encoding="utf-8")

    cfg = load_config(path)
    assert all(h.id for h in cfg.hosts)
    assert cfg.hosts[0].key != cfg.hosts[1].key
    assert cfg.hosts[0].name == "pve01"  # nothing else changed


def test_repointing_a_host_to_another_product_drops_the_old_secret():
    from app import main

    existing = AppConfig(hosts=[
        PveHostConfig(name="node", api_url="x", token_secret="pve-secret")])
    incoming = {"hosts": [
        {"name": "node", "type": "pbs", "api_url": "x", "token_secret": "**********"}]}

    merged = main._merge_config(incoming, existing)
    assert merged.hosts[0].token_secret.get_secret_value() == ""


def test_merge_config_accepts_hosts_without_a_type():
    """A stale, cached app.js posts the pre-3.5 payload; that must not be refused."""
    from app import main

    existing = AppConfig(hosts=[
        PveHostConfig(name="pve01", api_url="x", token_secret="kept")])
    incoming = {"hosts": [
        {"name": "pve01", "api_url": "x", "method": "api_token", "token_secret": "**********"}]}

    merged = main._merge_config(incoming, existing)
    assert merged.hosts[0].type == "pve"
    assert merged.hosts[0].token_secret.get_secret_value() == "kept"


# --- cluster awareness (read-only): Ceph flags + HA arm state ---------------
# The guest list every cluster fixture serves unless told otherwise: the appliance's own
# container plus two guests that a cluster-wide stop would have to take down.
_DEFAULT_GUESTS = (
    {"vmid": 950, "node": "pve01", "type": "lxc", "name": "pve-usv", "status": "running"},
    {"vmid": 100, "node": "pve01", "type": "qemu", "name": "web", "status": "running"},
    {"vmid": 101, "node": "pve02", "type": "lxc", "name": "db", "status": "running"},
)


def _cluster_routes(*, is_cluster=True, quorate=1, permissions=None, flags=None,
                    armed_state="armed", has_disarm=True, ha_services=1,
                    shutdown_policy="conditional", has_ceph=True, guests=None,
                    mons=("pve01", "pve02")):
    """Canned answers of a PVE cluster member, shaped like the real API."""
    status = []
    if is_cluster:
        status.append({"type": "cluster", "name": "prod", "quorate": quorate, "nodes": 3})
        status += [{"type": "node", "name": f"pve0{i}", "online": 1, "local": int(i == 1)}
                   for i in (1, 2, 3)]
    else:
        # A standalone node answers with one fake entry and no "cluster" record.
        status.append({"type": "node", "name": "pve01", "nodeid": 0, "online": 1, "local": 1})

    current = [{"type": "fencing", "armed-state": armed_state}]
    current += [{"type": "service", "sid": f"vm:{100 + i}", "state": "started"}
                for i in range(int(ha_services))]

    index = [{"name": "current"}, {"name": "manager_status"}]
    if has_disarm:
        index += [{"name": "disarm-ha"}, {"name": "arm-ha"}]

    flag_values = {"noout": False, "nobackfill": False, "norecover": False,
                   "norebalance": False, **(flags or {})}
    perms = (permissions if permissions is not None
             else {"Sys.Audit": 1, "Sys.Modify": 1, "Sys.Console": 1,
                   "VM.Audit": 1, "VM.PowerMgmt": 1, "Datastore.Audit": 1})
    guest_list = [dict(g) for g in (_DEFAULT_GUESTS if guests is None else guests)]
    routes = {
        "/access/permissions": _FakeJson(200, {"data": {"/": perms}}),
        "/cluster/status": _FakeJson(200, {"data": status}),
        "/cluster/ceph/status": _FakeJson(200, {"data": {
            "health": {"status": "HEALTH_OK"},
            "monmap": {"mons": [{"name": m} for m in mons]},
        }}),
        # Permission-FILTERED, never refused: a token without VM.Audit gets 200 and an
        # empty list, which is exactly why the code may not read "empty" as "no guests".
        "/cluster/resources?type=vm": _FakeJson(
            200, {"data": guest_list if perms.get("VM.Audit") else []}),
        "/cluster/resources?type=storage": _FakeJson(200, {"data": [
            {"storage": "local-lvm", "plugintype": "lvmthin"},
            {"storage": "cephpool", "plugintype": "rbd"},
        ] if perms.get("Datastore.Audit") else []}),
        "/cluster/ceph/flags": _FakeJson(200, {"data": [
            {"name": k, "value": v} for k, v in flag_values.items()]}),
        # The endpoint index is user => 'all', so it answers without Sys.Audit.
        "/cluster/ha/status": _FakeJson(200, {"data": index}),
        "/cluster/ha/status/current": _FakeJson(200, {"data": current}),
        "/cluster/options": _FakeJson(200, {"data": {"ha": {"shutdown_policy": shutdown_policy}}}),
    }
    for g in guest_list:
        routes[f"/nodes/{g['node']}/{g['type']}/{g['vmid']}/config"] = _FakeJson(
            200, {"data": {"rootfs": "local-lvm:vm-%s-disk-0,size=4G" % g["vmid"]}})
    # Realistic 403s: everything except the index is gated on Sys.Audit, and a token
    # without it gets a refusal rather than a helpful answer.
    if not perms.get("Sys.Audit"):
        for path in ("/cluster/status", "/cluster/ceph/status", "/cluster/ceph/flags",
                     "/cluster/ha/status/current", "/cluster/options"):
            routes[path] = _FakeJson(403, {})
    # A cluster without Ceph: pveceph was never initialised, so these endpoints exist in
    # the schema but die on the missing config — a 500, NOT a 403. Telling those two apart
    # is the whole point of ClusterInfo.ceph_unavailable.
    if not has_ceph:
        for path in ("/cluster/ceph/status", "/cluster/ceph/flags"):
            routes[path] = _FakeJson(500, {})
    return routes


def _fake_cluster(monkeypatch, **kw):
    from app import cluster

    calls: list = []
    monkeypatch.setattr(cluster.httpx, "AsyncClient",
                        _FakeApiClient(_cluster_routes(**kw), calls))
    return calls


def _pve(**kw):
    return PveHostConfig(name="pve01", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s", cluster=True, **kw)


@pytest.mark.asyncio
async def test_cluster_inspection_reads_membership_ceph_and_ha(monkeypatch):
    from app import cluster

    _fake_cluster(monkeypatch, flags={"noout": True})
    info = await cluster.inspect(_pve())

    assert info.reachable and info.is_cluster and info.name == "prod"
    assert info.quorate and info.nodes_online == 3 and len(info.nodes) == 3
    assert info.ceph_configured and info.ceph_flags_set == ["noout"]
    assert info.ha_services == 1 and info.ha_resources and info.ha_armed_state == "armed"
    assert info.ha_present and not info.ha_disarmed
    assert info.disarm_supported is True
    assert info.shutdown_policy == "conditional"
    assert info.can_audit and info.can_modify and info.can_console


@pytest.mark.asyncio
async def test_standalone_node_is_detected_and_nothing_else_is_queried(monkeypatch):
    """A standalone node must not look like a one-node cluster, and must not be probed
    for Ceph/HA it does not have."""
    from app import cluster

    calls = _fake_cluster(monkeypatch, is_cluster=False)
    info = await cluster.inspect(_pve())

    assert info.reachable and info.is_cluster is False
    queried = [c[1] for c in calls if c[0] == "GET"]
    assert "/cluster/ceph/flags" not in queried
    assert "/cluster/ha/status/current" not in queried


@pytest.mark.asyncio
async def test_disarm_support_is_detected_from_the_endpoint_index(monkeypatch):
    """Feature detection reads the index, not GET /version — that also covers backports."""
    from app import cluster

    _fake_cluster(monkeypatch, has_disarm=False)
    info = await cluster.inspect(_pve())
    assert info.disarm_supported is False
    # Everything else still works: the Ceph flags are not a 9.2 feature.
    assert info.ceph_configured is True


@pytest.mark.asyncio
async def test_needs_recovery_flags_a_cluster_left_prepared(monkeypatch):
    from app import cluster

    _fake_cluster(monkeypatch, flags={"noout": True, "norecover": True})
    info = await cluster.inspect(_pve())
    assert info.needs_recovery is True
    assert info.ceph_flags_set == ["noout", "norecover"]

    _fake_cluster(monkeypatch, armed_state="disarmed")
    info = await cluster.inspect(_pve())
    assert info.ha_disarmed and info.needs_recovery is True

    _fake_cluster(monkeypatch)  # clean cluster
    info = await cluster.inspect(_pve())
    assert info.needs_recovery is False


@pytest.mark.asyncio
async def test_missing_privileges_are_named_individually(monkeypatch):
    """"403" tells an operator nothing; the missing privilege points at the pveum line."""
    from app import cluster

    _fake_cluster(monkeypatch, permissions={"Sys.Audit": 1})
    info = await cluster.inspect(_pve())

    # Each name carries what it buys: "Sys.Console" alone does not tell an operator
    # whether handing out shell access is worth it.
    assert cluster.missing_privileges(info, want_ceph=True, want_disarm=True) == [
        "Sys.Modify (Ceph maintenance flags)", "Sys.Console (HA disarm)"]
    # Whoever only wants the Ceph flags must not be told to hand out Sys.Console.
    assert cluster.missing_privileges(info, want_ceph=True, want_disarm=False) == [
        "Sys.Modify (Ceph maintenance flags)"]
    assert cluster.missing_privileges(info, want_ceph=False, want_disarm=False) == []


@pytest.mark.asyncio
async def test_no_privilege_is_demanded_for_a_feature_this_cluster_cannot_do(monkeypatch):
    """Least privilege in both directions: a feature that is switched on but absent here
    must not send the operator off to widen a token for something that will never run."""
    from app import cluster

    _fake_cluster(monkeypatch, permissions={"Sys.Audit": 1}, has_ceph=False,
                  has_disarm=False)
    info = await cluster.inspect(_pve())

    assert info.ceph_unavailable and info.disarm_unavailable
    assert cluster.missing_privileges(info, want_ceph=True, want_disarm=True) == []


@pytest.mark.asyncio
async def test_an_unread_feature_still_has_its_privilege_demanded(monkeypatch):
    """"Denied" is not "absent". While the read was merely refused, the privilege is
    still reported — otherwise a token that lacks everything would be told about its
    missing rights one round at a time."""
    from app import cluster

    # No Sys.Audit at all: every cluster read comes back 403, so nothing is settled.
    _fake_cluster(monkeypatch, permissions={})
    info = await cluster.inspect(_pve())

    assert not info.ceph_unavailable, "a 403 must not read as 'no Ceph'"
    missing = cluster.missing_privileges(info, want_ceph=True, want_disarm=True)
    assert [m.split(" ")[0] for m in missing] == ["Sys.Audit", "Sys.Modify", "Sys.Console"]


@pytest.mark.asyncio
async def test_cluster_inspection_never_raises_and_stays_bounded(monkeypatch):
    """Same contract as targets.shutdown(): this runs on the poll loop while the battery
    drains, so a server that accepts the connection and goes quiet must not stall it."""
    import asyncio

    from app import cluster

    class _Hanging:
        def __call__(self, *a, **kw):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            await asyncio.sleep(30)

    monkeypatch.setattr(cluster.httpx, "AsyncClient", _Hanging())
    info = await cluster.inspect(_pve(), timeout=0.05)
    assert info.reachable is False and "gave up" in info.error


@pytest.mark.asyncio
async def test_cluster_inspection_survives_a_broken_answer(monkeypatch):
    """A changed/garbled payload must degrade the report, never break the poll loop."""
    from app import cluster

    routes = _cluster_routes()
    routes["/cluster/status"] = _FakeJson(200, {"data": "not-a-list"})
    routes["/cluster/ha/status/current"] = _FakeJson(500, {})
    monkeypatch.setattr(cluster.httpx, "AsyncClient", _FakeApiClient(routes, []))

    info = await cluster.inspect(_pve())
    assert info.reachable is True
    assert info.is_cluster is False  # unreadable membership: treated as "not a cluster"


def _cluster_engine(monkeypatch, all_nodes=False, host_names=None, disabled=(), **kw):
    """Engine with cluster-enabled PVE hosts, talking to a canned cluster.

    ``all_nodes`` configures every node of the fake cluster, which silences the
    (correct) "not every node is a configured target" warning. ``host_names`` overrides
    the entry names outright, which is how the misnamed-entry cases are built, and
    ``disabled`` names entries that exist but are switched off.
    """
    from app import cluster
    from app.engine import Engine

    monkeypatch.setattr(cluster.httpx, "AsyncClient", _FakeApiClient(_cluster_routes(**kw), []))
    names = host_names or (["pve01", "pve02", "pve03"] if all_nodes else ["pve01"])
    hosts = [PveHostConfig(name=n, api_url="https://pve:8006", token_id="ups@pve!x",
                           token_secret="s", cluster=True, enabled=n not in disabled)
             for n in names]
    return Engine(AppConfig(hosts=hosts))


@pytest.mark.asyncio
async def test_health_check_warns_about_leftovers_from_a_previous_outage(monkeypatch):
    """The two states an operator would otherwise only discover during the next outage."""
    eng = _cluster_engine(monkeypatch, flags={"noout": True}, armed_state="disarmed")
    events = _notify_recorder(eng)

    await eng._check_clusters()

    subjects = " | ".join(s for s, _, _ in events)
    assert "Ceph maintenance flags still set" in subjects
    assert "HA is still disarmed" in subjects
    assert all(sev == "warning" for _, sev, _ in events), "never worse than a warning"


@pytest.mark.asyncio
async def test_health_check_is_quiet_on_a_healthy_cluster(monkeypatch):
    eng = _cluster_engine(monkeypatch, all_nodes=True, shutdown_policy="freeze")
    events = _notify_recorder(eng)

    await eng._check_clusters()

    assert events == []
    snap = eng.cluster_snapshot()
    assert snap[0]["name"] == "prod" and snap[0]["needs_recovery"] is False


@pytest.mark.asyncio
async def test_health_check_warns_once_about_a_missing_disarm_endpoint(monkeypatch):
    """A version limitation repeated at every self-test would train the operator to
    ignore the feed — so it is said once, clearly."""
    eng = _cluster_engine(monkeypatch, has_disarm=False, shutdown_policy="freeze")
    events = _notify_recorder(eng)

    await eng._check_clusters()
    await eng._check_clusters()
    await eng._check_clusters()

    hits = [s for s, _, _ in events if "HA disarm not available" in s]
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_health_check_warns_about_a_shutdown_policy_that_fights_the_shutdown(monkeypatch):
    """'migrate' makes the LRM delay the shutdown while the battery drains, and the
    default 'conditional' recovers services onto nodes that are shutting down."""
    eng = _cluster_engine(monkeypatch, has_disarm=False, shutdown_policy="migrate")
    events = _notify_recorder(eng)
    await eng._check_clusters()
    body = " ".join(b for _, _, b in events)
    assert "shutdown_policy" in " ".join(s for s, _, _ in events)
    assert "delays the shutdown" in body and "freeze" in body

    # With HA actually being disarmed, the policy no longer matters.
    eng = _cluster_engine(monkeypatch, shutdown_policy="migrate")
    events = _notify_recorder(eng)
    await eng._check_clusters()
    assert not [s for s, _, _ in events if "shutdown_policy" in s]


@pytest.mark.asyncio
async def test_health_check_reports_missing_quorum_and_unconfigured_nodes(monkeypatch):
    eng = _cluster_engine(monkeypatch, quorate=0, shutdown_policy="freeze")
    events = _notify_recorder(eng)
    await eng._check_clusters()
    subjects = " | ".join(s for s, _, _ in events)
    assert "no quorum" in subjects
    # Three nodes in the cluster, one configured here.
    assert "not every node is a configured target" in subjects
    # ... and the body names BOTH sides of the comparison. Counts alone state the
    # conclusion while withholding everything needed to act on it.
    body = next(b for s_, _, b in events if "configured target" in s_)
    assert "pve01, pve02, pve03" in body
    assert "Configured here: pve01" in body
    assert "pve02, pve03" in body


@pytest.mark.asyncio
async def test_health_check_names_the_node_an_entry_probably_meant(monkeypatch):
    """A case difference is invisible to the eye and fatal to /nodes/<name>/status."""
    eng = _cluster_engine(monkeypatch, host_names=["PVE01"], shutdown_policy="freeze")
    events = _notify_recorder(eng)
    await eng._check_clusters()

    body = next(b for s_, _, b in events if "configured target" in s_)
    # The warning still fires: a near miss is explained, never silently accepted, because
    # PVE resolves the path segment literally and the shutdown really would fail.
    assert "Configured here: none" in body
    assert "'PVE01'" in body and "likely means 'pve01'" in body
    assert "/nodes/<name>/status" in body


@pytest.mark.asyncio
async def test_health_check_explains_an_fqdn_entry_the_same_way(monkeypatch):
    eng = _cluster_engine(monkeypatch, host_names=["pve01.example.com"],
                          shutdown_policy="freeze")
    events = _notify_recorder(eng)
    await eng._check_clusters()

    body = next(b for s_, _, b in events if "configured target" in s_)
    assert "likely means 'pve01'" in body


@pytest.mark.asyncio
async def test_health_check_tells_a_disabled_entry_from_a_misnamed_one(monkeypatch):
    """Both leave a node running, but the fix is completely different."""
    eng = _cluster_engine(monkeypatch, host_names=["pve01", "pve02", "pve03"],
                          disabled=("pve03",), shutdown_policy="freeze")
    events = _notify_recorder(eng)
    await eng._check_clusters()

    body = next(b for s_, _, b in events if "configured target" in s_)
    assert "'pve03' would match, but that entry is disabled." in body
    assert "matching no node" not in body


# --- node name matching -----------------------------------------------------
def test_node_coverage_separates_covered_missing_and_misnamed():
    from app.cluster import node_coverage

    cov = node_coverage(["pve01", "pve02"], ["pve01", "PVE02", "backup.lan"])
    assert cov.covered == ["pve01"]
    assert cov.missing == ["pve02"]
    assert cov.unmatched == ["PVE02", "backup.lan"]
    # Only the entry that plausibly meant a real node gets a suggestion; "backup.lan"
    # resembles nothing here and must not be paired with an arbitrary node.
    assert cov.near == [("PVE02", "pve02")]


def test_node_coverage_never_matches_loosely():
    """The loose form explains a mismatch; it must never resolve one."""
    from app.cluster import node_coverage

    cov = node_coverage(["pve01"], ["PVE01"])
    assert cov.covered == [] and cov.missing == ["pve01"]


@pytest.mark.asyncio
async def test_list_nodes_only_reports_a_readable_index(monkeypatch):
    """A token that may not enumerate nodes is not evidence of a wrong node name."""
    from app import proxmox

    host = PveHostConfig(name="pve01", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")

    async def run(route):
        monkeypatch.setattr(proxmox.httpx, "AsyncClient",
                            _FakeApiClient({"/nodes": route}, []))
        return await proxmox.list_nodes(host)

    ok = await run(_FakeJson(200, {"data": [{"node": "pve01"}, {"node": "pve02"}]}))
    assert ok.readable and ok.nodes == ["pve01", "pve02"]

    for refused in (_FakeJson(403, {}), _FakeJson(200, {"data": []}),
                    _FakeJson(200, {"data": None})):
        res = await run(refused)
        assert res.readable is False and res.nodes == []


async def _node_check(name, known):
    from app import main

    host = PveHostConfig(name=name, api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")
    return await main._check_node_name(host, known)


@pytest.mark.asyncio
async def test_node_check_stays_silent_when_the_api_will_not_name_its_nodes():
    from app import proxmox

    data, message = await _node_check("whatever", proxmox.NodeList())
    assert data["readable"] is False and data["suggestion"] is None
    # The regression this guards: "could not read" must never be reported as "wrong".
    assert message == ""


@pytest.mark.asyncio
async def test_node_check_offers_the_node_a_misspelled_entry_meant():
    from app import proxmox

    data, message = await _node_check(
        "PVE01", proxmox.NodeList(readable=True, nodes=["pve01", "pve02"]))
    assert data["match"] is False
    assert data["suggestion"] == "pve01" and data["reason"] == "near"
    # The diagnosis belongs to verify_node(), which the credential test already ran, and
    # the offer is rendered as a clickable link — so there is nothing left to say in words.
    assert message == ""


@pytest.mark.asyncio
async def test_node_check_does_not_guess_for_an_unrecognisable_name():
    """Picking one of several nodes at random could shut down the wrong machine."""
    from app import proxmox

    data, message = await _node_check(
        "backup", proxmox.NodeList(readable=True, nodes=["pve01", "pve02"]))
    assert data["suggestion"] is None and data["reason"] is None
    assert message == ""  # nothing to offer, so nothing is said twice


@pytest.mark.asyncio
async def test_node_check_fills_an_empty_field_from_the_node_that_answered():
    from app import proxmox

    data, _ = await _node_check("", proxmox.NodeList(
        readable=True, nodes=["pve01", "pve02"], local="pve02"))
    assert data["suggestion"] == "pve02" and data["reason"] == "local"

    # Without a local marker a single-node answer is still unambiguous.
    data, _ = await _node_check("", proxmox.NodeList(readable=True, nodes=["pve01"]))
    assert data["suggestion"] == "pve01" and data["reason"] == "only"

    # Several nodes and nothing pointing at one of them: no suggestion.
    data, _ = await _node_check(
        "", proxmox.NodeList(readable=True, nodes=["pve01", "pve02"]))
    assert data["suggestion"] is None


@pytest.mark.asyncio
async def test_node_check_offers_the_node_this_api_url_actually_answers_for():
    """The cluster case: every member is "a node this API knows", so the plain list said
    "ok" for a name belonging to a different machine. The local marker decides."""
    from app import proxmox

    data, message = await _node_check("pve02", proxmox.NodeList(
        readable=True, nodes=["pve01", "pve02"], local="pve01"))

    assert data["match"] is False
    # Not a guess: the API itself said which node answers here.
    assert data["suggestion"] == "pve01" and data["reason"] == "local"
    # The diagnosis is already in the test message (from verify_node), so nothing here.
    assert message == ""


@pytest.mark.asyncio
async def test_host_test_reports_coverage_and_reuses_the_cluster_node_list(monkeypatch):
    """The wizard is where this is still cheap to fix, so it says it there too — and
    /cluster/status already named every member, so no second round trip is made."""
    from app import cluster, main
    from app.engine import Engine

    calls: list = []
    monkeypatch.setattr(cluster.httpx, "AsyncClient",
                        _FakeApiClient(_cluster_routes(), calls))
    host = PveHostConfig(name="PVE01", api_url="https://pve:8006", token_id="ups@pve!x",
                         token_secret="s", cluster=True)
    monkeypatch.setattr(main, "engine", Engine(AppConfig(hosts=[host])))

    _, message, known = await main._check_host_cluster(host)

    assert known.readable and known.nodes == ["pve01", "pve02", "pve03"]
    assert known.local == "pve01"  # the member that answered this api_url
    assert "Not covered by any entry: pve01, pve02, pve03." in message
    assert not [c for c in calls if c[0] == "GET" and c[1] == "/nodes"]


# --- the clients stay inside the deadline app/targets.py gives them ----------
# Both entry points there are documented as never exceeding their timeout by more than
# DEADLINE_GRACE_S, and the guarantee is what keeps one slow target from costing another
# its shutdown. The clients underneath issue SEVERAL sequential requests, so handing each
# of them the full timeout breaks it from the inside: the wait_for fires first and a
# merely slow host is reported as "gave up" — for the self-test a CRITICAL failure on a
# target whose credentials are fine, for a shutdown the loss of the retry.


def _worst_case_s(calls) -> float:
    """Longest the recorded exchange can take: per client, its read timeout per request."""
    total, per_request = 0.0, 0.0
    for call in calls:
        if call[0] == "client":
            per_request = float(call[1]["timeout"].read)
        else:
            total += per_request
    return total


@pytest.mark.asyncio
async def test_credential_test_fits_in_the_deadline_targets_allows_it(monkeypatch):
    from app import proxmox, targets

    calls: list = []
    routes = {
        "/version": _FakeJson(200, {"data": {"version": "8.2"}}),
        "/access/permissions": _FakeJson(200, {"data": {"/": {"Sys.PowerMgmt": 1}}}),
        "/cluster/status": _FakeJson(200, {"data": [{"type": "node", "name": "pve01",
                                                     "local": 1}]}),
    }
    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _FakeApiClient(routes, calls))
    host = PveHostConfig(name="pve01", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")

    result = await proxmox.test_connection(host, timeout=10.0)

    assert result.ok and result.has_power_mgmt and result.node_state == "ok"
    # /version + /access/permissions + the node listing, all sequential.
    assert len([c for c in calls if c[0] == "GET"]) >= 3
    assert _worst_case_s(calls) <= 10.0 + targets.DEADLINE_GRACE_S


@pytest.mark.asyncio
async def test_pbs_credential_test_fits_in_the_same_deadline(monkeypatch):
    from app import pbs, targets

    calls = _fake_pbs(monkeypatch, {"/system/status": {"Sys.PowerManagement": 1}})

    host = PbsHostConfig(name="backup", api_url="https://pbs:8007",
                         token_id="ups@pbs!x", token_secret="s")
    result = await pbs.test_connection(host, timeout=10.0)

    assert result.ok and result.has_power_mgmt
    assert _worst_case_s(calls) <= 10.0 + targets.DEADLINE_GRACE_S


@pytest.mark.asyncio
async def test_both_shutdown_forms_share_one_budget(monkeypatch):
    """The fallback has to be reachable, not just written down.

    A first form that hangs rather than answering used to leave the second one the five
    seconds of grace, so the retry this routine exists for never happened and the result
    was a bare "gave up".
    """
    from app import proxmox, targets

    calls: list = []
    routes = {
        "/nodes/localhost/status": _FakeJson(403, {}),   # node-scoped privilege
        "/nodes/pve01/status": _FakeJson(200, {"data": None}),
    }
    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _FakeApiClient(routes, calls))
    host = PveHostConfig(name="pve01", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")

    ok, message = await proxmox.shutdown_node(host, timeout=60.0, use_localhost=True)

    assert ok and "/nodes/pve01" in message and "Fix this host entry" in message
    assert _worst_case_s(calls) <= 60.0 + targets.DEADLINE_GRACE_S


# --- node name verification (the shutdown path) -----------------------------
def _nodes_client(monkeypatch, nodes=("pve01", "pve02"), status=200):
    from app import proxmox

    payload = {"data": [{"node": n} for n in nodes]}
    monkeypatch.setattr(proxmox.httpx, "AsyncClient",
                        _FakeApiClient({"/nodes": _FakeJson(status, payload)}, []))
    return PveHostConfig(name="pve01", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")


@pytest.mark.asyncio
async def test_list_nodes_marks_the_node_that_answered(monkeypatch):
    """/cluster/status is asked first because it is the only listing that says which
    member is behind this API URL — without it every cluster name looked equally right."""
    from app import proxmox

    host = PveHostConfig(name="pve01", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")
    calls: list = []
    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _FakeApiClient({
        "/cluster/status": _FakeJson(200, {"data": [
            {"type": "cluster", "name": "prod"},
            {"type": "node", "name": "pve01", "local": 1},
            {"type": "node", "name": "pve02"},
        ]}),
    }, calls))

    known = await proxmox.list_nodes(host)

    assert known.readable and known.nodes == ["pve01", "pve02"]
    assert known.local == "pve01"          # the cluster record is not a node
    assert not [c for c in calls if c[1] == "/nodes"]   # no second listing needed


@pytest.mark.asyncio
async def test_list_nodes_falls_back_to_the_index_without_sys_audit(monkeypatch):
    """The node listing must not start demanding Sys.Audit: without it the index still
    answers, and the verdict simply stops short of "which one is local"."""
    from app import proxmox

    host = PveHostConfig(name="pve01", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")
    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _FakeApiClient({
        "/cluster/status": _FakeJson(403, {}),
        "/nodes": _FakeJson(200, {"data": [{"node": "pve01"}, {"node": "pve02"}]}),
    }, []))

    known = await proxmox.list_nodes(host)

    assert known.readable and known.nodes == ["pve01", "pve02"] and known.local is None
    assert (await proxmox.verify_node(host)).state == "ok"


@pytest.mark.asyncio
async def test_verify_node_confirms_a_name_the_api_lists(monkeypatch):
    from app import proxmox

    host = _nodes_client(monkeypatch)
    assert (await proxmox.verify_node(host)).state == "ok"


@pytest.mark.asyncio
async def test_verify_node_rejects_a_name_no_node_carries(monkeypatch):
    from app import proxmox

    host = _nodes_client(monkeypatch, nodes=("pve02", "pve03"))
    verdict = await proxmox.verify_node(host)
    assert verdict.state == "wrong"
    assert "pve02, pve03" in verdict.detail


@pytest.mark.asyncio
async def test_verify_node_rejects_an_fqdn_without_asking_the_api(monkeypatch):
    """PVE's own schema forbids dots, so this never even reaches the handler (HTTP 400).
    Checked locally: it costs no round trip and names the actual reason."""
    from app import proxmox

    calls: list = []
    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _FakeApiClient({}, calls))
    host = PveHostConfig(name="pve01.example.com", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")

    verdict = await proxmox.verify_node(host)
    assert verdict.state == "invalid"
    assert not [c for c in calls if c[0] == "GET"]


@pytest.mark.asyncio
async def test_verify_node_gives_no_verdict_when_no_node_was_named(monkeypatch):
    """The regression guard: "could not read" must never be reported as "wrong"."""
    from app import proxmox

    host = _nodes_client(monkeypatch, status=403)
    assert (await proxmox.verify_node(host)).state == "unverified"


@pytest.mark.asyncio
async def test_verify_node_accepts_whatever_the_api_lists_verbatim(monkeypatch):
    """The evidence beats the string comparison — which is what keeps us out of the
    question whether PVE matches the path segment case-sensitively."""
    from app import proxmox

    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _FakeApiClient(
        {"/nodes": _FakeJson(200, {"data": [{"node": "PVE01"}]})}, []))
    host = PveHostConfig(name="PVE01", api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")
    assert (await proxmox.verify_node(host)).state == "ok"


@pytest.mark.asyncio
async def test_verify_node_spots_a_real_node_behind_the_wrong_api_url(monkeypatch):
    """The failure mode no name list can see: the shutdown succeeds by proxy and takes
    down a different machine, leaving this one running."""
    from app import proxmox

    host = _nodes_client(monkeypatch)
    known = proxmox.NodeList(readable=True, nodes=["pve01", "pve02"], local="pve02")
    verdict = await proxmox.verify_node(host, known=known)
    assert verdict.state == "proxied"
    assert "belongs to 'pve02'" in verdict.detail


# --- the shutdown path itself ----------------------------------------------
def _shutdown_probe(monkeypatch, results):
    """Patch app.proxmox's httpx so each /nodes/<n>/status POST answers from `results`."""
    from app import proxmox

    calls: list = []
    routes = {f"/nodes/{n}/status": _FakeJson(code, {}) for n, code in results.items()}
    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _FakeApiClient(routes, calls))
    return calls


def _pve(name="pve01"):
    return PveHostConfig(name=name, api_url="https://pve:8006",
                         token_id="ups@pve!x", token_secret="s")


@pytest.mark.asyncio
async def test_shutdown_addresses_the_node_behind_the_url_by_default(monkeypatch):
    from app import proxmox

    calls = _shutdown_probe(monkeypatch, {"localhost": 200})
    ok, msg = await proxmox.shutdown_node(_pve(), use_localhost=True)
    assert ok and "localhost" in msg
    assert [c[1] for c in calls if c[0] == "POST"] == ["/nodes/localhost/status"]


@pytest.mark.asyncio
async def test_shutdown_keeps_the_name_when_the_url_is_shared(monkeypatch):
    from app import proxmox

    calls = _shutdown_probe(monkeypatch, {"pve01": 200})
    ok, _ = await proxmox.shutdown_node(_pve(), use_localhost=False)
    assert ok
    assert [c[1] for c in calls if c[0] == "POST"] == ["/nodes/pve01/status"]


@pytest.mark.asyncio
async def test_shutdown_retries_the_other_form_and_says_so(monkeypatch):
    """A misspelled name answers 400/500; localhost then still brings the machine down —
    and the message has to make clear the configuration is still broken."""
    from app import proxmox

    calls = _shutdown_probe(monkeypatch, {"pve01": 500, "localhost": 200})
    ok, msg = await proxmox.shutdown_node(_pve(), use_localhost=False)
    assert ok
    assert [c[1] for c in calls if c[0] == "POST"] == [
        "/nodes/pve01/status", "/nodes/localhost/status"]
    assert "Fix this host entry" in msg


@pytest.mark.asyncio
async def test_shutdown_retries_even_a_refusal(monkeypatch):
    """A token holding Sys.PowerMgmt on /nodes/<name> rather than /nodes refuses the
    localhost form. A refused call did nothing, so the retry is free."""
    from app import proxmox

    calls = _shutdown_probe(monkeypatch, {"localhost": 403, "pve01": 200})
    ok, _ = await proxmox.shutdown_node(_pve(), use_localhost=True)
    assert ok
    assert [c[1] for c in calls if c[0] == "POST"] == [
        "/nodes/localhost/status", "/nodes/pve01/status"]


@pytest.mark.asyncio
async def test_shutdown_does_not_call_the_same_path_twice(monkeypatch):
    from app import proxmox

    calls = _shutdown_probe(monkeypatch, {"localhost": 500})
    ok, msg = await proxmox.shutdown_node(_pve("localhost"), use_localhost=True)
    assert ok is False
    assert len([c for c in calls if c[0] == "POST"]) == 1
    assert "/nodes/localhost" in msg


@pytest.mark.asyncio
async def test_shutdown_failure_names_both_paths(monkeypatch):
    from app import proxmox

    _shutdown_probe(monkeypatch, {"pve01": 500, "localhost": 500})
    ok, msg = await proxmox.shutdown_node(_pve(), use_localhost=True)
    assert ok is False
    assert "/nodes/localhost" in msg and "/nodes/pve01" in msg


# --- duplicate API URLs ------------------------------------------------------
def test_duplicate_api_urls_ignores_spelling_and_disabled_entries():
    cfg = AppConfig(hosts=[
        PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006"),
        PveHostConfig(name="pve02", api_url="HTTPS://10.0.0.10:8006/"),
        PveHostConfig(name="pve03", api_url="https://10.0.0.30:8006"),
        PveHostConfig(name="pve04", api_url="https://10.0.0.30:8006", enabled=False),
    ])
    assert cfg.duplicate_api_urls() == ["https://10.0.0.10:8006"]
    assert cfg.api_url_is_unique(cfg.hosts[0]) is False
    # A disabled twin does not make the enabled entry ambiguous.
    assert cfg.api_url_is_unique(cfg.hosts[2]) is True


# --- node name: how the engine weighs it ------------------------------------
def _node_verdict_engine(monkeypatch, node_state, urls=("https://10.0.0.10:8006",)):
    """Engine whose credential test is fine and whose node verdict is `node_state`."""
    from app import engine as engine_mod
    from app.proxmox import TestResult

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))
    hosts = [PveHostConfig(name=f"pve0{i}", api_url=u) for i, u in enumerate(urls, 1)]
    eng = Engine(AppConfig(hosts=hosts))

    async def fake_test(host, *a, **k):
        return TestResult(True, "ok", has_power_mgmt=True, node_state=node_state)

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    monkeypatch.setattr(eng, "_log_quiet", lambda s, b, sev: None)
    return eng


@pytest.mark.asyncio
async def test_a_wrong_node_name_is_only_a_label_when_the_url_is_the_entrys_own(monkeypatch):
    """With one API URL per entry the shutdown addresses the node directly, so a wrong
    name misleads every event but cannot stop the machine from coming down."""
    eng = _node_verdict_engine(monkeypatch, "wrong")
    events = _notify_recorder(eng)

    await eng._run_selftest()

    assert eng.host_states[eng.cfg.hosts[0].key]["node_state"] == "wrong"
    assert eng.last_selftest_ok is True  # not a broken target
    assert not [s for s, _, _ in events if "FAILED" in s]


@pytest.mark.asyncio
async def test_a_wrong_node_name_fails_the_host_when_entries_share_one_url(monkeypatch):
    """There PVE's proxying is the only thing telling the entries apart: the name IS the
    path, and the shutdown will not land."""
    eng = _node_verdict_engine(
        monkeypatch, "wrong",
        urls=("https://10.0.0.10:8006", "https://10.0.0.10:8006"),
    )
    events = _notify_recorder(eng)

    await eng._run_selftest()

    assert eng.last_selftest_ok is False
    assert [s for s, _, _ in events if "FAILED" in s]
    assert [s for s, _, _ in events if "share one API URL" in s]


@pytest.mark.asyncio
async def test_an_unverified_node_name_is_never_held_against_a_host(monkeypatch):
    eng = _node_verdict_engine(monkeypatch, "unverified")
    events = _notify_recorder(eng)

    await eng._run_selftest()

    assert eng.last_selftest_ok is True
    assert not [s for s, _, _ in events if "FAILED" in s]


# --- entries stored in a state in which they cannot do their job --------------------
def test_incomplete_entries_names_what_each_card_is_missing():
    """The server-side half of app.js's incompleteCards().

    It has to exist separately because the two paths where this actually happens never
    pass a form: POST /api/config/import and a hand-edited config.yaml.
    """
    cfg = AppConfig(
        ups=[
            SnmpConfig(id="ok", host="10.0.0.1"),
            SnmpConfig(id="noaddr"),
            NutConfig(id="nonut", host="10.0.0.2"),  # address but no ups.conf section
        ],
        hosts=[
            PveHostConfig(id="good", name="pve01", api_url="https://10.0.0.10:8006",
                          token_id="ups@pve!s", token_secret="sec"),
            PveHostConfig(id="nourl", name="pve02", api_url="",
                          token_id="ups@pve!s", token_secret="sec"),
            PveHostConfig(id="notok", name="pve03", api_url="https://10.0.0.12:8006"),
            # Switched off: it shuts nothing down, so none of its fields can fail during
            # an outage — and demanding them would leave an installation upgrading from a
            # release that stored such entries happily unable to save at all.
            PveHostConfig(id="off", name="pve04", api_url="", enabled=False),
        ],
        notifications=Notifications(webhooks=[
            WebhookConfig(id="w1", enabled=True, url=""),
            WebhookConfig(id="w2", enabled=False, url=""),  # off: nothing is sent anyway
        ]),
    )
    found = {label: missing for _kind, label, missing in cfg.incomplete_entries()}

    assert "noaddr" in found and "nonut" in found
    assert "ok" not in found
    assert found["pve02"] == "no API URL"
    assert found["pve03"] == "no API token ID, no API token secret"
    assert "pve01" not in found and "pve04" not in found
    assert "w1" in found and "w2" not in found


@pytest.mark.asyncio
async def test_an_incomplete_host_is_reported_once_and_reaches_the_snapshot(monkeypatch):
    """A backup import stores it, the dashboard renders it complete, and nothing else says
    otherwise in time: verify_node() answers "unverified" for an entry it cannot even
    address, which _node_name_ok() correctly reads as *no verdict* rather than a fault."""
    from app import engine as engine_mod

    eng = Engine(AppConfig(hosts=[
        PveHostConfig(id="h", name="pve01", api_url="https://10.0.0.10:8006")]))

    async def fake_verify(host, *a, **k):
        from app.proxmox import NodeVerdict
        return NodeVerdict(state="unverified")

    monkeypatch.setattr(engine_mod.targets, "verify_node", fake_verify)
    events = _notify_recorder(eng)

    await eng._maybe_node_startup_check()
    said = [(s, sev) for s, sev, _ in events if "incomplete configuration" in s]
    assert said == [("Host pve01: incomplete configuration", db.CRITICAL)]

    # Once per finding and configuration — repeating it every self-test trains the
    # operator to skip the feed.
    events.clear()
    await eng._run_selftest()
    assert [s for s, _, _ in events if "incomplete configuration" in s] == []

    # ...but it stays true on the dashboard until it is actually fixed.
    host = eng.snapshot()["hosts"][0]
    assert host["incomplete"] == "no API token ID, no API token secret"


def test_an_unpollable_ups_says_so_instead_of_looking_unreachable():
    """Stored without an address it is never polled at all, which renders identically to a
    device that is simply not answering — while fail safe means every host it feeds is
    refused a shutdown either way."""
    eng = Engine(AppConfig(ups=[SnmpConfig(id="u", name="Rack A")]))
    assert eng.snapshot()["ups"][0]["incomplete"] == "no address to poll"

    ok = Engine(AppConfig(ups=[SnmpConfig(id="u", host="10.0.0.9")]))
    assert ok.snapshot()["ups"][0]["incomplete"] is None


@pytest.mark.asyncio
async def test_a_green_selftest_still_says_dry_run_is_on(monkeypatch):
    """The one thing a green self-test does not say by itself: every token can be valid
    while the master switch means none of it is ever used."""
    from app import engine as engine_mod
    from app.proxmox import TestResult

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))

    async def fake_test(host, *a, **k):
        return TestResult(True, "ok", has_power_mgmt=True, node_state="ok")

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)

    def _engine(dry_run):
        return Engine(AppConfig(
            dry_run=dry_run, configured=True,
            hosts=[PveHostConfig(id="h", name="pve01", api_url="https://10.0.0.10:8006",
                                 token_id="ups@pve!s", token_secret="sec")]))

    eng = _engine(True)
    quiet: list[str] = []
    eng._log_quiet = lambda subject, body, sev: quiet.append(subject)  # type: ignore
    await eng._run_selftest()
    assert [q for q in quiet if "Dry-run is on" in q]

    armed = _engine(False)
    quiet2: list[str] = []
    armed._log_quiet = lambda subject, body, sev: quiet2.append(subject)  # type: ignore
    await armed._run_selftest()
    assert [q for q in quiet2 if "Dry-run is on" in q] == []


def _green_selftest_engine(monkeypatch, **cfg_kw):
    """An engine whose hosts always pass the credential check, with the quiet log captured."""
    from app import engine as engine_mod
    from app.proxmox import TestResult

    monkeypatch.setattr(engine_mod, "_local_now", lambda: datetime(2026, 7, 25, 10, 0))

    async def fake_test(host, *a, **k):
        return TestResult(True, "ok", has_power_mgmt=True, node_state="ok")

    monkeypatch.setattr(engine_mod.targets, "test_connection", fake_test)
    eng = Engine(AppConfig(
        configured=True,
        hosts=[PveHostConfig(id="h", name="pve01", api_url="https://10.0.0.10:8006",
                             token_id="ups@pve!s", token_secret="sec")], **cfg_kw))
    quiet: list[str] = []
    eng._log_quiet = lambda subject, body, sev: quiet.append(subject)  # type: ignore
    return eng, quiet


@pytest.mark.asyncio
async def test_selftest_log_ok_off_silences_only_the_green_lines(monkeypatch):
    """#28: the daily "ok" line is noise in a small estate — the CHECK is not.

    The request was a weekly or monthly interval, which would have meant testing the
    credentials less often; this answers the actual complaint instead. Everything that
    reports a problem stays exactly as loud as before.
    """
    eng, quiet = _green_selftest_engine(monkeypatch, selftest_log_ok=False)
    await eng._run_selftest()
    assert [q for q in quiet if "Self-test" in q] == []
    # The verdict itself is unaffected: the dashboard, /api/status and /api/health all read
    # host_states, and none of them may go blind because the log line was switched off.
    assert eng.host_states["h"]["credentials_ok"] is True
    assert eng.last_selftest_ok is True and eng.last_selftest_at is not None

    on, quiet_on = _green_selftest_engine(monkeypatch, selftest_log_ok=True)
    await on._run_selftest()
    assert [q for q in quiet_on if "Self-test pve01: ok" in q]


@pytest.mark.asyncio
async def test_selftest_log_ok_off_keeps_failures_and_the_manual_run(monkeypatch):
    """The two things that must never be silenced: a failure, and an explicit request."""
    from app import engine as engine_mod
    from app.proxmox import TestResult

    eng, quiet = _green_selftest_engine(monkeypatch, selftest_log_ok=False)
    emitted: list = []
    eng._emit = lambda subject, body, severity: _async_append(emitted, (subject, severity))

    async def failing(host, *a, **k):
        return TestResult(False, "Authentication failed (token invalid?)")

    monkeypatch.setattr(engine_mod.targets, "test_connection", failing)
    await eng._run_selftest()
    assert [s for s, _ in emitted if "FAILED" in s]

    # The recovery closes out that failure and is written even with the switch off:
    # otherwise the log's last word on this host stays "FAILED" forever.
    async def passing(host, *a, **k):
        return TestResult(True, "ok", has_power_mgmt=True, node_state="ok")

    monkeypatch.setattr(engine_mod.targets, "test_connection", passing)
    quiet.clear()
    await eng._run_selftest()
    assert [q for q in quiet if "Self-test pve01: ok" in q]

    # And "Test now" reports its result whatever the switch says — pressing a button and
    # being told nothing is indistinguishable from a broken button.
    ok, quiet_ok = _green_selftest_engine(monkeypatch, selftest_log_ok=False)
    await ok._run_selftest(force_log=True)
    assert [q for q in quiet_ok if "Self-test pve01: ok" in q]


@pytest.mark.asyncio
async def test_dry_run_notice_survives_the_selftest_log_switch(monkeypatch):
    """The switch hides good news, not a standing misconfiguration.

    Both lines shared one flag, so silencing the green ones would also have silenced the
    only line saying the appliance will not actually shut anything down — which is why
    _run_selftest() keeps log_daily and log_ok apart.
    """
    eng, quiet = _green_selftest_engine(monkeypatch, dry_run=True, selftest_log_ok=False)
    await eng._run_selftest()
    assert [q for q in quiet if "Self-test" in q] == []
    assert [q for q in quiet if "Dry-run is on" in q]


async def _async_append(sink: list, item) -> None:
    sink.append(item)


def test_config_roundtrip_selftest_log_ok(tmp_path):
    path = tmp_path / "c.yaml"
    save_config(AppConfig(selftest_log_ok=False), path)
    assert load_config(path).selftest_log_ok is False
    # Absent (every config written before 4.2.0) means the previous behaviour, not silence.
    assert AppConfig().selftest_log_ok is True


def _startup_engine(monkeypatch, state, detail="nope", **cfg_kw):
    from app import engine as engine_mod
    from app.proxmox import NodeVerdict

    eng = Engine(AppConfig(
        hosts=[PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006",
                             token_id="ups@pve!s", token_secret="sec")], **cfg_kw))

    async def fake_verify(host, *a, **k):
        return NodeVerdict(state=state, detail=detail)

    monkeypatch.setattr(engine_mod.targets, "verify_node", fake_verify)
    return eng


@pytest.mark.asyncio
async def test_node_names_are_checked_at_startup_not_at_the_next_slot(monkeypatch):
    """last_selftest_slot is persisted and survives a restart, so after an update the next
    scheduled run may be a day away. A node name that points at nothing must not wait."""
    eng = _startup_engine(monkeypatch, "wrong")
    eng.last_selftest_slot = datetime(2099, 1, 1)  # nothing is due for a long time
    events = _notify_recorder(eng)

    await eng._maybe_node_startup_check()

    assert eng.host_states[eng.cfg.hosts[0].key]["node_state"] == "wrong"
    assert [s for s, _, _ in events if "node name does not match" in s]
    # Runs once per process start, not on every poll iteration.
    events.clear()
    await eng._maybe_node_startup_check()
    assert events == []


# --- a host pointing at a UPS that no longer exists ---------------------------------
def _stale_feed_engine(monkeypatch, ups_ids, policy="all"):
    from app import engine as engine_mod
    from app.proxmox import NodeVerdict

    eng = Engine(AppConfig(
        ups=[SnmpConfig(id="a", host="10.0.0.1")],
        hosts=[PveHostConfig(name="pve01", api_url="https://10.0.0.10:8006",
                             token_id="ups@pve!s", token_secret="sec",
                             ups_ids=ups_ids, ups_policy=policy)],
    ))

    async def fake_verify(host, *a, **k):
        return NodeVerdict(state="unverified")

    monkeypatch.setattr(engine_mod.targets, "verify_node", fake_verify)
    return eng


def test_stale_feed_ids_names_only_what_is_really_gone():
    cfg = AppConfig(ups=[SnmpConfig(id="a", host="10.0.0.1")],
                    hosts=[PveHostConfig(name="h", api_url="x", ups_ids=["a", "ghost"])])
    assert cfg.stale_feed_ids(cfg.hosts[0]) == ["ghost"]
    # An empty assignment means "all configured UPS" — that is not staleness.
    cfg.hosts[0].ups_ids = []
    assert cfg.stale_feed_ids(cfg.hosts[0]) == []


@pytest.mark.asyncio
async def test_a_host_whose_only_feed_vanished_is_reported_as_never_shut_down(monkeypatch):
    """The one silent failure: no feeds means never eligible, with nothing saying so."""
    eng = _stale_feed_engine(monkeypatch, ["ghost"])
    events = _notify_recorder(eng)

    await eng._maybe_node_startup_check()

    hits = [b for s, sev, b in events
            if "unknown UPS assignment" in s and sev == "critical"]
    assert hits and "NEVER" in hits[0]
    # And the dashboard can see it without waiting for an event to scroll past.
    assert eng.snapshot()["hosts"][0]["stale_ups_ids"] == ["ghost"]


@pytest.mark.asyncio
async def test_a_partly_stale_assignment_says_the_redundancy_is_gone(monkeypatch):
    """Quieter and just as wrong: policy "all" is satisfied by whatever is left."""
    eng = _stale_feed_engine(monkeypatch, ["a", "ghost"])
    events = _notify_recorder(eng)

    await eng._maybe_node_startup_check()

    hits = [b for s, _, b in events if "unknown UPS assignment" in s]
    assert hits and "redundancy" in hits[0]


@pytest.mark.asyncio
async def test_the_stale_feed_warning_is_said_once_per_configuration(monkeypatch):
    eng = _stale_feed_engine(monkeypatch, ["ghost"])
    events = _notify_recorder(eng)
    await eng._maybe_node_startup_check()
    assert len(events) == 1
    events.clear()

    await eng._maybe_node_startup_check()
    assert events == []                      # unchanged config: not repeated

    eng.update_config(eng.cfg)               # saving is a new question
    await eng._maybe_node_startup_check()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_a_healthy_assignment_says_nothing(monkeypatch):
    eng = _stale_feed_engine(monkeypatch, ["a"])
    events = _notify_recorder(eng)
    await eng._maybe_node_startup_check()
    assert [s for s, _, _ in events if "unknown UPS assignment" in s] == []


@pytest.mark.asyncio
async def test_the_silent_failure_is_reported_on_a_backup_server_too(monkeypatch):
    """These three warnings are pure config questions and have nothing to do with node
    names — but they sat at the bottom of the node check, behind "if not hosts: return".

    An estate of Proxmox Backup Servers has no PVE target at all, so the one failure the
    appliance calls completely silent stayed completely silent there: no event until the
    next scheduled self-test, up to a day after the save that caused it."""
    from app.engine import Engine

    cfg = AppConfig(
        ups=[SnmpConfig(id="a", host="10.0.0.1")],
        hosts=[PbsHostConfig(name="backup", api_url="https://pbs:8007",
                             token_id="ups@pbs!x", token_secret="s",
                             ups_ids=["ghost"])],
    )
    eng = Engine(cfg)
    events = _notify_recorder(eng)

    await eng._maybe_node_startup_check()

    hits = [b for s, sev, b in events
            if "unknown UPS assignment" in s and sev == "critical"]
    assert hits and "NEVER" in hits[0]


@pytest.mark.asyncio
async def test_the_startup_check_yields_to_an_outage_without_latching(monkeypatch):
    eng = _startup_engine(monkeypatch, "wrong")
    eng.shutdown_triggered = True
    events = _notify_recorder(eng)

    await eng._maybe_node_startup_check()
    assert events == [] and eng._node_startup_done is False

    # Once mains are back, the next iteration still catches it.
    eng.shutdown_triggered = False
    await eng._maybe_node_startup_check()
    assert [s for s, _, _ in events if "node name does not match" in s]


@pytest.mark.asyncio
async def test_saving_the_config_re_arms_the_node_check_but_not_the_selftest(monkeypatch):
    """One GET /nodes is cheap enough to repeat on save; the credential test (up to 10 s
    per host) deliberately is not."""
    eng = _startup_engine(monkeypatch, "ok")
    await eng._maybe_node_startup_check()
    assert eng._node_startup_done is True

    slot = eng.last_selftest_slot = datetime(2026, 7, 25, 10, 0)
    eng.update_config(eng.cfg)

    assert eng._node_startup_done is False
    assert eng.last_selftest_slot == slot


@pytest.mark.asyncio
async def test_the_startup_check_is_quiet_about_a_name_it_could_not_read(monkeypatch):
    eng = _startup_engine(monkeypatch, "unverified")
    events = _notify_recorder(eng)

    await eng._maybe_node_startup_check()
    assert events == []


@pytest.mark.asyncio
async def test_node_check_is_quiet_when_the_name_matches():
    from app import proxmox

    data, message = await _node_check(
        "pve01", proxmox.NodeList(readable=True, nodes=["pve01", "pve02"]))
    assert data["match"] is True and message == ""


@pytest.mark.asyncio
async def test_health_check_never_touches_the_engine_state(monkeypatch):
    """Cluster problems are about the NEXT outage; they must not make a running
    appliance look triggered or degraded."""
    from app.engine import ONLINE

    eng = _cluster_engine(monkeypatch, flags={"noout": True}, armed_state="disarmed")
    eng._emit = _notify_recorder(eng) and eng._emit  # keep real _emit out of the db
    events = _notify_recorder(eng)
    await eng._check_clusters()

    assert eng.state == ONLINE
    assert eng.shutdown_triggered is False
    assert eng.alarm_active is False
    assert events  # ... it did warn, it just changed nothing


# --- cluster preparation (writing) -----------------------------------------
class _CephServer:
    """A cluster whose Ceph flags actually change, so verification can be exercised.

    ``bulk_supported=False`` models a release that only has the per-flag endpoint;
    ``async_delay`` models the bulk PUT being a worker task whose effect shows up late.
    """

    def __init__(self, *, bulk_supported=True, async_delay=0, armed="armed",
                 disarm_works=True, has_disarm=True, has_ceph=True, guests=None,
                 guest_ignores_shutdown=(), guest_stop_fails=(), shutdown_delay=0,
                 vm_audit=True, vm_power=True, ds_audit=True, mons=("pve01", "pve02"),
                 self_on_ceph=False, ha_services=1, nodes=("pve01", "pve02")):
        self.flags = {f: False for f in
                      ("noout", "nobackfill", "norecover", "norebalance")}
        self.bulk_supported = bulk_supported
        self.async_delay = async_delay
        self._pending = None
        self.armed = armed
        self.disarm_works = disarm_works
        self.has_disarm = has_disarm
        self.has_ceph = has_ceph
        # Guests are stateful, like the flags: "stopped" is only ever concluded from a
        # re-read, so the fake has to actually change.
        self.guests = {
            g["vmid"]: dict(g)
            for g in (list(_DEFAULT_GUESTS) if guests is None else guests)
        }
        self.guest_ignores_shutdown = set(guest_ignores_shutdown)
        self.guest_stop_fails = set(guest_stop_fails)
        self.shutdown_delay = shutdown_delay
        self._pending_stop: dict[int, int] = {}
        self.vm_audit = vm_audit
        self.vm_power = vm_power
        self.ds_audit = ds_audit
        self.mons = list(mons)
        self.nodes = list(nodes)
        self.self_on_ceph = self_on_ceph
        self.ha_services = ha_services
        self.inflight = 0
        self.max_inflight = 0
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        self.calls.append(("GET", url))
        # Enough of a cluster for _inspect_clusters() to recognise one; the flag/HA
        # endpoints below are the parts these tests actually exercise.
        if url == "/access/permissions":
            root = {"Sys.PowerMgmt": 1, "Sys.Audit": 1, "Sys.Modify": 1, "Sys.Console": 1}
            if self.vm_audit:
                root["VM.Audit"] = 1
            if self.vm_power:
                root["VM.PowerMgmt"] = 1
            if self.ds_audit:
                root["Datastore.Audit"] = 1
            return _FakeJson(200, {"data": {"/": root}})
        if url == "/cluster/resources?type=vm":
            # Filtered, not refused (see _cluster_routes).
            if not self.vm_audit:
                return _FakeJson(200, {"data": []})
            for vmid, left in list(self._pending_stop.items()):
                if left <= 0:
                    self.guests[vmid]["status"] = "stopped"
                    self._pending_stop.pop(vmid)
                else:
                    self._pending_stop[vmid] = left - 1
            return _FakeJson(200, {"data": list(self.guests.values())})
        if url == "/cluster/resources?type=storage":
            if not self.ds_audit:
                return _FakeJson(200, {"data": []})
            return _FakeJson(200, {"data": [
                {"storage": "local-lvm", "plugintype": "lvmthin"},
                {"storage": "cephpool", "plugintype": "rbd"},
            ]})
        if url.endswith("/config") and url.startswith("/nodes/"):
            store = "cephpool" if self.self_on_ceph else "local-lvm"
            return _FakeJson(200, {"data": {"rootfs": f"{store}:vm-disk-0,size=4G"}})
        if url == "/cluster/status":
            return _FakeJson(200, {"data": [
                {"type": "cluster", "name": "prod", "quorate": 1,
                 "nodes": len(self.nodes)},
            ] + [{"type": "node", "name": n, "online": 1} for n in self.nodes]})
        if url == "/cluster/options":
            return _FakeJson(200, {"data": {"ha": {"shutdown_policy": "freeze"}}})
        if url.startswith("/cluster/ceph/") and not self.has_ceph:
            # pveceph never initialised: the endpoint dies on the missing config (500),
            # which is what distinguishes "no Ceph" from "not permitted" (403).
            return _FakeJson(500, {})
        if url == "/cluster/ceph/flags":
            if self._pending is not None:
                if self.async_delay > 0:
                    self.async_delay -= 1          # still "in flight"
                else:
                    self.flags.update(self._pending)
                    self._pending = None
            return _FakeJson(200, {"data": [{"name": k, "value": v}
                                            for k, v in self.flags.items()]})
        if url == "/cluster/ceph/status":
            return _FakeJson(200, {"data": {
                "health": {"status": "HEALTH_OK"},
                "monmap": {"mons": [{"name": m} for m in self.mons]},
            }})
        if url == "/cluster/ha/status/current":
            data = [{"type": "fencing", "armed-state": self.armed}]
            data += [{"type": "service", "sid": f"vm:{100 + i}"}
                     for i in range(int(self.ha_services))]
            return _FakeJson(200, {"data": data})
        if url == "/cluster/ha/status":
            index = [{"name": "current"}]
            if self.has_disarm:
                index += [{"name": "disarm-ha"}, {"name": "arm-ha"}]
            return _FakeJson(200, {"data": index})
        return _FakeJson(404, {})

    async def put(self, url, **kw):
        self.calls.append(("PUT", url))
        data = kw.get("data", {})
        if url.startswith("/cluster/ceph/") and not self.has_ceph:
            return _FakeJson(500, {})
        if url == "/cluster/ceph/flags":
            if not self.bulk_supported:
                return _FakeJson(501, {})
            self._pending = {k: bool(int(v)) for k, v in data.items()}
            return _FakeJson(200, {"data": "UPID:pve01:..."})   # worker task, async
        if url.startswith("/cluster/ceph/flags/"):
            self.flags[url.rsplit("/", 1)[1]] = bool(int(data.get("value", 0)))
            return _FakeJson(200, {"data": None})
        return _FakeJson(404, {})

    async def post(self, url, **kw):
        self.calls.append(("POST", url))
        if "/status/shutdown" in url or "/status/stop" in url:
            return await self._guest_power(url)
        if url == "/cluster/ha/status/disarm-ha":
            if not self.disarm_works:
                return _FakeJson(500, {})
            assert kw.get("data", {}).get("resource-mode") == "ignore"
            self.armed = "disarmed"
            return _FakeJson(200, {"data": None})
        if url == "/cluster/ha/status/arm-ha":
            self.armed = "armed"
            return _FakeJson(200, {"data": None})
        return _FakeJson(404, {})

    async def _guest_power(self, url):
        """Guest shutdown/stop, with the concurrency actually observed."""
        import asyncio

        if not self.vm_power:
            return _FakeJson(403, {})
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0)
            vmid = int(url.split("/status/")[0].rsplit("/", 1)[1])
            hard = url.endswith("/status/stop")
            if vmid not in self.guests:
                return _FakeJson(404, {})
            if hard:
                if vmid in self.guest_stop_fails:
                    return _FakeJson(500, {})
                self.guests[vmid]["status"] = "stopped"
            elif vmid not in self.guest_ignores_shutdown:
                self._pending_stop[vmid] = self.shutdown_delay
            return _FakeJson(200, {"data": "UPID:pve01:..."})
        finally:
            self.inflight -= 1


@pytest.mark.asyncio
async def test_prepare_sets_ceph_flags_and_disarms_ha_with_ignore(monkeypatch):
    from app import cluster

    srv = _CephServer()
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)

    result = await cluster.prepare(_pve(), want_ceph=True, want_disarm=True, timeout=30)

    assert result.ok, result.message
    assert all(srv.flags.values())
    assert srv.armed == "disarmed"
    # resource-mode "ignore" is asserted inside the fake: under "freeze" the guests stay
    # HA-managed, pve-guests skips them and the disarmed LRM no longer stops them.
    assert ("POST", "/cluster/ha/status/disarm-ha") in srv.calls


@pytest.mark.asyncio
async def test_prepare_verifies_the_async_bulk_put_by_reading_back(monkeypatch):
    """PUT /cluster/ceph/flags only returns a worker UPID — success must come from a GET."""
    from app import cluster

    srv = _CephServer(async_delay=2)
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)

    result = await cluster.prepare(_pve(), want_ceph=True, want_disarm=False, timeout=30)

    assert result.ok and all(srv.flags.values())
    assert len([c for c in srv.calls if c == ("GET", "/cluster/ceph/flags")]) > 1


@pytest.mark.asyncio
async def test_prepare_falls_back_to_single_flags_when_the_bulk_put_is_absent(monkeypatch):
    """On releases without the bulk endpoint the per-flag path is simply the normal one."""
    from app import cluster

    srv = _CephServer(bulk_supported=False)
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)

    result = await cluster.prepare(_pve(), want_ceph=True, want_disarm=False, timeout=30)

    assert result.ok and all(srv.flags.values())
    assert ("PUT", "/cluster/ceph/flags/noout") in srv.calls


@pytest.mark.asyncio
async def test_prepare_reports_a_refused_disarm_instead_of_claiming_success(monkeypatch):
    from app import cluster

    srv = _CephServer(disarm_works=False)
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)

    result = await cluster.prepare(_pve(), want_ceph=False, want_disarm=True, timeout=30)

    assert result.ok is False
    assert "disarm" in result.message.lower()
    assert srv.armed == "armed"  # unchanged, so the LRM still stops the guests itself


class _SlowDisarmServer(_CephServer):
    """A stack that passes through 'disarming' first, the way a real one does.

    Every LRM has to release its watchdog, in rounds of ten seconds — which is why the
    verification cannot live on a fixed few seconds.
    """

    def __init__(self, *, rounds, **kw):
        super().__init__(**kw)
        self._rounds = rounds

    async def get(self, url, **kw):
        if url == "/cluster/ha/status/current" and self.armed == "disarming":
            if self._rounds > 0:
                self._rounds -= 1
            else:
                self.armed = "disarmed"
        return await super().get(url, **kw)

    async def post(self, url, **kw):
        if url == "/cluster/ha/status/disarm-ha":
            self.calls.append(("POST", url))
            self.armed = "disarming"
            return _FakeJson(200, {"data": None})
        return await super().post(url, **kw)


@pytest.mark.asyncio
async def test_the_disarm_waits_out_the_configured_budget(monkeypatch):
    """The regression this guards: the wait was a module constant (15 s), so raising the
    configured timeout changed nothing — the shutdown ran on regardless while the stack
    was still disarming, which is the one state in which nobody stops the guests."""
    import asyncio

    from app import cluster

    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)

    async def run(timeout):
        srv = _SlowDisarmServer(rounds=10 ** 6)   # never gets there
        monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
        started = asyncio.get_running_loop().time()
        result = await cluster.prepare(
            _pve(), want_ceph=False, want_disarm=True, timeout=timeout)
        return result, asyncio.get_running_loop().time() - started

    short, waited_short = await run(2)
    long, waited_long = await run(5)

    assert short.ok is False and long.ok is False
    # Named, not just "not in time": still disarming means "give it more budget", while
    # a stack that never moved is a different problem entirely.
    assert "stopped at 'disarming'" in short.message
    # Each run spends its own budget, so the longer one waits measurably longer.
    assert waited_short >= 0.5 and waited_short < 3
    assert waited_long > waited_short + 1.5


@pytest.mark.asyncio
async def test_a_slow_disarm_is_confirmed_once_it_arrives(monkeypatch):
    from app import cluster

    srv = _SlowDisarmServer(rounds=25)
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)

    result = await cluster.prepare(
        _pve(), want_ceph=False, want_disarm=True, timeout=10)

    assert result.ok and srv.armed == "disarmed"
    assert "HA disarmed" in result.message


@pytest.mark.asyncio
async def test_an_already_disarmed_stack_is_not_disarmed_again(monkeypatch):
    """A cluster stays prepared until someone restores it, so a second outage arrives at
    a disarmed stack. Reading before writing keeps that out of the write path."""
    from app import cluster

    srv = _CephServer(armed="disarmed")
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)

    result = await cluster.prepare(
        _pve(), want_ceph=False, want_disarm=True, timeout=5)

    assert result.ok and "already disarmed" in result.message
    assert ("POST", "/cluster/ha/status/disarm-ha") not in srv.calls


@pytest.mark.asyncio
async def test_prepare_never_exceeds_its_budget(monkeypatch):
    """It runs inside the poll loop while the battery drains — the ceiling is hard."""
    import asyncio

    from app import cluster

    class _Hanging(_CephServer):
        async def get(self, url, **kw):
            await asyncio.sleep(30)

    monkeypatch.setattr(cluster.httpx, "AsyncClient", _Hanging())
    result = await cluster.prepare(_pve(), want_ceph=True, want_disarm=True, timeout=0.05)

    assert result.ok is False and "gave up" in result.message


@pytest.mark.asyncio
async def test_restore_arms_ha_and_clears_the_flags(monkeypatch):
    from app import cluster

    srv = _CephServer(armed="disarmed")
    srv.flags.update({f: True for f in srv.flags})
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)

    result = await cluster.restore(_pve())

    assert result.ok, result.message
    assert srv.armed == "armed"
    assert not any(srv.flags.values())


@pytest.mark.asyncio
async def test_restore_does_nothing_on_a_healthy_cluster(monkeypatch):
    from app import cluster

    srv = _CephServer()  # armed, flags clear
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)

    result = await cluster.restore(_pve())

    assert result.ok and "nothing to restore" in result.message
    assert not [c for c in srv.calls if c[0] in ("PUT", "POST")]


def _outage_cluster_engine(monkeypatch, srv, *, dry_run=False, abort=False, nodes=2,
                           self_vmid=950):
    """Engine with a triggered UPS feeding N cluster nodes, talking to ``srv``."""
    from app import cluster
    from app.engine import Engine
    from app.config import ApplianceConfig, Thresholds

    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)
    monkeypatch.setattr(cluster, "_GUEST_POLL_INTERVAL_S", 0.01)
    # cluster_ceph is pinned rather than defaulted: it ships OFF (Ceph is the exception,
    # not the rule), and these tests are about the Ceph path. It is also the ONLY switch
    # for the cluster-wide guest stop -- that step has none of its own.
    hosts = [PveHostConfig(name=f"pve0{i}", api_url="https://pve:8006",
                           token_id="ups@pve!x", token_secret="s",
                           cluster=True, cluster_ceph=True, ups_ids=["u"], order=i)
             for i in range(1, nodes + 1)]
    cfg = AppConfig(
        dry_run=dry_run,
        ups=[SnmpConfig(id="u", host="10.0.0.9")],
        hosts=hosts,
        # The appliance's own container, picked the way the UI picks it. Without this the
        # guest stop refuses outright, which is its own (separately tested) behaviour.
        appliance=ApplianceConfig(self_vmid=self_vmid, self_node="pve01"),
        thresholds=Thresholds(on_battery_low=True, on_battery_seconds=None,
                              runtime_below_minutes=None, charge_below_percent=None,
                              cluster_prep_timeout_s=5,
                              cluster_guest_shutdown_timeout_s=5,
                              cluster_abort_on_prep_failure=abort),
    )
    eng = Engine(cfg)
    # Pretend the self-test already discovered the cluster.
    for h in hosts:
        eng.host_states[h.key] = {"cluster_name": "prod"}
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_status="low")
    return eng


def _noop_fire(eng):
    """Latch the host the way _fire_host would, without shutting anything down."""

    async def fake_fire(host, reason):
        eng.host_fired[host.key] = True
        eng.shutdown_triggered = True

    return fake_fire


@pytest.mark.asyncio
async def test_cluster_is_prepared_once_before_any_node_goes_down(monkeypatch):
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv, nodes=3)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert fired == ["pve01", "pve02", "pve03"]
    # Exactly one preparation for the whole cluster, not one per node.
    assert len([c for c in srv.calls if c == ("POST", "/cluster/ha/status/disarm-ha")]) == 1
    assert all(srv.flags.values()) and srv.armed == "disarmed"

    # A second poll must not prepare again.
    before = len(srv.calls)
    await eng._evaluate()
    assert [c for c in srv.calls[before:] if c[0] in ("PUT", "POST")] == []


@pytest.mark.asyncio
async def test_the_preparation_says_that_it_started(monkeypatch):
    """It is the one step that deliberately delays the shutdown. Without a line before
    the work, the event log jumped from "power outage" straight to a result a minute
    later, and nothing said the nodes were waiting on purpose."""
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv)
    events = _notify_recorder(eng)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    subjects = [s for s, _, _ in events]
    assert "Cluster prod: preparing for shutdown" in subjects
    assert subjects.index("Cluster prod: preparing for shutdown") < subjects.index(
        "Cluster prod: preparation done")
    body = next(b for s, _, b in events if s.endswith("preparing for shutdown"))
    # Both budgets, added up: the preparation now holds the nodes for the HA disarm AND
    # the cluster-wide guest stop, and quoting only the first would understate the wait
    # by exactly the step that takes the longest.
    assert "wait up to 10s" in body
    # Says WHICH guests, and which one it will not touch.
    assert "guest shutdown: yes (2 guests, sparing CT 950 'pve-usv' on pve01)" in body

    # Once per cluster and episode, like the preparation itself.
    before = len(events)
    await eng._evaluate()
    assert not [s for s, _, _ in events[before:] if "preparing" in s]


@pytest.mark.asyncio
async def test_dry_run_logs_the_preparation_but_changes_nothing(monkeypatch):
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv, dry_run=True)
    events = _notify_recorder(eng)

    await eng._evaluate()

    assert any("would be prepared" in s for s, _, _ in events)
    assert not any(srv.flags.values())
    assert srv.armed == "armed"
    assert not [c for c in srv.calls if c[0] in ("PUT", "POST")]


@pytest.mark.asyncio
async def test_failed_preparation_still_shuts_the_nodes_down_by_default(monkeypatch):
    """The default is deliberate: a failed disarm leaves the LRM armed, and an armed LRM
    still stops the guests. Aborting would mean losing power uncontrolled instead."""
    srv = _CephServer(disarm_works=False)
    eng = _outage_cluster_engine(monkeypatch, srv, abort=False)
    events = _notify_recorder(eng)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert fired == ["pve01", "pve02"], "the shutdown must go ahead"
    failed = [(s, sev) for s, sev, _ in events if "preparation FAILED" in s]
    assert failed and failed[0][1] == "critical"


@pytest.mark.asyncio
async def test_abort_on_prep_failure_holds_the_cluster_back(monkeypatch):
    srv = _CephServer(disarm_works=False)
    eng = _outage_cluster_engine(monkeypatch, srv, abort=True)
    events = _notify_recorder(eng)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert fired == [], "opting in to abort must stop every node of that cluster"
    assert any("shutdown aborted" in s for s, _, _ in events)


@pytest.mark.asyncio
async def test_the_guest_budget_starts_when_the_guests_do_not_when_prepare_does(monkeypatch):
    """The HA disarm used to spend the guests' time before they got any.

    Both deadlines were taken at prepare() entry, so a disarm that legitimately ran for
    tens of seconds came straight off the guest budget — and a disarm longer than the
    guest budget left a grace of zero, which force-stops every guest at once instead of
    ever asking one to shut down.
    """
    import asyncio as _asyncio
    from app import cluster as cluster_mod

    seen: dict[str, float] = {}
    real_stop = cluster_mod._shutdown_guests

    async def spy(client, targets, steps, *, deadline, force_after_s):
        seen["left"] = deadline - _asyncio.get_running_loop().time()
        return await real_stop(client, targets, steps, deadline=deadline,
                               force_after_s=force_after_s)

    # A disarm that is armed on the first read and only reaches "disarmed" after a while,
    # which is what a real CRM/LRM round trip looks like.
    reads = {"n": 0}

    async def slow_armed_state(client):
        reads["n"] += 1
        if reads["n"] == 1:
            return cluster_mod.ARMED
        await _asyncio.sleep(1.2)
        return cluster_mod.DISARMED

    monkeypatch.setattr(cluster_mod, "_shutdown_guests", spy)
    monkeypatch.setattr(cluster_mod, "_read_armed_state", slow_armed_state)
    eng = _outage_cluster_engine(monkeypatch, _CephServer(), nodes=1)
    _noop_fire(eng)
    await eng._evaluate()

    guest_budget = eng.cfg.thresholds.cluster_guest_shutdown_timeout_s
    assert "left" in seen, "the guest step must have run"
    # Nearly the whole configured budget — not the budget minus the disarm.
    assert seen["left"] > guest_budget * 0.85


@pytest.mark.asyncio
async def test_an_unreachable_cluster_is_reported_and_not_silently_let_through(monkeypatch):
    """The abort opt-in used to be defeated by silence.

    A failed inspection walked away with `continue`: no event, no cluster_prep_failed
    entry, and no cluster_name for the abort filter to key on — so the node was powered
    off with HA armed and no flags, and the log said nothing about it.
    """
    from app import cluster as cluster_mod

    async def unreachable(host, *a, **k):
        return cluster_mod.ClusterInfo(reachable=False, error="connection refused")

    monkeypatch.setattr(cluster_mod, "inspect", unreachable)
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv, abort=True, nodes=1)
    eng.host_states.clear()                    # nothing discovered yet: cold start
    events = _notify_recorder(eng)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert [s for s, sev, _ in events
            if "not reachable for preparation" in s and sev == "critical"]
    assert fired == [], "abort-on-failure must hold a cluster nobody could inspect"


@pytest.mark.asyncio
async def test_an_unreachable_cluster_is_reported_once_not_every_poll(monkeypatch):
    """The sibling of the held-back-cluster latch, and this branch had been left out of it.

    With abort-on-failure the host is filtered out, so it never fires and stays eligible —
    and it came back through _prepare_clusters() on every poll: eight seconds apart on
    battery, each round paying for a full inspection and sending another CRITICAL, while
    the battery drained."""
    from app import cluster as cluster_mod
    inspections: list[str] = []

    async def unreachable(host, *a, **k):
        inspections.append(host.name)
        return cluster_mod.ClusterInfo(reachable=False, error="connection refused")

    monkeypatch.setattr(cluster_mod, "inspect", unreachable)
    eng = _outage_cluster_engine(monkeypatch, _CephServer(), abort=True, nodes=1)
    eng.host_states.clear()
    events = _notify_recorder(eng)
    _noop_fire(eng)

    for _ in range(5):
        await eng._evaluate()

    assert len(inspections) == 1, "one inspection per episode, not one per poll"
    assert len([s for s, _, _ in events if "not reachable for preparation" in s]) == 1

    # Mains back: the episode ends, the latch is released, and a NEW outage inspects again.
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     runtime_remaining_min=60)
    await eng._evaluate()
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery",
                                     battery_status="low")
    await eng._evaluate()
    assert len(inspections) == 2


@pytest.mark.asyncio
async def test_an_unreachable_cluster_still_shuts_down_when_abort_is_off(monkeypatch):
    """Default stays: unprepared is worse than uncontrolled, so the node still goes."""
    from app import cluster as cluster_mod

    async def unreachable(host, *a, **k):
        return cluster_mod.ClusterInfo(reachable=False, error="connection refused")

    monkeypatch.setattr(cluster_mod, "inspect", unreachable)
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv, abort=False, nodes=1)
    eng.host_states.clear()
    events = _notify_recorder(eng)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert fired == ["pve01"]
    assert any("not reachable for preparation" in s for s, _, _ in events)


@pytest.mark.asyncio
async def test_a_held_back_cluster_is_not_prepared_again_every_poll(monkeypatch):
    """The abort must cost one preparation and one pair of events, not one per poll.

    A blocked cluster fires no host, so nothing is latched — which used to read as
    "the episode is over" at the end of _evaluate_hosts() and cleared the preparation
    latch. The whole failing sequence then ran again on the very next iteration: in an
    outage that is every 8 s, each round writing to the cluster again and sending two
    CRITICAL notifications.
    """
    srv = _CephServer(disarm_works=False)
    eng = _outage_cluster_engine(monkeypatch, srv, abort=True)
    events = _notify_recorder(eng)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)

    eng._fire_host = fake_fire  # type: ignore[assignment]
    for _ in range(3):
        await eng._evaluate()

    assert fired == [], "the abort has to hold on every poll, not only the first"
    assert len([c for c in srv.calls if c == ("POST", "/cluster/ha/status/disarm-ha")]) == 1
    assert len([s for s, _, _ in events if "preparation FAILED" in s]) == 1
    assert len([s for s, _, _ in events if "shutdown aborted" in s]) == 1


@pytest.mark.asyncio
async def test_the_abort_survives_the_poll_that_no_longer_prepares(monkeypatch):
    """Whether the nodes may go down comes from the latched OUTCOME, not from this round.

    Deriving it from the work of a single iteration meant the filter was empty as soon as
    the preparation was latched as done — so the second poll shut the cluster down anyway,
    silently undoing the opt-in.
    """
    srv = _CephServer(disarm_works=False)
    eng = _outage_cluster_engine(monkeypatch, srv, abort=True)
    _notify_recorder(eng)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()
    assert eng.cluster_prepared.get("prod") is True
    assert eng.cluster_prep_failed.get("prod") is True

    await eng._evaluate()
    assert fired == []


@pytest.mark.asyncio
async def test_mains_return_clears_the_failed_preparation_too(monkeypatch):
    """Both latches belong to the episode: the next outage prepares, and may proceed."""
    srv = _CephServer(disarm_works=False)
    eng = _outage_cluster_engine(monkeypatch, srv, abort=True)
    _notify_recorder(eng)

    async def fake_fire(host, reason):
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()
    assert eng.cluster_prep_failed.get("prod") is True

    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     battery_status="normal")
    await eng._evaluate()
    assert eng.cluster_prepared == {} and eng.cluster_prep_failed == {}

    eng.reset()
    assert eng.cluster_prep_failed == {}


@pytest.mark.asyncio
async def test_a_cluster_without_ceph_is_not_asked_to_set_ceph_flags(monkeypatch):
    """The step is skipped, not attempted and failed.

    Attempting it produced a CRITICAL "preparation FAILED" on every single outage for a
    component that is simply not installed — and, with abort-on-failure, held every node
    of the cluster back over it.
    """
    srv = _CephServer(has_ceph=False)
    eng = _outage_cluster_engine(monkeypatch, srv, abort=True)
    events = _notify_recorder(eng)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert not [c for c in srv.calls if c[0] == "PUT"], "no writes at a cluster without Ceph"
    assert srv.armed == "disarmed", "the HA disarm is unaffected and still runs"
    assert fired == ["pve01", "pve02"], "abort-on-failure must not trigger on this"
    assert not [s for s, sev, _ in events if sev == "critical"]


@pytest.mark.asyncio
async def test_nothing_to_prepare_is_logged_quietly_not_as_a_failure(monkeypatch):
    """Both features on, neither available: that is a note, not a failed shutdown."""
    srv = _CephServer(has_ceph=False, has_disarm=False)
    eng = _outage_cluster_engine(monkeypatch, srv)
    events = _notify_recorder(eng)
    logged: list[tuple[str, str]] = []
    eng._log_quiet = lambda s, b, sev: logged.append((s, b))  # type: ignore[assignment]

    async def fake_fire(host, reason):
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert any("nothing to prepare" in s for s, _ in logged)
    # The body says WHY each step was dropped, so the tick can be corrected.
    body = " ".join(b for _, b in logged)
    assert "no Ceph" in body and "PVE 9.2" in body
    assert not [c for c in srv.calls if c[0] in ("PUT", "POST")]
    assert not [s for s, _, _ in events if "preparation" in s]


@pytest.mark.asyncio
async def test_cold_start_uses_the_inspection_it_just_made(monkeypatch):
    """Without a self-test yet, the feature detection must come from the fresh read.

    Re-fetching from cluster_states (empty at this point) and defaulting to "supported"
    meant a blind disarm-ha POST on PVE 8.x — a CRITICAL mid-outage, and with
    abort-on-failure a cluster that stayed up over a missing endpoint.
    """
    srv = _CephServer(has_disarm=False)
    eng = _outage_cluster_engine(monkeypatch, srv, abort=True)
    eng.host_states = {}          # no self-test has run
    eng.cluster_states = {}
    events = _notify_recorder(eng)
    fired: list[str] = []

    async def fake_fire(host, reason):
        fired.append(host.name)
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert ("POST", "/cluster/ha/status/disarm-ha") not in srv.calls
    assert all(srv.flags.values()), "the Ceph flags are not a 9.2 feature"
    assert fired == ["pve01", "pve02"]
    # Exactly one CRITICAL, and it is the honest one: HA manages guests here and cannot
    # be disarmed, so stopping them would only feed the HA manager. Saying so beats
    # doing it, and the nodes still go down the way they did before 4.0.
    assert [s for s, sev, _ in events if sev == "critical"] == [
        "Cluster prod: guest shutdown skipped"
    ]
    assert not any("/status/shutdown" in url for _m, url in srv.calls)
    # The reading is kept, so the next cluster of this episode does not re-probe.
    assert "prod" in eng.cluster_states


@pytest.mark.asyncio
async def test_a_cold_start_inspects_a_cluster_once_not_once_per_node(monkeypatch):
    """The other half of the cold start, and it is battery time.

    Only _inspect_clusters() writes host_states[...]["cluster_name"], and that one stands
    down for the whole duration of an outage. So every due node used to fall through to its
    own cluster.inspect() — three nodes, three sequential reads of up to the full inspect
    budget each, all of it before the first node was even asked to shut down.
    """
    from app import cluster

    srv = _CephServer(nodes=("pve01", "pve02", "pve03"), mons=("pve01",))
    eng = _outage_cluster_engine(monkeypatch, srv, nodes=3)
    eng.host_states = {}          # no self-test has run: nothing knows the cluster name
    eng.cluster_states = {}
    _notify_recorder(eng)

    real_inspect = cluster.inspect
    seen: list[str] = []

    async def counting_inspect(host, *a, **kw):
        seen.append(host.name)
        return await real_inspect(host, *a, **kw)

    monkeypatch.setattr(cluster, "inspect", counting_inspect)
    _noop_fire(eng)
    await eng._evaluate()

    assert seen == ["pve01"], f"one inspection per cluster, not per node: {seen}"
    # And the membership it established is on every node, which is what the abort filter
    # and the next poll both key on.
    assert [eng.host_states[h.key].get("cluster_name") for h in eng.cfg.hosts]         == ["prod", "prod", "prod"]


@pytest.mark.asyncio
async def test_an_unreachable_cluster_is_read_once_over_not_once_after_another(monkeypatch):
    """The other end of the cold start: a cluster that names nobody.

    One reading answers for the whole cluster because it brings back the node list and
    every member is stamped from it. An unreachable API brings back nothing, so each
    remaining node still needs its own read — correctly, its API may be the one that works
    — but they used to happen one after another, each up to inspect()'s full budget plus
    the grace. Three nodes were close to a minute, spent before the first machine was
    asked to shut down, cluster member or not.

    Asserted by construction rather than by clock: every read after the first blocks until
    all of them have started. Sequentially that never resolves.
    """
    import asyncio as _asyncio

    from app import cluster

    srv = _CephServer(nodes=("pve01", "pve02", "pve03"))
    eng = _outage_cluster_engine(monkeypatch, srv, nodes=3)
    eng.host_states = {}          # cold start: nothing knows the cluster name
    eng.cluster_states = {}
    _notify_recorder(eng)

    started = _asyncio.Event()
    seen: list[str] = []

    async def unreachable_inspect(host, *a, **kw):
        seen.append(host.name)
        if len(seen) >= 3:
            started.set()
        if len(seen) > 1:
            # Only the concurrent ones wait for each other; the first is read alone by
            # design, and waiting on it would deadlock whatever the fix.
            await _asyncio.wait_for(started.wait(), timeout=2)
        return cluster.ClusterInfo(reachable=False, error="connection refused")

    monkeypatch.setattr(cluster, "inspect", unreachable_inspect)
    _noop_fire(eng)
    await eng._evaluate()

    assert sorted(seen) == ["pve01", "pve02", "pve03"]
    # Every one of them is latched, so the next poll costs nothing at all and the CRITICAL
    # is written once per node per episode rather than every eight seconds.
    assert eng.cluster_inspect_failed == {h.key for h in eng.cfg.hosts}


@pytest.mark.asyncio
async def test_disarm_is_skipped_where_the_endpoint_does_not_exist(monkeypatch):
    """No blind POST that comes back 501 in the middle of an outage — but the Ceph flags
    are not a 9.2 feature and must still be set."""
    from app import cluster

    srv = _CephServer(has_disarm=False)
    eng = _outage_cluster_engine(monkeypatch, srv)
    # ha_index_read: we DID read the index and it had no disarm-ha — that is what makes
    # the absence settled rather than merely unknown.
    eng.cluster_states = {"prod": cluster.ClusterInfo(
        reachable=True, is_cluster=True, name="prod", ceph_configured=True,
        ha_index_read=True, disarm_supported=False)}

    async def fake_fire(host, reason):
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert ("POST", "/cluster/ha/status/disarm-ha") not in srv.calls
    assert all(srv.flags.values()), "Ceph flags still get set on older releases"


@pytest.mark.asyncio
async def test_preparation_latch_is_cleared_when_mains_return(monkeypatch):
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv)

    async def fake_fire(host, reason):
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()
    assert eng.cluster_prepared.get("prod") is True

    # Mains back: the latch clears, so the next outage prepares afresh.
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     battery_status="normal")
    eng.host_fired = {}
    await eng._evaluate()
    assert eng.cluster_prepared == {}

    eng.reset()
    assert eng.cluster_prepared == {}


# --- host connection test: cluster privileges (follow-up to 4.0.0) -----------
def _fake_host_and_cluster(monkeypatch, **kw):
    """Patch BOTH clients the host test now uses: proxmox (credentials) and cluster.

    Returns the shared call log, so a test can assert that the cluster endpoints were
    (or were not) queried at all.
    """
    from app import cluster, proxmox

    perms = kw.pop("permissions", {"Sys.PowerMgmt": 1, "Sys.Audit": 1,
                                   "Sys.Modify": 1, "Sys.Console": 1,
                                   "VM.Audit": 1, "VM.PowerMgmt": 1,
                                   "Datastore.Audit": 1})
    routes = _cluster_routes(permissions=perms, **kw)
    routes["/version"] = _FakeJson(200, {"data": {"version": "9.2.1"}})
    calls: list = []
    client = _FakeApiClient(routes, calls)
    monkeypatch.setattr(proxmox.httpx, "AsyncClient", client)
    monkeypatch.setattr(cluster.httpx, "AsyncClient", client)
    return calls


def _host_payload(**kw):
    return {"name": "pve01", "type": "pve", "api_url": "https://pve:8006",
            "method": "api_token", "token_id": "ups@pve!x", "token_secret": "s", **kw}


@pytest.mark.asyncio
async def test_host_test_skips_the_cluster_checks_when_not_a_member(_import_target, monkeypatch):
    """Without the cluster switch, no cluster FEATURE may be queried — otherwise every host
    test would cost six additional API calls for nothing."""
    main, _ = _import_target
    calls = _fake_host_and_cluster(monkeypatch)

    result = await main.api_test_host(_host_payload(cluster=False))

    assert result["ok"] is True and result["cluster"] is None
    # /cluster/status is the node listing (it marks the node that answered, which is
    # what tells a wrong name from a proxied one) — the feature endpoints stay untouched.
    assert not [
        c for c in calls
        if c[0] == "GET" and c[1].startswith("/cluster/") and c[1] != "/cluster/status"
    ]


@pytest.mark.asyncio
async def test_host_test_reports_the_cluster_it_found(_import_target, monkeypatch):
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch)

    result = await main.api_test_host(_host_payload(cluster=True))

    assert result["ok"] is True
    assert result["cluster"]["missing_privileges"] == []
    assert result["cluster"]["name"] == "prod" and result["cluster"]["nodes"] == 3
    # The plan's own acceptance criterion: name, node count, Ceph and HA must be visible.
    assert "prod" in result["message"] and "3 nodes" in result["message"]
    assert "Ceph detected" in result["message"]
    assert "HA armed (1 HA guests)" in result["message"]


@pytest.mark.asyncio
async def test_host_test_names_every_missing_cluster_privilege(_import_target, monkeypatch):
    """"403" tells an operator nothing; the privilege name points at the pveum command."""
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch,
                           permissions={"Sys.PowerMgmt": 1, "Sys.Audit": 1})

    result = await main.api_test_host(_host_payload(cluster=True, cluster_ceph=True))

    # The Ceph tick also buys the cluster-wide guest stop, so its two privileges are
    # demanded by the same tick and named in the same list.
    assert result["cluster"]["missing_privileges"] == [
        "Sys.Modify (Ceph maintenance flags)", "Sys.Console (HA disarm)",
        "VM.Audit (list the cluster's guests)",
        "VM.PowerMgmt (stop the guests before the shutdown)"]
    assert "Sys.Modify" in result["message"] and "Sys.Console" in result["message"]
    assert "manual" in result["message"]
    # The connection itself works, so this stays a warning — same convention as the
    # unconfirmed Sys.PowerMgmt message from proxmox.test_connection().
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_host_test_does_not_demand_sys_console_for_ceph_only(_import_target, monkeypatch):
    """Whoever leaves HA disarm off must not be told to hand out shell access."""
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch,
                           permissions={"Sys.PowerMgmt": 1, "Sys.Audit": 1, "Sys.Modify": 1,
                                        "VM.Audit": 1, "VM.PowerMgmt": 1})

    result = await main.api_test_host(
        _host_payload(cluster=True, cluster_ceph=True, cluster_ha_disarm=False))

    assert result["cluster"]["missing_privileges"] == []
    assert "Sys.Console" not in result["message"]
    # Datastore.Audit only buys a better diagnosis, so it never appears as "missing".
    assert result["cluster"]["advisory_privileges"] == [
        "Datastore.Audit (detect Ceph-backed storage)"]


@pytest.mark.asyncio
async def test_host_test_flags_the_switch_on_a_standalone_node(_import_target, monkeypatch):
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, is_cluster=False)

    result = await main.api_test_host(_host_payload(cluster=True))

    assert result["cluster"]["is_cluster"] is False
    assert "not part of a cluster" in result["message"]
    assert result["ok"] is True  # a misconfiguration, not a broken connection


@pytest.mark.asyncio
async def test_host_test_points_out_a_missing_disarm_endpoint(_import_target, monkeypatch):
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, has_disarm=False)

    result = await main.api_test_host(_host_payload(cluster=True))

    assert result["cluster"]["disarm_supported"] is False
    assert "9.2" in result["message"]
    assert "Ceph flags still work" in result["message"]


@pytest.mark.asyncio
async def test_host_test_reports_a_missing_quorum(_import_target, monkeypatch):
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, quorate=0)

    result = await main.api_test_host(_host_payload(cluster=True))

    assert result["cluster"]["quorate"] is False
    assert "no quorum" in result["message"]


@pytest.mark.asyncio
async def test_host_test_degrades_when_the_cluster_does_not_answer(_import_target, monkeypatch):
    """An unreachable cluster must produce a readable message, not an exception.

    cluster.inspect() is already proven bounded elsewhere; what matters here is that the
    endpoint turns its failure result into something the user can act on.
    """
    from app import cluster, proxmox

    main, _ = _import_target
    routes = {"/version": _FakeJson(200, {"data": {"version": "9.2.1"}}),
              "/access/permissions": _FakeJson(200, {"data": {"/": {"Sys.PowerMgmt": 1}}})}
    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _FakeApiClient(routes, []))

    async def unreachable(host, timeout=8.0, **kw):
        return cluster.ClusterInfo(reachable=False, error="No response within 13s — gave up")

    monkeypatch.setattr(cluster, "inspect", unreachable)

    result = await main.api_test_host(_host_payload(cluster=True))

    assert "Cluster check failed" in result["message"]
    assert "gave up" in result["message"]
    assert result["cluster"]["reachable"] is False
    # The credential test itself succeeded, so the host is not reported as broken.
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_host_test_does_not_chase_the_cluster_on_an_unreachable_host(
    _import_target, monkeypatch
):
    """An API that cannot be reached cannot answer the cluster questions either.

    Running the inspection anyway would add its own timeout to an already failed test
    (~23 s in total against a dead address) and append a second, redundant error.
    """
    from app import cluster, proxmox

    main, _ = _import_target

    class _Dead:
        def __call__(self, *a, **kw):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            raise OSError("connection refused")

    monkeypatch.setattr(proxmox.httpx, "AsyncClient", _Dead())
    inspected: list = []

    async def record(host, timeout=8.0):
        inspected.append(host.name)
        return cluster.ClusterInfo(reachable=False)

    monkeypatch.setattr(cluster, "inspect", record)

    result = await main.api_test_host(_host_payload(cluster=True))

    assert result["ok"] is False
    assert inspected == [], "the cluster must not be probed after a failed connection"
    assert result["cluster"] is None
    assert "Cluster check failed" not in result["message"]


# --- on-demand self-test + visible "checked and fine" ------------------------
@pytest.mark.asyncio
async def test_healthy_cluster_says_so_instead_of_staying_silent(monkeypatch):
    """Warnings alone leave "checked and fine" indistinguishable from "never checked" —
    which is exactly how a working self-test looked like a broken one."""
    eng = _cluster_engine(monkeypatch, all_nodes=True, shutdown_policy="freeze")
    notified = _notify_recorder(eng)
    quiet: list[tuple[str, str]] = []
    eng._log_quiet = lambda s, b, sev: quiet.append((s, b))  # type: ignore[assignment]

    await eng._check_clusters(log_ok=True)

    assert notified == [], "a healthy cluster still must not raise a warning"
    assert [s for s, _ in quiet] == ["Cluster prod: ok"]
    body = quiet[0][1]
    assert "3 nodes" in body and "quorum ok" in body and "armed" in body


@pytest.mark.asyncio
async def test_a_cluster_with_findings_gets_no_ok_line(monkeypatch):
    eng = _cluster_engine(monkeypatch, all_nodes=True, armed_state="disarmed",
                          shutdown_policy="freeze")
    _notify_recorder(eng)
    quiet: list[str] = []
    eng._log_quiet = lambda s, b, sev: quiet.append(s)  # type: ignore[assignment]

    await eng._check_clusters(log_ok=True)

    assert not [s for s in quiet if s.endswith(": ok")]


@pytest.mark.asyncio
async def test_cluster_checks_do_nothing_without_the_cluster_flag(monkeypatch):
    """The gate that made the self-test look broken: no flag, no cluster inspection.

    Worth pinning down — it is correct (no unasked API calls), but it means the checks
    are invisible until the host is actually marked as a cluster member.
    """
    from app import cluster
    from app.engine import Engine

    calls: list = []
    monkeypatch.setattr(cluster.httpx, "AsyncClient",
                        _FakeApiClient(_cluster_routes(), calls))
    eng = Engine(AppConfig(hosts=[PveHostConfig(
        name="pve01", api_url="https://pve:8006", token_id="ups@pve!x",
        token_secret="s", cluster=False)]))
    events = _notify_recorder(eng)

    await eng._check_clusters(log_ok=True)

    assert events == [] and calls == []
    assert eng.cluster_snapshot() == []


@pytest.mark.asyncio
async def test_manual_selftest_runs_immediately_and_always_logs(monkeypatch):
    """Saving settings deliberately does not fire a self-test, so the button must."""
    from app import cluster, targets
    from app.engine import Engine
    from app.proxmox import TestResult

    monkeypatch.setattr(cluster.httpx, "AsyncClient",
                        _FakeApiClient(_cluster_routes(), []))

    async def ok_test(host, timeout=10.0):
        return TestResult(True, "Connection and 'Sys.PowerMgmt' privilege ok.",
                          has_power_mgmt=True)

    monkeypatch.setattr(targets, "test_connection", ok_test)
    eng = Engine(AppConfig(hosts=[PveHostConfig(
        name="pve01", api_url="https://pve:8006", token_id="ups@pve!x",
        token_secret="s", cluster=True)]))
    quiet: list[str] = []
    eng._log_quiet = lambda s, b, sev: quiet.append(s)  # type: ignore[assignment]
    _notify_recorder(eng)

    ok, message = await eng.run_selftest_now()
    assert ok and "1 of 1 hosts ok" in message and "1 cluster(s) checked" in message
    assert "Self-test pve01: ok" in quiet

    # Run again straight away: the daily throttle must NOT swallow an explicit request.
    quiet.clear()
    await eng.run_selftest_now()
    assert "Self-test pve01: ok" in quiet


@pytest.mark.asyncio
async def test_manual_selftest_is_refused_during_an_outage(monkeypatch):
    """Every host costs up to 10 s; the battery countdown has priority."""
    from app.engine import Engine

    eng = Engine(AppConfig(
        ups=[SnmpConfig(id="u", host="10.0.0.9")],
        hosts=[PveHostConfig(name="pve01", api_url="x", token_secret="s")]))
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")

    ok, message = await eng.run_selftest_now()

    assert ok is False and "power outage" in message


@pytest.mark.asyncio
async def test_selftest_endpoint_reports_the_result(_import_target, monkeypatch):
    main, _ = _import_target

    async def fake(self=None):
        return True, "2 of 2 hosts ok."

    monkeypatch.setattr(main.engine, "run_selftest_now", fake)
    assert await main.api_selftest_run() == {"ok": True, "message": "2 of 2 hosts ok."}


# --- a disarmed HA stack without HA guests (reported from real hardware) -----
@pytest.mark.asyncio
async def test_disarmed_ha_is_reported_even_without_ha_guests(monkeypatch):
    """The bug this fixes: a 2-node cluster with HA disarmed but no HA resources.

    "the stack is disarmed" and "HA manages guests" are independent — tying the warning
    to the resource count silently swallowed a cluster-wide loss of fencing.
    """
    eng = _cluster_engine(monkeypatch, all_nodes=True, armed_state="disarmed",
                          ha_services=0, shutdown_policy="freeze")
    events = _notify_recorder(eng)

    await eng._check_clusters(log_ok=True)

    assert [s for s, _, _ in events if "HA is still disarmed" in s]
    snap = eng.cluster_snapshot()[0]
    assert snap["needs_recovery"] is True, "'Restore cluster' has to be offered"
    assert snap["ha_services"] == 0 and snap["ha_armed_state"] == "disarmed"


@pytest.mark.asyncio
async def test_armed_cluster_without_ha_guests_stays_quiet(monkeypatch):
    """The counterpart: nothing disarmed, nothing to complain about."""
    eng = _cluster_engine(monkeypatch, all_nodes=True, ha_services=0,
                          shutdown_policy="freeze")
    events = _notify_recorder(eng)
    quiet: list[tuple[str, str]] = []
    eng._log_quiet = lambda s, b, sev: quiet.append((s, b))  # type: ignore[assignment]

    await eng._check_clusters(log_ok=True)

    assert events == []
    assert quiet and "HA armed (0 HA guests)" in quiet[0][1]


@pytest.mark.asyncio
async def test_shutdown_policy_warning_stays_tied_to_ha_resources(monkeypatch):
    """Without HA guests there is nothing that could be recovered onto a dying node."""
    eng = _cluster_engine(monkeypatch, all_nodes=True, ha_services=0, has_disarm=False,
                          shutdown_policy="migrate")
    events = _notify_recorder(eng)
    await eng._check_clusters()
    assert not [s for s, _, _ in events if "shutdown_policy" in s]

    # With HA guests the warning is due again.
    eng = _cluster_engine(monkeypatch, all_nodes=True, ha_services=2, has_disarm=False,
                          shutdown_policy="migrate")
    events = _notify_recorder(eng)
    await eng._check_clusters()
    assert [s for s, _, _ in events if "shutdown_policy" in s]


@pytest.mark.asyncio
async def test_startup_inspects_the_clusters_without_waiting_for_a_selftest_slot(monkeypatch):
    """After the appliance's own shutdown, the leftovers must be visible immediately.

    The self-test latch survives a restart, so the next scheduled run can be a day away —
    and until then cluster_states was empty, which hid both the dashboard line and the
    "Restore cluster" button. That button is the only prompt the operator gets.
    """
    eng = _cluster_engine(monkeypatch, all_nodes=True, flags={"noout": True},
                          armed_state="disarmed")
    eng.last_selftest_slot = datetime.now()   # today's slot already ran
    events = _notify_recorder(eng)

    await eng._maybe_cluster_startup_check()

    assert eng.cluster_states["prod"].needs_recovery
    assert [c for c in eng.cluster_snapshot() if c["needs_recovery"]]
    assert any("still prepared for shutdown" in s for s, _, _ in events)

    # Exactly once per process start.
    before = len(eng.cluster_states)
    await eng._maybe_cluster_startup_check()
    assert len(eng.cluster_states) == before and eng._cluster_startup_done


@pytest.mark.asyncio
async def test_startup_inspection_is_skipped_while_a_ups_is_on_battery(monkeypatch):
    """Same reason the self-test is: the countdown has to stay responsive. No latch, so
    it still runs once mains are back."""
    eng = _cluster_engine(monkeypatch, all_nodes=True)
    eng.shutdown_triggered = True

    await eng._maybe_cluster_startup_check()

    assert eng.cluster_states == {} and not eng._cluster_startup_done


@pytest.mark.asyncio
async def test_restore_is_refused_while_the_shutdown_is_running(monkeypatch):
    """The button appears the moment the preparation lands — which is mid-shutdown.
    Arming HA there would undo the preparation at the one moment it is doing its job."""
    srv = _CephServer(armed="disarmed")
    eng = _outage_cluster_engine(monkeypatch, srv)
    eng.shutdown_triggered = True
    before = len(srv.calls)

    allowed, results = await eng.restore_clusters()

    assert allowed is False and results == []
    assert srv.calls[before:] == [], "not even a read is worth the poll budget here"


@pytest.mark.asyncio
async def test_node_count_warning_counts_every_target_not_only_the_ticked_ones(monkeypatch):
    """The tick governs the preparation (once per cluster anyway); this warning is about
    nodes nobody will shut down. Counting ticks accused a complete setup of leaving nodes
    running."""
    from app.engine import Engine

    eng = _cluster_engine(monkeypatch, all_nodes=True)
    # Every node is a target, but only the first carries the cluster tick.
    for h in eng.cfg.hosts[1:]:
        h.cluster = False
    events = _notify_recorder(eng)

    await eng._check_clusters()

    # The full subject, not a prefix: "some nodes are left out of the cluster
    # handling" is a different and perfectly correct warning about this same
    # configuration (see _check_cluster_optout), and matching loosely would make
    # this test pass or fail on the wording of its neighbour.
    assert not [s for s, _, _ in events
                if "not every node is a configured target" in s]

    # A genuinely incomplete setup is still reported.
    eng2 = _cluster_engine(monkeypatch)          # only pve01 of three configured
    events2 = _notify_recorder(eng2)
    await eng2._check_clusters()
    assert [s for s, _, _ in events2 if "not every node" in s]
    assert isinstance(eng2, Engine)


@pytest.mark.asyncio
async def test_a_node_that_opted_out_of_the_cluster_handling_is_still_named(monkeypatch):
    """The membership rule is right and it made this node invisible.

    Filtering _cluster_members() on ``h.cluster`` stopped an unticked node from being
    powered off by another node's outage — but the preparation is cluster-WIDE either way:
    HA is disarmed for the whole cluster and, with Ceph, every guest in it is stopped,
    including the ones running there. Every neighbouring check looks past it: the "only
    part of the cluster" event and _unit_additions() count over the ticked members, the
    switch-agreement check compares within them, and node_coverage() sees a perfectly good
    enabled target and calls it covered.
    """
    eng = _cluster_engine(monkeypatch, all_nodes=True)
    for h in eng.cfg.hosts[1:]:
        h.cluster = False
    events = _notify_recorder(eng)

    await eng._check_clusters()

    hit = [b for s, _, b in events if "left out of the cluster handling" in s]
    assert hit, "a node the preparation reaches but the shutdown skips must be named"
    assert "pve02" in hit[0] and "pve03" in hit[0]
    assert "HA" in hit[0], "it has to say what actually happens to that machine"

    # Ticked everywhere: nothing to say.
    quiet = _cluster_engine(monkeypatch, all_nodes=True)
    quiet_events = _notify_recorder(quiet)
    await quiet._check_clusters()
    assert not [s for s, _, _ in quiet_events if "left out of the cluster handling" in s]


@pytest.mark.asyncio
async def test_the_ceph_clause_is_only_said_where_the_guests_really_stop(monkeypatch):
    """With Ceph the opted-out node also loses its guests, which is the worse half."""
    eng = _cluster_engine(monkeypatch, all_nodes=True)
    eng.cfg.hosts[0].cluster_ceph = True
    for h in eng.cfg.hosts[1:]:
        h.cluster = False
    events = _notify_recorder(eng)

    await eng._check_clusters()

    body = [b for s, _, b in events if "left out of the cluster handling" in s][0]
    assert "guest" in body and "Ceph maintenance flags" in body


@pytest.mark.asyncio
async def test_a_standalone_node_marked_as_a_cluster_member_is_inspected_once(monkeypatch):
    """Reachable and settled: it is not a cluster, and that answer cannot change inside
    one episode. Without a latch every poll on which the host is still due — a retried
    shutdown — paid for another full inspection off the battery for a question already
    answered, and nothing ever told the operator the tick does nothing here."""
    from app import cluster as cluster_mod

    eng = _cluster_engine(monkeypatch, is_cluster=False)
    host = eng.cfg.hosts[0]
    calls: list[str] = []
    real = cluster_mod.inspect

    async def counting(h, *a, **kw):
        calls.append(h.name)
        return await real(h, *a, **kw)

    monkeypatch.setattr(cluster_mod, "inspect", counting)
    events = _notify_recorder(eng)

    for _ in range(3):
        assert await eng._prepare_clusters([(host, "battery low")]) == [
            (host, "battery low")
        ]

    assert calls == ["pve01"], "one inspection per episode, not one per poll"
    assert eng.cluster_standalone == {host.key}
    # "Not a cluster" is not a failed preparation: it must never hold the node back.
    assert not [s for s, _, _ in events if "not reachable for preparation" in s]
    eng.cfg.thresholds.cluster_abort_on_prep_failure = True
    assert await eng._prepare_clusters([(host, "battery low")]) == [
        (host, "battery low")
    ]
    assert calls == ["pve01"]

    # A new episode asks afresh, exactly like the other per-episode latches.
    eng._release_shutdown_latches()
    assert eng.cluster_standalone == set()
    await eng._prepare_clusters([(host, "battery low")])
    assert calls == ["pve01", "pve01"]


@pytest.mark.asyncio
async def test_host_snapshot_carries_cluster_membership_and_name(monkeypatch):
    """The UI marks a cluster member on the collapsed host card the way it marks the
    appliance with a star — which needs both the flag and the discovered name."""
    eng = _cluster_engine(monkeypatch, all_nodes=True)
    await eng._check_clusters()

    hosts = {h["name"]: h for h in eng.snapshot()["hosts"]}
    assert hosts["pve01"]["cluster"] is True
    assert hosts["pve01"]["cluster_name"] == "prod"

    # A host that is not a member reports the flag as False and no name at all.
    eng.cfg.hosts[1].cluster = False
    assert eng.snapshot()["hosts"][1]["cluster"] is False


def test_ceph_flags_are_off_by_default_but_ha_disarm_is_not():
    """Ceph is the exception (ZFS, NFS/iSCSI); writing to a storage layer is opted into.
    HA disarm is the reason one ticks "cluster member" in the first place."""
    h = PveHostConfig(name="pve01", api_url="https://pve:8006",
                      token_id="ups@pve!x", token_secret="s", cluster=True)
    assert h.cluster_ceph is False
    assert h.cluster_ha_disarm is True


@pytest.mark.asyncio
async def test_a_timed_out_preparation_reports_what_it_managed(monkeypatch):
    """"Gave up" alone cannot be told apart from "never touched it" — and half prepared
    is exactly the state the operator has to know about afterwards."""
    import asyncio

    from app import cluster

    class _DisarmOkThenHangingFlags(_CephServer):
        """The disarm succeeds, then the flags never land.

        Written against the real order (disarm, guests, flags), so the step that runs out
        of budget is the last one — which is exactly when "gave up" on its own would hide
        that HA is already disarmed and needs re-arming.
        """

        async def put(self, url, **kw):
            if url.startswith("/cluster/ceph/flags"):
                await asyncio.sleep(30)
            return await super().put(url, **kw)

    srv = _DisarmOkThenHangingFlags()
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)

    result = await cluster.prepare(_pve(), want_ceph=True, want_disarm=True, timeout=0.3)

    assert result.ok is False
    assert "gave up" in result.message
    assert result.steps, "the partial state must survive the cancellation"
    # Names what DID happen, so the operator knows HA is disarmed and needs restoring.
    assert "HA disarmed" in result.message and "done so far" in result.message
    assert srv.armed == "disarmed"


@pytest.mark.asyncio
async def test_restore_rearms_a_cluster_that_has_no_ha_guests(monkeypatch):
    """Previously skipped, because needs_recovery required HA resources."""
    from app import cluster
    from app.engine import Engine

    srv = _CephServer(armed="disarmed")
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)
    eng = Engine(AppConfig(hosts=[PveHostConfig(
        name="pve01", api_url="https://pve:8006", token_id="ups@pve!x",
        token_secret="s", cluster=True)]))
    _notify_recorder(eng)

    allowed, results = await eng.restore_clusters()

    assert allowed
    assert results and results[0]["ok"], results
    assert srv.armed == "armed"


@pytest.mark.asyncio
async def test_host_test_reports_a_disarmed_stack_and_points_at_the_button(
    _import_target, monkeypatch
):
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, armed_state="disarmed", ha_services=0)

    result = await main.api_test_host(_host_payload(cluster=True))

    assert result["cluster"]["ha_disarmed"] is True
    assert result["cluster"]["ha_services"] == 0
    assert "HA disarmed" in result["message"]
    assert "Restore cluster" in result["message"]


@pytest.mark.asyncio
async def test_cluster_probe_records_every_query_with_its_outcome(_import_target, monkeypatch):
    """The diagnostics panel: one entry per endpoint, so a failing check is debuggable."""
    from app.cluster import CLUSTER_PROBE_STATUSES

    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, armed_state="disarmed", ha_services=0)

    result = await main.api_test_host(_host_payload(cluster=True))
    entries = {e["name"]: e for e in result["cluster"]["entries"]}

    assert set(entries) == {
        "/access/permissions", "/cluster/status", "/cluster/ceph/status",
        "/cluster/resources?type=vm", "/cluster/resources?type=storage",
        "appliance guest",
        "/cluster/ceph/flags", "/cluster/ha/status", "/cluster/ha/status/current",
        "/cluster/options",
    }
    assert {e["status"] for e in entries.values()} <= set(CLUSTER_PROBE_STATUSES)
    # The one line that would have answered the question on real hardware.
    assert "armed-state=disarmed" in entries["/cluster/ha/status/current"]["value"]
    assert "0 HA services" in entries["/cluster/ha/status/current"]["value"]
    assert "Sys.Console" in entries["/access/permissions"]["value"]
    assert "quorate" in entries["/cluster/status"]["value"]
    # The guest list and the appliance's own guest are the two facts the new step needs,
    # so the panel has to show both rather than only their conclusion.
    assert "3 guests, 3 running" in entries["/cluster/resources?type=vm"]["value"]
    assert entries["appliance guest"]["status"] == "absent"
    assert "cephpool" in entries["/cluster/resources?type=storage"]["value"]


@pytest.mark.asyncio
async def test_cluster_probe_classifies_denied_and_absent(_import_target, monkeypatch):
    main, _ = _import_target
    # No Sys.Audit at all: every gated endpoint refuses.
    _fake_host_and_cluster(monkeypatch, permissions={"Sys.PowerMgmt": 1}, has_disarm=False)

    result = await main.api_test_host(_host_payload(cluster=True))
    entries = {e["name"]: e for e in result["cluster"]["entries"]}

    # A 403 is actionable and must not be filed as a generic error.
    assert entries["/cluster/status"]["status"] == "denied"
    # The summary must not mistake "not allowed to look" for "standalone node".
    assert "lacks Sys.Audit" in result["message"]
    assert "not part of a cluster" not in result["message"]
    # Membership is unreadable here, so the HA/Ceph endpoints are deliberately not
    # queried at all — no point piling up six more refusals.
    assert "/cluster/ha/status/current" not in entries


@pytest.mark.asyncio
async def test_cluster_probe_marks_a_missing_endpoint_as_absent(_import_target, monkeypatch):
    """A release without disarm-ha is a fact about the version, not a failure."""
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, has_disarm=False)

    result = await main.api_test_host(_host_payload(cluster=True))
    entries = {e["name"]: e for e in result["cluster"]["entries"]}

    assert entries["/cluster/ha/status"]["status"] == "absent"
    assert "9.2" in entries["/cluster/ha/status"]["value"]
    assert entries["/cluster/status"]["status"] == "ok"


def _diag_trouble(cluster_result: dict) -> bool:
    """The UI's rule for unfolding the diagnostics panel in orange (app.js testHost())."""
    return bool(cluster_result["missing_privileges"]) or any(
        e["status"] in ("denied", "error") for e in cluster_result["entries"]
    )


@pytest.mark.asyncio
async def test_cluster_probe_does_not_flag_an_unneeded_guest_list(_import_target, monkeypatch):
    """#39: a token scoped to a plain cluster lacks VM.Audit on purpose. The guest list it
    cannot read is then 'not needed', not an error that turns the panel orange."""
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, has_ceph=False,
                           permissions={"Sys.PowerMgmt": 1, "Sys.Audit": 1, "Sys.Console": 1})

    result = await main.api_test_host(_host_payload(cluster=True))
    entries = {e["name"]: e for e in result["cluster"]["entries"]}

    assert entries["/cluster/resources?type=vm"]["status"] == "unneeded"
    assert result["cluster"]["missing_privileges"] == []
    assert not _diag_trouble(result["cluster"])


@pytest.mark.asyncio
async def test_cluster_probe_keeps_flagging_a_guest_list_the_options_need(
        _import_target, monkeypatch):
    """The same unreadable list IS a problem once an option stops the guests — and it is a
    missing privilege ("denied"), not a generic error."""
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch,
                           permissions={"Sys.PowerMgmt": 1, "Sys.Audit": 1, "Sys.Modify": 1,
                                        "Sys.Console": 1})

    result = await main.api_test_host(_host_payload(cluster=True, cluster_ceph=True))
    entries = {e["name"]: e for e in result["cluster"]["entries"]}

    assert entries["/cluster/resources?type=vm"]["status"] == "denied"
    assert _diag_trouble(result["cluster"])


def test_mark_unneeded_only_touches_failed_reads_of_unwanted_features():
    from app import cluster

    info = cluster.ClusterInfo()
    for name, status, feature in (
        ("ceph-denied", "denied", cluster.FEATURE_CEPH),
        ("ceph-ok", "ok", cluster.FEATURE_CEPH),
        ("guests-error", "error", cluster.FEATURE_GUESTS),
        ("always", "denied", ""),
    ):
        info.probe.append(cluster.ClusterProbeEntry(name=name, status=status, feature=feature))

    cluster.mark_unneeded(info, want_ceph=False, want_guests=True)
    got = {e.name: e.status for e in info.probe}

    assert got == {"ceph-denied": "unneeded", "ceph-ok": "ok",
                   "guests-error": "error", "always": "denied"}


def test_ceph_error_separates_no_ceph_from_unreadable():
    """"not configured" and "not permitted" must not look the same to the operator."""
    from app.cluster import ClusterInfo

    no_ceph = ClusterInfo(ceph_error="/cluster/ceph/flags: HTTP 500")
    denied = ClusterInfo(ceph_error="/cluster/ceph/flags: not permitted (missing privilege)")

    assert "not permitted" not in (no_ceph.ceph_error or "")
    assert "not permitted" in (denied.ceph_error or "")


# --- cluster-wide guest shutdown (hyper-converged Ceph) ----------------------
# The step that makes a Ceph cluster survivable: every guest has to stop BEFORE the first
# node loses power. Stopping them node by node drops the pool below min_size while guests
# are still running on the survivors, their IO blocks, and their shutdown never finishes.


def _guest_calls(srv, kind="shutdown"):
    return [u for m, u in srv.calls if m == "POST" and u.endswith(f"/status/{kind}")]


@pytest.mark.asyncio
async def test_guests_are_stopped_after_the_disarm_and_before_the_ceph_flags(monkeypatch):
    """The whole point of the feature is the ORDER, so that is what is pinned.

    Disarming after the guests would let the HA manager restart them as fast as they are
    stopped; setting the flags first would leave them on if the stop then fails.
    """
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    order = [u for m, u in srv.calls if m in ("POST", "PUT")]
    disarm = order.index("/cluster/ha/status/disarm-ha")
    first_guest = min(order.index(u) for u in _guest_calls(srv))
    flags = order.index("/cluster/ceph/flags")
    assert disarm < first_guest < flags


@pytest.mark.asyncio
async def test_the_appliances_own_guest_is_never_stopped(monkeypatch):
    """Stopping every guest would stop the appliance halfway through the outage."""
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert not [u for u in _guest_calls(srv) if "/lxc/950/" in u]
    assert not [u for u in _guest_calls(srv, "stop") if "/lxc/950/" in u]
    assert srv.guests[950]["status"] == "running"
    # The others did go down.
    assert {srv.guests[100]["status"], srv.guests[101]["status"]} == {"stopped"}


@pytest.mark.asyncio
async def test_a_guest_carrying_our_hostname_is_spared_even_with_a_wrong_pick(monkeypatch):
    """Belt and braces against a mis-selected vmid, which is the one mistake that would
    switch this appliance off in the middle of an outage."""
    from app import cluster

    srv = _CephServer()
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)
    monkeypatch.setattr(cluster, "_GUEST_POLL_INTERVAL_S", 0.01)
    info = await cluster.inspect(_pve(), self_vmid=100, self_node="pve01")

    result = await cluster.prepare(
        _pve(), want_ceph=True, want_disarm=True, want_guests=True,
        guests=info.guests, self_guest=info.self_guest, guest_needs_disarm=False,
        hostname="pve-usv", timeout=5, guest_timeout=5, force_after_s=1,
    )

    assert result.ok
    assert srv.guests[950]["status"] == "running", "matched by hostname, not by the pick"
    assert srv.guests[100]["status"] == "running", "the (wrong) pick is spared too"
    assert srv.guests[101]["status"] == "stopped"


@pytest.mark.asyncio
async def test_a_guest_that_ignores_the_shutdown_is_force_stopped(monkeypatch):
    srv = _CephServer(guest_ignores_shutdown=(101,))
    eng = _outage_cluster_engine(monkeypatch, srv)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert any("/lxc/101/status/stop" in u for u in _guest_calls(srv, "stop"))
    assert srv.guests[101]["status"] == "stopped"
    assert eng.cluster_prep_failed["prod"] is False


@pytest.mark.asyncio
async def test_an_unreadable_guest_list_is_never_read_as_all_guests_stopped(monkeypatch):
    """"Stopped" is only ever concluded from a re-read - so a read that did not happen
    cannot conclude it.

    _read_running_vmids() used to answer an unreadable listing with an empty set, which is
    exactly what a finished cluster looks like. The verification then reported success on
    no evidence at all, in the one module whose whole doctrine is that a write is
    confirmed by a read or not at all."""
    from app import cluster

    class _BlindAfterShutdown(_CephServer):
        """The guest listing stops answering the moment the guests were asked to stop."""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.blind = False

        async def get(self, url, **kw):
            if url == "/cluster/resources?type=vm" and self.blind:
                self.calls.append(("GET", url))
                return _FakeJson(500, {})
            return await super().get(url, **kw)

        async def _guest_power(self, url):
            resp = await super()._guest_power(url)
            if "/status/shutdown" in url:
                self.blind = True
            return resp

    srv = _BlindAfterShutdown()
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)
    monkeypatch.setattr(cluster, "_GUEST_POLL_INTERVAL_S", 0.01)
    info = await cluster.inspect(_pve(), self_vmid=950, self_node="pve01")

    result = await cluster.prepare(
        _pve(), want_ceph=True, want_disarm=True, want_guests=True,
        guests=info.guests, self_guest=info.self_guest, guest_needs_disarm=False,
        hostname="pve-usv", timeout=3, guest_timeout=1, force_after_s=None,
    )

    assert result.ok is False, "an unreadable list is not a stopped cluster"
    assert "could not be re-read" in result.message
    # And the flags still go on: they cost nothing and still stop the rebalancing.
    assert all(srv.flags.values())


@pytest.mark.asyncio
async def test_a_slow_force_stop_cannot_swallow_the_ceph_step(monkeypatch):
    """The force-stop round is bounded and concurrent, so the step behind it still runs.

    It used to be a sequential loop bounded only by the client's timeout, so a control
    plane that had stopped answering spent CONNECT_TIMEOUT_S per straggler. Enough of them
    outlasted prepare()'s outer wait_for, and the whole preparation came back as a bare
    "gave up" - taking the Ceph flags, whose reserve exists for exactly this, with it."""
    import asyncio as _asyncio

    from app import cluster

    hung = [
        {"vmid": 200 + i, "node": "pve01", "type": "qemu", "name": f"vm{i}",
         "status": "running"}
        for i in range(12)
    ]
    guests = [
        {"vmid": 950, "node": "pve01", "type": "lxc", "name": "pve-usv",
         "status": "running"},
    ] + hung

    class _SlowStop(_CephServer):
        """Counts the FORCE-STOP round separately: the graceful round was already
        concurrent, so the shared max_inflight says nothing about the one under test."""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.stop_inflight = 0
            self.max_stop_inflight = 0

        async def _guest_power(self, url):
            if not url.endswith("/status/stop"):
                return await super()._guest_power(url)
            self.stop_inflight += 1
            self.max_stop_inflight = max(self.max_stop_inflight, self.stop_inflight)
            try:
                await _asyncio.sleep(0.4)     # a control plane answering very slowly
                return await super()._guest_power(url)
            finally:
                self.stop_inflight -= 1

    srv = _SlowStop(guests=guests,
                    guest_ignores_shutdown=tuple(g["vmid"] for g in hung))
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)
    monkeypatch.setattr(cluster, "_GUEST_POLL_INTERVAL_S", 0.01)
    info = await cluster.inspect(_pve(), self_vmid=950, self_node="pve01")

    result = await cluster.prepare(
        _pve(), want_ceph=True, want_disarm=True, want_guests=True,
        guests=info.guests, self_guest=info.self_guest, guest_needs_disarm=False,
        hostname="pve-usv", timeout=3, guest_timeout=2, force_after_s=1,
    )

    # Twelve stragglers at 0.4 s each is 4.8 s in sequence - past the whole budget. Run
    # under the module's own semaphore they fit, and the Ceph step behind them still runs.
    assert srv.max_stop_inflight > 1, "the force-stop round has to be concurrent"
    assert "gave up" not in result.message
    assert all(srv.flags.values()), "the step after the guests must still have happened"


@pytest.mark.asyncio
async def test_without_force_a_hung_guest_fails_the_preparation_by_name(monkeypatch):
    """"Never force" has to mean it — and then say which guest is still up, because that
    is the machine someone has to go and look at."""
    from app import cluster

    srv = _CephServer(guest_ignores_shutdown=(101,))
    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)
    monkeypatch.setattr(cluster, "_GUEST_POLL_INTERVAL_S", 0.01)
    info = await cluster.inspect(_pve(), self_vmid=950, self_node="pve01")

    result = await cluster.prepare(
        _pve(), want_ceph=True, want_disarm=True, want_guests=True,
        guests=info.guests, self_guest=info.self_guest, guest_needs_disarm=False,
        timeout=3, guest_timeout=1, force_after_s=None,
    )

    assert result.ok is False
    assert not _guest_calls(srv, "stop"), "force-stop is off, so nothing may be killed"
    assert "CT 101 'db' on pve02" in result.message
    # The flags are still set: they cost nothing, they still stop the rebalancing, and
    # leaving them off would give the worst of both worlds.
    assert all(srv.flags.values())


@pytest.mark.asyncio
async def test_a_cluster_without_ceph_never_touches_the_guests(monkeypatch):
    """The whole special procedure exists for Ceph. A plain cluster keeps its old
    behaviour, node by node, exactly as before."""
    srv = _CephServer(has_ceph=True)
    eng = _outage_cluster_engine(monkeypatch, srv)
    for h in eng.cfg.hosts:
        h.cluster_ceph = False
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert not _guest_calls(srv)
    assert ("POST", "/cluster/ha/status/disarm-ha") in srv.calls
    assert not any(srv.flags.values())


# --- the guest stop on its own switch, without Ceph (#43) ---------------------
def _guest_stop_only(eng, *, ceph=False):
    for h in eng.cfg.hosts:
        h.cluster_ceph = ceph
        h.cluster_guest_stop = True


@pytest.mark.asyncio
async def test_the_guest_stop_runs_without_ceph_when_switched_on(monkeypatch):
    """#43: a plain cluster (NFS, no Ceph) can ask for the parallel cluster-wide guest
    stop on its own. The guests go, the appliance stays, and no Ceph flag is touched."""
    srv = _CephServer(has_ceph=False)
    eng = _outage_cluster_engine(monkeypatch, srv)
    _guest_stop_only(eng)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert len(_guest_calls(srv)) == 2
    assert srv.guests[950]["status"] == "running"
    assert ("POST", "/cluster/ha/status/disarm-ha") in srv.calls
    assert not any(srv.flags.values())


@pytest.mark.asyncio
async def test_the_guest_stop_tick_does_not_set_ceph_flags_on_a_ceph_cluster(monkeypatch):
    """The two switches stay apart in the direction that matters: the guest stop alone
    never writes into the storage layer, even where there is Ceph."""
    srv = _CephServer(has_ceph=True)
    eng = _outage_cluster_engine(monkeypatch, srv)
    _guest_stop_only(eng)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert len(_guest_calls(srv)) == 2
    assert not any(srv.flags.values())


@pytest.mark.asyncio
async def test_a_ceph_tick_without_ceph_still_honours_the_guest_stop_tick(monkeypatch):
    """Ceph ticked on a cluster that has none: the Ceph part is skipped as before, but an
    explicit guest-stop tick is a request of its own and still runs."""
    srv = _CephServer(has_ceph=False)
    eng = _outage_cluster_engine(monkeypatch, srv)
    _guest_stop_only(eng, ceph=True)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert len(_guest_calls(srv)) == 2
    assert not any(srv.flags.values())


@pytest.mark.asyncio
async def test_a_skipped_guest_stop_without_ceph_is_a_warning_not_critical(monkeypatch):
    """Without Ceph a skipped guest stop costs speed, not the storage: the nodes still
    stop their own guests as they power off."""
    srv = _CephServer(has_ceph=False, has_disarm=False, ha_services=2)
    eng = _outage_cluster_engine(monkeypatch, srv)
    _guest_stop_only(eng)
    events = _notify_recorder(eng)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert not _guest_calls(srv)
    skipped = [(sev, body) for s, sev, body in events if s.endswith("guest shutdown skipped")]
    assert skipped and skipped[0][0] == "warning"
    assert "its own guests" in skipped[0][1]


@pytest.mark.asyncio
async def test_the_preview_applies_the_same_guest_stop_rules_as_the_preparation(monkeypatch):
    """The preview used to promise a guest stop the preparation then skipped for want of
    an HA disarm. Both now read _guest_plan()."""
    from app import cluster

    srv = _CephServer(has_ceph=False, has_disarm=False, ha_services=2)
    eng = _outage_cluster_engine(monkeypatch, srv)
    _guest_stop_only(eng)
    eng.cluster_states["prod"] = await cluster.inspect(
        eng.cfg.hosts[0], self_vmid=950, self_node="pve01")

    preview = eng._cluster_preview()

    assert "guest stop skipped (needs HA disarm" in preview
    assert "Ceph flags" not in preview

    srv_ok = _CephServer(has_ceph=False)
    eng2 = _outage_cluster_engine(monkeypatch, srv_ok)
    _guest_stop_only(eng2)
    eng2.cluster_states["prod"] = await cluster.inspect(
        eng2.cfg.hosts[0], self_vmid=950, self_node="pve01")
    assert "stop 2 of 3 running guests" in eng2._cluster_preview()


def test_the_shutdown_budget_counts_the_guest_stop_without_ceph():
    from app.engine import shutdown_budget

    th = Thresholds(cluster_prep_timeout_s=60, cluster_guest_shutdown_timeout_s=300)

    def budget(**switches):
        return shutdown_budget(AppConfig(thresholds=th, hosts=[
            PveHostConfig(name="a", api_url="x", cluster=True, **switches)])).cluster_s

    assert budget() == 60
    assert budget(cluster_guest_stop=True) == 360
    assert budget(cluster_ceph=True) == 360


def test_the_guest_stop_switch_defaults_off_and_round_trips():
    host = PveHostConfig(name="a", api_url="x")
    assert host.cluster_guest_stop is False
    again = PveHostConfig.model_validate(
        PveHostConfig(name="a", api_url="x", cluster_guest_stop=True).model_dump())
    assert again.cluster_guest_stop is True


@pytest.mark.asyncio
async def test_host_test_asks_for_the_guest_privileges_with_the_guest_stop_alone(
        _import_target, monkeypatch):
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, has_ceph=False,
                           permissions={"Sys.PowerMgmt": 1, "Sys.Audit": 1, "Sys.Console": 1})

    result = await main.api_test_host(
        _host_payload(cluster=True, cluster_guest_stop=True))
    entries = {e["name"]: e for e in result["cluster"]["entries"]}

    assert result["cluster"]["missing_privileges"] == [
        "VM.Audit (list the cluster's guests)",
        "VM.PowerMgmt (stop the guests before the shutdown)"]
    # Needed now, so the unreadable list stays a finding (#39 only hides unneeded ones).
    assert entries["/cluster/resources?type=vm"]["status"] == "denied"
    assert "guest list cannot be read" in result["message"]


@pytest.mark.asyncio
async def test_host_test_says_when_ha_blocks_the_guest_stop(_import_target, monkeypatch):
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch, has_ceph=False, has_disarm=False, ha_services=2)

    result = await main.api_test_host(
        _host_payload(cluster=True, cluster_guest_stop=True))

    assert "NOT stopped cluster-wide" in result["message"]
    assert "9.2" in result["message"]


@pytest.mark.asyncio
async def test_no_disarm_means_no_guest_stop(monkeypatch):
    """Chosen behaviour: without a verified disarm the guests are left alone. Stopping
    them while the HA manager is live and holding resources only feeds it work."""
    srv = _CephServer(has_disarm=False, ha_services=2)
    eng = _outage_cluster_engine(monkeypatch, srv)
    events = _notify_recorder(eng)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert not _guest_calls(srv)
    assert "Cluster prod: guest shutdown skipped" in [s for s, _, _ in events]
    body = next(b for s, _, b in events if s.endswith("guest shutdown skipped"))
    assert "9.2" in body
    # Everything that DOES work still happens.
    assert all(srv.flags.values())


@pytest.mark.asyncio
async def test_without_ha_resources_the_guests_stop_even_without_disarm_ha(monkeypatch):
    """The one carve-out: with zero HA resources there is no HA manager to restart
    anything, so the precondition simply does not apply."""
    srv = _CephServer(has_disarm=False, ha_services=0)
    eng = _outage_cluster_engine(monkeypatch, srv)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert len(_guest_calls(srv)) == 2
    assert srv.guests[950]["status"] == "running"


@pytest.mark.asyncio
async def test_an_unidentifiable_own_guest_refuses_the_step_but_not_the_shutdown(monkeypatch):
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv, self_vmid=None)
    eng.cfg.appliance.self_node = ""
    events = _notify_recorder(eng)
    fired: list = []

    async def fake_fire(host, reason):
        fired.append(host.name)
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    await eng._evaluate()

    assert not _guest_calls(srv)
    assert "Cluster prod: guest shutdown skipped" in [s for s, _, _ in events]
    # Refusing the guest stop must never refuse the shutdown: that would be worse than
    # the 4.0 behaviour, not better.
    assert fired == ["pve01", "pve02"]
    assert all(srv.flags.values())


@pytest.mark.asyncio
async def test_a_permission_filtered_empty_guest_list_is_not_no_guests(monkeypatch):
    """/cluster/resources answers 200 with an empty list when the token lacks VM.Audit.
    Reading that as "nothing to stop" would report success while forty guests keep
    writing to Ceph — the most dangerous failure this feature can have."""
    srv = _CephServer(vm_audit=False)
    eng = _outage_cluster_engine(monkeypatch, srv)
    events = _notify_recorder(eng)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    body = next(b for s, _, b in events if s.endswith("guest shutdown skipped"))
    assert "VM.Audit" in body
    assert not _guest_calls(srv)


@pytest.mark.asyncio
async def test_guest_shutdowns_stay_within_the_concurrency_limit(monkeypatch):
    """Sequential is hopeless (a connect timeout per guest), unbounded opens a socket per
    guest at the worst possible moment."""
    from app import cluster

    guests = [{"vmid": 950, "node": "pve01", "type": "lxc", "name": "pve-usv",
               "status": "running"}]
    guests += [{"vmid": 200 + i, "node": "pve01", "type": "qemu", "name": f"vm{i}",
                "status": "running"} for i in range(30)]
    srv = _CephServer(guests=guests)
    eng = _outage_cluster_engine(monkeypatch, srv)
    eng._fire_host = _noop_fire(eng)  # type: ignore[assignment]

    await eng._evaluate()

    assert len(_guest_calls(srv)) == 30
    assert 1 < srv.max_inflight <= cluster._GUEST_CONCURRENCY


@pytest.mark.asyncio
async def test_the_graceful_guest_round_cannot_outlast_its_own_deadline(monkeypatch):
    """The round that ASKS the guests to stop is bounded, not only the one that kills them.

    Each request is limited by the client's timeout alone, so a control plane that accepts
    connections and then goes quiet cost a full timeout per batch of _GUEST_CONCURRENCY.
    That time comes off the front of the guest budget -- ``grace`` is measured from what is
    left afterwards -- so on a large cluster it collapsed to zero and every guest was
    force-stopped at once instead of ever being asked. The kill round below it had already
    been fixed; this one had been left out."""
    import asyncio as aio
    from app import cluster as cluster_mod

    guests = [
        cluster_mod.GuestInfo(vmid=200 + i, node="pve01", kind="qemu", name=f"vm{i}",
                              status="running")
        for i in range(3 * cluster_mod._GUEST_CONCURRENCY)
    ]

    async def hanging_post(client, path, **kw):
        await aio.sleep(30)          # accepts, then goes quiet
        return None

    async def unreadable(client, timeout=0.0):
        return None                  # the re-read cannot confirm anything either

    monkeypatch.setattr(cluster_mod, "_post", hanging_post)
    monkeypatch.setattr(cluster_mod, "_read_running_vmids", unreadable)
    monkeypatch.setattr(cluster_mod, "_GUEST_POLL_INTERVAL_S", 0.01)

    steps: list[str] = []
    loop = aio.get_running_loop()
    started = loop.time()
    ok, left = await cluster_mod._shutdown_guests(
        object(), guests, steps, deadline=loop.time() + 2.0, force_after_s=1,
    )

    elapsed = loop.time() - started
    assert elapsed < 6.0, f"the round has to end with the deadline, not with the requests ({elapsed:.1f}s)"
    assert ok is False
    assert any("cut short by the guest deadline" in line for line in steps)
    # An unreadable re-read still means "assume they are all up", never "all stopped".
    assert len(left) == len(guests)


@pytest.mark.asyncio
async def test_the_preview_describes_the_four_steps_without_touching_anything(monkeypatch):
    """A POST the operator clicks: it must be instant and free of side effects, so it is
    built from the last inspection rather than from fresh calls."""
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv)
    await eng._check_clusters()
    before = len(srv.calls)

    msg = await eng.simulate_shutdown()

    assert len(srv.calls) == before, "the preview may not talk to the cluster"
    assert "HA disarm -> stop 2 of 3 running guests" in msg
    assert "sparing CT 950 'pve-usv' on pve01" in msg
    assert "Ceph flags noout,nobackfill,norecover,norebalance" in msg
    assert "NOTHING was shut down." in msg


# --- the appliance's own guest: resolution and storage -----------------------


def _g(vmid, node="pve01", kind="lxc", name="pve-usv"):
    from app.cluster import GuestInfo

    return GuestInfo(vmid=vmid, node=node, kind=kind, name=name, status="running")


def test_find_self_guest_prefers_the_explicit_pick_and_never_guesses_past_it():
    from app.cluster import find_self_guest

    guests = [_g(950), _g(100, "pve02", "qemu", "web")]

    assert find_self_guest(guests, 950, "", "")[1] == "config"
    # The decisive one: a pick that is not there must NOT fall back to the hostname.
    # Guessing after an explicit choice is how a renumbered appliance stops itself.
    assert find_self_guest(guests, 999, "", "pve-usv") == (None, "missing")
    assert find_self_guest(guests, 950, "pve02", "") == (None, "missing")


def test_find_self_guest_matches_the_hostname_only_when_it_is_unambiguous():
    from app.cluster import find_self_guest

    guests = [_g(950), _g(100, "pve02", "qemu", "web")]
    assert find_self_guest(guests, None, "", "PVE-USV.lan.example")[0].vmid == 950
    assert find_self_guest(guests, None, "", "nothing-like-this") == (None, "none")
    assert find_self_guest(guests, None, "", "") == (None, "none")

    # A container is preferred over a VM of the same name (that is what the installer
    # creates); two containers of one name stay unresolved rather than being picked from.
    both = [_g(950), _g(951, "pve02", "qemu", "pve-usv")]
    assert find_self_guest(both, None, "", "pve-usv")[0].vmid == 950
    twins = [_g(950), _g(951, "pve02", "lxc", "pve-usv")]
    assert find_self_guest(twins, None, "", "pve-usv") == (None, "ambiguous")


def test_storages_of_config_finds_every_volume_reference():
    from app.cluster import storages_of_config

    cfg = {
        "rootfs": "local-lvm:vm-950-disk-0,size=4G",
        "mp0": "cephpool:vm-950-disk-1,mp=/data",
        "scsi0": "local-zfs:vm-100-disk-0",
        "unused0": "cephpool:vm-100-disk-9",   # detached still pins the storage
        "scsi1": "/dev/sdb",                   # a path has no storage in front of it
        "net0": "name=eth0,bridge=vmbr0",
    }
    assert storages_of_config(cfg) == ["local-lvm", "cephpool", "local-zfs"]


def test_mon_ordering_is_reported_but_never_enforced():
    from app.cluster import mon_order_report, mon_ordering_ok

    assert mon_ordering_ok(["pve03", "pve01", "pve02"], ["pve01", "pve02"])
    assert not mon_ordering_ok(["pve01", "pve03"], ["pve01"])
    assert mon_ordering_ok(["pve01", "pve02"], []) is True

    report = mon_order_report(["pve01", "pve02", "pve03"], ["pve01"], "pve03")
    assert "pve01" in report and "raise 'order'" in report
    # The appliance's node is forced last by the sort key, so suggesting a number for it
    # would be advice that cannot work.
    assert "always shut down last" in report
    assert mon_order_report(["pve03", "pve01"], ["pve01"], "") == ""


@pytest.mark.asyncio
async def test_the_selftest_warns_about_an_appliance_on_ceph_storage(monkeypatch):
    """The deployment mistake that only shows up when it is far too late to fix it."""
    srv = _CephServer(self_on_ceph=True)
    eng = _outage_cluster_engine(monkeypatch, srv)
    events = _notify_recorder(eng)

    await eng._check_clusters()

    subjects = [s for s, _, _ in events]
    assert "Cluster prod: this appliance runs on Ceph storage" in subjects
    body = next(b for s, _, b in events if s.endswith("on Ceph storage"))
    assert "cephpool" in body and "min_size" in body


@pytest.mark.asyncio
async def test_the_selftest_warns_when_the_appliances_node_is_not_marked_this_host(monkeypatch):
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv)
    events = _notify_recorder(eng)

    await eng._check_clusters()

    assert "Cluster prod: this appliance's node is not marked 'this host'" in [
        s for s, _, _ in events]


@pytest.mark.asyncio
async def test_the_selftest_warns_when_the_battery_reserve_is_shorter_than_the_sequence(
        monkeypatch):
    """Warned about, never acted on: lowering someone's trigger for them is not this
    appliance's decision."""
    from app import cluster as cluster_mod
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv)
    eng.cfg.thresholds.runtime_below_minutes = 1
    eng.cfg.thresholds.cluster_guest_shutdown_timeout_s = 300
    events = _notify_recorder(eng)

    await eng._warn_about_battery_reserve()

    body = next((b for s, _, b in events if "battery reserve" in s.lower()), "")
    # 5 s HA disarm + 300 s guests + the cluster inspection + TWO node stages. A stage
    # costs the host timeout PLUS targets.DEADLINE_GRACE_S, because that is what
    # targets.shutdown() is really bounded at, and it pays that per stage. Both
    # corrections point the same way: the figure an operator sizes a battery against must
    # never be short.
    from app import targets as targets_mod
    stage = 60 + int(targets_mod.DEADLINE_GRACE_S)
    total = (5 + 300 + int(cluster_mod.INSPECT_BUDGET_S
                           + cluster_mod.DEADLINE_GRACE_S) + 2 * stage)
    assert f"{total}s" in body
    assert f"2 node stage(s) {2 * stage}s" in body
    # And the arithmetic it prints adds up to the total it prints — they used to be
    # computed from two different stage counts.
    assert "cluster preparation 305s" in body and "cluster inspection 17s" in body


@pytest.mark.asyncio
async def test_the_battery_reserve_warning_does_not_need_a_cluster():
    """It used to live inside the Ceph-only self-guest check, so the majority of
    installations — standalone nodes, Backup Servers, clusters without Ceph — never saw the
    one warning that says the trigger fires later than the shutdown lasts."""
    cfg = AppConfig(
        hosts=[PveHostConfig(name="a", api_url="x", order=0),
               PveHostConfig(name="b", api_url="x2", order=1)],
        thresholds=Thresholds(runtime_below_minutes=1, host_shutdown_timeout_s=60),
    )
    eng = Engine(cfg)
    events = _notify_recorder(eng)

    await eng._warn_about_battery_reserve()

    body = next((b for s, _, b in events if "battery reserve" in s.lower()), "")
    from app import targets as targets_mod
    assert f"{2 * (60 + int(targets_mod.DEADLINE_GRACE_S))}s" in body
    assert "2 node stage(s)" in body
    assert "cluster" not in body       # no cluster here, so no cluster terms

    # Said once per configuration, not on every self-test run.
    events.clear()
    await eng._warn_about_battery_reserve()
    assert not events


@pytest.mark.asyncio
async def test_no_reserve_warning_when_the_reserve_is_long_enough():
    cfg = AppConfig(
        hosts=[PveHostConfig(name="a", api_url="x")],
        thresholds=Thresholds(runtime_below_minutes=30, host_shutdown_timeout_s=60),
    )
    eng = Engine(cfg)
    events = _notify_recorder(eng)
    await eng._warn_about_battery_reserve()
    assert not events


def test_the_shutdown_budget_counts_the_stages_that_really_run():
    """One term per thing that happens in sequence — not one per kind of thing."""
    from app.engine import shutdown_budget_s
    from app import targets as targets_mod

    th = Thresholds(host_shutdown_timeout_s=60, cluster_prep_timeout_s=60,
                    cluster_guest_shutdown_timeout_s=300)
    # What ONE stage really costs: targets.shutdown() bounds a call at the configured
    # timeout PLUS its grace — the backstop for everything httpx does not bound — and it
    # pays that per stage, not once for the whole sequence.
    stage = 60 + int(targets_mod.DEADLINE_GRACE_S)

    # No cluster, one stage.
    one = AppConfig(hosts=[PveHostConfig(name="a", api_url="x")], thresholds=th)
    assert shutdown_budget_s(one) == stage

    # Three distinct stages: two order values plus the appliance's own host, which always
    # forms the last stage on its own — PLUS the one retry pass _evaluate_hosts runs
    # immediately before that final stage, because no poll follows it to carry a retry.
    three = AppConfig(thresholds=th, hosts=[
        PveHostConfig(name="a", api_url="x", order=0),
        PveHostConfig(name="b", api_url="x", order=0),   # same stage as "a"
        PveHostConfig(name="c", api_url="x", order=1),
        PveHostConfig(name="me", api_url="x", this_host=True, order=0),
    ])
    assert shutdown_budget_s(three) == 4 * stage

    # A disabled host does not cost a stage.
    assert shutdown_budget_s(AppConfig(thresholds=th, hosts=[
        PveHostConfig(name="a", api_url="x", order=0),
        PveHostConfig(name="off", api_url="x", order=5, enabled=False),
    ])) == stage

    # The retry pass is counted only where it can actually run: it fires between the
    # earlier stages and ``this_host``, so it needs both to exist.
    no_own_host = AppConfig(thresholds=th, hosts=[
        PveHostConfig(name="a", api_url="x", order=0),
        PveHostConfig(name="c", api_url="x", order=1),
    ])
    assert shutdown_budget_s(no_own_host) == 2 * stage  # two stages, nothing after
    own_host_alone = AppConfig(thresholds=th, hosts=[
        PveHostConfig(name="me", api_url="x", this_host=True),
    ])
    assert shutdown_budget_s(own_host_alone) == stage  # one stage, nothing before it

    # With the cluster preparation on, its budget comes first — plus the inspection,
    # which on a cold start runs from inside the shutdown path (once per cluster).
    from app import cluster as cluster_mod
    from app.engine import shutdown_budget
    inspect_s = int(cluster_mod.INSPECT_BUDGET_S + cluster_mod.DEADLINE_GRACE_S)
    clustered = AppConfig(thresholds=th, hosts=[
        PveHostConfig(name="a", api_url="x", cluster=True)])
    # Without Ceph the guest stop cannot run (see _prepare_clusters: want_guests requires
    # want_ceph), so its five minutes are NOT charged. Counting them off the cluster tick
    # alone was enough on its own to push a plain three-node cluster past the default
    # 10-minute trigger and warn a correctly sized installation about its battery reserve.
    assert shutdown_budget_s(clustered) == 60 + inspect_s + stage
    # With it, the guest stop is real and counted — read as "any member asked for it",
    # the same way _cluster_switches() resolves the switch.
    with_ceph = AppConfig(thresholds=th, hosts=[
        PveHostConfig(name="a", api_url="x", cluster=True),
        PveHostConfig(name="b", api_url="x", cluster=True, cluster_ceph=True)])
    assert shutdown_budget(with_ceph).cluster_s == 60 + 300
    # A disabled Ceph member does not buy the term either.
    off = AppConfig(thresholds=th, hosts=[
        PveHostConfig(name="a", api_url="x", cluster=True),
        PveHostConfig(name="b", api_url="x", cluster=True, cluster_ceph=True,
                      enabled=False)])
    assert shutdown_budget(off).cluster_s == 60

    # A webhook that accepts the connection and then goes quiet costs real battery: _emit()
    # awaits the notification round, and several of them run in sequence before the last
    # stage. Counted only where a target is actually configured — and one round PER STAGE
    # on top of the fixed ones, because every stage emits its own "shutdown sent".
    from app.engine import NOTIFY_EVENTS_ON_PATH, shutdown_budget
    from app import notify as notify_mod
    from app.config import Notifications, WebhookConfig
    noisy = AppConfig(thresholds=th,
                      hosts=[PveHostConfig(name="a", api_url="x")],
                      notifications=Notifications(webhooks=[
                          WebhookConfig(id="w", enabled=True, url="https://hook")]))
    assert shutdown_budget_s(noisy) == (
        stage + int(notify_mod.NOTIFY_BUDGET_S) * (NOTIFY_EVENTS_ON_PATH + 1)
    )
    # And it grows with the stages rather than staying a constant: the shutdown is staged,
    # so the rounds are too.
    noisy_staged = AppConfig(thresholds=th,
                             hosts=[PveHostConfig(name="a", api_url="x", order=0),
                                    PveHostConfig(name="c", api_url="x", order=1)],
                             notifications=Notifications(webhooks=[
                                 WebhookConfig(id="w", enabled=True, url="https://hook")]))
    assert shutdown_budget(noisy_staged).notify_s == (
        int(notify_mod.NOTIFY_BUDGET_S) * (NOTIFY_EVENTS_ON_PATH + 2)
    )
    # A disabled or URL-less target costs nothing.
    quiet = AppConfig(thresholds=th,
                      hosts=[PveHostConfig(name="a", api_url="x")],
                      notifications=Notifications(webhooks=[
                          WebhookConfig(id="w", enabled=False, url="https://hook")]))
    assert shutdown_budget_s(quiet) == stage

    # The breakdown adds up to the total it is printed next to — the arithmetic in the
    # reserve warning used to quote a different stage count than the number above it.
    b = shutdown_budget(three)
    assert b.total == shutdown_budget_s(three)
    assert f"{b.stages} node stage(s)" in b.explain()


@pytest.mark.asyncio
async def test_the_guest_list_endpoint_serves_the_picker(_import_target, monkeypatch):
    """Picked from a list, never typed — so the list has to be reachable without first
    running a credential test."""
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch)

    result = await main.api_cluster_guests(_host_payload(cluster=True))

    assert result["ok"] is True
    assert [g["vmid"] for g in result["guests"]] == [100, 101, 950]
    assert result["guests"][2]["label"] == "CT 950 'pve-usv' on pve01"


@pytest.mark.asyncio
async def test_the_guest_list_endpoint_says_when_it_may_not_look(_import_target, monkeypatch):
    """An empty list and "not allowed to look" arrive identically on the wire; they must
    not read identically here."""
    main, _ = _import_target
    _fake_host_and_cluster(monkeypatch,
                           permissions={"Sys.PowerMgmt": 1, "Sys.Audit": 1})

    result = await main.api_cluster_guests(_host_payload(cluster=True))

    assert result["ok"] is False and result["guests"] == []
    assert "VM.Audit" in result["message"]


# --- a re-arm asks for a self-test -------------------------------------------


@pytest.mark.asyncio
async def test_a_re_arm_runs_a_self_test(monkeypatch):
    """Right after a re-arm is when a leftover problem shows up — an expired token, a
    cluster still prepared, a node that never came back. Waiting hours for the next
    scheduled slot to find that out is the wrong trade."""
    eng, _sent = _fired_engine(monkeypatch)
    runs: list = []

    async def fake_selftest(force_log=False):
        runs.append(force_log)

    eng._run_selftest = fake_selftest  # type: ignore[assignment]
    # Pin the schedule to "already done for this slot", so what the test observes is the
    # re-arm run and not the ordinary first-run-of-the-process one.
    from app.engine import selftest_slot
    eng.last_selftest_slot = selftest_slot(
        datetime.now(), eng.cfg.selftest_hour, eng.cfg.selftest_interval_min)
    slot_before = eng.last_selftest_slot

    _on_battery_low_runtime(eng, "a")
    await eng._evaluate()
    _on_mains(eng, "a")
    await eng._evaluate()
    eng._mains_ok_since = eng._mains_ok_since - timedelta(minutes=6)
    await eng._evaluate()
    await eng._maybe_selftest()

    assert runs == [True], "once, and logged even when everything is fine"
    assert eng.last_selftest_slot == slot_before, "the regular schedule is untouched"

    # And not again on the next iteration.
    await eng._maybe_selftest()
    assert runs == [True]


@pytest.mark.asyncio
async def test_a_re_arm_selftest_waits_out_a_new_outage_instead_of_being_lost(monkeypatch):
    """The flag is cleared when the test RUNS, not when it is set: a self-test costs up
    to ten seconds per host, which the next outage cannot spare."""
    eng, _sent = _fired_engine(monkeypatch)
    runs: list = []

    async def fake_selftest(force_log=False):
        runs.append(force_log)

    eng._run_selftest = fake_selftest  # type: ignore[assignment]

    _on_battery_low_runtime(eng, "a")
    await eng._evaluate()
    _on_mains(eng, "a")
    await eng._evaluate()
    eng._mains_ok_since = eng._mains_ok_since - timedelta(minutes=6)
    await eng._evaluate()

    # The grid dips again before the test got its turn.
    _on_battery_low_runtime(eng, "a")
    await eng._maybe_selftest()
    assert runs == [] and eng._rearm_selftest_pending is True

    _on_mains(eng, "a")
    eng.shutdown_triggered = False
    eng.ups_rt["a"].on_battery_since = None
    await eng._maybe_selftest()
    assert runs == [True]


# --- the cluster goes down as a unit -----------------------------------------
# The preparation is cluster-wide (HA disarmed, with Ceph every guest stopped) while the
# shutdown is per host. When only some nodes' UPS devices trigger, those two disagree and
# the cluster is left in halves -- observed in the field on a four-node cluster, where two
# of three Ceph monitors went down with it.


def _split_feed_engine(monkeypatch, srv, *, unit=True, ceph=True, nodes=4, dry_run=False):
    """Four cluster nodes on two UPS devices; only the first one is on battery."""
    from app import cluster
    from app.engine import Engine
    from app.config import ApplianceConfig, Thresholds

    monkeypatch.setattr(cluster.httpx, "AsyncClient", srv)
    monkeypatch.setattr(cluster, "_VERIFY_INTERVAL_S", 0.01)
    monkeypatch.setattr(cluster, "_GUEST_POLL_INTERVAL_S", 0.01)
    names = [f"pve0{i}" for i in range(1, nodes + 1)]
    half = len(names) // 2
    hosts = [
        PveHostConfig(name=n, api_url=f"https://{n}:8006", token_id="ups@pve!x",
                      token_secret="s", cluster=True, cluster_ceph=ceph,
                      cluster_shutdown_all=unit,
                      ups_ids=["a" if i < half else "b"], order=i)
        for i, n in enumerate(names)
    ]
    cfg = AppConfig(
        dry_run=dry_run,
        # ``label`` is a read-only property derived from ``name`` (UpsBase.label).
        ups=[SnmpConfig(id="a", host="10.0.0.9", name="UPS-A"),
             SnmpConfig(id="b", host="10.0.0.8", name="UPS-B")],
        hosts=hosts,
        appliance=ApplianceConfig(self_vmid=950, self_node="pve01"),
        thresholds=Thresholds(on_battery_low=True, on_battery_seconds=None,
                              runtime_below_minutes=None, charge_below_percent=None,
                              cluster_prep_timeout_s=5,
                              cluster_guest_shutdown_timeout_s=5),
    )
    eng = Engine(cfg)
    for h in hosts:
        eng.host_states[h.key] = {"cluster_name": "prod"}
    # Only the first UPS fails. The second stays on mains, exactly as in the field report.
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="battery",
                                     battery_status="low")
    eng.ups_rt["b"].state = UpsState(reachable=True, power_source="mains")
    return eng


def _fire_recorder(eng):
    fired: list = []

    async def fake_fire(host, reason):
        fired.append((host.name, reason))
        eng.host_fired[host.key] = True

    eng._fire_host = fake_fire  # type: ignore[assignment]
    return fired


def _srv4(**kw):
    nodes = ("pve01", "pve02", "pve03", "pve04")
    guests = [
        {"vmid": 950, "node": "pve01", "type": "lxc", "name": "pve-usv", "status": "running"},
        {"vmid": 100, "node": "pve02", "type": "qemu", "name": "web", "status": "running"},
        {"vmid": 101, "node": "pve04", "type": "lxc", "name": "db", "status": "running"},
    ]
    kw.setdefault("nodes", nodes)
    kw.setdefault("guests", guests)
    kw.setdefault("mons", ("pve01", "pve02", "pve03"))
    return _CephServer(**kw)


@pytest.mark.asyncio
async def test_one_ups_takes_the_whole_cluster_down(monkeypatch):
    """The field case: only UPS-A failed, so only two of four nodes were due -- while the
    preparation had already stopped every guest and disarmed HA for all four."""
    eng = _split_feed_engine(monkeypatch, _srv4())
    fired = _fire_recorder(eng)

    await eng._evaluate()

    assert [n for n, _ in fired] == ["pve01", "pve02", "pve03", "pve04"]
    # The two that asked for it name their UPS; the two taken along say so.
    assert "UPS-A" in dict(fired)["pve01"]
    assert "shut down as a unit" in dict(fired)["pve03"]
    assert "shut down as a unit" in dict(fired)["pve04"]


@pytest.mark.asyncio
async def test_a_node_without_the_cluster_switch_is_not_taken_along(monkeypatch):
    """"As a unit" may only cover the nodes that asked for cluster handling.

    _cluster_members() matched on the API's node list alone, so a node whose cluster
    switch was deliberately left off — its own UPS perfectly happy — was powered off by
    another node's outage. The warning meant to catch exactly that (_check_cluster_feeds)
    did filter on the switch and returned early below two members, so the one
    configuration in which the action over-reached was the one in which nothing said so.
    """
    eng = _split_feed_engine(monkeypatch, _srv4())
    # pve03 and pve04 stay in the cluster and in the API's node list, but opt out.
    for host in eng.cfg.hosts:
        if host.name in ("pve03", "pve04"):
            host.cluster = False
    fired = _fire_recorder(eng)

    await eng._evaluate()

    # Only the two that both opted in AND are fed by the failing UPS.
    assert [n for n, _ in fired] == ["pve01", "pve02"]
    assert eng.cluster_unit_hosts == {}


def test_cluster_switches_are_read_per_cluster_not_per_triggering_node(monkeypatch):
    """One answer per cluster, or the preview promises what the preparation skips.

    The preparation used to read the three switches off whichever candidate triggered
    first, while the shutdown preview and the scheduled health check read any(member) —
    so with the switches set unevenly the two disagreed, and which one was right depended
    on which node's UPS failed.
    """
    eng = _split_feed_engine(monkeypatch, _srv4(), ceph=False)
    members = list(eng.cfg.hosts)
    # (ceph, disarm, unit, guest_stop)
    assert eng._cluster_switches(members) == (False, True, True, False)

    # A single node asking for Ceph is enough: skipping a step that was asked for costs
    # the storage, running one nobody asked for costs a maintenance flag.
    members[3].cluster_ceph = True
    sw = eng._cluster_switches(members)
    assert sw.ceph is True
    # ... and Ceph always brings the guest stop along, whatever its own tick says.
    assert sw.guest_stop is False and sw.guests is True


@pytest.mark.asyncio
async def test_uneven_cluster_switches_are_reported(monkeypatch):
    """Half-on is not two settings but one contradiction, and it has to be said."""
    from app import cluster

    eng = _split_feed_engine(monkeypatch, _srv4())
    for host in eng.cfg.hosts[2:]:
        host.cluster_ha_disarm = False
    info = cluster.ClusterInfo(reachable=True, is_cluster=True, name="prod",
                               nodes=["pve01", "pve02", "pve03", "pve04"])
    events: list[tuple[str, str, str]] = []

    async def rec(subject, body, severity):
        events.append((subject, body, severity))

    eng._emit = rec  # type: ignore[assignment]

    n = await eng._check_cluster_switch_agreement("prod", eng._cluster_members(info, "prod"))

    assert n == 1
    subject, body, _sev = events[0]
    assert "'Disarm HA' is not set the same on every node" in subject
    # Both sides named, and what the appliance actually does with the contradiction.
    assert "On: pve01, pve02. Off: pve03, pve04." in body
    assert "at least one node asked for it" in body


@pytest.mark.asyncio
async def test_a_node_taken_along_by_its_cluster_is_retried_like_any_other(monkeypatch):
    """The retry has to reach the nodes that have no trigger of their own.

    They are the ones a cluster pulled in, and their eligibility comes from
    cluster_unit_hosts rather than from a UPS — so the branch that recognises them was
    skipping them before any retry was considered. _prepare_clusters() cannot offer them
    again either (its per-episode latch returns first), which left them with exactly one
    attempt while the event promised three."""
    eng = _split_feed_engine(monkeypatch, _srv4())
    attempts: list[str] = []

    async def flaky_fire(host, reason):
        attempts.append(host.name)
        # pve03 is a taken-along node: it fails once, then works.
        ok = host.name != "pve03" or attempts.count("pve03") > 1
        eng.host_fired[host.key] = ok
        eng.host_states.setdefault(host.key, {}).update({
            "shutdown_state": "sent" if ok else "failed",
            "shutdown_attempts": attempts.count(host.name),
        })

    eng._fire_host = flaky_fire  # type: ignore[assignment]

    await eng._evaluate()
    assert attempts.count("pve03") == 1
    assert eng.host_fired[eng.cfg.hosts[2].key] is False

    await eng._evaluate()
    assert attempts.count("pve03") == 2, "the next poll has to try it again"
    assert eng.host_fired[eng.cfg.hosts[2].key] is True
    # And nothing else is disturbed: the nodes that succeeded are not re-sent.
    assert attempts.count("pve01") == 1 and attempts.count("pve04") == 1


@pytest.mark.asyncio
async def test_a_taken_along_node_is_not_shut_down_once_mains_are_back(monkeypatch):
    """The retry must not outlive the outage that justified it.

    A node its cluster took along has no trigger of its own, so "no reason now" says
    nothing about it -- which is why the branch skips the release. But it also stopped
    asking whether the episode was still running at all: a taken-along node whose first
    attempt had failed was re-sent on every following poll, mains back or not. With every
    attempt of the round failing, that is a healthy production node powered off during
    normal operation."""
    from app import db

    eng = _split_feed_engine(monkeypatch, _srv4())
    events = _notify_recorder(eng)
    attempts: list[str] = []

    async def failing_fire(host, reason):
        attempts.append(host.name)
        eng.host_fired[host.key] = False
        eng.host_states.setdefault(host.key, {}).update({
            "shutdown_state": "failed",
            "shutdown_attempts": attempts.count(host.name),
        })

    eng._fire_host = failing_fire  # type: ignore[assignment]

    await eng._evaluate()
    assert sorted(set(attempts)) == ["pve01", "pve02", "pve03", "pve04"]

    # Mains come back between the first and the second attempt: nothing was ever sent, so
    # there is no half-down cluster for the taken-along nodes to follow.
    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="mains")
    events.clear()
    await eng._evaluate()

    assert attempts.count("pve03") == 1, "a healthy node must not be shut down with mains back"
    assert attempts.count("pve04") == 1
    key = eng.cfg.hosts[2].key
    assert eng.host_fired.get(key) is False
    assert "shutdown_state" not in eng.host_states[key], "the attempt record has to go too"
    assert key not in eng.cluster_unit_hosts, "nothing is left to take it along with"
    # And it is said out loud, at the level a machine that is still running deserves.
    aborted = [e for e in events if e[0] == "Host pve03: shutdown aborted"]
    assert aborted and aborted[0][1] == db.CRITICAL
    assert "never succeeded" in aborted[0][2]


@pytest.mark.asyncio
async def test_a_taken_along_node_still_follows_a_cluster_that_did_go_down(monkeypatch):
    """The other half of the rule above, and the reason it keys on "sent".

    When the nodes that triggered really were shut down, the cluster is half down and
    prepared -- HA disarmed, with Ceph every guest stopped -- so a taken-along node whose
    own attempt failed has to keep trying even after its UPS reports mains again. Refusing
    there would leave a node standing in a cluster with no guests and no storage."""
    eng = _split_feed_engine(monkeypatch, _srv4())
    attempts: list[str] = []

    async def flaky_fire(host, reason):
        attempts.append(host.name)
        ok = host.name != "pve03"
        eng.host_fired[host.key] = ok
        eng.host_states.setdefault(host.key, {}).update({
            "shutdown_state": "sent" if ok else "failed",
            "shutdown_attempts": attempts.count(host.name),
        })

    eng._fire_host = flaky_fire  # type: ignore[assignment]

    await eng._evaluate()
    assert attempts.count("pve03") == 1

    eng.ups_rt["a"].state = UpsState(reachable=True, power_source="mains")
    await eng._evaluate()
    assert attempts.count("pve03") == 2, "its cluster is down; it has to follow"


@pytest.mark.asyncio
async def test_a_raising_shutdown_does_not_orphan_a_taken_along_node(monkeypatch):
    """The defensive path has to leave a retryable state, not an unshuttable one.

    _fire_host is total in practice, but _fire_stage guards it anyway. That guard used to
    drop cluster_unit_hosts along with the latch — and for a node the cluster takes down as
    a unit, that entry is the ONLY record of why it is due: _host_trigger_reason returns
    None for it and _prepare_clusters cannot offer it again behind the per-episode latch.
    So the one host the guard was protecting was the one it left permanently unshut."""
    eng = _split_feed_engine(monkeypatch, _srv4())
    attempts: list[str] = []

    async def raising_fire(host, reason):
        attempts.append(host.name)
        if host.name == "pve03" and attempts.count("pve03") == 1:
            raise RuntimeError("boom")
        eng.host_fired[host.key] = True
        eng.host_states.setdefault(host.key, {}).update({"shutdown_state": "sent"})

    eng._fire_host = raising_fire  # type: ignore[assignment]

    await eng._evaluate()
    key = eng.cfg.hosts[2].key
    assert attempts.count("pve03") == 1
    assert eng.host_fired[key] is False
    assert key in eng.cluster_unit_hosts, "the reason is what makes the retry possible"

    await eng._evaluate()
    assert attempts.count("pve03") == 2, "the next poll has to try it again"
    assert eng.host_fired[key] is True


@pytest.mark.asyncio
async def test_the_unit_shutdown_keeps_the_configured_order(monkeypatch):
    """The added hosts are merged through ordered_hosts(), not appended: _evaluate_hosts
    groups by (this_host, order) and needs the list sorted for the groups to be
    contiguous."""
    eng = _split_feed_engine(monkeypatch, _srv4())
    # pve03 goes first, and the appliance's node is forced last whatever its order says.
    for h in eng.cfg.hosts:
        h.order = {"pve03": 0, "pve02": 1, "pve04": 2, "pve01": 3}[h.name]
        h.this_host = h.name == "pve01"
    fired = _fire_recorder(eng)

    await eng._evaluate()

    assert [n for n, _ in fired] == ["pve03", "pve02", "pve04", "pve01"]


@pytest.mark.asyncio
async def test_without_the_switch_only_the_triggered_nodes_go_and_it_is_said(monkeypatch):
    """The old behaviour stays available, but it no longer happens quietly."""
    srv = _srv4()
    eng = _split_feed_engine(monkeypatch, srv, unit=False)
    events = _notify_recorder(eng)
    fired = _fire_recorder(eng)

    await eng._evaluate()

    assert [n for n, _ in fired] == ["pve01", "pve02"]
    subjects = [s for s, _, _ in events]
    assert "Cluster prod: only part of the cluster is shut down" in subjects
    body = next(b for s, _, b in events if s.endswith("only part of the cluster is shut down"))
    assert "2 of 4 nodes triggered" in body
    assert "pve03" in body and "pve04" in body
    # The guests were stopped for all of them regardless -- that is the point.
    assert srv.guests[101]["status"] == "stopped"


@pytest.mark.asyncio
async def test_the_announcement_names_how_many_nodes_triggered(monkeypatch):
    eng = _split_feed_engine(monkeypatch, _srv4())
    events = _notify_recorder(eng)
    _fire_recorder(eng)

    await eng._evaluate()

    body = next(b for s, _, b in events if s.endswith("preparing for shutdown"))
    assert "2 of 4 nodes triggered" in body
    assert "the rest are shut down with them" in body


@pytest.mark.asyncio
async def test_members_are_found_without_a_previous_self_test(monkeypatch):
    """Cold start: nothing has filled cluster_name yet, so membership has to come from
    the node list the API just returned."""
    eng = _split_feed_engine(monkeypatch, _srv4())
    eng.host_states = {}
    fired = _fire_recorder(eng)

    await eng._evaluate()

    assert sorted(n for n, _ in fired) == ["pve01", "pve02", "pve03", "pve04"]


@pytest.mark.asyncio
async def test_disabled_hosts_are_not_taken_along(monkeypatch):
    eng = _split_feed_engine(monkeypatch, _srv4())
    next(h for h in eng.cfg.hosts if h.name == "pve04").enabled = False
    fired = _fire_recorder(eng)

    await eng._evaluate()

    assert [n for n, _ in fired] == ["pve01", "pve02", "pve03"]


@pytest.mark.asyncio
async def test_a_host_taken_along_is_not_reported_as_aborted(monkeypatch):
    """It never had a reason of its own, so "no reason now" says nothing about it.
    Without the unit latch the dry-run released it again on the very next poll, with a
    misleading "shutdown aborted" -- and it could never fire again."""
    eng = _split_feed_engine(monkeypatch, _srv4(), dry_run=True)
    events = _notify_recorder(eng)

    await eng._evaluate()
    before = len(events)
    await eng._evaluate()

    assert not [s for s, _, _ in events[before:] if "aborted" in s]
    assert eng.host_fired[next(h.key for h in eng.cfg.hosts if h.name == "pve03")]


@pytest.mark.asyncio
async def test_the_selftest_warns_about_independent_ups_branches(monkeypatch):
    eng = _split_feed_engine(monkeypatch, _srv4())
    events = _notify_recorder(eng)

    await eng._check_clusters()

    body = next((b for s, _, b in events if "different UPS devices" in s), "")
    assert "UPS-A" in body and "UPS-B" in body
    assert "shut down the whole cluster" in body
    # The other half of the field case: a UPS that goes silent triggers nothing at all.
    assert "communication loss" in body


@pytest.mark.asyncio
async def test_the_feed_warning_changes_with_the_switch(monkeypatch):
    eng = _split_feed_engine(monkeypatch, _srv4(), unit=False)
    events = _notify_recorder(eng)

    await eng._check_clusters()

    body = next((b for s, _, b in events if "different UPS devices" in s), "")
    assert "stopped every guest in the cluster" in body
    assert "Switch 'shut the whole cluster down as a unit' on" in body


@pytest.mark.asyncio
async def test_no_feed_warning_when_every_node_hangs_on_the_same_ups(monkeypatch):
    srv = _CephServer()
    eng = _outage_cluster_engine(monkeypatch, srv)
    events = _notify_recorder(eng)

    await eng._check_clusters()

    assert not [s for s, _, _ in events if "different UPS devices" in s]


@pytest.mark.asyncio
async def test_abort_on_failure_also_holds_back_the_taken_along_nodes(monkeypatch):
    """The abort filter keys on host_states["cluster_name"]. On a cold start the
    taken-along nodes were matched through the API's node list instead, so without
    recording that membership they would have gone down despite the abort."""
    srv = _srv4(bulk_supported=False, guest_ignores_shutdown=(100, 101))
    eng = _split_feed_engine(monkeypatch, srv)
    eng.cfg.thresholds.cluster_abort_on_prep_failure = True
    eng.cfg.thresholds.cluster_guest_force_after_s = None
    eng.host_states = {}          # cold start: nothing knows the cluster name yet
    fired = _fire_recorder(eng)

    await eng._evaluate()

    assert eng.cluster_prep_failed["prod"] is True
    assert fired == [], "a held-back cluster shuts down no node of it, taken-along or not"

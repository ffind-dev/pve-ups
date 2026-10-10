"""Per-UPS history (#37): storage, queries, the engine's sampler and the endpoints."""

from __future__ import annotations

import asyncio
import time

import pytest
import yaml

from app import history
from app.config import AppConfig, HistoryConfig, SnmpConfig, load_config
from app.engine import Engine
from app.ups import UpsState


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from app import engine as engine_mod

    monkeypatch.setattr(engine_mod, "STATE_PATH", tmp_path / "engine-state.json")
    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "history.db")


def _row(ts, state="mains", on_battery=0, triggered=0, runtime=30, charge=100, load=40,
         ups="u"):
    return (ups, ts, state, on_battery, triggered, runtime, charge, load)


# --- configuration: on for a new install, off after an update -----------------
def test_a_new_installation_records_the_history(tmp_path):
    assert load_config(tmp_path / "missing.yaml").history.enabled is True


def test_an_existing_config_without_history_gets_it_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"dry_run": False}), encoding="utf-8")

    assert load_config(path).history.enabled is False


def test_a_stored_history_setting_survives_loading(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"history": {"enabled": True, "retention_days": 14}}),
                    encoding="utf-8")

    cfg = load_config(path)

    assert cfg.history.enabled is True and cfg.history.retention_days == 14


def test_an_out_of_range_retention_is_corrected_not_refused():
    cfg = AppConfig(history=HistoryConfig(retention_days=5000))

    assert cfg.history.retention_days == 90
    assert any(c.startswith("history:") for c in cfg.value_corrections())


def test_a_form_without_the_history_section_keeps_the_stored_one():
    """A cached app.js from before the history existed must not switch it back on/off."""
    from app import main

    existing = AppConfig(history=HistoryConfig(enabled=False, retention_days=30))
    merged = main._merge_config({"dry_run": True}, existing)

    assert merged.history.enabled is False and merged.history.retention_days == 30


# --- storage and queries --------------------------------------------------------
def test_points_are_raw_for_short_ranges_and_bucketed_for_long_ones():
    now = 1_800_000_000
    history.write([_row(now - 3600 + i * 60, runtime=30 - (i % 7)) for i in range(60)])

    short = history.query([("u", "UPS")], now - 3600, now, now=now)
    assert short["bucket_s"] == 0
    assert len(short["ups"][0]["points"]) == 60

    long = history.query([("u", "UPS")], now - 90 * 86400, now, now=now)
    assert long["bucket_s"] > 0
    pts = long["ups"][0]["points"]
    assert len(pts) <= history.MAX_POINTS
    # MIN, not an average: the dip is what the chart is for.
    assert min(p[1] for p in pts) == 24


def test_segments_and_outages_follow_the_state_changes():
    now = 1_800_000_000
    t0 = now - 1800
    rows = [_row(t0 + i * 60) for i in range(5)]                      # mains
    rows += [_row(t0 + 300 + i * 8, "battery", 1, runtime=20 - i, charge=90 - i, load=45 + i)
             for i in range(10)]                                      # outage
    rows += [_row(t0 + 380 + i * 8, "battery", 1, 1, runtime=9, charge=70) for i in range(3)]
    rows += [_row(t0 + 420 + i * 60, "mains") for i in range(20)]     # back
    history.write(rows)

    out = history.query([("u", "UPS")], now - 3600, now, now=now)
    kinds = [s[2] for s in out["ups"][0]["segments"]]

    assert kinds == ["mains", "battery", "triggered", "mains"]
    (ep,) = out["outages"]
    assert ep["start"] == t0 + 300 and ep["end"] == t0 + 420
    assert ep["min_runtime"] == 9 and ep["min_charge"] == 70 and ep["max_load"] == 54
    assert ep["triggered"] is True and ep["ongoing"] is False


def test_a_zoom_into_part_of_an_outage_still_reports_it_whole():
    """Found on screen: zoomed onto one UPS's outage, the overlapping outage of another
    showed up cut to the visible window — shorter and without its trigger."""
    now = 1_800_000_000
    t0 = now - 3600
    rows = [_row(t0 - 60)]
    rows += [_row(t0 + i * 8, "battery", 1, int(i > 150), runtime=30 - i // 10)
             for i in range(200)]
    rows += [_row(t0 + 1600 + i * 60) for i in range(5)]
    history.write(rows)

    out = history.query([("u", "UPS")], t0 + 300, t0 + 600, now=now)
    (ep,) = out["outages"]

    assert ep["start"] == t0 and ep["end"] == t0 + 1600
    assert ep["triggered"] is True and ep["min_runtime"] == 11
    # The drawn stretches stay inside the window, though.
    assert all(t0 + 300 <= a <= b <= t0 + 600 for a, b, _ in out["ups"][0]["segments"])


def test_a_pause_in_the_record_is_no_data_not_a_long_stretch():
    now = 1_800_000_000
    history.write([_row(now - 7200), _row(now - 7140), _row(now - 600), _row(now - 540)])

    segs = history.query([("u", "UPS")], now - 7200, now, now=now)["ups"][0]["segments"]

    assert len(segs) == 2
    assert segs[0][1] == now - 7140      # ends at its last sample, not at the next one
    assert segs[1][0] == now - 600


def test_an_outage_that_is_still_running_is_marked_ongoing():
    now = 1_800_000_000
    history.write([_row(now - 120), _row(now - 60, "battery", 1), _row(now - 10, "battery", 1)])

    (ep,) = history.query([("u", "UPS")], now - 3600, now, now=now)["outages"]

    assert ep["ongoing"] is True and ep["end"] == now


def test_prune_drops_what_is_older_than_the_retention():
    now = int(time.time())
    history.write([_row(now - 10 * 86400), _row(now - 3600)])

    history.write([], prune_days=7)

    assert history.info()["rows"] == 1


def test_clear_empties_the_history():
    history.write([_row(int(time.time()))])

    assert history.clear() == 1
    assert history.info()["rows"] == 0


def test_the_range_never_reaches_before_the_retention():
    now = 1_800_000_000
    s, e = history.resolve_range("90d", None, None, retention_days=7, now=now)

    assert (s, e) == (now - 7 * 86400, now)
    # An explicit zoom wins over the named range.
    assert history.resolve_range("24h", now - 600, now - 300, 90, now=now) == (
        now - 600, now - 300)


def test_a_typed_period_is_clamped_to_what_can_exist():
    """The Von/Bis fields send whatever was typed: an end in the future becomes "now",
    a start before the retention becomes its limit — and the fields then show that."""
    now = 1_800_000_000
    s, e = history.resolve_range(None, now - 400 * 86400, now + 3600, retention_days=30,
                                 now=now)

    assert (s, e) == (now - 30 * 86400, now)


def test_csv_export_lists_raw_rows_with_names():
    now = 1_800_000_000
    history.write([_row(now - 60), _row(now - 30, "unreachable", runtime=None, charge=None,
                                       load=None)])

    text = history.csv_export([("u", "Rack A")], now - 3600, now)
    lines = text.strip().splitlines()

    assert lines[0].startswith("time_utc,ups_id,ups_name,state")
    assert len(lines) == 3
    assert ",Rack A,unreachable,0,0,,," in lines[2]


# --- the engine's sampler -------------------------------------------------------
def _engine(enabled=True):
    cfg = AppConfig(ups=[SnmpConfig(id="u", host="10.0.0.9")],
                    history=HistoryConfig(enabled=enabled))
    return Engine(cfg)


async def _drain(eng):
    if eng._history_task is not None:
        await eng._history_task


@pytest.mark.asyncio
async def test_the_sampler_writes_once_a_minute_on_mains_and_on_every_change():
    eng = _engine()
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains",
                                     runtime_remaining_min=30, last_poll=None, error=None)
    eng.ups_rt["u"].state.last_poll = eng.started_at

    eng._maybe_record_history()
    await _drain(eng)
    eng._maybe_record_history()            # same state, same minute: nothing new
    await _drain(eng)
    assert history.info()["rows"] == 1

    eng.ups_rt["u"].state.power_source = "battery"
    eng._maybe_record_history()            # a change is written at once
    await _drain(eng)
    eng._maybe_record_history()            # on battery: every poll
    await _drain(eng)
    assert history.info()["rows"] == 3


@pytest.mark.asyncio
async def test_a_switched_off_history_records_nothing():
    eng = _engine(enabled=False)
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    eng.ups_rt["u"].state.last_poll = eng.started_at

    eng._maybe_record_history()
    await _drain(eng)

    assert history.info()["rows"] == 0


@pytest.mark.asyncio
async def test_a_hanging_writer_never_blocks_the_sampler(monkeypatch):
    """The disk may hang; the loop must not. Rows queue up (bounded) instead."""
    import threading

    from app import engine as engine_mod

    release = threading.Event()
    monkeypatch.setattr(history, "write", lambda rows, prune=None: release.wait(5))
    monkeypatch.setattr(engine_mod, "HISTORY_BUFFER_MAX", 3)
    eng = _engine()
    st = eng.ups_rt["u"].state = UpsState(reachable=True, power_source="battery")
    st.last_poll = eng.started_at

    started = time.monotonic()
    for _ in range(10):
        eng._maybe_record_history()
        await asyncio.sleep(0)
    assert time.monotonic() - started < 1.0
    assert len(eng._history_buf) <= 3
    release.set()
    await _drain(eng)


@pytest.mark.asyncio
async def test_a_failing_writer_does_not_raise_into_the_loop(monkeypatch):
    def boom(rows, prune=None):
        raise OSError("disk full")

    monkeypatch.setattr(history, "write", boom)
    eng = _engine()
    eng.ups_rt["u"].state = UpsState(reachable=True, power_source="mains")
    eng.ups_rt["u"].state.last_poll = eng.started_at

    eng._maybe_record_history()
    await _drain(eng)

    assert eng._history_failing is True


# --- endpoints ----------------------------------------------------------------------
@pytest.fixture
def _main(monkeypatch):
    from app import main

    eng = _engine()
    monkeypatch.setattr(main, "engine", eng)
    monkeypatch.setattr(main.db, "events_between", lambda *a, **k: [
        {"ts": "2027-01-15T08:00:00+00:00", "severity": "warning",
         "event": "Power outage", "detail": "UPS on battery"}])
    return main


def test_history_endpoint_answers_disabled_when_switched_off(_main):
    _main.engine.cfg.history.enabled = False

    assert _main.api_history(range_key="24h", start=None, end=None) == {"enabled": False}


def test_history_endpoint_carries_points_threshold_and_events(_main):
    now = int(time.time())
    history.write([_row(now - 120), _row(now - 60)])

    out = _main.api_history(range_key="1h", start=None, end=None)

    assert out["enabled"] is True
    (ups,) = out["ups"]
    assert ups["id"] == "u" and len(ups["points"]) == 2
    assert ups["threshold_runtime_min"] == _main.engine.cfg.thresholds.runtime_below_minutes
    assert out["events"][0]["event"] == "Power outage"
    assert isinstance(out["events"][0]["ts"], int)


def test_history_csv_is_a_download(_main):
    history.write([_row(int(time.time()) - 60)])

    resp = _main.api_history_csv(range_key="1h", start=None, end=None)

    assert resp.media_type.startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]


def test_history_endpoints_require_a_login(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setattr(main, "engine", Engine(AppConfig(ui_password_hash="x")))
    client = TestClient(main.app)
    for method, url in (("get", "/api/history"), ("get", "/api/history.csv"),
                        ("get", "/api/history/info"), ("delete", "/api/history")):
        assert getattr(client, method)(url).status_code == 401, url

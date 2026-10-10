"""Per-UPS history for the History tab (#37): a small SQLite time series.

One row per UPS and sample: what the UPS reported (power source, runtime, charge, load)
and what the engine made of it (on battery, triggered). The engine decides WHEN to sample
(Engine._maybe_record_history) and hands the rows over through a background thread; this
module only stores and reads them.

Kept in a file of its own, next to the event log, for three reasons: its write rate and
its size are of a different order (about 1,440 rows per UPS and day), "Clear log" must
not take the history with it, and a damaged or locked history file must never be able
to touch the event log the shutdown path writes to.

Everything here is synchronous and opens its own connection per call, like db.py. The
engine never calls it on the event loop, and nothing on the shutdown path waits for it.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import db

HISTORY_PATH = db.DB_PATH.parent / "history.db"

# What a sample's ``state`` column may hold: the UPS's own power source as UpsState
# normalises it, plus "unreachable" for a poll that got no usable answer.
STATES = ("mains", "battery", "bypass", "none", "other", "unknown", "unreachable")

# The time ranges the History tab offers, in seconds. "all" goes back to the oldest row.
RANGES = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
    "90d": 90 * 86400,
}

# The chart cannot show more than a few hundred points per series anyway; anything finer
# is bucketed on the server, so a 90-day range is not 130,000 points per UPS on the wire.
MAX_POINTS = 600

# A pause between two samples longer than this is "no data" (appliance down, history
# switched off), not a long stretch of whatever came before. Regular samples are at most
# a minute apart, so five minutes leaves room for a slow poll without drawing a gap.
GAP_S = 300

# How far beyond the range an outage that crosses its edge is followed, so the outage
# table reports it whole (a zoom into the middle of an outage still shows its real start,
# length and whether it triggered). Bounded, so a malformed record cannot make one
# request walk the whole file.
EPISODE_REACH_S = 2 * 86400

# Caps for one answer: a range packed with events must not turn the History tab into a
# multi-megabyte response.
MAX_EVENTS = 1000
MAX_OUTAGES = 500


def _path(path: Optional[Path]) -> Path:
    # Resolved at call time, not as a default argument: tests (and anything else) can
    # point HISTORY_PATH elsewhere after import.
    return path if path is not None else HISTORY_PATH


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            ups_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            state TEXT NOT NULL,
            on_battery INTEGER NOT NULL DEFAULT 0,
            triggered INTEGER NOT NULL DEFAULT 0,
            runtime_min INTEGER,
            charge_pct INTEGER,
            load_pct INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS samples_ups_ts ON samples (ups_id, ts)")
    return conn


def write(rows: Iterable[tuple], prune_days: Optional[int] = None,
          path: Optional[Path] = None) -> None:
    """Append samples and, if asked, drop everything older than ``prune_days``.

    A row is (ups_id, ts, state, on_battery, triggered, runtime_min, charge_pct,
    load_pct). One transaction for the whole batch: one fsync per write, not per row.
    """
    rows = list(rows)
    if not rows and prune_days is None:
        return
    with _connect(path) as conn:
        if rows:
            conn.executemany(
                "INSERT INTO samples (ups_id, ts, state, on_battery, triggered, "
                "runtime_min, charge_pct, load_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        if prune_days is not None:
            conn.execute(
                "DELETE FROM samples WHERE ts < ?", (int(time.time()) - prune_days * 86400,)
            )
        conn.commit()


def clear(path: Optional[Path] = None) -> int:
    """Delete the whole history. Returns the number of rows removed."""
    with _connect(path) as conn:
        cur = conn.execute("DELETE FROM samples")
        conn.commit()
        removed = cur.rowcount
    # Give the space back: unlike the daily prune, which frees pages for the next rows,
    # "clear" is usually asked for because the disk space is wanted.
    with _connect(path) as conn:
        conn.execute("VACUUM")
    return removed


def info(path: Optional[Path] = None) -> dict:
    """Size on disk, row count and the oldest sample, for the settings page."""
    p = _path(path)
    size = 0
    for f in (p, p.with_name(p.name + "-journal")):
        try:
            size += f.stat().st_size
        except OSError:
            pass
    if not p.exists():
        return {"size_bytes": 0, "rows": 0, "oldest": None}
    with _connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n, MIN(ts) AS oldest FROM samples").fetchone()
    return {"size_bytes": size, "rows": row["n"], "oldest": row["oldest"]}


def resolve_range(range_key: Optional[str], start: Optional[int], end: Optional[int],
                  retention_days: int, now: Optional[int] = None,
                  oldest: Optional[int] = None) -> tuple[int, int]:
    """The (start, end) a request asks for, in epoch seconds, clamped to what can exist.

    Explicit ``start``/``end`` (a zoom) win over the named range. Nothing before the
    retention window is asked for: it has been pruned, and showing it as "no data" would
    read like an outage of the appliance.
    """
    now = int(now if now is not None else time.time())
    floor = now - retention_days * 86400
    if start is not None or end is not None:
        e = min(int(end) if end is not None else now, now)
        s = int(start) if start is not None else e - RANGES["24h"]
    else:
        e = now
        if range_key == "all":
            s = oldest if oldest is not None else now - RANGES["24h"]
        else:
            s = now - RANGES.get(range_key or "24h", RANGES["24h"])
    s = max(s, floor)
    if e - s < 60:  # a zoom narrower than a minute shows nothing useful
        s = e - 60
    return s, e


def _bucket_s(start: int, end: int) -> int:
    """Bucket width for at most MAX_POINTS points; raw samples (0) where they fit."""
    span = max(1, end - start)
    if span <= MAX_POINTS * 60:  # up to 10 h: at most one regular sample per minute
        return 0
    return int(math.ceil(span / MAX_POINTS))


def _points(conn: sqlite3.Connection, ups_id: str, start: int, end: int,
            bucket: int) -> list[list]:
    """[ts, runtime_min, charge_pct, load_pct] per point, oldest first.

    Bucketed with MIN for runtime and charge and MAX for load: the chart exists to show
    how close an outage came, and an average would smooth exactly that dip away.
    """
    if bucket <= 0:
        rows = conn.execute(
            "SELECT ts, runtime_min, charge_pct, load_pct FROM samples "
            "WHERE ups_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (ups_id, start, end),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT MIN(ts) AS ts, MIN(runtime_min) AS runtime_min, "
            "MIN(charge_pct) AS charge_pct, MAX(load_pct) AS load_pct FROM samples "
            "WHERE ups_id = ? AND ts BETWEEN ? AND ? "
            "GROUP BY (ts - ?) / ? ORDER BY ts",
            (ups_id, start, end, start, bucket),
        ).fetchall()
    return [[r["ts"], r["runtime_min"], r["charge_pct"], r["load_pct"]] for r in rows]


def _kind(state: str, on_battery: int, triggered: int) -> str:
    """What a stretch of samples is drawn as. Triggered wins, then the UPS's own word."""
    if triggered:
        return "triggered"
    if state == "unreachable":
        return "unreachable"
    if state == "battery" or (on_battery and state not in ("mains", "bypass")):
        return "battery"
    if state == "bypass":
        return "bypass"
    if state == "mains":
        return "mains"
    return "other"


def _segments(conn: sqlite3.Connection, ups_id: str, start: int, end: int,
              now: int) -> tuple[list[list], list[dict]]:
    """Exact state stretches ([from, to, kind]) and battery episodes, from the rows.

    Walked in Python over the rows of the range: up to ~130,000 per UPS for 90 days, a
    fraction of a second in the thread pool this runs in — and the only way to get the
    stretches exact rather than bucket-sized, which the "when did it switch" question
    needs.

    The walk starts at the last sample OFF battery before ``start`` and ends at the first
    one after ``end`` (each at most EPISODE_REACH_S away), so a range that begins or ends
    in the middle of an outage neither starts with a blank nor reports a truncated
    outage. The stretches are clipped to the range afterwards; an outage is kept whole
    if any part of it falls inside.
    """
    before = conn.execute(
        "SELECT MAX(ts) AS ts FROM samples "
        "WHERE ups_id = ? AND ts < ? AND ts >= ? AND on_battery = 0",
        (ups_id, start, start - EPISODE_REACH_S),
    ).fetchone()["ts"]
    after = conn.execute(
        "SELECT MIN(ts) AS ts FROM samples "
        "WHERE ups_id = ? AND ts > ? AND ts <= ? AND on_battery = 0",
        (ups_id, end, end + EPISODE_REACH_S),
    ).fetchone()["ts"]
    rows = conn.execute(
        "SELECT ts, state, on_battery, triggered, runtime_min, charge_pct, load_pct "
        "FROM samples WHERE ups_id = ? AND ts BETWEEN ? AND ? ORDER BY ts",
        (ups_id,
         before if before is not None else start - GAP_S,
         after if after is not None else min(now, end + EPISODE_REACH_S)),
    ).fetchall()

    segments: list[list] = []
    outages: list[dict] = []
    cur: Optional[list] = None   # [from, to, kind]
    ep: Optional[dict] = None    # the battery episode being collected
    last_ts: Optional[int] = None

    def close_episode(at: int, ongoing: bool) -> None:
        nonlocal ep
        if ep is None:
            return
        ep["end"] = at
        ep["duration_s"] = max(0, at - ep["start"])
        ep["ongoing"] = ongoing
        outages.append(ep)
        ep = None

    for r in rows:
        ts = r["ts"]
        kind = _kind(r["state"], r["on_battery"], r["triggered"])
        if last_ts is not None and ts - last_ts > GAP_S:
            # A hole in the record: close what was open at the last sample, and do not
            # pretend to know how an outage went across it.
            if cur is not None:
                cur[1] = last_ts
                segments.append(cur)
                cur = None
            close_episode(last_ts, False)
        if cur is None or cur[2] != kind:
            if cur is not None:
                cur[1] = ts
                segments.append(cur)
            cur = [ts, ts, kind]
        else:
            cur[1] = ts

        if r["on_battery"]:
            if ep is None:
                ep = {"start": ts, "min_runtime": None, "min_charge": None,
                      "max_load": None, "triggered": False}
            for key, col, pick in (("min_runtime", "runtime_min", min),
                                   ("min_charge", "charge_pct", min),
                                   ("max_load", "load_pct", max)):
                v = r[col]
                if v is not None:
                    ep[key] = v if ep[key] is None else pick(ep[key], v)
            ep["triggered"] = ep["triggered"] or bool(r["triggered"])
        else:
            close_episode(ts, False)
        last_ts = ts

    if cur is not None and last_ts is not None:
        # Still current: stretch to "now" while samples are fresh; an old last sample
        # ends where the record ends.
        live = now - last_ts <= GAP_S
        cur[1] = now if live else last_ts
        segments.append(cur)
        close_episode(cur[1], live and ep is not None)
    segments = [
        [max(a, start), min(b, end), kind]
        for a, b, kind in segments
        if b >= start and a <= end
    ]
    outages = [ep for ep in outages if ep["end"] >= start and ep["start"] <= end]
    return segments, outages


def query(ups: list[tuple[str, str]], start: int, end: int,
          path: Optional[Path] = None, now: Optional[int] = None) -> dict:
    """Everything the History tab draws for ``ups`` ([(id, name)]) between start and end."""
    now = int(now if now is not None else time.time())
    bucket = _bucket_s(start, end)
    out: dict = {"from": start, "to": end, "bucket_s": bucket, "ups": [], "outages": []}
    p = _path(path)
    if not p.exists():
        for ups_id, name in ups:
            out["ups"].append({"id": ups_id, "name": name, "points": [], "segments": []})
        return out
    with _connect(path) as conn:
        for ups_id, name in ups:
            segments, outages = _segments(conn, ups_id, start, end, now)
            out["ups"].append({
                "id": ups_id,
                "name": name,
                "points": _points(conn, ups_id, start, end, bucket),
                "segments": segments,
            })
            for ep in outages:
                out["outages"].append({"ups_id": ups_id, "name": name, **ep})
    out["outages"].sort(key=lambda e: e["start"], reverse=True)
    del out["outages"][MAX_OUTAGES:]
    return out


def csv_export(ups: list[tuple[str, str]], start: int, end: int,
               path: Optional[Path] = None) -> str:
    """Raw samples of the range as CSV, one row per sample, oldest first."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", "ups_id", "ups_name", "state", "on_battery", "triggered",
                "runtime_min", "charge_pct", "load_pct"])
    if not _path(path).exists():
        return buf.getvalue()
    names = dict(ups)
    ids = list(names)
    if not ids:
        return buf.getvalue()
    marks = ",".join("?" for _ in ids)
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ups_id, ts, state, on_battery, triggered, runtime_min, charge_pct, "
            f"load_pct FROM samples WHERE ups_id IN ({marks}) AND ts BETWEEN ? AND ? "
            "ORDER BY ts, ups_id",
            (*ids, start, end),
        )
        for r in rows:
            w.writerow([
                datetime.fromtimestamp(r["ts"], timezone.utc).isoformat(),
                r["ups_id"], names.get(r["ups_id"], ""), r["state"], r["on_battery"],
                r["triggered"], _blank(r["runtime_min"]), _blank(r["charge_pct"]),
                _blank(r["load_pct"]),
            ])
    return buf.getvalue()


def _blank(value) -> str:
    return "" if value is None else str(value)

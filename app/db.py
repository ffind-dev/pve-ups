"""Tiny SQLite event log. Not user-editable; only the engine/UI append and read it.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.environ.get("PVE_USV_DB", "/var/lib/pve-usv/events.db"))

# Severities used by the UI for colour coding.
INFO = "info"
WARNING = "warning"
CRITICAL = "critical"


def _connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: Path = DB_PATH) -> None:
    with _connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                severity TEXT NOT NULL,
                event TEXT NOT NULL,
                detail TEXT
            )
            """
        )
        conn.commit()


def log_event(event: str, detail: str = "", severity: str = INFO, path: Path = DB_PATH) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO events (ts, severity, event, detail) VALUES (?, ?, ?, ?)",
            (ts, severity, event, detail),
        )
        conn.commit()


def recent_events(limit: int = 100, path: Path = DB_PATH) -> list[dict]:
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, severity, event, detail FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def events_since(hours: int = 48, limit: int = 500, path: Path = DB_PATH) -> list[dict]:
    """Events from the last ``hours`` (newest first, capped at ``limit``).

    Timestamps are stored as ISO-8601 UTC strings, which sort lexicographically, so a
    plain string comparison against the cutoff is correct and index-friendly.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT ts, severity, event, detail FROM events "
            "WHERE ts >= ? ORDER BY id DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def severity_counts_since(hours: int = 48, path: Path = DB_PATH) -> dict:
    """Count events per severity in the last ``hours`` (accurate regardless of any cap)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT severity, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY severity",
            (cutoff,),
        ).fetchall()
    counts = {INFO: 0, WARNING: 0, CRITICAL: 0}
    for r in rows:
        counts[r["severity"]] = r["n"]
    return counts


def clear_events(path: Path = DB_PATH) -> int:
    """Delete the whole event log (UI 'clear log' action). Returns rows removed."""
    with _connect(path) as conn:
        cur = conn.execute("DELETE FROM events")
        conn.commit()
        return cur.rowcount


def prune(keep: int = 5000, path: Path = DB_PATH) -> None:
    """Keep the table bounded."""
    with _connect(path) as conn:
        conn.execute(
            "DELETE FROM events WHERE id NOT IN "
            "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
            (keep,),
        )
        conn.commit()

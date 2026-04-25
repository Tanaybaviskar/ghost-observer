"""
ghost/_storage.py

SQLite persistence layer + background flush thread.

Schema
------
sessions   — one row per ghost run invocation
events     — raw profile tuples, indexed by fn_key and session
profiles   — aggregated per-function stats, updated by the aggregator

The flush thread drains the in-memory buffer every N seconds (adaptive:
faster under load) and writes rows in a single transaction per batch.
N defaults to 5s but drops to 1s when a batch exceeds 5 000 events.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ghost._observer import drain_buffer

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _db_dir() -> Path:
    base = Path(os.environ.get("GHOST_DATA_DIR", Path.home() / ".ghost"))
    sessions_dir = base / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


def session_db_path(session_id: str) -> Path:
    return _db_dir() / f"{session_id}.db"


def list_sessions() -> list[str]:
    """Return session IDs sorted newest-first.

    Sort by the unix timestamp embedded in the session ID (format: <ts>-<hex>)
    rather than file mtime, which can be perturbed by read-only DB opens.
    """
    d = _db_dir()
    stems = [p.stem for p in d.glob("*.db")]

    def _ts(sid: str) -> int:
        try:
            return int(sid.split("-")[0])
        except (ValueError, IndexError):
            return 0

    return sorted(stems, key=_ts, reverse=True)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    script_path  TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    started_at   INTEGER NOT NULL,   -- unix epoch ms
    ended_at     INTEGER,
    total_events INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    fn_key       TEXT NOT NULL,
    event        TEXT NOT NULL,
    arg_types    TEXT NOT NULL,      -- JSON array of type-name strings
    ret_or_exc   TEXT,
    is_exception INTEGER NOT NULL DEFAULT 0,
    timestamp_ns INTEGER NOT NULL,
    caller_key   TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_events_fn_key    ON events(fn_key);
CREATE INDEX IF NOT EXISTS idx_events_session   ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_fn_sess   ON events(fn_key, session_id);

CREATE TABLE IF NOT EXISTS profiles (
    session_id      TEXT NOT NULL,
    fn_key          TEXT NOT NULL,
    call_count      INTEGER NOT NULL DEFAULT 0,
    exception_count INTEGER NOT NULL DEFAULT 0,
    arg_type_dist   TEXT NOT NULL DEFAULT '{}',  -- JSON {sig: count}
    ret_type_dist   TEXT NOT NULL DEFAULT '{}',  -- JSON {type: count}
    callers         TEXT NOT NULL DEFAULT '{}',  -- JSON {caller_key: count}
    callees         TEXT NOT NULL DEFAULT '{}',  -- JSON {callee_key: count}
    total_latency_ns INTEGER NOT NULL DEFAULT 0,
    min_latency_ns   INTEGER,
    max_latency_ns   INTEGER,
    PRIMARY KEY (session_id, fn_key)
);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def open_db(session_id: str, read_only: bool = False) -> sqlite3.Connection:
    """Open the session DB.

    read_only=True skips the DDL commit so we don't bump the file mtime
    (which would corrupt the mtime-based session ordering on some platforms).
    The DB must already exist when read_only=True.
    """
    path = session_db_path(session_id)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    if not read_only:
        conn.executescript(_DDL)
        conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def insert_session(conn: sqlite3.Connection, session_id: str,
                   script_path: str, pid: int) -> None:
    now_ms = int(time.time() * 1000)
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, script_path, pid, started_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, script_path, pid, now_ms),
    )
    conn.commit()


def close_session(conn: sqlite3.Connection, session_id: str,
                  total_events: int) -> None:
    now_ms = int(time.time() * 1000)
    conn.execute(
        "UPDATE sessions SET ended_at=?, total_events=? WHERE session_id=?",
        (now_ms, total_events, session_id),
    )
    conn.commit()


def _write_batch(conn: sqlite3.Connection, session_id: str,
                 events: list[tuple]) -> int:
    """Insert a batch of raw event tuples.  Returns number of rows written."""
    rows = []
    for ev in events:
        fn_key, event, arg_types, ret_or_exc, is_exc, ts_ns, caller_key = ev
        rows.append((
            session_id,
            fn_key,
            event,
            json.dumps(list(arg_types)),
            ret_or_exc,
            1 if is_exc else 0,
            ts_ns,
            caller_key,
        ))
    conn.executemany(
        "INSERT INTO events "
        "(session_id, fn_key, event, arg_types, ret_or_exc, is_exception, timestamp_ns, caller_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Background flush thread
# ---------------------------------------------------------------------------

class FlushThread(threading.Thread):
    """Drains the observer buffer and writes to SQLite on a background thread.

    The interval is adaptive:
      - 5.0s  when the last batch had < 5 000 events  (quiet app)
      - 1.0s  when the last batch had >= 5 000 events  (hot loop)
    """

    QUIET_INTERVAL = 5.0
    BUSY_INTERVAL  = 1.0
    BUSY_THRESHOLD = 5_000

    def __init__(self, conn: sqlite3.Connection, session_id: str,
                 aggregator: "Aggregator | None" = None) -> None:  # noqa: F821
        super().__init__(name="ghost-flush", daemon=True)
        self._conn = conn
        self._session_id = session_id
        self._aggregator = aggregator
        self._stop_event = threading.Event()
        self._total_written = 0

    def run(self) -> None:
        interval = self.QUIET_INTERVAL
        while not self._stop_event.is_set():
            self._stop_event.wait(interval)
            events = drain_buffer()
            if events:
                written = _write_batch(self._conn, self._session_id, events)
                self._total_written += written
                if self._aggregator:
                    self._aggregator.ingest(events)
                interval = (
                    self.BUSY_INTERVAL
                    if len(events) >= self.BUSY_THRESHOLD
                    else self.QUIET_INTERVAL
                )
            else:
                interval = self.QUIET_INTERVAL

    def stop(self) -> int:
        """Signal stop, flush remaining events, return total written."""
        self._stop_event.set()
        self.join(timeout=10)
        # Final drain after hook is uninstalled
        events = drain_buffer()
        if events:
            _write_batch(self._conn, self._session_id, events)
            self._total_written += len(events)
            if self._aggregator:
                self._aggregator.ingest(events)
        return self._total_written
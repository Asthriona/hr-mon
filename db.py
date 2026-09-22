"""Server-side SQLite store for HR readings.

WAL + FULL sync: commits are flushed/fsynced so an ACKed response means the
data has actually been written to the journal, not just buffered.
"""

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,           -- UTC ISO-8601
    bpm INTEGER NOT NULL,
    UNIQUE(ts, bpm)
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
"""


class Db:
    def __init__(self, path: str = "hr-server.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def insert_many(self, readings) -> int:
        inserted = 0
        with self._lock:
            for r in readings:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO readings (ts, bpm) VALUES (?, ?)",
                    (r.ts, r.bpm),
                )
                inserted += cur.rowcount
            self._conn.commit()
        return inserted

    def query_range(self, start: str | None, end: str | None, limit: int) -> list:
        sql = "SELECT ts, bpm FROM readings"
        clauses, params = [], []
        if start:
            clauses.append("ts >= ?")
            params.append(start)
        if end:
            clauses.append("ts <= ?")
            params.append(end)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def latest_bpm(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT bpm FROM readings ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return row["bpm"] if row else None

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
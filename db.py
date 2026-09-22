"""Server-side SQLite store for HR readings + session labels.

WAL + FULL sync: commits are flushed/fsynced so an ACKed response means the
data has actually been written to the journal, not just buffered.
"""

import math
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,           -- UTC ISO-8601
    bpm INTEGER NOT NULL,
    UNIQUE(ts, bpm)
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,        -- race | practice | work | other
    start_ts TEXT NOT NULL,     -- UTC ISO-8601
    end_ts TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);
"""

LABELS = ("race", "practice", "work", "other")


def parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _parse_rows(rows) -> list:
    out = []
    for r in rows:
        try:
            out.append((parse_ts(r["ts"]), int(r["bpm"])))
        except Exception:
            pass
    out.sort(key=lambda p: p[0])
    return out


def zone_of(bpm: int, max_hr: int) -> str:
    pct = (bpm / max_hr) * 100
    if pct >= 90:
        return "z5"
    if pct >= 80:
        return "z4"
    if pct >= 70:
        return "z3"
    if pct >= 60:
        return "z2"
    return "z1"


def compute_stats(parsed: list, max_hr: int) -> dict | None:
    """All the derived numbers we can squeeze out of a (dt, bpm) series."""
    n = len(parsed)
    if n == 0:
        return None
    bpms = [b for _, b in parsed]
    dur = (parsed[-1][0] - parsed[0][0]).total_seconds()
    avg = sum(bpms) / n
    srt = sorted(bpms)
    mid = n // 2
    median = srt[mid] if n % 2 else (srt[mid - 1] + srt[mid]) / 2
    p10 = srt[max(0, int(math.ceil(n * 0.10)) - 1)]
    p90 = srt[min(n - 1, int(math.ceil(n * 0.90)) - 1)]
    stdev = math.sqrt(sum((b - avg) ** 2 for b in bpms) / n)
    top_n = max(1, int(n * 0.10))
    top10_avg = sum(srt[n - top_n:]) / top_n

    # time-in-zone (counted between consecutive readings, so gaps don't count)
    zones_s = defaultdict(float)
    for i in range(n - 1):
        secs = (parsed[i + 1][0] - parsed[i][0]).total_seconds()
        if secs > 0:
            zones_s[zone_of(bpms[i], max_hr)] += secs
    total_zone = sum(zones_s.values())
    zone_keys = ("z1", "z2", "z3", "z4", "z5")
    zones_pct = {z: round(zones_s[z] / total_zone * 100, 1) if total_zone else 0.0 for z in zone_keys}
    zones_sec = {z: round(zones_s[z]) for z in zone_keys}

    # rolling peaks/rest: sliding window averages over 60s and 300s
    def rolling_avg(window_s: int, pick_min: bool) -> float:
        best = None
        j, acc, cnt = 0, 0, 0
        for i in range(n):
            while j < n and (parsed[j][0] - parsed[i][0]).total_seconds() <= window_s:
                acc += bpms[j]
                cnt += 1
                j += 1
            if cnt:
                cur = acc / cnt
                best = cur if best is None or (cur < best if pick_min else cur > best) else best
            acc -= bpms[i]
            cnt -= 1
        return best or 0.0

    return {
        "count": n,
        "duration_s": round(dur),
        "avg": round(avg, 1),
        "min": min(bpms),
        "max": max(bpms),
        "median": round(median, 1),
        "p10": p10,
        "p90": p90,
        "stdev": round(stdev, 1),
        "top10_avg": round(top10_avg, 1),
        "peak_1min": round(rolling_avg(60, False), 1),
        "rest_estimate": round(rolling_avg(300, True), 1),
        "zones_sec": zones_sec,
        "zones_pct": zones_pct,
    }


def series(parsed: list, bucket_s: int) -> list:
    if not parsed:
        return []
    bucket_s = max(5, bucket_s)
    groups = defaultdict(list)
    for t, b in parsed:
        groups[int(t.timestamp() // bucket_s)].append(b)
    out = []
    for k in sorted(groups):
        v = groups[k]
        ts = datetime.fromtimestamp(k * bucket_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append([ts, round(sum(v) / len(v), 1), min(v), max(v), len(v)])
    return out


def histogram(parsed: list, bin_w: int = 5) -> list:
    if not parsed:
        return []
    bpms = [b for _, b in parsed]
    lo = (min(bpms) // bin_w) * bin_w
    buckets = defaultdict(int)
    for b in bpms:
        buckets[(b - lo) // bin_w * bin_w + lo] += 1
    return [[k, k + bin_w, buckets[k]] for k in sorted(buckets)]


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

    # ----- readings -----

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

    def readings_parsed(self, start: str, end: str) -> list:
        """Parsed+sorted (dt, bpm) pairs within [start, end]."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, bpm FROM readings WHERE ts >= ? AND ts <= ?",
                (start, end),
            ).fetchall()
        return _parse_rows([dict(r) for r in rows])

    def latest_bpm(self) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT bpm FROM readings ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return row["bpm"] if row else None

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def all_time_range(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM readings"
            ).fetchone()
        return row[0], row[1]

    def detect_stretches(self, start: str, end: str, gap_s: int = 120) -> list:
        """Split readings into activity blocks separated by gaps > gap_s."""
        parsed = self.readings_parsed(start, end)
        if not parsed:
            return []
        out = []
        s_start = s_end = parsed[0][0]
        cnt = 1

        def flush():
            out.append(
                {
                    "start_ts": s_start.isoformat(),
                    "end_ts": s_end.isoformat(),
                    "count": cnt,
                }
            )

        for t, _ in parsed[1:]:
            if (t - s_end).total_seconds() > gap_s:
                flush()
                s_start = t
                cnt = 1
            else:
                cnt += 1
            s_end = t
        flush()
        return out

    # ----- sessions (labels) -----

    def list_sessions(self, start: str | None = None, end: str | None = None) -> list:
        sql = "SELECT id, label, start_ts, end_ts, note FROM sessions"
        clauses, params = [], []
        if start:
            clauses.append("end_ts >= ?")
            params.append(start)
        if end:
            clauses.append("start_ts <= ?")
            params.append(end)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY start_ts"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def add_session(self, label: str, start_ts: str, end_ts: str, note: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (label, start_ts, end_ts, note) VALUES (?, ?, ?, ?)",
                (label, start_ts, end_ts, note),
            )
            self._conn.commit()
        return cur.lastrowid

    def delete_session(self, sid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            self._conn.commit()
        return cur.rowcount > 0

    def readings_for_session(self, sid: int) -> list:
        with self._lock:
            row = self._conn.execute(
                "SELECT start_ts, end_ts FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
        return [dict(row)] if row else []
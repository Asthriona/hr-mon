"""Home server API -- receives HR readings, serves live bpm, dashboard stats.

Run:
    pip install -r requirements.txt
    python run.py          (or: uvicorn app:app --host 0.0.0.0 --port 8000)

Config (server/.env, gitignored):
    HR_AUTH_TOKEN   token required on /readings, /stats and /sessions
    HR_PORT         port run.py binds (default 8000)
    HR_DB           path to the sqlite file (default: <repo>/server/hr-server.db)
"""

from pathlib import Path
import os
import secrets

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import Db, LABELS, compute_stats, series, histogram

def _load_env(path: Path) -> None:
    """Load KEY=VALUE pairs from an (optional, gitignored) .env file."""
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


# Env/.env is read first so HR_DB and HR_WEB below can come from server/.env.
_load_env(Path(__file__).with_name(".env"))

app = FastAPI(title="HR Logger Server", version="2.1")
db = Db(os.environ.get("HR_DB", str(Path(__file__).parent / "hr-server.db")))

STATIC_DIR = Path(__file__).parent / "static"          # live.html overlay


def _web_root() -> Path:
    """Where the dashboard build lives. Tries, in order:
    - HR_WEB env override (a path to web/ or web/dist)
    - git layout: <repo>/web   (server/ is a subfolder of the repo root)
    - flat layout: <script dir>/web  (server files deployed loose, like on Fubuki)
    """
    override = os.environ.get("HR_WEB")
    if override:
        p = Path(override)
        return p if p.name == "web" else p.parent
    git = Path(__file__).resolve().parent.parent / "web"
    if (git / "dist" / "index.html").exists():
        return git
    return Path(__file__).resolve().parent / "web"


WEB_DIR = _web_root()
WEB_DIST = WEB_DIR / "dist"
WEB_STATIC = WEB_DIR / "static"


# Secret required to write or query data. Comes from server/.env (gitignored)
# or the HR_AUTH_TOKEN env var; if unset a random one is generated at startup.
AUTH_TOKEN = os.environ.get("HR_AUTH_TOKEN")
if not AUTH_TOKEN:
    AUTH_TOKEN = secrets.token_hex(32)
    print(f"HR_AUTH_TOKEN not set; generated for this run: {AUTH_TOKEN}", flush=True)


def _check_auth(token: str | None) -> None:
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


class Reading(BaseModel):
    ts: str
    bpm: int


class ReadingBatch(BaseModel):
    readings: list[Reading]


class SessionIn(BaseModel):
    label: str
    start_ts: str
    end_ts: str
    note: str = ""


# ----- ingestion / query -----

@app.post("/readings")
def post_readings(
    batch: ReadingBatch,
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
):
    """Accept a batch (or single element) of readings. Idempotent. Auth required."""
    _check_auth(x_auth_token)
    accepted = db.insert_many(batch.readings)
    return {"received": len(batch.readings), "accepted": accepted}


@app.get("/readings")
def get_readings(
    start: str | None = Query(None, description="ISO ts lower bound (inclusive)"),
    end: str | None = Query(None, description="ISO ts upper bound (inclusive)"),
    limit: int = Query(10000, le=100000),
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
):
    """Query readings back -- used by the client to verify before purging. Auth required."""
    _check_auth(x_auth_token)
    return {"readings": db.query_range(start, end, limit)}


@app.get("/bpm", response_class=PlainTextResponse)
def get_bpm():
    """Latest bpm as plain text, for a text-only OBS source."""
    bpm = db.latest_bpm()
    return str(bpm) if bpm is not None else "--"


@app.get("/live", response_class=HTMLResponse)
def get_live():
    """OBS browser source: auto-refreshing current bpm overlay."""
    return (STATIC_DIR / "live.html").read_text()


@app.get("/health")
def health():
    return {"status": "ok", "stored_readings": db.count()}


# ----- stats dashboard -----

@app.get("/stats")
def get_stats(
    start: str | None = Query(None),
    end: str | None = Query(None),
    labels: str | None = Query(None, description="comma-separated: race,practice,work,other"),
    max_hr: int = Query(190, ge=80, le=250),
    bucket_s: int = Query(60, ge=5, le=86400),
    gap_s: int = Query(120, ge=10),
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
):
    """Everything the dashboard needs for a range (optionally filtered to labeled sessions)."""
    _check_auth(x_auth_token)

    labeled = [l.strip() for l in (labels.split(",") if labels else []) if l.strip()]
    per_session = []
    intervals = []

    if labeled:
        sessions = [s for s in db.list_sessions(start=start, end=end) if s["label"] in labeled]
        for s in sessions:
            lo = max(start, s["start_ts"]) if start else s["start_ts"]
            hi = min(end, s["end_ts"]) if end else s["end_ts"]
            if lo > hi:
                continue
            parsed = db.readings_parsed(lo, hi)
            st = compute_stats(parsed, max_hr) or {}
            per_session.append(
                {k: s[k] for k in ("id", "label", "start_ts", "end_ts", "note")}
                | st
            )
            intervals.append((lo, hi))
    else:
        if not start or not end:
            lo, hi = db.all_time_range()
            start = start or lo
            end = end or hi
        if start and end:
            intervals.append((start, end))

    parsed_all = []
    for lo, hi in intervals:
        parsed_all.extend(db.readings_parsed(lo, hi))

    n = len(parsed_all)
    hist = histogram(parsed_all)
    return {
        "meta": {
            "start": start,
            "end": end,
            "labels": labeled,
            "max_hr": max_hr,
            "bucket_s": bucket_s,
            "data_points": n,
        },
        "current_bpm": db.latest_bpm(),
        "overall": compute_stats(parsed_all, max_hr),
        "per_session": per_session,
        "series": series(parsed_all, bucket_s),
        "histogram": hist,
        "hist_min": hist[0][0] if hist else None,
        "hist_max": hist[-1][1] if hist else None,
        "stretches": db.detect_stretches(start, end, gap_s) if start and end else [],
        "sessions": db.list_sessions(start=start, end=end),
    }


@app.get("/sessions")
def get_sessions(
    start: str | None = Query(None),
    end: str | None = Query(None),
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
):
    _check_auth(x_auth_token)
    return {"sessions": db.list_sessions(start=start, end=end)}


@app.post("/sessions")
def post_session(
    body: SessionIn,
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
):
    _check_auth(x_auth_token)
    if body.label not in LABELS:
        raise HTTPException(status_code=400, detail=f"label must be one of {LABELS}")
    if body.start_ts >= body.end_ts:
        raise HTTPException(status_code=400, detail="end must be after start")
    sid = db.add_session(body.label, body.start_ts, body.end_ts, body.note.strip())
    return {"id": sid}


@app.delete("/sessions/{sid}")
def delete_session(
    sid: int,
    x_auth_token: str | None = Header(None, alias="X-Auth-Token"),
):
    _check_auth(x_auth_token)
    if not db.delete_session(sid):
        raise HTTPException(status_code=404, detail="session not found")
    return {"deleted": sid}


# ----- frontend -----

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    index = WEB_DIST / "index.html"
    if not index.exists():
        raise HTTPException(status_code=503, detail="frontend not built - see web/README.md")
    html = index.read_text().replace("__AUTH_TOKEN__", AUTH_TOKEN)
    return html


@app.get("/")
def read_root():
    return RedirectResponse("/dashboard")


if WEB_STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_STATIC)), name="static")
if (WEB_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")
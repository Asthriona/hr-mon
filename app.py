"""Home server API -- receives HR readings, lets clients verify, serves live bpm.

Run:
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000
"""

from pathlib import Path
import os
import secrets

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import Db

app = FastAPI(title="HR Logger Server", version="1.1")
db = Db(Path(__file__).parent / "hr-server.db")

STATIC_DIR = Path(__file__).parent / "static"


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


# Secret required to write or query readings. Comes from server/.env (gitignored)
# or the HR_AUTH_TOKEN env var; if unset a random one is generated at startup.
_load_env(Path(__file__).with_name(".env"))
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


@app.get("/")
def read_root():
    return {"service": "HR Logger Server", "live": "/live", "bpm": "/bpm", "health": "/health"}


@app.get("/health")
def health():
    return {"status": "ok", "stored_readings": db.count()}


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
# HR Logger server

Small API + SQLite database on the home LAN. Receives HR readings from the
laptop client, lets the client verify data is durable before it purges local
copies, and serves the current bpm for OBS.

## Run

Set up once:

```bash
cp .env.example .env       # then put values in .env (gitignored)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Start (reads the port from `.env` `HR_PORT`, default 8000):

```bash
.venv/bin/python run.py
```

`HR_PORT` is what you change if 8000 is taken (e.g. icecast). Auth: writes,
`GET /readings`, `/stats` and `/sessions` require header `X-Auth-Token`
matching `.env` `HR_AUTH_TOKEN`; `/live`, `/bpm`, `/health` stay open.

The stats dashboard is a separate React app in `../web` — build it once so the
API can serve it:

```bash
cd ../web && npm install && npm run build
```

## Production (systemd)

A ready-made unit is in `systemd/hr-server.service` (edit paths if your repo
isn't at `/root/code/hr-mon`):

```bash
sudo cp systemd/hr-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hr-server
```

Bind it in Cloudflare Tunnel to `http://localhost:8765` (or whatever
`HR_PORT` is). After a deploy, rebuild the frontend then restart:

```bash
cd web && npm ci && npm run build
sudo systemctl restart hr-server
```

## Endpoints

| Method   | Path            | Purpose                                                                  |
|----------|-----------------|--------------------------------------------------------------------------|
| POST     | `/readings`     | Receive single or batch readings `{ts, bpm}`. Idempotent.                |
| GET      | `/readings`     | Query by `start`/`end` ISO range (verify-before-purge).                  |
| GET      | `/stats`        | All dashboard numbers for a range (see below).                           |
| GET      | `/sessions`     | List labeled sessions.                                                   |
| POST     | `/sessions`     | Label a range: `{label: race\|practice\|work\|other, start_ts, end_ts}` |
| DELETE   | `/sessions/{id}`| Remove a label.                                                          |
| GET      | `/bpm`          | Latest bpm as plain text (text-only OBS source).                         |
| GET      | `/live`         | Auto-refreshing HTML overlay (OBS browser source).                       |
| GET      | `/dashboard`    | The stats web app (built SPA from `../web`).                             |
| GET      | `/health`       | Health check + stored count.                                             |

Interactive API docs: `http://host:8000/docs`.

### `/stats`

`GET /stats?start&end&labels&max_hr&bucket_s&gap_s`

- `start`/`end` — UTC ISO bounds of the window (omit for all time).
- `labels` — comma-separated; when set, stats cover only that union of labeled
  sessions (e.g. all races). Per-session breakdown is included.
- Without labels, `stretches` auto-detects continuous activity blocks
  (gap > `gap_s`, default 150s) so you can label them from the UI.

Returns `meta`, `current_bpm`, `overall` (avg/min/max/median/p10/p90/stdev,
top-10% avg, peak-1min, resting estimate, time-in-zone by 5-zone model vs
`max_hr`), `per_session`, `series` (bucketed), `histogram`, `stretches`,
`sessions`.

## Durability

The DB runs WAL + `synchronous=FULL`, so a 200 OK response is only sent
after the row is flushed to disk. `INSERT OR IGNORE` with a `UNIQUE(ts, bpm)`
constraint makes re-pushes harmless in case the client retries a batch.

## Data

`hr-server.db` in this folder. Query directly for dashboards/stats:

```bash
sqlite3 hr-server.db "SELECT ts, bpm FROM readings ORDER BY ts DESC LIMIT 20;"
```
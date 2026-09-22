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

`HR_PORT` is what you change if 8000 is taken (e.g. icecast). Auth: writes and
`GET /readings` require header `X-Auth-Token` matching `.env` `HR_AUTH_TOKEN`;
`/live`, `/bpm`, `/health` stay open.

## Production (systemd)

A ready-made unit is in `systemd/hr-server.service` (edit paths if your repo
isn't at `/root/code/hr-mon`):

```bash
sudo cp systemd/hr-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hr-server
```

Bind it in Cloudflare Tunnel to `http://localhost:8765` (or whatever
`HR_PORT` is).

## Endpoints

| Method | Path        | Purpose                                                        |
|--------|-------------|----------------------------------------------------------------|
| POST   | `/readings` | Receive single or batch readings `{ts, bpm}`. Idempotent.      |
| GET    | `/readings` | Query by `start`/`end` ISO range (used for verify-before-purge)|
| GET    | `/bpm`      | Latest bpm as plain text (text-only OBS source).               |
| GET    | `/live`     | Auto-refreshing HTML overlay (OBS browser source).             |
| GET    | `/health`   | Health check + stored count.                                   |

Interactive API docs: `http://host:8000/docs`.

## Durability

The DB runs WAL + `synchronous=FULL`, so a 200 OK response is only sent
after the row is flushed to disk. `INSERT OR IGNORE` with a `UNIQUE(ts, bpm)`
constraint makes re-pushes harmless in case the client retries a batch.

## Data

`hr-server.db` in this folder. Query directly for dashboards/stats:

```bash
sqlite3 hr-server.db "SELECT ts, bpm FROM readings ORDER BY ts DESC LIMIT 20;"
```
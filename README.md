# HR Logger server

Small API + SQLite database on the home LAN. Receives HR readings from the
laptop client, lets the client verify data is durable before it purges local
copies, and serves the current bpm for OBS.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

Optionally install as a systemd service so it's always up:

```
[Unit]
Description=HR Logger server
After=network.target

[Service]
WorkingDirectory=/home/makoto/code/HrStuff/server
ExecStart=/home/makoto/code/HrStuff/server/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

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
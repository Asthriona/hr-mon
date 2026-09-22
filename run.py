"""HR Logger server launcher.

Reads HR_PORT from server/.env (default 8000) and serves on 0.0.0.0.
Production: use the systemd unit (systemd/hr-server.service) instead.
"""

import os
import sys

import uvicorn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import app  # noqa: E402  (imports app -> loads .env, sets AUTH_TOKEN)

if __name__ == "__main__":
    port = int(os.environ.get("HR_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
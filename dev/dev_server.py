"""
Tiny development server that reuses the production templates/static
to render the dashboard against a static fixture (`sample_panels.json`).

Usage:
    python dev/dev_server.py            # serves on http://127.0.0.1:8089
    PORT=9000 python dev/dev_server.py

Useful for taking screenshots of the dashboard for the README without
needing a real MMU on the network.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from flask import Flask, jsonify, render_template

REPO_ROOT  = Path(__file__).resolve().parent.parent
FIXTURE    = Path(__file__).resolve().parent / "sample_panels.json"

app = Flask(
    __name__,
    template_folder=str(REPO_ROOT / "templates"),
    static_folder=str(REPO_ROOT / "static"),
)


def _load_fixture() -> dict:
    """Load the fixture each request so edits show up on refresh."""
    data = json.loads(FIXTURE.read_text())
    now = time.time()
    # Keep timestamps "live" so the header pill says `live` rather than offline.
    data["last_success_at"]       = now
    data["last_data_received_at"] = now
    return data


@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/panels")
def api_panels():
    return jsonify(_load_fixture())


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8089"))
    print(f"dev dashboard: http://127.0.0.1:{port}/  (fixture: {FIXTURE})")
    app.run(host="127.0.0.1", port=port, debug=False)

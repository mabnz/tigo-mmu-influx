"""
Flask + Waitress wrapper for the Tigo MMU scraper.

Endpoints:
    GET /            — responsive HTML dashboard
    GET /text        — plain-text status (former "/")
    GET /healthz     — 200 if last scrape succeeded recently, else 503
    GET /status      — JSON: scheduler state + last scrape result
    GET /api/panels  — JSON: latest panel readings (consumed by /)
    GET /metrics     — Prometheus-style counters
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask import Flask, jsonify, render_template
from waitress import serve

import tigo_scraper

# ============================================================
# CONFIG
# ============================================================
SCRAPE_INTERVAL_SEC    = int(os.getenv("SCRAPE_INTERVAL_SEC", "10"))
MAX_INTERVAL_SEC       = int(os.getenv("MAX_INTERVAL_SEC", "300"))
BACKOFF_AFTER_FAILURES = int(os.getenv("BACKOFF_AFTER_FAILURES", "3"))

LISTEN_HOST       = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT       = int(os.getenv("LISTEN_PORT", "8088"))
HEALTH_STALE_SEC  = int(os.getenv("HEALTH_STALE_SEC", "60"))

STATE_FILE = os.getenv("STATE_FILE", "/opt/tigo/state.json")
APP_TZ     = ZoneInfo(os.getenv("PAGE_TZ", "Pacific/Auckland"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tigo")


# ============================================================
# Shared state
# ============================================================
def _now_local_iso() -> str:
    return datetime.now(APP_TZ).isoformat(timespec="seconds")


_state_lock = threading.Lock()
_state = {
    "started_at":            _now_local_iso(),
    "last_success_at":       None,
    "last_failure_at":       None,
    "last_error":            None,
    "success_count":         0,
    "failure_count":         0,
    "consecutive_failures":  0,
    "last_panels":           0,
    "last_panels_reporting": 0,
    "last_unit_id":          None,
    "last_status_message":   None,
    "last_data_received_at": None,   # epoch when panel readings last changed
    "last_data_signature":   None,   # hash of the most recent panel readings
    "current_interval_sec":  SCRAPE_INTERVAL_SEC,
    "panels":                [],   # latest panel readings (full list)
}

_PERSIST_KEYS = (
    "last_success_at", "last_failure_at", "last_error",
    "success_count", "failure_count",
    "last_panels", "last_panels_reporting",
    "last_unit_id", "last_status_message",
    "last_data_received_at", "last_data_signature",
    "panels",
)


def _load_state() -> None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except FileNotFoundError:
        log.info("no persisted state at %s; starting fresh", STATE_FILE)
        return
    except Exception as e:                                  # noqa: BLE001
        log.warning("could not read state file %s: %s", STATE_FILE, e)
        return
    with _state_lock:
        for k in _PERSIST_KEYS:
            if k in saved:
                _state[k] = saved[k]
    log.info("restored persisted state from %s", STATE_FILE)


def _save_state() -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with _state_lock:
            snapshot = dict(_state)
        tmp = f"{STATE_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:                                  # noqa: BLE001
        log.warning("could not persist state to %s: %s", STATE_FILE, e)


# ============================================================
# Backoff
# ============================================================
def _desired_interval(consecutive_failures: int) -> int:
    if consecutive_failures < BACKOFF_AFTER_FAILURES:
        return SCRAPE_INTERVAL_SEC
    overshoot = consecutive_failures - BACKOFF_AFTER_FAILURES + 1
    return min(SCRAPE_INTERVAL_SEC * (2 ** overshoot), MAX_INTERVAL_SEC)


def _maybe_reschedule() -> None:
    with _state_lock:
        target = _desired_interval(_state["consecutive_failures"])
        current = _state["current_interval_sec"]
    if target == current:
        return
    try:
        scheduler.reschedule_job(
            "tigo_scrape",
            trigger=IntervalTrigger(seconds=target),
        )
        with _state_lock:
            _state["current_interval_sec"] = target
        log.warning("scrape interval changed: %ds -> %ds", current, target)
    except Exception as e:                                  # noqa: BLE001
        log.warning("could not reschedule job: %s", e)


# ============================================================
# Scrape job
# ============================================================
def _scrape_job() -> None:
    t0 = time.time()
    try:
        data = tigo_scraper.scrape_and_write()
        reporting = sum(1 for p in data["panels"] if p["vin"] is not None)
        now = time.time()
        status_msg = data.get("status_message")
        # Detect whether the panel readings actually changed since last scrape.
        # We hash the panel list (sorted keys, only the volatile fields), so
        # "data updated" reflects when the MMU genuinely produced new numbers,
        # ignoring its own (often minute-rounded) freshness banner.
        sig_payload = [
            {k: p.get(k) for k in (
                "label", "vin", "vout", "current_a", "power_w",
                "temp_c", "rssi", "event", "status_raw",
            )}
            for p in data["panels"]
        ]
        signature = hashlib.sha1(
            json.dumps(sig_payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        with _state_lock:
            prev_sig = _state.get("last_data_signature")
            if prev_sig != signature or _state["last_data_received_at"] is None:
                _state["last_data_received_at"] = now
                _state["last_data_signature"]   = signature
            _state["last_success_at"]       = now
            _state["success_count"]        += 1
            _state["consecutive_failures"]  = 0
            _state["last_panels"]           = len(data["panels"])
            _state["last_panels_reporting"] = reporting
            _state["last_unit_id"]          = data.get("unit_id")
            _state["last_status_message"]   = status_msg
            _state["last_error"]            = None
            _state["panels"]                = data["panels"]
        log.info("scrape ok: %d/%d panels reporting in %.2fs",
                 reporting, len(data["panels"]), time.time() - t0)
    except Exception as e:                                  # noqa: BLE001
        with _state_lock:
            _state["last_failure_at"]       = time.time()
            _state["failure_count"]        += 1
            _state["consecutive_failures"] += 1
            _state["last_error"]            = f"{type(e).__name__}: {e}"
            cf = _state["consecutive_failures"]
        log.error("scrape failed (consecutive=%d): %s", cf, e)
    finally:
        _maybe_reschedule()
        _save_state()


# ============================================================
# Scheduler
# ============================================================
scheduler = BackgroundScheduler(
    timezone="UTC",
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30},
)
scheduler.add_job(
    _scrape_job,
    trigger=IntervalTrigger(seconds=SCRAPE_INTERVAL_SEC),
    id="tigo_scrape",
    next_run_time=datetime.now(timezone.utc),
)


# ============================================================
# Flask
# ============================================================
app = Flask(__name__)


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


# ---------- HTML dashboard ----------
# Template lives in templates/dashboard.html; CSS/JS in static/.


@app.get("/")
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/panels")
def api_panels():
    """JSON consumed by the dashboard. Includes everything needed to render."""
    with _state_lock:
        s = dict(_state)
    return jsonify({
        "started_at":            s["started_at"],
        "last_success_at":       s["last_success_at"],
        "last_failure_at":       s["last_failure_at"],
        "last_error":            s["last_error"],
        "success_count":         s["success_count"],
        "failure_count":         s["failure_count"],
        "consecutive_failures":  s["consecutive_failures"],
        "current_interval_sec":  s["current_interval_sec"],
        "last_unit_id":          s["last_unit_id"],
        "last_status_message":   s["last_status_message"],
        "last_data_received_at": s["last_data_received_at"],
        "panels":                s["panels"],
    })


@app.get("/text")
def text_view():
    with _state_lock:
        s = dict(_state)
    age = (time.time() - s["last_success_at"]) if s["last_success_at"] else None
    last_ok = (f"{_iso(s['last_success_at'])} ({age:.1f}s ago)"
               if age is not None else "never")
    body = (
        "Tigo MMU scraper\n"
        f"  started:           {s['started_at']}\n"
        f"  base interval:     {SCRAPE_INTERVAL_SEC}s\n"
        f"  current interval:  {s['current_interval_sec']}s\n"
        f"  last success:      {last_ok}\n"
        f"  last failure:      {_iso(s['last_failure_at'])}\n"
        f"  last error:        {s['last_error']}\n"
        f"  successes:         {s['success_count']}\n"
        f"  failures:          {s['failure_count']}\n"
        f"  consecutive fails: {s['consecutive_failures']}\n"
        f"  panels reporting:  {s['last_panels_reporting']}/{s['last_panels']}\n"
        f"  unit id:           {s['last_unit_id']}\n"
        f"  status message:    {s['last_status_message']}\n"
    )
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/status")
def status():
    with _state_lock:
        s = dict(_state)
    s["last_success_at_iso"] = _iso(s["last_success_at"])
    s["last_failure_at_iso"] = _iso(s["last_failure_at"])
    s["base_interval_sec"]   = SCRAPE_INTERVAL_SEC
    s["max_interval_sec"]    = MAX_INTERVAL_SEC
    s["scheduler_running"]   = scheduler.running
    return jsonify(s)


@app.get("/healthz")
def healthz():
    with _state_lock:
        last_ok = _state["last_success_at"]
        cf      = _state["consecutive_failures"]
    if last_ok is None:
        return jsonify(status="starting", consecutive_failures=cf), 503
    age = time.time() - last_ok
    if age > HEALTH_STALE_SEC:
        return jsonify(status="stale", age_sec=age, consecutive_failures=cf), 503
    return jsonify(status="ok", age_sec=age, consecutive_failures=cf), 200


@app.get("/metrics")
def metrics():
    with _state_lock:
        s = dict(_state)
    lines = [
        "# HELP tigo_scrape_success_total Successful scrapes since start",
        "# TYPE tigo_scrape_success_total counter",
        f"tigo_scrape_success_total {s['success_count']}",
        "# HELP tigo_scrape_failure_total Failed scrapes since start",
        "# TYPE tigo_scrape_failure_total counter",
        f"tigo_scrape_failure_total {s['failure_count']}",
        "# HELP tigo_consecutive_failures Consecutive scrape failures",
        "# TYPE tigo_consecutive_failures gauge",
        f"tigo_consecutive_failures {s['consecutive_failures']}",
        "# HELP tigo_current_interval_seconds Current scrape interval",
        "# TYPE tigo_current_interval_seconds gauge",
        f"tigo_current_interval_seconds {s['current_interval_sec']}",
        "# HELP tigo_panels_reporting Panels reporting in last scrape",
        "# TYPE tigo_panels_reporting gauge",
        f"tigo_panels_reporting {s['last_panels_reporting']}",
        "# HELP tigo_panels_total Total panels in last scrape",
        "# TYPE tigo_panels_total gauge",
        f"tigo_panels_total {s['last_panels']}",
        "# HELP tigo_last_success_timestamp_seconds Unix time of last success",
        "# TYPE tigo_last_success_timestamp_seconds gauge",
        f"tigo_last_success_timestamp_seconds {s['last_success_at'] or 0}",
    ]
    return "\n".join(lines) + "\n", 200, {"Content-Type": "text/plain; version=0.0.4"}


# ============================================================
# Lifecycle
# ============================================================
def _shutdown(*_):
    log.info("shutting down…")
    try:
        scheduler.shutdown(wait=False)
    except Exception:                                       # noqa: BLE001
        pass
    _save_state()
    os._exit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)
    _load_state()
    scheduler.start()
    log.info("tigo scraper started; base_interval=%ds; max_interval=%ds; "
             "backoff_after=%d; listening on %s:%d",
             SCRAPE_INTERVAL_SEC, MAX_INTERVAL_SEC, BACKOFF_AFTER_FAILURES,
             LISTEN_HOST, LISTEN_PORT)
    serve(app, host=LISTEN_HOST, port=LISTEN_PORT, threads=4, ident="tigo-mmu")


if __name__ == "__main__":
    main()
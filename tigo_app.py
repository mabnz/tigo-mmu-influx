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
from flask import Flask, jsonify, render_template_string
from waitress import serve

import tigo_scraper

# ============================================================
# CONFIG
# ============================================================
SCRAPE_INTERVAL_SEC    = int(os.getenv("SCRAPE_INTERVAL_SEC", "10"))
MAX_INTERVAL_SEC       = int(os.getenv("MAX_INTERVAL_SEC", "300"))
BACKOFF_AFTER_FAILURES = int(os.getenv("BACKOFF_AFTER_FAILURES", "3"))

LISTEN_HOST       = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT       = int(os.getenv("LISTEN_PORT", "8080"))
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
    "current_interval_sec":  SCRAPE_INTERVAL_SEC,
    "panels":                [],   # latest panel readings (full list)
}

_PERSIST_KEYS = (
    "last_success_at", "last_failure_at", "last_error",
    "success_count", "failure_count",
    "last_panels", "last_panels_reporting",
    "last_unit_id", "last_status_message",
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
        with _state_lock:
            _state["last_success_at"]       = time.time()
            _state["success_count"]        += 1
            _state["consecutive_failures"]  = 0
            _state["last_panels"]           = len(data["panels"])
            _state["last_panels_reporting"] = reporting
            _state["last_unit_id"]          = data.get("unit_id")
            _state["last_status_message"]   = data.get("status_message")
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
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>Tigo MMU</title>
<style>
:root {
    --bg:        #f4f6fa;
    --panel:     #ffffff;       /* summary tiles */
    --card: #fff7e6;      --card-edge: #d97706;
    /* --card: #2a2010;      --card-edge: #f59e0b; */
    --text:      #1a2030;
    --muted:     #66708a;
    --border:    #e3e7ef;
    --border-card: #d6deef;
    --accent:    #2f80ed;
    --good:      #1eaa5c;
    --warn:      #e8a317;
    --bad:       #d4453a;
    --offline:   #9aa1b1;
    --shadow:    0 1px 2px rgba(20,30,60,.06), 0 4px 14px rgba(20,30,60,.06);
}
@media (prefers-color-scheme: dark) {
    :root {
        --bg:          #0f1320;
        --panel:       #181d2e;     /* summary tiles */
        --card:        #1f2742;     /* panel cards — lighter/bluer than summary */
        --card-edge:   #5aa1ff;
        --text:        #e6e9f2;
        --muted:       #8b93a8;
        --border:      #262c40;
        --border-card: #324070;
        --accent:      #5aa1ff;
        --good:        #3fcf7f;
        --warn:        #f0b94a;
        --bad:         #ef5a4f;
        --offline:     #5b6479;
        --shadow:      0 1px 2px rgba(0,0,0,.4), 0 6px 20px rgba(0,0,0,.35);
    }
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    background: var(--bg);
    padding: max(env(safe-area-inset-top), 16px) 16px 24px;
}
.wrap { max-width: 1200px; margin: 0 auto; }

header {
    display: flex; flex-wrap: wrap; align-items: center; gap: 12px;
    margin-bottom: 18px;
}
header h1 {
    margin: 0; font-size: 22px; font-weight: 600; letter-spacing: -0.01em;
}
.pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 600;
    background: var(--panel); border: 1px solid var(--border);
    color: var(--muted);
}
.pill .dot {
    width: 8px; height: 8px; border-radius: 50%; background: var(--offline);
}
.pill.ok      .dot { background: var(--good); box-shadow: 0 0 0 3px rgba(63,207,127,.15); }
.pill.warn    .dot { background: var(--warn); }
.pill.bad     .dot { background: var(--bad);  }
.pill .label  { color: var(--text); }

/* ---------- summary tiles ---------- */
.summary {
    display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    margin-bottom: 20px;
}
.summary .stat {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px; padding: 12px 14px; box-shadow: var(--shadow);
}
.summary .stat .k { font-size: 11px; text-transform: uppercase;
                    letter-spacing: .07em; color: var(--muted); }
.summary .stat .v { font-size: 20px; font-weight: 600; margin-top: 2px; }
.summary .stat .sub { font-size: 12px; color: var(--muted); }

/* ---------- panel cards (visually distinct from summary tiles) ---------- */
.grid {
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
}
.card {
    background: var(--card);
    border: 1px solid var(--border-card);
    border-left: 4px solid var(--card-edge);     /* accent stripe */
    border-radius: 14px; padding: 14px 16px;
    box-shadow: var(--shadow);
    position: relative; overflow: hidden;
    transition: transform .12s ease, border-color .12s ease;
}
.card:hover { transform: translateY(-1px); border-color: var(--accent); }
.card.offline {
    opacity: .7;
    border-left-color: var(--offline);
}
.card .top {
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 8px;
}
.card .label { font-size: 22px; font-weight: 700; letter-spacing: -.02em; }
.card .badge {
    font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 999px;
    background: rgba(63,207,127,.15); color: var(--good);
}
.card.offline .badge { background: rgba(154,161,177,.22); color: var(--offline); }

.card .meta { font-size: 11px; color: var(--muted); line-height: 1.35;
              margin-bottom: 10px; word-break: break-all; }
.card .meta code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                   font-size: 11px; color: var(--text); }

.kpis {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px;
    border-top: 1px dashed var(--border-card); padding-top: 10px;
}
.kpis .k { font-size: 11px; color: var(--muted);
           text-transform: uppercase; letter-spacing: .06em; }
.kpis .v { font-size: 17px; font-weight: 600; line-height: 1.2; }
.kpis .v small { font-size: 11px; color: var(--muted);
                 font-weight: 500; margin-left: 3px; }

.power-bar {
    grid-column: 1 / -1; height: 6px; border-radius: 4px;
    background: var(--border-card); overflow: hidden; margin-top: 4px;
}
.power-bar > span { display: block; height: 100%; background: var(--accent);
                    transition: width .3s ease; }

.rssi { display: inline-flex; gap: 2px; align-items: end;
        height: 14px; vertical-align: -2px; margin-left: 6px; }
.rssi i { display: inline-block; width: 3px; background: var(--border-card);
          border-radius: 1px; }
.rssi i.on { background: var(--good); }
.rssi i:nth-child(1) { height: 25%; }
.rssi i:nth-child(2) { height: 50%; }
.rssi i:nth-child(3) { height: 75%; }
.rssi i:nth-child(4) { height: 100%; }

.banner {
    margin-bottom: 14px; padding: 10px 14px;
    border-radius: 10px; border: 1px solid var(--border);
    background: rgba(212,69,58,.10); color: var(--bad); font-weight: 500;
}
footer {
    margin-top: 24px; font-size: 12px; color: var(--muted); text-align: center;
}
footer a { color: var(--muted); }
@media (max-width: 480px) {
    .card .label { font-size: 20px; }
    header h1 { font-size: 19px; }
}
</style>
</head>
<body>
<div class="wrap">
    <header>
        <h1>Tigo MMU</h1>
        <span class="pill" id="conn"><span class="dot"></span><span class="label">connecting…</span></span>
        <span class="pill" id="updated">last update: —</span>
    </header>

    <div id="banner" class="banner" hidden></div>

    <section class="summary">
        <div class="stat"><div class="k">Panels reporting</div>
                          <div class="v" id="s-reporting">—</div>
                          <div class="sub" id="s-reporting-sub"></div></div>
        <div class="stat"><div class="k">Total power</div>
                          <div class="v" id="s-power">—</div>
                          <div class="sub">across reporting panels</div></div>
        <div class="stat"><div class="k">Unit ID</div>
                          <div class="v" id="s-unit" style="font-size:14px;font-family:ui-monospace,monospace">—</div>
                          <div class="sub" id="s-started"></div></div>
        <div class="stat"><div class="k">Scrape interval</div>
                          <div class="v" id="s-interval">—</div>
                          <div class="sub" id="s-failures"></div></div>
    </section>

    <section class="grid" id="grid"></section>

    <footer>
        <a href="/status">/status</a> ·
        <a href="/healthz">/healthz</a> ·
        <a href="/metrics">/metrics</a> ·
        <a href="/text">/text</a>
    </footer>
</div>

<script>
const REFRESH_MS = 5000;        // poll cadence; independent of scrape interval

function fmt(v, digits = 2) {
    return (v === null || v === undefined || Number.isNaN(v))
           ? "—" : Number(v).toFixed(digits);
}
function rssiBars(rssi) {
    // The MMU reports a number that's typically 80–160; treat >=100 as full bars.
    if (rssi == null) return 0;
    if (rssi >= 130) return 4;
    if (rssi >= 110) return 3;
    if (rssi >=  90) return 2;
    if (rssi >=  70) return 1;
    return 0;
}
function timeAgo(epoch) {
    if (!epoch) return "never";
    const s = Math.max(0, Math.round(Date.now()/1000 - epoch));
    if (s < 60)   return s + "s ago";
    if (s < 3600) return Math.round(s/60) + "m ago";
    return Math.round(s/3600) + "h ago";
}

function render(data) {
    // header pill
    const conn = document.getElementById("conn");
    const lbl  = conn.querySelector(".label");
    const ageS = data.last_success_at
                 ? Math.round(Date.now()/1000 - data.last_success_at) : null;
    let cls = "pill";
    if (ageS == null)       { cls += " bad";  lbl.textContent = "no data"; }
    else if (ageS <= 30)    { cls += " ok";   lbl.textContent = "live"; }
    else if (ageS <= 120)   { cls += " warn"; lbl.textContent = "stale " + timeAgo(data.last_success_at); }
    else                    { cls += " bad";  lbl.textContent = "offline " + timeAgo(data.last_success_at); }
    conn.className = cls;

    document.getElementById("updated").textContent =
        "last update: " + timeAgo(data.last_success_at);

    // banner
    const banner = document.getElementById("banner");
    if (data.last_status_message) {
        banner.textContent = data.last_status_message;
        banner.hidden = false;
    } else { banner.hidden = true; }

    // summary
    const panels = data.panels || [];
    const reporting = panels.filter(p => p.vin != null);
    const totalPower = reporting.reduce((a, p) => a + (p.power_w || 0), 0);

    document.getElementById("s-reporting").textContent =
        reporting.length + " / " + panels.length;
    document.getElementById("s-reporting-sub").textContent =
        panels.length ? Math.round(reporting.length / panels.length * 100) + "% online" : "";
    document.getElementById("s-power").innerHTML =
        fmt(totalPower, 2) + ' <small style="font-size:13px;color:var(--muted)">W</small>';
    document.getElementById("s-unit").textContent = data.last_unit_id || "—";
    document.getElementById("s-started").textContent =
        data.started_at ? "since " + data.started_at : "";
    document.getElementById("s-interval").innerHTML =
        (data.current_interval_sec || "—") + ' <small style="font-size:13px;color:var(--muted)">s</small>';
    document.getElementById("s-failures").textContent =
        data.consecutive_failures
            ? data.consecutive_failures + " consecutive failures"
            : (data.failure_count + " failures total");

    // grid
    const grid = document.getElementById("grid");
    grid.innerHTML = "";
    for (const p of panels) {
        const offline = p.vin == null;
        const card = document.createElement("div");
        card.className = "card" + (offline ? " offline" : "");

        const bars = rssiBars(p.rssi);
        const rssiHtml = `<span class="rssi" title="RSSI ${p.rssi ?? "n/a"}">
            ${[1,2,3,4].map(i =>
                `<i class="${bars >= i ? "on" : ""}"></i>`).join("")}
        </span>`;

        // power as % of card-local 100% scale; otherwise just show value
        const powerPct = p.power_pct != null ? Math.max(0, Math.min(100, p.power_pct)) : 0;

        card.innerHTML = `
            <div class="top">
                <span class="label">${p.label ?? "?"}</span>
                <span class="badge">${offline ? "offline" : "online"}</span>
            </div>
            <div class="meta">
                <code>${p.mac ?? ""}</code><br>
                ${p.barcode ?? ""}
            </div>
            <div class="kpis">
                <div><div class="k">V in</div>
                     <div class="v">${fmt(p.vin)}<small>V</small></div></div>
                <div><div class="k">V out</div>
                     <div class="v">${fmt(p.vout)}<small>V</small></div></div>
                <div><div class="k">Power</div>
                     <div class="v">${fmt(p.power_w)}<small>W</small></div></div>
                <div><div class="k">RSSI ${rssiHtml}</div>
                     <div class="v">${p.rssi ?? "—"}</div></div>
                <div class="power-bar"><span style="width:${powerPct}%"></span></div>
            </div>
        `;
        grid.appendChild(card);
    }
}

async function tick() {
    try {
        const r = await fetch("/api/panels", { cache: "no-store" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        render(await r.json());
    } catch (e) {
        const conn = document.getElementById("conn");
        conn.className = "pill bad";
        conn.querySelector(".label").textContent = "fetch error";
    } finally {
        setTimeout(tick, REFRESH_MS);
    }
}
tick();
</script>
</body>
</html>
"""


@app.get("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


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
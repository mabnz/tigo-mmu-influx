"""
Scrape the Tigo MMU status page and write panel readings to InfluxDB.

This module is import-safe: importing it has no side effects.
Call scrape_and_write() to perform one cycle.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

# ============================================================
# CONFIG (env vars override the hardcoded defaults)
# ============================================================
MMU_URL  = os.getenv("MMU_URL",  "http://192.168.1.1/cgi-bin/mmdstatus")
MMU_USER = os.getenv("MMU_USER", "user")
MMU_PASS = os.getenv("MMU_PASS", "tigo1")

# InfluxDB 2.x
INFLUX_URL    = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUX_TOKEN  = os.getenv("INFLUX_TOKEN",  "PUT-YOUR-INFLUX-TOKEN-HERE")
INFLUX_ORG    = os.getenv("INFLUX_ORG",    "my-org")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "tigo")

PANEL_MEASUREMENT  = os.getenv("PANEL_MEASUREMENT",  "tigo_panel")
SYSTEM_MEASUREMENT = os.getenv("SYSTEM_MEASUREMENT", "tigo_system")

USE_PAGE_TIMESTAMP = os.getenv("USE_PAGE_TIMESTAMP", "1") == "1"
PAGE_TZ            = os.getenv("PAGE_TZ", "Pacific/Auckland")

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))

# Reuse a single Session — keeps TCP connections alive between scrapes.
_session = requests.Session()


# ============================================================
# Scrape
# ============================================================
def fetch_html() -> str:
    last_err: Optional[Exception] = None
    for auth in (HTTPBasicAuth(MMU_USER, MMU_PASS),
                 HTTPDigestAuth(MMU_USER, MMU_PASS)):
        try:
            r = _session.get(MMU_URL, auth=auth, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_err = e
            continue
        if r.status_code == 200:
            return r.text
        if r.status_code != 401:
            r.raise_for_status()
    raise RuntimeError(f"Auth/connect failed against {MMU_URL}: {last_err}")


def _num(text: str) -> Optional[float]:
    if text is None:
        return None
    t = text.strip().replace("\xa0", " ")
    if not t or t.lower() == "n/a":
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def _int(text: str) -> Optional[int]:
    f = _num(text)
    return int(f) if f is not None else None


def _txt(td) -> str:
    return td.get_text(" ", strip=True).replace("\xa0", " ")


def parse_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {
        "timestamp_ns": None, "unit_id": None, "status_message": None,
        "lmus_reporting": None, "lmus_total": None, "panels": [],
    }

    m = re.search(r"on (\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})", html)
    if m and USE_PAGE_TIMESTAMP:
        y, mo, d, hh, mm, ss = map(int, m.groups())
        local = datetime(y, mo, d, hh, mm, ss, tzinfo=ZoneInfo(PAGE_TZ))
        out["timestamp_ns"] = int(local.timestamp() * 1_000_000_000)
    else:
        out["timestamp_ns"] = time.time_ns()

    m = re.search(r"Unit id:\s*([0-9A-Fa-f]+)", html)
    if m:
        out["unit_id"] = m.group(1)

    banner = soup.find("font", {"color": "#C00000"})
    if banner:
        out["status_message"] = banner.get_text(" ", strip=True)

    m = re.search(r"<b>(\d+)</b>\s*LMUs reporting data out of\s*<b>(\d+)</b>", html)
    if m:
        out["lmus_reporting"] = int(m.group(1))
        out["lmus_total"]     = int(m.group(2))

    target = next((t for t in soup.find_all("table")
                   if "Barcode" in t.get_text() and "MAC" in t.get_text()), None)
    if target is None:
        return out

    for tr in target.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 23:
            continue
        c = [_txt(td) for td in tds]
        ev = c[19]
        out["panels"].append({
            "label": c[0], "barcode": c[1], "mac": c[2],
            "vin": _num(c[3]),  "vin_pct":  _num(c[4]),
            "vout": _num(c[5]), "vout_pct": _num(c[6]),
            "current_a": _num(c[7]),
            "power_w":   _num(c[8]),  "power_pct": _num(c[9]),
            "temp_c":    _num(c[10]),
            "rssi":      _int(c[11]), "brssi":     _int(c[12]),
            "slot":      c[13],
            "vmpe":      _int(c[14]),
            "sync_evt":  c[16], "mode": c[17], "bypass": c[18],
            "event":     None if ev.lower() == "n/a" else ev,
            "status_raw":  _int(c[20]),
            "extra_raw":   _int(c[21]),
            "details_raw": c[22],
        })
    return out


# ============================================================
# Line protocol
# ============================================================
_LP_TAG   = str.maketrans({",": r"\,", " ": r"\ ", "=": r"\="})
_LP_FIELD = str.maketrans({'"': r"\"", "\\": r"\\"})


def _tag(v: str) -> str:
    return str(v).translate(_LP_TAG)


def _field_kv(k, v):
    if v is None:                 return None
    if isinstance(v, bool):       return f"{k}={'true' if v else 'false'}"
    if isinstance(v, int):        return f"{k}={v}i"
    if isinstance(v, float):      return f"{k}={v}"
    return f'{k}="{str(v).translate(_LP_FIELD)}"'


def _line(measurement, tags, fields, ts_ns):
    fps = [p for p in (_field_kv(k, v) for k, v in fields.items()) if p]
    if not fps:
        return None
    tps = [f"{_tag(k)}={_tag(v)}" for k, v in tags.items() if v not in (None, "")]
    head = measurement + ("," + ",".join(tps) if tps else "")
    return f"{head} {','.join(fps)} {ts_ns}"


def build_payload(data: dict) -> str:
    ts = data["timestamp_ns"]
    unit = data.get("unit_id") or "unknown"
    lines = []

    sysl = _line(SYSTEM_MEASUREMENT, {"unit_id": unit}, {
        "lmus_reporting": data.get("lmus_reporting"),
        "lmus_total":     data.get("lmus_total"),
        "status_message": data.get("status_message"),
    }, ts)
    if sysl:
        lines.append(sysl)

    for p in data["panels"]:
        l = _line(PANEL_MEASUREMENT,
            {"unit_id": unit, "label": p["label"], "barcode": p["barcode"],
             "mac": p["mac"], "slot": p["slot"]},
            {
                "vin": p["vin"], "vin_pct": p["vin_pct"],
                "vout": p["vout"], "vout_pct": p["vout_pct"],
                "current_a": p["current_a"],
                "power_w": p["power_w"], "power_pct": p["power_pct"],
                "temp_c": p["temp_c"],
                "rssi": p["rssi"], "brssi": p["brssi"],
                "vmpe": p["vmpe"],
                "sync_evt": p["sync_evt"], "mode": p["mode"],
                "bypass": p["bypass"], "event": p["event"],
                "status_raw": p["status_raw"], "extra_raw": p["extra_raw"],
                "details_raw": p["details_raw"],
                "reporting": p["vin"] is not None,
            }, ts)
        if l:
            lines.append(l)
    return "\n".join(lines)


def write_influx(payload: str) -> None:
    if not payload:
        return
    url = f"{INFLUX_URL.rstrip('/')}/api/v2/write"
    params = {"org": INFLUX_ORG, "bucket": INFLUX_BUCKET, "precision": "ns"}
    headers = {
        "Authorization": f"Token {INFLUX_TOKEN}",
        "Content-Type":  "text/plain; charset=utf-8",
    }
    r = _session.post(url, params=params, headers=headers,
                      data=payload.encode("utf-8"), timeout=HTTP_TIMEOUT)

    if r.status_code >= 300:
        raise RuntimeError(f"InfluxDB write failed {r.status_code}: {r.text}")


def scrape_and_write() -> dict:
    """Run one scrape + write cycle. Returns the parsed data dict."""
    html = fetch_html()
    data = parse_page(html)
    write_influx(build_payload(data))
    return data
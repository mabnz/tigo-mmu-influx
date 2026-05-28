const REFRESH_MS = 5000;        // poll cadence; independent of scrape interval
const PANEL_MAX_WATTS = 505;    // cap for per-panel power bar scaling

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
    // header pill — reflect actual data freshness, not just scrape success.
    // If the MMU is reporting a status message (e.g. cloud disconnected) and
    // the underlying panel readings haven't changed in a while, we shouldn't
    // claim we're "live" just because scrapes are still succeeding.
    const conn = document.getElementById("conn");
    const lbl  = conn.querySelector(".label");
    const freshnessEpoch = data.last_status_message
        ? (data.last_data_received_at || data.last_success_at)
        : data.last_success_at;
    const ageS = freshnessEpoch
                 ? Math.round(Date.now()/1000 - freshnessEpoch) : null;
    let cls = "pill";
    if (ageS == null)       { cls += " bad";  lbl.textContent = "no data"; }
    else if (ageS <= 30)    { cls += " ok";   lbl.textContent = "live"; }
    else if (ageS <= 120)   { cls += " warn"; lbl.textContent = "stale"; }
    else                    { cls += " bad";  lbl.textContent = "offline"; }
    conn.className = cls;

    // status message pill + matching "data last updated" pill
    const statusPill = document.getElementById("status-msg");
    const dataAgePill = document.getElementById("data-age");
    if (data.last_status_message) {
        statusPill.querySelector(".label").textContent = "Cloud disconnected";
        statusPill.title = data.last_status_message;
        statusPill.hidden = false;

        document.getElementById("data-age-val").textContent =
            timeAgo(data.last_data_received_at);
        dataAgePill.hidden = false;
    } else {
        statusPill.hidden = true;
        dataAgePill.hidden = true;
    }

    // summary
    const panels = data.panels || [];
    const reporting = panels.filter(p => p.vin != null);
    const totalPower = reporting.reduce((a, p) => a + (p.power_w || 0), 0);

    document.getElementById("s-reporting").textContent =
        reporting.length + " / " + panels.length;
    document.getElementById("s-reporting-bar").style.width =
        (panels.length ? (reporting.length / panels.length * 100) : 0) + "%";
    document.getElementById("s-power").innerHTML =
        fmt(totalPower, 2) + ' <small style="font-size:13px;color:var(--muted)">W</small>';

    // Per-string breakdown (string letter = first char of panel label, e.g. A1 -> A)
    const stringTotals = {};
    for (const p of reporting) {
        const s = (p.label || "").charAt(0).toUpperCase();
        if (!s) continue;
        stringTotals[s] = (stringTotals[s] || 0) + (p.power_w || 0);
    }
    const stringKeys = Object.keys(stringTotals).sort();
    document.getElementById("s-power-sub").textContent = stringKeys.length
        ? stringKeys.map(k => `${k} ${fmt(stringTotals[k], 0)} W`).join(" · ")
        : "";
    document.getElementById("s-unit").textContent = data.last_unit_id || "—";
    document.getElementById("s-started").textContent =
        data.started_at ? "since " + data.started_at : "";
    document.getElementById("s-interval").innerHTML =
        (data.current_interval_sec || "—") + ' <small style="font-size:13px;color:var(--muted)">s</small>';
    document.getElementById("s-failures").textContent =
        data.consecutive_failures
            ? data.consecutive_failures.toLocaleString() + " consecutive failures"
            : (data.success_count.toLocaleString() + " successful scrapes");

    // grid
    // If the underlying panel readings haven't changed in >30min, mark each
    // reporting panel's badge as "stale" — the values shown are last-known,
    // not live.
    const STALE_AFTER_SEC = 30 * 60;
    const dataAgeS = data.last_data_received_at
        ? Math.round(Date.now()/1000 - data.last_data_received_at) : null;
    const dataStale = dataAgeS != null && dataAgeS > STALE_AFTER_SEC;

    const grid = document.getElementById("grid");
    grid.innerHTML = "";
    for (const p of panels) {
        const offline = p.vin == null;
        const stale   = !offline && dataStale;
        const card = document.createElement("div");
        card.className = "card"
            + (offline ? " offline" : "")
            + (stale   ? " stale"   : "");

        const bars = rssiBars(p.rssi);
        const rssiHtml = `<span class="rssi" title="RSSI ${p.rssi ?? "n/a"}">
            ${[1,2,3,4].map(i =>
                `<i class="${bars >= i ? "on" : ""}"></i>`).join("")}
        </span>`;

        // Scale bar to each panel's output, capped at PANEL_MAX_WATTS.
        const powerW = Number(p.power_w) || 0;
        const powerPct = Math.max(0, Math.min(100, (powerW / PANEL_MAX_WATTS) * 100));

        const eventHtml = p.event
            ? `<div class="event${p.power_w === 0 ? " danger" : ""}">Event: ${p.event}</div>`
            : "";

        const badgeText = offline ? "offline" : (stale ? "stale" : "online");
        card.innerHTML = `
            <div class="top">
                <span class="label">${p.label ?? "?"}</span>
                <span class="badge">${badgeText}</span>
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
                ${eventHtml}
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

/* ==========================================================================
   AI Quant Trading Dashboard — Frontend Logic (India + US Market Support)
   ========================================================================== */

let activeMarket = "INDIA"; // "INDIA" or "US"
let liveGeneration = 0;
let scannerGeneration = 0;
let liveAbort = null;
let scannerAbort = null;

document.addEventListener("DOMContentLoaded", () => {
    fetchLiveData();
    fetchScannerData();
    fetchScoutData();
    fetchLogs();
    fetchTrades();
    fetchHealth();
    setInterval(fetchLiveData, 3000);
    setInterval(fetchScannerData, 60000);
    setInterval(fetchScoutData, 60000);
    setInterval(fetchLogs, 5000);
    setInterval(fetchTrades, 15000);
    setInterval(fetchHealth, 10000);
});

function isLiveStale(gen) {
    return gen !== liveGeneration;
}

function isScannerStale(gen) {
    return gen !== scannerGeneration;
}

function beginLiveFetch() {
    if (liveAbort) liveAbort.abort();
    liveAbort = new AbortController();
    liveGeneration += 1;
    return { gen: liveGeneration, signal: liveAbort.signal };
}

function beginScannerFetch() {
    if (scannerAbort) scannerAbort.abort();
    scannerAbort = new AbortController();
    scannerGeneration += 1;
    return { gen: scannerGeneration, signal: scannerAbort.signal };
}

function currentFormatter() {
    return activeMarket === "US" ? formatUSD : formatINR;
}

function switchMarket(market) {
    if (activeMarket === market) return;
    activeMarket = market;

    // Update tab styling
    document.getElementById("tab-india").classList.toggle("active", market === "INDIA");
    document.getElementById("tab-us").classList.toggle("active", market === "US");

    // Update headers and universe label
    const headerSub = document.getElementById("header-sub");
    const univSub = document.getElementById("kpi-universe-sub");
    const scannerTitle = document.getElementById("scanner-title");
    const scoutPanel = document.getElementById("scout-panel");

    if (market === "US") {
        if (headerSub) headerSub.textContent = "US Trading Engine (Dhan Global)";
        if (univSub) univSub.textContent = "Universe: US Equities (Dhan Global)";
        if (scannerTitle) scannerTitle.innerHTML = `<i class="fa-solid fa-radar"></i> Strategy Scanner (US Equities)`;
        if (scoutPanel) scoutPanel.style.display = "none";
    } else {
        if (headerSub) headerSub.textContent = "India Trading Engine";
        if (univSub) univSub.textContent = "Universe: Nifty Large-Caps (Dhan / NSE)";
        if (scannerTitle) scannerTitle.innerHTML = `<i class="fa-solid fa-radar"></i> Strategy Scanner (NSE)`;
        if (scoutPanel) scoutPanel.style.display = "";
        fetchScoutData();
    }

    clearMarketPanels();
    fetchCurrentTabData();
    fetchTrades();
}

function clearMarketPanels() {
    document.getElementById("equity-val").textContent = "—";
    document.getElementById("equity-sub").textContent = "Loading…";
    document.getElementById("daily-pl-val").textContent = "—";
    document.getElementById("daily-pl-pct").textContent = "…";
    document.getElementById("daily-pl-val").style.color = "";
    document.getElementById("daily-pl-pct").className = "kpi-badge";
    document.getElementById("buying-power-val").textContent = "—";
    document.getElementById("cash-val").textContent = "…";
    document.getElementById("positions-count").textContent = "…";
    document.getElementById("table-count-tag").textContent = "Loading…";
    document.getElementById("market-status").textContent = "Loading…";
    document.getElementById("last-updated").textContent = "Updating…";

    document.getElementById("positions-tbody").innerHTML =
        `<tr><td colspan="9" class="empty-state"><i class="fa-solid fa-spinner fa-spin"></i> Loading positions…</td></tr>`;
    document.getElementById("scanner-container").innerHTML =
        `<div class="loading-state"><i class="fa-solid fa-spinner fa-spin"></i> Loading scanner…</div>`;
    const scoutBody = document.getElementById("scout-tbody");
    if (scoutBody && activeMarket === "INDIA") {
        scoutBody.innerHTML =
            `<tr><td colspan="6" class="empty-state"><i class="fa-solid fa-spinner fa-spin"></i> Loading scout…</td></tr>`;
    }
}

function fetchLiveData() {
    const { gen, signal } = beginLiveFetch();
    fetchLive(gen, signal);
}

function fetchScannerData() {
    const { gen, signal } = beginScannerFetch();
    fetchScanner(gen, signal);
}

function fetchCurrentTabData() {
    fetchLiveData();
    fetchScannerData();
    if (activeMarket === "INDIA") fetchScoutData();
}

async function fetchScoutData() {
    if (activeMarket !== "INDIA") return;
    const tbody = document.getElementById("scout-tbody");
    const hint = document.getElementById("scout-hint");
    if (!tbody) return;
    try {
        const res = await fetch("/api/scout");
        if (!res.ok) return;
        const data = await res.json();
        updateScoutUI(data);
        if (hint && data.message) {
            const meta = data.meta || {};
            const scanned = meta.scanned != null ? ` · scanned ${meta.scanned}` : "";
            const buys = meta.buys != null ? ` · buys ${meta.buys}` : "";
            hint.textContent = `${data.message}${scanned}${buys}`;
        }
    } catch (err) {
        console.error("Error fetching scout:", err);
    }
}

function updateScoutUI(data) {
    const tbody = document.getElementById("scout-tbody");
    if (!tbody) return;
    const items = (data && data.items) || [];
    if (!data || data.enabled === false) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-state">${(data && data.message) || "Scout disabled"}</td></tr>`;
        return;
    }
    if (items.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="empty-state">${(data && data.message) || "No near setups yet."}</td></tr>`;
        return;
    }
    const fmt = currentFormatter();
    tbody.innerHTML = items.map((row) => {
        const score = Number(row.score) || 0;
        const scoreClass = score >= 70 ? "scout-score-hot" : score >= 50 ? "scout-score-warm" : "";
        const inTrade = row.in_core_universe || row.in_trade_universe
            ? `<span class="scout-in-trade" title="Also on core Strategy Scanner">core</span>`
            : `<span class="scout-ready" title="Scout-only — trade-eligible on full signal">scout</span>`;
        const ready = "";
        const rsi = row.rsi != null ? row.rsi : "—";
        const adx = row.adx != null ? row.adx : "—";
        const price = row.price != null && row.price > 0 ? fmt(row.price) : "—";
        return `
            <tr>
                <td style="font-weight:700;">${row.symbol || ""} ${inTrade}${ready}</td>
                <td class="mono">${price}</td>
                <td class="mono">${rsi}</td>
                <td class="mono">${adx}</td>
                <td class="mono ${scoreClass}">${score.toFixed(1)}</td>
                <td class="scout-reason">${row.reason || "—"}</td>
            </tr>`;
    }).join("");
}

async function fetchLive(gen, signal) {
    const statusUrl = activeMarket === "US" ? '/api/us/status' : '/api/status';
    const posUrl = activeMarket === "US" ? '/api/us/positions' : '/api/positions';
    const fmt = currentFormatter();

    try {
        const [statusRes, positionsRes] = await Promise.all([
            fetch(statusUrl, { signal }),
            fetch(posUrl, { signal })
        ]);
        if (isLiveStale(gen)) return;

        if (statusRes.ok) {
            const status = await statusRes.json();
            if (isLiveStale(gen)) return;
            if (status.status === "disabled" || status.status === "error") {
                renderDisabledState(status.message);
                return;
            }
            updateStatusUI(status);
        } else {
            renderDisabledState(activeMarket === "US" ? "Set DHAN_* keys & US_PAPER=true" : "Add Dhan / Angel keys in environment");
        }

        if (positionsRes.ok) {
            const positions = await positionsRes.json();
            if (isLiveStale(gen)) return;
            updatePositionsUI(positions, fmt, 'closePosition');
        }
    } catch (err) {
        if (err.name === 'AbortError' || isLiveStale(gen)) return;
        console.error("Error fetching live data:", err);
        renderDisabledState(activeMarket === "US" ? "Set DHAN_* keys & US_PAPER=true" : "Add Dhan / Angel keys in environment");
    }
}

async function fetchScanner(gen, signal) {
    const scannerUrl = activeMarket === "US" ? '/api/us/scanner' : '/api/scanner';
    const fmt = currentFormatter();

    try {
        const scannerRes = await fetch(scannerUrl, { signal });
        if (isScannerStale(gen) || !scannerRes.ok) return;
        const scanner = await scannerRes.json();
        if (isScannerStale(gen)) return;
        updateScannerUI(scanner, fmt);
    } catch (err) {
        if (err.name === 'AbortError' || isScannerStale(gen)) return;
        console.error("Error fetching scanner:", err);
    }
}

function updateKillSwitchBadge(active) {
    const badge = document.getElementById("kill-switch-badge");
    const statusEl = document.getElementById("system-status");
    const icon = badge ? badge.querySelector("i") : null;
    if (!badge || !statusEl) return;

    if (active) {
        statusEl.textContent = `${activeMarket} Kill Switch ON`;
        badge.style.color = "var(--danger)";
        badge.style.background = "var(--danger-bg)";
        if (icon) icon.className = "fa-solid fa-triangle-exclamation";
    } else {
        statusEl.textContent = "System Normal";
        badge.style.color = "";
        badge.style.background = "";
        if (icon) icon.className = "fa-solid fa-circle-check";
    }
}

function updateStatusUI(data) {
    if (!data || data.status === "error") return;

    const fmt = currentFormatter();
    document.getElementById("equity-val").textContent = fmt(data.equity);
    document.getElementById("equity-sub").textContent = activeMarket === "US" ? "Dhan Global Account (USD)" : "India Broker Account (INR)";

    const dailyPlEl = document.getElementById("daily-pl-val");
    const dailyPctEl = document.getElementById("daily-pl-pct");
    const pl = data.daily_pl != null ? data.daily_pl : 0;
    const plPct = data.daily_pl_pct != null ? data.daily_pl_pct : 0;
    dailyPlEl.textContent = `${pl >= 0 ? '+' : ''}${fmt(pl)}`;
    dailyPctEl.textContent = `${plPct >= 0 ? '+' : ''}${Number(plPct).toFixed(2)}%`;
    dailyPlEl.style.color = pl >= 0 ? "var(--success)" : "var(--danger)";
    dailyPctEl.className = pl >= 0 ? "kpi-badge positive" : "kpi-badge negative";

    document.getElementById("buying-power-val").textContent = fmt(data.available_cash);
    document.getElementById("cash-val").textContent = data.paper_trading
        ? `Paper sim (live ${activeMarket === "US" ? "US" : "NSE"} prices)`
        : `Margin Used: ${fmt(data.used_margin || 0)}`;

    const modeText = document.getElementById("mode-text");
    if (modeText) {
        if (activeMarket === "US") {
            modeText.textContent = data.paper_trading
                ? "US Paper (live USD)"
                : (data.live_armed ? "US LIVE MONEY" : "US SCAN only");
        } else {
            modeText.textContent = data.paper_trading
                ? "India Paper (live NSE)"
                : (data.live_armed ? "India LIVE MONEY" : "India SCAN only");
        }
    }

    const marketStatusEl = document.getElementById("market-status");
    const marketBadge = document.getElementById("market-badge");
    if (data.market_open) {
        marketStatusEl.textContent = activeMarket === "US" ? "NYSE Open" : "NSE India Open";
        marketBadge.style.color = "var(--success)";
        marketBadge.style.background = "var(--success-bg)";
    } else {
        marketStatusEl.textContent = activeMarket === "US" ? "NYSE Closed" : "NSE India Closed";
        marketBadge.style.color = "var(--warning)";
        marketBadge.style.background = "var(--warning-bg)";
    }

    updateKillSwitchBadge(!!data.kill_switch_active);
    if (data.timestamp) {
        const parts = data.timestamp.split(' ');
        if (parts.length > 1) {
            document.getElementById("last-updated").textContent = `Updated: ${parts[1]}`;
        }
    }
}

function renderDisabledState(msg) {
    const zero = activeMarket === "US" ? "$0.00" : "₹0.00";
    document.getElementById("equity-val").textContent = `${activeMarket} Pending`;
    document.getElementById("equity-sub").textContent = msg || "Check credentials in environment";
    document.getElementById("daily-pl-val").textContent = zero;
    document.getElementById("daily-pl-pct").textContent = "0.00%";
    document.getElementById("buying-power-val").textContent = zero;
    document.getElementById("cash-val").textContent = `Cash: ${zero}`;

    const marketStatusEl = document.getElementById("market-status");
    const marketBadge = document.getElementById("market-badge");
    marketStatusEl.textContent = "Keys Needed";
    marketBadge.style.color = "var(--warning)";
    marketBadge.style.background = "var(--warning-bg)";
}

function updatePositionsUI(positions, formatter, closeFnName) {
    const tbody = document.getElementById("positions-tbody");
    document.getElementById("positions-count").textContent = positions.length;
    document.getElementById("table-count-tag").textContent = `${positions.length} Active`;

    if (!positions || positions.length === 0) {
        tbody.innerHTML = `<tr><td colspan="9" class="empty-state">No open ${activeMarket} positions currently held.</td></tr>`;
        return;
    }

    let rowsHtml = "";
    positions.forEach(pos => {
        const plClass = pos.unrealized_pl >= 0 ? "pl-positive" : "pl-negative";
        const plSign = pos.unrealized_pl >= 0 ? "+" : "";
        const side = String(pos.side || "BUY").toUpperCase();
        const qtyText = side === "SELL" ? `-${Math.abs(Number(pos.qty || 0))}` : `${pos.qty}`;
        const sideClass = side === "SELL" ? "side-sell" : "side-buy";

        rowsHtml += `
            <tr>
                <td style="font-weight:700; color:var(--text-main);">
                    ${pos.symbol} <span class="side-pill ${sideClass}">${side}</span>
                </td>
                <td>${qtyText}</td>
                <td>${formatter(pos.avg_entry_price)}</td>
                <td>${formatter(pos.current_price)}</td>
                <td>${formatter(pos.market_value)}</td>
                <td style="color:var(--danger);">${formatter(pos.stop_loss)}</td>
                <td style="color:var(--success);">${formatter(pos.take_profit)}</td>
                <td class="${plClass}">${plSign}${formatter(pos.unrealized_pl)} (${plSign}${Number(pos.unrealized_plpc || 0).toFixed(2)}%)</td>
                <td>
                    <button class="btn btn-danger btn-sm" onclick="${closeFnName}('${pos.symbol}')">
                        Close
                    </button>
                </td>
            </tr>
        `;
    });

    tbody.innerHTML = rowsHtml;
}

function updateScannerUI(scannerList, formatter) {
    const container = document.getElementById("scanner-container");
    if (!scannerList || scannerList.length === 0) {
        container.innerHTML = `<div class="loading-state">No scanner data yet.</div>`;
        return;
    }

    let cardsHtml = "";
    scannerList.forEach(item => {
        const signalClass = `signal-${item.signal}`;
        const rsiStyle = item.rsi < 35
            ? 'color:var(--success);font-weight:700;'
            : item.rsi > 65
                ? 'color:var(--danger);font-weight:700;'
                : '';

        cardsHtml += `
            <div class="stock-card">
                <div class="stock-head">
                    <span class="stock-sym">${item.symbol}</span>
                    <span class="stock-price">${item.price > 0 ? formatter(item.price) : 'N/A'}</span>
                </div>
                <div class="signal-badge ${signalClass}">${item.signal}</div>
                <div class="indicator-list">
                    <div class="ind-row">
                        <span>14-RSI:</span>
                        <span class="ind-val" style="${rsiStyle}">${item.rsi != null ? item.rsi : '-'}</span>
                    </div>
                    ${item.reason ? `<div class="ind-row"><span>Why:</span><span class="ind-val" style="font-size:0.75rem;">${item.reason}</span></div>` : ''}
                </div>
                ${activeMarket === "US" ? `
                <div style="margin-top: 10px; text-align: right;">
                    <button class="btn btn-buy btn-sm" onclick="buyStock('${item.symbol}')">
                        <i class="fa-solid fa-cart-shopping"></i> Buy
                    </button>
                </div>
                ` : ''}
            </div>
        `;
    });

    container.innerHTML = cardsHtml;
}

async function buyStock(symbol) {
    if (activeMarket !== "US") {
        alert("Manual buy from scanner is available on the US tab.");
        return;
    }
    if (!confirm(`Place BUY order for ${symbol}? (Risk manager will size position)`)) return;
    try {
        const res = await fetch('/api/us/buy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol })
        });
        const data = await res.json();
        alert(data.message);
        fetchCurrentTabData();
    } catch (e) {
        alert("Failed to place buy order: " + e);
    }
}

async function fetchLogs() {
    try {
        const res = await fetch('/api/logs');
        if (!res.ok) return;
        const data = await res.json();
        const logBox = document.getElementById("log-terminal");
        logBox.innerHTML = "";

        data.logs.forEach(line => {
            const div = document.createElement("div");
            div.className = "log-line";
            if (line.includes("BUY")) div.classList.add("buy");
            else if (line.includes("SELL") || line.includes("CLOSE")) div.classList.add("sell");
            else if (line.includes("CRITICAL") || line.includes("KILL-SWITCH")) div.classList.add("critical");
            else if (line.includes("WARNING") || line.includes("CLOSED")) div.classList.add("warn");
            else div.classList.add("info");
            div.textContent = line;
            logBox.appendChild(div);
        });

        logBox.scrollTop = logBox.scrollHeight;
    } catch (e) {
        console.error("Error fetching logs:", e);
    }
}

async function fetchTrades() {
    const tbody = document.getElementById("trades-tbody");
    if (!tbody) return;
    try {
        const res = await fetch(`/api/trades?market=${activeMarket}&limit=50`);
        if (!res.ok) return;
        const trades = await res.json();
        if (!trades.length) {
            tbody.innerHTML = `<tr><td colspan="6" class="empty-state">No journaled ${activeMarket} trades yet.</td></tr>`;
            return;
        }
        tbody.innerHTML = trades.map(t => {
            const pnl = t.pnl != null ? Number(t.pnl) : null;
            const plClass = pnl == null ? '' : (pnl >= 0 ? 'pl-positive' : 'pl-negative');
            const fmt = currentFormatter();
            return `<tr>
                <td>${t.symbol}</td>
                <td>${t.side || t.status || '—'}</td>
                <td>${t.qty ?? '—'}</td>
                <td>${t.entry_price != null ? fmt(t.entry_price) : '—'}</td>
                <td class="${plClass}">${pnl != null ? (pnl >= 0 ? '+' : '') + fmt(pnl) : '—'}</td>
                <td>${(t.closed_at || t.opened_at || '').toString().slice(0, 19)}</td>
            </tr>`;
        }).join('');
    } catch (e) {
        console.error("Error fetching trades:", e);
    }
}

async function fetchHealth() {
    const el = document.getElementById("health-text");
    if (!el) return;
    try {
        const res = await fetch('/api/health');
        if (!res.ok) return;
        const h = await res.json();
        const inAge = h.india_cycle_age_sec != null ? `${Math.round(h.india_cycle_age_sec)}s` : '—';
        const usAge = h.us_cycle_age_sec != null ? `${Math.round(h.us_cycle_age_sec)}s` : '—';
        el.textContent = `IN ${inAge} | US ${usAge}`;
        if (h.india_last_error || h.us_last_error) {
            el.textContent += ' | err';
            el.style.color = 'var(--warning)';
        } else {
            el.style.color = '';
        }
    } catch (e) {
        /* ignore */
    }
}

async function closePosition(symbol) {
    if (!confirm(`Close position for ${symbol}?`)) return;
    const url = activeMarket === "US" ? `/api/us/close_position/${symbol}` : `/api/close_position/${symbol}`;
    try {
        const res = await fetch(url, { method: 'POST' });
        const data = await res.json();
        if (data.status === "success" && data.pnl != null) {
            const fmt = activeMarket === "US" ? formatUSD : formatINR;
            alert(
                `${data.message}\n` +
                `Entry ${fmt(data.entry_price)} → Exit ${fmt(data.exit_price)}\n` +
                `Equity now: ${fmt(data.equity)} | Today's P&L: ${fmt(data.daily_pl)}`
            );
        } else {
            alert(data.message || "Close failed");
        }
        fetchCurrentTabData();
        fetchTrades();
    } catch (e) {
        alert("Failed to close position: " + e);
    }
}

async function toggleKillSwitch() {
    const url = activeMarket === "US" ? '/api/us/toggle_kill_switch' : '/api/toggle_kill_switch';
    try {
        const res = await fetch(url, { method: 'POST' });
        const data = await res.json();
        alert(data.message);
        fetchCurrentTabData();
    } catch (e) {
        alert("Failed to toggle kill switch: " + e);
    }
}

function formatUSD(val) {
    if (val == null || isNaN(val)) return "$0.00";
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(val);
}

function formatINR(val) {
    if (val == null || isNaN(val)) return "₹0.00";
    return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 }).format(val);
}

async function fetchSegmentsStatus() {
    try {
        const res = await fetch('/api/segments/status');
        if (!res.ok) return;
        const data = await res.json();
        const wsEl = document.getElementById("segment-ws-status");
        const usWsEl = document.getElementById("segment-us-ws-status");
        const prodEl = document.getElementById("segment-product-type");
        const fnoEl = document.getElementById("segment-fno-status");
        const mcxEl = document.getElementById("segment-mcx-status");
        const fxEl = document.getElementById("segment-fx-status");

        if (wsEl) {
            const feed = data.dhan_live_feed || {};
            const isConn = feed.connected;
            const age = feed.last_heartbeat_age_sec;
            const sub = feed.subscribed_count != null ? feed.subscribed_count : "?";
            wsEl.innerHTML = isConn
                ? `<i class="fa-solid fa-wifi" style="color: #4ade80;"></i> Connected (${sub} sym, ${age}s)`
                : `<i class="fa-solid fa-wifi" style="color: #f87171;"></i> REST Fallback`;
        }
        if (usWsEl) {
            const usFeed = data.dhan_us_live_feed || (data.segments && data.segments.us_global && data.segments.us_global.live_feed) || {};
            const isConn = usFeed.connected;
            const age = usFeed.last_heartbeat_age_sec;
            const sub = usFeed.subscribed_count != null ? usFeed.subscribed_count : "?";
            const mode = usFeed.mode || "off";
            if (!usFeed.enabled && mode === "rest_yahoo_fallback") {
                usWsEl.innerHTML = `<i class="fa-solid fa-wifi" style="color: #94a3b8;"></i> Off (Yahoo/REST)`;
            } else if (isConn) {
                usWsEl.innerHTML = `<i class="fa-solid fa-wifi" style="color: #4ade80;"></i> US Live (${sub} sym, ${age}s)`;
            } else {
                usWsEl.innerHTML = `<i class="fa-solid fa-wifi" style="color: #fbbf24;"></i> US ${mode}`;
            }
        }
        if (prodEl && data.product_type) {
            prodEl.textContent = data.product_type + " Mode";
        }
        function segLabel(seg) {
            if (!seg) return "—";
            const pos = seg.positions_count != null ? seg.positions_count : 0;
            const util = seg.utilization && seg.utilization.utilization_pct != null
                ? ` · cap ${seg.utilization.utilization_pct}%`
                : "";
            const kill = seg.kill_switch ? " · KILL" : "";
            const on = seg.enabled ? "" : " (OFF)";
            return `${seg.mode}${on} · ${pos} pos${util}${kill}`;
        }
        if (fnoEl && data.segments.india_fno) {
            fnoEl.textContent = segLabel(data.segments.india_fno);
        }
        if (mcxEl && data.segments.mcx_commodities) {
            mcxEl.textContent = segLabel(data.segments.mcx_commodities);
        }
        if (fxEl && data.segments.currency_fx) {
            fxEl.textContent = segLabel(data.segments.currency_fx);
        }

        updateExpansionPositionsUI(data.expansion_positions || []);
    } catch (e) {
        console.debug("Segment status poll error:", e);
    }
}

function updateExpansionPositionsUI(rows) {
    const tbody = document.getElementById("expansion-positions-tbody");
    const countEl = document.getElementById("expansion-pos-count");
    if (!tbody) return;
    const list = Array.isArray(rows) ? rows : [];
    if (countEl) countEl.textContent = `${list.length} Active`;
    if (list.length === 0) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No F&O / MCX / FX positions.</td></tr>`;
        return;
    }
    const fmt = (n) => {
        const v = Number(n) || 0;
        return "₹" + v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    };
    tbody.innerHTML = list.map((pos) => {
        const plClass = (pos.unrealized_pl || 0) >= 0 ? "pl-positive" : "pl-negative";
        const plSign = (pos.unrealized_pl || 0) >= 0 ? "+" : "";
        return `
            <tr>
                <td>${pos.segment || ""}</td>
                <td style="font-weight:700;">${pos.symbol || ""}</td>
                <td>${pos.qty ?? 0}</td>
                <td>${fmt(pos.avg_entry_price)}</td>
                <td>${fmt(pos.current_price)}</td>
                <td>${fmt(pos.market_value)}</td>
                <td class="${plClass}">${plSign}${fmt(pos.unrealized_pl)} (${plSign}${pos.unrealized_plpc ?? 0}%)</td>
            </tr>`;
    }).join("");
}

// Global Poll Timers
document.addEventListener("DOMContentLoaded", () => {
    fetchSegmentsStatus();
    setInterval(fetchSegmentsStatus, 5000);
});

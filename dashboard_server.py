"""
dashboard_server.py — Web Admin Dashboard Backend
=============================================================
Provides REST API and Web Interface for India (Dhan/Angel) and US (Dhan Global) trading bot.

Supports:
  - India and US market endpoints (/api/status vs /api/us/status, etc.)
  - Cloud deployment (Render, Heroku) via PORT env var
"""

import logging
import os
import time
import json
from datetime import datetime, timezone
import threading

from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS

import config
from strategy import Strategy, create_strategy
from risk_manager import RiskManager
import trade_journal
import bot_state
import alerts

logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)


def _broker_style_day_pl(
    market: str, broker, equity: float
) -> tuple[float, float, dict]:
    """Today's P&L ≈ Dhan: realized (closed today) + unrealized (open MTM).

    India MIS kill-switch uses this same figure (via day_pl=) — not raw equity vs SOD.
    """
    # Before summing journal realized, rewrite today's exits from Dhan SELL fills
    # when the broker exposes that lookup (fixes stale-LTP / missing-LTP rows).
    fill_lookup = getattr(broker, "latest_sell_fill_price", None) if broker else None
    if market.upper() == "INDIA" and callable(fill_lookup):
        try:
            trade_journal.resync_today_exits_from_fills(
                "INDIA",
                fill_lookup,
                tz_name="Asia/Kolkata",
            )
        except Exception as e:
            logger.debug(f"{market} day P&L fill resync skipped: {e}")

    positions = {}
    try:
        positions = broker.get_open_positions() or {}
    except Exception as e:
        logger.debug(f"{market} day P&L positions failed: {e}")

    unreal = 0.0
    cost = 0.0
    for pos in positions.values():
        if not isinstance(pos, dict):
            continue
        qty = abs(float(pos.get("qty") or 0))
        avg = float(pos.get("avg_entry_price") or 0)
        cur = float(pos.get("current_price") or 0)
        if qty > 0 and avg > 0:
            cost += qty * avg
        side = str(pos.get("side") or "BUY").upper()
        if qty > 0 and avg > 0 and cur > 0:
            if side == "SELL":
                unreal += (avg - cur) * qty
            else:
                unreal += (cur - avg) * qty
        else:
            unreal += float(pos.get("unrealized_pl") or 0)

    tz_name = "Asia/Kolkata" if market.upper() == "INDIA" else "America/New_York"
    try:
        realized = float(trade_journal.realized_pnl_today(market, tz_name=tz_name))
    except Exception as e:
        logger.debug(f"{market} realized day P&L failed: {e}")
        realized = 0.0

    day_pl = unreal + realized
    denom = cost if cost > 0 else (float(equity) if equity and equity > 0 else 0.0)
    day_pct = (day_pl / denom * 100.0) if denom else 0.0
    detail = {
        "unrealized": round(unreal, 2),
        "realized": round(realized, 2),
        "position_cost": round(cost, 2),
    }
    return day_pl, day_pct, detail

# India components
_india_broker = None
_india_strategy = None
_india_risk_mgr = None
_india_equity_history = []

SCANNER_CACHE_TTL_SEC = 90.0
_india_scanner_cache: tuple[float, list] | None = None
_india_scanner_lock = threading.Lock()

# US components
_us_broker = None
_us_strategy = None
_us_risk_mgr = None
_us_equity_history = []
_us_scanner_cache: tuple[float, list] | None = None
_us_scanner_lock = threading.Lock()


def get_india_components():
    """Initialize India market components — shared broker singleton."""
    global _india_broker, _india_strategy, _india_risk_mgr
    if not config.INDIA_ENABLED:
        return None, None

    if _india_broker is None:
        try:
            from india_client import get_shared_india_broker
            _india_broker = get_shared_india_broker(auto_login=True)
            _india_strategy = create_strategy("INDIA")
            _india_risk_mgr = RiskManager(market="INDIA")
        except Exception as e:
            logger.error(f"Error initializing India dashboard components: {e}")
    return _india_broker, _india_strategy


def get_india_risk():
    global _india_risk_mgr
    if _india_risk_mgr is None:
        _india_risk_mgr = RiskManager(market="INDIA")
    return _india_risk_mgr


def get_us_components():
    """Initialize US market components — shared broker singleton."""
    global _us_broker, _us_strategy, _us_risk_mgr
    if not config.US_ENABLED:
        return None, None

    if _us_broker is None:
        try:
            from us_client import get_shared_us_broker
            _us_broker = get_shared_us_broker(auto_login=True)
            _us_strategy = create_strategy("US")
            _us_risk_mgr = RiskManager(market="US")
        except Exception as e:
            logger.error(f"Error initializing US dashboard components: {e}")
    return _us_broker, _us_strategy


def get_us_risk():
    global _us_risk_mgr
    if _us_risk_mgr is None:
        _us_risk_mgr = RiskManager(market="US")
    return _us_risk_mgr


# ===========================================================================
# Web Page Route
# ===========================================================================
@app.route("/")
def index():
    return render_template("index.html")


# ===========================================================================
# SSE Live Stream — Real-time position/status push via WebSocket quote cache
# ===========================================================================
_sse_pos_cache: dict[str, tuple[float, dict]] = {}  # market -> (timestamp, positions_base)
_SSE_POS_REFRESH_SEC = max(
    3.0, float(getattr(config, "DASH_SSE_POS_REFRESH_SEC", 8.0))
)  # full broker API call interval for base position data


def _get_live_feed_for_market(market: str):
    """Return the appropriate live feed manager for the market."""
    if market == "US":
        try:
            from dhan_us_live_feed import get_us_live_feed_manager
            return get_us_live_feed_manager()
        except Exception:
            return None
    else:
        try:
            from dhan_live_feed import get_live_feed_manager
            return get_live_feed_manager()
        except Exception:
            return None


def _fetch_positions_base(market: str) -> tuple[list, dict | None]:
    """Fetch full position data from broker (heavy API call). Returns (positions_list, account_info)."""
    if market == "US":
        broker, _ = get_us_components()
        risk_mgr = get_us_risk()
    else:
        broker, _ = get_india_components()
        risk_mgr = get_india_risk()

    if not broker or not broker.is_logged_in:
        return [], None

    positions_dict = broker.get_open_positions() or {}
    positions_list = []

    for symbol, pos in positions_dict.items():
        entry_price = pos["avg_entry_price"]
        side = str(pos.get("side") or "BUY").upper()
        atr = pos.get("atr")
        sl_price = pos.get("stop_loss")
        tp_price = pos.get("take_profit")

        if sl_price is None:
            if market == "INDIA" and side == "SELL":
                if atr is not None:
                    try:
                        sl_price = float(entry_price) + (
                            float(risk_mgr.atr_stop_mult) * float(atr)
                        )
                    except Exception:
                        sl_price = float(entry_price) * (1 + float(risk_mgr.stop_loss_pct))
                else:
                    sl_price = float(entry_price) * (1 + float(risk_mgr.stop_loss_pct))
            else:
                sl_price = risk_mgr.get_stop_loss_price(entry_price, atr)

        if tp_price is None:
            if market == "INDIA" and side == "SELL":
                risk = max(float(sl_price) - float(entry_price), 0.0)
                if risk > 0:
                    tp_price = float(entry_price) - (float(risk_mgr.take_profit_r) * risk)
                else:
                    tp_price = float(entry_price) * (1 - float(risk_mgr.take_profit_pct))
            else:
                tp_price = risk_mgr.get_take_profit_price(
                    entry_price, stop_loss_price=sl_price, atr=atr
                )

        positions_list.append({
            "symbol": symbol,
            "qty": pos["qty"],
            "side": side,
            "avg_entry_price": entry_price,
            "current_price": pos["current_price"],
            "market_value": pos["market_value"],
            "unrealized_pl": pos["unrealized_pl"],
            "unrealized_plpc": round(pos["unrealized_plpc"] * 100, 2),
            "stop_loss": sl_price,
            "take_profit": tp_price,
        })

    account_info = broker.get_account_info()
    return positions_list, account_info


def _apply_live_quotes(positions: list, feed) -> list:
    """Overlay live WebSocket LTP onto position data for instant price updates."""
    if not feed or not positions:
        return positions

    updated = []
    for pos in positions:
        pos = dict(pos)  # shallow copy
        quote = feed.get_live_quote(pos["symbol"])
        if quote and quote.get("ltp") and float(quote["ltp"]) > 0:
            ltp = float(quote["ltp"])
            q_ts = float(quote.get("timestamp") or 0)
            entry = float(pos["avg_entry_price"] or 0)
            qty = abs(int(pos["qty"] or 0))
            side = str(pos.get("side") or "BUY").upper()

            pos["current_price"] = ltp
            pos["market_value"] = qty * ltp
            if entry > 0 and qty > 0:
                if side == "SELL":
                    pos["unrealized_pl"] = (entry - ltp) * qty
                    pos["unrealized_plpc"] = round(((entry - ltp) / entry) * 100, 2)
                else:
                    pos["unrealized_pl"] = (ltp - entry) * qty
                    pos["unrealized_plpc"] = round(((ltp - entry) / entry) * 100, 2)
            pos["quote_age_sec"] = max(0.0, round(time.time() - q_ts, 2)) if q_ts > 0 else None
        else:
            pos["quote_age_sec"] = None
        updated.append(pos)
    return updated


def _build_status_payload(
    market: str,
    broker,
    account_info: dict | None,
    *,
    broker_positions_age_sec: float | None = None,
    quote_age_sec: float | None = None,
) -> dict | None:
    """Build a status payload similar to /api/status but lightweight for SSE."""
    if not account_info:
        return None

    equity = account_info["equity"]
    cash = account_info["available_cash"]

    if market == "US":
        risk_mgr = get_us_risk()
        last_eq = float(bot_state.us_sod_equity(equity))
        market_open = broker.is_market_open() if broker else False
    else:
        risk_mgr = get_india_risk()
        last_eq = float(bot_state.india_sod_equity(equity))
        try:
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
        except ImportError:
            import pytz
            IST = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.now(IST)
        is_weekday = now_ist.weekday() < 5
        mkt_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        mkt_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        market_open = is_weekday and mkt_open <= now_ist <= mkt_close

    daily_pl, daily_pl_pct, day_pl_detail = _broker_style_day_pl(
        market, broker, equity
    )

    return {
        "status": "success",
        "market": market,
        "currency": "USD" if market == "US" else "INR",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "equity": equity,
        "last_equity": last_eq,
        "daily_pl": round(daily_pl, 2),
        "daily_pl_pct": round(daily_pl_pct, 2),
        "available_cash": cash,
        "used_margin": account_info.get("used_margin", 0),
        "market_open": market_open,
        "logged_in": broker.is_logged_in if broker else False,
        "broker": "dhan_global" if market == "US" else config.INDIA_BROKER,
        "paper_trading": config.US_PAPER if market == "US" else config.INDIA_PAPER,
        "live_armed": config.US_LIVE_CONFIRMED if market == "US" else config.LIVE_CONFIRMED,
        "kill_switch_active": risk_mgr.is_kill_switch_active if risk_mgr else False,
        "broker_positions_age_sec": broker_positions_age_sec,
        "quote_age_sec": quote_age_sec,
    }


def _sse_generator(market: str):
    """Generator that yields SSE events with live position + status data."""
    global _sse_pos_cache

    tick = 0
    last_pos_refresh = 0.0
    positions_base = []
    account_info = None
    broker = None

    while True:
        try:
            now = time.time()

            # Refresh base position data from broker every _SSE_POS_REFRESH_SEC
            if now - last_pos_refresh > _SSE_POS_REFRESH_SEC:
                try:
                    positions_base, account_info = _fetch_positions_base(market)
                    if market == "US":
                        broker, _ = get_us_components()
                    else:
                        broker, _ = get_india_components()
                    last_pos_refresh = now
                except Exception as e:
                    logger.debug(f"SSE position refresh error ({market}): {e}")

            # Get live feed and overlay LTP from WebSocket cache
            feed = _get_live_feed_for_market(market)
            live_positions = _apply_live_quotes(positions_base, feed)

            # Every tick (~1s): push positions with live prices
            pos_payload = json.dumps(live_positions, default=str)
            yield f"event: positions\ndata: {pos_payload}\n\n"

            # Every 3 ticks (~3s): push status with P&L
            if tick % 3 == 0:
                try:
                    broker_age = (now - last_pos_refresh) if last_pos_refresh > 0 else None
                    quote_ages = [
                        float(p.get("quote_age_sec"))
                        for p in live_positions
                        if p.get("quote_age_sec") is not None
                    ]
                    worst_quote_age = max(quote_ages) if quote_ages else None
                    status = _build_status_payload(
                        market,
                        broker,
                        account_info,
                        broker_positions_age_sec=(
                            round(max(0.0, broker_age), 2) if broker_age is not None else None
                        ),
                        quote_age_sec=(
                            round(max(0.0, worst_quote_age), 2)
                            if worst_quote_age is not None
                            else None
                        ),
                    )
                    if status:
                        # Add live feed connection info
                        feed_connected = feed.is_connected() if feed else False
                        status["live_feed_connected"] = feed_connected
                        status_payload = json.dumps(status, default=str)
                        yield f"event: status\ndata: {status_payload}\n\n"
                except Exception as e:
                    logger.debug(f"SSE status build error: {e}")

            # Every 15 ticks (~15s): heartbeat
            if tick % 15 == 0:
                hb = json.dumps({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "feed_connected": feed.is_connected() if feed else False,
                    "symbols_cached": len(feed._quote_cache) if feed else 0,
                })
                yield f"event: heartbeat\ndata: {hb}\n\n"

            tick += 1
            time.sleep(1.0)

        except GeneratorExit:
            logger.debug(f"SSE client disconnected ({market})")
            return
        except Exception as e:
            logger.warning(f"SSE generator error ({market}): {e}")
            err = json.dumps({"error": str(e)})
            yield f"event: error\ndata: {err}\n\n"
            time.sleep(3.0)


@app.route("/api/live-stream")
def live_stream():
    """Server-Sent Events endpoint for real-time position/status updates."""
    market = request.args.get("market", "INDIA").upper()
    if market not in ("INDIA", "US"):
        market = "INDIA"

    return Response(
        _sse_generator(market),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ===========================================================================
# India API Endpoints
# ===========================================================================
@app.route("/api/status")
def get_india_status():
    """Get India market account status."""
    if not config.INDIA_ENABLED:
        return jsonify({
            "status": "disabled",
            "message": "India trading disabled. Add ANGEL_* or DHAN_* keys to environment."
        })

    india_broker, _ = get_india_components()
    if not india_broker or not india_broker.is_logged_in:
        err_msg = (
            india_broker.last_error
            if (india_broker and india_broker.last_error)
            else f"{config.INDIA_BROKER} authentication failed. Verify credentials."
        )
        return jsonify({
            "status": "error",
            "message": err_msg
        })

    account_info = india_broker.get_account_info()
    if not account_info:
        err_msg = (
            india_broker.last_error
            if india_broker.last_error
            else f"Unable to fetch {config.INDIA_BROKER} account info."
        )
        return jsonify({
            "status": "error",
            "message": err_msg
        })

    equity = account_info["equity"]
    cash = account_info["available_cash"]

    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if not _india_equity_history or _india_equity_history[-1]["timestamp"] != now_str:
        _india_equity_history.append({"timestamp": now_str, "equity": round(equity, 2)})
        if len(_india_equity_history) > 60:
            _india_equity_history.pop(0)

    try:
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
    except ImportError:
        import pytz
        IST = pytz.timezone("Asia/Kolkata")

    now_ist = datetime.now(IST)
    is_weekday = now_ist.weekday() < 5
    market_open_time = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close_time = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    india_market_open = is_weekday and market_open_time <= now_ist <= market_close_time

    india_risk = get_india_risk()
    # Display P&L = open MTM only (Dhan-style). Equity-vs-SOD for kill-switch / diagnostics.
    last_eq = float(bot_state.india_sod_equity(equity))
    equity_day_pl = equity - last_eq
    daily_pl, daily_pl_pct, day_pl_detail = _broker_style_day_pl(
        "INDIA", india_broker, equity
    )
    return jsonify({
        "status": "success",
        "market": "INDIA",
        "currency": "INR",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "equity": equity,
        "last_equity": last_eq,
        "daily_pl": round(daily_pl, 2),
        "daily_pl_pct": round(daily_pl_pct, 2),
        "equity_day_pl": round(equity_day_pl, 2),
        "day_pl_detail": day_pl_detail,
        "available_cash": cash,
        "used_margin": account_info.get("used_margin", 0),
        "market_open": india_market_open,
        "logged_in": india_broker.is_logged_in,
        "broker": config.INDIA_BROKER,
        "paper_trading": config.INDIA_PAPER,
        "live_armed": config.LIVE_CONFIRMED,
        "strategy": config.STRATEGY_NAME,
        "kill_switch_active": india_risk.is_kill_switch_active,
        "kill_switch_reason": bot_state.kill_switch_reason("INDIA"),
        "tide_bearish": bot_state.is_tide_bearish(),
        "tide": bot_state.get_tide_state(),
        "zombie_rescues": bot_state.list_zombie_rescues(),
        "equity_history": _india_equity_history,
        "performance": trade_journal.performance_stats("INDIA"),
        "open_risk_pct": round(
            india_risk.open_risk_pct(equity, india_broker.get_open_positions() or {}),
            4,
        ),
    })


@app.route("/api/positions")
def get_india_positions():
    if not config.INDIA_ENABLED:
        return jsonify([])

    india_broker, _ = get_india_components()
    if not india_broker or not india_broker.is_logged_in:
        return jsonify([])

    positions_dict = india_broker.get_open_positions() or {}
    positions_list = []

    risk_mgr = get_india_risk()

    for symbol, pos in positions_dict.items():
        entry_price = pos["avg_entry_price"]
        side = str(pos.get("side") or "BUY").upper()
        atr = pos.get("atr")
        sl_price = pos.get("stop_loss")
        tp_price = pos.get("take_profit")
        if sl_price is None:
            if side == "SELL":
                if atr is not None:
                    try:
                        sl_price = float(entry_price) + (
                            float(risk_mgr.atr_stop_mult) * float(atr)
                        )
                    except Exception:
                        sl_price = float(entry_price) * (1 + float(risk_mgr.stop_loss_pct))
                else:
                    sl_price = float(entry_price) * (1 + float(risk_mgr.stop_loss_pct))
            else:
                sl_price = risk_mgr.get_stop_loss_price(entry_price, atr)
        if tp_price is None:
            if side == "SELL":
                risk = max(float(sl_price) - float(entry_price), 0.0)
                if risk > 0:
                    tp_price = float(entry_price) - (float(risk_mgr.take_profit_r) * risk)
                else:
                    tp_price = float(entry_price) * (1 - float(risk_mgr.take_profit_pct))
            else:
                tp_price = risk_mgr.get_take_profit_price(
                    entry_price, stop_loss_price=sl_price, atr=atr
                )

        positions_list.append({
            "symbol": symbol,
            "qty": pos["qty"],
            "side": side,
            "avg_entry_price": entry_price,
            "current_price": pos["current_price"],
            "market_value": pos["market_value"],
            "unrealized_pl": pos["unrealized_pl"],
            "unrealized_plpc": round(pos["unrealized_plpc"] * 100, 2),
            "stop_loss": sl_price,
            "take_profit": tp_price
        })

    return jsonify(positions_list)


def _serialize_segment_positions(segment: str, positions: dict) -> list[dict]:
    """Normalize F&O / MCX / FX paper positions for the dashboard."""
    rows = []
    for key, pos in (positions or {}).items():
        qty = int(pos.get("qty") or 0)
        avg = float(pos.get("avg_entry_price") or pos.get("avg_price") or 0)
        cur = float(pos.get("current_price") or avg)
        mv = float(pos.get("market_value") or (qty * cur))
        upl = float(pos.get("unrealized_pl") or 0)
        uplpc = float(pos.get("unrealized_plpc") or 0)
        if abs(uplpc) < 1 and abs(uplpc) > 0 and abs(upl) > 0:
            # paper helpers return fraction; show percent like equity API
            uplpc = round(uplpc * 100, 2)
        else:
            uplpc = round(uplpc, 2)
        rows.append({
            "segment": segment,
            "symbol": pos.get("contract_key") or pos.get("symbol") or key,
            "qty": qty,
            "avg_entry_price": avg,
            "current_price": cur,
            "market_value": mv,
            "unrealized_pl": upl,
            "unrealized_plpc": uplpc,
            "stop_loss": float(pos.get("stop_loss") or 0),
            "take_profit": float(pos.get("take_profit") or 0),
        })
    return rows


@app.route("/api/segments/status")
def get_segments_status():
    from dhan_live_feed import get_live_feed_manager
    from india_fno_instruments import master_status

    ws_feed = get_live_feed_manager()

    us_feed_summary = {}
    us_master = {}
    try:
        from dhan_us_live_feed import get_us_live_feed_manager
        from us_instruments import master_status as us_master_status

        us_feed_summary = get_us_live_feed_manager().status_summary()
        us_master = us_master_status()
    except Exception:
        us_feed_summary = {"enabled": False, "connected": False, "mode": "unavailable"}

    fno_util = mcx_util = cur_util = {}
    fno_pos = mcx_pos = cur_pos = {}
    try:
        from india_fno_broker import get_shared_fno_broker

        fb = get_shared_fno_broker()
        fno_util = fb.capital_utilization()
        fno_pos = fb.get_open_positions()
    except Exception:
        pass
    try:
        from mcx_broker import get_shared_mcx_broker

        mb = get_shared_mcx_broker()
        mcx_util = mb.capital_utilization()
        mcx_pos = mb.get_open_positions()
    except Exception:
        pass
    try:
        from currency_broker import get_shared_currency_broker

        cb = get_shared_currency_broker()
        cur_util = cb.capital_utilization()
        cur_pos = cb.get_open_positions()
    except Exception:
        pass

    fno_rows = _serialize_segment_positions("F&O", fno_pos)
    mcx_rows = _serialize_segment_positions("MCX", mcx_pos)
    fx_rows = _serialize_segment_positions("FX", cur_pos)

    fno_info = {
        "enabled": config.INDIA_FNO_ENABLED,
        "mode": "PAPER" if config.INDIA_FNO_PAPER else ("LIVE" if config.INDIA_FNO_LIVE_CONFIRMED else "DISABLED"),
        "capital_cap": config.INDIA_FNO_CAPITAL_CAP,
        "max_lots": config.INDIA_FNO_MAX_LOTS,
        "utilization": fno_util,
        "positions_count": len(fno_pos),
        "positions": fno_rows,
        "kill_switch": fno_util.get("kill_switch", False),
    }
    mcx_info = {
        "enabled": config.MCX_ENABLED,
        "mode": "PAPER" if config.MCX_PAPER else ("LIVE" if config.MCX_LIVE_CONFIRMED else "DISABLED"),
        "capital_cap": config.MCX_CAPITAL_CAP,
        "utilization": mcx_util,
        "positions_count": len(mcx_pos),
        "positions": mcx_rows,
        "kill_switch": mcx_util.get("kill_switch", False),
    }
    currency_info = {
        "enabled": config.CURRENCY_ENABLED,
        "mode": "PAPER" if config.CURRENCY_PAPER else ("LIVE" if config.CURRENCY_LIVE_CONFIRMED else "DISABLED"),
        "capital_cap": config.CURRENCY_CAPITAL_CAP,
        "utilization": cur_util,
        "positions_count": len(cur_pos),
        "positions": fx_rows,
        "kill_switch": cur_util.get("kill_switch", False),
    }
    equity_info = {
        "enabled": config.INDIA_ENABLED,
        "mode": "PAPER" if config.INDIA_PAPER else ("LIVE" if config.LIVE_CONFIRMED else "DISABLED"),
        "product_type": config.INDIA_PRODUCT_TYPE,
    }
    us_info = {
        "enabled": config.US_ENABLED,
        "mode": "PAPER" if config.US_PAPER else ("LIVE" if config.US_LIVE_CONFIRMED else "DISABLED"),
        "capital_cap": float(getattr(config, "US_CAPITAL_CAP", 0) or 0),
        "live_feed": us_feed_summary,
        "scrip_master": us_master,
    }

    return jsonify({
        "status": "success",
        "dhan_live_feed": ws_feed.status_summary(),
        "dhan_us_live_feed": us_feed_summary,
        "scrip_master": master_status(),
        "us_scrip_master": us_master,
        "product_type": config.INDIA_PRODUCT_TYPE,
        "expansion_positions": fno_rows + mcx_rows + fx_rows,
        "segments": {
            "india_equity": equity_info,
            "india_fno": fno_info,
            "mcx_commodities": mcx_info,
            "currency_fx": currency_info,
            "us_global": us_info,
        },
    })


@app.route("/api/scanner")
def get_india_scanner():
    global _india_scanner_cache

    if not config.INDIA_ENABLED:
        return jsonify([])

    cached_signals = bot_state.get_signals("INDIA", max_age_sec=max(600, config.INDIA_LOOP_INTERVAL_SEC * 3))
    if cached_signals:
        return jsonify([
            {
                "symbol": s["symbol"],
                "price": s.get("price") or 0.0,
                "rsi": s.get("rsi"),
                "adx": s.get("adx"),
                "signal": s.get("signal", "HOLD"),
                "reason": s.get("reason", ""),
                "strategy": s.get("strategy"),
                "source": "bot_cache",
            }
            for s in cached_signals
        ])

    now = time.time()
    if _india_scanner_cache and (now - _india_scanner_cache[0]) < SCANNER_CACHE_TTL_SEC:
        return jsonify(_india_scanner_cache[1])

    with _india_scanner_lock:
        now = time.time()
        if _india_scanner_cache and (now - _india_scanner_cache[0]) < SCANNER_CACHE_TTL_SEC:
            return jsonify(_india_scanner_cache[1])

        india_broker, strategy = get_india_components()
        if not india_broker or not india_broker.is_logged_in or not strategy:
            return jsonify([])

        sma_slow = getattr(strategy.p, "sma_slow", config.INDIA_SMA_SLOW) if hasattr(strategy, "p") else config.INDIA_SMA_SLOW
        sma_fast = getattr(strategy.p, "sma_fast", config.INDIA_SMA_FAST) if hasattr(strategy, "p") else config.INDIA_SMA_FAST
        rsi_period = getattr(strategy.p, "rsi_period", config.INDIA_RSI_PERIOD) if hasattr(strategy, "p") else config.INDIA_RSI_PERIOD

        scanner_results = []
        for symbol in config.INDIA_STOCK_UNIVERSE:
            try:
                df = india_broker.get_historical_bars(symbol)
                if df is not None and not df.empty:
                    df = strategy.compute_indicators(df)
                    signal = strategy.generate_signal(df, symbol)
                    latest = df.iloc[-1]

                    scanner_results.append({
                        "symbol": symbol,
                        "price": round(float(latest["close"]), 2),
                        "sma_200": round(float(latest[f"SMA_{sma_slow}"]), 2) if not pd_isna(latest.get(f"SMA_{sma_slow}")) else None,
                        "sma_20": round(float(latest[f"SMA_{sma_fast}"]), 2) if not pd_isna(latest.get(f"SMA_{sma_fast}")) else None,
                        "rsi": round(float(latest[f"RSI_{rsi_period}"]), 1) if not pd_isna(latest.get(f"RSI_{rsi_period}")) else None,
                        "bbl": round(float(latest["BBL"]), 2) if not pd_isna(latest.get("BBL")) else None,
                        "bbu": round(float(latest["BBU"]), 2) if not pd_isna(latest.get("BBU")) else None,
                        "atr": round(float(latest["ATR"]), 2) if not pd_isna(latest.get("ATR")) else None,
                        "adx": round(float(latest["ADX"]), 1) if not pd_isna(latest.get("ADX")) else None,
                        "signal": signal
                    })
                else:
                    scanner_results.append({
                        "symbol": symbol,
                        "price": 0.0,
                        "signal": "NO_DATA"
                    })
            except Exception as e:
                logger.error(f"Error scanning India {symbol}: {e}")
                scanner_results.append({
                    "symbol": symbol,
                    "price": 0.0,
                    "signal": "ERROR"
                })

        _india_scanner_cache = (time.time(), scanner_results)
        return jsonify(scanner_results)


@app.route("/api/scout")
def get_india_scout():
    """Near Setups panel — close-but-not-confirmed. Scout BUYs run in background loop."""
    if not config.INDIA_ENABLED:
        return jsonify(
            {
                "trade_eligible": False,
                "enabled": False,
                "items": [],
                "message": "India market disabled",
            }
        )

    if not config.INDIA_SCOUT_ENABLED:
        return jsonify(
            {
                "trade_eligible": False,
                "enabled": False,
                "items": [],
                "message": "Scout disabled (INDIA_SCOUT_ENABLED=false)",
            }
        )

    max_age = max(1800.0, float(config.INDIA_SCOUT_INTERVAL_SEC) * 3.0)
    blob = bot_state.get_scout("INDIA", max_age_sec=max_age)
    auto_buy = bool(config.INDIA_SCOUT_AUTO_BUY)
    if not blob:
        return jsonify(
            {
                "trade_eligible": True,
                "auto_buy": auto_buy,
                "enabled": True,
                "items": [],
                "meta": {},
                "updated_at": None,
                "message": (
                    "No near setups yet — waiting for next scout cycle "
                    f"(auto-buy {'ON' if auto_buy else 'OFF'} for full signals)"
                ),
            }
        )

    return jsonify(
        {
            "trade_eligible": True,
            "auto_buy": auto_buy,
            "enabled": True,
            "items": blob["list"],
            "meta": blob.get("meta") or {},
            "updated_at": blob.get("updated_at"),
            "message": (
                "Near setups (close, not confirmed). "
                f"Scout universe auto-buy is {'ON' if auto_buy else 'OFF'} for full signals."
            ),
        }
    )


@app.route("/api/close_position/<symbol>", methods=["POST"])
def close_india_position(symbol):
    india_broker, _ = get_india_components()
    if not india_broker:
        return jsonify({"status": "error", "message": "India broker not available"}), 500

    symbol = symbol.strip().upper()
    positions = india_broker.get_open_positions() or {}
    pos = positions.get(symbol)
    if not pos:
        return jsonify({"status": "error", "message": f"No open India position for {symbol}"}), 404

    qty = int(pos.get("qty") or 0)
    side = str(pos.get("side") or "BUY").upper()
    entry = float(pos.get("avg_entry_price") or 0)
    exit_px = float(pos.get("current_price") or 0)
    if exit_px <= 0:
        quote = india_broker.get_latest_quote(symbol)
        if quote and quote.get("ltp"):
            exit_px = float(quote["ltp"])
        elif entry > 0:
            exit_px = entry
            logger.warning(
                f"[CLOSE] {symbol}: no LTP — journaling exit at entry {entry:.2f}"
            )
    unreal = float(pos.get("unrealized_pl") or ((exit_px - entry) * qty))

    fill = india_broker.close_position(symbol)
    if fill is None:
        return jsonify({"status": "error", "message": f"Failed to close India position for {symbol}"}), 400
    if float(fill) > 0:
        exit_px = float(fill)

    # Journal the exit so Recent Trades / performance P&L update
    journal_row = trade_journal.record_exit(
        "INDIA", symbol, exit_px, reason="manual_close", qty=qty
    )
    if journal_row is None and entry > 0 and qty > 0:
        # Position existed in paper but was never journaled on entry
        trade_journal.record_entry(
            "INDIA",
            symbol,
            qty,
            entry,
            side=side,
            reason="manual_close_backfill",
            strategy=config.STRATEGY_NAME,
        )
        journal_row = trade_journal.record_exit(
            "INDIA", symbol, exit_px, reason="manual_close", qty=qty
        )

    india_risk = get_india_risk()
    if india_risk:
        india_risk.clear_trade(symbol)
        if hasattr(india_risk, "release_margin"):
            india_risk.release_margin(symbol)

    acct = india_broker.get_account_info() or {}
    equity = float(acct.get("equity") or 0)
    daily_pl, _, _ = _broker_style_day_pl("INDIA", india_broker, equity)
    pnl = float(journal_row["pnl"]) if journal_row else unreal
    pnl_pct = float(journal_row["pnl_pct"]) if journal_row else (
        ((exit_px - entry) / entry) if entry else 0.0
    )

    return jsonify({
        "status": "success",
        "message": f"Closed {symbol} | P&L: ₹{pnl:+,.2f} ({pnl_pct:+.2%})",
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry,
        "exit_price": exit_px,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct * 100, 2),
        "equity": equity,
        "daily_pl": round(daily_pl, 2),
        "available_cash": acct.get("available_cash"),
    })


@app.route("/api/toggle_kill_switch", methods=["POST"])
def toggle_india_kill_switch():
    india_risk = get_india_risk()
    if not india_risk:
        return jsonify({"status": "error", "message": "Risk manager not available"}), 500

    if india_risk.is_kill_switch_active:
        india_risk.reset_kill_switch()
        alerts.kill_switch_alert("INDIA", False)
        state = "reset"
    else:
        india_risk.activate_kill_switch("dashboard")
        alerts.kill_switch_alert("INDIA", True)
        state = "activated"

    return jsonify({"status": "success", "message": f"India kill switch {state}",
                    "kill_switch_active": india_risk.is_kill_switch_active})


# ===========================================================================
# US API Endpoints
# ===========================================================================
@app.route("/api/us/status")
def get_us_status():
    """Get US market account status."""
    if not config.US_ENABLED:
        return jsonify({
            "status": "disabled",
            "message": "US trading disabled. Set DHAN_* and US_PAPER=true or US_LIVE_TRADING=true."
        })

    us_broker, _ = get_us_components()
    if not us_broker or not us_broker.is_logged_in:
        err_msg = (
            us_broker.last_error
            if (us_broker and us_broker.last_error)
            else "Dhan Global authentication failed."
        )
        return jsonify({
            "status": "error",
            "message": err_msg
        })

    account_info = us_broker.get_account_info()
    if not account_info:
        err_msg = (
            us_broker.last_error
            if us_broker.last_error
            else "Unable to fetch Dhan Global account info."
        )
        return jsonify({
            "status": "error",
            "message": err_msg
        })

    equity = account_info["equity"]
    cash = account_info["available_cash"]

    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if not _us_equity_history or _us_equity_history[-1]["timestamp"] != now_str:
        _us_equity_history.append({"timestamp": now_str, "equity": round(equity, 2)})
        if len(_us_equity_history) > 60:
            _us_equity_history.pop(0)

    us_market_open = us_broker.is_market_open()
    us_risk = get_us_risk()
    last_eq = float(bot_state.us_sod_equity(equity))
    equity_day_pl = equity - last_eq
    daily_pl, daily_pl_pct, day_pl_detail = _broker_style_day_pl(
        "US", us_broker, equity
    )

    us_feed_summary = {}
    try:
        from dhan_us_live_feed import get_us_live_feed_manager

        us_feed_summary = get_us_live_feed_manager().status_summary()
    except Exception:
        us_feed_summary = {"enabled": False, "connected": False, "mode": "unavailable"}

    return jsonify({
        "status": "success",
        "market": "US",
        "currency": "USD",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "equity": equity,
        "last_equity": last_eq,
        "daily_pl": round(daily_pl, 2),
        "daily_pl_pct": round(daily_pl_pct, 2),
        "equity_day_pl": round(equity_day_pl, 2),
        "day_pl_detail": day_pl_detail,
        "available_cash": cash,
        "buying_power": account_info.get("buying_power", cash),
        "used_margin": account_info.get("used_margin", 0),
        "market_open": us_market_open,
        "logged_in": us_broker.is_logged_in,
        "broker": "dhan_global",
        "paper_trading": config.US_PAPER,
        "live_armed": config.US_LIVE_CONFIRMED,
        "capital_cap": float(getattr(config, "US_CAPITAL_CAP", 0) or 0),
        "global_activated": us_broker.global_stocks_available,
        "live_feed": us_feed_summary,
        "strategy": config.STRATEGY_NAME,
        "kill_switch_active": us_risk.is_kill_switch_active,
        "equity_history": _us_equity_history,
        "performance": trade_journal.performance_stats("US"),
        "open_risk_pct": round(
            us_risk.open_risk_pct(equity, us_broker.get_open_positions()),
            4,
        ),
    })


@app.route("/api/us/positions")
def get_us_positions():
    if not config.US_ENABLED:
        return jsonify([])

    us_broker, _ = get_us_components()
    if not us_broker or not us_broker.is_logged_in:
        return jsonify([])

    positions_dict = us_broker.get_open_positions()
    positions_list = []

    risk_mgr = get_us_risk()

    for symbol, pos in positions_dict.items():
        entry_price = pos["avg_entry_price"]
        atr = pos.get("atr")
        sl_price = pos.get("stop_loss")
        tp_price = pos.get("take_profit")
        if sl_price is None:
            sl_price = risk_mgr.get_stop_loss_price(entry_price, atr)
        if tp_price is None:
            tp_price = risk_mgr.get_take_profit_price(
                entry_price, stop_loss_price=sl_price, atr=atr
            )

        positions_list.append({
            "symbol": symbol,
            "qty": pos["qty"],
            "avg_entry_price": entry_price,
            "current_price": pos["current_price"],
            "market_value": pos["market_value"],
            "unrealized_pl": pos["unrealized_pl"],
            "unrealized_plpc": round(pos["unrealized_plpc"] * 100, 2),
            "stop_loss": sl_price,
            "take_profit": tp_price
        })

    return jsonify(positions_list)


@app.route("/api/us/scanner")
def get_us_scanner():
    global _us_scanner_cache

    if not config.US_ENABLED:
        return jsonify([])

    cached_signals = bot_state.get_signals("US", max_age_sec=max(600, config.US_LOOP_INTERVAL_SEC * 3))
    if cached_signals:
        return jsonify([
            {
                "symbol": s["symbol"],
                "price": s.get("price") or 0.0,
                "rsi": s.get("rsi"),
                "adx": s.get("adx"),
                "signal": s.get("signal", "HOLD"),
                "reason": s.get("reason", ""),
                "strategy": s.get("strategy"),
                "source": "bot_cache",
            }
            for s in cached_signals
        ])

    now = time.time()
    if _us_scanner_cache and (now - _us_scanner_cache[0]) < SCANNER_CACHE_TTL_SEC:
        return jsonify(_us_scanner_cache[1])

    with _us_scanner_lock:
        now = time.time()
        if _us_scanner_cache and (now - _us_scanner_cache[0]) < SCANNER_CACHE_TTL_SEC:
            return jsonify(_us_scanner_cache[1])

        us_broker, strategy = get_us_components()
        if not us_broker or not us_broker.is_logged_in or not strategy:
            return jsonify([])

        sma_slow = getattr(strategy.p, "sma_slow", config.US_SMA_SLOW) if hasattr(strategy, "p") else config.US_SMA_SLOW
        sma_fast = getattr(strategy.p, "sma_fast", config.US_SMA_FAST) if hasattr(strategy, "p") else config.US_SMA_FAST
        rsi_period = getattr(strategy.p, "rsi_period", config.US_RSI_PERIOD) if hasattr(strategy, "p") else config.US_RSI_PERIOD

        scanner_results = []
        for symbol in config.US_STOCK_UNIVERSE:
            try:
                df = us_broker.get_historical_bars(symbol)
                if df is not None and not df.empty:
                    df = strategy.compute_indicators(df)
                    signal = strategy.generate_signal(df, symbol)
                    latest = df.iloc[-1]

                    scanner_results.append({
                        "symbol": symbol,
                        "price": round(float(latest["close"]), 2),
                        "sma_200": round(float(latest[f"SMA_{sma_slow}"]), 2) if not pd_isna(latest.get(f"SMA_{sma_slow}")) else None,
                        "sma_20": round(float(latest[f"SMA_{sma_fast}"]), 2) if not pd_isna(latest.get(f"SMA_{sma_fast}")) else None,
                        "rsi": round(float(latest[f"RSI_{rsi_period}"]), 1) if not pd_isna(latest.get(f"RSI_{rsi_period}")) else None,
                        "bbl": round(float(latest["BBL"]), 2) if not pd_isna(latest.get("BBL")) else None,
                        "bbu": round(float(latest["BBU"]), 2) if not pd_isna(latest.get("BBU")) else None,
                        "atr": round(float(latest["ATR"]), 2) if not pd_isna(latest.get("ATR")) else None,
                        "adx": round(float(latest["ADX"]), 1) if not pd_isna(latest.get("ADX")) else None,
                        "signal": signal
                    })
                else:
                    scanner_results.append({
                        "symbol": symbol,
                        "price": 0.0,
                        "signal": "NO_DATA"
                    })
            except Exception as e:
                logger.error(f"Error scanning US {symbol}: {e}")
                scanner_results.append({
                    "symbol": symbol,
                    "price": 0.0,
                    "signal": "ERROR"
                })

        _us_scanner_cache = (time.time(), scanner_results)
        return jsonify(scanner_results)


@app.route("/api/us/buy", methods=["POST"])
def buy_us_stock():
    """Manual buy trigger for US stock."""
    if not config.US_ENABLED:
        return jsonify({"status": "error", "message": "US trading disabled"}), 400

    us_broker, strategy = get_us_components()
    if not us_broker:
        return jsonify({"status": "error", "message": "US broker unavailable"}), 500

    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").strip().upper()
    qty_in = data.get("qty")

    if not symbol:
        return jsonify({"status": "error", "message": "Missing symbol"}), 400

    if symbol not in config.US_STOCK_UNIVERSE:
        return jsonify({"status": "error", "message": f"{symbol} not in US universe"}), 400

    us_risk = get_us_risk()
    if us_risk.is_kill_switch_active:
        return jsonify({"status": "error", "message": "US kill switch is ACTIVE — buy blocked"}), 400

    account = us_broker.get_account_info()
    if not account:
        return jsonify({"status": "error", "message": "Cannot fetch US account info"}), 500

    quote = us_broker.get_latest_quote(symbol)
    price = float(quote["ltp"]) if (quote and quote.get("ltp")) else 0.0
    if price <= 0:
        return jsonify({"status": "error", "message": f"No valid price quote for {symbol}"}), 400

    df = us_broker.get_historical_bars(symbol)
    atr = None
    if df is not None and not df.empty and strategy:
        df = strategy.compute_indicators(df)
        atr = strategy.latest_atr(df)

    sl = us_risk.get_stop_loss_price(price, atr)
    tp = us_risk.get_take_profit_price(price, stop_loss_price=sl, atr=atr)

    if qty_in is not None:
        try:
            qty = int(qty_in)
        except ValueError:
            return jsonify({"status": "error", "message": "Invalid qty"}), 400
    else:
        stop_dist = price - sl
        qty = us_risk.calculate_position_size(
            account["equity"],
            price,
            stop_distance=stop_dist,
            available_cash=float(account.get("available_cash") or account["equity"]),
        )

    if qty <= 0:
        return jsonify({"status": "error", "message": "Position sizing calculated 0 shares"}), 400

    order_id = us_broker.place_buy_order(
        symbol=symbol,
        qty=qty,
        limit_price=price,
        stop_loss_price=sl,
        take_profit_price=tp,
        atr=atr,
    )

    if order_id:
        us_risk.register_trade(symbol, price, sl, atr)
        trade_journal.record_entry(
            "US", symbol, qty, price,
            stop_price=sl, take_profit=tp,
            reason="manual_dashboard_buy",
            strategy=strategy.name if strategy else "manual",
            meta={"atr": atr, "order_id": order_id},
        )
        alerts.trade_alert("US", "BUY", symbol, f"qty={qty} @${price:.2f} (manual)")
        return jsonify({
            "status": "success",
            "message": f"BUY order placed for {qty} shares of {symbol} @ ${price:.2f} (ID: {order_id})",
            "order_id": order_id
        })
    else:
        return jsonify({
            "status": "error",
            "message": us_broker.last_error or f"Failed to place BUY order for {symbol}"
        }), 400


@app.route("/api/us/close_position/<symbol>", methods=["POST"])
def close_us_position(symbol):
    us_broker, _ = get_us_components()
    if not us_broker:
        return jsonify({"status": "error", "message": "US broker not available"}), 500

    symbol = symbol.strip().upper()
    positions = us_broker.get_open_positions() or {}
    pos = positions.get(symbol)
    if not pos:
        return jsonify({"status": "error", "message": f"No open US position for {symbol}"}), 404

    qty = int(pos.get("qty") or 0)
    entry = float(pos.get("avg_entry_price") or 0)
    exit_px = float(pos.get("current_price") or entry)
    unreal = float(pos.get("unrealized_pl") or ((exit_px - entry) * qty))

    success = us_broker.close_position(symbol)
    if not success:
        return jsonify({"status": "error", "message": f"Failed to close US position for {symbol}"}), 400

    journal_row = trade_journal.record_exit(
        "US", symbol, exit_px, reason="manual_close", qty=qty
    )
    if journal_row is None and entry > 0 and qty > 0:
        trade_journal.record_entry(
            "US",
            symbol,
            qty,
            entry,
            reason="manual_close_backfill",
            strategy=config.STRATEGY_NAME,
        )
        journal_row = trade_journal.record_exit(
            "US", symbol, exit_px, reason="manual_close", qty=qty
        )

    us_risk = get_us_risk()
    if us_risk:
        us_risk.clear_trade(symbol)
        if hasattr(us_risk, "release_margin"):
            us_risk.release_margin(symbol)

    acct = us_broker.get_account_info() or {}
    equity = float(acct.get("equity") or 0)
    daily_pl, _, _ = _broker_style_day_pl("US", us_broker, equity)
    pnl = float(journal_row["pnl"]) if journal_row else unreal
    pnl_pct = float(journal_row["pnl_pct"]) if journal_row else (
        ((exit_px - entry) / entry) if entry else 0.0
    )

    return jsonify({
        "status": "success",
        "message": f"Closed {symbol} | P&L: ${pnl:+,.2f} ({pnl_pct:+.2%})",
        "symbol": symbol,
        "qty": qty,
        "entry_price": entry,
        "exit_price": exit_px,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct * 100, 2),
        "equity": equity,
        "daily_pl": round(daily_pl, 2),
        "available_cash": acct.get("available_cash"),
    })


@app.route("/api/us/toggle_kill_switch", methods=["POST"])
def toggle_us_kill_switch():
    us_risk = get_us_risk()
    if not us_risk:
        return jsonify({"status": "error", "message": "Risk manager not available"}), 500

    if us_risk.is_kill_switch_active:
        us_risk.reset_kill_switch()
        alerts.kill_switch_alert("US", False)
        state = "reset"
    else:
        us_risk.activate_kill_switch("dashboard")
        alerts.kill_switch_alert("US", True)
        state = "activated"

    return jsonify({"status": "success", "message": f"US kill switch {state}"})


# ===========================================================================
# General API Endpoints
# ===========================================================================
@app.route("/api/performance")
def get_performance():
    """Trade journal performance metrics (win rate, PF, max DD, open risk)."""
    market = request.args.get("market", "INDIA").upper()
    stats = trade_journal.performance_stats(market)
    curve = trade_journal.equity_curve(market, limit=200)
    trades = trade_journal.recent_trades(limit=30, market=market)

    open_risk = 0.0
    if market == "US":
        us_broker, _ = get_us_components()
        if us_broker and us_broker.is_logged_in:
            acct = us_broker.get_account_info()
            if acct:
                open_risk = get_us_risk().open_risk_pct(
                    acct["equity"], us_broker.get_open_positions()
                )
    else:
        india_broker, _ = get_india_components()
        if india_broker and india_broker.is_logged_in:
            acct = india_broker.get_account_info()
            if acct:
                open_risk = get_india_risk().open_risk_pct(
                    acct["equity"], india_broker.get_open_positions() or {}
                )

    return jsonify({
        "status": "success",
        "market": market,
        "strategy": config.STRATEGY_NAME,
        "stats": stats,
        "equity_curve": curve,
        "recent_trades": trades,
        "open_risk_pct": round(open_risk, 4),
    })


@app.route("/api/trades")
def get_trades():
    market = request.args.get("market", "INDIA").upper()
    limit = int(request.args.get("limit", "50"))
    rows = trade_journal.recent_trades(limit=limit, market=market)

    # Journal historically stored side='BUY' for all entries.
    # For India + Dhan, prefer broker order-side so Recent Trades mirrors Dhan.
    if market == "INDIA":
        try:
            india_broker, _ = get_india_components()
            get_side = getattr(india_broker, "get_order_transaction_side", None)
            if callable(get_side):
                side_cache: dict[str, str] = {}
                for r in rows:
                    meta = {}
                    raw_meta = r.get("meta_json")
                    if raw_meta:
                        try:
                            meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
                        except Exception:
                            meta = {}
                    oid = str(meta.get("order_id") or "").strip()
                    if not oid:
                        continue
                    side = side_cache.get(oid)
                    if side is None:
                        side = str(get_side(oid) or "").upper()
                        side_cache[oid] = side
                    if side in ("BUY", "SELL"):
                        r["side"] = side
        except Exception as e:
            logger.debug(f"trade-side enrichment skipped: {e}")

    # Exits are represented by closed trades. Add explicit side fields so UI can
    # show SELL/BUY at exit time (instead of only the entry side).
    for r in rows:
        entry_side = str(r.get("side") or "BUY").upper()
        status = str(r.get("status") or "").lower()
        if entry_side not in ("BUY", "SELL"):
            entry_side = "BUY"
        exit_side = "BUY" if entry_side == "SELL" else "SELL"
        r["entry_side"] = entry_side
        r["exit_side"] = exit_side if status == "closed" else None
        r["display_side"] = r["exit_side"] or entry_side

    return jsonify(rows)


def pd_isna(val):
    import pandas as pd
    return pd.isna(val)


@app.route("/api/logs")
def get_logs():
    log_file = os.path.join("logs", "trading_bot.log")
    if not os.path.exists(log_file):
        return jsonify({"logs": ["Log file not created yet. Run the main bot to generate logs."]})

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return jsonify({"logs": [line.strip() for line in lines[-50:]]})
    except Exception as e:
        return jsonify({"logs": [f"Error reading log file: {e}"]})


@app.route("/api/health")
def get_health():
    h = bot_state.get_health()
    india_risk = get_india_risk()
    us_risk = get_us_risk()
    h["india_kill_switch"] = bool(india_risk.is_kill_switch_active) if india_risk else False
    h["us_kill_switch"] = bool(us_risk.is_kill_switch_active) if us_risk else False
    h["tide_bearish"] = bot_state.is_tide_bearish()
    h["tide"] = bot_state.get_tide_state()
    h["india_enabled"] = config.INDIA_ENABLED
    h["us_enabled"] = config.US_ENABLED
    return jsonify(h)


@app.route("/api/equity_curves")
def get_equity_curves():
    return jsonify({
        "india": trade_journal.equity_curve("INDIA", limit=120),
        "us": trade_journal.equity_curve("US", limit=120),
    })


def run_dashboard_server(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", 8080))
    logger.info(f"Admin Dashboard running on port {port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


def start_dashboard_in_background(port=None):
    if port is None:
        port = int(os.environ.get("PORT", 8080))
    t = threading.Thread(target=run_dashboard_server, kwargs={"port": port}, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_dashboard_server()

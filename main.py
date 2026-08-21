"""India-only MIS intraday bot orchestrator and dashboard launcher."""

import logging
import json
import os
import sys
import time
import threading
from datetime import datetime

import config
from strategy import create_strategy, RelativeStrengthFilter
from risk_manager import RiskManager
from dashboard_server import start_dashboard_in_background
import trade_journal
import bot_state
import alerts
from filters import (
    apply_tide_lock_to_signal,
    fetch_daily_bars,
    market_tide_filter,
    mtf_allows,
    regime_allows,
)
from strategy import snapshot_signal
import order_guards

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except ImportError:
    import pytz  # type: ignore
    IST = pytz.timezone("Asia/Kolkata")


def setup_logging():
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_format = "%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    root_logger.addHandler(console_handler)

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "trading_bot.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
    root_logger.addHandler(file_handler)
    logging.getLogger("main").info(
        "Logging to logs/trading_bot.log (RotatingFileHandler 50MB x 5 backups)"
    )


logger = logging.getLogger("main")


def log_india_portfolio_summary(india_broker, market_label: str = "INDIA"):
    positions = india_broker.get_open_positions()
    if not positions:
        logger.info(f"[{market_label} PORTFOLIO] No open positions.")
        return

    logger.info(f"[{market_label} PORTFOLIO] Portfolio Summary:")
    header = f"   {'Symbol':<12} {'Qty':>6} {'Entry':>10} {'Current':>10} {'P&L':>12} {'P&L (%)':>8}"
    logger.info(header)
    logger.info("   " + "-" * 64)

    total_pl = 0.0
    for symbol, pos in positions.items():
        total_pl += pos["unrealized_pl"]
        logger.info(
            f"   {symbol:<12} {pos['qty']:>6} "
            f"Rs{pos['avg_entry_price']:>8.2f} "
            f"Rs{pos['current_price']:>8.2f} "
            f"Rs{pos['unrealized_pl']:>10.2f} "
            f"{pos['unrealized_plpc']:>7.2%}"
        )

    logger.info("   " + "-" * 64)
    logger.info(f"   Total Unrealized P&L: Rs {total_pl:,.2f}")



def _refresh_rs_filter(rs_filter, symbol_dfs: dict):
    if rs_filter is not None and config.USE_RELATIVE_STRENGTH:
        rs_filter.update_scores(symbol_dfs)


def _refresh_strategy_universe(strategy, symbol_dfs: dict):
    """Push the cycle bar_cache into mis_regime Playbook 1 RS ranking. No extra order path."""
    updater = getattr(strategy, "update_universe", None)
    if callable(updater):
        updater(symbol_dfs)


def _india_mis_day_pl(india_broker, equity: float) -> float:
    """Journal-style day P&L for MIS kill-switch (not raw Dhan equity delta)."""
    from dashboard_server import _broker_style_day_pl

    day_pl, _, _ = _broker_style_day_pl("INDIA", india_broker, float(equity or 0))
    return float(day_pl)


def _india_try_buy(
    *,
    india_broker,
    risk_mgr,
    strategy,
    symbol: str,
    snap: dict,
    atr,
    current_equity: float,
    account: dict,
    current_positions: dict,
    tradable_window: bool,
    regime_ok: bool,
    reason: str = "signal_buy",
    source: str = "core",
) -> bool:
    """
    Shared India equity BUY path (core + scout). Honors session/regime/risk caps.
    Returns True only after a confirmed TRADED/PART_TRADED fill.
    """
    source = "scout" if str(source).lower() == "scout" or str(reason).startswith("scout") else "core"

    if not tradable_window:
        logger.info(f"{symbol}: BUY skipped — outside tradable session window")
        return False
    if not regime_ok:
        logger.info(f"{symbol}: BUY skipped — market SMA filter blocked entries")
        return False
    if risk_mgr.is_kill_switch_active:
        logger.info(f"{symbol}: BUY skipped — kill switch active")
        return False
    if bot_state.is_tide_bearish():
        logger.info(f"{symbol}: BUY skipped — TIDE LOCK")
        return False
    if order_guards.is_exit_pending_stuck(symbol) or order_guards.is_exit_inflight(symbol):
        logger.info(f"{symbol}: BUY skipped — EXIT_PENDING_STUCK / exit in flight")
        return False

    continuation = bool((snap or {}).get("orb_continuation"))

    # One entry per symbol per IST day (journal) — survives restarts / core+scout.
    # ORB continuation is the only exception (volume-confirmed 0.5% extension).
    try:
        if trade_journal.has_entry_today("INDIA", symbol) and not continuation:
            logger.info(
                f"{symbol}: BUY skipped — journal already has an entry today"
            )
            if hasattr(strategy, "mark_day_fired"):
                try:
                    strategy.mark_day_fired(symbol, "BUY")
                except Exception:
                    pass
            return False
    except Exception as e:
        logger.debug(f"{symbol}: has_entry_today check skip: {e}")

    # ORB day-fired shared lock (reload from disk)
    if hasattr(strategy, "has_day_fired"):
        try:
            if strategy.has_day_fired(symbol, "BUY") and not continuation:
                logger.info(f"{symbol}: BUY skipped — ORB already fired today")
                return False
        except Exception as e:
            logger.debug(f"{symbol}: has_day_fired check skip: {e}")

    ok_n, why_n = risk_mgr.check_max_trades_per_day()
    if not ok_n:
        logger.info(f"{symbol}: BUY skipped — {why_n}")
        return False

    blocked, block_reason = order_guards.is_buy_blocked(symbol)
    if blocked:
        logger.info(f"{symbol}: BUY skipped — {block_reason}")
        return False

    quote = india_broker.get_latest_quote(symbol)
    limit_price = None
    if quote and quote.get("ltp"):
        limit_price = float(quote["ltp"])
    elif snap.get("price"):
        limit_price = float(snap["price"])
        logger.warning(
            f"{symbol}: no live LTP — using signal candle close "
            f"{limit_price:.2f} for entry"
        )
    if limit_price is None or limit_price <= 0:
        logger.warning(f"{symbol}: BUY skipped — no usable price")
        return False

    try:
        from price_guards import india_buy_locked

        locked, lock_why = india_buy_locked(symbol, quote if isinstance(quote, dict) else None)
        if locked:
            logger.info(f"{symbol}: BUY skipped — {lock_why}")
            return False
    except Exception as ce:
        logger.debug(f"{symbol}: circuit/ask guard skip: {ce}")

    sizing_cash = float(account.get("available_cash") or current_equity)
    sl = risk_mgr.get_stop_loss_price(limit_price, atr)
    tp = risk_mgr.get_take_profit_price(limit_price, stop_loss_price=sl, atr=atr)
    ok_cf, why_cf = risk_mgr.passes_cost_floor(limit_price, tp)
    if not ok_cf:
        logger.info(f"{symbol}: BUY skipped — {why_cf}")
        return False
    stop_dist = limit_price - sl
    qty = risk_mgr.calculate_position_size(
        current_equity,
        limit_price,
        stop_distance=stop_dist,
        symbol=symbol,
        available_cash=sizing_cash,
    )
    if qty <= 0:
        logger.warning(
            f"{symbol}: BUY skipped — sized to 0 shares "
            f"(price={limit_price:.2f}, equity={current_equity:.0f})"
        )
        return False

    new_risk = max(stop_dist, 0.0) * qty
    sizing_equity = risk_mgr.effective_equity(min(current_equity, sizing_cash))

    # Cluster guard before max-open / already-held CAP
    cluster_fn = getattr(risk_mgr, "cluster_open_count", None)
    if callable(cluster_fn):
        pair = cluster_fn(symbol, current_positions)
        if isinstance(pair, (tuple, list)) and len(pair) == 2:
            cluster, cluster_n = pair
            cap_n = int(getattr(risk_mgr, "max_cluster_positions", 2) or 2)
            if cluster and int(cluster_n or 0) >= cap_n:
                logger.info(
                    f"[CLUSTER BLOCK] {symbol}: sector {cluster} already has {cluster_n} positions"
                )
                return False

    product = str(getattr(config, "INDIA_PRODUCT_TYPE", "INTRADAY") or "INTRADAY")
    margin_required = 0.0
    if hasattr(india_broker, "get_margin_required"):
        try:
            margin_required = float(
                india_broker.get_margin_required(symbol, qty, limit_price, product) or 0
            )
        except Exception:
            margin_required = 0.0
    if margin_required <= 0:
        margin_required = (qty * limit_price) / 5.0
    if hasattr(risk_mgr, "free_cash_for_entry"):
        free = float(
            risk_mgr.free_cash_for_entry(
                sizing_cash,
                current_positions,
                broker_used_margin=account.get("used_margin"),
            )
        )
    else:
        used = 0.0
        if hasattr(risk_mgr, "total_margin_used"):
            try:
                used = float(risk_mgr.total_margin_used() or 0)
            except Exception:
                used = 0.0
        free = sizing_cash - used
    if margin_required > free:
        logger.info(
            f"[MARGIN REJECTED] {symbol}: needs {margin_required}, free {free}"
        )
        return False

    # Atomic Core/Scout gate: position limits + portfolio risk + symbol reservation
    with risk_mgr.entry_gate():
        if not risk_mgr.is_position_allowed(symbol, current_positions):
            return False
        ok_risk, risk_why = risk_mgr.can_add_trade_risk(
            sizing_equity, new_risk, current_positions
        )
        if not ok_risk:
            logger.info(f"{symbol}: BUY skipped — {risk_why}")
            return False
        reserved, res_why = order_guards.try_reserve_buy(symbol, owner=source)
        if not reserved:
            logger.info(f"{symbol}: BUY skipped — {res_why}")
            return False
        risk_mgr.set_pending_risk(symbol, new_risk)

    order_id = None
    try:
        order_id = india_broker.place_buy_order(
            symbol=symbol,
            qty=qty,
            limit_price=limit_price,
            stop_loss_price=sl,
            take_profit_price=tp,
            atr=atr,
            available_cash=free,
            margin_used=0.0,
        )
        if not order_id:
            logger.warning(
                f"{symbol}: BUY order failed — "
                f"{getattr(india_broker, 'last_error', 'unknown')}"
            )
            err = str(getattr(india_broker, "last_error", "") or "")
            if "reject" in err.lower() or "16283" in err or "tick" in err.lower():
                order_guards.block_buy_after_reject(symbol, err[:160])
            return False

        # place_buy_order only returns id after TRADED/PART_TRADED confirmation.
        fill_status = str(
            getattr(india_broker, "last_order_status", "") or ""
        ).upper()
        fill_qty = int(getattr(india_broker, "last_fill_qty", 0) or 0)
        if fill_qty <= 0 and hasattr(india_broker, "get_order_filled_qty"):
            try:
                fill_qty = int(india_broker.get_order_filled_qty(order_id) or 0)
            except Exception:
                fill_qty = 0
        # PART_TRADED: never substitute intended size — reconcile from orderbook.
        if fill_status == "PART_TRADED" or (0 < fill_qty < int(qty)):
            rec = india_broker.reconcile_partial_fill(
                order_id,
                risk_mgr=risk_mgr,
                strategy=strategy,
                intended_qty=qty,
                planned_entry=limit_price,
                planned_sl=sl,
                planned_tp=tp,
                atr=atr,
                source=source,
                reason=reason,
                symbol=symbol,
                continuation=continuation,
            )
            if not rec or not rec.get("ok"):
                logger.warning(
                    f"{symbol}: PART_TRADED reconcile failed — "
                    f"{(rec or {}).get('reason') or getattr(india_broker, 'last_error', '')}"
                )
                return False
            fill_qty = int(rec.get("filled_qty") or 0)
            entry_px = float(rec.get("average_price") or 0)
            sl = float(rec.get("stop_loss_price") or sl)
            tp = float(rec.get("target_price") or tp)
            alerts.trade_alert(
                "INDIA",
                "BUY",
                symbol,
                f"qty={fill_qty}/{qty} @{entry_px} PART_TRADED src={source}",
            )
            current_positions[symbol] = {
                "qty": fill_qty,
                "avg_entry_price": entry_px,
            }
            booked = margin_required * (fill_qty / float(qty)) if qty else margin_required
            risk_mgr.add_margin(symbol, booked)
            return True

        if fill_qty <= 0:
            fill_qty = int(qty)
        entry_px = float(getattr(india_broker, "last_fill_price", 0) or 0)
        if entry_px <= 0 and hasattr(india_broker, "resolve_entry_fill_price"):
            try:
                entry_px = float(
                    india_broker.resolve_entry_fill_price(
                        symbol, order_id, fallback=limit_price
                    )
                    or 0
                )
            except Exception:
                entry_px = 0.0
        if entry_px <= 0:
            entry_px = float(limit_price)

        # Recompute SL/TP from actual fill (risk distance preserved via ATR when possible)
        sl = risk_mgr.get_stop_loss_price(entry_px, atr)
        tp = risk_mgr.get_take_profit_price(entry_px, stop_loss_price=sl, atr=atr)

        if hasattr(strategy, "mark_day_fired"):
            try:
                strategy.mark_day_fired(symbol, "BUY", continuation=continuation)
            except Exception as me:
                logger.debug(f"{symbol}: mark_day_fired skipped: {me}")

        risk_mgr.register_trade(
            symbol,
            entry_px,
            sl,
            atr,
            qty=fill_qty,
            take_profit=tp,
            source=source,
            strategy=getattr(strategy, "name", ""),
            sl_order_id=getattr(india_broker, "last_sl_order_id", None),
            super_order_id=getattr(india_broker, "last_super_order_id", None),
            order_id=order_id,
        )
        if hasattr(india_broker, "_set_sl_tp_status"):
            india_broker._set_sl_tp_status(
                symbol,
                "ACTIVE",
                order_id=order_id,
                qty=fill_qty,
                intended_qty=qty,
                entry=entry_px,
                stop_loss_price=sl,
                target_price=tp,
            )
        trade_journal.record_entry(
            "INDIA",
            symbol,
            fill_qty,
            entry_px,
            stop_price=sl,
            take_profit=tp,
            reason=reason,
            strategy=strategy.name,
            meta={
                "atr": atr,
                "order_id": order_id,
                "limit_price": limit_price,
                "source": source,
                "entry_reason": reason,
                "intended_qty": qty,
                "fill_status": getattr(india_broker, "last_order_status", ""),
            },
        )
        alerts.trade_alert(
            "INDIA", "BUY", symbol, f"qty={fill_qty} @{entry_px} src={source}"
        )
        current_positions[symbol] = {
            "qty": fill_qty,
            "avg_entry_price": entry_px,
        }
        booked = margin_required * (fill_qty / float(qty)) if qty else margin_required
        risk_mgr.add_margin(symbol, booked)
        return True
    finally:
        risk_mgr.clear_pending_risk(symbol)
        order_guards.release_buy_reservation(symbol)


def _india_restore_runtime_state(india_broker, risk_mgr, strategy) -> None:
    """Load persisted meta / ORB locks / kill latch and reconcile broker positions."""
    try:
        bot_state.ensure_kill_loaded()
        if bot_state.is_kill_switch_active("INDIA"):
            logger.critical(
                f"[INDIA] Kill switch restored from disk: "
                f"{bot_state.kill_switch_reason('INDIA')} — no new entries today"
            )
    except Exception as e:
        logger.debug(f"kill load: {e}")
    try:
        risk_mgr.load_state()
    except Exception as e:
        logger.debug(f"risk state load: {e}")
    if hasattr(india_broker, "load_sl_tp_meta"):
        try:
            india_broker.load_sl_tp_meta()
        except Exception as e:
            logger.debug(f"sl_tp_meta load: {e}")
    if strategy is not None and hasattr(strategy, "load_fired_state"):
        try:
            strategy.load_fired_state()
        except Exception as e:
            logger.debug(f"ORB fired load: {e}")
    # Also mark ORB fired for any journal entries already taken today
    try:
        if strategy is not None and hasattr(strategy, "mark_day_fired"):
            seeded: dict[str, int] = {}
            for row in trade_journal.entries_today("INDIA"):
                sym = str(row.get("symbol") or "")
                if not sym:
                    continue
                seeded[sym] = int(seeded.get(sym) or 0) + 1
                strategy.mark_day_fired(sym, "BUY", continuation=seeded[sym] > 1)
    except Exception as e:
        logger.debug(f"ORB seed from journal: {e}")
    try:
        positions = india_broker.get_open_positions()
    except Exception as e:
        logger.warning(f"[INDIA] startup positions fetch failed: {e}")
        return
    if positions is None:
        logger.warning(
            "[INDIA] startup reconcile skipped — positions unavailable "
            "(will not invent opens from journal)"
        )
        return
    risk_mgr.reconcile_meta_with_broker(positions)
    # Journal opens not on broker → broker_flat; never create broker positions from journal
    _reconcile_india_journal(india_broker, risk_mgr, positions)
    # Enrich meta from open journal rows when broker has the symbol
    try:
        for row in trade_journal.list_open_trades("INDIA"):
            sym = str(row.get("symbol") or "")
            if not sym or sym not in positions:
                continue
            meta = risk_mgr._trade_meta.get(sym) or {}
            try:
                jmeta = json.loads(row.get("meta_json") or "{}")
            except Exception:
                jmeta = {}
            if not meta.get("source") and jmeta.get("source"):
                meta["source"] = jmeta.get("source")
            if row.get("stop_price") and not meta.get("initial_stop"):
                meta["initial_stop"] = float(row["stop_price"])
                meta["stop"] = float(row["stop_price"])
            if row.get("take_profit"):
                meta["take_profit"] = float(row["take_profit"])
            meta["qty"] = int(positions[sym].get("qty") or row.get("qty") or 0)
            meta["entry"] = float(
                positions[sym].get("avg_entry_price") or row.get("entry_price") or 0
            )
            risk_mgr._trade_meta[sym] = meta
        risk_mgr.persist_state()
    except Exception as e:
        logger.debug(f"journal enrich meta skip: {e}")
    logger.info(
        f"[INDIA] Startup reconcile done | broker_open={list(positions.keys())} "
        f"| meta={list(risk_mgr._trade_meta.keys())}"
    )
    _rebuild_india_margin_book(india_broker, risk_mgr, positions)
    if hasattr(india_broker, "rescue_zombie_positions"):
        try:
            rescued = india_broker.rescue_zombie_positions(risk_mgr, strategy)
            if rescued:
                logger.warning(f"[INDIA] Startup ZOMBIE RESCUED: {rescued}")
        except Exception as ze:
            logger.debug(f"[INDIA] startup zombie rescue skipped: {ze}")


def _rebuild_india_margin_book(india_broker, risk_mgr, positions: dict | None) -> None:
    """Recompute in-memory MIS margin from broker-open qty (API, else notional/5)."""
    if not hasattr(risk_mgr, "reset_margin_book"):
        return
    risk_mgr.reset_margin_book()
    product = str(getattr(config, "INDIA_PRODUCT_TYPE", "INTRADAY") or "INTRADAY")
    for sym, pos in (positions or {}).items():
        qty = int((pos or {}).get("qty") or 0)
        px = float((pos or {}).get("avg_entry_price") or (pos or {}).get("current_price") or 0)
        if qty <= 0 or px <= 0:
            continue
        m = 0.0
        if hasattr(india_broker, "get_margin_required"):
            try:
                m = float(india_broker.get_margin_required(sym, qty, px, product) or 0)
            except Exception:
                m = 0.0
        if m <= 0:
            m = (qty * px) / 5.0
        risk_mgr.add_margin(sym, m)


def _india_try_sell(
    *,
    india_broker,
    risk_mgr,
    symbol: str,
    current_positions: dict,
) -> bool:
    if symbol not in current_positions:
        return False
    if order_guards.is_exit_inflight(symbol):
        logger.info(f"{symbol}: SELL skipped — exit already in flight")
        return False
    pos = current_positions[symbol]
    entry = float(pos.get("avg_entry_price") or 0)
    fallback = float(pos.get("current_price") or 0)
    fill = india_broker.close_position(symbol)
    if fill is not None:
        px = float(fill) if float(fill) > 0 else 0.0
        if px <= 0:
            try:
                px = float(india_broker.latest_sell_fill_price(symbol) or 0)
            except Exception:
                px = 0.0
        if px <= 0 and fallback > 0 and (entry <= 0 or abs(fallback - entry) > 1e-6):
            px = fallback
        if px <= 0 or (entry > 0 and abs(px - entry) < 1e-9):
            logger.error(
                f"{symbol}: SELL filled but refuse journal @ entry/unknown "
                f"(fill={fill} entry={entry}) — will resync later"
            )
            # Position may be flat at broker; clear local meta but leave journal open
            risk_mgr.clear_trade(symbol)
            risk_mgr.release_margin(symbol)
            current_positions.pop(symbol, None)
            return True
        qty = int(getattr(india_broker, "last_fill_qty", 0) or pos.get("qty") or 0) or None
        bpnl = None
        try:
            bpnl = india_broker.get_closed_position_pnl(symbol)
        except Exception:
            pass
        trade_journal.record_exit(
            "INDIA", symbol, px, reason="signal_sell", qty=qty, broker_pnl=bpnl
        )
        risk_mgr.clear_trade(symbol)
        risk_mgr.release_margin(symbol)
        alerts.trade_alert("INDIA", "SELL", symbol, f"@{px}")
        current_positions.pop(symbol, None)
        return True
    err = str(getattr(india_broker, "last_error", "") or "")
    if err:
        logger.error(f"{symbol}: SELL failed — {err}")
    return False


def _reconcile_india_journal(india_broker, risk_mgr, current_positions: dict) -> None:
    """Close journal rows when Dhan is already flat (broker SL / app close)."""
    try:
        def _px(sym: str) -> float:
            # Prefer today's Dhan SELL/SL fill so broker_flat never journals @ entry.
            try:
                fill = float(india_broker.latest_sell_fill_price(sym) or 0)
                if fill > 0:
                    return fill
            except Exception:
                pass
            pos = (current_positions or {}).get(sym) or {}
            mark = float(pos.get("current_price") or 0)
            if mark > 0:
                return mark
            q = india_broker.get_latest_quote(sym)
            if q and q.get("ltp"):
                return float(q["ltp"])
            return 0.0

        def _bpnl(sym: str) -> float | None:
            try:
                return india_broker.get_closed_position_pnl(sym)
            except Exception:
                return None

        closed = trade_journal.reconcile_broker_flats(
            "INDIA",
            set((current_positions or {}).keys()),
            price_lookup=_px,
            pnl_lookup=_bpnl,
            reason="broker_flat",
        )
        for sym in closed:
            risk_mgr.clear_trade(sym)
            risk_mgr.release_margin(sym)
            if current_positions is not None:
                current_positions.pop(sym, None)
        if closed:
            logger.info(f"[INDIA] Journal reconciled broker flats: {closed}")

        # Fix already-closed bad rows (stale LTP / entry-as-exit) from Dhan fills.
        try:
            fixed = trade_journal.resync_today_exits_from_fills(
                "INDIA",
                india_broker.latest_sell_fill_price,
                tz_name="Asia/Kolkata",
            )
            if fixed:
                logger.warning(
                    f"[INDIA] Journal exit fills resynced from Dhan: "
                    f"{[r.get('symbol') for r in fixed]}"
                )
        except Exception as re:
            logger.debug(f"[INDIA] journal fill resync skipped: {re}")
    except Exception as e:
        logger.debug(f"[INDIA] journal reconcile skipped: {e}")


def is_india_market_open() -> bool:
    """Returns True if current time is between Mon-Fri 9:15 AM - 3:30 PM IST."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:
        return False
    
    open_time = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    close_time = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_time <= now_ist <= close_time



def run_india_loop(strategy, risk_mgr, rs_filter=None):
    from india_client import get_shared_india_broker

    logger.info("[INDIA] India Market trading loop starting (24/7 process)...")

    try:
        india_broker = get_shared_india_broker(auto_login=True)
    except Exception as e:
        logger.error(f"[INDIA] Failed to initialize India broker: {e}")
        return

    if not india_broker.is_logged_in:
        logger.error(
            f"[INDIA] Login failed: {india_broker.last_error}. "
            "Will keep process alive and retry after cooldown "
            "(do not restart the container repeatedly)."
        )
        # Do not exit — cooldown + ensure_session will retry later
    else:
        india_broker.cancel_all_open_orders()

    start_of_day_equity = None
    last_reset_date = None
    paper = india_broker.paper is not None

    logger.info(
        f"[INDIA] Ready | Mode={'PAPER SIM + live NSE data' if paper else 'LIVE REAL MONEY'} "
        f"| Strategy={strategy.name} | universe={len(config.INDIA_STOCK_UNIVERSE)} "
        f"| interval={config.INDIA_LOOP_INTERVAL_SEC}s "
        f"| fetch_gap={float(getattr(config, 'INDIA_LOOP_FETCH_GAP_SEC', 0.4) or 0)}s"
    )
    _india_restore_runtime_state(india_broker, risk_mgr, strategy)
    if hasattr(india_broker, "start_exit_health_monitor"):
        try:
            india_broker.start_exit_health_monitor()
        except Exception as he:
            logger.warning(f"[INDIA] exit health monitor skip: {he}")

    while True:
        try:
            loop_start = time.time()
            now_ist = datetime.now(IST)

            if last_reset_date != now_ist.date():
                risk_mgr.reset_kill_switch()
                bot_state.set_tide_state(bearish=False, reason="")
                start_of_day_equity = None
                last_reset_date = now_ist.date()
                if india_broker.paper is not None:
                    india_broker.paper.start_of_day_equity = None
                logger.info(f"[INDIA] New day {now_ist.date()} — kill-switch reset")

            if hasattr(india_broker, "maybe_session_reset"):
                try:
                    prev_reset = getattr(india_broker, "_session_reset_day", None)
                    india_broker.maybe_session_reset(now_ist)
                    if getattr(india_broker, "_session_reset_day", None) != prev_reset:
                        risk_mgr.reset_margin_book()
                except Exception:
                    pass

            if not is_india_market_open():
                logger.info(
                    f"[INDIA STATUS] Market CLOSED ({now_ist.strftime('%A %I:%M %p IST')}). "
                    f"Bot still alive — next check in {config.INDIA_LOOP_INTERVAL_SEC}s..."
                )
                if now_ist.hour == 9 and 10 <= now_ist.minute < 15:
                    # Single attempt; login() no-ops during cooldown
                    india_broker.login()
                time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                continue

            if not india_broker.is_logged_in:
                india_broker.ensure_session()
                if not india_broker.is_logged_in:
                    logger.warning(
                        f"[INDIA] Not logged in ({india_broker.last_error}) — "
                        f"skipping cycle"
                    )
                    time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                    continue

            logger.info("=" * 60)
            logger.info(
                f"[INDIA CYCLE] {now_ist.strftime('%Y-%m-%d %I:%M:%S %p IST')} | "
                f"Mode={'PAPER' if paper else 'LIVE'} | Strategy={strategy.name}"
            )

            closed = india_broker.check_sl_tp(risk_mgr)
            if closed:
                logger.info(f"[INDIA] Closed via SL/TP: {closed}")

            if hasattr(india_broker, "rescue_zombie_positions"):
                try:
                    rescued = india_broker.rescue_zombie_positions(risk_mgr, strategy)
                    if rescued:
                        logger.warning(f"[INDIA] ZOMBIE RESCUED: {rescued}")
                except Exception as ze:
                    logger.debug(f"[INDIA] zombie rescue skipped: {ze}")

            # Early positions snapshot for journal/broker sync (also refreshed later)
            try:
                early_pos = india_broker.get_open_positions()
                if early_pos is None:
                    logger.warning(
                        "[INDIA] Positions fetch failed early in cycle — skipping cycle "
                        "to avoid broker_flat reconciliation on stale data"
                    )
                    time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                    continue
                _reconcile_india_journal(india_broker, risk_mgr, early_pos)
            except Exception as e:
                logger.debug(f"[INDIA] early reconcile skipped: {e}")

            if config.INDIA_PRODUCT_TYPE.upper() in ("INTRADAY", "INTRA", "MIS"):
                # Auto square-off from SQUAREOFF_TIME (default 14:55 IST)
                sq_h, sq_m = risk_mgr.squareoff_hm()
                if risk_mgr.past_squareoff(now_ist):
                    logger.info(
                        f"[INDIA MIS SQUAREOFF] >= {sq_h:02d}:{sq_m:02d} IST — "
                        f"queueing staggered flatten (skip universe scan)"
                    )
                    if hasattr(india_broker, "request_intraday_squareoff"):
                        india_broker.request_intraday_squareoff(risk_mgr=risk_mgr)
                    elif hasattr(india_broker, "square_off_intraday_positions"):
                        sq = india_broker.square_off_intraday_positions()
                        if sq:
                            logger.info(f"[INDIA INTRADAY AUTO-SQUAREOFF] Closed: {sq}")
                            for _sym in sq:
                                risk_mgr.release_margin(_sym)
                                risk_mgr.clear_trade(_sym)
                        failed = getattr(india_broker, "last_squareoff_failed", None) or []
                        if failed:
                            logger.critical(
                                f"[INDIA MIS SQUAREOFF] FAILED (still open / RMS reject): "
                                f"{failed} — do NOT assume flat"
                            )
                            alerts.notify(
                                f"MIS square-off FAILED still open: {failed}",
                                event="squareoff_fail",
                            )
                    elif india_broker.paper is not None and hasattr(
                        india_broker.paper, "check_intraday_squareoff"
                    ):
                        marks = {}
                        for sym in list(india_broker.paper.positions.keys()):
                            q = india_broker.get_latest_quote(sym)
                            if q:
                                marks[sym] = float(q.get("ltp", 0))
                        sq_closed = india_broker.paper.check_intraday_squareoff(marks)
                        if sq_closed:
                            logger.info(
                                f"[INDIA INTRADAY AUTO-SQUAREOFF] Closed positions: {sq_closed}"
                            )
                    bot_state.mark_healthy("INDIA")
                    log_india_portfolio_summary(india_broker, "INDIA")
                    remaining = float(config.INDIA_LOOP_INTERVAL_SEC)
                    risk_tick = max(1, int(getattr(config, "INDIA_RISK_CHECK_INTERVAL_SEC", 5)))
                    logger.info(
                        "[INDIA] Square-off window — scanner paused; fast SL/TP + "
                        "EXIT_PENDING_STUCK health only"
                    )
                    while remaining > 0:
                        step = min(float(risk_tick), remaining)
                        time.sleep(step)
                        remaining -= step
                        if remaining <= 0:
                            break
                        try:
                            if hasattr(india_broker, "reconcile_stuck_exits"):
                                india_broker.reconcile_stuck_exits()
                            if hasattr(india_broker, "rescue_zombie_positions"):
                                india_broker.rescue_zombie_positions(risk_mgr, strategy)
                            fast_closed = india_broker.check_sl_tp(risk_mgr)
                            if fast_closed:
                                logger.info(
                                    f"[INDIA FAST RISK] Closed via SL/TP: {fast_closed}"
                                )
                        except Exception as e:
                            logger.debug(f"[INDIA FAST RISK] square-off wait skip: {e}")
                    continue
                elif india_broker.paper is not None and hasattr(
                    india_broker.paper, "check_intraday_squareoff"
                ):
                    marks = {
                        sym: float(india_broker.get_latest_quote(sym).get("ltp", 0))
                        for sym in india_broker.paper.positions
                        if india_broker.get_latest_quote(sym)
                    }
                    sq_closed = india_broker.paper.check_intraday_squareoff(marks)
                    if sq_closed:
                        logger.info(
                            f"[INDIA INTRADAY AUTO-SQUAREOFF] Closed positions: {sq_closed}"
                        )

            account = india_broker.get_account_info()
            if account is None:
                logger.error("[INDIA] Cannot retrieve account info — skipping.")
                time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                continue

            current_equity = account["equity"]
            sod = account.get("last_equity") or start_of_day_equity
            if sod is None or sod <= 0:
                sod = bot_state.india_sod_equity(current_equity)
                start_of_day_equity = sod
            else:
                start_of_day_equity = sod
                bot_state.india_sod_equity(sod)

            trade_journal.snapshot_equity("INDIA", current_equity)

            mis_day_pl = _india_mis_day_pl(india_broker, current_equity)
            if risk_mgr.check_daily_drawdown(
                current_equity, start_of_day_equity, day_pl=mis_day_pl
            ):
                logger.critical("[INDIA KILL-SWITCH] Trading PAUSED (daily drawdown).")
                alerts.kill_switch_alert("INDIA", True)
                india_broker.cancel_all_open_orders()
                if getattr(config, "DAILY_FLATTEN_ON_KILL", True) and config.INDIA_PRODUCT_TYPE.upper() in (
                    "INTRADAY",
                    "INTRA",
                    "MIS",
                ):
                    if hasattr(india_broker, "square_off_intraday_positions"):
                        sq = india_broker.square_off_intraday_positions()
                        if sq:
                            logger.warning(f"[INDIA KILL FLATTEN] Closed: {sq}")
                            for _sym in sq:
                                risk_mgr.release_margin(_sym)
                                risk_mgr.clear_trade(_sym)
                bot_state.mark_healthy("INDIA")
                time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                continue

            if risk_mgr.is_kill_switch_active:
                logger.critical("[INDIA KILL-SWITCH] Active — skipping entries.")
                bot_state.mark_healthy("INDIA")
                time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                continue

            tide_lock = market_tide_filter(india_broker)

            tradable_window = risk_mgr.is_tradable_session(
                now_ist, market_open_hm=(9, 15), market_close_hm=(15, 30)
            )

            current_positions = india_broker.get_open_positions()
            if current_positions is None:
                logger.warning(
                    "[INDIA] Positions fetch failed — skipping cycle to avoid fake exits/duplicate buys"
                )
                time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                continue
            _reconcile_india_journal(india_broker, risk_mgr, current_positions)

            # Core loop scans INDIA_STOCK_UNIVERSE only. Scout is a separate optional loop.
            # 5-min ORB: 60s interval is enough for ~50 names; pause between fetches to avoid 429.
            universe = list(config.INDIA_STOCK_UNIVERSE)
            fetch_gap = float(getattr(config, "INDIA_LOOP_FETCH_GAP_SEC", 0.4) or 0)
            bar_cache: dict = {}
            for i, symbol in enumerate(universe):
                df = india_broker.get_historical_bars(symbol)
                if df is not None and not df.empty:
                    bar_cache[symbol] = strategy.compute_indicators(df)
                pause = config.inter_symbol_fetch_gap_sec(fetch_gap, i, len(universe))
                if pause > 0:
                    time.sleep(pause)
            _refresh_rs_filter(rs_filter, bar_cache)
            _refresh_strategy_universe(strategy, bar_cache)

            regime_ok = regime_allows("INDIA", bar_cache)
            signal_rows = []

            for symbol in universe:
                logger.info(f"[INDIA] -- Scanning {symbol} " + "-" * (40 - len(symbol)))

                df = bar_cache.get(symbol)
                if df is None or df.empty:
                    logger.warning(f"[INDIA] {symbol}: No data — skipping.")
                    continue

                snap = snapshot_signal(strategy, df, symbol)
                if tide_lock:
                    snap = apply_tide_lock_to_signal(snap)
                signal_rows.append(snap)
                signal = snap["signal"]
                atr = strategy.latest_atr(df)

                if signal == "BUY":
                    _india_try_buy(
                        india_broker=india_broker,
                        risk_mgr=risk_mgr,
                        strategy=strategy,
                        symbol=symbol,
                        snap=snap,
                        atr=atr,
                        current_equity=current_equity,
                        account=account,
                        current_positions=current_positions,
                        tradable_window=tradable_window,
                        regime_ok=regime_ok,
                        reason="signal_buy",
                        source="core",
                    )

                elif signal == "SELL":
                    _india_try_sell(
                        india_broker=india_broker,
                        risk_mgr=risk_mgr,
                        symbol=symbol,
                        current_positions=current_positions,
                    )

            bot_state.publish_signals("INDIA", signal_rows)
            bot_state.mark_healthy("INDIA")
            log_india_portfolio_summary(india_broker, "INDIA")

            elapsed = time.time() - loop_start
            sleep_time = max(0, config.INDIA_LOOP_INTERVAL_SEC - elapsed)
            logger.info(f"[INDIA COMPLETE] Cycle done ({elapsed:.1f}s). Next in {sleep_time:.0f}s.\n")

            # Run lightweight SL/TP checks during idle wait so exits are not delayed
            # by full scanner-cycle latency.
            remaining = float(sleep_time)
            risk_tick = max(1, int(getattr(config, "INDIA_RISK_CHECK_INTERVAL_SEC", 5)))
            while remaining > 0:
                step = min(float(risk_tick), remaining)
                time.sleep(step)
                remaining -= step
                if remaining <= 0:
                    break
                if not is_india_market_open():
                    continue
                try:
                    if hasattr(india_broker, "reconcile_stuck_exits"):
                        india_broker.reconcile_stuck_exits()
                    if hasattr(india_broker, "rescue_zombie_positions"):
                        india_broker.rescue_zombie_positions(risk_mgr, strategy)
                    fast_closed = india_broker.check_sl_tp(risk_mgr)
                    if fast_closed:
                        logger.info(f"[INDIA FAST RISK] Closed via SL/TP: {fast_closed}")
                except Exception as e:
                    logger.debug(f"[INDIA FAST RISK] check skipped: {e}")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"[INDIA ERROR] {e}", exc_info=True)
            bot_state.mark_cycle("INDIA", error=str(e))
            alerts.health_alert(f"INDIA loop error: {e}")
            time.sleep(config.INDIA_LOOP_INTERVAL_SEC)


def run_india_scout_loop(strategy, risk_mgr, rs_filter=None):
    """
    Optional scout loop (INDIA_SCOUT_ENABLED). Core INDIA_STOCK_UNIVERSE stays
    on the India Strategy Scanner. Scout-only names get BUY here when
    INDIA_SCOUT_AUTO_BUY=true. Near Setups (close but not confirmed) for the dashboard.
    """
    from india_client import get_shared_india_broker
    from india_scout import (
        rank_near_setups,
        resolve_scout_universe,
        score_near_setup,
        scout_only_symbols,
    )
    from strategy import params_for_market

    if not config.INDIA_SCOUT_ENABLED:
        logger.info("[INDIA SCOUT] Disabled (INDIA_SCOUT_ENABLED=false)")
        return

    auto_buy = bool(config.INDIA_SCOUT_AUTO_BUY)
    logger.info(
        f"[INDIA SCOUT] Starting | auto_buy={auto_buy} | "
        f"near-setups panel for close-but-not-confirmed"
    )

    try:
        india_broker = get_shared_india_broker(auto_login=True)
    except Exception as e:
        logger.error(f"[INDIA SCOUT] Broker init failed: {e}")
        bot_state.mark_cycle("INDIA_SCOUT", error=str(e))
        return

    params = params_for_market("INDIA")
    universe = resolve_scout_universe()
    core_set = {s.upper() for s in config.INDIA_STOCK_UNIVERSE}
    scout_trade_set = {s for s in scout_only_symbols()}
    gap = max(0.15, float(config.INDIA_SCOUT_FETCH_GAP_SEC))
    interval = max(300, int(config.INDIA_SCOUT_INTERVAL_SEC))
    paper = india_broker.paper is not None

    rs_n = getattr(rs_filter, "top_n", None) if rs_filter else None
    logger.info(
        f"[INDIA SCOUT] Ready | universe={len(universe)} "
        f"(scout-only tradeable={len(scout_trade_set)}, core={len(core_set)}) | "
        f"interval={interval}s | near_top_n={config.INDIA_SCOUT_TOP_N} | "
        f"rs_top_n={rs_n} | auto_buy={auto_buy} | Mode={'PAPER' if paper else 'LIVE'}"
    )
    # Shared risk_mgr already restored by core loop; refresh broker reconcile once here too
    _india_restore_runtime_state(india_broker, risk_mgr, strategy)

    while True:
        try:
            loop_start = time.time()
            now_ist = datetime.now(IST)

            if not is_india_market_open():
                logger.info(
                    f"[INDIA SCOUT] Market CLOSED ({now_ist.strftime('%A %I:%M %p IST')}). "
                    f"Next check in {interval}s..."
                )
                time.sleep(interval)
                continue

            if not india_broker.is_logged_in:
                india_broker.ensure_session()
                if not india_broker.is_logged_in:
                    logger.warning(
                        f"[INDIA SCOUT] Not logged in ({india_broker.last_error}) — skip"
                    )
                    time.sleep(interval)
                    continue

            # Refresh caps / kill state via account (same risk surface as core)
            account = india_broker.get_account_info()
            if account is None:
                logger.error("[INDIA SCOUT] No account info — skipping cycle")
                time.sleep(interval)
                continue

            current_equity = account["equity"]
            sod = account.get("last_equity")
            if sod is None or sod <= 0:
                sod = bot_state.india_sod_equity(current_equity)
            else:
                bot_state.india_sod_equity(sod)

            if risk_mgr.check_daily_drawdown(
                current_equity, sod, day_pl=_india_mis_day_pl(india_broker, current_equity)
            ):
                logger.critical("[INDIA SCOUT] Kill-switch (daily drawdown) — no entries")
                bot_state.mark_healthy("INDIA_SCOUT")
                time.sleep(interval)
                continue
            if risk_mgr.is_kill_switch_active:
                logger.critical("[INDIA SCOUT] Kill-switch active — skipping entries")
                bot_state.mark_healthy("INDIA_SCOUT")
                time.sleep(interval)
                continue

            tide_lock = market_tide_filter(india_broker)

            tradable_window = risk_mgr.is_tradable_session(
                now_ist, market_open_hm=(9, 15), market_close_hm=(15, 30)
            )
            current_positions = india_broker.get_open_positions()
            if current_positions is None:
                logger.warning(
                    "[INDIA SCOUT] Positions fetch failed — skipping cycle "
                    "to avoid fake exits/duplicate buys"
                )
                time.sleep(interval)
                continue
            _reconcile_india_journal(india_broker, risk_mgr, current_positions)

            bar_cache: dict = {}
            scored: list = []
            scanned = 0
            skipped = 0
            buys = 0
            sells = 0

            for i, symbol in enumerate(universe):
                try:
                    df = india_broker.get_historical_bars(symbol)
                    if df is None or df.empty:
                        skipped += 1
                    else:
                        df = strategy.compute_indicators(df)
                        bar_cache[symbol] = df
                        scored.append(score_near_setup(df, params, symbol=symbol))
                        scanned += 1
                except Exception as se:
                    skipped += 1
                    logger.debug(f"[INDIA SCOUT] {symbol}: {se}")
                pause = config.inter_symbol_fetch_gap_sec(gap, i, len(universe))
                if pause > 0:
                    time.sleep(pause)

            _refresh_rs_filter(rs_filter, bar_cache)
            _refresh_strategy_universe(strategy, bar_cache)
            # Regime uses scout bars when available (RELIANCE usually present)
            regime_ok = regime_allows("INDIA", bar_cache)

            # Trade scout-only names on full signal; core INDIA_STOCK_UNIVERSE is handled by India loop
            for symbol in scout_trade_set:
                df = bar_cache.get(symbol)
                if df is None or df.empty:
                    continue
                snap = snapshot_signal(strategy, df, symbol)
                if tide_lock:
                    snap = apply_tide_lock_to_signal(snap)
                signal = snap["signal"]
                atr = strategy.latest_atr(df)

                if signal == "BUY" and auto_buy:
                    logger.info(f"[INDIA SCOUT] BUY candidate {symbol}")
                    if _india_try_buy(
                        india_broker=india_broker,
                        risk_mgr=risk_mgr,
                        strategy=strategy,
                        symbol=symbol,
                        snap=snap,
                        atr=atr,
                        current_equity=current_equity,
                        account=account,
                        current_positions=current_positions,
                        tradable_window=tradable_window,
                        regime_ok=regime_ok,
                        reason="scout_signal_buy",
                        source="scout",
                    ):
                        buys += 1
                elif signal == "SELL":
                    if _india_try_sell(
                        india_broker=india_broker,
                        risk_mgr=risk_mgr,
                        symbol=symbol,
                        current_positions=current_positions,
                    ):
                        sells += 1

            near = rank_near_setups(scored, exclude_confirmed=True)
            meta = {
                "scanned": scanned,
                "skipped": skipped,
                "universe_size": len(universe),
                "scout_only_size": len(scout_trade_set),
                "top_n": config.INDIA_SCOUT_TOP_N,
                "min_score": config.INDIA_SCOUT_MIN_SCORE,
                "auto_buy": auto_buy,
                "buys": buys,
                "sells": sells,
                "trade_eligible": True,
                "core_universe_size": len(core_set),
            }
            bot_state.publish_scout("INDIA", near, meta=meta)
            bot_state.mark_healthy("INDIA_SCOUT")

            preview = ", ".join(f"{r['symbol']}={r['score']}" for r in near[:5]) or "(none)"
            elapsed = time.time() - loop_start
            sleep_time = max(0, interval - elapsed)
            logger.info(
                f"[INDIA SCOUT] Cycle done ({elapsed:.1f}s) scanned={scanned} "
                f"skipped={skipped} buys={buys} sells={sells} near={preview} | "
                f"next in {sleep_time:.0f}s"
            )
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"[INDIA SCOUT ERROR] {e}", exc_info=True)
            bot_state.mark_cycle("INDIA_SCOUT", error=str(e))
            time.sleep(interval)



def run_bot():
    logger.info("=" * 70)
    logger.info("   AI MIS INTRADAY BOT — INDIA NSE (same stack as New_StartUp)")
    logger.info(
        f"   India: enabled={config.INDIA_ENABLED} | broker={config.INDIA_BROKER} | "
        f"product={config.INDIA_PRODUCT_TYPE} | "
        f"paper_sim={config.INDIA_PAPER} | live_armed={config.LIVE_CONFIRMED}"
    )
    logger.info(
        f"   Strategy={config.STRATEGY_NAME} | TF={config.TIMEFRAME} | "
        f"OR={getattr(config,'OR_MINUTES',15)}m | "
        f"ORB_window={getattr(config,'ORB_WINDOW_MINUTES',60)}m | "
        f"cutoff={config.ENTRY_CUTOFF} | "
        f"squareoff={config.SQUAREOFF_TIME} | capital_cap={getattr(config,'INDIA_CAPITAL_CAP',0):,.0f}"
    )
    logger.info(
        f"   Risk/trade={config.RISK_PER_TRADE:.2%} | ATR_SL={config.ATR_STOP_MULT}x | "
        f"TP={config.TAKE_PROFIT_R}R | MaxOpen={config.MAX_OPEN_POSITIONS} | "
        f"DD={config.DAILY_DRAWDOWN_LIMIT:.2%}"
    )
    logger.info("=" * 70)

    if not any((config.INDIA_ENABLED, config.INDIA_FNO_ENABLED, config.MCX_ENABLED, config.CURRENCY_ENABLED)):
        logger.critical(
            "No markets configured. Add Dhan (DHAN_*) or Angel (ANGEL_*) keys to .env"
        )
        return

    if config.LIVE_CONFIRMED:
        logger.critical("!!! INDIA REAL MONEY MIS MODE ARMED !!!")
    elif config.INDIA_PAPER:
        logger.info("India PAPER SIM on — live NSE data, fake INR (safe for testing)")


    trade_journal.init_db()

    # Automatically load Dhan Scrip Master at startup (optional module)
    try:
        from india_fno_instruments import load_dhan_scrip_master

        load_dhan_scrip_master()
    except Exception as e:
        logger.warning(f"Dhan Scrip Master initialization warning: {e}")

    # Authenticate Dhan first so PIN/TOTP refreshes the access token used by
    # the paid ₹499 Live Market Feed (WebSocket) before we subscribe.
    if config.INDIA_BROKER == "dhan" and config.DHAN_CONFIGURED:
        try:
            from india_client import get_shared_india_broker

            india_auth = get_shared_india_broker(auto_login=True)
            if getattr(india_auth, "is_logged_in", False):
                logger.info(
                    "[FEED] Dhan login OK — Live Data API credentials synced to WebSocket"
                )
            else:
                logger.warning(
                    "[FEED] Dhan login failed before Live Feed: %s",
                    getattr(india_auth, "last_error", "unknown"),
                )
        except Exception as auth_e:
            logger.warning(f"[FEED] Pre-feed Dhan login warning: {auth_e}")

    # Subscribe active universes to Live WebSocket feed
    try:
        from dhan_live_feed import get_live_feed_manager

        feed_mgr = get_live_feed_manager()
        if feed_mgr.enabled or getattr(feed_mgr, "_want_live", False):
            n = 0
            n += feed_mgr.subscribe_universe(config.INDIA_STOCK_UNIVERSE, "NSE_EQ")
            if config.INDIA_SCOUT_ENABLED:
                try:
                    from india_scout import resolve_scout_universe

                    n += feed_mgr.subscribe_universe(resolve_scout_universe(), "NSE_EQ")
                except Exception as se:
                    logger.warning(f"[FEED] Scout universe subscribe warning: {se}")
            if config.INDIA_FNO_ENABLED:
                n += feed_mgr.subscribe_universe(config.INDIA_FNO_UNIVERSE, "NSE_FNO")
            if config.MCX_ENABLED:
                n += feed_mgr.subscribe_universe(config.MCX_UNIVERSE, "MCX_COMM")
            if config.CURRENCY_ENABLED:
                n += feed_mgr.subscribe_universe(config.CURRENCY_UNIVERSE, "NSE_CURRENCY")
            logger.info(
                f"[FEED] Subscribed {n} India/MCX/FX instruments | "
                f"feed_enabled={feed_mgr.enabled} connected={feed_mgr.is_connected()}"
            )
        else:
            logger.info("[FEED] India Live WebSocket disabled — REST quote mode")
    except Exception as fe:
        logger.warning(f"[FEED] Universe live feed subscription warning: {fe}")


    try:
        dash_port = int(os.environ.get("PORT", 5000))
        start_dashboard_in_background(port=dash_port)
        logger.info(f"[DASHBOARD] http://0.0.0.0:{dash_port}")
    except Exception as e:
        logger.warning(f"Dashboard failed to start: {e}")

    # --- India loop ---
    if config.INDIA_ENABLED:
        india_rs = RelativeStrengthFilter() if config.USE_RELATIVE_STRENGTH else None
        india_strategy = create_strategy("INDIA", rs_filter=india_rs)
        india_risk = RiskManager(market="INDIA")

        india_thread = threading.Thread(
            target=run_india_loop,
            args=(india_strategy, india_risk, india_rs),
            daemon=True,
            name="IndiaMarketLoop",
        )
        india_thread.start()
        logger.info(
            f"[INDIA] Background loop started ({config.INDIA_BROKER} + paper/live)"
        )
        if config.INDIA_SCOUT_ENABLED:
            scout_rs = (
                RelativeStrengthFilter(top_n=config.INDIA_SCOUT_RS_TOP_N)
                if config.USE_RELATIVE_STRENGTH
                else None
            )
            scout_strategy = create_strategy("INDIA", rs_filter=scout_rs)
            scout_thread = threading.Thread(
                target=run_india_scout_loop,
                args=(scout_strategy, india_risk, scout_rs),
                daemon=True,
                name="IndiaScoutLoop",
            )
            scout_thread.start()
            logger.info(
                f"[INDIA SCOUT] Background loop started "
                f"(interval={config.INDIA_SCOUT_INTERVAL_SEC}s, "
                f"rs_top_n={config.INDIA_SCOUT_RS_TOP_N}, "
                f"auto_buy={config.INDIA_SCOUT_AUTO_BUY})"
            )
        else:
            logger.info("[INDIA SCOUT] Skipped (INDIA_SCOUT_ENABLED=false)")
    else:
        logger.warning(
            "[INDIA] Disabled — missing Dhan/Angel credentials "
            f"(INDIA_BROKER={config.INDIA_BROKER})"
        )

    def _calc_rsi_adx(df):
        """Lightweight RSI + ADX for expansion loops (no spam heuristics)."""
        import pandas as pd

        close = df["close"].astype(float)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, pd.NA)
        rsi = float((100 - (100 / (1 + rs))).iloc[-1])
        # Simplified ADX via |+DM - -DM| proxy from high/low if available
        adx = 25.0
        if "high" in df.columns and "low" in df.columns:
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            plus_dm = high.diff().clip(lower=0)
            minus_dm = (-low.diff()).clip(lower=0)
            tr = (high - low).rolling(14).mean()
            plus_di = 100 * (plus_dm.rolling(14).mean() / tr.replace(0, pd.NA))
            minus_di = 100 * (minus_dm.rolling(14).mean() / tr.replace(0, pd.NA))
            dx = (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, pd.NA)) * 100
            adx_v = dx.rolling(14).mean().iloc[-1]
            if adx_v == adx_v:  # not NaN
                adx = float(adx_v)
        atr = float((df["high"] - df["low"]).tail(14).mean()) if "high" in df.columns else 0.0
        sma_fast = float(close.tail(20).mean())
        sma_slow = float(close.tail(min(50, len(close))).mean())
        return rsi, adx, sma_fast, sma_slow, atr

    # --- India F&O loop ---
    if config.INDIA_FNO_ENABLED:

        def run_fno_loop():
            logger.info("[FNO] Loop started (09:15-15:30 IST)...")
            from india_fno_broker import get_shared_fno_broker
            from fno_strategy import FnoStrategy

            fno_broker = get_shared_fno_broker()
            fno_strat = FnoStrategy(config.INDIA_FNO_STRATEGY)

            while True:
                try:
                    if is_india_market_open():
                        fno_broker.check_exits()
                        for sym in config.INDIA_FNO_UNIVERSE:
                            if fno_broker.risk_mgr.is_kill_switch_active:
                                logger.warning("[FNO] Kill switch — idle")
                                break
                            quote = fno_broker.dhan_broker.get_latest_quote(sym)
                            from price_guards import require_tradeable_quote

                            spot, qerr = require_tradeable_quote(sym, quote, segment="FNO")
                            if qerr:
                                logger.info(f"[FNO] {sym}: skip — {qerr}")
                                continue
                            df = fno_broker.dhan_broker.get_historical_candles(
                                sym, timeframe="1Hour", days=30
                            )
                            min_bars = 20 if config.INDIA_FNO_PAPER else 30
                            if df is None or len(df) < min_bars:
                                logger.info(
                                    f"[FNO] {sym}: skip — insufficient candles "
                                    f"({0 if df is None else len(df)}<{min_bars})"
                                )
                                continue
                            rsi, adx, sma_fast, sma_slow, atr = _calc_rsi_adx(df)
                            sig = fno_strat.generate_signal(
                                sym,
                                spot,
                                rsi=rsi,
                                adx=adx,
                                sma_fast=sma_fast,
                                sma_slow=sma_slow,
                                atr=atr,
                            )
                            if not sig:
                                logger.info(
                                    f"[FNO] {sym}: skip — {fno_strat.last_skip_reason or 'no signal'} "
                                    f"(RSI={rsi:.1f} ADX={adx:.1f})"
                                )
                                continue
                            chain = fno_broker.get_option_chain(sym)
                            strike = fno_broker.get_atm_strike(sym, spot, chain=chain)
                            prem = fno_broker.fetch_live_option_premium(
                                sym, strike, sig["option_type"]
                            )
                            if prem <= 0:
                                logger.info(f"[FNO] Skip {sym}: no valid premium")
                                continue
                            sl = max(prem * 0.5, prem - sig.get("stop_loss_dist", 0) * 0.01)
                            tp = prem * 1.8
                            fno_broker.place_option_order(
                                sym,
                                strike,
                                sig["option_type"],
                                limit_price=prem,
                                stop_loss=sl,
                                take_profit=tp,
                            )
                    else:
                        logger.debug("[FNO] Outside India session — idle")
                    time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                except Exception as fe:
                    logger.error(f"[FNO] Loop error: {fe}", exc_info=True)
                    time.sleep(config.INDIA_LOOP_INTERVAL_SEC)

        fno_thread = threading.Thread(target=run_fno_loop, daemon=True, name="IndiaFnoLoop")
        fno_thread.start()
        logger.info("[FNO] Background loop started")

    # --- MCX Commodities loop ---
    if config.MCX_ENABLED:

        def run_mcx_loop_runner():
            logger.info("[MCX] Loop started (09:00-23:30 IST)...")
            from mcx_broker import get_shared_mcx_broker

            mcx_broker = get_shared_mcx_broker()
            # Quality gate: require RSI oversold + ADX trend; otherwise log-only
            idle_until_quality = True

            while True:
                try:
                    if mcx_broker.is_mcx_market_open():
                        mcx_broker.check_exits()
                        for sym in config.MCX_UNIVERSE:
                            if mcx_broker.risk_mgr.is_kill_switch_active:
                                break
                            quote = mcx_broker.dhan_broker.get_latest_quote(sym)
                            from price_guards import require_tradeable_quote

                            price, qerr = require_tradeable_quote(sym, quote, segment="MCX")
                            if qerr:
                                logger.info(f"[MCX] {sym}: skip — {qerr}")
                                continue
                            df = mcx_broker.dhan_broker.get_historical_candles(
                                sym, timeframe="1Hour", days=45
                            )
                            min_bars = 15 if config.MCX_PAPER else 30
                            if df is None or len(df) < min_bars:
                                logger.info(
                                    f"[MCX] {sym}: skip — insufficient candles "
                                    f"({0 if df is None else len(df)}<{min_bars})"
                                )
                                continue
                            rsi, adx, sma_fast, sma_slow, atr = _calc_rsi_adx(df)
                            # Paper: RSI<45 + ADX>12; Live: RSI<32 + ADX>18 + SMA filter
                            if config.MCX_PAPER and not config.MCX_LIVE_CONFIRMED:
                                ok = rsi < 45.0 and adx > 12.0
                            else:
                                ok = (
                                    rsi < 32.0
                                    and adx > 18.0
                                    and sma_fast >= sma_slow * 0.995
                                )
                            if ok:
                                sl = price - max(atr * 1.5, price * 0.01)
                                tp = price + max(atr * 2.5, price * 0.015)
                                logger.info(
                                    f"[MCX] Signal {sym}: RSI={rsi:.1f} ADX={adx:.1f} @ {price:.2f}"
                                )
                                mcx_broker.place_buy_order(
                                    sym, 1, price, stop_loss=sl, take_profit=tp
                                )
                                idle_until_quality = False
                            else:
                                logger.info(
                                    f"[MCX] {sym}: skip — no setup (RSI={rsi:.1f} ADX={adx:.1f})"
                                )
                    else:
                        logger.debug("[MCX] Outside session — idle")
                    time.sleep(300)
                except Exception as me:
                    logger.error(f"[MCX] Loop error: {me}", exc_info=True)
                    time.sleep(300)

        mcx_thread = threading.Thread(
            target=run_mcx_loop_runner, daemon=True, name="MCXLoop"
        )
        mcx_thread.start()
        logger.info("[MCX] Background loop started (Commodities)")

    # --- Currency FX loop ---
    if config.CURRENCY_ENABLED:

        def run_currency_loop_runner():
            logger.info("[CURRENCY] Loop started (09:00-17:00 IST)...")
            from currency_broker import get_shared_currency_broker

            currency_broker = get_shared_currency_broker()

            while True:
                try:
                    if currency_broker.is_currency_market_open():
                        currency_broker.check_exits()
                        for sym in config.CURRENCY_UNIVERSE:
                            if currency_broker.risk_mgr.is_kill_switch_active:
                                break
                            quote = currency_broker.dhan_broker.get_latest_quote(sym)
                            from price_guards import require_tradeable_quote

                            price, qerr = require_tradeable_quote(sym, quote, segment="FX")
                            if qerr:
                                logger.info(f"[CURRENCY] {sym}: skip — {qerr}")
                                continue
                            df = currency_broker.dhan_broker.get_historical_candles(
                                sym, timeframe="1Hour", days=60
                            )
                            min_bars = 10 if config.CURRENCY_PAPER else 30
                            if df is None or len(df) < min_bars:
                                logger.info(
                                    f"[CURRENCY] {sym}: skip — insufficient candles "
                                    f"({0 if df is None else len(df)}<{min_bars})"
                                )
                                continue
                            rsi, adx, sma_fast, sma_slow, atr = _calc_rsi_adx(df)
                            if config.CURRENCY_PAPER and not config.CURRENCY_LIVE_CONFIRMED:
                                ok = rsi < 48.0 and adx > 10.0 and price > 0
                            else:
                                ok = rsi < 30.0 and adx > 15.0 and price > 0
                            if ok:
                                sl = price - max(atr * 1.5, price * 0.002)
                                tp = price + max(atr * 2.0, price * 0.003)
                                logger.info(
                                    f"[CURRENCY] Signal {sym}: RSI={rsi:.1f} @ {price:.4f}"
                                )
                                currency_broker.place_buy_order(
                                    sym, 1, price, stop_loss=sl, take_profit=tp
                                )
                            else:
                                logger.info(
                                    f"[CURRENCY] {sym}: skip — no setup (RSI={rsi:.1f} ADX={adx:.1f})"
                                )
                    else:
                        logger.debug("[CURRENCY] Outside session — idle")
                    time.sleep(300)
                except Exception as ce:
                    logger.error(f"[CURRENCY] Loop error: {ce}", exc_info=True)
                    time.sleep(300)

        curr_thread = threading.Thread(
            target=run_currency_loop_runner, daemon=True, name="CurrencyLoop"
        )
        curr_thread.start()
        logger.info("[CURRENCY] Background loop started (NSE USDINR)")


    logger.info(
        "[MAIN] Process staying alive 24/7 for dashboard + market loops | "
        "India sessions only; weekends/holidays closed — NOT 24x7 trading"
    )
    while True:
        time.sleep(60)


if __name__ == "__main__":
    setup_logging()
    logger.info("Starting India MIS Intraday Bot...")
    try:
        run_bot()
    except KeyboardInterrupt:
        logger.info("\nBot stopped by user (Ctrl+C).")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

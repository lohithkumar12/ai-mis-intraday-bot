"""
main.py — Main Orchestrator & Web Admin Dashboard Launcher
===========================================================
24/7 Dual-market bot:
  India → Dhan (default) or Angel One — paper sim / live INR
  US    → Dhan Global Stocks — paper sim / live USD

Dashboard stays alive around the clock; trades only during market hours.
"""

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
from filters import regime_allows, mtf_allows, fetch_daily_bars
from strategy import snapshot_signal
import order_guards

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    ET = ZoneInfo("America/New_York")
except ImportError:
    import pytz  # type: ignore
    IST = pytz.timezone("Asia/Kolkata")
    ET = pytz.timezone("America/New_York")


def setup_logging():
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
    file_handler = logging.FileHandler(log_dir / "trading_bot.log", mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format, date_format))
    root_logger.addHandler(file_handler)


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


def log_us_portfolio_summary(us_broker, market_label: str = "US"):
    positions = us_broker.get_open_positions()
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
            f"${pos['avg_entry_price']:>8.2f} "
            f"${pos['current_price']:>8.2f} "
            f"${pos['unrealized_pl']:>10.2f} "
            f"{pos['unrealized_plpc']:>7.2%}"
        )

    logger.info("   " + "-" * 64)
    logger.info(f"   Total Unrealized P&L: ${total_pl:,.2f}")


def _refresh_rs_filter(rs_filter, symbol_dfs: dict):
    if rs_filter is not None and config.USE_RELATIVE_STRENGTH:
        rs_filter.update_scores(symbol_dfs)


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
        logger.info(f"{symbol}: BUY skipped — regime filter blocked entries")
        return False
    if risk_mgr.is_kill_switch_active:
        logger.info(f"{symbol}: BUY skipped — kill switch active")
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

    sizing_equity = min(
        current_equity,
        float(account.get("available_cash") or current_equity),
    )
    sizing_equity = risk_mgr.effective_equity(sizing_equity)
    sl = risk_mgr.get_stop_loss_price(limit_price, atr)
    tp = risk_mgr.get_take_profit_price(limit_price, stop_loss_price=sl, atr=atr)
    stop_dist = limit_price - sl
    qty = risk_mgr.calculate_position_size(
        sizing_equity, limit_price, stop_distance=stop_dist
    )
    if qty <= 0:
        logger.warning(
            f"{symbol}: BUY skipped — sized to 0 shares "
            f"(price={limit_price:.2f}, equity={sizing_equity:.0f})"
        )
        return False

    new_risk = max(stop_dist, 0.0) * qty

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
        fill_qty = int(getattr(india_broker, "last_fill_qty", 0) or 0)
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
                strategy.mark_day_fired(symbol, "BUY")
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
        return True
    finally:
        risk_mgr.clear_pending_risk(symbol)
        order_guards.release_buy_reservation(symbol)


def _india_restore_runtime_state(india_broker, risk_mgr, strategy) -> None:
    """Load persisted meta / ORB locks and reconcile against live broker positions."""
    try:
        risk_mgr.load_state()
    except Exception as e:
        logger.debug(f"risk state load: {e}")
    if strategy is not None and hasattr(strategy, "load_fired_state"):
        try:
            strategy.load_fired_state()
        except Exception as e:
            logger.debug(f"ORB fired load: {e}")
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
    fallback = float(pos.get("current_price") or pos.get("avg_entry_price") or 0)
    fill = india_broker.close_position(symbol)
    if fill is not None:
        px = float(fill) if float(fill) > 0 else fallback
        qty = int(getattr(india_broker, "last_fill_qty", 0) or pos.get("qty") or 0) or None
        trade_journal.record_exit(
            "INDIA", symbol, px, reason="signal_sell", qty=qty
        )
        risk_mgr.clear_trade(symbol)
        alerts.trade_alert("INDIA", "SELL", symbol, f"@{px}")
        current_positions.pop(symbol, None)
        return True
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

        closed = trade_journal.reconcile_broker_flats(
            "INDIA",
            set((current_positions or {}).keys()),
            price_lookup=_px,
            reason="broker_flat",
        )
        for sym in closed:
            risk_mgr.clear_trade(sym)
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


def is_us_market_open() -> bool:
    """Returns True if current time is between Mon-Fri 9:30 AM - 4:00 PM ET."""
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return False

    open_time = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now_et <= close_time


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
        f"| Strategy={strategy.name}"
    )
    _india_restore_runtime_state(india_broker, risk_mgr, strategy)

    while True:
        try:
            loop_start = time.time()
            now_ist = datetime.now(IST)

            if last_reset_date != now_ist.date():
                risk_mgr.reset_kill_switch()
                start_of_day_equity = None
                last_reset_date = now_ist.date()
                if india_broker.paper is not None:
                    india_broker.paper.start_of_day_equity = None
                logger.info(f"[INDIA] New day {now_ist.date()} — kill-switch reset")

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
                # Auto square-off from SQUAREOFF_TIME (default 15:10 IST)
                sq_h, sq_m = risk_mgr.squareoff_hm()
                if risk_mgr.past_squareoff(now_ist):
                    logger.info(
                        f"[INDIA MIS SQUAREOFF] >= {sq_h:02d}:{sq_m:02d} IST — flattening"
                    )
                    if hasattr(india_broker, "square_off_intraday_positions"):
                        sq = india_broker.square_off_intraday_positions()
                        if sq:
                            logger.info(f"[INDIA INTRADAY AUTO-SQUAREOFF] Closed: {sq}")
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
                bot_state.mark_healthy("INDIA")
                time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                continue

            if risk_mgr.is_kill_switch_active:
                logger.critical("[INDIA KILL-SWITCH] Active — skipping entries.")
                bot_state.mark_healthy("INDIA")
                time.sleep(config.INDIA_LOOP_INTERVAL_SEC)
                continue

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

            bar_cache: dict = {}
            for symbol in config.INDIA_STOCK_UNIVERSE:
                df = india_broker.get_historical_bars(symbol)
                if df is not None and not df.empty:
                    bar_cache[symbol] = strategy.compute_indicators(df)
            _refresh_rs_filter(rs_filter, bar_cache)

            regime_ok = regime_allows("INDIA", bar_cache)
            signal_rows = []

            for symbol in config.INDIA_STOCK_UNIVERSE:
                logger.info(f"[INDIA] -- Scanning {symbol} " + "-" * (40 - len(symbol)))

                df = bar_cache.get(symbol)
                if df is None or df.empty:
                    logger.warning(f"[INDIA] {symbol}: No data — skipping.")
                    continue

                snap = snapshot_signal(strategy, df, symbol)
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
    Scout universe (~Nifty 50): trade-eligible on full BUY + same gates as core.
    Also publishes Near Setups (close but not confirmed) for the dashboard.
    Core INDIA_STOCK_UNIVERSE stays on Strategy Scanner / faster India loop.
    Scout-only names get BUY here when INDIA_SCOUT_AUTO_BUY=true (default).
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
                if i + 1 < len(universe):
                    time.sleep(gap)

            _refresh_rs_filter(rs_filter, bar_cache)
            # Regime uses scout bars when available (RELIANCE usually present)
            regime_ok = regime_allows("INDIA", bar_cache)

            # Trade scout-only names on full signal; core 12 handled by India loop
            for symbol in scout_trade_set:
                df = bar_cache.get(symbol)
                if df is None or df.empty:
                    continue
                snap = snapshot_signal(strategy, df, symbol)
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


def run_us_loop(strategy, risk_mgr, rs_filter=None):
    """US market trading loop — mirrors India loop for Dhan Global Stocks."""
    from us_client import get_shared_us_broker

    logger.info("[US] US Market trading loop starting (24/7 process)...")

    try:
        us_broker = get_shared_us_broker(auto_login=True)
    except Exception as e:
        logger.error(f"[US] Failed to initialize US broker: {e}")
        bot_state.mark_cycle("US", error=str(e))
        return

    if not us_broker.is_logged_in:
        logger.error(
            f"[US] Login failed: {us_broker.last_error}. "
            "Will keep process alive and retry after cooldown."
        )

    if not us_broker.global_stocks_available:
        logger.warning(
            "[US] Global Stocks NOT activated on Dhan account. "
            "US loop will idle. Enable at dhan.co → Settings → Global Stocks."
        )

    start_of_day_equity = None
    last_reset_date_et = None
    paper = us_broker.paper is not None
    mode_label = "US PAPER" if paper else "US LIVE"

    logger.info(
        f"[US] Ready | Mode={'PAPER SIM + live US data' if paper else 'LIVE REAL MONEY'} "
        f"| Strategy={strategy.name}"
    )

    while True:
        try:
            loop_start = time.time()
            now_et = datetime.now(ET)

            # Reset kill switch on new US trading day (ET date)
            if last_reset_date_et != now_et.date():
                risk_mgr.reset_kill_switch()
                start_of_day_equity = None
                last_reset_date_et = now_et.date()
                if us_broker.paper is not None:
                    us_broker.paper.start_of_day_equity = None
                logger.info(f"[US] New day {now_et.date()} (ET) — kill-switch reset")

            if not is_us_market_open():
                now_ist = datetime.now(IST)
                logger.info(
                    f"[US STATUS] Market CLOSED "
                    f"({now_et.strftime('%A %I:%M %p ET')} / "
                    f"{now_ist.strftime('%I:%M %p IST')}). "
                    f"Bot alive — next check in {config.US_LOOP_INTERVAL_SEC}s..."
                )
                # Pre-open login attempt
                if now_et.hour == 9 and 25 <= now_et.minute < 30:
                    us_broker.login()
                time.sleep(config.US_LOOP_INTERVAL_SEC)
                continue

            if not us_broker.is_logged_in:
                us_broker.ensure_session()
                if not us_broker.is_logged_in:
                    logger.warning(
                        f"[US] Not logged in ({us_broker.last_error}) — skipping cycle"
                    )
                    time.sleep(config.US_LOOP_INTERVAL_SEC)
                    continue

            if not us_broker.global_stocks_available and not paper:
                logger.warning("[US] Global Stocks not activated — skipping cycle")
                time.sleep(config.US_LOOP_INTERVAL_SEC)
                continue

            logger.info("=" * 60)
            logger.info(
                f"[{mode_label} CYCLE] "
                f"{now_et.strftime('%Y-%m-%d %I:%M:%S %p ET')} | "
                f"Strategy={strategy.name}"
            )

            # SL/TP check
            closed = us_broker.check_sl_tp(risk_mgr)
            if closed:
                logger.info(f"[US] Closed via SL/TP: {closed}")

            # Account info
            account = us_broker.get_account_info()
            if account is None:
                logger.error("[US] Cannot retrieve account info — skipping.")
                time.sleep(config.US_LOOP_INTERVAL_SEC)
                continue

            current_equity = account["equity"]
            sod = account.get("last_equity") or start_of_day_equity
            if sod is None or sod <= 0:
                sod = bot_state.us_sod_equity(current_equity)
                start_of_day_equity = sod
            else:
                start_of_day_equity = sod
                bot_state.us_sod_equity(sod)

            trade_journal.snapshot_equity("US", current_equity)

            # Daily drawdown check
            if risk_mgr.check_daily_drawdown(current_equity, start_of_day_equity):
                logger.critical(f"[{mode_label} KILL-SWITCH] Trading PAUSED (daily drawdown).")
                alerts.kill_switch_alert("US", True)
                time.sleep(config.US_LOOP_INTERVAL_SEC)
                continue

            if risk_mgr.is_kill_switch_active:
                logger.critical(f"[{mode_label} KILL-SWITCH] Active — skipping entries.")
                time.sleep(config.US_LOOP_INTERVAL_SEC)
                continue

            # Session window check (NYSE hours)
            tradable_window = risk_mgr.is_tradable_session(
                now_et, market_open_hm=(9, 30), market_close_hm=(16, 0)
            )

            current_positions = us_broker.get_open_positions()

            # Scan universe
            bar_cache: dict = {}
            for symbol in config.US_STOCK_UNIVERSE:
                df = us_broker.get_historical_bars(symbol)
                if df is not None and not df.empty:
                    bar_cache[symbol] = strategy.compute_indicators(df)
            _refresh_rs_filter(rs_filter, bar_cache)

            regime_ok = regime_allows("US", bar_cache)
            signal_rows = []

            for symbol in config.US_STOCK_UNIVERSE:
                logger.info(f"[US] -- Scanning {symbol} " + "-" * (40 - len(symbol)))

                df = bar_cache.get(symbol)
                if df is None or df.empty:
                    logger.warning(f"[US] {symbol}: No data — skipping.")
                    continue

                snap = snapshot_signal(strategy, df, symbol)
                signal_rows.append(snap)
                signal = snap["signal"]
                atr = strategy.latest_atr(df)

                if signal == "BUY":
                    if not tradable_window:
                        logger.info(f"[US] {symbol}: BUY skipped — outside tradable session")
                        continue
                    if not regime_ok:
                        logger.info(f"[US] {symbol}: BUY skipped — regime filter blocked")
                        continue
                    if not risk_mgr.is_position_allowed(symbol, current_positions):
                        continue

                    quote = us_broker.get_latest_quote(symbol)
                    limit_price = None
                    if quote and quote.get("ltp"):
                        limit_price = float(quote["ltp"])
                    elif snap.get("price"):
                        limit_price = float(snap["price"])
                        logger.warning(
                            f"[US] {symbol}: no live LTP — using candle close "
                            f"${limit_price:.2f} for entry"
                        )
                    if limit_price is None or limit_price <= 0:
                        logger.warning(f"[US] {symbol}: BUY skipped — no usable price")
                        continue

                    sizing_equity = min(
                        current_equity,
                        float(account.get("available_cash") or current_equity),
                    )

                    sl = risk_mgr.get_stop_loss_price(limit_price, atr)
                    tp = risk_mgr.get_take_profit_price(
                        limit_price, stop_loss_price=sl, atr=atr
                    )
                    stop_dist = limit_price - sl
                    qty = risk_mgr.calculate_position_size(
                        sizing_equity, limit_price, stop_distance=stop_dist
                    )
                    if qty <= 0:
                        logger.warning(
                            f"[US] {symbol}: BUY skipped — sized to 0 shares "
                            f"(price=${limit_price:.2f}, equity=${sizing_equity:.0f})"
                        )
                        continue

                    order_id = us_broker.place_buy_order(
                        symbol=symbol,
                        qty=qty,
                        limit_price=limit_price,
                        stop_loss_price=sl,
                        take_profit_price=tp,
                        atr=atr,
                    )
                    if not order_id:
                        logger.warning(
                            f"[US] {symbol}: BUY order failed — "
                            f"{getattr(us_broker, 'last_error', 'unknown')}"
                        )
                    if order_id:
                        risk_mgr.register_trade(symbol, limit_price, sl, atr)
                        trade_journal.record_entry(
                            "US", symbol, qty, limit_price,
                            stop_price=sl, take_profit=tp,
                            reason="signal_buy", strategy=strategy.name,
                            meta={"atr": atr, "order_id": order_id},
                        )
                        alerts.trade_alert("US", "BUY", symbol, f"qty={qty} @${limit_price:.2f}")
                        current_positions[symbol] = {"qty": qty}

                elif signal == "SELL" and symbol in current_positions:
                    pos = current_positions[symbol]
                    px = pos.get("current_price") or pos.get("avg_entry_price")
                    if us_broker.close_position(symbol):
                        trade_journal.record_exit("US", symbol, float(px), reason="signal_sell")
                        risk_mgr.clear_trade(symbol)
                        alerts.trade_alert("US", "SELL", symbol, f"@${px:.2f}")

            bot_state.publish_signals("US", signal_rows)
            bot_state.mark_healthy("US")
            log_us_portfolio_summary(us_broker, mode_label)

            elapsed = time.time() - loop_start
            sleep_time = max(0, config.US_LOOP_INTERVAL_SEC - elapsed)
            logger.info(f"[{mode_label} COMPLETE] Cycle done ({elapsed:.1f}s). Next in {sleep_time:.0f}s.\n")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"[US ERROR] {e}", exc_info=True)
            bot_state.mark_cycle("US", error=str(e))
            alerts.health_alert(f"US loop error: {e}")
            time.sleep(config.US_LOOP_INTERVAL_SEC)


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
        f"OR={getattr(config,'OR_MINUTES',15)}m | cutoff={config.ENTRY_CUTOFF} | "
        f"squareoff={config.SQUAREOFF_TIME} | capital_cap={getattr(config,'INDIA_CAPITAL_CAP',0):,.0f}"
    )
    logger.info(
        f"   Risk/trade={config.RISK_PER_TRADE:.2%} | ATR_SL={config.ATR_STOP_MULT}x | "
        f"TP={config.TAKE_PROFIT_R}R | MaxOpen={config.MAX_OPEN_POSITIONS} | "
        f"DD={config.DAILY_DRAWDOWN_LIMIT:.2%}"
    )
    logger.info("=" * 70)

    if not config.INDIA_ENABLED and not config.US_ENABLED:
        logger.critical(
            "No markets configured. Add Dhan (DHAN_*) or Angel (ANGEL_*) keys to .env"
        )
        return

    if config.LIVE_CONFIRMED:
        logger.critical("!!! INDIA REAL MONEY MIS MODE ARMED !!!")
    elif config.INDIA_PAPER:
        logger.info("India PAPER SIM on — live NSE data, fake INR (safe for testing)")

    if config.US_ENABLED:
        if config.US_LIVE_CONFIRMED:
            logger.critical("!!! US REAL MONEY MODE ARMED !!!")
        elif config.US_PAPER:
            logger.info("US PAPER SIM on — live US data, fake USD")

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

    # US Global Stocks live feed (separate GlobalStocksFeed socket)
    if config.US_ENABLED and getattr(config, "DHAN_US_LIVE_WEBSOCKET", False):
        try:
            from dhan_us_live_feed import get_us_live_feed_manager
            from us_instruments import load_us_scrip_master

            load_us_scrip_master()
            us_feed = get_us_live_feed_manager()
            # Prefer credentials already refreshed by India Dhan login
            if config.DHAN_CONFIGURED:
                try:
                    from india_client import get_shared_india_broker

                    india_auth = get_shared_india_broker(auto_login=False)
                    if getattr(india_auth, "access_token", None):
                        us_feed.update_credentials(
                            india_auth.client_id,
                            india_auth.access_token,
                            reconnect=False,
                        )
                except Exception:
                    pass
            n_us = us_feed.subscribe_universe(config.US_STOCK_UNIVERSE)
            logger.info(
                f"[US FEED] Subscribed {n_us}/{len(config.US_STOCK_UNIVERSE)} "
                f"US symbols via GlobalStocksFeed | enabled={us_feed.enabled} "
                f"connected={us_feed.is_connected()} mode={us_feed.status_summary().get('mode')}"
            )
        except Exception as ufe:
            logger.warning(f"[US FEED] Universe subscription warning: {ufe}")

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

    # --- US loop ---
    if config.US_ENABLED:
        us_rs = RelativeStrengthFilter() if config.USE_RELATIVE_STRENGTH else None
        us_strategy = create_strategy("US", rs_filter=us_rs)
        us_risk = RiskManager(market="US")

        us_thread = threading.Thread(
            target=run_us_loop,
            args=(us_strategy, us_risk, us_rs),
            daemon=True,
            name="USMarketLoop",
        )
        us_thread.start()
        logger.info(
            "[US] Background loop started (dhan_global + paper/live)"
        )
    else:
        logger.warning(
            "[US] Disabled — missing Dhan credentials or US_PAPER/US_LIVE not set"
        )

    logger.info(
        "[MAIN] Process staying alive 24/7 for dashboard + market loops | "
        "Dead zone ~01:30–09:00 IST weekdays; weekends/holidays closed — NOT 24x7 trading"
    )
    while True:
        time.sleep(60)


if __name__ == "__main__":
    setup_logging()
    logger.info("Starting AI Quant Bot (24/7 dual market)...")
    try:
        run_bot()
    except KeyboardInterrupt:
        logger.info("\nBot stopped by user (Ctrl+C).")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)

"""
currency_broker.py — Real NSE Currency FX Broker Client
======================================================
Wraps DhanHQ API for trading NSE Currency derivatives (USDINR)
during currency market hours (Mon-Fri 09:00 AM – 05:00 PM IST).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import config
from currency_paper import CurrencyPaperPortfolio
from dhan_broker import get_shared_dhan_broker
from india_fno_instruments import is_placeholder_security_id, resolve_instrument_info
from price_guards import mark_vs_entry_sane, require_tradeable_quote
from risk_manager import RiskManager

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except ImportError:
    import pytz  # type: ignore

    IST = pytz.timezone("Asia/Kolkata")

try:
    from dhanhq import dhanhq
except ImportError:
    dhanhq = None  # type: ignore

logger = logging.getLogger(__name__)

_shared_currency_broker: "CurrencyBroker | None" = None
_currency_lock = threading.Lock()
ENTRY_COOLDOWN_SEC = 3600.0


def get_shared_currency_broker() -> "CurrencyBroker":
    global _shared_currency_broker
    with _currency_lock:
        if _shared_currency_broker is None:
            _shared_currency_broker = CurrencyBroker()
        return _shared_currency_broker


class CurrencyBroker:
    def __init__(self):
        self.dhan_broker = get_shared_dhan_broker(auto_login=True)
        self.paper = CurrencyPaperPortfolio() if config.CURRENCY_PAPER else None
        self.risk_mgr = RiskManager(market="CURRENCY")
        self.risk_mgr.max_open_positions = 1
        self._cooldowns: dict[str, float] = {}
        mode = "PAPER SIM (live USDINR quotes, fake INR)" if self.paper else "LIVE CURRENCY REAL MONEY"
        logger.info(f"[CURRENCY] CurrencyBroker mode: {mode}")

    @staticmethod
    def is_currency_market_open() -> bool:
        """NSE Currency session Mon-Fri 09:00 - 17:00 IST."""
        now_ist = datetime.now(IST)
        if now_ist.weekday() >= 5:
            return False
        open_time = now_ist.replace(hour=9, minute=0, second=0, microsecond=0)
        close_time = now_ist.replace(hour=17, minute=0, second=0, microsecond=0)
        return open_time <= now_ist <= close_time

    def place_buy_order(
        self,
        symbol: str,
        qty: int,
        price: float,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
    ) -> str | None:
        if qty <= 0 or price <= 0:
            return None
        if self.risk_mgr.is_kill_switch_active:
            logger.warning("[CURRENCY] Kill switch active — block")
            return None

        quote = self.dhan_broker.get_latest_quote(symbol)
        live_px, qerr = require_tradeable_quote(symbol, quote, segment="FX")
        if qerr:
            logger.error(f"[CURRENCY] Order blocked — {qerr}")
            return None
        if abs(live_px - price) / max(live_px, price) > 0.05:
            logger.warning(
                f"[CURRENCY] Repricing {symbol}: caller={price:.4f} live={live_px:.4f}"
            )
            price = live_px

        total_cost = qty * price
        if total_cost > config.CURRENCY_CAPITAL_CAP:
            logger.warning(
                f"[CURRENCY CAPITAL CAP] Order cost (Rs{total_cost:,.2f}) exceeds "
                f"segment cap (Rs{config.CURRENCY_CAPITAL_CAP:,.2f})"
            )
            return None

        last = self._cooldowns.get(symbol, 0.0)
        if time.time() - last < ENTRY_COOLDOWN_SEC:
            logger.info(f"[CURRENCY] Cooldown active for {symbol} — skip")
            return None

        positions = self.get_open_positions()
        if not self.risk_mgr.can_open_position(symbol, positions):
            logger.warning(f"[CURRENCY RISK] Cannot open position in {symbol}")
            return None

        if self.paper is not None:
            oid = self.paper.buy(symbol, qty, price, stop_loss=stop_loss, take_profit=take_profit)
            if oid and oid != "REJECTED_FUNDS":
                self._cooldowns[symbol] = time.time()
                return oid
            return None

        if not config.CURRENCY_LIVE_CONFIRMED:
            logger.critical("[CURRENCY] Live order blocked: CURRENCY_LIVE_CONFIRMED is False.")
            return None

        sec_info = resolve_instrument_info(symbol, exchange_segment="NSE_CURRENCY")
        sec_id = sec_info.get("security_id")
        if is_placeholder_security_id(sec_id) or not self.dhan_broker.dhan:
            logger.error(
                f"[CURRENCY] Order rejected: unresolved security_id for {symbol} "
                f"(ensure currency segment activated + scrip master fresh)"
            )
            return None

        try:
            margin = self.dhan_broker.get_margin_required(symbol, qty, price, "MARGIN")
            if margin > config.CURRENCY_CAPITAL_CAP:
                logger.warning(f"[CURRENCY] Margin Rs{margin:,.2f} exceeds cap — block")
                return None
        except Exception:
            pass

        logger.warning(f"[LIVE CURRENCY] ORDER | {symbol} | Qty={qty} @ Rs{price:.4f}")
        try:
            raw = self.dhan_broker.dhan.place_order(
                security_id=str(sec_id),
                exchange_segment="NSE_CURRENCY",
                transaction_type=getattr(dhanhq, "BUY", "BUY"),
                quantity=int(qty),
                order_type=getattr(dhanhq, "LIMIT", "LIMIT"),
                product_type=getattr(dhanhq, "MARGIN", "MARGIN"),
                price=float(price),
                trigger_price=0,
                validity=getattr(dhanhq, "DAY", "DAY"),
                tag=f"BOT-FX-{symbol}"[:20],
            )
            oid = self.dhan_broker._extract_order_id(raw)
            if oid:
                self._cooldowns[symbol] = time.time()
            return oid
        except Exception as e:
            logger.error(f"[CURRENCY] Failed to place live order: {e}", exc_info=True)
            return None

    def check_exits(self) -> list[str]:
        closed = []
        if self.paper is None:
            return closed
        for sym, pos in list(self.paper.positions.items()):
            quote = self.dhan_broker.get_latest_quote(sym)
            mark, qerr = require_tradeable_quote(sym, quote, segment="FX")
            if qerr:
                logger.warning(f"[CURRENCY] Skip exit check {sym}: {qerr}")
                continue
            entry = float(pos.get("avg_price") or 0)
            sane, serr = mark_vs_entry_sane(entry, mark)
            if not sane:
                logger.error(f"[CURRENCY] Refusing exit {sym}: {serr}")
                continue
            sl = float(pos.get("stop_loss") or 0)
            tp = float(pos.get("take_profit") or 0)
            reason = None
            if sl > 0 and mark <= sl:
                reason = "stop_loss"
            elif tp > 0 and mark >= tp:
                reason = "take_profit"
            if reason and hasattr(self.paper, "sell"):
                if self.paper.sell(sym, pos["qty"], mark):
                    closed.append(sym)
                    logger.info(f"[CURRENCY] Closed {sym} ({reason})")
        return closed

    def get_open_positions(self) -> dict:
        if self.paper is not None:
            marks = {}
            for k, pos in self.paper.positions.items():
                marks[k] = pos["avg_price"]
            return self.paper.get_open_positions(marks)
        return self.dhan_broker.get_open_positions()

    def capital_utilization(self) -> dict:
        positions = self.get_open_positions()
        used = sum(float(p.get("market_value") or 0) for p in positions.values())
        cap = float(config.CURRENCY_CAPITAL_CAP)
        return {
            "used": used,
            "cap": cap,
            "utilization_pct": round((used / cap) * 100, 2) if cap else 0,
            "cash": float(self.paper.cash) if self.paper else None,
            "positions": len(positions),
            "kill_switch": self.risk_mgr.is_kill_switch_active,
        }

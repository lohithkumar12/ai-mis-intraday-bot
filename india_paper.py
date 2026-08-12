"""
india_paper.py — Virtual INR Portfolio (Angel One has no paper account)
=======================================================================
Uses LIVE Angel One candles + LTP for marks, but never places broker orders.
State persists in india_paper_portfolio.json so Render restarts keep history.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_default_portfolio = Path(__file__).resolve().parent / "india_paper_portfolio.json"
PORTFOLIO_PATH = Path(
    os.getenv("INDIA_PAPER_PORTFOLIO_PATH", str(_default_portfolio))
).expanduser().resolve()
_lock = threading.Lock()


class IndiaPaperPortfolio:
    """Fake INR account marked to live NSE prices."""

    def __init__(self, starting_cash: float | None = None):
        self.starting_cash = (
            starting_cash
            if starting_cash is not None
            else config.INDIA_PAPER_STARTING_CASH
        )
        self.cash: float = self.starting_cash
        self.positions: dict[str, dict] = {}  # symbol -> {qty, avg_entry_price}
        self.realized_pl: float = 0.0
        self.start_of_day_equity: float | None = None
        self.start_of_day_date: str | None = None  # IST calendar date YYYY-MM-DD
        self._load()

    @staticmethod
    def _ist_today() -> str:
        try:
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
        except ImportError:
            import pytz  # type: ignore
            IST = pytz.timezone("Asia/Kolkata")
        return datetime.now(IST).date().isoformat()

    def _load(self):
        if not PORTFOLIO_PATH.exists():
            self._save()
            logger.info(
                f"India PAPER portfolio created | Cash=Rs {self.cash:,.2f}"
            )
            return
        try:
            data = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
            self.cash = float(data.get("cash", self.starting_cash))
            self.positions = data.get("positions", {}) or {}
            self.realized_pl = float(data.get("realized_pl", 0))
            self.start_of_day_equity = data.get("start_of_day_equity")
            self.start_of_day_date = data.get("start_of_day_date")
            logger.info(
                f"India PAPER portfolio loaded | Cash=Rs {self.cash:,.2f} | "
                f"Positions={list(self.positions.keys())}"
            )
        except Exception as e:
            logger.error(f"Failed to load paper portfolio: {e}")

    def _save(self):
        payload = {
            "cash": self.cash,
            "positions": self.positions,
            "realized_pl": self.realized_pl,
            "start_of_day_equity": self.start_of_day_equity,
            "start_of_day_date": self.start_of_day_date,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
        PORTFOLIO_PATH.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

    def reset(self, starting_cash: float | None = None):
        with _lock:
            self.cash = starting_cash if starting_cash is not None else self.starting_cash
            self.positions = {}
            self.realized_pl = 0.0
            self.start_of_day_equity = None
            self.start_of_day_date = None
            self._save()
            logger.warning(f"India PAPER portfolio RESET | Cash=Rs {self.cash:,.2f}")

    def get_account_info(self, mark_prices: dict[str, float] | None = None) -> dict:
        marks = mark_prices or {}
        equity = self.cash
        for symbol, pos in self.positions.items():
            # Prefer live mark; fall back to entry (never invent a fake LTP for equity)
            px = float(marks.get(symbol) or pos.get("avg_entry_price", 0) or 0)
            equity += pos["qty"] * px

        today = self._ist_today()
        # Roll SOD each IST calendar day (legacy files without start_of_day_date also rebaseline)
        if (
            self.start_of_day_equity is None
            or self.start_of_day_date != today
        ):
            self.start_of_day_equity = equity
            self.start_of_day_date = today
            self._save()
            logger.info(
                f"[INDIA PAPER] Start-of-day equity set to Rs {equity:,.2f} ({today})"
            )

        return {
            "equity": round(equity, 2),
            "available_cash": round(self.cash, 2),
            "used_margin": 0.0,
            "net": round(equity, 2),
            "last_equity": round(float(self.start_of_day_equity), 2),
            "paper": True,
        }

    def get_open_positions(self, mark_prices: dict[str, float] | None = None) -> dict:
        marks = mark_prices or {}
        out = {}
        for symbol, pos in self.positions.items():
            qty = int(pos["qty"])
            entry = float(pos["avg_entry_price"])
            ltp = float(marks.get(symbol, entry))
            pnl = (ltp - entry) * qty
            pnl_pct = ((ltp - entry) / entry) if entry > 0 else 0.0
            out[symbol] = {
                "qty": qty,
                "avg_entry_price": entry,
                "current_price": ltp,
                "market_value": qty * ltp,
                "unrealized_pl": pnl,
                "unrealized_plpc": pnl_pct,
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "atr": pos.get("atr"),
                "peak_price": pos.get("peak_price", entry),
                "trading_symbol": f"{symbol}-EQ",
                "token": "",
                "source": "paper",
            }
        return out

    def update_position_meta(self, symbol: str, **fields):
        """Update SL/TP/peak on an open paper position."""
        with _lock:
            if symbol not in self.positions:
                return
            for k, v in fields.items():
                if v is not None:
                    self.positions[symbol][k] = v
            self._save()

    def buy(
        self,
        symbol: str,
        qty: int,
        price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        atr: float | None = None,
    ) -> str | None:
        if qty <= 0 or price <= 0:
            return None
        cost = qty * price
        with _lock:
            if cost > self.cash:
                logger.warning(
                    f"[PAPER] BUY blocked {symbol}: need Rs {cost:,.2f}, "
                    f"have Rs {self.cash:,.2f}"
                )
                return None

            self.cash -= cost
            if symbol in self.positions:
                old = self.positions[symbol]
                new_qty = old["qty"] + qty
                new_avg = ((old["avg_entry_price"] * old["qty"]) + cost) / new_qty
                self.positions[symbol] = {
                    "qty": new_qty,
                    "avg_entry_price": round(new_avg, 4),
                    "stop_loss": stop_loss if stop_loss is not None else old.get("stop_loss"),
                    "take_profit": take_profit if take_profit is not None else old.get("take_profit"),
                    "atr": atr if atr is not None else old.get("atr"),
                    "peak_price": max(float(old.get("peak_price") or 0), price),
                }
            else:
                self.positions[symbol] = {
                    "qty": qty,
                    "avg_entry_price": round(price, 4),
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "atr": atr,
                    "peak_price": price,
                }

            order_id = f"PAPER-{uuid.uuid4().hex[:10].upper()}"
            self._save()
            logger.warning(
                f"[PAPER BUY] {symbol} | Qty={qty} | Price={price:.2f} | "
                f"SL={stop_loss} TP={take_profit} | "
                f"Cash left=Rs {self.cash:,.2f} | ID={order_id}"
            )
            return order_id

    def sell(self, symbol: str, qty: int, price: float) -> str | None:
        if qty <= 0 or price <= 0:
            return None
        with _lock:
            if symbol not in self.positions:
                logger.warning(f"[PAPER] SELL blocked — no position in {symbol}")
                return None

            held = self.positions[symbol]["qty"]
            if qty > held:
                qty = held

            entry = self.positions[symbol]["avg_entry_price"]
            proceeds = qty * price
            self.realized_pl += (price - entry) * qty
            self.cash += proceeds

            remaining = held - qty
            if remaining <= 0:
                del self.positions[symbol]
            else:
                self.positions[symbol]["qty"] = remaining

            order_id = f"PAPER-{uuid.uuid4().hex[:10].upper()}"
            self._save()
            logger.warning(
                f"[PAPER SELL] {symbol} | Qty={qty} | Price={price:.2f} | "
                f"Cash=Rs {self.cash:,.2f} | ID={order_id}"
            )
            return order_id

    def reset_day_equity(self, equity: float):
        self.start_of_day_equity = equity
        self._save()

    def check_intraday_squareoff(self, mark_prices: dict[str, float]) -> list[str]:
        """Automatically squares off MIS/INTRADAY positions at SQUAREOFF_TIME IST."""
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
        except ImportError:
            import pytz  # type: ignore
            IST = pytz.timezone("Asia/Kolkata")

        now_ist = datetime.now(IST)
        raw = getattr(config, "SQUAREOFF_TIME", "15:00") or "15:00"
        try:
            hh, mm = raw.split(":")[:2]
            cutoff = now_ist.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except Exception:
            cutoff = now_ist.replace(hour=15, minute=0, second=0, microsecond=0)
        closed = []

        product = (config.INDIA_PRODUCT_TYPE or "").strip().upper()
        if product in ("INTRADAY", "INTRA", "MIS") and now_ist >= cutoff:
            with _lock:
                for sym in list(self.positions.keys()):
                    pos = self.positions[sym]
                    mark = mark_prices.get(sym, pos.get("avg_entry_price", 0))
                    if mark > 0:
                        self.sell(sym, pos["qty"], mark)
                        closed.append(sym)
                        logger.warning(
                            f"[INTRADAY AUTO-SQUAREOFF] Closed {sym} @ {mark:.2f} before market close"
                        )
        return closed

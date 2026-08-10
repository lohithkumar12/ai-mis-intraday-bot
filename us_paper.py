"""
us_paper.py — Virtual USD Portfolio (Dhan Global Stocks has no paper account)
===============================================================================
Uses LIVE Dhan Global Stocks quotes for marks, but never places real orders.
State persists in us_paper_portfolio.json so restarts keep history.
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

_default_portfolio = Path(__file__).resolve().parent / "us_paper_portfolio.json"
PORTFOLIO_PATH = Path(
    os.getenv("US_PAPER_PORTFOLIO_PATH", str(_default_portfolio))
).expanduser().resolve()
_lock = threading.Lock()


class USPaperPortfolio:
    """Fake USD account marked to live US stock prices."""

    def __init__(self, starting_cash: float | None = None):
        self.starting_cash = (
            starting_cash
            if starting_cash is not None
            else config.US_PAPER_STARTING_CASH
        )
        self.cash: float = self.starting_cash
        self.positions: dict[str, dict] = {}  # symbol -> {qty, avg_entry_price, ...}
        self.realized_pl: float = 0.0
        self.start_of_day_equity: float | None = None
        self._load()

    def _load(self):
        if not PORTFOLIO_PATH.exists():
            self._save()
            logger.info(
                f"[US PAPER] Portfolio created | Cash=${self.cash:,.2f}"
            )
            return
        try:
            data = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
            self.cash = float(data.get("cash", self.starting_cash))
            self.positions = data.get("positions", {}) or {}
            self.realized_pl = float(data.get("realized_pl", 0))
            self.start_of_day_equity = data.get("start_of_day_equity")
            logger.info(
                f"[US PAPER] Portfolio loaded | Cash=${self.cash:,.2f} | "
                f"Positions={list(self.positions.keys())}"
            )
        except Exception as e:
            logger.error(f"[US PAPER] Failed to load portfolio: {e}")

    def _save(self):
        payload = {
            "cash": self.cash,
            "positions": self.positions,
            "realized_pl": self.realized_pl,
            "start_of_day_equity": self.start_of_day_equity,
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
            self._save()
            logger.warning(f"[US PAPER] Portfolio RESET | Cash=${self.cash:,.2f}")

    def get_account_info(self, mark_prices: dict[str, float] | None = None) -> dict:
        marks = mark_prices or {}
        equity = self.cash
        for symbol, pos in self.positions.items():
            px = float(marks.get(symbol, pos.get("avg_entry_price", 0)))
            equity += pos["qty"] * px

        if self.start_of_day_equity is None:
            self.start_of_day_equity = equity
            self._save()

        return {
            "equity": round(equity, 2),
            "available_cash": round(self.cash, 2),
            "buying_power": round(self.cash, 2),
            "used_margin": 0.0,
            "net": round(equity, 2),
            "last_equity": round(self.start_of_day_equity, 2),
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
                    f"[US PAPER] BUY blocked {symbol}: need ${cost:,.2f}, "
                    f"have ${self.cash:,.2f}"
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

            order_id = f"US-PAPER-{uuid.uuid4().hex[:10].upper()}"
            self._save()
            logger.warning(
                f"[US PAPER BUY] {symbol} | Qty={qty} | Price=${price:.2f} | "
                f"SL={stop_loss} TP={take_profit} | "
                f"Cash left=${self.cash:,.2f} | ID={order_id}"
            )
            return order_id

    def sell(self, symbol: str, qty: int, price: float) -> str | None:
        if qty <= 0 or price <= 0:
            return None
        with _lock:
            if symbol not in self.positions:
                logger.warning(f"[US PAPER] SELL blocked — no position in {symbol}")
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

            order_id = f"US-PAPER-{uuid.uuid4().hex[:10].upper()}"
            self._save()
            logger.warning(
                f"[US PAPER SELL] {symbol} | Qty={qty} | Price=${price:.2f} | "
                f"Cash=${self.cash:,.2f} | ID={order_id}"
            )
            return order_id

    def reset_day_equity(self, equity: float):
        self.start_of_day_equity = equity
        self._save()

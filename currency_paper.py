"""
currency_paper.py — Currency Derivatives Paper Portfolio Simulation
===================================================================
Virtual paper portfolio for NSE Currency FX (USDINR).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import config

logger = logging.getLogger(__name__)

_default = Path(__file__).resolve().parent / "currency_paper_portfolio.json"
PAPER_CURRENCY_FILE = Path(
    os.getenv("CURRENCY_PAPER_PORTFOLIO_PATH", str(_default))
).expanduser().resolve()


class CurrencyPaperPortfolio:
    def __init__(self, starting_cash: float = config.CURRENCY_PAPER_STARTING_CASH):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, dict] = {}
        self.trade_history: list[dict] = []
        self._load()

    def _load(self):
        if PAPER_CURRENCY_FILE.exists():
            try:
                with open(PAPER_CURRENCY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.cash = float(data.get("cash", self.starting_cash))
                    self.positions = data.get("positions", {})
                    self.trade_history = data.get("trade_history", [])
            except Exception as e:
                logger.error(f"Error loading Currency paper file: {e}")

    def _save(self):
        try:
            PAPER_CURRENCY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(PAPER_CURRENCY_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "cash": self.cash,
                    "positions": self.positions,
                    "trade_history": self.trade_history,
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving Currency paper file: {e}")

    def buy(self, symbol: str, qty: int, price: float, stop_loss: float = 0.0, take_profit: float = 0.0) -> str:
        cost = qty * price
        if self.cash < cost:
            logger.warning(f"Currency Paper BUY rejected: insufficient cash ({cost:.2f} > {self.cash:.2f})")
            return "REJECTED_FUNDS"

        self.cash -= cost
        if symbol in self.positions:
            pos = self.positions[symbol]
            new_qty = pos["qty"] + qty
            avg_p = ((pos["qty"] * pos["avg_price"]) + (qty * price)) / new_qty
            pos["qty"] = new_qty
            pos["avg_price"] = avg_p
        else:
            self.positions[symbol] = {
                "symbol": symbol,
                "qty": qty,
                "avg_price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        self._save()
        logger.info(f"[CURRENCY PAPER] BOUGHT {qty} {symbol} @ Rs{price:.4f}")
        return f"PAPER-FX-{symbol}"

    def close(self, symbol: str, current_price: float, reason: str = "SIGNAL") -> bool:
        pos = self.positions.get(symbol)
        if not pos:
            return False

        qty = pos["qty"]
        proceeds = qty * current_price
        pnl = proceeds - (qty * pos["avg_price"])
        self.cash += proceeds

        self.trade_history.append({
            "symbol": symbol,
            "qty": qty,
            "entry_price": pos["avg_price"],
            "exit_price": current_price,
            "pnl": pnl,
            "reason": reason,
        })
        del self.positions[symbol]
        self._save()
        logger.info(f"[CURRENCY PAPER] CLOSED {symbol} @ Rs{current_price:.4f} | P&L: Rs{pnl:,.2f} ({reason})")
        return True

    def sell(self, symbol: str, qty: int, price: float) -> bool:
        return self.close(symbol, price, reason="SELL")

    def get_open_positions(self, current_marks: dict[str, float]) -> dict:
        res = {}
        for sym, pos in self.positions.items():
            mark = current_marks.get(sym, pos["avg_price"])
            pnl = (mark - pos["avg_price"]) * pos["qty"]
            pnl_pct = ((mark - pos["avg_price"]) / pos["avg_price"]) if pos["avg_price"] > 0 else 0
            res[sym] = {
                "qty": pos["qty"],
                "avg_entry_price": pos["avg_price"],
                "current_price": mark,
                "market_value": pos["qty"] * mark,
                "unrealized_pl": pnl,
                "unrealized_plpc": pnl_pct,
            }
        return res

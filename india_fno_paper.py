"""
india_fno_paper.py — Virtual Portfolio Engine for India F&O
===========================================================
Simulates paper trades for NSE Futures and Options (Call/Put contracts,
lot sizes, premiums, margin utilization, and P&L).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import config

logger = logging.getLogger(__name__)

_default = Path(__file__).resolve().parent / "india_fno_paper_portfolio.json"
PAPER_FNO_FILE = Path(
    os.getenv("INDIA_FNO_PAPER_PORTFOLIO_PATH", str(_default))
).expanduser().resolve()


class IndiaFnoPaperPortfolio:
    def __init__(self, starting_cash: float = config.INDIA_FNO_PAPER_STARTING_CASH):
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, dict] = {}
        self.trade_history: list[dict] = []
        self._load()

    def _load(self):
        if PAPER_FNO_FILE.exists():
            try:
                with open(PAPER_FNO_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.cash = float(data.get("cash", self.starting_cash))
                    self.positions = data.get("positions", {})
                    self.trade_history = data.get("trade_history", [])
            except Exception as e:
                logger.error(f"Error loading India F&O paper file: {e}")

    def _save(self):
        try:
            PAPER_FNO_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(PAPER_FNO_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "cash": self.cash,
                    "positions": self.positions,
                    "trade_history": self.trade_history,
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving India F&O paper file: {e}")

    def reset(self, starting_cash: float | None = None):
        self.cash = starting_cash if starting_cash is not None else self.starting_cash
        self.positions = {}
        self.trade_history = []
        self._save()

    def buy_option(self, symbol: str, strike: float, option_type: str, lots: int, lot_size: int, premium: float, stop_loss: float = 0.0, take_profit: float = 0.0) -> str:
        total_qty = lots * lot_size
        cost = total_qty * premium
        if self.cash < cost:
            logger.warning(f"F&O Paper BUY rejected: insufficient cash (Needed {cost:.2f}, Have {self.cash:.2f})")
            return "REJECTED_FUNDS"

        contract_key = f"{symbol}-{int(strike)}-{option_type.upper()}"
        self.cash -= cost

        if contract_key in self.positions:
            existing = self.positions[contract_key]
            old_qty = existing["qty"]
            new_qty = old_qty + total_qty
            avg_price = ((old_qty * existing["avg_price"]) + (total_qty * premium)) / new_qty
            existing["qty"] = new_qty
            existing["avg_price"] = avg_price
        else:
            self.positions[contract_key] = {
                "symbol": symbol,
                "contract_key": contract_key,
                "strike": strike,
                "option_type": option_type.upper(),
                "lots": lots,
                "lot_size": lot_size,
                "qty": total_qty,
                "avg_price": premium,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }

        self._save()
        logger.info(f"[PAPER F&O] BOUGHT {total_qty} {contract_key} @ premium Rs{premium:.2f} (Cost Rs{cost:,.2f})")
        return f"PAPER-FNO-{contract_key}"

    def close_position(self, contract_key: str, current_premium: float, reason: str = "SIGNAL") -> bool:
        pos = self.positions.get(contract_key)
        if not pos:
            return False

        qty = pos["qty"]
        proceeds = qty * current_premium
        pnl = proceeds - (qty * pos["avg_price"])
        self.cash += proceeds

        self.trade_history.append({
            "contract": contract_key,
            "qty": qty,
            "entry_premium": pos["avg_price"],
            "exit_premium": current_premium,
            "pnl": pnl,
            "reason": reason,
        })
        del self.positions[contract_key]
        self._save()
        logger.info(f"[PAPER F&O] CLOSED {contract_key} @ premium Rs{current_premium:.2f} | P&L: Rs{pnl:,.2f} ({reason})")
        return True

    def get_open_positions(self, current_marks: dict[str, float]) -> dict:
        result = {}
        for key, pos in self.positions.items():
            mark = current_marks.get(key, pos["avg_price"])
            pnl = (mark - pos["avg_price"]) * pos["qty"]
            pnl_pct = ((mark - pos["avg_price"]) / pos["avg_price"]) if pos["avg_price"] > 0 else 0
            result[key] = {
                "qty": pos["qty"],
                "avg_entry_price": pos["avg_price"],
                "current_price": mark,
                "market_value": pos["qty"] * mark,
                "unrealized_pl": pnl,
                "unrealized_plpc": pnl_pct,
                "contract_key": key,
            }
        return result

"""
risk_manager.py — Risk Management Module
==========================================
Capital preservation rules:

  1. Risk-per-trade sizing — shares from (equity × RISK_PER_TRADE) / stop_distance
  2. ATR-based stop-loss   — default 2.0× ATR (fallback: STOP_LOSS_PCT)
  3. Take-profit           — TAKE_PROFIT_R × risk (R-multiple) + trailing ATR
  4. Max open positions    — book-wide cap
  5. Correlation cluster   — cap same-theme pile-ons
  6. Daily drawdown kill-switch — reset only on new trading day
  7. Session window        — skip first/last N minutes unless configured
  8. Duplicate prevention  — one position per symbol
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
import bot_state

logger = logging.getLogger(__name__)


def _round_px(market: str, price: float, mode: str = "nearest") -> float:
    """India equity → NSE tick; other markets keep 2dp."""
    if market.upper() == "INDIA":
        try:
            from nse_tick import round_to_nse_tick

            return float(round_to_nse_tick(price, mode=mode))  # type: ignore[arg-type]
        except Exception:
            return round(float(price), 2)
    return round(float(price), 2)


class RiskManager:
    def __init__(self, market: str = "US"):
        self.market = market.upper()
        self.risk_per_trade = config.RISK_PER_TRADE
        self.max_position_pct = config.MAX_POSITION_PCT
        self.atr_stop_mult = config.ATR_STOP_MULT
        self.atr_trail_mult = config.ATR_TRAIL_MULT
        self.take_profit_r = config.TAKE_PROFIT_R
        self.stop_loss_pct = config.STOP_LOSS_PCT
        self.take_profit_pct = config.TAKE_PROFIT_PCT
        self.daily_drawdown_limit = config.DAILY_DRAWDOWN_LIMIT
        self.max_open_positions = config.MAX_OPEN_POSITIONS
        self.max_cluster_positions = config.MAX_CLUSTER_POSITIONS

        # Track high-water marks for trailing stops: symbol -> peak price since entry
        self._trail_peaks: dict[str, float] = {}
        # Track entry + initial stop for R calc: symbol -> {entry, stop, atr, ...}
        self._trade_meta: dict[str, dict] = {}
        # Pending entry risk (orders submitted, not yet filled/failed): symbol -> ₹ risk
        self._pending_risk: dict[str, float] = {}
        # MIS margin booked per open symbol (₹)
        self._margin_by_symbol: dict[str, float] = {}
        self._entry_lock = threading.RLock()

        clusters = (
            config.INDIA_CORRELATION_CLUSTERS
            if self.market == "INDIA"
            else config.US_CORRELATION_CLUSTERS
        )
        self._clusters = clusters

        logger.info(
            f"RiskManager[{self.market}] — "
            f"Risk/trade={self.risk_per_trade:.2%}, "
            f"MaxPos%={self.max_position_pct:.0%}, "
            f"ATR_SL={self.atr_stop_mult}x, "
            f"TP={self.take_profit_r}R, "
            f"MaxOpen={self.max_open_positions}, "
            f"ClusterCap={self.max_cluster_positions}, "
            f"DailyDD={self.daily_drawdown_limit:.0%}"
        )

    @contextmanager
    def entry_gate(self):
        """Thread-safe gate so Core and Scout cannot both pass risk checks at once."""
        self._entry_lock.acquire()
        try:
            yield
        finally:
            self._entry_lock.release()

    def state_path(self) -> Path:
        journal = Path(str(getattr(config, "TRADE_JOURNAL_PATH", "trade_journal.db")))
        return journal.expanduser().resolve().parent / f"{self.market.lower()}_trade_state.json"

    # -----------------------------------------------------------------------
    # India capital sleeve (INDIA_CAPITAL_CAP)
    # -----------------------------------------------------------------------
    def capital_cap(self) -> float:
        """Return INDIA_CAPITAL_CAP when this is the India book; else 0 (no sleeve)."""
        if self.market != "INDIA":
            return 0.0
        return float(getattr(config, "INDIA_CAPITAL_CAP", 0) or 0)

    def effective_equity(self, equity: float) -> float:
        """
        Sizing / risk base: min(equity, INDIA_CAPITAL_CAP) when cap > 0 (India only).
        """
        eq = float(equity or 0)
        cap = self.capital_cap()
        if cap > 0:
            return min(eq, cap)
        return eq

    def drawdown_vs_cap(
        self, current_equity: float, start_of_day_equity: float
    ) -> float:
        """
        Daily loss as a fraction of the MIS sleeve base.
        base = min(SOD, INDIA_CAPITAL_CAP) when cap > 0, else SOD.
        So a ₹5k loss on a ₹3L account with ₹1.5L cap ≈ 3.33% sleeve DD.
        """
        sod = float(start_of_day_equity or 0)
        cur = float(current_equity or 0)
        if sod <= 0:
            return 1.0
        loss = sod - cur
        base = self.effective_equity(sod)
        if base <= 0:
            return 1.0
        return loss / base

    # -----------------------------------------------------------------------
    # Position Sizing (risk-to-stop)
    # -----------------------------------------------------------------------
    def calculate_position_size(
        self,
        equity: float,
        price: float,
        stop_distance: float | None = None,
        *,
        symbol: str = "",
        available_cash: float | None = None,
    ) -> int:
        """
        qty = min(shares_risk, max_by_pct, MAX_SHARES_PER_ORDER)
        equity = min(equity, available_cash, INDIA_CAPITAL_CAP) on India.
        MAX_POSITION_PCT=1.00 means 100% of sleeve notional (MIS ~5x on margin).
        """
        eq = float(equity or 0)
        cash = float(available_cash) if available_cash is not None else eq
        cap = self.capital_cap() if self.market == "INDIA" else 0.0
        if cap > 0:
            equity = min(eq, cash, cap)
        elif available_cash is not None:
            equity = min(eq, cash)
        else:
            equity = self.effective_equity(eq) if self.market == "INDIA" else eq
        if price <= 0 or equity <= 0:
            logger.warning("Invalid equity/price for sizing — 0 shares.")
            return 0

        max_shares = int(getattr(config, "MAX_SHARES_PER_ORDER", 500) or 500)
        stop = float(stop_distance or 0)
        risk_budget = equity * self.risk_per_trade
        if stop > 0:
            shares_risk = risk_budget / stop
        else:
            shares_risk = (equity * self.max_position_pct) / price
        max_by_pct = (equity * self.max_position_pct) / price if price > 0 else 0.0
        qty = int(min(shares_risk, max_by_pct, max_shares))
        if qty < 0:
            qty = 0
        notional = qty * price
        logger.info(
            f"[POSITION SIZING] {symbol or '-'}: equity={equity}, "
            f"risk_budget={risk_budget:.2f}, stop_dist={stop:.2f}, "
            f"shares_risk={shares_risk}, max_by_pct={max_by_pct}, "
            f"max_shares={max_shares}, final_qty={qty}, "
            f"notional={notional:.2f}, est_margin={notional / 5.0:.2f}"
        )
        return qty

    def total_margin_used(self) -> float:
        return float(sum((self._margin_by_symbol or {}).values()))

    def add_margin(self, symbol: str, amount: float) -> None:
        sym = str(symbol or "").upper()
        if not sym:
            return
        if not hasattr(self, "_margin_by_symbol") or self._margin_by_symbol is None:
            self._margin_by_symbol = {}
        self._margin_by_symbol[sym] = max(0.0, float(amount or 0))

    def release_margin(self, symbol: str) -> None:
        if not hasattr(self, "_margin_by_symbol") or not self._margin_by_symbol:
            return
        self._margin_by_symbol.pop(str(symbol or "").upper(), None)

    def reset_margin_book(self) -> None:
        self._margin_by_symbol = {}

    def cluster_for(self, symbol: str) -> str | None:
        return self._cluster_for(symbol)

    def cluster_open_count(self, symbol: str, current_positions: dict) -> tuple[str | None, int]:
        cluster = self._cluster_for(symbol)
        if not cluster:
            return None, 0
        n = sum(
            1
            for s in (current_positions or {})
            if self._cluster_for(s) == cluster
        )
        return cluster, n

    # -----------------------------------------------------------------------
    # ATR Stop / Take-Profit / Trailing
    # -----------------------------------------------------------------------
    def get_stop_loss_price(
        self,
        entry_price: float,
        atr: float | None = None,
    ) -> float:
        if atr is not None and atr > 0:
            sl = entry_price - (self.atr_stop_mult * atr)
        else:
            sl = entry_price * (1 - self.stop_loss_pct)
        # Never above entry for longs
        if sl >= entry_price:
            sl = entry_price * (1 - self.stop_loss_pct)
        sl = _round_px(self.market, sl, mode="floor")
        return max(sl, 0.01)

    def get_take_profit_price(
        self,
        entry_price: float,
        stop_loss_price: float | None = None,
        atr: float | None = None,
    ) -> float:
        """R-multiple target from stop distance; fallback to TAKE_PROFIT_PCT."""
        if stop_loss_price is not None and stop_loss_price < entry_price:
            risk = entry_price - stop_loss_price
            tp = entry_price + (self.take_profit_r * risk)
        elif atr is not None and atr > 0:
            risk = self.atr_stop_mult * atr
            tp = entry_price + (self.take_profit_r * risk)
        else:
            tp = entry_price * (1 + self.take_profit_pct)
        return _round_px(self.market, tp, mode="nearest")

    def register_trade(
        self,
        symbol: str,
        entry_price: float,
        stop_loss_price: float,
        atr: float | None = None,
        *,
        qty: int | None = None,
        take_profit: float | None = None,
        source: str | None = None,
        strategy: str | None = None,
        sl_order_id: str | None = None,
        super_order_id: str | None = None,
        order_id: str | None = None,
    ):
        self._trade_meta[symbol] = {
            "entry": float(entry_price),
            "stop": float(stop_loss_price),
            "atr": atr,
            "initial_stop": float(stop_loss_price),
            "qty": int(qty) if qty is not None else None,
            "take_profit": float(take_profit) if take_profit is not None else None,
            "source": str(source or ""),
            "strategy": str(strategy or ""),
            "sl_order_id": sl_order_id,
            "super_order_id": super_order_id,
            "order_id": order_id,
            "peak": float(entry_price),
        }
        self._trail_peaks[symbol] = float(entry_price)
        self.clear_pending_risk(symbol)
        self.persist_state()

    def apply_partial_fill(
        self,
        symbol: str,
        filled_qty: int,
        average_price: float,
        *,
        planned_entry: float | None = None,
        planned_sl: float | None = None,
        planned_tp: float | None = None,
        atr: float | None = None,
        order_id: str | None = None,
        source: str | None = None,
        strategy: str | None = None,
        sl_order_id: str | None = None,
        super_order_id: str | None = None,
    ) -> dict:
        """
        Resize an open long to the broker's actual PART_TRADED fill.

        Stop and target keep the same % distance as the original plan
        (planned_sl/tp vs planned_entry, else existing meta, else config %).
        Trailing peak resets to the fill so the trail does not use intended size/price.
        """
        qty = int(filled_qty or 0)
        avg = float(average_price or 0)
        if qty <= 0 or avg <= 0:
            raise ValueError(
                f"{symbol}: apply_partial_fill needs filled_qty>0 and average_price>0 "
                f"(got qty={qty} avg={avg})"
            )

        prev = dict(self._trade_meta.get(symbol) or {})
        entry_ref = float(
            planned_entry
            if planned_entry and planned_entry > 0
            else (prev.get("entry") or 0)
        )
        sl_ref = float(
            planned_sl
            if planned_sl and planned_sl > 0
            else (prev.get("initial_stop") or prev.get("stop") or 0)
        )
        tp_ref = float(
            planned_tp
            if planned_tp and planned_tp > 0
            else (prev.get("take_profit") or 0)
        )

        if entry_ref > 0 and sl_ref > 0 and sl_ref < entry_ref:
            sl_pct = (entry_ref - sl_ref) / entry_ref
        else:
            sl_pct = float(self.stop_loss_pct)
        if entry_ref > 0 and tp_ref > entry_ref:
            tp_pct = (tp_ref - entry_ref) / entry_ref
        else:
            tp_pct = float(self.take_profit_pct)

        new_sl = _round_px(self.market, avg * (1.0 - sl_pct), mode="floor")
        if new_sl >= avg:
            new_sl = _round_px(self.market, avg * (1.0 - max(sl_pct, 1e-4)), mode="floor")
        new_sl = max(new_sl, 0.01)
        new_tp = _round_px(self.market, avg * (1.0 + tp_pct), mode="nearest")
        if new_tp <= avg:
            new_tp = _round_px(
                self.market, avg * (1.0 + max(tp_pct, 1e-4)), mode="nearest"
            )

        use_atr = atr if atr is not None else prev.get("atr")
        self.register_trade(
            symbol,
            avg,
            new_sl,
            use_atr,
            qty=qty,
            take_profit=new_tp,
            source=source or prev.get("source"),
            strategy=strategy or prev.get("strategy"),
            sl_order_id=sl_order_id if sl_order_id is not None else prev.get("sl_order_id"),
            super_order_id=super_order_id if super_order_id is not None else prev.get("super_order_id"),
            order_id=order_id if order_id is not None else prev.get("order_id"),
        )
        logger.warning(
            f"{symbol}: PART_TRADED risk resize qty={qty} avg={avg:.2f} "
            f"SL={new_sl:.2f} ({sl_pct:.2%}) TP={new_tp:.2f} ({tp_pct:.2%}) "
            f"(plan entry={entry_ref:.2f} sl={sl_ref:.2f} tp={tp_ref:.2f})"
        )
        return dict(self._trade_meta[symbol])

    def clear_trade(self, symbol: str):
        self._trade_meta.pop(symbol, None)
        self._trail_peaks.pop(symbol, None)
        self.clear_pending_risk(symbol)
        self.persist_state()

    def set_pending_risk(self, symbol: str, risk_rupees: float) -> None:
        sym = str(symbol).upper()
        with self._entry_lock:
            if risk_rupees > 0:
                self._pending_risk[sym] = float(risk_rupees)
            else:
                self._pending_risk.pop(sym, None)

    def clear_pending_risk(self, symbol: str) -> None:
        with self._entry_lock:
            self._pending_risk.pop(str(symbol).upper(), None)

    def portfolio_risk_budget_rupees(self, equity: float) -> float:
        """Max open+pending risk ≈ daily drawdown budget on the sizing sleeve."""
        base = self.effective_equity(equity) if self.market == "INDIA" else float(equity or 0)
        return max(0.0, float(base) * float(self.daily_drawdown_limit))

    def open_risk_rupees(self, positions: dict | None = None) -> float:
        total = 0.0
        # Prefer live meta; fall back to position marks
        meta_syms = set(self._trade_meta.keys())
        for symbol, meta in self._trade_meta.items():
            entry = float(meta.get("entry") or 0)
            stop = float(meta.get("stop") or meta.get("initial_stop") or 0)
            qty = int(meta.get("qty") or 0)
            if positions and symbol in positions:
                qty = int(positions[symbol].get("qty") or qty or 0)
            if entry <= 0 or qty <= 0:
                continue
            total += max(entry - stop, 0.0) * qty
        if positions:
            for symbol, pos in positions.items():
                if symbol in meta_syms:
                    continue
                entry = float(pos.get("avg_entry_price") or 0)
                qty = int(pos.get("qty") or 0)
                if entry <= 0 or qty <= 0:
                    continue
                stop = self.get_effective_stop(symbol, entry)
                total += max(entry - stop, 0.0) * qty
        return total

    def pending_risk_rupees(self) -> float:
        with self._entry_lock:
            return float(sum(self._pending_risk.values()))

    def can_add_trade_risk(
        self,
        equity: float,
        new_risk_rupees: float,
        positions: dict | None = None,
    ) -> tuple[bool, str]:
        """
        True if open risk + pending risk + new trade risk <= portfolio/daily budget.
        Must be called under entry_gate for Core/Scout atomicity.
        """
        budget = self.portfolio_risk_budget_rupees(equity)
        if budget <= 0:
            return True, ""
        open_r = self.open_risk_rupees(positions)
        pend_r = self.pending_risk_rupees()
        total = open_r + pend_r + max(0.0, float(new_risk_rupees))
        if total > budget + 1e-6:
            return (
                False,
                f"portfolio_risk ₹{total:,.0f} > budget ₹{budget:,.0f} "
                f"(open={open_r:,.0f} pending={pend_r:,.0f} new={new_risk_rupees:,.0f})",
            )
        return True, ""

    def persist_state(self) -> None:
        if self.market != "INDIA":
            return
        try:
            path = self.state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "trade_meta": self._trade_meta,
                "trail_peaks": self._trail_peaks,
  "saved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.debug(f"persist trade state skipped: {e}")

    def load_state(self) -> None:
        if self.market != "INDIA":
            return
        path = self.state_path()
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            meta = raw.get("trade_meta") or {}
            peaks = raw.get("trail_peaks") or {}
            if isinstance(meta, dict):
                self._trade_meta = {str(k): dict(v) for k, v in meta.items() if isinstance(v, dict)}
            if isinstance(peaks, dict):
                self._trail_peaks = {str(k): float(v) for k, v in peaks.items()}
            logger.info(
                f"RiskManager[{self.market}] restored meta for "
                f"{len(self._trade_meta)} symbols from {path}"
            )
        except Exception as e:
            logger.warning(f"load trade state failed: {e}")

    def reconcile_meta_with_broker(self, broker_positions: dict | None) -> None:
        """
        Keep meta only for symbols actually open at the broker.
        Do not invent positions from stale local state.
        """
        if broker_positions is None:
            return
        live = {str(s) for s in broker_positions.keys()}
        stale = [s for s in list(self._trade_meta.keys()) if s not in live]
        for s in stale:
            self._trade_meta.pop(s, None)
            self._trail_peaks.pop(s, None)
        for symbol, pos in broker_positions.items():
            if symbol in self._trade_meta:
                meta = self._trade_meta[symbol]
                meta["qty"] = int(pos.get("qty") or meta.get("qty") or 0)
                entry = float(pos.get("avg_entry_price") or 0)
                if entry > 0:
                    meta["entry"] = entry
                continue
            # Broker-open without meta: reconstruct conservative SL/TP from pct/ATR if present
            entry = float(pos.get("avg_entry_price") or 0)
            qty = int(pos.get("qty") or 0)
            if entry <= 0 or qty <= 0:
                continue
            atr = pos.get("atr")
            try:
                atr_f = float(atr) if atr is not None else None
            except (TypeError, ValueError):
                atr_f = None
            sl = self.get_stop_loss_price(entry, atr_f)
            tp = self.get_take_profit_price(entry, stop_loss_price=sl, atr=atr_f)
            self._trade_meta[symbol] = {
                "entry": entry,
                "stop": sl,
                "atr": atr_f,
                "initial_stop": sl,
                "qty": qty,
                "take_profit": tp,
                "source": "broker_reconcile",
                "strategy": "",
                "sl_order_id": None,
                "super_order_id": None,
                "order_id": None,
                "peak": entry,
            }
            self._trail_peaks[symbol] = entry
            logger.warning(
                f"{symbol}: reconstructed local SL/TP after restart "
                f"(entry={entry:.2f} sl={sl:.2f}) — verify broker stop still armed"
            )
        self.persist_state()

    def update_trailing_stop(
        self,
        symbol: str,
        current_price: float,
        atr: float | None = None,
    ) -> Optional[float]:
        """
        After price moves in favor, trail stop at peak − ATR_TRAIL_MULT × ATR.
        Returns the effective stop (max of initial SL and trail), or None if unknown.
        Never loosens below the current stored stop.
        """
        meta = self._trade_meta.get(symbol)
        if not meta:
            return None

        peak = max(self._trail_peaks.get(symbol, meta["entry"]), current_price)
        self._trail_peaks[symbol] = peak
        meta["peak"] = peak

        use_atr = atr if (atr and atr > 0) else meta.get("atr")
        initial_stop = meta["initial_stop"]
        prev_stop = float(meta.get("stop") or initial_stop)

        # Only start trailing once we've moved at least 1R in favor
        risk = meta["entry"] - initial_stop
        if risk <= 0 or (peak - meta["entry"]) < risk:
            return prev_stop

        if use_atr and use_atr > 0:
            trail = round(peak - (self.atr_trail_mult * use_atr), 2)
        else:
            trail = round(peak * (1 - self.stop_loss_pct), 2)

        # Never loosen vs initial OR vs already-ratcheted stop
        effective = max(initial_stop, prev_stop, trail)
        meta["stop"] = effective
        self.persist_state()
        return effective

    def get_effective_stop(self, symbol: str, entry_price: float, atr: float | None = None) -> float:
        meta = self._trade_meta.get(symbol)
        if meta:
            return float(meta.get("stop") or meta["initial_stop"])
        return self.get_stop_loss_price(entry_price, atr)

    # -----------------------------------------------------------------------
    # Session window (avoid open/close noise)
    # -----------------------------------------------------------------------
    def is_tradable_session(
        self,
        now: datetime | None = None,
        market_open_hm: tuple[int, int] = (9, 30),
        market_close_hm: tuple[int, int] = (16, 0),
    ) -> bool:
        """
        Returns False during first/last AVOID_* minutes unless ALLOW_OPEN_CLOSE_WINDOW.
        Also enforces ENTRY_CUTOFF (HH:MM) for MIS — no new entries after cutoff.
        `now` should already be in the market's local timezone.
        """
        if config.ALLOW_OPEN_CLOSE_WINDOW:
            # Still honor hard MIS entry cutoff even if avoid windows are disabled
            if now is None:
                now = datetime.now()
            if self._past_entry_cutoff(now):
                logger.info(f"[{self.market}] Skipping — past ENTRY_CUTOFF={config.ENTRY_CUTOFF}")
                return False
            return True

        if now is None:
            now = datetime.now()

        open_mins = market_open_hm[0] * 60 + market_open_hm[1]
        close_mins = market_close_hm[0] * 60 + market_close_hm[1]
        cur = now.hour * 60 + now.minute

        if cur < open_mins + config.AVOID_OPEN_MINUTES:
            logger.info(
                f"[{self.market}] Skipping — within first "
                f"{config.AVOID_OPEN_MINUTES}m of session"
            )
            return False
        if cur > close_mins - config.AVOID_CLOSE_MINUTES:
            logger.info(
                f"[{self.market}] Skipping — within last "
                f"{config.AVOID_CLOSE_MINUTES}m of session"
            )
            return False
        if self._past_entry_cutoff(now):
            logger.info(f"[{self.market}] Skipping — past ENTRY_CUTOFF={config.ENTRY_CUTOFF}")
            return False
        return True

    @staticmethod
    def _past_entry_cutoff(now: datetime) -> bool:
        raw = getattr(config, "ENTRY_CUTOFF", "") or ""
        if not raw or ":" not in raw:
            return False
        try:
            hh, mm = raw.split(":")[:2]
            cut = int(hh) * 60 + int(mm)
        except Exception:
            return False
        return (now.hour * 60 + now.minute) >= cut

    @staticmethod
    def squareoff_hm() -> tuple[int, int]:
        raw = getattr(config, "SQUAREOFF_TIME", "15:00") or "15:00"
        try:
            hh, mm = raw.split(":")[:2]
            return int(hh), int(mm)
        except Exception:
            return 15, 0

    def past_squareoff(self, now: datetime | None = None) -> bool:
        if now is None:
            now = datetime.now()
        hh, mm = self.squareoff_hm()
        return (now.hour * 60 + now.minute) >= (hh * 60 + mm)

    # -----------------------------------------------------------------------
    # Trade count / absolute loss / cost floor
    # -----------------------------------------------------------------------
    def passes_cost_floor(
        self,
        entry_price: float,
        take_profit_price: float,
        *,
        qty: int = 1,
    ) -> tuple[bool, str]:
        """
        Expected move to TP must cover round-trip fee estimate + min edge.
        Simple MIS cost guard — not a full fee model.
        """
        if not bool(getattr(config, "COST_FLOOR_ENABLED", True)):
            return True, ""
        entry = float(entry_price or 0)
        tp = float(take_profit_price or 0)
        if entry <= 0 or tp <= entry:
            return False, "cost_floor: invalid entry/tp"
        move_pct = (tp - entry) / entry
        rt = float(getattr(config, "COST_FLOOR_RT_PCT", 0.0005) or 0)
        edge_bps = float(getattr(config, "COST_FLOOR_MIN_EDGE_BPS", 5) or 0)
        need = rt + (edge_bps / 10_000.0)
        if move_pct + 1e-12 < need:
            return (
                False,
                f"cost_floor: TP move {move_pct:.3%} < need {need:.3%} "
                f"(rt={rt:.3%}+edge={edge_bps:.0f}bps)",
            )
        return True, ""

    def check_max_trades_per_day(self) -> tuple[bool, str]:
        """False when journal entries today >= MAX_TRADES_PER_DAY (0=unlimited)."""
        cap = int(getattr(config, "MAX_TRADES_PER_DAY", 0) or 0)
        if cap <= 0 or self.market != "INDIA":
            return True, ""
        try:
            import trade_journal

            n = int(trade_journal.count_entries_today("INDIA"))
        except Exception as e:
            logger.debug(f"max_trades count skip: {e}")
            return True, ""
        if n >= cap:
            return False, f"max_trades_per_day {n}>={cap}"
        return True, ""

    def check_max_daily_loss_inr(self, day_pl: float | None) -> bool:
        """
        Absolute ₹ hard stop. Returns True when limit breached (kill ON).
        MAX_DAILY_LOSS_INR=0 disables.
        """
        lim = float(getattr(config, "MAX_DAILY_LOSS_INR", 0) or 0)
        if lim <= 0 or day_pl is None:
            return False
        loss = max(0.0, -float(day_pl))
        if loss + 1e-9 >= lim:
            reason = f"max_daily_loss_inr ₹{loss:,.0f}>=₹{lim:,.0f}"
            bot_state.activate_kill_switch(self.market, reason)
            logger.critical(
                f"MAX DAILY LOSS INR KILL-SWITCH! loss=₹{loss:,.2f} limit=₹{lim:,.0f}"
            )
            return True
        return False

    # -----------------------------------------------------------------------
    # Position / cluster limits
    # -----------------------------------------------------------------------
    def _cluster_for(self, symbol: str) -> str | None:
        for name, members in self._clusters.items():
            if symbol in members:
                return name
        return None

    def is_position_allowed(
        self,
        symbol: str,
        current_positions: dict,
    ) -> bool:
        if symbol in current_positions:
            logger.info(
                f"{symbol}: Already holding "
                f"({current_positions[symbol].get('qty', '?')} shares) — skip."
            )
            return False

        if len(current_positions) >= self.max_open_positions:
            logger.info(
                f"{symbol}: Max open positions ({self.max_open_positions}) reached — skip."
            )
            return False

        cluster = self._cluster_for(symbol)
        if cluster:
            held_in_cluster = sum(
                1
                for s in current_positions
                if self._cluster_for(s) == cluster
            )
            if held_in_cluster >= self.max_cluster_positions:
                logger.info(
                    f"[CLUSTER BLOCK] {symbol}: sector {cluster} already has "
                    f"{held_in_cluster} positions"
                )
                return False

        return True

    def can_open_position(self, symbol: str, current_positions) -> bool:
        """Alias used by F&O / MCX / Currency brokers."""
        if self.is_kill_switch_active:
            logger.warning(f"[{self.market}] Kill switch active — block {symbol}")
            return False
        if isinstance(current_positions, int):
            # Legacy call shape: can_open_position(symbol, len(positions))
            if current_positions >= self.max_open_positions:
                logger.info(
                    f"{symbol}: Max open positions ({self.max_open_positions}) reached — skip."
                )
                return False
            return True
        return self.is_position_allowed(symbol, current_positions or {})

    # -----------------------------------------------------------------------
    # Daily Drawdown Kill-Switch
    # -----------------------------------------------------------------------
    def check_daily_drawdown(
        self,
        current_equity: float,
        start_of_day_equity: float,
        *,
        day_pl: float | None = None,
    ) -> bool:
        """
        Returns True when daily loss exceeds DAILY_DRAWDOWN_LIMIT (kill-switch ON).

        India MIS (INDIA_CAPITAL_CAP > 0): uses journal-style day_pl when provided
        so CNC holdings / margin equity swings do not false-trip the switch.
        """
        cap = self.capital_cap()
        use_mis_day_pl = (
            self.market == "INDIA" and cap > 0 and day_pl is not None
        )

        if use_mis_day_pl:
            loss = max(0.0, -float(day_pl))
            base = cap
            drawdown = (loss / base) if base > 0 else 0.0
            dd_note = (
                f"MIS day P&L ₹{float(day_pl):+,.2f} vs sleeve ₹{base:,.0f}"
            )
        else:
            if start_of_day_equity <= 0:
                logger.error(
                    "Start-of-day equity invalid — halting for safety."
                )
                bot_state.activate_kill_switch(self.market, "invalid_sod")
                return True

            if self.market == "INDIA" and cap > 0:
                drawdown = self.drawdown_vs_cap(current_equity, start_of_day_equity)
                base = self.effective_equity(start_of_day_equity)
                dd_note = f"vs sleeve base={base:,.0f}"
            else:
                drawdown = (start_of_day_equity - current_equity) / start_of_day_equity
                dd_note = "vs full SOD"

        if drawdown >= self.daily_drawdown_limit:
            reason = f"daily_drawdown {drawdown:.2%}"
            bot_state.activate_kill_switch(self.market, reason)
            logger.critical(
                f"DAILY DRAWDOWN KILL-SWITCH TRIGGERED! "
                f"Drawdown={drawdown:.2%} (Limit={self.daily_drawdown_limit:.0%}) "
                f"{dd_note}"
            )
            return True

        # Absolute ₹ hard stop (optional)
        if self.check_max_daily_loss_inr(day_pl):
            return True

        # Latch: once daily_drawdown / max_daily_loss trips, do NOT auto-clear on recovery.
        # Reset only on next trading day via reset_kill_switch().
        if bot_state.is_kill_switch_active(self.market):
            prev_reason = bot_state.kill_switch_reason(self.market)
            if (
                prev_reason.startswith("daily_drawdown")
                or prev_reason.startswith("max_daily_loss")
                or prev_reason == "invalid_sod"
            ):
                logger.warning(
                    f"[{self.market}] Kill switch still latched ({prev_reason}) — "
                    f"current DD {drawdown:.2%} ({dd_note})"
                )
                return True

        logger.info(
            f"Daily drawdown: {drawdown:.2%} "
            f"(Limit={self.daily_drawdown_limit:.0%}, {dd_note}) — OK"
        )
        return False

    @property
    def is_kill_switch_active(self) -> bool:
        return bot_state.is_kill_switch_active(self.market)

    def activate_kill_switch(self, reason: str = "manual"):
        bot_state.activate_kill_switch(self.market, reason)
        logger.critical(f"[{self.market}] Kill switch ACTIVATED ({reason})")

    def reset_kill_switch(self):
        """Reset only for a new trading day (caller should gate by date)."""
        bot_state.reset_kill_switch(self.market)
        logger.info(
            f"[{self.market}] Daily drawdown kill-switch reset for new trading day."
        )

    def open_risk_pct(self, equity: float, positions: dict) -> float:
        """Approximate open risk as sum of (entry−stop)×qty / equity."""
        if equity <= 0:
            return 0.0
        total_risk = 0.0
        for symbol, pos in positions.items():
            entry = float(pos.get("avg_entry_price") or 0)
            qty = int(pos.get("qty") or 0)
            if entry <= 0 or qty <= 0:
                continue
            stop = self.get_effective_stop(symbol, entry)
            risk_per_share = max(entry - stop, 0)
            total_risk += risk_per_share * qty
        return total_risk / equity

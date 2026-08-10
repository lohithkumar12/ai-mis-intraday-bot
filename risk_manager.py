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

import logging
from datetime import datetime
from typing import Optional

import config

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

        self._kill_switch_active: bool = False
        self._kill_switch_day = None  # date when kill switch tripped

        # Track high-water marks for trailing stops: symbol -> peak price since entry
        self._trail_peaks: dict[str, float] = {}
        # Track entry + initial stop for R calc: symbol -> {entry, stop, atr}
        self._trade_meta: dict[str, dict] = {}

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
    ) -> int:
        """
        Size by risk-per-trade when stop_distance is known:
          shares = floor((equity × RISK_PER_TRADE) / stop_distance)
        Also capped by MAX_POSITION_PCT of equity and MAX_SHARES_PER_ORDER.
        India: caller should pass effective_equity(...) so INDIA_CAPITAL_CAP applies.
        """
        equity = self.effective_equity(equity) if self.market == "INDIA" else float(equity)
        if price <= 0 or equity <= 0:
            logger.warning("Invalid equity/price for sizing — 0 shares.")
            return 0

        # Risk-based shares
        if stop_distance is not None and stop_distance > 0:
            risk_budget = equity * self.risk_per_trade
            shares_risk = int(risk_budget // stop_distance)
        else:
            # Fallback: % of equity (legacy)
            shares_risk = int((equity * self.max_position_pct) // price)

        # Hard dollar cap
        max_by_pct = int((equity * self.max_position_pct) // price)
        shares = min(shares_risk, max_by_pct) if max_by_pct > 0 else 0

        max_shares = getattr(config, "MAX_SHARES_PER_ORDER", 50)
        if shares > max_shares:
            logger.warning(
                f"Position size capped: {shares} → {max_shares} (MAX_SHARES_PER_ORDER)"
            )
            shares = max_shares

        logger.info(
            f"Sizing: equity={equity:,.2f} risk={self.risk_per_trade:.2%} "
            f"stop_dist={stop_distance if stop_distance else 'n/a'} "
            f"→ {shares} shares @ {price:.2f}"
        )
        return shares

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
    ):
        self._trade_meta[symbol] = {
            "entry": entry_price,
            "stop": stop_loss_price,
            "atr": atr,
            "initial_stop": stop_loss_price,
        }
        self._trail_peaks[symbol] = entry_price

    def clear_trade(self, symbol: str):
        self._trade_meta.pop(symbol, None)
        self._trail_peaks.pop(symbol, None)

    def update_trailing_stop(
        self,
        symbol: str,
        current_price: float,
        atr: float | None = None,
    ) -> Optional[float]:
        """
        After price moves in favor, trail stop at peak − ATR_TRAIL_MULT × ATR.
        Returns the effective stop (max of initial SL and trail), or None if unknown.
        """
        meta = self._trade_meta.get(symbol)
        if not meta:
            return None

        peak = max(self._trail_peaks.get(symbol, meta["entry"]), current_price)
        self._trail_peaks[symbol] = peak

        use_atr = atr if (atr and atr > 0) else meta.get("atr")
        initial_stop = meta["initial_stop"]

        # Only start trailing once we've moved at least 1R in favor
        risk = meta["entry"] - initial_stop
        if risk <= 0 or (peak - meta["entry"]) < risk:
            return initial_stop

        if use_atr and use_atr > 0:
            trail = round(peak - (self.atr_trail_mult * use_atr), 2)
        else:
            trail = round(peak * (1 - self.stop_loss_pct), 2)

        effective = max(initial_stop, trail)
        meta["stop"] = effective
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
        raw = getattr(config, "SQUAREOFF_TIME", "15:10") or "15:10"
        try:
            hh, mm = raw.split(":")[:2]
            return int(hh), int(mm)
        except Exception:
            return 15, 10

    def past_squareoff(self, now: datetime | None = None) -> bool:
        if now is None:
            now = datetime.now()
        hh, mm = self.squareoff_hm()
        return (now.hour * 60 + now.minute) >= (hh * 60 + mm)

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
                    f"{symbol}: Cluster '{cluster}' already has "
                    f"{held_in_cluster} positions (cap={self.max_cluster_positions}) — skip."
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
        self, current_equity: float, start_of_day_equity: float
    ) -> bool:
        if start_of_day_equity <= 0:
            logger.error(
                "Start-of-day equity invalid — halting for safety."
            )
            return True

        if self.market == "INDIA" and self.capital_cap() > 0:
            drawdown = self.drawdown_vs_cap(current_equity, start_of_day_equity)
            base = self.effective_equity(start_of_day_equity)
            dd_note = f"vs sleeve base={base:,.0f}"
        else:
            drawdown = (start_of_day_equity - current_equity) / start_of_day_equity
            dd_note = "vs full SOD"

        if drawdown >= self.daily_drawdown_limit:
            self._kill_switch_active = True
            self._kill_switch_day = datetime.now().date()
            logger.critical(
                f"DAILY DRAWDOWN KILL-SWITCH TRIGGERED! "
                f"Drawdown={drawdown:.2%} (Limit={self.daily_drawdown_limit:.0%}) "
                f"{dd_note} | SOD={start_of_day_equity:,.2f} → Current={current_equity:,.2f}"
            )
            return True

        logger.info(
            f"Daily drawdown: {drawdown:.2%} "
            f"(Limit={self.daily_drawdown_limit:.0%}, {dd_note}) — OK"
        )
        return False

    @property
    def is_kill_switch_active(self) -> bool:
        return bool(self._kill_switch_active)

    def activate_kill_switch(self, reason: str = "manual"):
        self._kill_switch_active = True
        self._kill_switch_day = datetime.now().date()
        logger.critical(f"[{self.market}] Kill switch ACTIVATED ({reason})")

    def reset_kill_switch(self):
        """Reset only for a new trading day (caller should gate by date)."""
        self._kill_switch_active = False
        self._kill_switch_day = None
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

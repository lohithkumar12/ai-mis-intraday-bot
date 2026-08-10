"""
fno_strategy.py — Defined-Risk Options Strategy Engine
=====================================================
Generates option signals for paper/live. Paper mode uses looser RSI/ADX
gates so the expansion book can actually take trades during sessions.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import config

logger = logging.getLogger(__name__)

# Live stays strict; paper is relaxed for testing
SIGNAL_COOLDOWN_LIVE_SEC = 1800.0
SIGNAL_COOLDOWN_PAPER_SEC = 600.0


class FnoStrategy:
    def __init__(self, strategy_type: str = "directional_options"):
        self.strategy_type = strategy_type
        self._last_signal_time: dict[str, float] = {}
        self.last_skip_reason: str = ""
        logger.info(f"[FNO] FnoStrategy initialized: {self.strategy_type}")

    def _paper(self) -> bool:
        return bool(getattr(config, "INDIA_FNO_PAPER", True)) and not getattr(
            config, "INDIA_FNO_LIVE_CONFIRMED", False
        )

    def generate_signal(
        self,
        symbol: str,
        spot_price: float,
        rsi: float = 50.0,
        adx: float = 25.0,
        sma_fast: float = 0.0,
        sma_slow: float = 0.0,
        atr: float = 0.0,
    ) -> dict[str, Any] | None:
        self.last_skip_reason = ""
        if spot_price <= 0:
            self.last_skip_reason = "no spot"
            return None

        if self.strategy_type not in ("directional_options", "directional", ""):
            self.last_skip_reason = f"strategy {self.strategy_type} gated off"
            return None

        cooldown = SIGNAL_COOLDOWN_PAPER_SEC if self._paper() else SIGNAL_COOLDOWN_LIVE_SEC
        last = self._last_signal_time.get(symbol, 0.0)
        if time.time() - last < cooldown:
            self.last_skip_reason = "cooldown"
            return None

        # Paper: ADX>12, CE when RSI<48, PE when RSI>55
        # Live: ADX>20, CE RSI<38, PE RSI>62
        if self._paper():
            adx_min, rsi_ce, rsi_pe = 12.0, 48.0, 55.0
        else:
            adx_min, rsi_ce, rsi_pe = 20.0, 38.0, 62.0

        if adx < adx_min:
            self.last_skip_reason = f"ADX {adx:.1f}<{adx_min}"
            return None

        signal = None
        if (sma_fast > sma_slow or spot_price > sma_slow) and (rsi < rsi_ce):
            sl_dist = (atr * 1.5) if atr > 0 else (spot_price * 0.015)
            signal = {
                "action": "BUY_CALL",
                "symbol": symbol,
                "spot_price": spot_price,
                "option_type": "CE",
                "stop_loss_dist": round(sl_dist, 2),
                "reason": f"Bullish pullback (RSI={rsi:.1f}, ADX={adx:.1f})",
            }
        elif (sma_fast < sma_slow or spot_price < sma_slow) and (rsi > rsi_pe):
            sl_dist = (atr * 1.5) if atr > 0 else (spot_price * 0.015)
            signal = {
                "action": "BUY_PUT",
                "symbol": symbol,
                "spot_price": spot_price,
                "option_type": "PE",
                "stop_loss_dist": round(sl_dist, 2),
                "reason": f"Bearish rejection (RSI={rsi:.1f}, ADX={adx:.1f})",
            }

        if signal:
            self._last_signal_time[symbol] = time.time()
            logger.info(f"[FNO] Signal {symbol}: {signal['action']} — {signal['reason']}")
        else:
            self.last_skip_reason = f"RSI={rsi:.1f} no CE/PE setup"
        return signal

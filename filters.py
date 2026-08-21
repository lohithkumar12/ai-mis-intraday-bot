"""
filters.py — Extra entry gates (MTF + index regime + Nifty/BankNifty tide)
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

import config
import bot_state
from strategy import calc_sma

logger = logging.getLogger(__name__)

# Process-wide latch (also mirrored in bot_state for dashboard / scout).
TIDE_BEARISH: bool = False
TIDE_LOCK_REASON = "TIDE LOCK"
_TIDE_INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY")


def is_uptrend_df(df: Optional[pd.DataFrame], sma_len: int = 200) -> bool:
    if df is None or df.empty or len(df) < sma_len:
        return True  # don't block if insufficient data
    close = df["close"]
    sma = calc_sma(close, sma_len)
    last_sma = sma.iloc[-1]
    if pd.isna(last_sma):
        return True
    return float(close.iloc[-1]) > float(last_sma)


def market_sma_allows(market: str, bar_cache: dict) -> bool:
    """
    Optional market-wide SMA gate (USE_MARKET_SMA_FILTER).
    When enabled, blocks new BUYs if REGIME_SYMBOL_* is below SMA_SLOW.
    Independent of USE_REGIME_FILTER (mis_regime playbook selection).
    """
    if not getattr(config, "USE_MARKET_SMA_FILTER", False):
        return True
    symbol = (
        config.REGIME_SYMBOL_US
        if market.upper() == "US"
        else config.REGIME_SYMBOL_INDIA
    )
    df = bar_cache.get(symbol)
    ok = is_uptrend_df(df, config.SMA_SLOW)
    if not ok:
        logger.info(
            f"[{market}] Market SMA filter blocked entries — "
            f"{symbol} below SMA{config.SMA_SLOW}"
        )
    return ok


# Back-compat alias used by main.py
regime_allows = market_sma_allows


def mtf_allows(symbol: str, daily_df: Optional[pd.DataFrame]) -> bool:
    if not config.USE_MTF_FILTER:
        return True
    ok = is_uptrend_df(daily_df, config.SMA_SLOW)
    if not ok:
        logger.info(f"{symbol}: MTF filter blocked — daily below SMA{config.SMA_SLOW}")
    return ok


def _index_5m_pct_change(df: Optional[pd.DataFrame]) -> float | None:
    """(current 5m close − previous 5m close) / previous, as percent."""
    if df is None or getattr(df, "empty", True) or "close" not in df.columns:
        return None
    closes = df["close"].dropna().astype(float)
    if len(closes) < 2:
        return None
    prev = float(closes.iloc[-2])
    cur = float(closes.iloc[-1])
    if prev <= 0:
        return None
    return (cur - prev) / prev * 100.0


def _fetch_index_5m_bars(broker, symbol: str):
    """Force 5-minute index candles even if TIMEFRAME is not 5."""
    if broker is None or not hasattr(broker, "get_historical_bars"):
        return None
    try:
        return broker.get_historical_bars(symbol, days=3, interval=5)
    except TypeError:
        return broker.get_historical_bars(symbol, days=3)
    except Exception as e:
        logger.debug(f"[TIDE] {symbol} 5m bars failed: {e}")
        return None


def apply_tide_lock_to_signal(snap: dict | None) -> dict:
    """Force HOLD + dashboard reason 'TIDE LOCK' (entries only)."""
    row = dict(snap or {})
    row["signal"] = "HOLD"
    row["reason"] = TIDE_LOCK_REASON
    return row


def market_tide_filter(broker) -> bool:
    """
    Nifty / BankNifty 5-minute dump circuit breaker.

    Latch ON when either index's last 5m close-to-close change is <= TIDE_TRIGGER_PCT.
    Stay ON until BOTH indices' 5m change is > TIDE_CLEAR_PCT.
    Does not flatten; callers must skip new buys only.

    Returns True when TIDE_BEARISH (block all _india_try_buy).
    """
    global TIDE_BEARISH

    if not bool(getattr(config, "TIDE_FILTER_ENABLED", True)):
        TIDE_BEARISH = False
        bot_state.set_tide_state(bearish=False, reason="")
        return False

    trigger = float(getattr(config, "TIDE_TRIGGER_PCT", -0.65) or -0.65)
    clear = float(getattr(config, "TIDE_CLEAR_PCT", -0.20) or -0.20)

    pcts: dict[str, float | None] = {}
    for sym in _TIDE_INDEX_SYMBOLS:
        try:
            df = _fetch_index_5m_bars(broker, sym)
            pcts[sym] = _index_5m_pct_change(df)
        except Exception as e:
            logger.debug(f"[TIDE] {sym} change skipped: {e}")
            pcts[sym] = None

    nifty_pct = pcts.get("NIFTY")
    bank_pct = pcts.get("BANKNIFTY")
    known = [p for p in (nifty_pct, bank_pct) if p is not None]
    locked = bool(bot_state.is_tide_bearish() or TIDE_BEARISH)

    if any(p <= trigger for p in known):
        locked = True
    elif (
        nifty_pct is not None
        and bank_pct is not None
        and nifty_pct > clear
        and bank_pct > clear
    ):
        locked = False
    # else: incomplete data or still in the hysteresis band — keep latch

    TIDE_BEARISH = bool(locked)
    reason = TIDE_LOCK_REASON if locked else ""
    bot_state.set_tide_state(
        bearish=locked,
        nifty_pct=nifty_pct,
        banknifty_pct=bank_pct,
        reason=reason,
    )
    n_txt = f"{nifty_pct:+.2f}%" if nifty_pct is not None else "n/a"
    b_txt = f"{bank_pct:+.2f}%" if bank_pct is not None else "n/a"
    if locked:
        logger.warning(
            f"[INDIA TIDE LOCK] Nifty 5m={n_txt} BankNifty 5m={b_txt} "
            f"(trigger={trigger:.2f}% clear>{clear:.2f}%) — new entries blocked"
        )
    else:
        logger.info(
            f"[INDIA TIDE] clear Nifty 5m={n_txt} BankNifty 5m={b_txt}"
        )
    return locked


def fetch_daily_bars(data_feed, symbol: str) -> Optional[pd.DataFrame]:
    """Temporarily request daily bars from Alpaca DataFeed."""
    if data_feed is None:
        return None
    old_tf = config.TIMEFRAME
    old_lb = config.LOOKBACK_BARS
    try:
        config.TIMEFRAME = "1Day"
        config.LOOKBACK_BARS = max(config.SMA_SLOW + 20, 220)
        return data_feed.get_historical_bars(symbol)
    except Exception as e:
        logger.debug(f"Daily bars for {symbol} failed: {e}")
        return None
    finally:
        config.TIMEFRAME = old_tf
        config.LOOKBACK_BARS = old_lb

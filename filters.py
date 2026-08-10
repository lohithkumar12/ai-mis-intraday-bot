"""
filters.py — Extra entry gates (MTF + index regime)
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

import config
from strategy import calc_sma

logger = logging.getLogger(__name__)


def is_uptrend_df(df: Optional[pd.DataFrame], sma_len: int = 200) -> bool:
    if df is None or df.empty or len(df) < sma_len:
        return True  # don't block if insufficient data
    close = df["close"]
    sma = calc_sma(close, sma_len)
    last_sma = sma.iloc[-1]
    if pd.isna(last_sma):
        return True
    return float(close.iloc[-1]) > float(last_sma)


def regime_allows(market: str, bar_cache: dict) -> bool:
    if not config.USE_REGIME_FILTER:
        return True
    symbol = (
        config.REGIME_SYMBOL_US
        if market.upper() == "US"
        else config.REGIME_SYMBOL_INDIA
    )
    df = bar_cache.get(symbol)
    ok = is_uptrend_df(df, config.SMA_SLOW)
    if not ok:
        logger.info(f"[{market}] Regime filter blocked entries — {symbol} below SMA{config.SMA_SLOW}")
    return ok


def mtf_allows(symbol: str, daily_df: Optional[pd.DataFrame]) -> bool:
    if not config.USE_MTF_FILTER:
        return True
    ok = is_uptrend_df(daily_df, config.SMA_SLOW)
    if not ok:
        logger.info(f"{symbol}: MTF filter blocked — daily below SMA{config.SMA_SLOW}")
    return ok


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

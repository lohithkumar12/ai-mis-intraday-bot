"""
india_scout.py — India scout universe (~Nifty 50) + Near Setups scoring
=======================================================================
Scout universe is TRADE-ELIGIBLE (orders placed from main India scout loop
when INDIA_SCOUT_AUTO_BUY=true and full trend_pullback + risk gates pass).

This module only scores closeness for the Near Setups panel (names close
but not confirmed). It never places orders itself.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

import config
from strategy import MarketParams, params_for_market

logger = logging.getLogger(__name__)

# Sensible Nifty-50-style liquid names (subset). Broker resolve / instruments map
# must cover each symbol; unknowns are skipped at scan time.
DEFAULT_INDIA_SCOUT_UNIVERSE: list[str] = [
    "RELIANCE",
    "TCS",
    "HDFCBANK",
    "INFY",
    "ICICIBANK",
    "HINDUNILVR",
    "ITC",
    "SBIN",
    "BHARTIARTL",
    "LT",
    "KOTAKBANK",
    "WIPRO",
    "AXISBANK",
    "BAJFINANCE",
    "MARUTI",
    "SUNPHARMA",
    "TITAN",
    "ASIANPAINT",
    "ULTRACEMCO",
    "NESTLEIND",
    "POWERGRID",
    "NTPC",
    "ONGC",
    "TATAMOTORS",
    "TATASTEEL",
    "JSWSTEEL",
    "M&M",
    "TECHM",
    "HCLTECH",
    "ADANIENT",
    "ADANIPORTS",
    "COALINDIA",
    "BPCL",
    "CIPLA",
    "DRREDDY",
    "APOLLOHOSP",
    "EICHERMOT",
    "HEROMOTOCO",
    "INDUSINDBK",
    "BAJAJFINSV",
    "BAJAJ-AUTO",
    "BRITANNIA",
    "GRASIM",
    "HINDALCO",
    "DIVISLAB",
    "HDFCLIFE",
    "SBILIFE",
    "TATACONSUM",
    "BEL",
    "TRENT",
]


def resolve_scout_universe() -> list[str]:
    """
    Scout trade/watch list: env INDIA_SCOUT_UNIVERSE if set, else DEFAULT.
    Deduped, uppercased. Does not mutate INDIA_STOCK_UNIVERSE.
    """
    raw = (config.INDIA_SCOUT_UNIVERSE or []).copy()
    if not raw:
        raw = list(DEFAULT_INDIA_SCOUT_UNIVERSE)
    seen: set[str] = set()
    out: list[str] = []
    for s in raw:
        sym = str(s).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def scout_only_symbols() -> list[str]:
    """Scout names not already in the core trade/scanner universe."""
    core = {s.upper() for s in config.INDIA_STOCK_UNIVERSE}
    return [s for s in resolve_scout_universe() if s not in core]


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if np.isnan(v):
        return None
    return v


def score_near_setup(
    df: pd.DataFrame,
    params: MarketParams | None = None,
    *,
    symbol: str = "",
) -> dict[str, Any]:
    """
    Score how close bars are to a trend_pullback BUY (0–100).

    Pure analytics — orders are placed only by the India scout loop.
    """
    p = params or params_for_market("INDIA")
    empty = {
        "symbol": symbol,
        "score": 0.0,
        "price": None,
        "rsi": None,
        "adx": None,
        "reason": "insufficient data",
        "components": {},
        "would_buy": False,
        "near_only": True,
        "in_core_universe": symbol.upper() in set(config.INDIA_STOCK_UNIVERSE),
    }
    if df is None or df.empty:
        return empty

    need = max(p.sma_slow, p.ema_pullback, p.volume_avg_period) + 2
    if len(df) < need:
        return {**empty, "reason": f"need {need} bars, have {len(df)}"}

    sma_s = f"SMA_{p.sma_slow}"
    sma_f = f"SMA_{p.sma_fast}"
    ema_p = f"EMA_{p.ema_pullback}"
    rsi_c = f"RSI_{p.rsi_period}"

    for col in (sma_s, sma_f, ema_p, rsi_c, "VOL_AVG", "ADX", "volume", "close"):
        if col not in df.columns:
            return {**empty, "reason": f"missing {col}"}

    latest = df.iloc[-1]
    close = _safe_float(latest["close"])
    sma_slow = _safe_float(latest[sma_s])
    sma_fast = _safe_float(latest[sma_f])
    ema = _safe_float(latest[ema_p])
    rsi = _safe_float(latest[rsi_c])
    vol = _safe_float(latest.get("volume")) or 0.0
    vol_avg = _safe_float(latest["VOL_AVG"])
    adx = _safe_float(latest["ADX"])

    if any(v is None for v in (close, sma_slow, sma_fast, ema, rsi, vol_avg)):
        return {**empty, "reason": "indicators warming up"}

    assert close is not None and sma_slow is not None and sma_fast is not None
    assert ema is not None and rsi is not None and vol_avg is not None

    pct_above_slow = (close / sma_slow) - 1.0
    if pct_above_slow >= 0:
        trend_score = 30.0
        trend_note = f"above SMA{p.sma_slow}"
    elif pct_above_slow >= -0.02:
        trend_score = 15.0 * (1.0 + pct_above_slow / 0.02)
        trend_note = f"{pct_above_slow:.1%} vs SMA{p.sma_slow}"
    else:
        trend_score = 0.0
        trend_note = f"below SMA{p.sma_slow} ({pct_above_slow:.1%})"

    band = 0.01
    dist_sma = (close / sma_fast) - 1.0
    dist_ema = (close / ema) - 1.0
    best_dist = min(dist_sma, dist_ema)
    near_ma = (close <= sma_fast * (1.0 + band)) or (close <= ema * (1.0 + band))

    if close <= max(sma_fast, ema) * (1.0 + band) and close >= min(sma_fast, ema) * 0.97:
        pullback_score = 30.0
        pb_note = "at SMA/EMA pullback zone"
    elif near_ma and close > min(sma_fast, ema):
        pullback_score = 28.0
        pb_note = "near SMA20/EMA21"
    elif 0 < best_dist <= 0.04:
        pullback_score = max(0.0, 30.0 * (1.0 - best_dist / 0.04))
        pb_note = f"{best_dist:.1%} above nearest MA"
    elif best_dist > 0.04:
        pullback_score = 0.0
        pb_note = "extended above MAs"
    else:
        depth = abs(best_dist)
        pullback_score = max(5.0, 20.0 - depth * 200.0)
        pb_note = f"{best_dist:.1%} vs MAs (deep)"

    rsi_lo = p.rsi_buy
    rsi_hi = p.rsi_buy + 10.0
    prev_rsi = _safe_float(df[rsi_c].iloc[-2]) if len(df) >= 2 else None
    rsi_oversold_recent = prev_rsi is not None and prev_rsi < rsi_lo
    rsi_in_window = rsi_lo <= rsi < rsi_hi

    if rsi_in_window and rsi_oversold_recent:
        rsi_score = 20.0
        rsi_note = f"RSI recovering {rsi:.0f}"
    elif rsi_in_window:
        rsi_score = 16.0
        rsi_note = f"RSI in entry window {rsi:.0f}"
    elif rsi < rsi_lo:
        gap = rsi_lo - rsi
        rsi_score = max(4.0, 14.0 - gap)
        rsi_note = f"RSI oversold {rsi:.0f}, wait turn"
    elif rsi < rsi_hi + 5:
        rsi_score = 8.0
        rsi_note = f"RSI {rsi:.0f} slightly high"
    else:
        rsi_score = 0.0
        rsi_note = f"RSI elevated {rsi:.0f}"

    if rsi_in_window and rsi_oversold_recent and not near_ma:
        pullback_score = max(pullback_score, 22.0)
        pb_note = "RSI recovery path"

    vol_ratio = (vol / vol_avg) if vol_avg > 0 else 0.0
    if vol_ratio >= 1.0:
        vol_score = 15.0
        vol_note = f"vol {vol_ratio:.1f}x avg"
    elif vol_ratio >= 0.8:
        vol_score = 10.0 * (vol_ratio - 0.8) / 0.2 + 5.0
        vol_note = f"vol {vol_ratio:.1f}x (soft)"
    else:
        vol_score = max(0.0, 5.0 * vol_ratio / 0.8)
        vol_note = f"vol light {vol_ratio:.1f}x"

    if adx is None:
        adx_score = 2.0
    elif adx >= 25:
        adx_score = 5.0
    elif adx >= 18:
        adx_score = 3.5
    else:
        adx_score = 1.0

    uptrend = df["close"] > df[sma_s]
    near_ma_s = (df["close"] <= df[sma_f] * 1.01) | (df["close"] <= df[ema_p] * 1.01)
    rsi_series = df[rsi_c]
    rsi_os = rsi_series.shift(1) < p.rsi_buy
    rsi_rec = (rsi_series >= p.rsi_buy) & (rsi_series < p.rsi_buy + 10)
    pullback_or_rsi = near_ma_s | (rsi_os & rsi_rec)
    vol_ok = df["volume"].astype(float) > df["VOL_AVG"]
    buy_flags = (uptrend & pullback_or_rsi & vol_ok).fillna(False)
    n = max(1, int(p.confirm_bars))
    recent = buy_flags.iloc[-n:] if len(buy_flags) >= n else buy_flags
    confirm_hits = int(recent.sum())
    would_buy = bool(len(recent) >= n and recent.all())
    confirm_frac = confirm_hits / float(n)

    raw = trend_score + pullback_score + rsi_score + vol_score + adx_score
    score = min(100.0, raw + confirm_frac * 5.0)

    if pct_above_slow < -0.05:
        score = min(score, 25.0)

    bits = [trend_note, pb_note, rsi_note]
    if vol_ratio < 1.0:
        bits.append(vol_note)
    if confirm_hits and not would_buy:
        bits.append(f"confirm {confirm_hits}/{n}")
    if would_buy:
        bits = ["READY (full signal)", trend_note, pb_note]

    reason = " · ".join(bits[:3])
    in_core = symbol.upper() in set(config.INDIA_STOCK_UNIVERSE)

    return {
        "symbol": symbol,
        "score": round(float(score), 1),
        "price": round(close, 2),
        "rsi": round(rsi, 1),
        "adx": round(adx, 1) if adx is not None else None,
        "reason": reason,
        "components": {
            "trend": round(trend_score, 1),
            "pullback": round(pullback_score, 1),
            "rsi": round(rsi_score, 1),
            "volume": round(vol_score, 1),
            "adx": round(adx_score, 1),
            "confirm_hits": confirm_hits,
            "confirm_bars": n,
            "vol_ratio": round(vol_ratio, 2),
            "pct_above_sma_slow": round(pct_above_slow * 100.0, 2),
        },
        "would_buy": would_buy,
        "near_only": not would_buy,
        "in_core_universe": in_core,
        "in_trade_universe": in_core,
        "watch_only": not would_buy,
    }


def rank_near_setups(
    scored: list[dict[str, Any]],
    top_n: int | None = None,
    *,
    min_score: float | None = None,
    exclude_confirmed: bool = True,
) -> list[dict[str, Any]]:
    """
    Sort by score desc for the Near Setups panel.
    By default excludes full would_buy confirmations (those trade via scout loop).
    """
    n = top_n if top_n is not None else config.INDIA_SCOUT_TOP_N
    floor = min_score if min_score is not None else float(config.INDIA_SCOUT_MIN_SCORE)
    filtered = [r for r in scored if float(r.get("score") or 0) >= floor]
    if exclude_confirmed:
        filtered = [r for r in filtered if not r.get("would_buy")]
    filtered.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
    return filtered[: max(1, int(n))]

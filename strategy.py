"""
strategy.py — Pluggable Trading Strategies
============================================
Selectable via config.STRATEGY_NAME:

  A) mis_regime / regime_mis / vwap_regime (INDIA DEFAULT)
     - Regime XOR selector: at most one playbook BUY per symbol per cycle
     - OPEN_DRIVE → Improved ORB (no automatic fall-through)
     - TREND_UP → VWAP momentum+RS, else EMA pullback
     - RANGE → VWAP mean reversion
     - TREND_DOWN → HOLD (long-only)
     - Completed 5m bars only

  B) opening_range_breakout / orb (legacy MIS)
     - First OR_MINUTES after 09:15 IST → OR high/low
     - BUY when CONFIRM_BARS close above OR high + soft volume
     - Optional EMA trend filter on recent closes

  C) trend_pullback (CNC-style; available)
     - LONG only if price > SMA200
     - Enter on pullback to SMA20/EMA21 OR RSI recovery from oversold

  D) mean_reversion / regime_adaptive / breakout — same as New_StartUp

Same interface for India (and US if enabled); params from market-specific config.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)

# Process-wide ORB day-fired locks (core + scout share one dict + disk file).
# Values: {"side": "BUY", "count": 1, "first_vol": ..., "first_high": ...}
# Legacy files may still store a plain "BUY"/"SELL" string.
_orb_fired_lock = threading.RLock()
_orb_fired: dict[str, dict | str] = {}
_orb_fired_loaded = False
_orb_trigger_pending: dict[str, dict] = {}


def reset_orb_fired_for_tests() -> None:
    """Clear shared ORB day-fired memory (+ optional disk via tests' temp journal path)."""
    global _orb_fired_loaded
    with _orb_fired_lock:
        _orb_fired.clear()
        _orb_trigger_pending.clear()
        _orb_fired_loaded = False


def _normalize_fired(value) -> dict | None:
    """Coerce persisted day-fired payload to a dict. None if empty/unknown."""
    if not value:
        return None
    if isinstance(value, str):
        side = value.strip().upper()
        if side in ("BUY", "SELL"):
            return {"side": side, "count": 1}
        return None
    if isinstance(value, dict):
        side = str(value.get("side") or "").strip().upper()
        if side not in ("BUY", "SELL"):
            return None
        rec = dict(value)
        rec["side"] = side
        try:
            rec["count"] = max(1, int(value.get("count") or 1))
        except (TypeError, ValueError):
            rec["count"] = 1
        return rec
    return None


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------
def calc_sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()


def calc_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, min_periods=length, adjust=False).mean()


def calc_rsi(series: pd.Series, length: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calc_bbands(
    series: pd.Series, length: int = 20, std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    middle = series.rolling(window=length, min_periods=length).mean()
    rolling_std = series.rolling(window=length, min_periods=length).std()
    upper = middle + (std * rolling_std)
    lower = middle - (std * rolling_std)
    return lower, middle, upper


def calc_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def calc_adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average Directional Index — low ADX ≈ ranging market."""
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    atr = calc_atr(df, length)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / length, min_periods=length, adjust=False
    ).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / length, min_periods=length, adjust=False
    ).mean() / atr.replace(0, np.nan)

    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def _ist_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Kolkata")
    except ImportError:
        import pytz  # type: ignore

        return pytz.timezone("Asia/Kolkata")


def _et_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz  # type: ignore

        return pytz.timezone("America/New_York")


def now_ist() -> datetime:
    return datetime.now(_ist_tz())


def now_et() -> datetime:
    return datetime.now(_et_tz())


def market_now(market: str) -> datetime:
    """Local clock for the given market (IST for India, ET for US)."""
    if str(market or "").upper() == "US":
        return now_et()
    return now_ist()


def entry_cutoff_for_market(market: str) -> str:
    """HH:MM cutoff string for the market's local clock."""
    if str(market or "").upper() == "US":
        return str(getattr(config, "US_ENTRY_CUTOFF", "") or "").strip()
    return str(getattr(config, "ENTRY_CUTOFF", "") or "").strip()


def timeframe_minutes() -> int:
    raw = str(getattr(config, "TIMEFRAME", "5") or "5").strip().lower()
    digits = "".join(ch for ch in raw if ch.isdigit())
    try:
        return max(1, int(digits or "5"))
    except Exception:
        return 5


def completed_bars_only(
    df: pd.DataFrame, now: datetime | None = None
) -> pd.DataFrame:
    """
    Drop the currently forming candle so no playbook uses live close/volume.
    Bars whose period end is still in the future (IST) are excluded.
    """
    if df is None or df.empty:
        return df
    ist = _ist_tz()
    if now is None:
        now = now_ist()
    elif getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=ist)
    else:
        now = now.astimezone(ist)
    tf_m = timeframe_minutes()
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        # No timestamps: refuse the last row as potentially forming.
        return df.iloc[:-1] if len(df) > 1 else df
    if getattr(idx, "tz", None) is None:
        local = idx.tz_localize(ist)
    else:
        local = idx.tz_convert(ist)
    # Bar labeled T covers [T, T+tf). Complete when now >= T+tf.
    complete_mask = [
        (ts.to_pydatetime() + timedelta(minutes=tf_m)) <= now for ts in local
    ]
    mask = pd.Series(complete_mask, index=df.index)
    out = df.loc[mask]
    if out.empty and len(df) > 1:
        return df.iloc[:-1]
    if len(out) == len(df) and len(df) > 0:
        last_end = local[-1].to_pydatetime() + timedelta(minutes=tf_m)
        if now < last_end:
            return df.iloc[:-1]
    return out if not out.empty else df.iloc[:-1]


def calc_session_vwap(
    df: pd.DataFrame,
    session_open_hm: tuple[int, int] = (9, 15),
    tz=None,
) -> pd.Series:
    """
    Session VWAP from today's session-open completed bars:

        typical_price = (high + low + close) / 3
        VWAP = cumsum(typical_price * volume) / cumsum(volume)

    Resets each calendar session. Pre-open bars are NaN.
    A RangeIndex (tests) is treated as a single session using every row.
    tz defaults to Asia/Kolkata; pass America/New_York for US.
    """
    if df is None or df.empty:
        return pd.Series(dtype=float)
    typical = (
        df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)
    ) / 3.0
    vol = df["volume"].astype(float).clip(lower=0.0)
    tpv = typical * vol

    if not isinstance(df.index, pd.DatetimeIndex):
        cum_vol = vol.cumsum().replace(0, np.nan)
        return tpv.cumsum() / cum_vol

    local_tz = tz or _ist_tz()
    idx = df.index
    local = (
        idx.tz_localize(local_tz)
        if getattr(idx, "tz", None) is None
        else idx.tz_convert(local_tz)
    )
    open_m = int(session_open_hm[0]) * 60 + int(session_open_hm[1])
    vwap_vals = np.full(len(df), np.nan, dtype=float)
    cum_tpv = 0.0
    cum_vol = 0.0
    prev_date = None
    for i, ts in enumerate(local):
        d = ts.date()
        if d != prev_date:
            cum_tpv = 0.0
            cum_vol = 0.0
            prev_date = d
        mins = int(ts.hour) * 60 + int(ts.minute)
        if mins < open_m:
            continue
        cum_tpv += float(tpv.iloc[i])
        cum_vol += float(vol.iloc[i])
        if cum_vol > 0:
            vwap_vals[i] = cum_tpv / cum_vol
    return pd.Series(vwap_vals, index=df.index)


def in_open_drive_window(
    now: datetime | None = None,
    window_minutes: int | None = None,
    *,
    market: str = "INDIA",
) -> bool:
    """True during session open drive window (IST 09:15 / US ET 09:30)."""
    if now is None:
        now = market_now(market)
    if window_minutes is None:
        window_minutes = int(getattr(config, "ORB_WINDOW_MINUTES", 60) or 60)
    mins = now.hour * 60 + now.minute
    if str(market or "").upper() == "US":
        open_m = 9 * 60 + 30
    else:
        open_m = 9 * 60 + 15
    return open_m <= mins < open_m + int(window_minutes)


def past_entry_cutoff(
    now: datetime | None = None,
    *,
    market: str | None = None,
    cutoff: str | None = None,
) -> bool:
    """
    True when local time >= entry cutoff (HH:MM). No new BUYs after this.

    For US, pass market="US" (or cutoff=US_ENTRY_CUTOFF) and an ET `now`.
    India defaults: IST now + ENTRY_CUTOFF.
    """
    raw = cutoff
    if raw is None and market is not None:
        raw = entry_cutoff_for_market(market)
    if raw is None:
        raw = getattr(config, "ENTRY_CUTOFF", "") or ""
    if now is None:
        now = market_now(market or "INDIA")
    if not raw or ":" not in str(raw):
        return False
    try:
        hh, mm = str(raw).split(":")[:2]
        cut = int(hh) * 60 + int(mm)
    except Exception:
        return False
    return (now.hour * 60 + now.minute) >= cut


REGIME_OPEN_DRIVE = "OPEN_DRIVE"
REGIME_TREND_UP = "TREND_UP"
REGIME_RANGE = "RANGE"
REGIME_TREND_DOWN = "TREND_DOWN"

PLAYBOOK_ORB = "IMPROVED_ORB"
PLAYBOOK_MOMENTUM = "VWAP_MOMENTUM_RS"
PLAYBOOK_PULLBACK = "EMA_PULLBACK"
PLAYBOOK_MR = "VWAP_MEAN_REVERSION"
PLAYBOOK_NONE = "NONE"

_MIS_REGIME_NAMES = frozenset({"mis_regime", "regime_mis", "vwap_regime"})


@dataclass
class MarketParams:
    """Per-market strategy knobs."""

    sma_slow: int
    sma_fast: int
    ema_pullback: int
    rsi_period: int
    rsi_buy: float
    rsi_sell: float
    bb_std: float
    adx_range_max: float
    atr_period: int = 14
    adx_period: int = 14
    volume_avg_period: int = 20
    confirm_bars: int = 2
    market: str = "US"


def params_for_market(market: str) -> MarketParams:
    m = market.upper()
    if m == "INDIA":
        return MarketParams(
            sma_slow=config.INDIA_SMA_SLOW,
            sma_fast=config.INDIA_SMA_FAST,
            ema_pullback=config.INDIA_EMA_PULLBACK,
            rsi_period=config.INDIA_RSI_PERIOD,
            rsi_buy=config.INDIA_RSI_BUY,
            rsi_sell=config.INDIA_RSI_SELL,
            bb_std=config.INDIA_BB_STD,
            adx_range_max=config.INDIA_ADX_RANGE_MAX,
            atr_period=config.ATR_PERIOD,
            adx_period=config.ADX_PERIOD,
            volume_avg_period=config.VOLUME_AVG_PERIOD,
            confirm_bars=config.CONFIRM_BARS,
            market="INDIA",
        )
    return MarketParams(
        sma_slow=config.US_SMA_SLOW,
        sma_fast=config.US_SMA_FAST,
        ema_pullback=config.US_EMA_PULLBACK,
        rsi_period=config.US_RSI_PERIOD,
        rsi_buy=config.US_RSI_BUY,
        rsi_sell=config.US_RSI_SELL,
        bb_std=config.US_BB_STD,
        adx_range_max=config.US_ADX_RANGE_MAX,
        atr_period=config.ATR_PERIOD,
        adx_period=config.ADX_PERIOD,
        volume_avg_period=config.VOLUME_AVG_PERIOD,
        confirm_bars=config.CONFIRM_BARS,
        market="US",
    )


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------
class BaseStrategy(ABC):
    """Shared strategy contract for US and India loops."""

    name: str = "base"

    def __init__(self, params: MarketParams):
        self.p = params

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # Ensure volume column exists
        if "volume" not in df.columns:
            df["volume"] = 0.0

        df[f"SMA_{self.p.sma_slow}"] = calc_sma(df["close"], self.p.sma_slow)
        df[f"SMA_{self.p.sma_fast}"] = calc_sma(df["close"], self.p.sma_fast)
        df[f"EMA_{self.p.ema_pullback}"] = calc_ema(df["close"], self.p.ema_pullback)
        df[f"RSI_{self.p.rsi_period}"] = calc_rsi(df["close"], self.p.rsi_period)
        bbl, bbm, bbu = calc_bbands(df["close"], self.p.sma_fast, self.p.bb_std)
        df["BBL"] = bbl
        df["BBM"] = bbm
        df["BBU"] = bbu
        df["ATR"] = calc_atr(df, self.p.atr_period)
        df["ADX"] = calc_adx(df, self.p.adx_period)
        df["VOL_AVG"] = calc_sma(df["volume"].astype(float), self.p.volume_avg_period)
        return df

    def latest_atr(self, df: pd.DataFrame) -> Optional[float]:
        if df is None or df.empty or "ATR" not in df.columns:
            return None
        atr = df.iloc[-1].get("ATR")
        if atr is None or pd.isna(atr) or atr <= 0:
            return None
        return float(atr)

    def _bars_confirm(self, flags: pd.Series, n: int) -> bool:
        """True if the last n bars all satisfy the condition."""
        if n <= 1:
            return bool(flags.iloc[-1]) if len(flags) else False
        if len(flags) < n:
            return False
        return bool(flags.iloc[-n:].all())

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        ...


# ---------------------------------------------------------------------------
# A) Trend + Pullback (PRIMARY)
# ---------------------------------------------------------------------------
class TrendPullbackStrategy(BaseStrategy):
    name = "trend_pullback"

    def __init__(self, params: MarketParams):
        super().__init__(params)
        logger.info(
            f"[{params.market}] TrendPullback | "
            f"SMA{params.sma_slow}/{params.sma_fast} EMA{params.ema_pullback} "
            f"RSI<{params.rsi_buy} confirm={params.confirm_bars}"
        )

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        if config.TEST_MODE:
            logger.error(f"{symbol}: TEST_MODE on — refusing signal.")
            return "HOLD"

        need = max(self.p.sma_slow, self.p.ema_pullback, self.p.volume_avg_period) + 2
        if len(df) < need:
            logger.warning(f"{symbol}: Not enough data ({len(df)} bars). HOLD.")
            return "HOLD"

        sma_s = f"SMA_{self.p.sma_slow}"
        sma_f = f"SMA_{self.p.sma_fast}"
        ema_p = f"EMA_{self.p.ema_pullback}"
        rsi_c = f"RSI_{self.p.rsi_period}"

        latest = df.iloc[-1]
        close = float(latest["close"])
        sma_slow = latest.get(sma_s)
        sma_fast = latest.get(sma_f)
        ema = latest.get(ema_p)
        rsi = latest.get(rsi_c)
        vol = float(latest.get("volume") or 0)
        vol_avg = latest.get("VOL_AVG")
        bbu = latest.get("BBU")

        if any(pd.isna(x) for x in (sma_slow, sma_fast, ema, rsi, vol_avg)):
            return "HOLD"

        logger.info(
            f"{symbol} [{self.name}] Close={close:.2f} SMA{self.p.sma_slow}={sma_slow:.2f} "
            f"SMA{self.p.sma_fast}={sma_fast:.2f} EMA{self.p.ema_pullback}={ema:.2f} "
            f"RSI={rsi:.1f} Vol={vol:.0f}/{vol_avg:.0f}"
        )

        # --- SELL: overbought exit ---
        if config.STRICT_SELL:
            if rsi > self.p.rsi_sell and not pd.isna(bbu) and close >= float(bbu):
                logger.info(f"[SELL] {symbol} — overbought + upper BB")
                return "SELL"
        else:
            if rsi > self.p.rsi_sell or (not pd.isna(bbu) and close >= float(bbu)):
                logger.info(f"[SELL] {symbol} — overbought / upper BB")
                return "SELL"

        # --- BUY filters ---
        # 1) Uptrend only
        uptrend = df["close"] > df[sma_s]

        # 2) Pullback to SMA20/EMA21 OR RSI recovering from oversold
        near_ma = (df["close"] <= df[sma_f] * 1.01) | (df["close"] <= df[ema_p] * 1.01)
        # RSI was oversold recently and is now recovering
        rsi_series = df[rsi_c]
        rsi_oversold_recent = rsi_series.shift(1) < self.p.rsi_buy
        rsi_recovering = (rsi_series >= self.p.rsi_buy) & (rsi_series < self.p.rsi_buy + 10)
        pullback_or_rsi = near_ma | (rsi_oversold_recent & rsi_recovering)

        # 3) Volume confirmation
        vol_ok = df["volume"].astype(float) > df["VOL_AVG"]

        buy_flags = uptrend & pullback_or_rsi & vol_ok

        if self._bars_confirm(buy_flags.fillna(False), self.p.confirm_bars):
            logger.info(
                f"[BUY SIGNAL] {symbol} — Trend+Pullback confirmed "
                f"({self.p.confirm_bars} bars)"
            )
            return "BUY"

        return "HOLD"


# ---------------------------------------------------------------------------
# B) Mean Reversion (SECONDARY) — ranging markets only
# ---------------------------------------------------------------------------
class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"

    def __init__(self, params: MarketParams):
        super().__init__(params)
        logger.info(
            f"[{params.market}] MeanReversion | "
            f"RSI<{params.rsi_buy} BB ADX<{params.adx_range_max}"
        )

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        if config.TEST_MODE:
            logger.error(f"{symbol}: TEST_MODE on — refusing signal.")
            return "HOLD"

        need = max(self.p.sma_slow, self.p.adx_period) + 2
        if len(df) < need:
            logger.warning(f"{symbol}: Not enough data ({len(df)} bars). HOLD.")
            return "HOLD"

        rsi_c = f"RSI_{self.p.rsi_period}"
        sma_s = f"SMA_{self.p.sma_slow}"
        latest = df.iloc[-1]
        close = float(latest["close"])
        sma_slow = latest.get(sma_s)
        rsi = latest.get(rsi_c)
        bbl = latest.get("BBL")
        bbu = latest.get("BBU")
        adx = latest.get("ADX")

        if any(pd.isna(x) for x in (sma_slow, rsi, bbl, bbu, adx)):
            return "HOLD"

        logger.info(
            f"{symbol} [{self.name}] Close={close:.2f} SMA={sma_slow:.2f} "
            f"RSI={rsi:.1f} ADX={adx:.1f} BBL={bbl:.2f} BBU={bbu:.2f}"
        )

        # Disable mean-reversion buys in strong trends
        if float(adx) >= self.p.adx_range_max:
            logger.debug(
                f"{symbol}: ADX={adx:.1f} >= {self.p.adx_range_max} — "
                f"trending; mean-reversion BUY disabled"
            )
            # Still allow sells of existing positions when overbought
            if config.STRICT_SELL:
                if rsi > self.p.rsi_sell and close >= float(bbu):
                    return "SELL"
            else:
                if rsi > self.p.rsi_sell or close >= float(bbu):
                    return "SELL"
            return "HOLD"

        # Ranging: classic BB + RSI mean reversion (still prefer soft uptrend bias)
        buy_raw = (df["close"] > df[sma_s]) & (df[rsi_c] < self.p.rsi_buy) & (
            df["close"] <= df["BBL"]
        )
        if self._bars_confirm(buy_raw.fillna(False), self.p.confirm_bars):
            logger.info(f"[BUY SIGNAL] {symbol} — MeanReversion (ranging ADX={adx:.1f})")
            return "BUY"

        if config.STRICT_SELL:
            if rsi > self.p.rsi_sell and close >= float(bbu):
                logger.info(f"[SELL] {symbol} — overbought + upper BB")
                return "SELL"
        else:
            if rsi > self.p.rsi_sell or close >= float(bbu):
                logger.info(f"[SELL] {symbol} — overbought / upper BB")
                return "SELL"

        return "HOLD"


# ---------------------------------------------------------------------------
# C) Regime Adaptive — trend rules in trends, MR in ranges
# ---------------------------------------------------------------------------
class RegimeAdaptiveStrategy(BaseStrategy):
    """
    Uses ADX to pick the playbook:
      ADX >= adx_range_max  → TrendPullback (ride momentum after pullbacks)
      ADX <  adx_range_max  → MeanReversion (fade extremes in ranges)
    """

    name = "regime_adaptive"

    def __init__(self, params: MarketParams):
        super().__init__(params)
        self._trend = TrendPullbackStrategy(params)
        self._mr = MeanReversionStrategy(params)
        logger.info(
            f"[{params.market}] RegimeAdaptive | "
            f"ADX>={params.adx_range_max}→trend else→mean_reversion"
        )

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        if config.TEST_MODE:
            return "HOLD"
        need = max(self.p.sma_slow, self.p.adx_period) + 2
        if len(df) < need:
            return "HOLD"
        adx = df.iloc[-1].get("ADX")
        if adx is None or pd.isna(adx):
            return "HOLD"
        if float(adx) >= self.p.adx_range_max:
            return self._trend.generate_signal(df, symbol)
        return self._mr.generate_signal(df, symbol)


# ---------------------------------------------------------------------------
# D) Breakout / Momentum (Donchian-style)
# ---------------------------------------------------------------------------
class BreakoutStrategy(BaseStrategy):
    """
    Enter when close breaks above the prior N-bar high (default SMA_FAST window),
    still above SMA200, with volume confirmation. Exit on RSI/BB overbought or
    close back below SMA_FAST.
    """

    name = "breakout"

    def __init__(self, params: MarketParams, channel: int | None = None):
        super().__init__(params)
        self.channel = channel or max(params.sma_fast, 20)
        logger.info(
            f"[{params.market}] Breakout | channel={self.channel} SMA{params.sma_slow}"
        )

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        if config.TEST_MODE:
            return "HOLD"
        need = max(self.p.sma_slow, self.channel) + 2
        if len(df) < need:
            return "HOLD"

        sma_s = f"SMA_{self.p.sma_slow}"
        sma_f = f"SMA_{self.p.sma_fast}"
        rsi_c = f"RSI_{self.p.rsi_period}"
        latest = df.iloc[-1]
        if any(pd.isna(latest.get(c)) for c in (sma_s, sma_f, rsi_c, "VOL_AVG", "BBU")):
            return "HOLD"

        prior_high = float(df["high"].iloc[-(self.channel + 1) : -1].max())
        close = float(latest["close"])
        vol_ok = float(latest.get("volume") or 0) > float(latest["VOL_AVG"])
        uptrend = close > float(latest[sma_s])

        if config.STRICT_SELL:
            if float(latest[rsi_c]) > self.p.rsi_sell and close >= float(latest["BBU"]):
                return "SELL"
        else:
            if float(latest[rsi_c]) > self.p.rsi_sell or close >= float(latest["BBU"]):
                return "SELL"
        if close < float(latest[sma_f]):
            return "SELL"

        if uptrend and close > prior_high and vol_ok:
            logger.info(f"[BUY SIGNAL] {symbol} — Breakout above {prior_high:.2f}")
            return "BUY"
        return "HOLD"


class OpeningRangeBreakoutStrategy(BaseStrategy):
    """
    MIS opening-range breakout on intraday bars (5m/15m).
    Builds OR from session open for OR_MINUTES, then requires CONFIRM_BARS
    closes beyond the range with soft volume confirmation.
    """

    name = "opening_range_breakout"

    def __init__(
        self,
        params: MarketParams,
        *,
        or_minutes: int | None = None,
        volume_mult: float | None = None,
        confirm_bars: int | None = None,
    ):
        super().__init__(params)
        self.or_minutes = int(
            or_minutes if or_minutes is not None else (getattr(config, "OR_MINUTES", 15) or 15)
        )
        self.volume_mult = float(
            volume_mult
            if volume_mult is not None
            else (getattr(config, "VOLUME_MULT", 0.8) or 0.8)
        )
        self.confirm_bars = int(
            confirm_bars if confirm_bars is not None else config.CONFIRM_BARS
        )
        self.allow_short = bool(getattr(config, "ORB_ALLOW_SHORT", False))
        self.use_htf = bool(getattr(config, "ORB_USE_HTF_FILTER", True))
        self.htf_ema = int(getattr(config, "ORB_HTF_EMA_PERIOD", 20) or 20)
        self.last_orb_continuation = False
        self.last_decision: dict = {}
        # Day-key → payload only after a confirmed broker fill (not on bare signal).
        # Shared across all ORB instances via module-level _orb_fired.
        self.load_fired_state()
        logger.info(
            f"[{params.market}] ORB | or={self.or_minutes}m confirm={self.confirm_bars} "
            f"vol_mult={self.volume_mult} htf_ema={self.htf_ema if self.use_htf else 'off'}"
        )

    @property
    def _fired(self) -> dict[str, dict | str]:
        return _orb_fired

    @_fired.setter
    def _fired(self, value: dict[str, dict | str]) -> None:
        global _orb_fired
        with _orb_fired_lock:
            _orb_fired = dict(value or {})

    def _fire_key(self, symbol: str, day=None) -> str:
        if day is None:
            try:
                from zoneinfo import ZoneInfo

                day = datetime.now(ZoneInfo("Asia/Kolkata")).date()
            except Exception:
                day = datetime.now(timezone.utc).date()
        return f"{str(symbol).upper()}:{day}"

    def mark_day_fired(
        self, symbol: str, side: str = "BUY", day=None, continuation: bool = False, **kwargs
    ) -> None:
        """Lock ORB side for the session day after a confirmed fill (not on signal).

        First fill is idempotent (count=1). Pass continuation=True on the second
        ORB fill so count increments; journal/zombie re-marks must not bump count.
        """
        side_u = str(side or "BUY").upper()
        if side_u not in ("BUY", "SELL"):
            return
        key = self._fire_key(symbol, day=day)
        with _orb_fired_lock:
            pending = _orb_trigger_pending.pop(key, None)
            rec = _normalize_fired(_orb_fired.get(key))
            if rec is None:
                rec = {"side": side_u, "count": 1}
                if pending:
                    rec["first_vol"] = float(pending.get("vol") or 0)
                    rec["first_high"] = float(pending.get("high") or 0)
            elif continuation:
                rec["count"] = int(rec.get("count") or 1) + 1
                rec["side"] = side_u
            else:
                rec["side"] = side_u
                if pending and not rec.get("first_vol"):
                    rec["first_vol"] = float(pending.get("vol") or 0)
                    rec["first_high"] = float(pending.get("high") or 0)
            _orb_fired[key] = rec
            logger.info(
                f"[ORB] marked fired {key}={side_u} count={rec.get('count')} "
                f"first_vol={rec.get('first_vol')} continuation={bool(continuation)} "
                f"(after confirmed fill)"
            )
            self._persist_fired()

    def has_day_fired(self, symbol: str, side: str | None = None, day=None) -> bool:
        # Reload from disk so core/scout (separate strategy wrappers) stay in sync
        # after the other loop marks a fill, and across process restarts.
        self.load_fired_state()
        key = self._fire_key(symbol, day=day)
        with _orb_fired_lock:
            rec = _normalize_fired(_orb_fired.get(key))
        if not rec:
            return False
        if side is None:
            return True
        return rec.get("side") == str(side).upper()

    def _note_pending_trigger(self, key: str, latest) -> None:
        """Remember the first ORB signal candle's volume until mark_day_fired."""
        try:
            vol = float(latest.get("volume") or 0)
            high = float(latest.get("high") or 0)
        except Exception:
            return
        with _orb_fired_lock:
            if key in _orb_trigger_pending:
                return
            _orb_trigger_pending[key] = {"vol": vol, "high": high}

    def _orb_continuation_ok(self, df: pd.DataFrame, fired: dict) -> tuple[bool, str]:
        """Second ORB BUY: close ≥ prior 5m high by 0.5% AND vol > first ORB vol."""
        if not bool(getattr(config, "ORB_CONTINUATION_ENABLED", True)):
            return False, "orb_continuation_disabled"
        max_n = max(1, int(getattr(config, "ORB_CONTINUATION_MAX", 2) or 2))
        count = int(fired.get("count") or 1)
        if count >= max_n:
            return False, "orb_continuation_exhausted"
        first_vol = 0.0
        try:
            first_vol = float(fired.get("first_vol") or 0)
        except (TypeError, ValueError):
            first_vol = 0.0
        if first_vol <= 0:
            return False, "orb_continuation_no_first_vol"
        if df is None or len(df) < 2:
            return False, "orb_continuation_need_2_bars"
        prev_high = float(df["high"].iloc[-2])
        close = float(df["close"].iloc[-1])
        vol = float(df["volume"].iloc[-1] or 0)
        break_pct = float(getattr(config, "ORB_CONTINUATION_BREAK_PCT", 0.005) or 0.005)
        need = prev_high * (1.0 + break_pct)
        if close + 1e-12 < need:
            return False, f"orb_continuation_no_break close={close:.2f} need={need:.2f}"
        if vol <= first_vol:
            return False, f"orb_continuation_weak_vol {vol:.0f}<={first_vol:.0f}"
        return True, (
            f"orb_continuation close={close:.2f}>={need:.2f} "
            f"vol={vol:.0f}>{first_vol:.0f}"
        )

    def _fired_state_path(self) -> Path:
        journal = Path(str(getattr(config, "TRADE_JOURNAL_PATH", "trade_journal.db")))
        return journal.expanduser().resolve().parent / "orb_day_fired.json"

    def _persist_fired(self) -> None:
        try:
            path = self._fired_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Drop keys from prior calendar days
            try:
                from zoneinfo import ZoneInfo
                today = str(datetime.now(ZoneInfo("Asia/Kolkata")).date())
            except Exception:
                today = str(datetime.now(timezone.utc).date())
            # keys are SYMBOL:date — keep only today
            with _orb_fired_lock:
                keep = {
                    k: v
                    for k, v in _orb_fired.items()
                    if str(k).split(":")[-1] == today
                }
                _orb_fired.clear()
                _orb_fired.update(keep)
                payload = dict(_orb_fired)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.debug(f"ORB fired persist skip: {e}")

    def load_fired_state(self) -> None:
        global _orb_fired_loaded
        try:
            path = self._fired_state_path()
            if not path.is_file():
                _orb_fired_loaded = True
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                _orb_fired_loaded = True
                return
            try:
                from zoneinfo import ZoneInfo
                today = str(datetime.now(ZoneInfo("Asia/Kolkata")).date())
            except Exception:
                today = str(datetime.now(timezone.utc).date())
            with _orb_fired_lock:
                # Merge disk → memory (other instance may have written newer keys)
                for k, v in raw.items():
                    ks = str(k)
                    if ks.split(":")[-1] != today:
                        continue
                    disk_rec = _normalize_fired(v)
                    if disk_rec is None:
                        continue
                    mem = _normalize_fired(_orb_fired.get(ks))
                    if mem is None:
                        _orb_fired[ks] = disk_rec
                        continue
                    merged = dict(disk_rec)
                    if int(mem.get("count") or 1) > int(merged.get("count") or 1):
                        merged["count"] = mem["count"]
                        merged["side"] = mem.get("side") or merged.get("side")
                    if not merged.get("first_vol") and mem.get("first_vol"):
                        merged["first_vol"] = mem["first_vol"]
                        merged["first_high"] = mem.get("first_high")
                    _orb_fired[ks] = merged
                # Prune stale in-memory keys
                stale = [k for k in _orb_fired if str(k).split(":")[-1] != today]
                for k in stale:
                    _orb_fired.pop(k, None)
                n = len(_orb_fired)
            if not _orb_fired_loaded:
                logger.info(f"[ORB] restored {n} day-fired locks from {path}")
            _orb_fired_loaded = True
        except Exception as e:
            logger.debug(f"ORB fired load skip: {e}")
            _orb_fired_loaded = True

    @staticmethod
    def _timeframe_minutes() -> int:
        return timeframe_minutes()

    def completed_bars_only(
        self, df: pd.DataFrame, now: datetime | None = None
    ) -> pd.DataFrame:
        """
        Drop the currently forming candle so ORB never uses live close/volume.
        Bars whose period end is still in the future (IST) are excluded.
        """
        return completed_bars_only(df, now=now)

    def _session_date(self, ts) -> object:
        try:
            from zoneinfo import ZoneInfo
            ist = ZoneInfo("Asia/Kolkata")
        except ImportError:
            import pytz  # type: ignore
            ist = pytz.timezone("Asia/Kolkata")
        if getattr(ts, "tzinfo", None) is None:
            return pd.Timestamp(ts).tz_localize(ist).date()
        return pd.Timestamp(ts).tz_convert(ist).date()

    def _or_levels(self, day_df: pd.DataFrame) -> tuple[float, float] | None:
        if day_df is None or day_df.empty:
            return None
        # Bars whose clock time is within [09:15, 09:15+OR)
        try:
            from zoneinfo import ZoneInfo
            ist = ZoneInfo("Asia/Kolkata")
        except ImportError:
            import pytz  # type: ignore
            ist = pytz.timezone("Asia/Kolkata")

        idx = day_df.index
        if getattr(idx, "tz", None) is None:
            local = idx.tz_localize(ist)
        else:
            local = idx.tz_convert(ist)
        minutes = local.hour * 60 + local.minute
        open_m = 9 * 60 + 15
        end_m = open_m + self.or_minutes
        mask = (minutes >= open_m) & (minutes < end_m)
        or_bars = day_df.loc[mask]
        if or_bars.empty:
            return None
        hi = float(or_bars["high"].max())
        lo = float(or_bars["low"].min())
        if hi <= lo:
            return None
        return hi, lo

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        self.last_orb_continuation = False
        if config.TEST_MODE:
            return "HOLD"
        need = max(30, config.ATR_PERIOD + 5, self.htf_ema + 2)
        if df is None or len(df) < need:
            return "HOLD"
        if "VOL_AVG" not in df.columns:
            df = self.compute_indicators(df)

        # Sync shared day-fired locks (core ↔ scout ↔ disk) before signal.
        self.load_fired_state()

        # Never use the forming candle for ORB confirm / volume / HTF bias.
        df = self.completed_bars_only(df)
        if df is None or len(df) < need:
            return "HOLD"

        latest = df.iloc[-1]
        ts = df.index[-1]
        day = self._session_date(ts)
        # Same calendar day bars
        day_mask = [self._session_date(t) == day for t in df.index]
        day_df = df.loc[day_mask]
        levels = self._or_levels(day_df)
        if not levels:
            return "HOLD"
        or_high, or_low = levels

        # Need post-OR bars
        try:
            from zoneinfo import ZoneInfo
            ist = ZoneInfo("Asia/Kolkata")
        except ImportError:
            import pytz  # type: ignore
            ist = pytz.timezone("Asia/Kolkata")
        idx = day_df.index
        local = idx.tz_localize(ist) if getattr(idx, "tz", None) is None else idx.tz_convert(ist)
        minutes = local.hour * 60 + local.minute
        post = day_df.loc[minutes >= (9 * 60 + 15 + self.or_minutes)]
        confirm_n = max(1, int(self.confirm_bars))
        if len(post) < confirm_n:
            return "HOLD"
        confirm = post.iloc[-confirm_n:]

        bias = None
        if self.use_htf and len(df) >= self.htf_ema:
            ema = calc_ema(df["close"], self.htf_ema)
            e = float(ema.iloc[-1]) if ema is not None and not pd.isna(ema.iloc[-1]) else None
            if e is not None:
                last_c = float(latest["close"])
                bias = "BUY" if last_c > e else ("SELL" if last_c < e else None)

        key = self._fire_key(symbol, day=day)
        long_ok = bool((confirm["close"] > or_high).all())
        short_ok = bool((confirm["close"] < or_low).all())

        # Do NOT mark _fired here — only after confirmed fill via mark_day_fired().
        # Failed/rejected buys must be allowed to retry while the breakout holds.
        with _orb_fired_lock:
            fired = _normalize_fired(_orb_fired.get(key))
        fired_side = fired.get("side") if fired else None

        # After the first filled BUY, only a volume-confirmed 0.5% extension
        # above the previous completed 5m high may unlock a second BUY.
        if fired_side == "BUY":
            ok, why = self._orb_continuation_ok(df, fired or {})
            if ok:
                self.last_orb_continuation = True
                self.last_decision = {
                    "symbol": symbol,
                    "signal": "BUY",
                    "reason": why,
                    "orb_continuation": True,
                    "playbook": "orb",
                }
                logger.info(f"[BUY SIGNAL] {symbol} — ORB continuation {why}")
                return "BUY"
            if config.STRICT_SELL:
                rsi_v = float(latest.get(f"RSI_{self.p.rsi_period}", 50) or 50)
                if float(latest["close"]) < or_high and rsi_v > self.p.rsi_sell:
                    return "SELL"
            return "HOLD"

        vol_avg = latest.get("VOL_AVG")
        vol = float(latest.get("volume") or 0)
        if vol_avg is not None and not pd.isna(vol_avg) and float(vol_avg) > 0:
            if vol < float(vol_avg) * self.volume_mult:
                return "HOLD"

        if long_ok and fired_side != "BUY":
            if bias == "SELL":
                return "HOLD"
            self._note_pending_trigger(key, latest)
            logger.info(
                f"[BUY SIGNAL] {symbol} — ORB long break ORH={or_high:.2f} "
                f"confirm={confirm_n} vol_ok"
            )
            return "BUY"

        if self.allow_short and short_ok and fired_side != "SELL":
            if bias == "BUY":
                return "HOLD"
            logger.info(
                f"[SELL SIGNAL] {symbol} — ORB short break ORL={or_low:.2f}"
            )
            return "SELL"

        return "HOLD"


class MisRegimeStrategy(BaseStrategy):
    """
    Regime-based MIS selector. Hard XOR: at most one playbook may BUY
    per symbol per cycle. First qualifier wins; later playbooks are not
    executed for that symbol.

    Precedence (explicit):
      1. OPEN_DRIVE + Improved ORB
         ORB qualifies → BUY and stop.
         ORB fails → HOLD by default. Do NOT fall through to RANGE / MR.
         TREND_UP playbooks run only when TREND_UP is independently true.
      2. TREND_UP + VWAP momentum + RS
      3. TREND_UP + EMA pullback + momentum
      4. RANGE + VWAP mean reversion
      5. HOLD

    TREND_DOWN → HOLD (long-only; ORB_ALLOW_SHORT stays caller-controlled).
    Uses completed 5-minute bars only. No Dhan/order calls.
    """

    name = "mis_regime"

    def __init__(self, params: MarketParams):
        super().__init__(params)
        self.confirm_bars = int(getattr(config, "MIS_REGIME_CONFIRM_BARS", 2) or 2)
        self.volume_mult = float(getattr(config, "MIS_REGIME_VOLUME_MULT", 1.2) or 1.2)
        self.orb_window = int(getattr(config, "ORB_WINDOW_MINUTES", 60) or 60)
        pullback_params = MarketParams(
            sma_slow=params.sma_slow,
            sma_fast=params.sma_fast,
            ema_pullback=params.ema_pullback,
            rsi_period=params.rsi_period,
            rsi_buy=params.rsi_buy,
            rsi_sell=params.rsi_sell,
            bb_std=params.bb_std,
            adx_range_max=params.adx_range_max,
            atr_period=params.atr_period,
            adx_period=params.adx_period,
            volume_avg_period=params.volume_avg_period,
            confirm_bars=self.confirm_bars,
            market=params.market,
        )
        self._orb = OpeningRangeBreakoutStrategy(
            params,
            volume_mult=self.volume_mult,
            confirm_bars=self.confirm_bars,
        )
        self._trend = TrendPullbackStrategy(pullback_params)
        self._rs = RelativeStrengthFilter()
        self.last_decision: dict = {}
        self.last_orb_continuation = False
        logger.info(
            f"[{params.market}] MisRegime | window={self.orb_window}m "
            f"confirm={self.confirm_bars} vol_mult={self.volume_mult} "
            f"adx_range={params.adx_range_max} XOR precedence="
            f"ORB > VWAP_MOMENTUM_RS > EMA_PULLBACK > VWAP_MR"
        )

    def update_universe(self, symbol_dfs: dict) -> None:
        """Rank INDIA_STOCK_UNIVERSE (or current bar_cache) for Playbook 1 RS."""
        self._rs.update_scores(symbol_dfs or {})

    def mark_day_fired(self, symbol: str, side: str = "BUY", day=None, **kwargs) -> None:
        self._orb.mark_day_fired(symbol, side=side, day=day, **kwargs)

    def has_day_fired(self, symbol: str, side: str | None = None, day=None) -> bool:
        return bool(self._orb.has_day_fired(symbol, side=side, day=day))

    def completed_bars_only(
        self, df: pd.DataFrame, now: datetime | None = None
    ) -> pd.DataFrame:
        return completed_bars_only(df, now=now)

    def _record(
        self,
        symbol: str,
        regime: str,
        playbook: str,
        signal: str,
        reason: str,
        evaluations: list[dict],
        ts=None,
        orb_continuation: bool = False,
    ) -> str:
        self.last_orb_continuation = bool(orb_continuation)
        self.last_decision = {
            "symbol": symbol,
            "regime": regime,
            "playbook": playbook,
            "signal": signal,
            "reason": reason,
            "evaluations": list(evaluations),
            "timestamp": str(ts) if ts is not None else None,
            "orb_continuation": bool(orb_continuation),
        }
        eval_txt = "; ".join(
            f"{e.get('playbook')}={e.get('signal')}:{e.get('reason')}"
            for e in evaluations
        ) or "none"
        logger.info(
            f"{symbol} | ts={ts} | regime={regime} | selected={playbook} | "
            f"{signal} | {reason} | evals=[{eval_txt}]"
        )
        return signal

    def _htf_bullish(self, df: pd.DataFrame, close: float) -> bool | None:
        """Optional HTF EMA filter. None = filter disabled / insufficient data."""
        if not bool(getattr(config, "ORB_USE_HTF_FILTER", False)):
            return None
        period = int(getattr(config, "ORB_HTF_EMA_PERIOD", 20) or 20)
        if len(df) < period:
            return None
        ema = calc_ema(df["close"], period)
        e = ema.iloc[-1]
        if e is None or pd.isna(e):
            return None
        return close > float(e)

    def _momentum_falling(self, df: pd.DataFrame) -> bool:
        n = max(2, int(getattr(config, "ADX_RISING_LOOKBACK", 2) or 2))
        if len(df) <= n:
            return False
        return float(df["close"].iloc[-1]) < float(df["close"].iloc[-1 - n])

    def _open_drive_expansion(self, df: pd.DataFrame) -> tuple[bool, str]:
        """
        OPEN_DRIVE requires opening expansion:
          ADX[-1] > ADX[-1 - ADX_RISING_LOOKBACK]
          OR current bar range >= RANGE_EXPANSION_MULT * mean(prior 5 bar ranges)
        """
        lookback = max(1, int(getattr(config, "ADX_RISING_LOOKBACK", 2) or 2))
        mult = float(getattr(config, "RANGE_EXPANSION_MULT", 1.2) or 1.2)
        reasons: list[str] = []
        adx_rising = False
        if "ADX" in df.columns and len(df) > lookback:
            a0 = df["ADX"].iloc[-1]
            a1 = df["ADX"].iloc[-1 - lookback]
            if a0 is not None and a1 is not None and not pd.isna(a0) and not pd.isna(a1):
                adx_rising = float(a0) > float(a1)
                if adx_rising:
                    reasons.append("adx_rising")
        range_expanding = False
        if len(df) >= 6:
            bar_range = df["high"].astype(float) - df["low"].astype(float)
            recent = float(bar_range.iloc[-1])
            base = float(bar_range.iloc[-6:-1].mean())
            if base > 0 and recent >= mult * base:
                range_expanding = True
                reasons.append("range_expanding")
        ok = adx_rising or range_expanding
        return ok, "+".join(reasons) if reasons else "no_expansion"

    def _trend_up_clear(self, close: float, vwap: float, adx: float, df: pd.DataFrame) -> bool:
        """TREND_UP must be independently and clearly true (not merely morning hours)."""
        if adx < self.p.adx_range_max:
            return False
        if close <= vwap:
            return False
        htf = self._htf_bullish(df, close)
        if htf is False:
            return False
        return not self._momentum_falling(df)

    def classify_regime(
        self,
        df: pd.DataFrame,
        vwap: float,
        now: datetime | None = None,
        *,
        market: str | None = None,
    ) -> tuple[str, str]:
        """
        Regime precedence (explicit):
          1. OPEN_DRIVE — in ORB window AND expansion evidence
          2. TREND_UP   — ADX >= max AND close > VWAP AND trend confirmed
          3. TREND_DOWN — ADX >= max AND close < VWAP AND momentum falling
          4. RANGE      — ADX < max
        High-ADX mixed tape (not clearly up or down) is treated as TREND_DOWN
        for long-only (HOLD) when close < VWAP, else not TREND_UP.
        """
        mkt = str(market or getattr(self.p, "market", "INDIA") or "INDIA").upper()
        if now is None:
            now = market_now(mkt)
        latest = df.iloc[-1]
        close = float(latest["close"])
        adx_raw = latest.get("ADX")
        adx = float(adx_raw) if adx_raw is not None and not pd.isna(adx_raw) else 0.0

        in_window = in_open_drive_window(now, self.orb_window, market=mkt)
        expansion_ok, exp_why = self._open_drive_expansion(df)
        if in_window and expansion_ok:
            return REGIME_OPEN_DRIVE, f"window+{exp_why}"
        if in_window and not expansion_ok:
            logger.debug(f"OPEN_DRIVE window but {exp_why} — not classifying OPEN_DRIVE")

        if self._trend_up_clear(close, vwap, adx, df):
            return REGIME_TREND_UP, f"adx={adx:.1f} close>vwap"
        if adx >= self.p.adx_range_max and close < vwap and self._momentum_falling(df):
            return REGIME_TREND_DOWN, f"adx={adx:.1f} close<vwap momentum_falling"
        if adx < self.p.adx_range_max:
            return REGIME_RANGE, f"adx={adx:.1f}<{self.p.adx_range_max}"
        if close < vwap:
            return REGIME_TREND_DOWN, f"adx={adx:.1f} close<vwap mixed"
        return REGIME_TREND_UP, f"adx={adx:.1f} close>vwap unconfirmed_htf"

    def _playbook_orb(self, df: pd.DataFrame, symbol: str) -> tuple[str, str]:
        sig = self._orb.generate_signal(df, symbol)
        if sig == "BUY":
            if getattr(self._orb, "last_orb_continuation", False):
                return "BUY", "orb_continuation"
            return "BUY", "orb_breakout"
        if sig == "SELL":
            return "HOLD", "orb_sell_ignored_long_only"
        return "HOLD", "orb_no_setup"

    def _rs_is_leader(self, symbol: str) -> bool:
        """Playbook 1 always requires its own RS ranking (not USE_RELATIVE_STRENGTH)."""
        if not self._rs._scores:
            return False
        ranked = sorted(self._rs._scores.items(), key=lambda x: x[1], reverse=True)
        top_n = max(1, int(self._rs.top_n))
        top_symbols = {s for s, _ in ranked[:top_n]}
        return symbol in top_symbols

    def _playbook_vwap_momentum_rs(
        self, df: pd.DataFrame, symbol: str, vwap: float
    ) -> tuple[str, str]:
        latest = df.iloc[-1]
        close = float(latest["close"])
        if close <= vwap:
            return "HOLD", "close_not_above_vwap"
        n = max(2, int(getattr(config, "MOMENTUM_LOOKBACK_BARS", 20) or 20))
        if len(df) < n + 1:
            return "HOLD", "not_enough_bars_momentum"
        prior_high = float(df["high"].iloc[-(n + 1) : -1].max())
        if close <= prior_high:
            return "HOLD", f"no_break_prior_high={prior_high:.2f}"
        vol = float(latest.get("volume") or 0)
        vol_avg = latest.get("VOL_AVG")
        if vol_avg is None or pd.isna(vol_avg) or float(vol_avg) <= 0:
            return "HOLD", "no_vol_avg"
        if vol < float(vol_avg) * self.volume_mult:
            return "HOLD", f"volume<{self.volume_mult:.2f}x_avg"
        if not self._rs_is_leader(symbol):
            return "HOLD", f"not_rs_top_{self._rs.top_n}"
        return "BUY", f"close>{prior_high:.2f} vol_ok rs_leader"

    def _is_extended(self, close: float, vwap: float, ema, atr) -> bool:
        if self.p.market == "US":
            k = float(getattr(config, "US_EMA_EXTENSION_ATR", 1.2) or 1.2)
        else:
            k = float(getattr(config, "EMA_EXTENSION_ATR", 1.0) or 1.0)
        if atr is None or pd.isna(atr) or float(atr) <= 0:
            return False
        atr_f = float(atr)
        if close > vwap + k * atr_f:
            return True
        if ema is not None and not pd.isna(ema) and close > float(ema) + k * atr_f:
            return True
        return False

    def _had_pullback(self, df: pd.DataFrame, vwap_series: pd.Series) -> bool:
        ema_c = f"EMA_{self.p.ema_pullback}"
        look_n = min(5, len(df))
        look = df.iloc[-look_n:]
        ema = look.get(ema_c)
        near_ema = False
        if ema is not None:
            near_ema = bool((look["close"].astype(float) <= ema.astype(float) * 1.01).any())
        vwap_look = vwap_series.reindex(look.index)
        near_vwap = bool(
            ((look["close"].astype(float) <= vwap_look.astype(float) * 1.005).fillna(False)).any()
        )
        return near_ema or near_vwap

    def _playbook_ema_pullback(
        self, df: pd.DataFrame, symbol: str, vwap: float, vwap_series: pd.Series
    ) -> tuple[str, str]:
        latest = df.iloc[-1]
        close = float(latest["close"])
        ema = latest.get(f"EMA_{self.p.ema_pullback}")
        atr = latest.get("ATR")
        if self._is_extended(close, vwap, ema, atr):
            return "HOLD", "extended_wait_pullback"
        if not self._had_pullback(df, vwap_series):
            return "HOLD", "no_pullback"
        sig = self._trend.generate_signal(df, symbol)
        if sig == "BUY":
            return "BUY", "pullback_momentum_resume"
        return "HOLD", "pullback_not_confirmed"

    def _playbook_vwap_mr(
        self, df: pd.DataFrame, symbol: str, vwap: float
    ) -> tuple[str, str]:
        latest = df.iloc[-1]
        close = float(latest["close"])
        adx_raw = latest.get("ADX")
        atr_raw = latest.get("ATR")
        rsi_c = f"RSI_{self.p.rsi_period}"
        rsi_raw = latest.get(rsi_c)
        if any(x is None or pd.isna(x) for x in (adx_raw, atr_raw, rsi_raw)):
            return "HOLD", "missing_indicators"
        adx = float(adx_raw)
        atr = float(atr_raw)
        rsi = float(rsi_raw)
        if adx >= self.p.adx_range_max:
            return "HOLD", f"adx={adx:.1f}_not_range"
        if atr <= 0:
            return "HOLD", "atr_invalid"
        if self.p.market == "US":
            stretch_k = float(getattr(config, "US_VWAP_STRETCH_ATR", 0.7) or 0.7)
            rsi_os = float(getattr(config, "US_RSI_OVERSOLD", 42) or 42)
            reclaim_n = max(1, int(getattr(config, "US_VWAP_RECLAIM_BARS", 1) or 1))
        else:
            stretch_k = float(getattr(config, "VWAP_STRETCH_ATR", 1.0) or 1.0)
            rsi_os = float(getattr(config, "RSI_OVERSOLD", 30) or 30)
            reclaim_n = max(1, int(getattr(config, "VWAP_RECLAIM_BARS", 1) or 1))
        threshold = vwap - stretch_k * atr
        if close > threshold:
            return "HOLD", f"weak_stretch close={close:.2f} > {threshold:.2f}"
        if rsi > rsi_os:
            return "HOLD", f"rsi={rsi:.1f}>{rsi_os:.0f}"
        if len(df) < reclaim_n + 1:
            return "HOLD", "not_enough_bars_reclaim"
        # Reclaim: latest completed close > previous completed candle high
        prev_high = float(df["high"].iloc[-1 - reclaim_n])
        if close <= prev_high:
            return "HOLD", f"no_reclaim close<={prev_high:.2f}"
        return "BUY", f"stretch+rsi={rsi:.1f}+reclaim"

    def _loss_reentry_blocked(self, symbol: str, now: datetime) -> tuple[bool, str]:
        mins = int(getattr(config, "LOSS_REENTRY_COOLDOWN_MIN", 0) or 0)
        if mins <= 0:
            return False, ""
        try:
            import trade_journal

            rows = trade_journal.entries_today("INDIA", symbol=symbol)
        except Exception as e:
            logger.debug(f"{symbol}: loss-reentry check skip: {e}")
            return False, ""
        cutoff = now.timestamp() - mins * 60 if hasattr(now, "timestamp") else None
        for row in rows or []:
            if str(row.get("status") or "").lower() != "closed":
                continue
            pnl = row.get("pnl")
            try:
                if pnl is None or float(pnl) >= 0:
                    continue
            except (TypeError, ValueError):
                continue
            closed_at = str(row.get("closed_at") or "")
            if not closed_at:
                return True, f"loss_reentry_cooldown_{mins}m"
            try:
                ts = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                if cutoff is None or ts.timestamp() >= cutoff:
                    return True, f"loss_reentry_cooldown_{mins}m"
            except Exception:
                return True, f"loss_reentry_cooldown_{mins}m"
        return False, ""

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        if config.TEST_MODE:
            logger.error(f"{symbol}: TEST_MODE on — refusing signal.")
            return self._record(symbol, PLAYBOOK_NONE, PLAYBOOK_NONE, "HOLD", "test_mode", [])

        market = str(getattr(self.p, "market", "INDIA") or "INDIA").upper()
        now = market_now(market)
        cutoff_raw = entry_cutoff_for_market(market)
        if df is None or df.empty:
            return self._record(symbol, PLAYBOOK_NONE, PLAYBOOK_NONE, "HOLD", "no_data", [])

        df = self.completed_bars_only(df, now=now)
        need = max(30, self.p.adx_period + 5, self.p.atr_period + 5)
        if df is None or len(df) < need:
            return self._record(
                symbol, PLAYBOOK_NONE, PLAYBOOK_NONE, "HOLD", "not_enough_completed_bars", []
            )
        if "ADX" not in df.columns or "VOL_AVG" not in df.columns:
            df = self.compute_indicators(df)

        if market == "US":
            vwap_series = calc_session_vwap(
                df, session_open_hm=(9, 30), tz=_et_tz()
            )
        else:
            vwap_series = calc_session_vwap(df)
        vwap_raw = vwap_series.iloc[-1] if len(vwap_series) else np.nan
        if vwap_raw is None or pd.isna(vwap_raw) or float(vwap_raw) <= 0:
            return self._record(symbol, PLAYBOOK_NONE, PLAYBOOK_NONE, "HOLD", "no_session_vwap", [])
        vwap = float(vwap_raw)
        ts = df.index[-1] if len(df.index) else None

        regime, regime_why = self.classify_regime(df, vwap, now=now, market=market)
        evaluations: list[dict] = []

        def eval_one(playbook: str, pair: tuple[str, str]) -> str:
            sig, why = pair
            evaluations.append({"playbook": playbook, "signal": sig, "reason": why})
            return sig

        if past_entry_cutoff(now, market=market, cutoff=cutoff_raw):
            label = "US_ENTRY_CUTOFF" if market == "US" else "ENTRY_CUTOFF"
            return self._record(
                symbol,
                regime,
                PLAYBOOK_NONE,
                "HOLD",
                f"past {label}={cutoff_raw}",
                evaluations,
                ts=ts,
            )

        blocked, block_why = self._loss_reentry_blocked(symbol, now)
        if blocked:
            return self._record(
                symbol, regime, PLAYBOOK_NONE, "HOLD", block_why, evaluations, ts=ts
            )

        latest = df.iloc[-1]
        close = float(latest["close"])
        adx_raw = latest.get("ADX")
        adx = float(adx_raw) if adx_raw is not None and not pd.isna(adx_raw) else 0.0
        trend_up_ok = self._trend_up_clear(close, vwap, adx, df)

        # Second ORB may fire after OPEN_DRIVE if day-fired + 0.5%/volume continuation.
        if self.has_day_fired(symbol, "BUY"):
            orb_pair = self._playbook_orb(df, symbol)
            if (
                orb_pair[0] == "BUY"
                and getattr(self._orb, "last_orb_continuation", False)
            ):
                eval_one(PLAYBOOK_ORB, orb_pair)
                return self._record(
                    symbol,
                    regime,
                    PLAYBOOK_ORB,
                    "BUY",
                    f"{regime_why}; orb_continuation",
                    evaluations,
                    ts=ts,
                    orb_continuation=True,
                )

        # ---- 1. OPEN_DRIVE + Improved ORB ----
        if regime == REGIME_OPEN_DRIVE:
            if eval_one(PLAYBOOK_ORB, self._playbook_orb(df, symbol)) == "BUY":
                return self._record(
                    symbol, regime, PLAYBOOK_ORB, "BUY",
                    f"{regime_why}; orb", evaluations, ts=ts,
                )
            # ORB failed: HOLD unless TREND_UP is independently and clearly true.
            # Do not fall through to RANGE / mean-reversion in the same cycle.
            if not trend_up_ok:
                return self._record(
                    symbol, regime, PLAYBOOK_NONE, "HOLD",
                    f"open_drive_orb_fail_no_trend_up_fallback ({regime_why})",
                    evaluations, ts=ts,
                )
            if eval_one(
                PLAYBOOK_MOMENTUM, self._playbook_vwap_momentum_rs(df, symbol, vwap)
            ) == "BUY":
                return self._record(
                    symbol, regime, PLAYBOOK_MOMENTUM, "BUY",
                    "open_drive_fallback_trend_up_momentum", evaluations, ts=ts,
                )
            if eval_one(
                PLAYBOOK_PULLBACK,
                self._playbook_ema_pullback(df, symbol, vwap, vwap_series),
            ) == "BUY":
                return self._record(
                    symbol, regime, PLAYBOOK_PULLBACK, "BUY",
                    "open_drive_fallback_trend_up_pullback", evaluations, ts=ts,
                )
            return self._record(
                symbol, regime, PLAYBOOK_NONE, "HOLD",
                "open_drive_orb_fail_trend_up_playbooks_hold", evaluations, ts=ts,
            )

        # ---- 2/3. TREND_UP playbooks (XOR: momentum then pullback) ----
        if regime == REGIME_TREND_UP:
            if not trend_up_ok:
                return self._record(
                    symbol, regime, PLAYBOOK_NONE, "HOLD",
                    f"trend_up_not_independently_clear ({regime_why})",
                    evaluations, ts=ts,
                )
            if eval_one(
                PLAYBOOK_MOMENTUM, self._playbook_vwap_momentum_rs(df, symbol, vwap)
            ) == "BUY":
                return self._record(
                    symbol, regime, PLAYBOOK_MOMENTUM, "BUY",
                    "trend_up_momentum_rs", evaluations, ts=ts,
                )
            if eval_one(
                PLAYBOOK_PULLBACK,
                self._playbook_ema_pullback(df, symbol, vwap, vwap_series),
            ) == "BUY":
                return self._record(
                    symbol, regime, PLAYBOOK_PULLBACK, "BUY",
                    "trend_up_ema_pullback", evaluations, ts=ts,
                )
            return self._record(
                symbol, regime, PLAYBOOK_NONE, "HOLD",
                "trend_up_no_playbook", evaluations, ts=ts,
            )

        # ---- TREND_DOWN: long-only → HOLD ----
        if regime == REGIME_TREND_DOWN:
            return self._record(
                symbol, regime, PLAYBOOK_NONE, "HOLD",
                f"trend_down_no_shorts ({regime_why})", evaluations, ts=ts,
            )

        # ---- 4. RANGE + VWAP mean reversion only (no ORB / momentum chase) ----
        if regime == REGIME_RANGE:
            if eval_one(
                PLAYBOOK_MR, self._playbook_vwap_mr(df, symbol, vwap)
            ) == "BUY":
                return self._record(
                    symbol, regime, PLAYBOOK_MR, "BUY",
                    "range_vwap_mean_reversion", evaluations, ts=ts,
                )
            return self._record(
                symbol, regime, PLAYBOOK_NONE, "HOLD",
                "range_mean_reversion_weak", evaluations, ts=ts,
            )

        return self._record(
            symbol, regime, PLAYBOOK_NONE, "HOLD",
            f"no_playbook ({regime_why})", evaluations, ts=ts,
        )


def snapshot_signal(strategy: BaseStrategy, df: pd.DataFrame, symbol: str) -> dict:
    """Dashboard-friendly signal payload with a short reason."""
    signal = strategy.generate_signal(df, symbol)
    latest = df.iloc[-1]
    rsi_c = f"RSI_{strategy.p.rsi_period}"
    price = float(latest["close"])
    rsi = latest.get(rsi_c)
    adx = latest.get("ADX")
    reason = "no setup"
    if signal == "BUY":
        reason = f"{strategy.name} entry"
    elif signal == "SELL":
        reason = "overbought / exit rule"
    elif rsi is not None and not pd.isna(rsi) and float(rsi) > strategy.p.rsi_sell:
        reason = "HOLD — elevated RSI, waiting"
    elif adx is not None and not pd.isna(adx):
        reason = f"HOLD — scanning ({strategy.name}, ADX={float(adx):.0f})"
    else:
        reason = f"HOLD — scanning ({strategy.name})"
    last = getattr(strategy, "last_decision", None)
    regime = None
    playbook = None
    orb_continuation = False
    if isinstance(last, dict) and str(last.get("symbol") or "").upper() == str(symbol).upper():
        if last.get("reason"):
            reason = str(last["reason"])
        regime = last.get("regime")
        playbook = last.get("playbook")
        orb_continuation = bool(last.get("orb_continuation"))
    if not orb_continuation:
        orb_continuation = bool(getattr(strategy, "last_orb_continuation", False))
        inner = (
            getattr(strategy, "inner", None)
            or getattr(strategy, "_orb", None)
            or getattr(strategy, "_delegate", None)
        )
        if inner is not None:
            orb_continuation = orb_continuation or bool(
                getattr(inner, "last_orb_continuation", False)
            )
    return {
        "symbol": symbol,
        "signal": signal,
        "price": round(price, 2),
        "rsi": round(float(rsi), 1) if rsi is not None and not pd.isna(rsi) else None,
        "adx": round(float(adx), 1) if adx is not None and not pd.isna(adx) else None,
        "reason": reason,
        "strategy": strategy.name,
        "regime": regime,
        "playbook": playbook,
        "orb_continuation": bool(orb_continuation),
    }


# ---------------------------------------------------------------------------
# Relative Strength filter (optional overlay)
# ---------------------------------------------------------------------------
class RelativeStrengthFilter:
    """
    Rank universe by lookback return; only allow entries in top-N names.
    Applied as a gate around any base strategy.
    """

    def __init__(self, top_n: int | None = None, lookback: int | None = None):
        self.top_n = top_n if top_n is not None else config.RS_TOP_N
        self.lookback = lookback if lookback is not None else config.RS_LOOKBACK_BARS
        self._scores: dict[str, float] = {}

    def update_scores(self, symbol_dfs: dict[str, pd.DataFrame]):
        scores = {}
        for symbol, df in symbol_dfs.items():
            if df is None or len(df) < self.lookback + 1:
                continue
            start = float(df["close"].iloc[-(self.lookback + 1)])
            end = float(df["close"].iloc[-1])
            if start > 0:
                scores[symbol] = (end / start) - 1.0
        self._scores = scores
        if scores:
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            top = ranked[: self.top_n]
            logger.info(
                f"RS ranking top-{self.top_n}: "
                + ", ".join(f"{s}={r:.1%}" for s, r in top)
            )

    def allows(self, symbol: str) -> bool:
        if not self._scores:
            return True  # no data yet — don't block
        ranked = sorted(self._scores.items(), key=lambda x: x[1], reverse=True)
        top_symbols = {s for s, _ in ranked[: self.top_n]}
        ok = symbol in top_symbols
        if not ok:
            logger.info(f"{symbol}: blocked by relative-strength filter (not top-{self.top_n})")
        return ok

    def score(self, symbol: str) -> Optional[float]:
        return self._scores.get(symbol)


class FilteredStrategy(BaseStrategy):
    """Wraps a base strategy with an optional RS gate on BUY only."""

    def __init__(self, inner: BaseStrategy, rs_filter: RelativeStrengthFilter | None):
        super().__init__(inner.p)
        self.inner = inner
        self.rs = rs_filter
        self.name = inner.name + ("+rs" if rs_filter else "")

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.inner.compute_indicators(df)

    def latest_atr(self, df: pd.DataFrame) -> Optional[float]:
        return self.inner.latest_atr(df)

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        signal = self.inner.generate_signal(df, symbol)
        if hasattr(self.inner, "last_decision") and isinstance(self.inner.last_decision, dict):
            self.last_decision = dict(self.inner.last_decision)
        self.last_orb_continuation = bool(
            getattr(self.inner, "last_orb_continuation", False)
        )
        if signal == "BUY" and self.rs is not None and not self.rs.allows(symbol):
            self.last_orb_continuation = False
            return "HOLD"
        return signal

    def mark_day_fired(self, symbol: str, side: str = "BUY", day=None, **kwargs) -> None:
        inner = getattr(self, "inner", None)
        if inner is not None and hasattr(inner, "mark_day_fired"):
            inner.mark_day_fired(symbol, side=side, day=day, **kwargs)

    def has_day_fired(self, symbol: str, side: str | None = None, day=None) -> bool:
        inner = getattr(self, "inner", None)
        if inner is not None and hasattr(inner, "has_day_fired"):
            return bool(inner.has_day_fired(symbol, side=side, day=day))
        return False

    def update_universe(self, symbol_dfs: dict) -> None:
        inner = getattr(self, "inner", None)
        if inner is not None and hasattr(inner, "update_universe"):
            inner.update_universe(symbol_dfs)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_strategy(
    market: str = "US",
    name: str | None = None,
    rs_filter: RelativeStrengthFilter | None = None,
) -> BaseStrategy:
    """
    Build the configured strategy for a market.

    Args:
        market: "US" or "INDIA"
        name: override STRATEGY_NAME
        rs_filter: optional shared RS filter instance
    """
    strategy_name = (name or config.STRATEGY_NAME).strip().lower()
    params = params_for_market(market)

    if strategy_name in _MIS_REGIME_NAMES:
        base: BaseStrategy = MisRegimeStrategy(params)
    elif strategy_name in ("opening_range_breakout", "orb", "opening_range", "mis_orb"):
        base = OpeningRangeBreakoutStrategy(params)
    elif strategy_name in ("mean_reversion", "mean-reversion", "mr"):
        base = MeanReversionStrategy(params)
    elif strategy_name in ("regime_adaptive", "regime", "adaptive", "hybrid"):
        base = RegimeAdaptiveStrategy(params)
    elif strategy_name in ("breakout", "donchian", "momentum"):
        base = BreakoutStrategy(params)
    elif strategy_name in ("trend_pullback", "trend-pullback", "tp", "primary"):
        base = TrendPullbackStrategy(params)
    else:
        logger.warning(
            f"Unknown STRATEGY_NAME={strategy_name!r} — using opening_range_breakout"
        )
        base = OpeningRangeBreakoutStrategy(params)

    # mis_regime Playbook 1 applies its own RS ranking. Do not wrap with the
    # global USE_RELATIVE_STRENGTH gate (that remains a legacy-strategy overlay).
    if strategy_name in _MIS_REGIME_NAMES:
        return base

    use_rs = config.USE_RELATIVE_STRENGTH or rs_filter is not None
    if use_rs:
        filt = rs_filter or RelativeStrengthFilter()
        return FilteredStrategy(base, filt)
    return base


# Backward-compatible alias used by older imports / dashboard
class Strategy(TrendPullbackStrategy):
    """Legacy name — uses US params + configured STRATEGY_NAME via factory preferred."""

    def __init__(self, market: str = "US"):
        # Delegate construction through factory so STRATEGY_NAME is honored
        built = create_strategy(market=market)
        # Copy state for isinstance-compat callers that expect Strategy()
        super().__init__(built.p if hasattr(built, "p") else params_for_market(market))
        self._delegate = built
        self.name = built.name

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._delegate.compute_indicators(df)

    def generate_signal(self, df: pd.DataFrame, symbol: str) -> str:
        signal = self._delegate.generate_signal(df, symbol)
        if hasattr(self._delegate, "last_decision") and isinstance(
            self._delegate.last_decision, dict
        ):
            self.last_decision = dict(self._delegate.last_decision)
        self.last_orb_continuation = bool(
            getattr(self._delegate, "last_orb_continuation", False)
        )
        return signal

    def latest_atr(self, df: pd.DataFrame) -> Optional[float]:
        return self._delegate.latest_atr(df)

    def mark_day_fired(self, symbol: str, side: str = "BUY", day=None, **kwargs) -> None:
        if hasattr(self._delegate, "mark_day_fired"):
            self._delegate.mark_day_fired(symbol, side=side, day=day, **kwargs)

    def has_day_fired(self, symbol: str, side: str | None = None, day=None) -> bool:
        if hasattr(self._delegate, "has_day_fired"):
            return bool(self._delegate.has_day_fired(symbol, side=side, day=day))
        return False

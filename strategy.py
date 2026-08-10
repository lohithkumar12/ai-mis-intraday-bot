"""
strategy.py — Pluggable Trading Strategies
============================================
Selectable via config.STRATEGY_NAME:

  A) opening_range_breakout / orb (MIS DEFAULT)
     - First OR_MINUTES after 09:15 IST → OR high/low
     - BUY when CONFIRM_BARS close above OR high + soft volume
     - Optional EMA trend filter on recent closes

  B) trend_pullback (CNC-style; available)
     - LONG only if price > SMA200
     - Enter on pullback to SMA20/EMA21 OR RSI recovery from oversold

  C) mean_reversion / regime_adaptive / breakout — same as New_StartUp

Same interface for India (and US if enabled); params from market-specific config.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


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

    def __init__(self, params: MarketParams):
        super().__init__(params)
        self.or_minutes = int(getattr(config, "OR_MINUTES", 15) or 15)
        self.volume_mult = float(getattr(config, "VOLUME_MULT", 0.8) or 0.8)
        self.allow_short = bool(getattr(config, "ORB_ALLOW_SHORT", False))
        self.use_htf = bool(getattr(config, "ORB_USE_HTF_FILTER", True))
        self.htf_ema = int(getattr(config, "ORB_HTF_EMA_PERIOD", 20) or 20)
        self._fired: dict[str, str] = {}
        logger.info(
            f"[{params.market}] ORB | or={self.or_minutes}m confirm={config.CONFIRM_BARS} "
            f"vol_mult={self.volume_mult} htf_ema={self.htf_ema if self.use_htf else 'off'}"
        )

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
        if config.TEST_MODE:
            return "HOLD"
        need = max(30, config.ATR_PERIOD + 5, self.htf_ema + 2)
        if df is None or len(df) < need:
            return "HOLD"
        if "VOL_AVG" not in df.columns:
            df = self.compute_indicators(df)

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
        confirm_n = max(1, int(config.CONFIRM_BARS))
        if len(post) < confirm_n:
            return "HOLD"
        confirm = post.iloc[-confirm_n:]

        vol_avg = latest.get("VOL_AVG")
        vol = float(latest.get("volume") or 0)
        if vol_avg is not None and not pd.isna(vol_avg) and float(vol_avg) > 0:
            if vol < float(vol_avg) * self.volume_mult:
                return "HOLD"

        bias = None
        if self.use_htf and len(df) >= self.htf_ema:
            ema = calc_ema(df["close"], self.htf_ema)
            e = float(ema.iloc[-1]) if ema is not None and not pd.isna(ema.iloc[-1]) else None
            if e is not None:
                last_c = float(latest["close"])
                bias = "BUY" if last_c > e else ("SELL" if last_c < e else None)

        key = f"{symbol}:{day}"
        long_ok = bool((confirm["close"] > or_high).all())
        short_ok = bool((confirm["close"] < or_low).all())

        if long_ok and self._fired.get(key) != "BUY":
            if bias == "SELL":
                return "HOLD"
            self._fired[key] = "BUY"
            logger.info(
                f"[BUY SIGNAL] {symbol} — ORB long break ORH={or_high:.2f} "
                f"confirm={confirm_n} vol_ok"
            )
            return "BUY"

        if self.allow_short and short_ok and self._fired.get(key) != "SELL":
            if bias == "BUY":
                return "HOLD"
            self._fired[key] = "SELL"
            logger.info(
                f"[SELL SIGNAL] {symbol} — ORB short break ORL={or_low:.2f}"
            )
            return "SELL"

        # Soft exit: close back inside range after we fired long (optional)
        if config.STRICT_SELL and self._fired.get(key) == "BUY":
            if float(latest["close"]) < or_high and float(latest.get(f"RSI_{self.p.rsi_period}", 50) or 50) > self.p.rsi_sell:
                return "SELL"
        return "HOLD"


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
    return {
        "symbol": symbol,
        "signal": signal,
        "price": round(price, 2),
        "rsi": round(float(rsi), 1) if rsi is not None and not pd.isna(rsi) else None,
        "adx": round(float(adx), 1) if adx is not None and not pd.isna(adx) else None,
        "reason": reason,
        "strategy": strategy.name,
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
        if signal == "BUY" and self.rs is not None and not self.rs.allows(symbol):
            return "HOLD"
        return signal


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

    if strategy_name in ("opening_range_breakout", "orb", "opening_range", "mis_orb"):
        base: BaseStrategy = OpeningRangeBreakoutStrategy(params)
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
        return self._delegate.generate_signal(df, symbol)

    def latest_atr(self, df: pd.DataFrame) -> Optional[float]:
        return self._delegate.latest_atr(df)

"""
config.py — Central Configuration Module
==========================================
MIS / INTRADAY India equity bot (same architecture as New_StartUp CNC bot).

  India → Dhan (default) or Angel One — paper sim or live INR
  Product → INTRADAY (MIS) only — flat by square-off; no overnight CNC

US / F&O / MCX / Currency segments stay in the shared code shape but are
DISABLED by default for this MIS project.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _env_bool(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes")


def _env_float(key: str, default: str) -> float:
    return float(os.getenv(key, default))


def _env_int(key: str, default: str) -> int:
    return int(os.getenv(key, default))


# ===========================================================================
# Live Trading Safety Gate (India real money ONLY)
# ===========================================================================
# Real MIS orders ONLY when BOTH are set:
#   LIVE_TRADING=true
#   LIVE_CONFIRM=YES_REAL_MONEY
LIVE_TRADING: bool = _env_bool("LIVE_TRADING", "false")
LIVE_CONFIRM: str = os.getenv("LIVE_CONFIRM", "").strip()
LIVE_CONFIRMED: bool = LIVE_TRADING and LIVE_CONFIRM == "YES_REAL_MONEY"

TEST_MODE: bool = False

# ===========================================================================
# Market Toggles
# ===========================================================================
# India paper sim uses LIVE broker quotes/candles but places NO real orders.
# Forced OFF automatically when LIVE_CONFIRMED (real money).
_INDIA_PAPER_ENV: bool = _env_bool("INDIA_PAPER", "true")
INDIA_PAPER: bool = _INDIA_PAPER_ENV and not LIVE_CONFIRMED

INDIA_PAPER_STARTING_CASH: float = _env_float("INDIA_PAPER_STARTING_CASH", "150000")



# ===========================================================================
# India Market — Dhan (preferred) or Angel One
# ===========================================================================
# Broker selector: "dhan" | "angel"
# If unset: prefer dhan when Dhan client id is present, else angel.
_INDIA_BROKER_ENV: str = os.getenv("INDIA_BROKER", "").strip().lower()

DHAN_CLIENT_ID: str = os.getenv("DHAN_CLIENT_ID", "").strip()
DHAN_ACCESS_TOKEN: str = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
DHAN_PIN: str = os.getenv("DHAN_PIN", "").strip()
DHAN_TOTP_SECRET: str = os.getenv("DHAN_TOTP_SECRET", "").strip()
DHAN_API_KEY: str = os.getenv("DHAN_API_KEY", "").strip()
DHAN_API_SECRET: str = os.getenv("DHAN_API_SECRET", "").strip()

DHAN_CONFIGURED: bool = bool(
    DHAN_CLIENT_ID
    and (
        DHAN_ACCESS_TOKEN
        or (DHAN_PIN and DHAN_TOTP_SECRET)
    )
)

ANGEL_API_KEY: str = os.getenv("ANGEL_API_KEY", "").strip()
ANGEL_CLIENT_ID: str = os.getenv("ANGEL_CLIENT_ID", "").strip()
ANGEL_PIN: str = os.getenv("ANGEL_PIN", "").strip()
ANGEL_TOTP_SECRET: str = os.getenv("ANGEL_TOTP_SECRET", "").strip()

ANGEL_CONFIGURED: bool = bool(
    ANGEL_API_KEY and ANGEL_CLIENT_ID and ANGEL_PIN and ANGEL_TOTP_SECRET
)

if _INDIA_BROKER_ENV in ("dhan", "angel"):
    INDIA_BROKER: str = _INDIA_BROKER_ENV
elif DHAN_CONFIGURED:
    INDIA_BROKER = "dhan"
elif ANGEL_CONFIGURED:
    INDIA_BROKER = "angel"
else:
    INDIA_BROKER = "dhan"

INDIA_ENABLED: bool = (
    (INDIA_BROKER == "dhan" and DHAN_CONFIGURED)
    or (INDIA_BROKER == "angel" and ANGEL_CONFIGURED)
)

# Product Type — this MIS bot defaults to INTRADAY (aliases: MIS, INTRA)
INDIA_PRODUCT_TYPE: str = os.getenv("INDIA_PRODUCT_TYPE", "INTRADAY").strip().upper()

# Soft buying-power / sizing sleeve (₹). When > 0, India sizing + daily DD
# for entries use min(account_equity, INDIA_CAPITAL_CAP) as the risk base —
# so a larger Dhan book (e.g. leftover CNC cash) does not oversize MIS.
INDIA_CAPITAL_CAP: float = _env_float("INDIA_CAPITAL_CAP", "150000")

# Dhan Paid Data API Live Feed / WebSocket Toggle (India NSE/MCX/FX MarketFeed)
DHAN_LIVE_WEBSOCKET: bool = _env_bool("DHAN_LIVE_WEBSOCKET", "true") if DHAN_CONFIGURED else False

# Dhan Global Stocks Live Feed (US equities via GlobalStocksFeed / INX_EQ)
# Separate socket from India MarketFeed; requires Global Stocks activated + dhanhq>=2.3.0rc1
DHAN_US_LIVE_WEBSOCKET: bool = (
    _env_bool("DHAN_US_LIVE_WEBSOCKET", "true") if DHAN_CONFIGURED else False
)

INDIA_STOCK_UNIVERSE: list[str] = [
    s.strip().upper()
    for s in os.getenv(
        "INDIA_STOCK_UNIVERSE",
        "RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK,HINDUNILVR,ITC,SBIN,BHARTIARTL,LT,KOTAKBANK,WIPRO",
    ).split(",")
    if s.strip()
]

# India scout universe (~Nifty 50 liquid names) — TRADE-ELIGIBLE.
# Core INDIA_STOCK_UNIVERSE stays on Strategy Scanner (faster loop).
# Scout auto-buys on full trend_pullback + risk/session/regime/RS gates.
# Near-setups panel shows close-but-not-confirmed names only.
INDIA_SCOUT_ENABLED: bool = _env_bool("INDIA_SCOUT_ENABLED", "true")
_INDIA_SCOUT_UNIVERSE_RAW = os.getenv("INDIA_SCOUT_UNIVERSE", "").strip()
INDIA_SCOUT_UNIVERSE: list[str] = (
    [s.strip().upper() for s in _INDIA_SCOUT_UNIVERSE_RAW.split(",") if s.strip()]
    if _INDIA_SCOUT_UNIVERSE_RAW
    else []  # empty → india_scout.DEFAULT_INDIA_SCOUT_UNIVERSE
)
INDIA_SCOUT_INTERVAL_SEC: int = _env_int("INDIA_SCOUT_INTERVAL_SEC", "600")  # 10 min
INDIA_SCOUT_TOP_N: int = _env_int("INDIA_SCOUT_TOP_N", "10")
INDIA_SCOUT_MIN_SCORE: float = _env_float("INDIA_SCOUT_MIN_SCORE", "35")
INDIA_SCOUT_FETCH_GAP_SEC: float = _env_float("INDIA_SCOUT_FETCH_GAP_SEC", "0.4")
# RS gate for scout loop only (core India loop still uses RS_TOP_N). Wider list → higher N.
INDIA_SCOUT_RS_TOP_N: int = _env_int("INDIA_SCOUT_RS_TOP_N", "10")
# Auto-buy scout-only names on full BUY signal (default ON). Core 12 still traded by India loop.
INDIA_SCOUT_AUTO_BUY: bool = _env_bool("INDIA_SCOUT_AUTO_BUY", "false")
# Deprecated alias — prefer INDIA_SCOUT_AUTO_BUY
INDIA_SCOUT_AUTO_PROMOTE: bool = _env_bool("INDIA_SCOUT_AUTO_PROMOTE", "false")

INDIA_CORRELATION_CLUSTERS: dict[str, list[str]] = {
    "in_it": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
    "in_banks": [
        "HDFCBANK",
        "ICICIBANK",
        "SBIN",
        "KOTAKBANK",
        "AXISBANK",
        "INDUSINDBK",
    ],
    "in_energy": ["RELIANCE", "ONGC", "BPCL", "COALINDIA", "NTPC", "POWERGRID"],
    "in_fmcg": ["HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "TATACONSUM"],
    "in_infra": ["LT", "BHARTIARTL", "ADANIPORTS", "ULTRACEMCO"],
    "in_auto": ["MARUTI", "TATAMOTORS", "M&M", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO"],
    "in_pharma": ["SUNPHARMA", "CIPLA", "DRREDDY", "DIVISLAB", "APOLLOHOSP"],
    "in_metals": ["TATASTEEL", "JSWSTEEL", "HINDALCO"],
    "in_finance": ["BAJFINANCE", "BAJAJFINSV", "HDFCLIFE", "SBILIFE"],
    "in_consumer": ["ASIANPAINT", "TITAN", "TRENT", "BEL", "ADANIENT", "GRASIM"],
}

# ===========================================================================
# India F&O (Futures & Options) Market Segment
# ===========================================================================
INDIA_FNO_LIVE_TRADING: bool = _env_bool("INDIA_FNO_LIVE_TRADING", "false")
INDIA_FNO_LIVE_CONFIRM: str = os.getenv("INDIA_FNO_LIVE_CONFIRM", "").strip()
INDIA_FNO_LIVE_CONFIRMED: bool = INDIA_FNO_LIVE_TRADING and INDIA_FNO_LIVE_CONFIRM == "YES_REAL_MONEY"

_INDIA_FNO_PAPER_ENV: bool = _env_bool("INDIA_FNO_PAPER", "true")
INDIA_FNO_PAPER: bool = _INDIA_FNO_PAPER_ENV and not INDIA_FNO_LIVE_CONFIRMED
INDIA_FNO_PAPER_STARTING_CASH: float = _env_float("INDIA_FNO_PAPER_STARTING_CASH", "200000")

# MIS bot: F&O off unless explicitly enabled
INDIA_FNO_ENABLED: bool = DHAN_CONFIGURED and _env_bool("INDIA_FNO_ENABLED", "false")
INDIA_FNO_UNIVERSE: list[str] = [
    s.strip().upper()
    for s in os.getenv("INDIA_FNO_UNIVERSE", "NIFTY,BANKNIFTY,FINNIFTY").split(",")
    if s.strip()
]
INDIA_FNO_MAX_LOTS: int = _env_int("INDIA_FNO_MAX_LOTS", "2")
INDIA_FNO_STRATEGY: str = os.getenv("INDIA_FNO_STRATEGY", "directional_options").strip().lower()
INDIA_FNO_CAPITAL_CAP: float = _env_float("INDIA_FNO_CAPITAL_CAP", "200000.0")

# Never invent MCX/FX/F&O marks for paper (required false before live money).
ALLOW_PAPER_PRICE_ESTIMATES: bool = _env_bool("ALLOW_PAPER_PRICE_ESTIMATES", "false")

# ===========================================================================
# MCX Commodities Market Segment (Gold, Silver, Crude, NatGas)
# ===========================================================================
MCX_LIVE_TRADING: bool = _env_bool("MCX_LIVE_TRADING", "false")
MCX_LIVE_CONFIRM: str = os.getenv("MCX_LIVE_CONFIRM", "").strip()
MCX_LIVE_CONFIRMED: bool = MCX_LIVE_TRADING and MCX_LIVE_CONFIRM == "YES_REAL_MONEY"

_MCX_PAPER_ENV: bool = _env_bool("MCX_PAPER", "true")
MCX_PAPER: bool = _MCX_PAPER_ENV and not MCX_LIVE_CONFIRMED
MCX_PAPER_STARTING_CASH: float = _env_float("MCX_PAPER_STARTING_CASH", "500000")

# MIS bot: MCX off unless explicitly enabled
MCX_ENABLED: bool = DHAN_CONFIGURED and _env_bool("MCX_ENABLED", "false")
MCX_UNIVERSE: list[str] = [
    s.strip().upper()
    for s in os.getenv("MCX_UNIVERSE", "CRUDEOIL,GOLD,SILVER,NATURALGAS").split(",")
    if s.strip()
]
MCX_CAPITAL_CAP: float = _env_float("MCX_CAPITAL_CAP", "500000.0")

# ===========================================================================
# Currency Derivatives Market Segment (NSE FX — USDINR)
# ===========================================================================
CURRENCY_LIVE_TRADING: bool = _env_bool("CURRENCY_LIVE_TRADING", "false")
CURRENCY_LIVE_CONFIRM: str = os.getenv("CURRENCY_LIVE_CONFIRM", "").strip()
CURRENCY_LIVE_CONFIRMED: bool = CURRENCY_LIVE_TRADING and CURRENCY_LIVE_CONFIRM == "YES_REAL_MONEY"

_CURRENCY_PAPER_ENV: bool = _env_bool("CURRENCY_PAPER", "true")
CURRENCY_PAPER: bool = _CURRENCY_PAPER_ENV and not CURRENCY_LIVE_CONFIRMED
CURRENCY_PAPER_STARTING_CASH: float = _env_float("CURRENCY_PAPER_STARTING_CASH", "100000")

# MIS bot: Currency off unless explicitly enabled
CURRENCY_ENABLED: bool = DHAN_CONFIGURED and _env_bool("CURRENCY_ENABLED", "false")
CURRENCY_UNIVERSE: list[str] = [
    s.strip().upper()
    for s in os.getenv("CURRENCY_UNIVERSE", "USDINR").split(",")
    if s.strip()
]
CURRENCY_CAPITAL_CAP: float = _env_float("CURRENCY_CAPITAL_CAP", "100000.0")

# ===========================================================================
# Strategy Selection (MIS primary = opening_range_breakout)
#   opening_range_breakout / orb — first N-min range break + volume (MIS default)
#   trend_pullback   — CNC-style pullback (available but not default here)
#   mean_reversion   — BB+RSI only in ranging markets (low ADX)
#   regime_adaptive  — ADX switches between trend_pullback and mean_reversion
#   breakout         — Donchian-style high breakout with volume
#   + USE_RELATIVE_STRENGTH — optional RS top-N gate on BUY
# ===========================================================================
STRATEGY_NAME: str = os.getenv("STRATEGY_NAME", "opening_range_breakout").strip().lower()
USE_RELATIVE_STRENGTH: bool = _env_bool("USE_RELATIVE_STRENGTH", "false")
RS_TOP_N: int = _env_int("RS_TOP_N", "5")
RS_LOOKBACK_BARS: int = _env_int("RS_LOOKBACK_BARS", "60")

# Candle interval for Dhan intraday_minute_data: 1 | 5 | 15 | 25 | 60 (or 1Hour)
TIMEFRAME: str = os.getenv("TIMEFRAME", "5")
LOOKBACK_BARS: int = _env_int("LOOKBACK_BARS", "120")

# ORB knobs (used when STRATEGY_NAME=opening_range_breakout)
OR_MINUTES: int = _env_int("OR_MINUTES", "15")
VOLUME_MULT: float = _env_float("VOLUME_MULT", "0.8")
ORB_ALLOW_SHORT: bool = _env_bool("ORB_ALLOW_SHORT", "false")
# Optional HTF EMA filter on ORB (uses same bars resampled / longer TF when available)
ORB_USE_HTF_FILTER: bool = _env_bool("ORB_USE_HTF_FILTER", "true")
ORB_HTF_EMA_PERIOD: int = _env_int("ORB_HTF_EMA_PERIOD", "20")

# Shared indicator defaults (overridden per-market below)
SMA_SLOW: int = _env_int("SMA_SLOW", "200")
SMA_FAST: int = _env_int("SMA_FAST", "20")
EMA_PULLBACK: int = _env_int("EMA_PULLBACK", "21")
RSI_PERIOD: int = _env_int("RSI_PERIOD", "14")
RSI_BUY_THRESHOLD: float = _env_float("RSI_BUY_THRESHOLD", "35.0")
RSI_SELL_THRESHOLD: float = _env_float("RSI_SELL_THRESHOLD", "65.0")
BB_STD_DEV: float = _env_float("BB_STD_DEV", "2.0")
ATR_PERIOD: int = _env_int("ATR_PERIOD", "14")
ADX_PERIOD: int = _env_int("ADX_PERIOD", "14")
ADX_RANGE_MAX: float = _env_float("ADX_RANGE_MAX", "25.0")  # ranging if ADX below
VOLUME_AVG_PERIOD: int = _env_int("VOLUME_AVG_PERIOD", "20")
CONFIRM_BARS: int = _env_int("CONFIRM_BARS", "1")  # ORB: 1–2 bars beyond OR

STRICT_SELL: bool = _env_bool("STRICT_SELL", "true")



# Per-market strategy params (India)
INDIA_SMA_SLOW: int = _env_int("INDIA_SMA_SLOW", str(SMA_SLOW))
INDIA_SMA_FAST: int = _env_int("INDIA_SMA_FAST", str(SMA_FAST))
INDIA_EMA_PULLBACK: int = _env_int("INDIA_EMA_PULLBACK", str(EMA_PULLBACK))
INDIA_RSI_PERIOD: int = _env_int("INDIA_RSI_PERIOD", str(RSI_PERIOD))
INDIA_RSI_BUY: float = _env_float("INDIA_RSI_BUY", str(RSI_BUY_THRESHOLD))
INDIA_RSI_SELL: float = _env_float("INDIA_RSI_SELL", str(RSI_SELL_THRESHOLD))
INDIA_BB_STD: float = _env_float("INDIA_BB_STD", str(BB_STD_DEV))
INDIA_ADX_RANGE_MAX: float = _env_float("INDIA_ADX_RANGE_MAX", str(ADX_RANGE_MAX))

# ===========================================================================
# Risk Parameters — size by risk-to-stop, not only % of equity
# ===========================================================================
RISK_PER_TRADE: float = _env_float("RISK_PER_TRADE", "0.006")  # 0.6% equity (MIS band)
MAX_POSITION_PCT: float = _env_float("MAX_POSITION_PCT", "0.12")  # hard cap
ATR_STOP_MULT: float = _env_float("ATR_STOP_MULT", "1.2")
ATR_TRAIL_MULT: float = _env_float("ATR_TRAIL_MULT", "1.2")
TAKE_PROFIT_R: float = _env_float("TAKE_PROFIT_R", "1.25")  # tighter than CNC swing
# Legacy pct stops kept as fallback when ATR unavailable
STOP_LOSS_PCT: float = _env_float("STOP_LOSS_PCT", "0.006")
TAKE_PROFIT_PCT: float = _env_float("TAKE_PROFIT_PCT", "0.009")
DAILY_DRAWDOWN_LIMIT: float = _env_float("DAILY_DRAWDOWN_LIMIT", "0.02")
# Hard stops (0 = disabled). Count = journal entries opened today (IST).
MAX_TRADES_PER_DAY: int = _env_int("MAX_TRADES_PER_DAY", "8")
# Absolute ₹ loss hard stop (in addition to DAILY_DRAWDOWN_LIMIT %). 0 = off.
MAX_DAILY_LOSS_INR: float = _env_float("MAX_DAILY_LOSS_INR", "0")
MAX_SHARES_PER_ORDER: int = _env_int("MAX_SHARES_PER_ORDER", "500")
MAX_OPEN_POSITIONS: int = _env_int("MAX_OPEN_POSITIONS", "2")
MAX_CLUSTER_POSITIONS: int = _env_int("MAX_CLUSTER_POSITIONS", "2")
# After exchange reject / while limit sits pending — don't spam re-buys
BUY_REJECT_COOLDOWN_SEC: int = _env_int("BUY_REJECT_COOLDOWN_SEC", "120")
BUY_PENDING_COOLDOWN_SEC: int = _env_int("BUY_PENDING_COOLDOWN_SEC", "90")
ORDER_CONFIRM_TIMEOUT_SEC: float = _env_float("ORDER_CONFIRM_TIMEOUT_SEC", "8")

# Avoid first/last N minutes of the session (noise / poor fills)
# MIS: skip open noise while OR builds; last ~75m ≈ no new entries after ~14:15
AVOID_OPEN_MINUTES: int = _env_int("AVOID_OPEN_MINUTES", "15")
AVOID_CLOSE_MINUTES: int = _env_int("AVOID_CLOSE_MINUTES", "75")
ALLOW_OPEN_CLOSE_WINDOW: bool = _env_bool("ALLOW_OPEN_CLOSE_WINDOW", "false")
# Hard MIS entry cutoff HH:MM IST (no NEW entries after this, even if avoid-close allows)
# Earlier than broker RMS (~15:15) so we are not racing square-off.
ENTRY_CUTOFF: str = os.getenv("ENTRY_CUTOFF", "14:15").strip()
# Bot square-off start HH:MM IST — MUST be before broker MIS cutoff (~15:15–15:20)
SQUAREOFF_TIME: str = os.getenv("SQUAREOFF_TIME", "15:00").strip()
DAILY_FLATTEN_ON_KILL: bool = _env_bool("DAILY_FLATTEN_ON_KILL", "true")
# Sleep between flatten API calls (positions/orders) to avoid DH-904 / HTTP 429
FLATTEN_API_GAP_SEC: float = _env_float("FLATTEN_API_GAP_SEC", "1.5")
FLATTEN_RATE_LIMIT_BACKOFF_SEC: float = _env_float("FLATTEN_RATE_LIMIT_BACKOFF_SEC", "8")
# Pre-trade cost floor: expected TP move must cover RT fees + min edge (₹/share equiv).
# Approx MIS RT fees ~0.05% of notional + fixed; MIN_EDGE_BPS is extra buffer.
COST_FLOOR_RT_PCT: float = _env_float("COST_FLOOR_RT_PCT", "0.0005")  # 5 bps RT
COST_FLOOR_MIN_EDGE_BPS: float = _env_float("COST_FLOOR_MIN_EDGE_BPS", "5")  # 5 bps
COST_FLOOR_ENABLED: bool = _env_bool("COST_FLOOR_ENABLED", "true")

# Entry style: limit by default; market only if explicitly enabled
ALLOW_MARKET_ENTRIES: bool = _env_bool("ALLOW_MARKET_ENTRIES", "false")



# Sync software trailing stops onto broker stop orders when an SL/super order id
# is known (modify_order / modify_super_order). Never loosens. If no order id is
# stored after restart, local trail still runs but broker sync is skipped.
SYNC_BROKER_STOPS: bool = _env_bool("SYNC_BROKER_STOPS", "true")
# TTL for Core/Scout buy reservation lock (seconds)
BUY_RESERVE_TTL_SEC: float = _env_float("BUY_RESERVE_TTL_SEC", "45")

# Multi-timeframe / regime — softer defaults for MIS ORB (optional)
USE_MTF_FILTER: bool = _env_bool("USE_MTF_FILTER", "false")
USE_REGIME_FILTER: bool = _env_bool("USE_REGIME_FILTER", "false")
REGIME_SYMBOL_INDIA: str = os.getenv("REGIME_SYMBOL_INDIA", "RELIANCE").strip().upper()

# Backtest realism
BT_COMMISSION_PCT: float = _env_float("BT_COMMISSION_PCT", "0.001")  # 0.1% round-trip approx
BT_SLIPPAGE_PCT: float = _env_float("BT_SLIPPAGE_PCT", "0.0005")  # 5 bps per side

# Alerts (optional)
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ALERT_WEBHOOK_URL: str = os.getenv("ALERT_WEBHOOK_URL", "").strip()

# ===========================================================================
# Trade Journal
# ===========================================================================
TRADE_JOURNAL_PATH: str = os.getenv("TRADE_JOURNAL_PATH", "trade_journal.db").strip()

# ===========================================================================
# Loop Intervals (bot process runs 24/7; trades only in market hours)
# ===========================================================================

INDIA_LOOP_INTERVAL_SEC: int = _env_int("INDIA_LOOP_INTERVAL_SEC", "45")
# During wait/sleep between full India cycles, run lightweight SL/TP checks
# more frequently so exits are not delayed by scanner latency.
INDIA_RISK_CHECK_INTERVAL_SEC: int = _env_int("INDIA_RISK_CHECK_INTERVAL_SEC", "5")
# Dashboard SSE base-position refresh cadence (live prices still stream every ~1s).
DASH_SSE_POS_REFRESH_SEC: int = _env_int("DASH_SSE_POS_REFRESH_SEC", "8")

# ===========================================================================
# US Market — Dhan Global Stocks
# ===========================================================================
# Separate live safety gate for US (independent from India):
#   US_LIVE_TRADING=true + US_LIVE_CONFIRM=YES_REAL_MONEY
US_LIVE_TRADING: bool = _env_bool("US_LIVE_TRADING", "false")
US_LIVE_CONFIRM: str = os.getenv("US_LIVE_CONFIRM", "").strip()
US_LIVE_CONFIRMED: bool = US_LIVE_TRADING and US_LIVE_CONFIRM == "YES_REAL_MONEY"

# Paper sim: live Dhan US quotes, fake USD (no real global orders).
# Forced OFF automatically when US_LIVE_CONFIRMED.
_US_PAPER_ENV: bool = _env_bool("US_PAPER", "true")
US_PAPER: bool = _US_PAPER_ENV and not US_LIVE_CONFIRMED

US_PAPER_STARTING_CASH: float = _env_float("US_PAPER_STARTING_CASH", "10000")

# MIS bot: US off unless explicitly US_ENABLED=true
US_ENABLED: bool = DHAN_CONFIGURED and _env_bool("US_ENABLED", "false")

US_STOCK_UNIVERSE: list[str] = [
    s.strip().upper()
    for s in os.getenv(
        "US_STOCK_UNIVERSE",
        "AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,JPM,V,UNH",
    ).split(",")
    if s.strip()
]

US_CORRELATION_CLUSTERS: dict[str, list[str]] = {
    "us_mega_tech": ["AAPL", "MSFT", "GOOGL", "META"],
    "us_ai_semi": ["NVDA", "TSLA"],
    "us_ecommerce": ["AMZN"],
    "us_finance": ["JPM", "V"],
    "us_healthcare": ["UNH"],
}

# Per-market strategy params (US)
US_SMA_SLOW: int = _env_int("US_SMA_SLOW", str(SMA_SLOW))
US_SMA_FAST: int = _env_int("US_SMA_FAST", str(SMA_FAST))
US_EMA_PULLBACK: int = _env_int("US_EMA_PULLBACK", str(EMA_PULLBACK))
US_RSI_PERIOD: int = _env_int("US_RSI_PERIOD", str(RSI_PERIOD))
US_RSI_BUY: float = _env_float("US_RSI_BUY", str(RSI_BUY_THRESHOLD))
US_RSI_SELL: float = _env_float("US_RSI_SELL", str(RSI_SELL_THRESHOLD))
US_BB_STD: float = _env_float("US_BB_STD", str(BB_STD_DEV))
US_ADX_RANGE_MAX: float = _env_float("US_ADX_RANGE_MAX", str(ADX_RANGE_MAX))

REGIME_SYMBOL_US: str = os.getenv("REGIME_SYMBOL_US", "AAPL").strip().upper()

US_LOOP_INTERVAL_SEC: int = _env_int("US_LOOP_INTERVAL_SEC", "300")


"""
us_broker.py — Dhan Global Stocks Broker Module (US Equities)
===============================================================
Wraps DhanHQ Global Stocks API methods for US stock trading.

Handles:
  - Shared Dhan session (same client_id/access_token as India)
  - Global Stocks quotes (LTP via ticker_data or candle fallback)
  - Historical minute candle data → pandas DataFrame (cached; INX_EQ)
  - Account/fund info via get_global_fund_limit()
  - Positions via get_global_holdings()
  - Order placement via place_global_order()
  - Paper sim via USPaperPortfolio (live quotes, fake USD)

Market hours: NYSE 09:30–16:00 America/New_York
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

import config
from us_instruments import (
    US_INSTRUMENTS,
    get_us_security_id,
    get_us_exchange,
    is_us_symbol,
    load_us_scrip_master,
)
from us_paper import USPaperPortfolio

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

TOKEN_REFRESH_HOURS = 20
CANDLE_CACHE_SEC = 30.0
CANDLE_CALL_GAP_SEC = 0.35
QUOTE_CACHE_SEC = 45.0
MARKETFEED_COOLDOWN_SEC = 90.0
# Dhan Global Stocks intraday charts: keep chunks short (minute-range API).
INTRADAY_CHUNK_DAYS = 5
US_INTRADAY_LOOKBACK_DAYS = 7

_shared_broker: "USBroker | None" = None
_shared_lock = threading.Lock()


def _us_chart_interval() -> int:
    """Map bot TIMEFRAME to Dhan-supported minute intervals (1/5/15/25/60)."""
    raw = str(getattr(config, "TIMEFRAME", "5") or "5").strip().lower()
    digits = "".join(ch for ch in raw if ch.isdigit())
    try:
        tf = int(digits or "5")
    except Exception:
        tf = 5
    for allowed in (1, 5, 15, 25, 60):
        if tf <= allowed:
            return allowed
    return 60


def _normalize_us_bar_index(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure DatetimeIndex is America/New_York for session VWAP / ORB logic."""
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return df
    idx = df.index
    if getattr(idx, "tz", None) is None:
        try:
            idx = idx.tz_localize("UTC").tz_convert(ET)
        except Exception:
            try:
                idx = idx.tz_localize(ET)
            except Exception:
                return df
    else:
        idx = idx.tz_convert(ET)
    out = df.copy()
    out.index = idx
    return out.sort_index()


def _fetch_yahoo_candles(
    symbol: str, *, interval: str = "5m", range_: str = "5d"
) -> pd.DataFrame | None:
    """
    Fallback US bars via Yahoo chart API.
    Prefer intraday (5m/5d) so mis_regime is not fed daily candles.
    """
    import requests

    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval={interval}&range={range_}"
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if result:
                timestamps = result[0].get("timestamp", [])
                quote = result[0].get("indicators", {}).get("quote", [{}])[0]
                opens = quote.get("open", [])
                highs = quote.get("high", [])
                lows = quote.get("low", [])
                closes = quote.get("close", [])
                volumes = quote.get("volume", [])

                if timestamps and closes:
                    df = pd.DataFrame(
                        {
                            "open": opens,
                            "high": highs,
                            "low": lows,
                            "close": closes,
                            "volume": [v if v is not None else 0 for v in volumes],
                        },
                        index=pd.to_datetime(timestamps, unit="s", utc=True),
                    )
                    df = df.dropna(subset=["close"]).astype(float).sort_index()
                    df = _normalize_us_bar_index(df)
                    if not df.empty:
                        logger.info(
                            f"[US] {symbol}: Loaded {len(df)} {interval} candles "
                            f"via Yahoo fallback ({range_})"
                        )
                        return df
    except Exception as e:
        logger.debug(f"[US] Fallback feed for {symbol} error: {e}")
    return None


def get_shared_us_broker(auto_login: bool = True) -> "USBroker":
    """Process-wide singleton — bot loop and dashboard must share one session."""
    global _shared_broker
    with _shared_lock:
        if _shared_broker is None:
            _shared_broker = USBroker(auto_login=auto_login)
        return _shared_broker


class USBroker:
    """
    DhanHQ Global Stocks broker client for US equities.

    When config.US_PAPER is True:
      - Market data comes from LIVE Dhan Global APIs
      - Buys/sells update a virtual USD portfolio
    When US_LIVE_CONFIRMED:
      - Real global orders hit the Dhan account
    """

    def __init__(self, auto_login: bool = True):
        self.client_id = config.DHAN_CLIENT_ID
        self.access_token = config.DHAN_ACCESS_TOKEN
        self.pin = config.DHAN_PIN
        self.totp_secret = config.DHAN_TOTP_SECRET

        self.dhan = None
        self._session_time: datetime | None = None
        self._logged_in = False
        self.last_error = ""
        self._last_candle_call_time = 0.0
        self._candle_cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._quote_cache: dict[str, tuple[float, dict]] = {}
        self._marketfeed_cooldown_until = 0.0
        self._quote_warn_at: dict[str, float] = {}
        self._login_lock = threading.Lock()
        self._global_stocks_available = True

        self.paper = USPaperPortfolio() if config.US_PAPER else None
        mode = "PAPER SIM (live US data, fake USD)" if self.paper else "LIVE REAL MONEY"
        logger.info(f"[US] USBroker mode: {mode}")

        # Align SCRIP_CODEs from official Global Stocks master (best-effort)
        try:
            load_us_scrip_master()
        except Exception as me:
            logger.debug(f"[US] Scrip master preload note: {me}")

        if auto_login:
            self.login()

    def _sync_us_live_feed_credentials(self, reconnect: bool = True) -> None:
        """Push latest access token into the GlobalStocksFeed manager."""
        if not getattr(config, "DHAN_US_LIVE_WEBSOCKET", False):
            return
        try:
            from dhan_us_live_feed import get_us_live_feed_manager

            feed = get_us_live_feed_manager()
            feed.update_credentials(
                self.client_id, self.access_token, reconnect=reconnect
            )
        except Exception as e:
            logger.debug(f"[US] Live feed credential sync note: {e}")

    # -----------------------------------------------------------------------
    # Authentication (reuses Dhan credentials — same as India)
    # -----------------------------------------------------------------------
    def _build_client(self, access_token: str):
        from dhanhq import DhanContext, dhanhq as DhanHQ
        ctx = DhanContext(self.client_id, access_token)
        return DhanHQ(ctx)

    def _totp_now(self) -> str:
        import pyotp
        secret = (self.totp_secret or "").replace(" ", "").upper()
        return pyotp.TOTP(secret).now()

    def _refresh_access_token(self) -> str | None:
        if not (self.client_id and self.pin and self.totp_secret):
            return None
        try:
            from dhanhq import DhanLogin
            login = DhanLogin(self.client_id)
            resp = login.generate_token(self.pin, self._totp_now())
            token = None
            if isinstance(resp, dict):
                token = resp.get("accessToken") or resp.get("access_token")
                data = resp.get("data")
                if not token and isinstance(data, dict):
                    token = data.get("accessToken") or data.get("access_token")
            if token:
                logger.info("[US] Dhan access token refreshed via PIN/TOTP")
                return str(token)
            self.last_error = f"[US] Token refresh failed: {resp}"
            logger.error(self.last_error)
            return None
        except Exception as e:
            self.last_error = f"[US] Token refresh error: {e}"
            logger.error(self.last_error, exc_info=True)
            return None

    def login(self) -> bool:
        with self._login_lock:
            try:
                if not self.client_id:
                    self.last_error = "[US] Missing DHAN_CLIENT_ID"
                    self._logged_in = False
                    return False

                token = self.access_token
                prev_token = token
                need_refresh = not token
                if self._session_time and token:
                    age = datetime.now() - self._session_time
                    if age > timedelta(hours=TOKEN_REFRESH_HOURS):
                        need_refresh = True

                if need_refresh and self.pin and self.totp_secret:
                    refreshed = self._refresh_access_token()
                    if refreshed:
                        token = refreshed
                        self.access_token = refreshed
                    elif not token:
                        self._logged_in = False
                        return False
                elif not token:
                    self.last_error = (
                        "[US] Missing DHAN_ACCESS_TOKEN "
                        "(or set DHAN_PIN + DHAN_TOTP_SECRET to auto-generate)"
                    )
                    self._logged_in = False
                    return False

                self.dhan = self._build_client(token)

                # Probe: try global fund limit to verify Global Stocks access
                try:
                    funds = self.dhan.get_global_fund_limit()
                    if isinstance(funds, dict) and funds.get("status") == "failure":
                        remarks = funds.get("remarks") or funds.get("message") or str(funds)
                        remarks_str = str(remarks).lower()
                        if "global" in remarks_str or "not activated" in remarks_str or "not enabled" in remarks_str:
                            self._global_stocks_available = False
                            self.last_error = (
                                "[US] Global Stocks not activated on this Dhan account. "
                                "Enable at dhan.co → Settings → Global Stocks. "
                                "India trading continues normally."
                            )
                            logger.warning(self.last_error)
                            # Still mark as logged in so candle data may work
                        else:
                            # Token issue — try refresh
                            if self.pin and self.totp_secret:
                                refreshed = self._refresh_access_token()
                                if refreshed:
                                    self.access_token = refreshed
                                    token = refreshed
                                    self.dhan = self._build_client(refreshed)
                    elif isinstance(funds, dict):
                        # Success / non-failure → Global Stocks reachable
                        self._global_stocks_available = True
                except Exception as ge:
                    # Transient probe errors must NOT permanently disable live orders
                    logger.warning(
                        f"[US] Global fund limit probe: {ge} "
                        "(keeping Global Stocks enabled; orders may still work)"
                    )

                self.access_token = token
                self._session_time = datetime.now()
                self._logged_in = True
                if self._global_stocks_available:
                    self.last_error = ""
                logger.info(f"[US] Dhan LOGIN SUCCESS | Client: {self.client_id} | Global={self._global_stocks_available}")
                self._sync_us_live_feed_credentials(reconnect=(token != prev_token))
                return True

            except Exception as e:
                self.last_error = f"[US] Dhan login error: {e}"
                logger.error(self.last_error, exc_info=True)
                self._logged_in = False
                return False

    def ensure_session(self) -> None:
        if not self._logged_in or self.dhan is None:
            self.login()
            return
        if self._session_time:
            elapsed = datetime.now() - self._session_time
            if elapsed > timedelta(hours=TOKEN_REFRESH_HOURS):
                logger.info("[US] Dhan token nearing expiry — refreshing...")
                self.login()

    def _handle_api_error(self, context: str, err) -> bool:
        msg = str(err).lower()
        if any(k in msg for k in ("token", "unauthorized", "401", "auth", "expired", "invalid")):
            logger.warning(f"[US] {context}: session issue — re-login")
            self._logged_in = False
            return self.login()
        return False

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    @property
    def global_stocks_available(self) -> bool:
        return self._global_stocks_available

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------
    def _ok(self, resp) -> bool:
        if not isinstance(resp, dict):
            return False
        status = resp.get("status")
        return status in (True, "success", "Success", "SUCCESS")

    def _data(self, resp):
        if not isinstance(resp, dict):
            return None
        return resp.get("data")

    # -----------------------------------------------------------------------
    # Market Hours (NYSE)
    # -----------------------------------------------------------------------
    def is_market_open(self) -> bool:
        """NYSE regular session: Mon-Fri 9:30 AM – 4:00 PM ET."""
        now_et = datetime.now(ET)
        if now_et.weekday() >= 5:
            return False
        open_time = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        close_time = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_time <= now_et <= close_time

    def market_status_string(self) -> str:
        """Human-readable market status with ET and IST times."""
        try:
            from zoneinfo import ZoneInfo as ZI
        except ImportError:
            from backports.zoneinfo import ZoneInfo as ZI  # type: ignore
        IST = ZI("Asia/Kolkata")
        now_et = datetime.now(ET)
        now_ist = datetime.now(IST)
        if self.is_market_open():
            return f"NYSE Open ({now_et.strftime('%I:%M %p ET')} / {now_ist.strftime('%I:%M %p IST')})"
        return f"NYSE Closed ({now_et.strftime('%I:%M %p ET')} / {now_ist.strftime('%I:%M %p IST')})"

    # -----------------------------------------------------------------------
    # Account
    # -----------------------------------------------------------------------
    def get_account_info(self) -> dict | None:
        if self.paper is not None:
            marks = self._live_marks_for_positions(self.paper.positions.keys())
            info = self.paper.get_account_info(marks)
            logger.info(
                f"[US PAPER] Equity=${info['equity']:,.2f} | "
                f"Cash=${info['available_cash']:,.2f}"
            )
            return info

        if not self._global_stocks_available:
            self.last_error = "[US] Global Stocks not activated"
            return None

        self.ensure_session()
        if not self.dhan:
            return None
        try:
            resp = self.dhan.get_global_fund_limit()
            data = self._data(resp) if self._ok(resp) else None
            if not isinstance(data, dict):
                # Try parsing the response directly
                if isinstance(resp, dict) and not self._ok(resp):
                    self.last_error = f"[US] Fund limits error: {resp}"
                    logger.error(self.last_error)
                    return None
                data = resp if isinstance(resp, dict) else {}

            # Dhan Global fundlimit shape (live probe):
            #   availableCash / cashOnAccount / actualCash / marginUtilized
            # India-style keys kept as fallbacks.
            available_cash = float(
                data.get("availableCash")
                or data.get("cashOnAccount")
                or data.get("actualCash")
                or data.get("availabelBalance")  # India typo key
                or data.get("availableBalance")
                or data.get("balance")
                or data.get("sodLimit")
                or 0
            )
            buying_power = float(
                data.get("buyingPower")
                or data.get("buying_power")
                or data.get("availableCash")
                or available_cash
            )
            used_margin = float(
                data.get("marginUtilized")
                or data.get("utilizedAmount")
                or data.get("usedMargin")
                or 0
            )
            # Prefer cash-on-account as book equity when no positions MTM here
            cash_on_account = float(
                data.get("cashOnAccount")
                or data.get("actualCash")
                or available_cash
                or 0
            )
            net = cash_on_account if cash_on_account > 0 else (available_cash + used_margin)

            info = {
                "equity": round(net, 2) if net > 0 else round(available_cash, 2),
                "available_cash": round(available_cash, 2),
                "buying_power": round(buying_power, 2),
                "used_margin": round(used_margin, 2),
                "net": round(net, 2),
                "paper": False,
                "settled_cash": float(data.get("settledCash") or 0),
                "unsettled_cash": float(data.get("unsettledCash") or 0),
            }
            logger.info(
                f"[US LIVE] Net=${net:,.2f} | Cash=${available_cash:,.2f} | "
                f"BuyPower=${buying_power:,.2f} | MarginUsed=${used_margin:,.2f}"
            )
            return info
        except Exception as e:
            if self._handle_api_error("[US] fund limits", e):
                return self.get_account_info()
            self.last_error = f"[US] Fund limits error: {e}"
            logger.error(self.last_error, exc_info=True)
            return None

    def _live_marks_for_positions(self, symbols) -> dict[str, float]:
        marks = {}
        for symbol in symbols:
            quote = self.get_latest_quote(symbol)
            if quote and quote.get("ltp"):
                marks[symbol] = float(quote["ltp"])
        return marks

    # -----------------------------------------------------------------------
    # Historical bars
    # -----------------------------------------------------------------------
    def _throttle_candles(self) -> None:
        now_ts = time.time()
        gap = now_ts - self._last_candle_call_time
        if gap < CANDLE_CALL_GAP_SEC:
            time.sleep(CANDLE_CALL_GAP_SEC - gap)
        self._last_candle_call_time = time.time()

    def _parse_intraday(self, data) -> pd.DataFrame | None:
        if not data:
            return None
        if isinstance(data, dict) and "open" in data and "timestamp" in data:
            ts = data["timestamp"]
            if self.dhan:
                try:
                    ts = [self.dhan.convert_to_date_time(t) for t in ts]
                except Exception:
                    ts = pd.to_datetime(ts, unit="s", errors="coerce")
            df = pd.DataFrame(
                {
                    "open": data.get("open"),
                    "high": data.get("high"),
                    "low": data.get("low"),
                    "close": data.get("close"),
                    "volume": data.get("volume") or [0] * len(data["open"]),
                },
                index=pd.to_datetime(ts),
            )
            return _normalize_us_bar_index(df.astype(float).sort_index())
        if isinstance(data, list) and data:
            rows = []
            for row in data:
                if isinstance(row, dict):
                    rows.append(row)
                elif isinstance(row, (list, tuple)) and len(row) >= 5:
                    rows.append({
                        "timestamp": row[0],
                        "open": row[1],
                        "high": row[2],
                        "low": row[3],
                        "close": row[4],
                        "volume": row[5] if len(row) > 5 else 0,
                    })
            if not rows:
                return None
            df = pd.DataFrame(rows)
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df.set_index("timestamp", inplace=True)
            return _normalize_us_bar_index(
                df[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
            )
        return None

    def get_historical_bars(self, symbol: str, days: int | None = None) -> pd.DataFrame | None:
        """
        Load US equity bars for strategy (prefer Dhan INX_EQ intraday).

        days=None → US_INTRADAY_LOOKBACK_DAYS (minute data, not 300 daily bars).
        """
        sec_id = get_us_security_id(symbol)
        now_ts = time.time()
        if symbol in self._candle_cache:
            cache_time, cached_df = self._candle_cache[symbol]
            if now_ts - cache_time < CANDLE_CACHE_SEC:
                return cached_df

        lookback = int(days) if days is not None else US_INTRADAY_LOOKBACK_DAYS
        lookback = max(2, min(lookback, 30))
        interval = _us_chart_interval()
        # Global Stocks segment — NOT NSE_FNO / IDX_I (those caused HTTP 400).
        segments = ("INX_EQ",)
        try:
            from dhanhq import dhanhq as DhanHQ

            if getattr(DhanHQ, "INX", None):
                segments = (str(DhanHQ.INX), "INX_EQ")
        except Exception:
            pass

        frames: list[pd.DataFrame] = []
        if self._logged_in and self.dhan and sec_id:
            to_date = datetime.now(ET)
            from_date = to_date - timedelta(days=lookback)
            chunk_start = from_date
            try:
                while chunk_start < to_date:
                    chunk_end = min(
                        chunk_start + timedelta(days=INTRADAY_CHUNK_DAYS), to_date
                    )
                    self._throttle_candles()

                    resp = None
                    for segment in segments:
                        try:
                            resp = self.dhan.intraday_minute_data(
                                security_id=str(sec_id),
                                exchange_segment=segment,
                                instrument_type="EQUITY",
                                from_date=chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                                to_date=chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
                                interval=interval,
                            )
                            if self._ok(resp):
                                logger.debug(
                                    f"[US] {symbol}: intraday ok segment={segment} "
                                    f"interval={interval}m"
                                )
                                break
                            else:
                                logger.debug(
                                    f"[US] {symbol}: intraday fail segment={segment} "
                                    f"→ {resp}"
                                )
                        except Exception as e:
                            logger.debug(
                                f"[US] {symbol}: intraday exception segment={segment}: {e}"
                            )
                            continue

                    if resp and self._ok(resp):
                        part = self._parse_intraday(self._data(resp))
                        if part is not None and not part.empty:
                            frames.append(_normalize_us_bar_index(part))
                    chunk_start = chunk_end

                # Daily only as last Dhan attempt (still INX_EQ) — not for mis_regime primary path
                if not frames:
                    for segment in segments:
                        try:
                            self._throttle_candles()
                            resp = self.dhan.historical_daily_data(
                                security_id=str(sec_id),
                                exchange_segment=segment,
                                instrument_type="EQUITY",
                                from_date=(to_date - timedelta(days=120)).strftime(
                                    "%Y-%m-%d"
                                ),
                                to_date=to_date.strftime("%Y-%m-%d"),
                            )
                            if self._ok(resp):
                                part = self._parse_intraday(self._data(resp))
                                if part is not None and not part.empty:
                                    frames.append(_normalize_us_bar_index(part))
                                    logger.warning(
                                        f"[US] {symbol}: using Dhan daily bars "
                                        f"(intraday unavailable)"
                                    )
                                    break
                        except Exception:
                            continue
            except Exception as e:
                logger.debug(f"[US] Dhan candle fetch for {symbol}: {e}")

        if frames:
            df = pd.concat(frames).sort_index()
            df = df[~df.index.duplicated(keep="last")]
            df = _normalize_us_bar_index(df)
            logger.info(
                f"[US] {symbol}: Loaded {len(df)} Dhan candles "
                f"(interval={interval}m, lookback={lookback}d)"
            )
            self._candle_cache[symbol] = (time.time(), df)
            return df

        # Yahoo intraday fallback (5m), not 1d/1y
        y_interval = f"{interval}m" if interval in (1, 5, 15, 60) else "5m"
        if y_interval == "1m":
            y_range = "7d"
        elif y_interval in ("5m", "15m"):
            y_range = "5d"
        else:
            y_range = "1mo"
        fallback_df = _fetch_yahoo_candles(
            symbol, interval=y_interval, range_=y_range
        )
        if fallback_df is not None and not fallback_df.empty:
            self._candle_cache[symbol] = (time.time(), fallback_df)
            return fallback_df

        if symbol in self._candle_cache:
            return self._candle_cache[symbol][1]

        logger.warning(f"[US] {symbol}: No market data available from any feed")
        return None


    # -----------------------------------------------------------------------
    # Quotes
    # -----------------------------------------------------------------------
    def _cache_quote(self, symbol: str, quote: dict) -> dict:
        self._quote_cache[symbol] = (time.time(), quote)
        return quote

    def _cached_quote(self, symbol: str) -> dict | None:
        hit = self._quote_cache.get(symbol)
        if not hit:
            return None
        ts, quote = hit
        if time.time() - ts > QUOTE_CACHE_SEC:
            return None
        return quote

    def _arm_marketfeed_cooldown(self, reason: str) -> None:
        until = time.time() + MARKETFEED_COOLDOWN_SEC
        if until > self._marketfeed_cooldown_until:
            self._marketfeed_cooldown_until = until
            logger.warning(f"[US] Marketfeed cooldown {MARKETFEED_COOLDOWN_SEC:.0f}s ({reason})")

    def _quote_warn(self, symbol: str, msg: str) -> None:
        now = time.time()
        last = self._quote_warn_at.get(symbol, 0.0)
        if now - last < 60.0:
            logger.debug(msg)
            return
        self._quote_warn_at[symbol] = now
        logger.warning(msg)

    def _quote_from_candle_cache(self, symbol: str) -> dict | None:
        cached = self._candle_cache.get(symbol)
        if not cached:
            return None
        _, df = cached
        if df is None or df.empty or "close" not in df.columns:
            return None
        close = float(df["close"].iloc[-1])
        if close <= 0:
            return None
        self._quote_warn(
            symbol,
            f"[US] {symbol}: marketfeed LTP unavailable — using last candle close ${close:.2f}",
        )
        return self._cache_quote(symbol, {
            "ltp": close,
            "ask_price": close,
            "symbol": symbol,
            "exchange": "GLOBAL",
            "source": "candle_close",
        })

    def get_latest_quote(self, symbol: str) -> dict | None:
        cached = self._cached_quote(symbol)
        if cached:
            return cached

        # 1) GlobalStocksFeed WebSocket cache (preferred when live)
        try:
            from dhan_us_live_feed import get_us_live_feed_manager

            ws_quote = get_us_live_feed_manager().get_live_quote(symbol)
            if ws_quote and float(ws_quote.get("ltp") or 0) > 0:
                return self._cache_quote(symbol, {
                    "ltp": float(ws_quote["ltp"]),
                    "ask_price": float(ws_quote.get("ask_price") or ws_quote["ltp"]),
                    "symbol": symbol,
                    "exchange": "GLOBAL",
                    "source": "websocket_live",
                    "open": ws_quote.get("open"),
                    "high": ws_quote.get("high"),
                    "low": ws_quote.get("low"),
                    "close": ws_quote.get("close"),
                    "volume": ws_quote.get("volume"),
                })
        except Exception as fe:
            logger.debug(f"[US] Live feed quote skip: {fe}")

        if time.time() < self._marketfeed_cooldown_until:
            return self._quote_from_candle_cache(symbol)

        self.ensure_session()
        if not self.dhan:
            return self._yahoo_quote_fallback(symbol)

        sec_id = get_us_security_id(symbol)
        if not sec_id:
            logger.error(f"[US] No security_id for {symbol}")
            return self._yahoo_quote_fallback(symbol)

        # 2) Dhan Global REST ticker_data / ohlc_data
        try:
            method = getattr(self.dhan, "ticker_data", None) or getattr(
                self.dhan, "ohlc_data", None
            )
            if method is None:
                return self._yahoo_quote_fallback(symbol)

            # Try different segment keys for Global Stocks
            for segment_key in ("GLOBAL", "INX_EQ", "NSE_FNO", "IDX_I"):
                try:
                    securities = {segment_key: [int(sec_id)]}
                    resp = method(securities)
                    if self._ok(resp):
                        data = self._data(resp)
                        if isinstance(data, dict):
                            # Try to extract LTP from nested structure
                            bucket = data.get(segment_key) or data.get(str(sec_id)) or data
                            if isinstance(bucket, dict):
                                node = bucket.get(str(sec_id)) or bucket.get(int(sec_id)) or bucket
                                if isinstance(node, dict):
                                    ltp = (
                                        node.get("last_price")
                                        or node.get("LTP")
                                        or node.get("ltp")
                                        or node.get("last_trade_price")
                                        or (node.get("ohlc") or {}).get("close")
                                        or node.get("close")
                                    )
                                    if ltp is not None:
                                        ltp_val = float(ltp)
                                        if ltp_val > 0:
                                            return self._cache_quote(symbol, {
                                                "ltp": ltp_val,
                                                "ask_price": ltp_val,
                                                "symbol": symbol,
                                                "exchange": "GLOBAL",
                                                "source": "ticker_data",
                                            })
                except Exception:
                    continue

            # 3) Yahoo / candle last resort
            return self._yahoo_quote_fallback(symbol)

        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "rate" in msg:
                self._arm_marketfeed_cooldown("[US] exception 429")
                return self._yahoo_quote_fallback(symbol)
            if self._handle_api_error(f"[US] {symbol} LTP", e):
                return self.get_latest_quote(symbol)
            logger.error(f"[US] {symbol}: LTP error: {e}", exc_info=True)
            return self._yahoo_quote_fallback(symbol)

    def _yahoo_quote_fallback(self, symbol: str) -> dict | None:
        """Last-resort quote: candle cache, then Yahoo daily chart close."""
        from_candle = self._quote_from_candle_cache(symbol)
        if from_candle:
            return from_candle

        df = _fetch_yahoo_candles(symbol)
        if df is None or df.empty or "close" not in df.columns:
            return None
        close = float(df["close"].iloc[-1])
        if close <= 0:
            return None
        self._candle_cache[symbol] = (time.time(), df)
        self._quote_warn(
            symbol,
            f"[US] {symbol}: using Yahoo last-resort LTP ${close:.2f}",
        )
        return self._cache_quote(symbol, {
            "ltp": close,
            "ask_price": close,
            "symbol": symbol,
            "exchange": "GLOBAL",
            "source": "yahoo",
        })

    # -----------------------------------------------------------------------
    # Live gate
    # -----------------------------------------------------------------------
    def _assert_live_allowed(self, action: str) -> bool:
        if self.paper is not None:
            return True
        if not config.US_LIVE_CONFIRMED:
            logger.critical(
                f"[US] BLOCKED {action}: US live trading is OFF. "
                f"Keep US_PAPER=true for testing, or set "
                f"US_LIVE_TRADING=true and US_LIVE_CONFIRM=YES_REAL_MONEY for real money."
            )
            self.last_error = "[US] Live trading not confirmed"
            return False
        return True

    @staticmethod
    def _extract_order_id(result) -> str | None:
        if result is None:
            return None
        if isinstance(result, str) and result.strip():
            return result.strip()
        if isinstance(result, dict):
            if result.get("status") in (False, "failure", "Failure"):
                return None
            data = result.get("data")
            if isinstance(data, dict):
                oid = (
                    data.get("orderId")
                    or data.get("order_id")
                    or data.get("orderid")
                )
                if oid:
                    return str(oid)
            oid = result.get("orderId") or result.get("order_id")
            if oid:
                return str(oid)
        return None

    # -----------------------------------------------------------------------
    # Positions
    # -----------------------------------------------------------------------
    def get_open_positions(self) -> dict:
        if self.paper is not None:
            marks = self._live_marks_for_positions(self.paper.positions.keys())
            pos_dict = self.paper.get_open_positions(marks)
            if pos_dict:
                logger.info(f"[US PAPER] positions: {list(pos_dict.keys())}")
            return pos_dict

        if not self._global_stocks_available:
            return {}

        self.ensure_session()
        if not self.dhan:
            return {}

        pos_dict: dict = {}
        try:
            resp = self.dhan.get_global_holdings()
            data = self._data(resp) if self._ok(resp) else None
            for h in data or []:
                if not isinstance(h, dict):
                    continue
                qty = int(float(
                    h.get("availableQty")
                    or h.get("totalQty")
                    or h.get("quantity")
                    or h.get("qty")
                    or 0
                ))
                if qty <= 0:
                    continue
                sec_id = str(h.get("securityId") or h.get("security_id") or "")
                trading_symbol = (
                    h.get("tradingSymbol")
                    or h.get("trading_symbol")
                    or h.get("symbol")
                    or h.get("ticker")
                    or ""
                )

                # Reverse-lookup symbol from security_id
                symbol = trading_symbol
                for sym, info in US_INSTRUMENTS.items():
                    if info["security_id"] == sec_id:
                        symbol = sym
                        break

                buy_price = float(h.get("avgCostPrice") or h.get("averagePrice") or h.get("buyAvg") or 0)
                ltp = float(h.get("ltp") or h.get("lastTradedPrice") or 0)
                pnl = float(h.get("unrealizedProfit") or h.get("pnl") or 0)
                if buy_price <= 0 and ltp > 0:
                    buy_price = ltp
                pnl_pct = ((ltp - buy_price) / buy_price) if buy_price > 0 else 0

                pos_dict[symbol] = {
                    "qty": qty,
                    "avg_entry_price": buy_price,
                    "current_price": ltp,
                    "market_value": qty * ltp,
                    "unrealized_pl": pnl,
                    "unrealized_plpc": pnl_pct,
                    "security_id": sec_id,
                    "source": "global_holding",
                }
        except Exception as e:
            logger.error(f"[US] Holdings fetch error: {e}", exc_info=True)

        if pos_dict:
            logger.info(f"[US] positions/holdings: {list(pos_dict.keys())}")
        return pos_dict

    # -----------------------------------------------------------------------
    # Orders
    # -----------------------------------------------------------------------
    def place_buy_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        place_stoploss: bool = False,
        stop_loss_pct: float | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        atr: float | None = None,
    ) -> str | None:
        if not self._assert_live_allowed(f"BUY {symbol}"):
            return None
        if qty <= 0 or limit_price <= 0:
            logger.error(f"[US] Invalid buy params for {symbol}: qty={qty} price={limit_price}")
            return None

        if stop_loss_price is None:
            sl_pct = stop_loss_pct if stop_loss_pct is not None else config.STOP_LOSS_PCT
            stop_loss_price = round(limit_price * (1 - sl_pct), 2)

        # Paper sim
        if self.paper is not None:
            quote = self.get_latest_quote(symbol)
            fill = float(quote["ltp"]) if quote else float(limit_price)
            return self.paper.buy(
                symbol, qty, fill,
                stop_loss=stop_loss_price,
                take_profit=take_profit_price,
                atr=atr,
            )

        # Live
        if not self._global_stocks_available:
            self.last_error = "[US] Global Stocks not activated — cannot place orders"
            return None

        self.ensure_session()
        if not self.dhan:
            return None
        sec_id = get_us_security_id(symbol)
        if not sec_id:
            logger.error(f"[US] Cannot place order — no security_id for {symbol}")
            return None

        try:
            from dhanhq import dhanhq as DhanHQ
            order_kwargs = {
                "security_id": str(sec_id),
                "transaction_type": DhanHQ.BUY,
                "quantity": int(qty),
                "order_type": DhanHQ.LIMIT,
                "price": float(round(limit_price * 1.001, 2)),
            }
            # Attach broker-side protective exits when provided
            if stop_loss_price is not None and float(stop_loss_price) > 0:
                order_kwargs["stop_loss_price"] = float(round(float(stop_loss_price), 2))
            if take_profit_price is not None and float(take_profit_price) > 0:
                order_kwargs["target_price"] = float(round(float(take_profit_price), 2))

            raw = self.dhan.place_global_order(**order_kwargs)
            order_id = self._extract_order_id(raw)
            if not order_id:
                logger.error(f"[US LIVE] BUY rejected for {symbol}: {raw}")
                self.last_error = f"[US] Buy rejected: {raw}"
                return None

            logger.warning(
                f"[US LIVE] BUY ORDER | {symbol} | Qty={qty} | "
                f"Limit=${limit_price:.2f} | SL={stop_loss_price} | "
                f"TP={take_profit_price} | Order ID={order_id}"
            )
            return order_id
        except Exception as e:
            if self._handle_api_error(f"[US] BUY {symbol}", e):
                return self.place_buy_order(
                    symbol, qty, limit_price,
                    stop_loss_price=stop_loss_price,
                    take_profit_price=take_profit_price,
                    atr=atr,
                )
            logger.error(f"[US] Failed to place BUY for {symbol}: {e}", exc_info=True)
            return None

    def place_sell_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float = 0,
        order_type: str = "MARKET",
    ) -> str | None:
        if not self._assert_live_allowed(f"SELL {symbol}"):
            return None

        # Paper sim
        if self.paper is not None:
            quote = self.get_latest_quote(symbol)
            fill = float(quote["ltp"]) if quote else float(limit_price or 0)
            if fill <= 0:
                logger.error(f"[US PAPER] No LTP to sell {symbol}")
                return None
            return self.paper.sell(symbol, qty, fill)

        # Live
        if not self._global_stocks_available:
            self.last_error = "[US] Global Stocks not activated"
            return None

        self.ensure_session()
        if not self.dhan:
            return None
        sec_id = get_us_security_id(symbol)
        if not sec_id:
            logger.error(f"[US] Cannot place sell — no security_id for {symbol}")
            return None

        try:
            from dhanhq import dhanhq as DhanHQ
            price = 0.0
            dhan_order_type = DhanHQ.MARKET

            if order_type == "MARKET" and limit_price <= 0:
                quote = self.get_latest_quote(symbol)
                if quote:
                    price = round(quote["ltp"] * 0.995, 2)
                    dhan_order_type = DhanHQ.LIMIT
            elif limit_price > 0:
                dhan_order_type = DhanHQ.LIMIT
                price = float(round(limit_price, 2))

            raw = self.dhan.place_global_order(
                security_id=str(sec_id),
                transaction_type=DhanHQ.SELL,
                quantity=int(qty),
                order_type=dhan_order_type,
                price=float(price),
            )
            order_id = self._extract_order_id(raw)
            if not order_id:
                logger.error(f"[US LIVE] SELL rejected for {symbol}: {raw}")
                return None
            logger.warning(
                f"[US LIVE] SELL ORDER | {symbol} | Qty={qty} | Order ID={order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"[US] Failed to place SELL for {symbol}: {e}", exc_info=True)
            return None

    def close_position(self, symbol: str) -> bool:
        positions = self.get_open_positions()
        if symbol not in positions:
            logger.warning(f"[US] {symbol}: No open position to close")
            return False
        qty = positions[symbol]["qty"]
        order_id = self.place_sell_order(symbol, qty, order_type="MARKET")
        if order_id:
            logger.info(f"[US] Position CLOSED for {symbol} (Qty={qty})")
            return True
        return False

    def check_sl_tp(self, risk_mgr) -> list[str]:
        """Software SL/TP monitor — critical for paper; backup for live."""
        closed_symbols = []
        positions = self.get_open_positions()

        for symbol, pos in positions.items():
            entry_price = float(pos["avg_entry_price"])
            current_price = float(pos.get("current_price") or 0)
            if current_price <= 0:
                logger.warning(
                    f"[US SL/TP] {symbol}: skip — no usable mark (ltp={current_price})"
                )
                continue
            atr = pos.get("atr")
            if atr is not None:
                try:
                    atr = float(atr)
                except (TypeError, ValueError):
                    atr = None

            stored_sl = pos.get("stop_loss")
            stored_tp = pos.get("take_profit")

            if stored_sl is not None:
                sl_price = float(stored_sl)
            else:
                sl_price = risk_mgr.get_stop_loss_price(entry_price, atr)

            if stored_tp is not None:
                tp_price = float(stored_tp)
            else:
                tp_price = risk_mgr.get_take_profit_price(
                    entry_price, stop_loss_price=sl_price, atr=atr
                )

            if symbol not in getattr(risk_mgr, "_trade_meta", {}):
                risk_mgr.register_trade(symbol, entry_price, sl_price, atr)

            trailed = risk_mgr.update_trailing_stop(symbol, current_price, atr)
            if trailed is not None and trailed > sl_price:
                sl_price = trailed
                if self.paper is not None:
                    self.paper.update_position_meta(
                        symbol,
                        stop_loss=sl_price,
                        peak_price=max(
                            float(pos.get("peak_price") or entry_price),
                            current_price,
                        ),
                    )

            reason = None
            if current_price <= sl_price:
                reason = "stop_loss"
                logger.warning(
                    f"[US SL] {symbol} hit stop! "
                    f"Entry=${entry_price:.2f} Px=${current_price:.2f} SL=${sl_price:.2f}"
                )
            elif current_price >= tp_price:
                reason = "take_profit"
                logger.info(
                    f"[US TP] {symbol} hit target! "
                    f"Entry=${entry_price:.2f} Px=${current_price:.2f} TP=${tp_price:.2f}"
                )

            if reason and self.close_position(symbol):
                closed_symbols.append(symbol)
                risk_mgr.clear_trade(symbol)
                try:
                    import trade_journal
                    trade_journal.record_exit("US", symbol, current_price, reason=reason)
                except Exception as je:
                    logger.debug(f"[US] Journal exit skip: {je}")

        return closed_symbols

    def cancel_all_open_orders(self) -> bool:
        if self.paper is not None:
            logger.info("[US PAPER] No broker orders to cancel")
            return True
        # Global Stocks order cancellation — best effort
        logger.info("[US] cancel_all_open_orders (no-op for Global Stocks)")
        return True

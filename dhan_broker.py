"""
dhan_broker.py — DhanHQ Broker Module
=======================================
Wraps Dhan's Python SDK for Indian stock trading (NSE equity).

Handles:
  - Access-token session (manual token OR PIN+TOTP refresh)
  - Shared singleton so dashboard + trading loop share one client
  - Historical intraday candles (TIMEFRAME minutes) → pandas DataFrame (cached)
  - Real-time LTP quotes
  - MIS/INTRADAY (default in this project) or CNC order placement / positions
  - Paper sim via IndiaPaperPortfolio (live quotes, fake INR)
  - square_off_intraday_positions for MIS flat-by-close

Env (see config.py):
  DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN
  DHAN_PIN + DHAN_TOTP_SECRET  (optional auto-renew)
  INDIA_PRODUCT_TYPE=INTRADAY
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pyotp
from dhanhq import DhanContext, DhanLogin, dhanhq

import config
from india_instruments import (
    INDIA_INSTRUMENTS,
    get_exchange,
    get_token,
)
from india_paper import IndiaPaperPortfolio
from dhan_live_feed import get_live_feed_manager
from india_fno_instruments import is_placeholder_security_id, resolve_instrument_info
from nse_tick import round_buy_limit, round_sell_limit, round_to_nse_tick
import order_guards

logger = logging.getLogger(__name__)

_REJECT_STATUSES = frozenset({"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"})
_FILL_STATUSES = frozenset({"TRADED", "PART_TRADED", "COMPLETE", "COMPLETED"})
_OPEN_STATUSES = frozenset(
    {"PENDING", "TRANSIT", "OPEN", "TRIGGER_PENDING", "AFTER_MARKET_ORDER_REQ"}
)


def _dhan_product_type(product_type: str | None = None):
    """
    Map config product names to dhanhq constants.
    SDK exposes INTRA (value 'INTRADAY'), not INTRADAY — getattr(dhanhq,'INTRADAY')
    incorrectly falls back to CNC.
    """
    p = (product_type or config.INDIA_PRODUCT_TYPE or "CNC").strip().upper()
    if p in ("INTRADAY", "INTRA", "MIS"):
        return getattr(dhanhq, "INTRA", "INTRADAY")
    if p == "MTF":
        return getattr(dhanhq, "MTF", "MTF")
    if p == "MARGIN":
        return getattr(dhanhq, "MARGIN", "MARGIN")
    return getattr(dhanhq, "CNC", "CNC")


_MIS_PRODUCT_NAMES = frozenset({"INTRADAY", "INTRA", "MIS"})


def _trading_product_is_mis(product_type: str | None = None) -> bool:
    p = (product_type or config.INDIA_PRODUCT_TYPE or "CNC").strip().upper()
    return p in _MIS_PRODUCT_NAMES


def _row_product(row: dict) -> str:
    return str(
        row.get("productType")
        or row.get("product")
        or row.get("product_type")
        or ""
    ).strip().upper()


def should_include_open_position(
    *,
    net_qty: int,
    product: str,
    trading_is_mis: bool,
) -> bool:
    """
    Decide whether a Dhan positions-API row counts as an open trade.

    Sold-from-holding CNC day sells often arrive with netQty < 0 and must not
    be treated as open longs (abs(qty) previously inflated MAX_OPEN_POSITIONS).
    MIS bots only count INTRADAY/MIS rows — not CNC holdings or delivery sells.
    """
    if net_qty == 0:
        return False
    product = (product or "").strip().upper()
    if trading_is_mis:
        return product in _MIS_PRODUCT_NAMES
    # CNC/delivery sleeve: ignore negative day sells (sold-from-holding).
    if net_qty < 0:
        return False
    return True


def _resolved_security_id(symbol: str, exchange_segment: str | None = None) -> tuple[str | None, str]:
    """Prefer numeric master/token IDs; never send invented symbol strings as security_id."""
    info = resolve_instrument_info(symbol, exchange_segment=exchange_segment or "NSE")
    sec_id = str(info.get("security_id") or "")
    exch = info.get("exchange") or "NSE_EQ"
    if not is_placeholder_security_id(sec_id):
        return sec_id, exch
    tok = get_token(symbol)
    if tok:
        ex = get_exchange(symbol)
        return str(tok), ("NSE_EQ" if ex.upper() == "NSE" else "BSE_EQ")
    return None, exch

# Dhan access tokens are typically ~24h; refresh a bit early.
TOKEN_REFRESH_HOURS = 20
CANDLE_CACHE_SEC = 30.0
CANDLE_CALL_GAP_SEC = 0.35
# Dashboard polls every few seconds — cache quotes hard to avoid 429s.
QUOTE_CACHE_SEC = 45.0
# After marketfeed 429/401, skip live LTP and use candles for a while.
MARKETFEED_COOLDOWN_SEC = 90.0
# Chunk intraday history requests (API can reject very wide ranges).
INTRADAY_CHUNK_DAYS = 30

TOKEN_CACHE_PATH = Path(
    os.getenv(
        "DHAN_TOKEN_CACHE_PATH",
        str(Path(__file__).resolve().parent / "dhan_access_token.cache"),
    )
).expanduser().resolve()

_shared_broker: "DhanBroker | None" = None
_shared_lock = threading.Lock()


def get_shared_dhan_broker(auto_login: bool = True) -> "DhanBroker":
    """Process-wide singleton — bot loop and dashboard must share one session."""
    global _shared_broker
    with _shared_lock:
        if _shared_broker is None:
            _shared_broker = DhanBroker(auto_login=auto_login)
        return _shared_broker


class DhanBroker:
    """
    DhanHQ broker client for NSE equity.

    When config.INDIA_PAPER is True:
      - Market data comes from LIVE Dhan APIs
      - Buys/sells update a virtual INR portfolio
    When LIVE_CONFIRMED:
      - Real CNC orders hit the Dhan account (static IP whitelist may be required)
    """

    def __init__(self, auto_login: bool = True):
        self.client_id = config.DHAN_CLIENT_ID
        self.access_token = config.DHAN_ACCESS_TOKEN or self._load_cached_token()
        self.pin = config.DHAN_PIN
        self.totp_secret = config.DHAN_TOTP_SECRET
        self.api_key = config.DHAN_API_KEY
        self.api_secret = config.DHAN_API_SECRET

        self.dhan: dhanhq | None = None
        self._session_time: datetime | None = None
        self._logged_in = False
        self.last_error = ""
        self._last_candle_call_time = 0.0
        self._candle_cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._quote_cache: dict[str, tuple[float, dict]] = {}
        self._marketfeed_cooldown_until = 0.0
        self._quote_warn_at: dict[str, float] = {}
        self._login_lock = threading.Lock()

        self.paper = IndiaPaperPortfolio() if config.INDIA_PAPER else None
        mode = "PAPER SIM (live NSE data, fake INR)" if self.paper else "LIVE REAL MONEY"
        logger.info(f"DhanBroker mode: {mode}")

        if auto_login:
            self.login()

    # -----------------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------------
    @staticmethod
    def _load_cached_token() -> str:
        try:
            if TOKEN_CACHE_PATH.exists():
                tok = TOKEN_CACHE_PATH.read_text(encoding="utf-8").strip()
                if tok:
                    logger.info(f"[AUTH] Loaded cached Dhan access token from {TOKEN_CACHE_PATH.name}")
                    return tok
        except Exception as e:
            logger.debug(f"[AUTH] Token cache read skip: {e}")
        return ""

    def _persist_token(self, token: str) -> None:
        try:
            TOKEN_CACHE_PATH.write_text(token.strip(), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[AUTH] Token cache write skip: {e}")

    def _sync_live_feed_credentials(self, reconnect: bool = True) -> None:
        """Push latest access token into India + US live WebSocket feeds."""
        if config.DHAN_LIVE_WEBSOCKET:
            try:
                feed = get_live_feed_manager()
                feed.update_credentials(
                    self.client_id, self.access_token, reconnect=reconnect
                )
            except Exception as e:
                logger.debug(f"[AUTH] Live feed credential sync note: {e}")
        if getattr(config, "DHAN_US_LIVE_WEBSOCKET", False):
            try:
                from dhan_us_live_feed import get_us_live_feed_manager

                us_feed = get_us_live_feed_manager()
                us_feed.update_credentials(
                    self.client_id, self.access_token, reconnect=reconnect
                )
            except Exception as e:
                logger.debug(f"[AUTH] US live feed credential sync note: {e}")

    def _build_client(self, access_token: str) -> dhanhq:
        ctx = DhanContext(self.client_id, access_token)
        return dhanhq(ctx)

    def _totp_now(self) -> str:
        secret = (self.totp_secret or "").replace(" ", "").upper()
        return pyotp.TOTP(secret).now()

    def _refresh_access_token(self) -> str | None:
        """Generate a fresh 24h access token via PIN + TOTP (paid Data API uses same token)."""
        if not (self.client_id and self.pin and self.totp_secret):
            return None
        try:
            login = DhanLogin(self.client_id)
            resp = login.generate_token(self.pin, self._totp_now())
            token = None
            if isinstance(resp, dict):
                token = resp.get("accessToken") or resp.get("access_token")
                data = resp.get("data")
                if not token and isinstance(data, dict):
                    token = data.get("accessToken") or data.get("access_token")
            if token:
                logger.info("[AUTH] Dhan access token refreshed via PIN/TOTP")
                self._persist_token(str(token))
                return str(token)
            self.last_error = f"Dhan token refresh failed: {resp}"
            logger.error(self.last_error)
            return None
        except Exception as e:
            self.last_error = f"Dhan token refresh error: {e}"
            logger.error(self.last_error, exc_info=True)
            return None

    def login(self) -> bool:
        with self._login_lock:
            try:
                if not self.client_id:
                    self.last_error = "Missing DHAN_CLIENT_ID"
                    self._logged_in = False
                    return False

                token = self.access_token or self._load_cached_token()
                prev_token = token

                # Refresh via PIN/TOTP only when missing or session aged — avoids
                # minting a new JWT (and feed reconnect) on every login() call.
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
                        "Missing DHAN_ACCESS_TOKEN "
                        "(or set DHAN_PIN + DHAN_TOTP_SECRET to auto-generate)"
                    )
                    self._logged_in = False
                    return False

                self.dhan = self._build_client(token)
                # Lightweight auth probe
                funds = self.dhan.get_fund_limits()
                if isinstance(funds, dict) and funds.get("status") == "failure":
                    remarks = funds.get("remarks") or funds.get("message") or funds
                    if self.pin and self.totp_secret:
                        refreshed = self._refresh_access_token()
                        if refreshed:
                            self.access_token = refreshed
                            token = refreshed
                            self.dhan = self._build_client(refreshed)
                            funds = self.dhan.get_fund_limits()
                    if isinstance(funds, dict) and funds.get("status") == "failure":
                        self.last_error = f"Dhan auth failed: {remarks}"
                        logger.error(self.last_error)
                        self._logged_in = False
                        return False

                self.access_token = token
                self._persist_token(token)
                self._session_time = datetime.now()
                self._logged_in = True
                self.last_error = ""
                logger.info(f"Dhan LOGIN SUCCESS | Client: {self.client_id}")
                # Push credentials; only force socket reconnect if token changed
                # and feed is down (see update_credentials).
                self._sync_live_feed_credentials(reconnect=(token != prev_token))
                return True

            except Exception as e:
                self.last_error = f"Dhan login error: {e}"
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
                logger.info("Dhan token nearing expiry — refreshing...")
                self.login()

    def _handle_api_error(self, context: str, err) -> bool:
        msg = str(err).lower()
        if any(
            k in msg
            for k in ("token", "unauthorized", "401", "auth", "expired", "invalid")
        ):
            logger.warning(f"{context}: session issue — re-login")
            self._logged_in = False
            return self.login()
        return False

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------
    def _security_id(self, symbol: str) -> str | None:
        # NSE instrument tokens match our Angel map (e.g. HDFCBANK=1333).
        return get_token(symbol)

    def _exchange_segment(self, symbol: str) -> str:
        ex = get_exchange(symbol)
        return "NSE_EQ" if ex.upper() == "NSE" else "BSE_EQ"

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
    # Account
    # -----------------------------------------------------------------------
    def get_account_info(self) -> dict | None:
        if self.paper is not None:
            marks = self._live_marks_for_positions(self.paper.positions.keys())
            info = self.paper.get_account_info(marks)
            logger.info(
                f"Dhan PAPER | Equity=Rs {info['equity']:,.2f} | "
                f"Cash=Rs {info['available_cash']:,.2f}"
            )
            return info

        self.ensure_session()
        if not self.dhan:
            return None
        try:
            resp = self.dhan.get_fund_limits()
            data = self._data(resp) if self._ok(resp) else None
            if not isinstance(data, dict):
                self.last_error = f"Fund limits error: {resp}"
                logger.error(self.last_error)
                if self._resp_looks_like_auth_failure(resp) and not getattr(
                    self, "_fund_reauth_attempted", False
                ):
                    self._fund_reauth_attempted = True
                    try:
                        if self._force_relogin("fund limits"):
                            return self.get_account_info()
                    finally:
                        self._fund_reauth_attempted = False
                return None

            available_cash = float(
                data.get("availabelBalance")
                or data.get("availableBalance")
                or data.get("sodLimit")
                or 0
            )
            used_margin = float(data.get("utilizedAmount") or data.get("usedMargin") or 0)
            # Prefer broker collateral/net fields when present; otherwise cash-only
            # understates equity after CNC buys and falsely trips daily drawdown.
            net = float(
                data.get("net")
                or data.get("totalBalance")
                or data.get("availabelBalance")
                or data.get("availableBalance")
                or data.get("withdrawableBalance")
                or available_cash
            )
            holdings_mtm = self._holdings_mark_to_market()
            # True book equity ≈ free cash + long CNC/stock MTM.
            # If broker net already includes collateral, don't double-count:
            # only add MTM when net looks like cash (≈ available) while we hold stock.
            equity = net
            if holdings_mtm > 0:
                if abs(net - available_cash) < max(1.0, 0.01 * max(available_cash, 1.0)):
                    equity = available_cash + holdings_mtm
                    logger.info(
                        f"Dhan equity adjusted with holdings MTM "
                        f"cash={available_cash:,.2f} + mtm={holdings_mtm:,.2f} "
                        f"→ equity={equity:,.2f}"
                    )
            info = {
                "equity": equity if equity > 0 else available_cash,
                "available_cash": available_cash,
                "used_margin": used_margin,
                "net": net,
                "holdings_mtm": round(holdings_mtm, 2),
                "paper": False,
            }
            logger.info(
                f"Dhan LIVE | Equity={info['equity']:,.2f} | Cash={available_cash:,.2f} | "
                f"HoldingsMTM={holdings_mtm:,.2f} | Used={used_margin:,.2f}"
            )
            return info
        except Exception as e:
            if self._handle_api_error("fund limits", e):
                return self.get_account_info()
            self.last_error = f"Fund limits error: {e}"
            logger.error(self.last_error, exc_info=True)
            return None

    def _holdings_mark_to_market(self) -> float:
        """Sum qty × ltp for open equity positions/holdings (CNC book)."""
        total = 0.0
        try:
            positions = self.get_open_positions() or {}
            for pos in positions.values():
                qty = float(pos.get("qty") or 0)
                px = float(pos.get("current_price") or 0)
                if px <= 0:
                    px = float(pos.get("avg_entry_price") or 0)
                if qty > 0 and px > 0:
                    total += qty * px
        except Exception as e:
            logger.debug(f"holdings MTM failed: {e}")
        return total

    def _live_marks_for_positions(self, symbols) -> dict[str, float]:
        marks = {}
        for symbol in symbols:
            quote = self.get_latest_quote(symbol)
            if quote and quote.get("ltp"):
                marks[symbol] = float(quote["ltp"])
        return marks

    def _backfill_zero_ltp_from_quotes(self, pos_dict: dict) -> None:
        """When Dhan positions/holdings omit LTP, fill from quote/WS/candle cache.

        Dashboard CURRENT PRICE / MARKET VALUE otherwise show ₹0 and -100% PnL%.
        """
        for symbol, pos in pos_dict.items():
            if not isinstance(pos, dict):
                continue
            ltp = float(pos.get("current_price") or 0)
            if ltp > 0:
                continue
            try:
                quote = self.get_latest_quote(symbol)
            except Exception as e:
                logger.debug(f"{symbol}: LTP backfill quote failed: {e}")
                continue
            quote_ltp = float((quote or {}).get("ltp") or 0)
            if quote_ltp <= 0:
                continue
            qty = abs(float(pos.get("qty") or 0))
            buy = float(pos.get("avg_entry_price") or 0)
            pos["current_price"] = quote_ltp
            pos["market_value"] = qty * quote_ltp
            if buy > 0 and qty > 0:
                pos["unrealized_pl"] = (quote_ltp - buy) * qty
                pos["unrealized_plpc"] = (quote_ltp - buy) / buy
            src = (quote or {}).get("source") or "quote"
            pos["ltp_source"] = src
            logger.info(
                f"{symbol}: backfilled LTP from {src} → {quote_ltp:.2f} "
                f"(Dhan positions/holdings had 0)"
            )

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
            ts_raw = data["timestamp"]
            ts = None
            converter = getattr(self.dhan, "convert_to_date_time", None) if self.dhan else None
            # Prefer epoch conversion; only use SDK converter for real datetime results
            try:
                if callable(converter) and ts_raw and isinstance(ts_raw[0], (int, float)):
                    converted = [converter(t) for t in ts_raw]
                    from datetime import datetime as _dt

                    if converted and isinstance(converted[0], (_dt, pd.Timestamp)):
                        ts = converted
            except Exception:
                ts = None
            if ts is None:
                try:
                    ts = pd.to_datetime(list(ts_raw), unit="s", errors="coerce")
                except (TypeError, ValueError):
                    ts = pd.to_datetime(list(ts_raw), errors="coerce")
            n = len(data["open"])
            frame = {
                "open": data.get("open"),
                "high": data.get("high"),
                "low": data.get("low"),
                "close": data.get("close"),
                "volume": data.get("volume") or [0] * n,
            }
            # OI when Dhan returns it (F&O / commodities)
            if "oi" in data or "open_interest" in data:
                frame["oi"] = data.get("oi") or data.get("open_interest") or [0] * n
            df = pd.DataFrame(frame, index=pd.to_datetime(ts))
            return df.astype(float).sort_index()
        if isinstance(data, list) and data:
            # Fallback row format
            rows = []
            for row in data:
                if isinstance(row, dict):
                    rows.append(row)
                elif isinstance(row, (list, tuple)) and len(row) >= 5:
                    rows.append(
                        {
                            "timestamp": row[0],
                            "open": row[1],
                            "high": row[2],
                            "low": row[3],
                            "close": row[4],
                            "volume": row[5] if len(row) > 5 else 0,
                            "oi": row[6] if len(row) > 6 else 0,
                        }
                    )
            if not rows:
                return None
            df = pd.DataFrame(rows)
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df.set_index("timestamp", inplace=True)
            cols = ["open", "high", "low", "close", "volume"]
            if "oi" in df.columns:
                cols.append("oi")
            return df[cols].astype(float).sort_index()
        return None

    def get_historical_candles(self, symbol: str, timeframe: str = "1Hour", days: int = 30) -> pd.DataFrame | None:
        """Fetches historical candles for equities, F&O underlyings, MCX, or Currency (OI when available)."""
        return self.get_historical_bars(symbol, days=days)

    @staticmethod
    def _segment_hint_for_symbol(symbol: str) -> str:
        sym = (symbol or "").strip().upper()
        if sym in ("USDINR", "EURINR", "GBPINR", "JPYINR"):
            return "NSE_CURRENCY"
        if sym in ("CRUDEOIL", "GOLD", "SILVER", "NATURALGAS"):
            return "MCX_COMM"
        if sym in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            return "IDX_I"
        return "NSE"

    @staticmethod
    def _candle_interval_minutes() -> int:
        """Map config.TIMEFRAME → Dhan intraday interval minutes."""
        tf = str(getattr(config, "TIMEFRAME", "5") or "5").strip().lower()
        aliases = {
            "1": 1,
            "1m": 1,
            "1min": 1,
            "5": 5,
            "5m": 5,
            "5min": 5,
            "15": 15,
            "15m": 15,
            "15min": 15,
            "25": 25,
            "25m": 25,
            "60": 60,
            "1h": 60,
            "1hour": 60,
            "hour": 60,
        }
        return aliases.get(tf, 5)

    def get_historical_bars(self, symbol: str, days: int = 300) -> pd.DataFrame | None:
        self.ensure_session()
        if not self.dhan:
            return None

        hint = self._segment_hint_for_symbol(symbol)
        sec_info = resolve_instrument_info(symbol, exchange_segment=hint)
        sec_id = sec_info.get("security_id") if not is_placeholder_security_id(sec_info.get("security_id")) else None
        sec_id = sec_id or self._security_id(symbol)
        if not sec_id or is_placeholder_security_id(sec_id):
            logger.error(f"No security_id for {symbol}")
            return None

        interval = self._candle_interval_minutes()
        # Shorter TF → fewer calendar days needed for LOOKBACK_BARS
        if interval <= 5:
            days = min(days, 10)
        elif interval <= 15:
            days = min(days, 20)

        now_ts = time.time()
        cache_key = f"{symbol}:{interval}"
        if cache_key in self._candle_cache:
            cache_time, cached_df = self._candle_cache[cache_key]
            if now_ts - cache_time < CANDLE_CACHE_SEC:
                return cached_df
        # legacy key
        if symbol in self._candle_cache:
            cache_time, cached_df = self._candle_cache[symbol]
            if now_ts - cache_time < CANDLE_CACHE_SEC:
                return cached_df

        segment = sec_info.get("exchange") or self._exchange_segment(symbol)
        # Dhan historical API segment strings
        if segment == "IDX_I":
            segment = "IDX_I"
        elif segment == "NSE_CURRENCY":
            segment = "NSE_CURRENCY"
        inst_type = sec_info.get("instrument_type") or ""
        if symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY") or inst_type == "INDEX":
            inst_type = "INDEX"
            if segment in ("IDX_I", "NSE_EQ"):
                segment = "IDX_I"
        elif segment == "NSE_FNO" or inst_type in ("FUTIDX", "FUTSTK", "OPTIDX", "OPTSTK"):
            inst_type = inst_type if inst_type in ("FUTIDX", "FUTSTK") else "FUTIDX"
        elif segment == "MCX_COMM" or inst_type == "FUTCOM":
            inst_type = "FUTCOM"
        elif segment in ("NSE_CURRENCY", "NSE_CURR", "BSE_CURRENCY") or inst_type == "FUTCUR":
            inst_type = "FUTCUR"
            segment = "NSE_CURRENCY"
        else:
            inst_type = "EQUITY"
            segment = segment if segment in ("NSE_EQ", "BSE_EQ") else "NSE_EQ"

        to_date = datetime.now()
        # Currency/MCX need wider lookback — thin sessions + contract rolls
        fetch_days = max(days, 45 if inst_type in ("FUTCUR", "FUTCOM") else days)
        from_date = to_date - timedelta(days=fetch_days)
        frames: list[pd.DataFrame] = []

        segment_candidates = [segment]
        if inst_type == "FUTCUR":
            for alt in ("NSE_CURRENCY", "NSE_CURR"):
                if alt not in segment_candidates:
                    segment_candidates.append(alt)

        chunk_start = from_date
        try:
            while chunk_start < to_date:
                chunk_end = min(chunk_start + timedelta(days=INTRADAY_CHUNK_DAYS), to_date)
                part = None
                for seg_try in segment_candidates:
                    self._throttle_candles()
                    resp = self.dhan.intraday_minute_data(
                        security_id=str(sec_id),
                        exchange_segment=seg_try,
                        instrument_type=inst_type,
                        from_date=chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                        to_date=chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
                        interval=interval,
                    )
                    if self._ok(resp):
                        part = self._parse_intraday(self._data(resp))
                        if part is not None and not part.empty:
                            frames.append(part)
                            segment = seg_try
                            break
                    else:
                        logger.debug(f"{symbol}: intraday chunk miss ({seg_try}): {resp}")
                chunk_start = chunk_end

            # Daily fallback when intraday empty (common for rolled FX/MCX contracts)
            if not frames:
                daily_fn = getattr(self.dhan, "historical_daily_data", None)
                if callable(daily_fn):
                    for seg_try in segment_candidates:
                        try:
                            self._throttle_candles()
                            resp = daily_fn(
                                security_id=str(sec_id),
                                exchange_segment=seg_try,
                                instrument_type=inst_type,
                                from_date=from_date.strftime("%Y-%m-%d"),
                                to_date=to_date.strftime("%Y-%m-%d"),
                            )
                            if self._ok(resp):
                                part = self._parse_intraday(self._data(resp))
                                if part is not None and not part.empty:
                                    frames.append(part)
                                    logger.info(
                                        f"{symbol}: using daily history fallback "
                                        f"({len(part)} bars, seg={seg_try})"
                                    )
                                    break
                        except Exception as daily_e:
                            logger.debug(f"{symbol}: daily history miss ({seg_try}): {daily_e}")

            if not frames:
                logger.warning(
                    f"{symbol}: No Dhan candle data "
                    f"(sec_id={sec_id} seg={segment} inst={inst_type} "
                    f"contract={sec_info.get('trading_symbol')})"
                )
                if cache_key in self._candle_cache:
                    return self._candle_cache[cache_key][1]
                if symbol in self._candle_cache:
                    return self._candle_cache[symbol][1]
                return None

            df = pd.concat(frames).sort_index()
            df = df[~df.index.duplicated(keep="last")]
            self._candle_cache[cache_key] = (time.time(), df)
            logger.debug(
                f"{symbol}: Fetched {len(df)} bars from Dhan (interval={interval}m)"
            )
            return df

        except Exception as e:
            if self._handle_api_error(f"{symbol} candles", e):
                return self.get_historical_bars(symbol, days=days)
            logger.error(f"{symbol}: Dhan historical error: {e}", exc_info=True)
            if cache_key in self._candle_cache:
                return self._candle_cache[cache_key][1]
            if symbol in self._candle_cache:
                return self._candle_cache[symbol][1]
            return None

    # -----------------------------------------------------------------------
    # Quotes
    # -----------------------------------------------------------------------
    def _extract_ltp(self, data, segment: str, sec_id: str) -> float | None:
        """Parse LTP from ticker / ohlc / quote marketfeed payloads."""
        if not isinstance(data, dict):
            return None

        bucket = data.get(segment) or data.get(str(sec_id)) or data
        if not isinstance(bucket, dict):
            return None

        node = bucket.get(str(sec_id)) or bucket.get(int(sec_id)) or bucket
        if not isinstance(node, dict):
            return None

        ltp = (
            node.get("last_price")
            or node.get("LTP")
            or node.get("ltp")
            or node.get("last_trade_price")
            or (node.get("ohlc") or {}).get("close")
            or node.get("close")
            or node.get("average_price")
        )
        if ltp is None:
            return None
        try:
            val = float(ltp)
            return val if val > 0 else None
        except (TypeError, ValueError):
            return None

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
            logger.warning(
                f"Dhan marketfeed cooldown {MARKETFEED_COOLDOWN_SEC:.0f}s ({reason})"
            )

    def _quote_warn(self, symbol: str, msg: str) -> None:
        now = time.time()
        last = self._quote_warn_at.get(symbol, 0.0)
        if now - last < 60.0:
            logger.debug(msg)
            return
        self._quote_warn_at[symbol] = now
        logger.warning(msg)

    def _quote_from_candle_cache(self, symbol: str) -> dict | None:
        """Fallback when marketfeed LTP is unavailable (rate limit / Data API).

        Never invents fake prices (old Rs1500 paper_fallback caused bogus MCX P&L).
        """
        cached = self._candle_cache.get(symbol)
        if cached:
            _, df = cached
            if df is not None and not df.empty and "close" in df.columns:
                close = float(df["close"].iloc[-1])
                if close > 0:
                    self._quote_warn(
                        symbol,
                        f"{symbol}: marketfeed LTP unavailable — using last candle close {close:.2f}",
                    )
                    return self._cache_quote(
                        symbol,
                        {
                            "ltp": close,
                            "ask_price": close,
                            "symbol": symbol,
                            "exchange": get_exchange(symbol),
                            "source": "candle_close",
                        },
                    )
        return None

    def get_latest_quote(self, symbol: str) -> dict | None:
        cached = self._cached_quote(symbol)
        if cached:
            return cached

        # Check Dhan Live Feed WebSocket cache first (Paid Data API)
        ws_feed = get_live_feed_manager()
        ws_quote = ws_feed.get_live_quote(symbol)
        if ws_quote:
            return self._cache_quote(symbol, ws_quote)

        # Prefer candles while marketfeed is cooling down (dashboard polls often).
        if time.time() < self._marketfeed_cooldown_until:
            return self._quote_from_candle_cache(symbol)

        self.ensure_session()
        if not self.dhan:
            return self._quote_from_candle_cache(symbol)

        sec_id, _exch = _resolved_security_id(symbol)
        if not sec_id:
            logger.error(f"No security_id for {symbol}")
            return self._quote_from_candle_cache(symbol)
            logger.error(f"No security_id for {symbol}")
            return self._quote_from_candle_cache(symbol)

        segment = self._exchange_segment(symbol)
        securities = {segment: [int(sec_id)]}
        try:
            # One endpoint only — cascading ltp→ohlc→quote caused 429 storms.
            method = getattr(self.dhan, "ticker_data", None) or getattr(
                self.dhan, "ohlc_data", None
            )
            if method is None:
                return self._quote_from_candle_cache(symbol)

            resp = method(securities)
            status_code = None
            if isinstance(resp, dict):
                remarks = resp.get("remarks") or {}
                if isinstance(remarks, dict):
                    status_code = remarks.get("status_code") or remarks.get("error_code")

            if not self._ok(resp):
                raw = str(resp).lower()
                if "429" in raw or status_code in (429, "429", "DH-904"):
                    self._arm_marketfeed_cooldown("HTTP 429 rate limit")
                elif "401" in raw or status_code in (401, "401", "DH-901"):
                    self._arm_marketfeed_cooldown("HTTP 401 / auth")
                else:
                    self._quote_warn(symbol, f"{symbol}: LTP parse failed — {resp}")
                    # Soft cooldown so dashboard doesn't retry every 3s
                    self._arm_marketfeed_cooldown("marketfeed failure")
                return self._quote_from_candle_cache(symbol)

            ltp = self._extract_ltp(self._data(resp), segment, str(sec_id))
            if ltp is None:
                self._quote_warn(symbol, f"{symbol}: LTP missing in marketfeed payload")
                return self._quote_from_candle_cache(symbol)

            return self._cache_quote(
                symbol,
                {
                    "ltp": ltp,
                    "ask_price": ltp,
                    "symbol": symbol,
                    "exchange": get_exchange(symbol),
                    "source": "ticker_data",
                },
            )
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "rate" in msg:
                self._arm_marketfeed_cooldown("exception 429")
                return self._quote_from_candle_cache(symbol)
            if self._handle_api_error(f"{symbol} LTP", e):
                return self.get_latest_quote(symbol)
            logger.error(f"{symbol}: LTP error: {e}", exc_info=True)
            return self._quote_from_candle_cache(symbol)

    # -----------------------------------------------------------------------
    # Live gate
    # -----------------------------------------------------------------------
    def _assert_live_allowed(self, action: str) -> bool:
        if self.paper is not None:
            return True
        if not config.LIVE_CONFIRMED:
            logger.critical(
                f"BLOCKED {action}: Live trading is OFF. "
                f"Keep INDIA_PAPER=true for testing, or set "
                f"LIVE_TRADING=true and LIVE_CONFIRM=YES_REAL_MONEY for real money."
            )
            self.last_error = "Live trading not confirmed"
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

    @staticmethod
    def _resp_looks_like_auth_failure(resp) -> bool:
        text = str(resp or "").lower()
        return any(
            k in text
            for k in (
                "dh-901",
                "invalid_authentication",
                "access token is invalid",
                "token is invalid or expired",
                "unauthorized",
            )
        )

    def _force_relogin(self, context: str) -> bool:
        logger.warning(f"{context}: forcing Dhan re-login (PIN/TOTP if configured)")
        self._logged_in = False
        self.dhan = None
        return self.login()

    def _parse_order_payload(self, resp) -> dict:
        if not isinstance(resp, dict):
            return {}
        data = self._data(resp)
        if isinstance(data, list) and data:
            first = data[0]
            return first if isinstance(first, dict) else {}
        if isinstance(data, dict):
            return data
        return resp

    def get_order_status(self, order_id: str) -> tuple[str, str]:
        """
        Return (status_upper, detail) for a day order.
        detail includes oms error text when rejected.
        """
        if self.paper is not None or not order_id:
            return "TRADED", "paper"
        self.ensure_session()
        if not self.dhan:
            return "UNKNOWN", "no session"
        try:
            method = getattr(self.dhan, "get_order_by_id", None)
            if not callable(method):
                return "UNKNOWN", "get_order_by_id unavailable"
            resp = method(str(order_id))
            if self._resp_looks_like_auth_failure(resp):
                if self._force_relogin("get_order_by_id"):
                    resp = method(str(order_id))
            row = self._parse_order_payload(resp)
            status = str(
                row.get("orderStatus") or row.get("order_status") or row.get("status") or ""
            ).upper()
            detail = str(
                row.get("omsErrorDescription")
                or row.get("oms_error_description")
                or row.get("rejectionReason")
                or row.get("remarks")
                or row.get("message")
                or status
                or resp
            )
            return status or "UNKNOWN", detail[:300]
        except Exception as e:
            if self._handle_api_error(f"order status {order_id}", e):
                return self.get_order_status(order_id)
            logger.warning(f"Order status lookup failed for {order_id}: {e}")
            return "UNKNOWN", str(e)

    def get_order_fill_price(self, order_id: str) -> float:
        """Average traded / fill price from order book; 0 if unknown."""
        if self.paper is not None or not order_id:
            return 0.0
        self.ensure_session()
        if not self.dhan:
            return 0.0
        try:
            method = getattr(self.dhan, "get_order_by_id", None)
            if not callable(method):
                return 0.0
            resp = method(str(order_id))
            row = self._parse_order_payload(resp)
            for key in (
                "averageTradedPrice",
                "avgTradedPrice",
                "averagePrice",
                "avgPrice",
                "tradedPrice",
                "price",
            ):
                raw = row.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    px = float(raw)
                except (TypeError, ValueError):
                    continue
                if px > 0:
                    return px
        except Exception as e:
            logger.debug(f"fill price lookup failed for {order_id}: {e}")
        return 0.0

    @staticmethod
    def _row_fill_price(row: dict) -> float:
        if not isinstance(row, dict):
            return 0.0
        for key in (
            "averageTradedPrice",
            "avgTradedPrice",
            "averagePrice",
            "avgPrice",
            "tradedPrice",
            "traded_price",
            "price",
        ):
            raw = row.get(key)
            if raw is None or raw == "":
                continue
            try:
                px = float(raw)
            except (TypeError, ValueError):
                continue
            if px > 0:
                return px
        return 0.0

    @staticmethod
    def _row_event_time(row: dict) -> str:
        if not isinstance(row, dict):
            return ""
        for key in (
            "exchangeTime",
            "updateTime",
            "createTime",
            "exchange_time",
            "update_time",
            "create_time",
        ):
            val = row.get(key)
            if val:
                return str(val)
        return ""

    def _symbol_from_order_row(self, row: dict) -> str:
        if not isinstance(row, dict):
            return ""
        token_to_symbol = {
            str(info["token"]): sym for sym, info in INDIA_INSTRUMENTS.items()
        }
        sec_id = str(row.get("securityId") or row.get("security_id") or "")
        if sec_id and sec_id in token_to_symbol:
            return token_to_symbol[sec_id]
        trading_symbol = (
            row.get("tradingSymbol")
            or row.get("trading_symbol")
            or row.get("symbol")
            or row.get("customSymbol")
            or ""
        )
        return str(trading_symbol).replace("-EQ", "").strip().upper()

    def _list_order_rows(self) -> list[dict]:
        """Today's order book rows (empty on paper / failure)."""
        if self.paper is not None or not self.dhan:
            return []
        try:
            method = getattr(self.dhan, "get_order_list", None)
            if not callable(method):
                return []
            resp = method()
            data = self._data(resp) if self._ok(resp) else None
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        except Exception as e:
            logger.debug(f"order list lookup failed: {e}")
        return []

    def _list_trade_book_rows(self) -> list[dict]:
        """Today's trade-book rows (empty on paper / failure)."""
        if self.paper is not None or not self.dhan:
            return []
        try:
            method = getattr(self.dhan, "get_trade_book", None)
            if not callable(method):
                return []
            resp = method()
            data = self._data(resp) if self._ok(resp) else None
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
            if isinstance(data, dict):
                return [data]
        except Exception as e:
            logger.debug(f"trade book lookup failed: {e}")
        return []

    def latest_sell_fill_price(self, symbol: str) -> float:
        """
        Latest today's SELL (incl. SL) fill for symbol from Dhan trade book / order list.

        Prefer trade-book tradedPrice (actual executions), then order-book
        averageTradedPrice for TRADED/PART_TRADED SELL orders. Returns 0 if unknown.
        """
        if self.paper is not None or not symbol:
            return 0.0
        self.ensure_session()
        if not self.dhan:
            return 0.0

        want = str(symbol).upper().replace("-EQ", "").strip()
        best_px = 0.0
        best_ts = ""

        for row in self._list_trade_book_rows():
            side = str(
                row.get("transactionType") or row.get("transaction_type") or ""
            ).upper()
            if side != "SELL":
                continue
            if self._symbol_from_order_row(row) != want:
                continue
            px = self._row_fill_price(row)
            if px <= 0:
                continue
            ts = self._row_event_time(row)
            if not best_ts or ts >= best_ts:
                best_ts = ts or best_ts
                best_px = px

        if best_px > 0:
            return best_px

        for row in self._list_order_rows():
            side = str(
                row.get("transactionType") or row.get("transaction_type") or ""
            ).upper()
            if side != "SELL":
                continue
            status = str(
                row.get("orderStatus") or row.get("order_status") or row.get("status") or ""
            ).upper()
            if status and status not in _FILL_STATUSES:
                continue
            if self._symbol_from_order_row(row) != want:
                continue
            # Some payloads omit filledQty but still carry averageTradedPrice.
            px = self._row_fill_price(row)
            if px <= 0:
                continue
            ts = self._row_event_time(row)
            if not best_ts or ts >= best_ts:
                best_ts = ts or best_ts
                best_px = px

        return best_px

    def resolve_exit_fill_price(
        self,
        symbol: str,
        order_id: str | None = None,
        *,
        fallback: float = 0.0,
    ) -> float:
        """Prefer broker fill (order id → today's SELL history), then LTP, then fallback."""
        if order_id:
            fill = self.get_order_fill_price(order_id)
            if fill > 0:
                return fill
        try:
            hist = float(self.latest_sell_fill_price(symbol) or 0)
            if hist > 0:
                return hist
        except Exception as e:
            logger.debug(f"{symbol}: sell-history fill lookup failed: {e}")
        try:
            quote = self.get_latest_quote(symbol)
            if quote and quote.get("ltp"):
                ltp = float(quote["ltp"])
                if ltp > 0:
                    return ltp
        except Exception:
            pass
        return float(fallback or 0)

    def confirm_live_order(
        self,
        order_id: str,
        symbol: str,
        timeout_sec: float | None = None,
    ) -> tuple[bool, str]:
        """
        Poll until terminal status or timeout.

        Returns (accepted, status_or_reason).
        - REJECTED/CANCELLED/EXPIRED → (False, reason) + buy cooldown
        - TRADED/PART_TRADED → (True, status)
        - Still open after timeout → (True, PENDING) + pending cooldown
          so we do not spam a second entry while the first sits.
        """
        if self.paper is not None:
            return True, "paper"
        if not order_id:
            return False, "missing order id"

        timeout = float(
            timeout_sec
            if timeout_sec is not None
            else getattr(config, "ORDER_CONFIRM_TIMEOUT_SEC", 8)
        )
        deadline = time.time() + max(1.0, timeout)
        last_status = "UNKNOWN"
        last_detail = ""
        while time.time() < deadline:
            last_status, last_detail = self.get_order_status(order_id)
            if last_status in _REJECT_STATUSES:
                msg = f"{last_status}: {last_detail}"
                logger.error(f"{symbol}: live order {order_id} rejected — {msg}")
                self.last_error = msg
                order_guards.block_buy_after_reject(symbol, last_detail)
                return False, msg
            if last_status in _FILL_STATUSES:
                logger.info(f"{symbol}: order {order_id} confirmed {last_status}")
                return True, last_status
            time.sleep(0.75)

        if last_status in _OPEN_STATUSES or last_status in ("", "UNKNOWN"):
            logger.warning(
                f"{symbol}: order {order_id} still {last_status or 'PENDING'} "
                f"after {timeout:.0f}s — treating as working, cooldown on re-buy"
            )
            order_guards.block_buy_after_pending(symbol, order_id)
            return True, last_status or "PENDING"

        # Unexpected terminal-ish status
        if last_status in _REJECT_STATUSES:
            order_guards.block_buy_after_reject(symbol, last_detail)
            return False, f"{last_status}: {last_detail}"
        return True, last_status

    # -----------------------------------------------------------------------
    # Positions
    # -----------------------------------------------------------------------
    def get_open_positions(self) -> dict:
        if self.paper is not None:
            marks = self._live_marks_for_positions(self.paper.positions.keys())
            pos_dict = self.paper.get_open_positions(marks)
            if pos_dict:
                logger.info(f"India PAPER positions: {list(pos_dict.keys())}")
            return pos_dict

        self.ensure_session()
        if not self.dhan:
            return {}

        pos_dict: dict = {}
        token_to_symbol = {
            info["token"]: sym for sym, info in INDIA_INSTRUMENTS.items()
        }
        mis_mode = _trading_product_is_mis()

        try:
            resp = self.dhan.get_positions()
            data = self._data(resp) if self._ok(resp) else None
            for pos in data or []:
                if not isinstance(pos, dict):
                    continue
                net_qty = int(
                    float(
                        pos.get("netQty")
                        or pos.get("net_qty")
                        or pos.get("quantity")
                        or 0
                    )
                )
                product = _row_product(pos)
                if not should_include_open_position(
                    net_qty=net_qty,
                    product=product,
                    trading_is_mis=mis_mode,
                ):
                    continue
                sec_id = str(pos.get("securityId") or pos.get("security_id") or "")
                trading_symbol = (
                    pos.get("tradingSymbol")
                    or pos.get("trading_symbol")
                    or pos.get("symbol")
                    or ""
                )
                symbol = token_to_symbol.get(sec_id) or str(trading_symbol).replace(
                    "-EQ", ""
                )
                buy_price = float(
                    pos.get("avgCostPrice")
                    or pos.get("averagePrice")
                    or pos.get("buyAvg")
                    or 0
                )
                ltp = float(pos.get("ltp") or pos.get("lastTradedPrice") or 0)
                pnl = float(pos.get("unrealizedProfit") or pos.get("pnl") or 0)
                pnl_pct = ((ltp - buy_price) / buy_price) if buy_price > 0 else 0
                pos_dict[symbol] = {
                    "qty": abs(net_qty),
                    "avg_entry_price": buy_price,
                    "current_price": ltp,
                    "market_value": abs(net_qty) * ltp,
                    "unrealized_pl": pnl,
                    "unrealized_plpc": pnl_pct,
                    "trading_symbol": trading_symbol,
                    "token": sec_id,
                    "source": "position",
                    "product": product or ("INTRADAY" if mis_mode else "CNC"),
                    "side": "SELL" if net_qty < 0 else "BUY",
                }
        except Exception as e:
            logger.error(f"Dhan positions error: {e}", exc_info=True)

        # CNC sleeve may still show overnight holdings as exposure.
        # MIS bots must not — holdings would block new ORB entries and confuse SL/square-off.
        if not mis_mode:
            try:
                resp = self.dhan.get_holdings()
                data = self._data(resp) if self._ok(resp) else None
                for h in data or []:
                    if not isinstance(h, dict):
                        continue
                    qty = int(
                        float(
                            h.get("availableQty")
                            or h.get("totalQty")
                            or h.get("quantity")
                            or 0
                        )
                    )
                    if qty <= 0:
                        continue
                    sec_id = str(h.get("securityId") or h.get("security_id") or "")
                    trading_symbol = (
                        h.get("tradingSymbol")
                        or h.get("trading_symbol")
                        or h.get("symbol")
                        or ""
                    )
                    symbol = token_to_symbol.get(sec_id) or str(trading_symbol).replace(
                        "-EQ", ""
                    )
                    if symbol in pos_dict:
                        continue
                    buy_price = float(
                        h.get("avgCostPrice") or h.get("averagePrice") or 0
                    )
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
                        "trading_symbol": trading_symbol,
                        "token": sec_id,
                        "source": "holding",
                        "product": "CNC",
                        "side": "BUY",
                    }
            except Exception as e:
                logger.warning(f"Dhan holdings fetch warning: {e}")

        if pos_dict:
            self._backfill_zero_ltp_from_quotes(pos_dict)
            logger.info(f"India open positions: {list(pos_dict.keys())}")
        return pos_dict

    # -----------------------------------------------------------------------
    # Orders
    # -----------------------------------------------------------------------
    def place_buy_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        place_stoploss: bool = True,
        stop_loss_pct: float | None = None,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
        atr: float | None = None,
        product_type: str | None = None,
    ) -> str | None:
        if not self._assert_live_allowed(f"BUY {symbol}"):
            return None
        if qty <= 0 or limit_price <= 0:
            logger.error(f"Invalid buy params for {symbol}: qty={qty} price={limit_price}")
            return None

        # NSE rejects off-tick prices (e.g. DIVISLAB 8308.30 with ₹0.50 tick → 16283).
        raw_limit = float(limit_price)
        limit_price = round_buy_limit(raw_limit)
        if abs(limit_price - raw_limit) >= 1e-9:
            logger.info(
                f"{symbol}: buy limit rounded to NSE tick "
                f"{raw_limit:.4f} → {limit_price:.2f}"
            )
        if stop_loss_price is not None and float(stop_loss_price) > 0:
            stop_loss_price = round_to_nse_tick(float(stop_loss_price), mode="floor")
        if take_profit_price is not None and float(take_profit_price) > 0:
            take_profit_price = round_to_nse_tick(float(take_profit_price), mode="nearest")

        if stop_loss_price is None:
            sl_pct = stop_loss_pct if stop_loss_pct is not None else config.STOP_LOSS_PCT
            stop_loss_price = round_to_nse_tick(limit_price * (1 - sl_pct), mode="floor")

        p_type = (product_type or config.INDIA_PRODUCT_TYPE or "CNC").strip().upper()
        dhan_ptype = _dhan_product_type(p_type)
        margin_req = self.get_margin_required(symbol, qty, limit_price, p_type)

        if self.paper is not None:
            quote = self.get_latest_quote(symbol)
            fill = float(quote["ltp"]) if quote else float(limit_price)
            if self.paper.cash < margin_req:
                logger.warning(f"[PAPER BUY] Blocked {symbol}: Margin required (Rs{margin_req:,.2f}) > Available cash (Rs{self.paper.cash:,.2f})")
                return None
            return self.paper.buy(
                symbol,
                qty,
                fill,
                stop_loss=stop_loss_price,
                take_profit=take_profit_price,
                atr=atr,
            )

        self.ensure_session()
        if not self.dhan:
            return None
        sec_id, exch_seg = _resolved_security_id(symbol)
        if not sec_id:
            logger.error(f"Cannot place order — no security_id for {symbol}")
            return None

        # Live Super Order preferential routing if target & SL available
        if stop_loss_price and take_profit_price:
            logger.info(f"[INDIA] Routing live buy via Super Order for {symbol}...")
            oid = self.place_super_order(
                symbol, qty, limit_price, take_profit_price, stop_loss_price, product_type=p_type
            )
            if oid:
                ok, status = self.confirm_live_order(oid, symbol)
                if not ok:
                    logger.error(
                        f"SUPER BUY not confirmed for {symbol} | Order ID={oid} | {status}"
                    )
                    return None
                # Swing CNC: also arm Forever GTT for multi-day SL backup
                if p_type == "CNC" and stop_loss_price:
                    self.place_forever_order(
                        symbol,
                        qty,
                        trigger_price=stop_loss_price,
                        price=round_sell_limit(stop_loss_price * 0.995),
                        transaction_type="SELL",
                        order_flag="SINGLE",
                        product_type=p_type,
                    )
                logger.warning(
                    f"LIVE SUPER BUY (Dhan) | {symbol} | Qty={qty} | "
                    f"Order ID={oid} | Status={status}"
                )
                return oid

        try:
            entry_price = round_buy_limit(limit_price * 1.001)
            raw = self.dhan.place_order(
                security_id=str(sec_id),
                exchange_segment=exch_seg,
                transaction_type=dhanhq.BUY,
                quantity=int(qty),
                order_type=dhanhq.LIMIT,
                product_type=dhan_ptype,
                price=float(entry_price),
                trigger_price=0,
                validity=dhanhq.DAY,
                tag=f"BOT-BUY-{symbol}"[:20],
            )
            order_id = self._extract_order_id(raw)
            if not order_id:
                logger.error(f"BUY rejected for {symbol}: {raw}")
                self.last_error = f"Buy rejected: {raw}"
                order_guards.block_buy_after_reject(symbol, str(raw)[:160])
                return None

            ok, status = self.confirm_live_order(order_id, symbol)
            if not ok:
                logger.error(
                    f"BUY not confirmed for {symbol} | Order ID={order_id} | {status}"
                )
                return None

            logger.warning(
                f"LIVE BUY ORDER (Dhan) | {symbol} | Product={p_type} ({dhan_ptype}) | Qty={qty} | "
                f"Limit={entry_price:.2f} | Order ID={order_id} | Status={status}"
            )
            if place_stoploss and stop_loss_price:
                if p_type == "CNC":
                    # Multi-day swing: Forever GTT for SL (DAY SL orders expire)
                    self.place_forever_order(
                        symbol,
                        qty,
                        trigger_price=stop_loss_price,
                        price=round_sell_limit(stop_loss_price * 0.995),
                        transaction_type="SELL",
                        product_type=p_type,
                    )
                else:
                    self.place_stoploss_order(symbol, qty, stop_loss_price, product_type=p_type)
            return order_id
        except Exception as e:
            if self._handle_api_error(f"BUY {symbol}", e) and not getattr(
                self, "_buy_retrying", False
            ):
                self._buy_retrying = True
                try:
                    return self.place_buy_order(
                        symbol,
                        qty,
                        limit_price,
                        place_stoploss=place_stoploss,
                        stop_loss_price=stop_loss_price,
                        take_profit_price=take_profit_price,
                        atr=atr,
                        product_type=p_type,
                    )
                finally:
                    self._buy_retrying = False
            logger.error(f"Failed to place BUY for {symbol}: {e}", exc_info=True)
            return None

    def place_stoploss_order(
        self,
        symbol: str,
        qty: int,
        trigger_price: float,
        product_type: str | None = None,
    ) -> str | None:
        p_type = (product_type or config.INDIA_PRODUCT_TYPE or "CNC").strip().upper()
        dhan_ptype = _dhan_product_type(p_type)
        if self.paper is not None:
            logger.info(f"[PAPER] Soft stop-loss armed for {symbol} @ {trigger_price:.2f}")
            return f"PAPER-SL-{symbol}"

        if not self._assert_live_allowed(f"SL {symbol}"):
            return None

        self.ensure_session()
        if not self.dhan:
            return None
        sec_id, exch_seg = _resolved_security_id(symbol)
        if not sec_id or qty <= 0 or trigger_price <= 0:
            return None

        try:
            trigger_price = round_to_nse_tick(float(trigger_price), mode="nearest")
            limit_price = round_sell_limit(trigger_price * 0.995)
            raw = self.dhan.place_order(
                security_id=str(sec_id),
                exchange_segment=exch_seg,
                transaction_type=dhanhq.SELL,
                quantity=int(qty),
                order_type=dhanhq.SL,
                product_type=dhan_ptype,
                price=float(limit_price),
                trigger_price=float(trigger_price),
                validity=dhanhq.DAY,
                tag=f"BOT-SL-{symbol}"[:20],
            )
            order_id = self._extract_order_id(raw)
            if order_id:
                logger.warning(
                    f"LIVE STOP-LOSS (Dhan) | {symbol} | Product={p_type} | Qty={qty} | "
                    f"Trigger={trigger_price:.2f} | Order ID={order_id}"
                )
            else:
                logger.error(f"Stop-loss not accepted for {symbol}: {raw}")
            return order_id
        except Exception as e:
            logger.error(f"Failed to place stop-loss for {symbol}: {e}", exc_info=True)
            return None

    def place_sell_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float = 0,
        order_type: str = "MARKET",
        product_type: str | None = None,
    ) -> str | None:
        p_type = (product_type or config.INDIA_PRODUCT_TYPE or "CNC").strip().upper()
        dhan_ptype = _dhan_product_type(p_type)
        if not self._assert_live_allowed(f"SELL {symbol}"):
            return None

        if self.paper is not None:
            quote = self.get_latest_quote(symbol)
            fill = float(quote["ltp"]) if quote else float(limit_price or 0)
            if fill <= 0:
                logger.error(f"[PAPER] No LTP to sell {symbol}")
                return None
            return self.paper.sell(symbol, qty, fill)

        self.ensure_session()
        if not self.dhan:
            return None
        sec_id, exch_seg = _resolved_security_id(symbol)
        if not sec_id:
            logger.error(f"Cannot place sell — no security_id for {symbol}")
            return None

        try:
            dhan_order_type = dhanhq.MARKET
            price = 0.0
            if order_type == "MARKET" and limit_price <= 0:
                quote = self.get_latest_quote(symbol)
                if quote:
                    price = float(round_sell_limit(quote["ltp"] * 0.995))
                    dhan_order_type = dhanhq.LIMIT
                else:
                    dhan_order_type = dhanhq.MARKET
                    price = 0.0
            elif order_type == "LIMIT" or limit_price > 0:
                dhan_order_type = dhanhq.LIMIT
                price = float(round_sell_limit(limit_price))
                if price <= 0:
                    logger.error(f"Sell needs a limit price for {symbol}")
                    return None

            raw = self.dhan.place_order(
                security_id=str(sec_id),
                exchange_segment=exch_seg,
                transaction_type=dhanhq.SELL,
                quantity=int(qty),
                order_type=dhan_order_type,
                product_type=dhan_ptype,
                price=float(price),
                trigger_price=0,
                validity=dhanhq.DAY,
                tag=f"BOT-SELL-{symbol}"[:20],
            )
            order_id = self._extract_order_id(raw)
            if not order_id:
                logger.error(f"SELL rejected for {symbol}: {raw}")
                return None
            logger.warning(
                f"LIVE SELL ORDER (Dhan) | {symbol} | Product={p_type} ({dhan_ptype}) | "
                f"Qty={qty} | Order ID={order_id}"
            )
            return order_id
        except Exception as e:
            logger.error(f"Failed to place SELL for {symbol}: {e}", exc_info=True)
            return None

    def close_position(self, symbol: str) -> float | None:
        """
        Flatten symbol. Returns exit fill price on success, else None.
        (Truthy float so existing `if close_position(...)` checks keep working.)
        """
        positions = self.get_open_positions()
        if symbol not in positions:
            logger.warning(f"{symbol}: No open position to close")
            return None
        pos = positions[symbol]
        qty = int(pos["qty"])
        fallback = float(
            pos.get("current_price") or pos.get("avg_entry_price") or 0
        )
        order_id = self.place_sell_order(symbol, qty, order_type="MARKET")
        if not order_id:
            return None

        if self.paper is not None:
            # Paper sell id is not a broker order; use mark used by portfolio.
            fill = fallback
            try:
                quote = self.get_latest_quote(symbol)
                if quote and quote.get("ltp"):
                    fill = float(quote["ltp"])
            except Exception:
                pass
            self.last_fill_price = fill
            logger.info(f"Position CLOSED for {symbol} (Qty={qty}) @ {fill:.2f} paper")
            return fill if fill > 0 else None

        try:
            self.confirm_live_order(order_id, symbol)
        except Exception as e:
            logger.debug(f"{symbol}: sell confirm warn: {e}")
        fill = self.resolve_exit_fill_price(symbol, order_id, fallback=fallback)
        self.last_fill_price = fill
        if fill <= 0:
            logger.warning(
                f"Position CLOSED for {symbol} (Qty={qty}) but fill price unknown"
            )
            return None
        logger.info(f"Position CLOSED for {symbol} (Qty={qty}) @ {fill:.2f}")
        return fill

    def check_sl_tp(self, risk_mgr) -> list[str]:
        closed_symbols = []
        positions = self.get_open_positions()

        for symbol, pos in positions.items():
            entry_price = float(pos["avg_entry_price"])
            current_price = float(pos.get("current_price") or 0)
            # Missing/zero LTP must NEVER trip SL (0 <= stop would force a fake total-loss exit).
            if current_price <= 0:
                logger.warning(
                    f"[INDIA SL/TP] {symbol}: skip — no usable mark "
                    f"(ltp={current_price})"
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
                    f"[INDIA SL] {symbol} hit stop! "
                    f"Entry={entry_price:.2f} Px={current_price:.2f} SL={sl_price:.2f}"
                )
            elif current_price >= tp_price:
                reason = "take_profit"
                logger.info(
                    f"[INDIA TP] {symbol} hit target! "
                    f"Entry={entry_price:.2f} Px={current_price:.2f} TP={tp_price:.2f}"
                )

            if reason:
                fill = self.close_position(symbol)
                if fill is not None:
                    closed_symbols.append(symbol)
                    risk_mgr.clear_trade(symbol)
                    try:
                        import trade_journal

                        trade_journal.record_exit(
                            "INDIA", symbol, float(fill), reason=reason
                        )
                    except Exception as je:
                        logger.debug(f"Journal exit skip: {je}")

        return closed_symbols

    def cancel_all_open_orders(self) -> bool:
        if self.paper is not None:
            logger.info("[PAPER] No broker orders to cancel")
            return True

        self.ensure_session()
        if not self.dhan:
            return False
        try:
            resp = self.dhan.get_order_list()
            data = self._data(resp) if self._ok(resp) else None
            if not data:
                return True
            cancelled = 0
            for order in data:
                if not isinstance(order, dict):
                    continue
                status = str(
                    order.get("orderStatus") or order.get("status") or ""
                ).upper()
                if status not in (
                    "PENDING",
                    "TRANSIT",
                    "OPEN",
                    "TRIGGER_PENDING",
                    "PART_TRADED",
                ):
                    continue
                oid = order.get("orderId") or order.get("order_id")
                if not oid:
                    continue
                try:
                    self.dhan.cancel_order(str(oid))
                    cancelled += 1
                except Exception as ce:
                    logger.warning(f"Cancel order {oid} failed: {ce}")
            logger.info(f"Cancelled {cancelled} open Dhan orders")
            return True
        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")
            return False

    # -----------------------------------------------------------------------
    # Advanced Dhan Order Types & Utilities (Super Orders, Forever Orders, Margin)
    # -----------------------------------------------------------------------
    def place_super_order(
        self,
        symbol: str,
        qty: int,
        limit_price: float,
        target_price: float,
        stop_loss_price: float,
        trailing_jump: float = 0.0,
        transaction_type: str = "BUY",
        product_type: str | None = None,
    ) -> str | None:
        """
        Dhan Super Order — Entry + Target + Stop Loss (+ optional Trailing SL)
        placed as a single broker-side OCO order.
        """
        p_type = (product_type or config.INDIA_PRODUCT_TYPE or "CNC").strip().upper()
        dhan_ptype = _dhan_product_type(p_type)
        if transaction_type.upper() == "BUY":
            limit_price = round_buy_limit(float(limit_price))
        else:
            limit_price = round_sell_limit(float(limit_price))
        target_price = round_to_nse_tick(float(target_price), mode="nearest")
        stop_loss_price = round_to_nse_tick(float(stop_loss_price), mode="floor")
        if self.paper is not None:
            logger.info(
                f"[PAPER] Super Order armed for {symbol} | Qty={qty} Entry={limit_price:.2f} "
                f"Target={target_price:.2f} SL={stop_loss_price:.2f}"
            )
            return self.paper.buy(
                symbol, qty, limit_price, stop_loss=stop_loss_price, take_profit=target_price
            )

        if not self._assert_live_allowed(f"SUPER ORDER {transaction_type} {symbol}"):
            return None

        self.ensure_session()
        if not self.dhan:
            return None

        sec_id, exch_seg = _resolved_security_id(symbol)
        if not sec_id:
            return None

        try:
            method = getattr(self.dhan, "place_super_order", None) or getattr(self.dhan, "place_slice_order", None)
            if method:
                raw = method(
                    security_id=str(sec_id),
                    exchange_segment=exch_seg,
                    transaction_type=getattr(dhanhq, transaction_type.upper(), dhanhq.BUY),
                    quantity=int(qty),
                    order_type=dhanhq.LIMIT,
                    product_type=dhan_ptype,
                    price=float(limit_price),
                    target_price=float(target_price),
                    stop_loss_price=float(stop_loss_price),
                    trailing_jump=float(trailing_jump),
                )
                oid = self._extract_order_id(raw)
                if oid:
                    logger.warning(f"LIVE SUPER ORDER placed | {symbol} | Product={p_type} | ID={oid}")
                    return oid
            # Fallback to standard place_order with soft SL (avoid recursion into super)
            logger.info("Dhan SDK Super Order API fallback to place_order + SL")
            entry_price = round_buy_limit(limit_price * 1.001)
            raw = self.dhan.place_order(
                security_id=str(sec_id),
                exchange_segment=exch_seg,
                transaction_type=getattr(dhanhq, transaction_type.upper(), dhanhq.BUY),
                quantity=int(qty),
                order_type=dhanhq.LIMIT,
                product_type=dhan_ptype,
                price=float(entry_price),
                trigger_price=0,
                validity=dhanhq.DAY,
                tag=f"BOT-BUY-{symbol}"[:20],
            )
            oid = self._extract_order_id(raw)
            if oid and stop_loss_price:
                self.place_stoploss_order(symbol, qty, stop_loss_price, product_type=p_type)
            return oid
        except Exception as e:
            logger.error(f"Failed to place Super Order for {symbol}: {e}", exc_info=True)
            return None

    def place_forever_order(
        self,
        symbol: str,
        qty: int,
        trigger_price: float,
        price: float = 0.0,
        transaction_type: str = "BUY",
        order_flag: str = "SINGLE",
        target_price: float | None = None,
        stop_loss_price: float | None = None,
        product_type: str | None = None,
    ) -> str | None:
        """
        Dhan Forever Order (GTT / OCO) — sits on broker servers across multiple days.
        Used for CNC swing exits where DAY/Super orders are insufficient overnight.
        """
        p_type = (product_type or config.INDIA_PRODUCT_TYPE or "CNC").strip().upper()
        dhan_ptype = _dhan_product_type(p_type)
        if self.paper is not None:
            logger.info(
                f"[PAPER] Forever GTT armed for {symbol} @ Trigger={trigger_price:.2f} product={p_type}"
            )
            return f"PAPER-GTT-{symbol}"

        if not self._assert_live_allowed(f"FOREVER GTT {symbol}"):
            return None

        self.ensure_session()
        if not self.dhan:
            return None

        sec_id, exch_seg = _resolved_security_id(symbol)
        if not sec_id:
            return None

        trigger_price = round_to_nse_tick(float(trigger_price), mode="nearest")
        if transaction_type.upper() == "SELL":
            price = round_sell_limit(float(price or trigger_price))
        else:
            price = round_buy_limit(float(price or trigger_price))
        if target_price is not None:
            target_price = round_to_nse_tick(float(target_price), mode="nearest")
        if stop_loss_price is not None:
            stop_loss_price = round_to_nse_tick(float(stop_loss_price), mode="floor")

        try:
            method = getattr(self.dhan, "place_forever_order", None)
            if method:
                kwargs = dict(
                    security_id=str(sec_id),
                    exchange_segment=exch_seg,
                    transaction_type=getattr(dhanhq, transaction_type.upper(), dhanhq.BUY),
                    quantity=int(qty),
                    order_type=dhanhq.LIMIT,
                    product_type=dhan_ptype,
                    price=float(price),
                    trigger_price=float(trigger_price),
                    order_flag=order_flag,
                )
                try:
                    raw = method(**kwargs)
                except TypeError:
                    # Older SDK signatures
                    raw = method(
                        security_id=str(sec_id),
                        exchange_segment=exch_seg,
                        transaction_type=getattr(dhanhq, transaction_type.upper(), dhanhq.BUY),
                        quantity=int(qty),
                        order_type=dhanhq.LIMIT,
                        product_type=dhan_ptype,
                        price=float(price or trigger_price),
                        trigger_price=float(trigger_price),
                    )
                oid = self._extract_order_id(raw)
                if oid:
                    logger.warning(
                        f"LIVE FOREVER GTT | {symbol} | Product={p_type} | Trigger={trigger_price:.2f} | ID={oid}"
                    )
                return oid
            logger.info("Forever order API unavailable — soft trigger armed")
            return f"SOFT-GTT-{symbol}"
        except Exception as e:
            logger.error(f"Forever order error for {symbol}: {e}")
            return None

    def get_margin_required(self, symbol: str, qty: int, price: float, product_type: str = "CNC") -> float:
        """Calculate required margin using Dhan Margin Calculator or fallback estimation."""
        if price <= 0 or qty <= 0:
            return 0.0

        self.ensure_session()
        sec_id, exch_seg = _resolved_security_id(symbol)
        dhan_ptype = _dhan_product_type(product_type)
        if self.dhan and sec_id:
            try:
                method = getattr(self.dhan, "margin_calculator", None)
                if method:
                    resp = method(
                        security_id=str(sec_id),
                        exchange_segment=exch_seg,
                        transaction_type=dhanhq.BUY,
                        quantity=int(qty),
                        product_type=dhan_ptype,
                        price=float(price),
                    )
                    if self._ok(resp):
                        data = self._data(resp)
                        if isinstance(data, dict):
                            margin = data.get("totalMargin") or data.get("margin_required") or data.get("leverage")
                            if margin and float(margin) > 0:
                                return float(margin)
            except Exception as e:
                logger.debug(f"Dhan Margin Calculator API fallback: {e}")

        # Fallback estimation based on product type
        p_upper = (product_type or "CNC").upper()
        if p_upper in ("INTRADAY", "INTRA", "MIS"):
            return round((price * qty) / 5.0, 2)  # ~5x intraday leverage
        elif p_upper == "MTF":
            return round((price * qty) / 4.0, 2)  # ~4x MTF leverage
        return round(price * qty, 2)  # 1x CNC full value

    def square_off_intraday_positions(self) -> list[str]:
        """
        Auto square-off for INTRADAY product before broker cutoff (~15:15 IST).
        Live MIS is also squared off by the broker; this covers paper + soft closes.
        """
        if (config.INDIA_PRODUCT_TYPE or "CNC").upper() not in ("INTRADAY", "INTRA", "MIS"):
            return []
        closed = []
        positions = self.get_open_positions()
        for symbol in list(positions.keys()):
            fill = self.close_position(symbol)
            if fill is not None:
                closed.append(symbol)
                try:
                    import trade_journal

                    trade_journal.record_exit(
                        "INDIA", symbol, float(fill), reason="squareoff"
                    )
                except Exception as je:
                    logger.debug(f"Journal squareoff skip: {je}")
                logger.warning(f"[INDIA INTRADAY] Auto square-off {symbol} @ {fill:.2f}")
        return closed

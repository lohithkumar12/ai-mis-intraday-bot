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

import inspect
import json
import logging
import os
import queue
import random
import threading
import time
from datetime import datetime, timedelta, timezone
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
_BRACKET_PENDING_STATUSES = frozenset(
    {
        "PENDING",
        "TRANSIT",
        "OPEN",
        "TRIGGER_PENDING",
        "TRIGGER PENDING",
        "PART_TRADED",
        "AFTER_MARKET_ORDER_REQ",
    }
)
BRACKET_CANCEL_TIMEOUT_SEC = 1.5
BRACKET_CANCEL_POLL_SEC = 0.1


def _dhan_invoke(method, *, require_any: tuple[str, ...] = (), **kwargs):
    """Call a Dhan SDK method with only the kwargs its signature accepts."""
    if method is None:
        raise TypeError("dhan method is None")
    try:
        sig = inspect.signature(method)
    except (TypeError, ValueError):
        return method(**kwargs)
    params = sig.parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return method(**kwargs)
    filtered = {k: v for k, v in kwargs.items() if k in params}
    if require_any and not any(k in filtered for k in require_any):
        raise TypeError(f"SDK does not accept any of {require_any}")
    return method(**filtered)


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
# Keep cache short; longer cache can delay SL/TP reactions in fast markets.
QUOTE_CACHE_SEC = 3.0
# After marketfeed 429/401, skip live LTP and use candles for a while.
MARKETFEED_COOLDOWN_SEC = 90.0
# Chunk intraday history requests (API can reject very wide ranges).
INTRADAY_CHUNK_DAYS = 30


# ---------------------------------------------------------------------------
# Shared rate limiter — at most MAX_RPS Dhan API calls/second across all
# code paths.  On DH-904 / HTTP 429, back off exponentially (0.5→1→2→…8s).
# ---------------------------------------------------------------------------
MAX_RPS = 8
_BACKOFF_BASE = 0.5
_BACKOFF_MAX = 8.0


class _DhanRateLimiter:
    """Process-wide token-bucket rate limiter for all Dhan REST calls."""

    def __init__(self, max_rps: int = MAX_RPS):
        self._lock = threading.Lock()
        self._tokens = float(max_rps)
        self._max_tokens = float(max_rps)
        self._last_refill = time.monotonic()
        self._backoff = 0.0
        self._dh904_burst_logged = False

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._max_tokens, self._tokens + elapsed * self._max_tokens)
        self._last_refill = now

    def acquire(self) -> None:
        """Block until a token is available (respects backoff)."""
        while True:
            with self._lock:
                if self._backoff > 0:
                    wait = self._backoff
                    self._backoff = 0.0
                else:
                    wait = 0.0
            if wait > 0:
                time.sleep(wait)
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            time.sleep(1.0 / self._max_tokens)

    def report_rate_limit(self) -> None:
        """Called on DH-904 / 429 — apply exponential backoff."""
        with self._lock:
            if self._backoff <= 0:
                self._backoff = _BACKOFF_BASE
            else:
                self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
            if not self._dh904_burst_logged:
                self._dh904_burst_logged = True
                logger.warning(
                    "[RATE LIMIT] DH-904 / 429 detected — backing off %.1fs",
                    self._backoff,
                )

    def reset_burst_flag(self) -> None:
        with self._lock:
            self._dh904_burst_logged = False


_rate_limiter = _DhanRateLimiter()


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
        self.last_fill_qty = 0
        self.last_fill_price = 0.0
        self.last_order_status = ""
        self.last_sl_order_id = None
        self.last_super_order_id = None
        # symbol -> {status ACTIVE|COOLDOWN, active, sl, tp, qty, entry, order_id, ...}
        self.sl_tp_meta: dict[str, dict] = {}
        self._squareoff_done_today = False
        self._zombie_rescue_time: dict[str, float] = {}
        self._session_reset_day: str | None = None
        self.load_sl_tp_meta()
        self._last_candle_call_time = 0.0
        self._candle_cache: dict[str, tuple[float, pd.DataFrame]] = {}
        self._quote_cache: dict[str, tuple[float, dict]] = {}
        self._marketfeed_cooldown_until = 0.0
        self._quote_warn_at: dict[str, float] = {}
        self._login_lock = threading.Lock()
        self._flatten_q: queue.Queue = queue.Queue()
        self._flatten_lock = threading.Lock()
        self._flatten_queued: set[str] = set()
        self._flatten_thread: threading.Thread | None = None
        self._flatten_risk_mgr = None
        self._exit_health_thread: threading.Thread | None = None
        self._exit_health_stop = threading.Event()

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
        """Push latest access token into the India live WebSocket feed."""
        if config.DHAN_LIVE_WEBSOCKET:
            try:
                feed = get_live_feed_manager()
                feed.update_credentials(
                    self.client_id, self.access_token, reconnect=reconnect
                )
            except Exception as e:
                logger.debug(f"[AUTH] Live feed credential sync note: {e}")


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
        if "429" in msg or "dh-904" in msg or "rate limit" in msg or "too many" in msg:
            _rate_limiter.report_rate_limit()
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
            _rate_limiter.acquire()
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
            side = str(pos.get("side") or "BUY").upper()
            pos["current_price"] = quote_ltp
            pos["market_value"] = qty * quote_ltp
            if buy > 0 and qty > 0:
                if side == "SELL":
                    pos["unrealized_pl"] = (buy - quote_ltp) * qty
                    pos["unrealized_plpc"] = (buy - quote_ltp) / buy
                else:
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
        _rate_limiter.acquire()
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

    def get_historical_bars(
        self,
        symbol: str,
        days: int = 300,
        interval: int | None = None,
    ) -> pd.DataFrame | None:
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

        if interval is not None and int(interval) > 0:
            interval = int(interval)
        else:
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

    @staticmethod
    def _quote_extras_from_node(node: dict | None) -> dict:
        """Pull circuit / depth fields when the marketfeed payload has them."""
        extra: dict = {}
        if not isinstance(node, dict):
            return extra
        ohlc = node.get("ohlc") if isinstance(node.get("ohlc"), dict) else {}
        mapping = {
            "upper_circuit": (
                "upper_circuit_limit",
                "upperCircuitLimit",
                "upper_circuit",
                "upper_circuit_limit_price",
            ),
            "lower_circuit": (
                "lower_circuit_limit",
                "lowerCircuitLimit",
                "lower_circuit",
            ),
            "ask_qty": (
                "sell_quantity",
                "sellQuantity",
                "sell_qty",
                "ask_qty",
                "askQuantity",
            ),
            "best_ask": ("best_ask", "ask", "sell_price"),
        }
        for out_key, keys in mapping.items():
            for k in keys:
                raw = node.get(k)
                if raw is None or raw == "":
                    raw = ohlc.get(k) if ohlc else None
                if raw is None or raw == "":
                    continue
                try:
                    extra[out_key] = float(raw)
                    break
                except (TypeError, ValueError):
                    continue
        depth = node.get("depth") or node.get("marketDepth") or {}
        if isinstance(depth, dict):
            sells = depth.get("sell") or depth.get("asks") or []
            if isinstance(sells, list) and sells:
                top = sells[0] if isinstance(sells[0], dict) else {}
                try:
                    q = float(top.get("quantity") or top.get("qty") or 0)
                    extra["ask_qty"] = q
                except (TypeError, ValueError):
                    pass
                try:
                    px = float(top.get("price") or top.get("ltp") or 0)
                    if px > 0:
                        extra["best_ask"] = px
                except (TypeError, ValueError):
                    pass
        return extra

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

    def get_latest_quote(self, symbol: str, force_fresh: bool = False) -> dict | None:
        # Check Dhan Live Feed WebSocket cache first (Paid Data API)
        ws_feed = get_live_feed_manager()
        ws_quote = ws_feed.get_live_quote(symbol)
        if ws_quote:
            return self._cache_quote(symbol, ws_quote)

        if not force_fresh:
            cached = self._cached_quote(symbol)
            if cached:
                return cached

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

            _rate_limiter.acquire()
            resp = method(securities)
            status_code = None
            if isinstance(resp, dict):
                remarks = resp.get("remarks") or {}
                if isinstance(remarks, dict):
                    status_code = remarks.get("status_code") or remarks.get("error_code")

            if not self._ok(resp):
                raw = str(resp).lower()
                if "429" in raw or status_code in (429, "429", "DH-904"):
                    _rate_limiter.report_rate_limit()
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

            extras = {}
            try:
                data = self._data(resp)
                bucket = data.get(segment) or data.get(str(sec_id)) or data if isinstance(data, dict) else {}
                node = bucket.get(str(sec_id)) or bucket.get(int(sec_id)) or bucket if isinstance(bucket, dict) else {}
                extras = self._quote_extras_from_node(node if isinstance(node, dict) else None)
            except Exception:
                extras = {}
            quote = {
                "ltp": ltp,
                "ask_price": extras.get("best_ask") or ltp,
                "symbol": symbol,
                "exchange": get_exchange(symbol),
                "source": "ticker_data",
            }
            quote.update(extras)
            return self._cache_quote(symbol, quote)
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

    def get_order_transaction_side(self, order_id: str) -> str:
        """Return BUY/SELL for a day order id, else empty string."""
        if self.paper is not None or not order_id:
            return ""
        self.ensure_session()
        if not self.dhan:
            return ""
        try:
            method = getattr(self.dhan, "get_order_by_id", None)
            if not callable(method):
                return ""
            resp = method(str(order_id))
            if self._resp_looks_like_auth_failure(resp):
                if self._force_relogin("get_order_by_id"):
                    resp = method(str(order_id))
            row = self._parse_order_payload(resp)
            side = str(
                row.get("transactionType")
                or row.get("transaction_type")
                or row.get("side")
                or ""
            ).upper()
            return side if side in ("BUY", "SELL") else ""
        except Exception as e:
            logger.debug(f"order side lookup failed for {order_id}: {e}")
            return ""

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
        """Latest today's SELL (incl. SL) fill for symbol. Returns 0 if unknown."""
        return self._latest_side_fill_price(symbol, "SELL")

    def latest_buy_fill_price(self, symbol: str) -> float:
        """Latest today's BUY fill for symbol. Returns 0 if unknown."""
        return self._latest_side_fill_price(symbol, "BUY")

    def _latest_side_fill_price(self, symbol: str, side: str) -> float:
        """
        Latest today's fill for symbol+side from Dhan trade book / order list.

        Prefer trade-book tradedPrice, then order-book averageTradedPrice for
        TRADED/PART_TRADED orders. Returns 0 if unknown.
        """
        if self.paper is not None or not symbol:
            return 0.0
        self.ensure_session()
        if not self.dhan:
            return 0.0

        want_side = str(side or "").upper()
        if want_side not in ("BUY", "SELL"):
            return 0.0
        want = str(symbol).upper().replace("-EQ", "").strip()
        best_px = 0.0
        best_ts = ""

        for row in self._list_trade_book_rows():
            row_side = str(
                row.get("transactionType") or row.get("transaction_type") or ""
            ).upper()
            if row_side != want_side:
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
            row_side = str(
                row.get("transactionType") or row.get("transaction_type") or ""
            ).upper()
            if row_side != want_side:
                continue
            status = str(
                row.get("orderStatus") or row.get("order_status") or row.get("status") or ""
            ).upper()
            if status and status not in _FILL_STATUSES:
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

    def resolve_entry_fill_price(
        self,
        symbol: str,
        order_id: str | None = None,
        *,
        fallback: float = 0.0,
    ) -> float:
        """Prefer broker BUY fill (order id → today's BUY history), then LTP, then fallback."""
        if order_id:
            fill = self.get_order_fill_price(order_id)
            if fill > 0:
                return fill
        try:
            hist = float(self.latest_buy_fill_price(symbol) or 0)
            if hist > 0:
                return hist
        except Exception as e:
            logger.debug(f"{symbol}: buy-history fill lookup failed: {e}")
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
        - TRADED/PART_TRADED/COMPLETE → (True, status)  # fill only
        - Still open after timeout → (False, PENDING) + pending cooldown
          (does NOT count as a successful fill — no journal / day-fire)
        """
        if self.paper is not None:
            self.last_order_status = "paper"
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
                self.last_order_status = last_status
                order_guards.block_buy_after_reject(symbol, last_detail)
                return False, msg
            if last_status in _FILL_STATUSES:
                # PART_TRADED: keep polling up to 10s for the order to reach
                # TRADED so we journal the final fill qty, not an interim slice.
                if last_status == "PART_TRADED":
                    settle_deadline = time.time() + 10.0
                    while time.time() < settle_deadline:
                        time.sleep(1.0)
                        st, _ = self.get_order_status(order_id)
                        if st == "TRADED" or st == "COMPLETE" or st == "COMPLETED":
                            last_status = st
                            break
                        if st in _REJECT_STATUSES:
                            break

                filled_qty = self.get_order_filled_qty(order_id)
                self.last_fill_qty = int(filled_qty) if filled_qty > 0 else int(
                    getattr(self, "last_fill_qty", 0) or 0
                )
                self.last_order_status = last_status
                logger.info(
                    f"{symbol}: order {order_id} confirmed {last_status} "
                    f"filled_qty={self.last_fill_qty}"
                )
                # PART_TRADED with zero filled qty is not a usable fill
                if last_status == "PART_TRADED" and self.last_fill_qty <= 0:
                    order_guards.block_buy_after_pending(symbol, order_id)
                    self._set_sl_tp_status(symbol, "COOLDOWN", order_id=order_id)
                    self.last_error = "PART_TRADED with zero filled qty"
                    return False, "PART_TRADED_ZERO_FILL"
                try:
                    fill_px = float(self.get_order_fill_price(order_id) or 0)
                    if fill_px > 0:
                        self.last_fill_price = fill_px
                except Exception:
                    pass
                if last_status == "PART_TRADED" or (
                    0 < int(self.last_fill_qty or 0) < int(
                        getattr(self, "_confirm_intended_qty", 0) or 0
                    )
                ):
                    try:
                        snap = self.get_order_fill_snapshot(order_id)
                        rem = int(snap.get("remaining_qty") or 0)
                        filled = int(self.last_fill_qty or snap.get("filled_qty") or 0)
                        if rem > 0 or filled > 0:
                            self.cancel_unfilled_remainder(
                                order_id,
                                symbol=symbol,
                                filled_qty=filled,
                                remaining_qty=rem,
                                super_order_id=getattr(self, "last_super_order_id", None),
                            )
                    except Exception as ce:
                        logger.debug(f"{symbol}: remainder cancel on confirm skip: {ce}")
                return True, last_status
            time.sleep(0.75)

        if last_status in _OPEN_STATUSES or last_status in ("", "UNKNOWN"):
            adopted, adopt_status = self._adopt_or_cancel_open_entry(
                order_id, symbol, last_status=last_status or "PENDING"
            )
            if adopted:
                return True, adopt_status
            resolved_status = adopt_status or last_status or "PENDING"
            logger.warning(
                f"{symbol}: order {order_id} still {resolved_status} "
                f"after {timeout:.0f}s — cancelled leftover; NOT treating as fill"
            )
            if resolved_status == "PENDING_CANCELLED":
                # Clean cancel with zero fill — clear the guard immediately
                # so the bot can retry on the next cycle instead of waiting 90s.
                order_guards.clear_buy_block(symbol)
            else:
                order_guards.block_buy_after_pending(symbol, order_id)
            self._set_sl_tp_status(symbol, "COOLDOWN", order_id=order_id)
            self.last_error = f"pending:{resolved_status}"
            self.last_order_status = resolved_status
            return False, resolved_status

        if last_status in _REJECT_STATUSES:
            order_guards.block_buy_after_reject(symbol, last_detail)
            self.last_order_status = last_status
            return False, f"{last_status}: {last_detail}"
        # Unknown terminal — do not treat as fill
        self.last_order_status = last_status
        self.last_error = f"unconfirmed:{last_status}"
        order_guards.block_buy_after_pending(symbol, order_id)
        return False, last_status

    def _adopt_or_cancel_open_entry(
        self,
        order_id: str,
        symbol: str,
        *,
        last_status: str = "PENDING",
    ) -> tuple[bool, str]:
        """
        After confirm timeout: adopt a late fill, else cancel leftover working qty.

        Returns (adopted, status). adopted=True means filled_qty>0 and local
        last_fill_* is set. Remaining qty is cancelled either way.
        """
        snap = {}
        try:
            snap = self.get_order_fill_snapshot(order_id) or {}
        except Exception:
            snap = {}
        filled = int(snap.get("filled_qty") or 0)
        remaining = int(snap.get("remaining_qty") or 0)
        status = str(snap.get("status") or last_status or "PENDING").upper()
        avg = float(snap.get("average_price") or 0)
        super_id = getattr(self, "last_super_order_id", None)

        if filled > 0:
            self.last_fill_qty = filled
            if avg > 0:
                self.last_fill_price = avg
            self.last_order_status = status if status in _FILL_STATUSES else "PART_TRADED"
            self.cancel_unfilled_remainder(
                order_id,
                symbol=symbol,
                filled_qty=filled,
                remaining_qty=remaining,
                super_order_id=super_id,
            )
            logger.warning(
                f"{symbol}: timeout adopt fill qty={filled} remaining={remaining} "
                f"status={self.last_order_status}"
            )
            return True, self.last_order_status

        # Zero fill — cancel so a later glitch fill cannot become a naked zombie.
        self.cancel_unfilled_remainder(
            order_id,
            symbol=symbol,
            filled_qty=0,
            remaining_qty=max(remaining, 1),
            super_order_id=super_id,
            force=True,
        )
        try:
            snap2 = self.get_order_fill_snapshot(order_id) or {}
        except Exception:
            snap2 = {}
        filled2 = int(snap2.get("filled_qty") or 0)
        if filled2 > 0:
            avg2 = float(snap2.get("average_price") or 0)
            self.last_fill_qty = filled2
            if avg2 > 0:
                self.last_fill_price = avg2
            self.last_order_status = str(
                snap2.get("status") or "PART_TRADED"
            ).upper()
            logger.warning(
                f"{symbol}: fill landed during cancel qty={filled2} — adopting"
            )
            return True, self.last_order_status
        return False, "PENDING_CANCELLED"

    def get_order_filled_qty(self, order_id: str) -> int:
        """Filled/traded quantity from order book; 0 if unknown."""
        if self.paper is not None or not order_id:
            return int(getattr(self, "last_fill_qty", 0) or 0)
        if not getattr(self, "dhan", None):
            return int(getattr(self, "last_fill_qty", 0) or 0)
        try:
            if hasattr(self, "ensure_session"):
                self.ensure_session()
        except Exception:
            pass
        if not getattr(self, "dhan", None):
            return 0
        try:
            method = getattr(self.dhan, "get_order_by_id", None)
            if not callable(method):
                return 0
            resp = method(str(order_id))
            row = self._parse_order_payload(resp)
            for key in (
                "filledQty",
                "filled_qty",
                "tradedQuantity",
                "traded_quantity",
                "quantityTraded",
                "filled_quantity",
            ):
                raw = row.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    qty = int(float(raw))
                except (TypeError, ValueError):
                    continue
                if qty > 0:
                    return qty
            # Some payloads use remaining vs total
            try:
                total = int(float(row.get("quantity") or row.get("qty") or 0))
                remaining = int(
                    float(row.get("remainingQuantity") or row.get("remaining_quantity") or -1)
                )
                if total > 0 and remaining >= 0 and total >= remaining:
                    return max(0, total - remaining)
            except (TypeError, ValueError):
                pass
        except Exception as e:
            logger.debug(f"filled qty lookup failed for {order_id}: {e}")
        return 0

    def get_order_fill_snapshot(self, order_id: str) -> dict:
        """
        One orderbook lookup: filled_quantity, average_price, status, symbol, qty.
        Missing fields are 0 / empty; never invents intended size.
        """
        empty = {
            "order_id": str(order_id or ""),
            "symbol": "",
            "status": "",
            "filled_qty": 0,
            "average_price": 0.0,
            "quantity": 0,
            "remaining_qty": 0,
        }
        if not order_id:
            return empty
        if self.paper is not None:
            empty["status"] = str(getattr(self, "last_order_status", "") or "paper")
            empty["filled_qty"] = int(getattr(self, "last_fill_qty", 0) or 0)
            empty["average_price"] = float(getattr(self, "last_fill_price", 0) or 0)
            return empty
        if not getattr(self, "dhan", None):
            return empty
        try:
            if hasattr(self, "ensure_session"):
                self.ensure_session()
        except Exception:
            pass
        if not getattr(self, "dhan", None):
            return empty
        try:
            method = getattr(self.dhan, "get_order_by_id", None)
            if not callable(method):
                return empty
            resp = method(str(order_id))
            if self._resp_looks_like_auth_failure(resp):
                if self._force_relogin("get_order_by_id"):
                    resp = method(str(order_id))
            row = self._parse_order_payload(resp)
            status = str(
                row.get("orderStatus") or row.get("order_status") or row.get("status") or ""
            ).upper()
            qty = 0
            try:
                qty = int(float(row.get("quantity") or row.get("qty") or 0))
            except (TypeError, ValueError):
                qty = 0
            filled = 0
            for key in (
                "filledQty",
                "filled_qty",
                "tradedQuantity",
                "traded_quantity",
                "quantityTraded",
                "filled_quantity",
            ):
                raw = row.get(key)
                if raw is None or raw == "":
                    continue
                try:
                    filled = int(float(raw))
                except (TypeError, ValueError):
                    continue
                if filled > 0:
                    break
            remaining = 0
            try:
                remaining = int(
                    float(row.get("remainingQuantity") or row.get("remaining_quantity") or 0)
                )
            except (TypeError, ValueError):
                remaining = 0
            if filled <= 0 and qty > 0 and remaining >= 0 and qty >= remaining:
                filled = max(0, qty - remaining)
            if remaining <= 0 and qty > 0 and filled >= 0:
                remaining = max(0, qty - filled)
            return {
                "order_id": str(order_id),
                "symbol": self._symbol_from_order_row(row),
                "status": status,
                "filled_qty": int(filled),
                "average_price": float(self._row_fill_price(row) or 0),
                "quantity": int(qty),
                "remaining_qty": int(remaining),
            }
        except Exception as e:
            logger.debug(f"order fill snapshot failed for {order_id}: {e}")
            return empty

    def _set_sl_tp_status(
        self,
        symbol: str,
        status: str,
        *,
        order_id: str | None = None,
        persist: bool = True,
        **fields,
    ) -> None:
        sym = str(symbol or "").upper()
        if not sym:
            return
        meta = dict((getattr(self, "sl_tp_meta", None) or {}).get(sym) or {})
        status_u = str(status or "").upper()
        meta["status"] = status_u
        meta["active"] = status_u == "ACTIVE"
        if order_id:
            meta["order_id"] = str(order_id)
        for k, v in fields.items():
            if v is not None:
                meta[k] = v
        if meta.get("stop_loss_price") is not None and meta.get("sl") is None:
            meta["sl"] = meta.get("stop_loss_price")
        if meta.get("target_price") is not None and meta.get("tp") is None:
            meta["tp"] = meta.get("target_price")
        if meta.get("sl") is not None and not meta.get("stop_loss_price"):
            meta["stop_loss_price"] = meta["sl"]
        if meta.get("tp") is not None and not meta.get("target_price"):
            meta["target_price"] = meta["tp"]
        if not hasattr(self, "sl_tp_meta") or self.sl_tp_meta is None:
            self.sl_tp_meta = {}
        self.sl_tp_meta[sym] = meta
        if persist:
            self.save_sl_tp_meta()

    def _sl_tp_state_path(self) -> Path:
        journal = Path(str(getattr(config, "TRADE_JOURNAL_PATH", "trade_journal.db")))
        return journal.expanduser().resolve().parent / "sl_tp_meta.json"

    def save_sl_tp_meta(self) -> None:
        """Persist sl_tp_meta so SL/TP survive process restarts."""
        try:
            path = self._sl_tp_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(getattr(self, "sl_tp_meta", None) or {})
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.debug(f"sl_tp_meta persist skip: {e}")

    def load_sl_tp_meta(self) -> None:
        """Restore SL/TP monitors from sl_tp_meta.json at startup."""
        try:
            path = self._sl_tp_state_path()
            if not path.is_file():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            restored: dict[str, dict] = {}
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                rec = dict(v)
                status = str(rec.get("status") or "").upper()
                if rec.get("active") is True or status == "ACTIVE":
                    rec["status"] = "ACTIVE"
                    rec["active"] = True
                else:
                    rec["active"] = bool(rec.get("active"))
                if rec.get("sl") is not None and not rec.get("stop_loss_price"):
                    rec["stop_loss_price"] = rec["sl"]
                if rec.get("tp") is not None and not rec.get("target_price"):
                    rec["target_price"] = rec["tp"]
                restored[str(k).upper()] = rec
            self.sl_tp_meta = restored
            if restored:
                logger.info(f"[SL/TP] restored {len(restored)} monitors from {path.name}")
        except Exception as e:
            logger.debug(f"sl_tp_meta load skip: {e}")

    def register_sl_tp(self, symbol: str, sl: float, tp: float, **fields) -> None:
        """Mark a fill as locally monitored and persist."""
        self._set_sl_tp_status(
            symbol,
            "ACTIVE",
            sl=sl,
            tp=tp,
            stop_loss_price=sl,
            target_price=tp,
            active=True,
            **fields,
        )
        logger.info(f"[SL/TP REGISTERED] {str(symbol).upper()}")

    def reset_session_state(self) -> None:
        self._squareoff_done_today = False
        self._zombie_rescue_time = {}
        logger.info("[SESSION RESET] State cleared")

    def maybe_session_reset(self, now: datetime | None = None) -> None:
        """Clear daily square-off / zombie throttle flags at 09:15 IST."""
        if now is None:
            try:
                from zoneinfo import ZoneInfo

                now = datetime.now(ZoneInfo("Asia/Kolkata"))
            except Exception:
                now = datetime.now(timezone.utc)
        mins = int(now.hour) * 60 + int(now.minute)
        if mins < 9 * 60 + 15:
            return
        day = str(now.date())
        if getattr(self, "_session_reset_day", None) == day:
            return
        self.reset_session_state()
        self._session_reset_day = day

    def _live_fill_qty(self, order_id: str, intended_qty: int, status: str) -> int:
        """Broker filled qty. Never substitute intended size on PART_TRADED."""
        filled = int(getattr(self, "last_fill_qty", 0) or 0)
        if filled <= 0:
            filled = int(self.get_order_filled_qty(order_id) or 0)
        status_u = str(status or getattr(self, "last_order_status", "") or "").upper()
        if filled <= 0 and status_u != "PART_TRADED":
            filled = int(intended_qty or 0)
        return int(filled)

    def reconcile_partial_fill(
        self,
        existing_order_id: str,
        risk_mgr=None,
        strategy=None,
        *,
        intended_qty: int | None = None,
        planned_entry: float | None = None,
        planned_sl: float | None = None,
        planned_tp: float | None = None,
        atr: float | None = None,
        source: str = "",
        reason: str = "signal_buy",
        symbol: str | None = None,
        continuation: bool = False,
    ) -> dict:
        """
        Sync local risk + SL/TP monitor to Dhan's actual PART_TRADED fill.

        Fetches filled_quantity / average_price from the orderbook, resizes
        RiskManager stop/target using the original plan's % distance, marks
        sl_tp_meta ACTIVE (clears buy cooldown), and keeps day_fired True.
        Does not overwrite an existing journal row unless the order fully cancels.
        """
        order_id = str(existing_order_id or "")
        snap = self.get_order_fill_snapshot(order_id)
        status = str(snap.get("status") or getattr(self, "last_order_status", "") or "").upper()
        filled_qty = int(snap.get("filled_qty") or 0)
        avg = float(snap.get("average_price") or 0)
        sym = str(symbol or snap.get("symbol") or "").upper()
        intended = int(intended_qty or snap.get("quantity") or 0)
        cancelled = status in _REJECT_STATUSES

        # Don't mix leftover last_fill_* into a cancelled 0-fill snapshot.
        if not cancelled:
            if filled_qty <= 0:
                filled_qty = int(getattr(self, "last_fill_qty", 0) or 0)
            if avg <= 0:
                avg = float(getattr(self, "last_fill_price", 0) or 0)
                if avg <= 0 and order_id:
                    try:
                        avg = float(self.get_order_fill_price(order_id) or 0)
                    except Exception:
                        avg = 0.0
        if not sym:
            # Fall back to risk meta keyed by order id
            for k, meta in (getattr(risk_mgr, "_trade_meta", None) or {}).items():
                if str(meta.get("order_id") or "") == order_id:
                    sym = str(k).upper()
                    break

        result = {
            "ok": False,
            "symbol": sym,
            "order_id": order_id,
            "order_status": status,
            "filled_qty": filled_qty,
            "average_price": avg,
            "stop_loss_price": 0.0,
            "target_price": 0.0,
            "monitor": "COOLDOWN",
            "journal": "skipped",
            "reason": "",
        }

        if cancelled and filled_qty <= 0:
            if sym:
                self._set_sl_tp_status(sym, "COOLDOWN", order_id=order_id)
                if risk_mgr is not None:
                    try:
                        risk_mgr.clear_trade(sym)
                    except Exception:
                        pass
                try:
                    import trade_journal

                    if trade_journal.void_open_entry(
                        "INDIA", sym, reason="order_cancelled"
                    ):
                        result["journal"] = "voided"
                except Exception as je:
                    logger.debug(f"{sym}: journal void skip: {je}")
            result["reason"] = f"order {status or 'CANCELLED'} with 0 fill"
            result["ok"] = False
            logger.warning(
                f"{sym or order_id}: reconcile_partial_fill — {result['reason']}"
            )
            return result

        if filled_qty <= 0 or avg <= 0 or not sym:
            if sym:
                self._set_sl_tp_status(sym, "COOLDOWN", order_id=order_id)
                order_guards.block_buy_after_pending(sym, order_id)
            result["reason"] = (
                f"unusable partial fill qty={filled_qty} avg={avg} symbol={sym!r}"
            )
            logger.error(f"reconcile_partial_fill({order_id}): {result['reason']}")
            return result

        # Lock local state to the filled size — never the intended remainder.
        self.last_fill_qty = filled_qty
        self.last_fill_price = avg
        self.last_order_status = status or "PART_TRADED"

        # Drop leftover working qty so a later fill cannot exceed local risk.
        try:
            rem = int(snap.get("remaining_qty") or 0)
            if rem <= 0 and intended > filled_qty:
                rem = int(intended - filled_qty)
            if rem > 0 or (0 < filled_qty < intended):
                self.cancel_unfilled_remainder(
                    order_id,
                    symbol=sym,
                    filled_qty=filled_qty,
                    remaining_qty=rem,
                    super_order_id=getattr(self, "last_super_order_id", None),
                )
        except Exception as ce:
            logger.warning(f"{sym}: remainder cancel after PART_TRADED skipped: {ce}")

        sl = tp = 0.0
        if risk_mgr is not None:
            try:
                meta = risk_mgr.apply_partial_fill(
                    sym,
                    filled_qty,
                    avg,
                    planned_entry=planned_entry,
                    planned_sl=planned_sl,
                    planned_tp=planned_tp,
                    atr=atr,
                    order_id=order_id,
                    source=source,
                    strategy=getattr(strategy, "name", "") if strategy is not None else None,
                    sl_order_id=getattr(self, "last_sl_order_id", None),
                    super_order_id=getattr(self, "last_super_order_id", None),
                )
                sl = float(meta.get("stop") or 0)
                tp = float(meta.get("take_profit") or 0)
            except Exception as e:
                logger.error(f"{sym}: RiskManager partial-fill update failed: {e}")
                result["reason"] = str(e)
                return result
        else:
            entry_ref = float(planned_entry or avg)
            sl_ref = float(planned_sl or 0)
            tp_ref = float(planned_tp or 0)
            sl_pct = (
                (entry_ref - sl_ref) / entry_ref
                if entry_ref > 0 and sl_ref > 0 and sl_ref < entry_ref
                else float(getattr(config, "STOP_LOSS_PCT", 0.006) or 0.006)
            )
            tp_pct = (
                (tp_ref - entry_ref) / entry_ref
                if entry_ref > 0 and tp_ref > entry_ref
                else float(getattr(config, "TAKE_PROFIT_PCT", 0.009) or 0.009)
            )
            sl = round_to_nse_tick(avg * (1.0 - sl_pct), mode="floor")
            tp = round_to_nse_tick(avg * (1.0 + tp_pct), mode="nearest")

        self.register_sl_tp(
            sym,
            sl,
            tp,
            order_id=order_id,
            qty=filled_qty,
            intended_qty=intended,
            entry=avg,
        )
        order_guards.clear_buy_block(sym)

        if strategy is not None and hasattr(strategy, "mark_day_fired"):
            try:
                strategy.mark_day_fired(sym, "BUY", continuation=continuation)
            except Exception as me:
                logger.debug(f"{sym}: mark_day_fired skipped: {me}")

        # First journal write only. Existing rows are left untouched unless cancelled above.
        try:
            import trade_journal

            if trade_journal.has_entry_today("INDIA", sym):
                result["journal"] = "kept"
            else:
                trade_journal.record_entry(
                    "INDIA",
                    sym,
                    filled_qty,
                    avg,
                    stop_price=sl,
                    take_profit=tp,
                    reason=reason or "partial_fill",
                    strategy=getattr(strategy, "name", "") if strategy is not None else "",
                    meta={
                        "atr": atr,
                        "order_id": order_id,
                        "source": source,
                        "entry_reason": reason,
                        "intended_qty": intended,
                        "fill_status": status or "PART_TRADED",
                        "partial_fill": True,
                    },
                )
                result["journal"] = "recorded"
        except Exception as je:
            logger.debug(f"{sym}: journal skip on partial fill: {je}")

        # Resize resting SL to filled qty when we have a live SL order (not Super).
        sl_oid = getattr(self, "last_sl_order_id", None)
        if sl_oid and sl > 0 and self.paper is None:
            try:
                self.sync_broker_stop(
                    sym,
                    sl,
                    qty=filled_qty,
                    sl_order_id=str(sl_oid),
                    prev_stop=None,
                )
            except Exception as se:
                logger.debug(f"{sym}: SL resize after partial fill skipped: {se}")

        result.update(
            {
                "ok": True,
                "symbol": sym,
                "filled_qty": filled_qty,
                "average_price": avg,
                "stop_loss_price": sl,
                "target_price": tp,
                "monitor": "ACTIVE",
                "reason": "partial_fill_reconciled",
            }
        )
        logger.warning(
            f"{sym}: reconcile_partial_fill ACTIVE qty={filled_qty}/{intended or '?'} "
            f"avg={avg:.2f} SL={sl:.2f} TP={tp:.2f} journal={result['journal']}"
        )
        return result

    # -----------------------------------------------------------------------
    # Positions
    # -----------------------------------------------------------------------
    def get_open_positions(self) -> dict | None:
        if self.paper is not None:
            marks = self._live_marks_for_positions(self.paper.positions.keys())
            pos_dict = self.paper.get_open_positions(marks)
            if pos_dict:
                logger.info(f"India PAPER positions: {list(pos_dict.keys())}")
            return pos_dict

        self.ensure_session()
        if not self.dhan:
            self.last_error = "Dhan positions fetch unavailable: broker session not ready"
            logger.error(self.last_error)
            return None

        pos_dict: dict = {}
        token_to_symbol = {
            info["token"]: sym for sym, info in INDIA_INSTRUMENTS.items()
        }
        mis_mode = _trading_product_is_mis()

        try:
            _rate_limiter.acquire()
            resp = self.dhan.get_positions()
            if not self._ok(resp):
                self.last_error = f"Dhan positions API failed: {resp}"
                logger.error(self.last_error)
                return None
            data = self._data(resp)
            if data is None:
                self.last_error = "Dhan positions API returned empty payload"
                logger.error(self.last_error)
                return None
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
                side = "SELL" if net_qty < 0 else "BUY"
                avg_entry = float(
                    pos.get("avgCostPrice")
                    or pos.get("averagePrice")
                    or (pos.get("sellAvg") if side == "SELL" else pos.get("buyAvg"))
                    or pos.get("avgSellPrice")
                    or pos.get("averageSellPrice")
                    or pos.get("buyAvg")
                    or 0
                )
                ltp = float(pos.get("ltp") or pos.get("lastTradedPrice") or 0)
                qty_abs = abs(net_qty)
                # Normalize MTM by side so dashboard and logs never depend on
                # broker payload sign conventions for short positions.
                if side == "SELL":
                    pnl = (avg_entry - ltp) * qty_abs if avg_entry > 0 and ltp > 0 else 0.0
                else:
                    pnl = (ltp - avg_entry) * qty_abs if avg_entry > 0 and ltp > 0 else 0.0
                if avg_entry > 0 and ltp > 0:
                    pnl_pct = ((avg_entry - ltp) / avg_entry) if side == "SELL" else ((ltp - avg_entry) / avg_entry)
                else:
                    pnl_pct = 0.0
                pos_dict[symbol] = {
                    "qty": qty_abs,
                    "avg_entry_price": avg_entry,
                    "current_price": ltp,
                    "market_value": qty_abs * ltp,
                    "unrealized_pl": pnl,
                    "unrealized_plpc": pnl_pct,
                    "trading_symbol": trading_symbol,
                    "token": sec_id,
                    "source": "position",
                    "product": product or ("INTRADAY" if mis_mode else "CNC"),
                    "side": side,
                }
        except Exception as e:
            self.last_error = f"Dhan positions error: {e}"
            logger.error(f"Dhan positions error: {e}", exc_info=True)
            return None

        # Cache the full raw positions payload for realised P&L lookups on closed rows.
        self._last_raw_positions = data or []

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

    def get_closed_position_pnl(self, symbol: str) -> float | None:
        """Return Dhan's realised P&L for a closed position (netQty=0), or None."""
        token_to_symbol = {
            info["token"]: sym for sym, info in INDIA_INSTRUMENTS.items()
        }
        want = str(symbol).upper()
        for pos in getattr(self, "_last_raw_positions", None) or []:
            if not isinstance(pos, dict):
                continue
            net_qty = int(float(pos.get("netQty") or pos.get("net_qty") or pos.get("quantity") or 0))
            if net_qty != 0:
                continue
            sec_id = str(pos.get("securityId") or pos.get("security_id") or "")
            tsym = str(
                pos.get("tradingSymbol") or pos.get("trading_symbol") or pos.get("symbol") or ""
            ).replace("-EQ", "")
            resolved = token_to_symbol.get(sec_id) or tsym
            if resolved.upper() != want:
                continue
            rpnl = pos.get("realizedProfit") or pos.get("realisedProfit") or pos.get("dayPnl") or pos.get("pnl")
            if rpnl is not None:
                try:
                    return float(rpnl)
                except (TypeError, ValueError):
                    pass
        return None

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
        available_cash: float | None = None,
        margin_used: float | None = None,
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
        if margin_req <= 0:
            margin_req = (float(qty) * float(limit_price)) / 5.0 if p_type in (
                "INTRADAY",
                "INTRA",
                "MIS",
            ) else float(qty) * float(limit_price)
        if available_cash is not None:
            used = float(margin_used or 0)
            free = float(available_cash) - used
            if margin_req > free:
                logger.info(
                    f"[MARGIN REJECTED] {symbol}: needs {margin_req}, free {free}"
                )
                self.last_error = f"margin_rejected needs={margin_req} free={free}"
                return None

        if self.paper is not None:
            quote = self.get_latest_quote(symbol)
            fill = float(quote["ltp"]) if quote else float(limit_price)
            if self.paper.cash < margin_req:
                logger.warning(f"[PAPER BUY] Blocked {symbol}: Margin required (Rs{margin_req:,.2f}) > Available cash (Rs{self.paper.cash:,.2f})")
                return None
            oid = self.paper.buy(
                symbol,
                qty,
                fill,
                stop_loss=stop_loss_price,
                take_profit=take_profit_price,
                atr=atr,
            )
            self.last_fill_price = float(fill) if fill and float(fill) > 0 else float(limit_price)
            self.last_fill_qty = int(qty)
            self.last_order_status = "paper"
            self.last_sl_order_id = None
            self.last_super_order_id = None
            if stop_loss_price and take_profit_price:
                self.register_sl_tp(
                    symbol,
                    float(stop_loss_price),
                    float(take_profit_price),
                    qty=int(qty),
                    entry=float(fill),
                )
            return oid

        self.ensure_session()
        if not self.dhan:
            return None
        sec_id, exch_seg = _resolved_security_id(symbol)
        if not sec_id:
            logger.error(f"Cannot place order — no security_id for {symbol}")
            return None

        self.last_fill_qty = 0
        self.last_sl_order_id = None
        self.last_super_order_id = None

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
                filled_qty = self._live_fill_qty(oid, qty, status)
                if filled_qty <= 0:
                    logger.error(f"SUPER BUY {symbol}: no filled qty after {status}")
                    return None
                self.last_fill_qty = filled_qty
                self.last_super_order_id = str(oid)
                # Swing CNC: also arm Forever GTT for multi-day SL backup
                if p_type == "CNC" and stop_loss_price:
                    self.place_forever_order(
                        symbol,
                        filled_qty,
                        trigger_price=stop_loss_price,
                        price=round_sell_limit(stop_loss_price * 0.995),
                        transaction_type="SELL",
                        order_flag="SINGLE",
                        product_type=p_type,
                    )
                fill = self.resolve_entry_fill_price(
                    symbol, oid, fallback=float(limit_price)
                )
                self.last_fill_price = fill
                if stop_loss_price and take_profit_price:
                    self.register_sl_tp(
                        symbol,
                        float(stop_loss_price),
                        float(take_profit_price),
                        order_id=str(oid),
                        qty=filled_qty,
                        entry=fill,
                    )
                logger.warning(
                    f"LIVE SUPER BUY (Dhan) | {symbol} | Qty={filled_qty}/{qty} | "
                    f"Order ID={oid} | Status={status} | Fill={fill:.2f}"
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

            filled_qty = self._live_fill_qty(order_id, qty, status)
            if filled_qty <= 0:
                logger.error(f"BUY {symbol}: confirmed {status} but filled qty=0")
                return None
            self.last_fill_qty = filled_qty

            fill = self.resolve_entry_fill_price(
                symbol, order_id, fallback=float(limit_price)
            )
            self.last_fill_price = fill
            if stop_loss_price and take_profit_price:
                self.register_sl_tp(
                    symbol,
                    float(stop_loss_price),
                    float(take_profit_price),
                    order_id=str(order_id),
                    qty=filled_qty,
                    entry=fill,
                )
            logger.warning(
                f"LIVE BUY ORDER (Dhan) | {symbol} | Product={p_type} ({dhan_ptype}) | "
                f"Qty={filled_qty}/{qty} | "
                f"Limit={entry_price:.2f} | Fill={fill:.2f} | Order ID={order_id} | Status={status}"
            )
            if place_stoploss and stop_loss_price:
                if p_type == "CNC":
                    # Multi-day swing: Forever GTT for SL (DAY SL orders expire)
                    self.place_forever_order(
                        symbol,
                        filled_qty,
                        trigger_price=stop_loss_price,
                        price=round_sell_limit(stop_loss_price * 0.995),
                        transaction_type="SELL",
                        product_type=p_type,
                    )
                else:
                    sl_oid = self.place_stoploss_order(
                        symbol, filled_qty, stop_loss_price, product_type=p_type
                    )
                    self.last_sl_order_id = sl_oid
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
                self.last_error = f"Sell rejected: {raw}"
                logger.error(f"SELL rejected for {symbol}: {raw}")
                return None
            logger.warning(
                f"LIVE SELL ORDER (Dhan) | {symbol} | Product={p_type} ({dhan_ptype}) | "
                f"Qty={qty} | Order ID={order_id}"
            )
            return order_id
        except Exception as e:
            self.last_error = f"Sell exception: {e}"
            logger.error(f"Failed to place SELL for {symbol}: {e}", exc_info=True)
            return None

    def _order_matches_symbol(self, order: dict, symbol: str) -> bool:
        if not isinstance(order, dict):
            return False
        sym = str(symbol or "").upper()
        sec_id, _ = _resolved_security_id(sym)
        oid_sec = str(order.get("securityId") or order.get("security_id") or "")
        if sec_id and oid_sec and str(sec_id) == oid_sec:
            return True
        tsym = str(
            order.get("tradingSymbol")
            or order.get("trading_symbol")
            or order.get("symbol")
            or ""
        ).upper().replace("-EQ", "")
        return tsym == sym or tsym.startswith(sym + "-")

    def _pending_bracket_ids(self, symbol: str) -> list[str]:
        """Order ids still working for this symbol (Super / SL / target / entry)."""
        ids: list[str] = []
        seen: set[str] = set()

        def _add(oid: str | None) -> None:
            s = str(oid or "").strip()
            if s and s not in seen:
                seen.add(s)
                ids.append(s)

        row = (getattr(self, "sl_tp_meta", None) or {}).get(str(symbol).upper()) or {}
        _add(row.get("order_id") or row.get("super_order_id"))
        _add(row.get("sl_order_id"))
        _add(getattr(self, "last_super_order_id", None))
        _add(getattr(self, "last_sl_order_id", None))

        if not getattr(self, "dhan", None):
            return ids
        try:
            resp = self.dhan.get_order_list()
            data = self._data(resp) if self._ok(resp) else None
            for order in data or []:
                if not self._order_matches_symbol(order, symbol):
                    continue
                status = str(
                    order.get("orderStatus") or order.get("status") or ""
                ).upper()
                if status not in _BRACKET_PENDING_STATUSES:
                    continue
                _add(order.get("orderId") or order.get("order_id"))
        except Exception as e:
            logger.warning(f"{symbol}: pending-bracket order-book scan failed: {e}")
        try:
            super_list = getattr(self.dhan, "get_super_order_list", None) or getattr(
                self.dhan, "getSuperOrderList", None
            )
            if callable(super_list):
                resp = super_list()
                data = self._data(resp) if isinstance(resp, dict) else resp
                for order in data or []:
                    if not isinstance(order, dict):
                        continue
                    if not self._order_matches_symbol(order, symbol):
                        continue
                    status = str(
                        order.get("orderStatus") or order.get("status") or ""
                    ).upper()
                    if status and status not in _BRACKET_PENDING_STATUSES:
                        continue
                    _add(
                        order.get("orderId")
                        or order.get("order_id")
                        or order.get("superOrderId")
                    )
        except Exception as e:
            logger.debug(f"{symbol}: super-order list scan skip: {e}")
        return ids

    def _cancel_order_id(self, oid: str, *, prefer_super: bool = False) -> None:
        if not oid or not getattr(self, "dhan", None):
            return
        methods: list = []
        if prefer_super:
            methods.extend(
                [
                    getattr(self.dhan, "cancel_super_order", None),
                    getattr(self.dhan, "cancelSuperOrder", None),
                ]
            )
        methods.append(getattr(self.dhan, "cancel_order", None))
        last_err = None
        for fn in methods:
            if not callable(fn):
                continue
            try:
                fn(str(oid))
                logger.warning(f"[CANCEL-VERIFY] cancelled working order {oid}")
                return
            except Exception as e:
                last_err = e
        if last_err:
            logger.warning(f"[CANCEL-VERIFY] cancel {oid} failed: {last_err}")

    def cancel_unfilled_remainder(
        self,
        order_id: str,
        *,
        symbol: str = "",
        filled_qty: int = 0,
        remaining_qty: int = 0,
        super_order_id: str | None = None,
        force: bool = False,
    ) -> bool:
        """
        Cancel leftover working qty after PART_TRADED / confirm timeout.

        Regular orders: cancel_order drops unfilled remainder, keeps fills.
        Super Orders: cancel ENTRY_LEG only so SL/TP stay armed on filled qty.
        Never cancels TARGET/STOP legs when filled_qty > 0.
        """
        if self.paper is not None:
            return True
        filled = int(filled_qty or 0)
        remaining = int(remaining_qty or 0)
        if remaining <= 0 and filled > 0 and not force:
            return True
        if remaining <= 0 and not force:
            return True
        oid = str(order_id or super_order_id or "").strip()
        if not oid:
            return False
        if not getattr(self, "dhan", None):
            logger.info(
                f"{symbol or oid}: remainder cancel skipped — no live Dhan client"
            )
            return False

        super_id = str(super_order_id or getattr(self, "last_super_order_id", "") or "").strip()
        logger.warning(
            f"{symbol or oid}: cancelling unfilled remainder "
            f"filled={filled} remaining={remaining} order={oid} super={super_id or '-'}"
        )
        try:
            if super_id and hasattr(self.dhan, "cancel_super_order"):
                try:
                    resp = self.dhan.cancel_super_order(str(super_id), "ENTRY_LEG")
                    logger.warning(
                        f"{symbol or oid}: Super ENTRY_LEG cancel resp={resp}"
                    )
                    return True
                except Exception as se:
                    logger.warning(
                        f"{symbol or oid}: Super ENTRY_LEG cancel failed: {se}"
                    )
            self._cancel_order_id(oid, prefer_super=bool(super_id))
            return True
        except Exception as e:
            logger.warning(f"{symbol or oid}: remainder cancel error: {e}")
            return False

    def _cancel_and_verify_brackets(self, symbol: str) -> bool:
        """
        Cancel Super Order / SL / target legs, then poll the order book every
        100ms for up to 1.5s until nothing pending remains.
        """
        if self.paper is not None:
            return True
        if not getattr(self, "dhan", None):
            logger.info(f"{symbol}: cancel-verify skipped — no live Dhan client")
            return True

        pending = self._pending_bracket_ids(symbol)
        logger.info(
            f"[CANCEL-VERIFY] {symbol}: cancelling {len(pending)} working bracket/order id(s) "
            f"{pending or '[]'}"
        )
        for oid in pending:
            self._cancel_order_id(oid, prefer_super=True)

        deadline = time.time() + BRACKET_CANCEL_TIMEOUT_SEC
        while time.time() < deadline:
            leftover = self._pending_bracket_ids(symbol)
            if not leftover:
                logger.info(
                    f"[CANCEL-VERIFY] {symbol}: broker confirmed brackets terminated"
                )
                return True
            time.sleep(BRACKET_CANCEL_POLL_SEC)

        leftover = self._pending_bracket_ids(symbol)
        if leftover:
            logger.warning(
                f"[CANCEL-VERIFY] {symbol}: timeout {BRACKET_CANCEL_TIMEOUT_SEC}s — "
                f"still pending {leftover}; will re-check netQty before SELL"
            )
            return False
        logger.info(f"[CANCEL-VERIFY] {symbol}: brackets clear after poll")
        return True

    def close_position(self, symbol: str) -> float | None:
        """
        Flatten symbol. Returns exit fill price on success, else None.
        Cancel-and-verify Super Order legs first; never SELL if fresh netQty <= 0.
        """
        if not order_guards.try_begin_exit(symbol):
            logger.info(f"{symbol}: close skipped — exit already in flight or EXIT_PENDING_STUCK")
            return None
        try:
            self._cancel_and_verify_brackets(symbol)

            # Poll positions until netQty stabilises (two consecutive reads
            # agree) so a partial fill still in flight doesn't cause a qty
            # mismatch on the flatten SELL.  Max 3 extra polls, 0.5s apart.
            prev_qty = None
            positions = None
            for _poll in range(4):
                positions = self.get_open_positions()
                if positions is None:
                    logger.warning(f"{symbol}: skip close — positions snapshot unavailable")
                    return None
                cur_qty = int((positions.get(symbol) or {}).get("qty") or 0)
                if prev_qty is not None and cur_qty == prev_qty:
                    break
                prev_qty = cur_qty
                if _poll < 3:
                    time.sleep(0.5)
            if symbol not in positions:
                logger.warning(
                    f"{symbol}: skip SELL — netQty=0 after cancel-verify "
                    f"(broker already flat; blocking double-sell)"
                )
                return None
            pos = positions[symbol]
            qty = int(pos.get("qty") or 0)
            side = str(pos.get("side") or "BUY").upper()
            signed_qty = -qty if side == "SELL" else qty
            if signed_qty == 0 or qty <= 0:
                logger.warning(
                    f"{symbol}: skip flatten — fresh netQty={signed_qty} after cancel-verify "
                    f"(broker already flat; blocking double-sell)"
                )
                return None
            if signed_qty < 0:
                logger.warning(
                    f"{symbol}: skip SELL — fresh netQty={signed_qty} (already short). "
                    f"Covering with BUY, not another SELL."
                )
            product = str(pos.get("product") or config.INDIA_PRODUCT_TYPE or "INTRADAY")
            fallback = float(
                pos.get("current_price") or pos.get("avg_entry_price") or 0
            )
            logger.info(
                f"[CANCEL-VERIFY] {symbol}: netQty={signed_qty} after bracket cancel — "
                f"placing flatten {'BUY-cover' if side == 'SELL' else 'SELL'}"
            )
            if side == "SELL":
                # Short position -> BUY to cover.
                order_id = self.place_buy_order(
                    symbol=symbol,
                    qty=qty,
                    limit_price=max(fallback, 0.01),
                    place_stoploss=False,
                    stop_loss_price=0.0,
                    take_profit_price=None,
                    product_type=product,
                )
            else:
                order_id = self.place_sell_order(
                    symbol, qty, order_type="MARKET", product_type=product
                )
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
                self.last_fill_qty = qty
                logger.info(f"Position CLOSED for {symbol} (Qty={qty}) @ {fill:.2f} paper")
                return fill if fill > 0 else None

            ok, status = self.confirm_live_order(order_id, symbol)
            if not ok:
                status_u = str(status or getattr(self, "last_order_status", "") or "").upper()
                if status_u in _OPEN_STATUSES or status_u in ("", "UNKNOWN", "PENDING"):
                    order_guards.mark_exit_pending_stuck(
                        symbol, order_id=str(order_id), status=status_u or "PENDING"
                    )
                    logger.error(
                        f"{symbol}: exit order {order_id} still {status_u or 'PENDING'} — "
                        f"EXIT_PENDING_STUCK; lock held until TRADED/CANCELLED"
                    )
                    return None
                logger.error(
                    f"{symbol}: exit order {order_id} not filled ({status}) — "
                    f"not journaling as closed"
                )
                return None
            filled_qty = int(getattr(self, "last_fill_qty", 0) or 0) or self.get_order_filled_qty(
                order_id
            )
            if filled_qty > 0:
                self.last_fill_qty = filled_qty
            fill = self.resolve_exit_fill_price(symbol, order_id, fallback=fallback)
            self.last_fill_price = fill
            if fill <= 0:
                logger.warning(
                    f"Position CLOSE submitted for {symbol} but fill price unknown"
                )
                return None
            logger.info(
                f"Position CLOSED for {symbol} (Qty={self.last_fill_qty or qty}) @ {fill:.2f}"
            )
            return fill
        finally:
            released = order_guards.end_exit(symbol)
            if not released:
                logger.warning(
                    f"{symbol}: exit lock retained (EXIT_PENDING_STUCK) — "
                    f"5s risk loop will not send a second SELL"
                )

    def sync_broker_stop(
        self,
        symbol: str,
        new_stop: float,
        *,
        qty: int | None = None,
        sl_order_id: str | None = None,
        super_order_id: str | None = None,
        prev_stop: float | None = None,
    ) -> bool:
        """
        Ratchet broker-side stop to new_stop (never downwards).
        Uses modify_super_order STOP_LOSS_LEG when super id known, else modify_order
        on a resting SL order. Returns False if sync not possible / disabled.
        """
        if not bool(getattr(config, "SYNC_BROKER_STOPS", False)):
            return False
        if self.paper is not None:
            return True
        if new_stop is None or float(new_stop) <= 0:
            return False
        new_stop = float(round_to_nse_tick(float(new_stop), mode="floor"))
        if prev_stop is not None and new_stop + 1e-9 < float(prev_stop):
            logger.info(
                f"{symbol}: skip broker SL sync — new {new_stop:.2f} < prev {prev_stop:.2f}"
            )
            return False

        self.ensure_session()
        if not self.dhan:
            return False

        retries = max(1, int(getattr(config, "SYNC_BROKER_STOP_RETRIES", 3) or 3))
        last_err = ""
        for attempt in range(1, retries + 1):
            try:
                if super_order_id and hasattr(self.dhan, "modify_super_order"):
                    resp = _dhan_invoke(
                        self.dhan.modify_super_order,
                        order_id=str(super_order_id),
                        order_type=getattr(dhanhq, "LIMIT", "LIMIT"),
                        leg_name="STOP_LOSS_LEG",
                        stopLossPrice=float(new_stop),
                        stop_loss_price=float(new_stop),
                        trailingJump=0.0,
                        trailing_jump=0.0,
                    )
                    ok = self._ok(resp) if hasattr(self, "_ok") else True
                    if ok:
                        logger.info(
                            f"{symbol}: broker Super SL synced → {new_stop:.2f} "
                            f"(order={super_order_id} attempt={attempt})"
                        )
                        return True
                    last_err = str(resp)
                    logger.warning(
                        f"{symbol}: modify_super_order failed attempt {attempt}/{retries}: {resp}"
                    )
                elif sl_order_id and hasattr(self.dhan, "modify_order"):
                    trigger = float(new_stop)
                    limit_px = float(round_sell_limit(trigger * 0.995))
                    q = int(qty or 0)
                    if q <= 0:
                        logger.warning(
                            f"{symbol}: skip broker SL sync — qty={q} (DH-905 if sent)"
                        )
                        return False
                    # Dhan v2: modifying a resting SL needs ENTRY_LEG, not "".
                    # Empty leg_name was today's DH-905 on every trail sync.
                    sl_type = (
                        getattr(dhanhq, "SL", None)
                        or getattr(dhanhq, "STOP_LOSS", None)
                        or "STOP_LOSS"
                    )
                    resp = _dhan_invoke(
                        self.dhan.modify_order,
                        order_id=str(sl_order_id),
                        order_type=sl_type,
                        leg_name="ENTRY_LEG",
                        quantity=q,
                        price=limit_px,
                        trigger_price=trigger,
                        disclosed_quantity=0,
                        validity=getattr(dhanhq, "DAY", "DAY"),
                    )
                    ok = self._ok(resp) if hasattr(self, "_ok") else True
                    if ok:
                        logger.info(
                            f"{symbol}: broker SL order synced → {new_stop:.2f} "
                            f"(order={sl_order_id} attempt={attempt})"
                        )
                        return True
                    last_err = str(resp)
                    logger.warning(
                        f"{symbol}: modify_order SL failed attempt {attempt}/{retries}: {resp}"
                    )
                else:
                    logger.info(
                        f"{symbol}: SYNC_BROKER_STOPS on but no sl/super order id stored — "
                        f"local trail only (cannot safely invent broker modify target)"
                    )
                    return False
            except Exception as e:
                last_err = str(e)
                logger.warning(
                    f"{symbol}: broker stop sync error attempt {attempt}/{retries}: {e}"
                )
            if attempt < retries:
                time.sleep(0.35 * attempt)
        logger.warning(
            f"{symbol}: broker SL sync exhausted {retries} attempts ({last_err}) — "
            f"software trail still active, will retry next risk tick"
        )
        return False

    def _sl_tp_monitor_active(self, symbol: str) -> bool:
        row = (getattr(self, "sl_tp_meta", None) or {}).get(str(symbol or "").upper()) or {}
        if row.get("active") is True:
            try:
                return float(row.get("stop_loss_price") or row.get("sl") or 0) > 0
            except (TypeError, ValueError):
                return False
        if str(row.get("status") or "").upper() != "ACTIVE":
            return False
        try:
            return float(row.get("stop_loss_price") or row.get("sl") or 0) > 0
        except (TypeError, ValueError):
            return False

    def rescue_zombie_positions(self, risk_mgr=None, strategy=None) -> list[str]:
        """
        Arm local SL/TP for MIS names that are live at Dhan but missing sl_tp_meta.

        Does not flatten. Used after restart / PENDING fills while the bot was idle.
        """
        rescued: list[str] = []
        try:
            positions = self.get_open_positions()
        except Exception as e:
            logger.warning(f"[ZOMBIE] positions fetch failed: {e}")
            return rescued
        if not positions:
            return rescued

        sl_pct = float(getattr(config, "ZOMBIE_SL_PCT", 0) or 0)
        if sl_pct <= 0:
            sl_pct = float(
                getattr(risk_mgr, "stop_loss_pct", None)
                or getattr(config, "STOP_LOSS_PCT", 0.0045)
                or 0.0045
            )
        tp_pct = float(getattr(config, "ZOMBIE_TP_PCT", 0) or 0) or 0.008

        import bot_state

        if not hasattr(self, "_zombie_rescue_time") or self._zombie_rescue_time is None:
            self._zombie_rescue_time = {}

        for symbol, pos in positions.items():
            qty = int(pos.get("qty") or 0)
            if qty <= 0:
                continue
            row = (getattr(self, "sl_tp_meta", None) or {}).get(str(symbol).upper()) or {}
            if row.get("active") is True or self._sl_tp_monitor_active(symbol):
                logger.info(f"[ZOMBIE SKIP] {symbol} already monitored")
                continue
            last = self._zombie_rescue_time.get(str(symbol).upper())
            if last and (time.time() - float(last)) < 300:
                logger.info(f"[ZOMBIE SKIP] {symbol} rescued recently - throttling")
                continue

            ltp = 0.0
            try:
                q = self.get_latest_quote(symbol, force_fresh=True)
                ltp = float((q or {}).get("ltp") or 0)
            except Exception:
                ltp = 0.0
            if ltp <= 0:
                try:
                    ltp = float(pos.get("current_price") or 0)
                except (TypeError, ValueError):
                    ltp = 0.0
            if ltp <= 0:
                logger.warning(
                    f"[ZOMBIE] {symbol}: open qty={qty} but no LTP — cannot arm SL/TP"
                )
                continue

            entry = float(pos.get("avg_entry_price") or 0) or ltp
            prev = {}
            if risk_mgr is not None:
                prev = dict(getattr(risk_mgr, "_trade_meta", {}).get(symbol) or {})
            prev_sl = float(prev.get("stop") or prev.get("initial_stop") or 0)
            prev_tp = float(prev.get("take_profit") or 0)

            ltp_sl = round_to_nse_tick(ltp * (1.0 - sl_pct), mode="floor")
            ltp_tp = round_to_nse_tick(ltp * (1.0 + tp_pct), mode="nearest")
            # Prefer an already-planned stop if present; never leave the name naked.
            sl = prev_sl if prev_sl > 0 else ltp_sl
            tp = prev_tp if prev_tp > entry else ltp_tp
            if sl >= ltp:
                sl = ltp_sl
            if tp <= ltp:
                tp = ltp_tp
            sl = max(sl, 0.01)

            if risk_mgr is not None and hasattr(risk_mgr, "register_trade"):
                try:
                    risk_mgr.register_trade(
                        symbol,
                        float(prev.get("entry") or entry),
                        sl,
                        prev.get("atr"),
                        qty=qty,
                        take_profit=tp,
                        source=prev.get("source") or "zombie_rescue",
                        strategy=prev.get("strategy")
                        or getattr(strategy, "name", "")
                        or "",
                        sl_order_id=prev.get("sl_order_id"),
                        super_order_id=prev.get("super_order_id"),
                        order_id=prev.get("order_id"),
                    )
                except Exception as re:
                    logger.debug(f"[ZOMBIE] {symbol}: risk meta register skip: {re}")

            self._set_sl_tp_status(
                symbol,
                "ACTIVE",
                qty=qty,
                entry=float(prev.get("entry") or entry),
                stop_loss_price=sl,
                target_price=tp,
                sl=sl,
                tp=tp,
                active=True,
                source="zombie_rescue",
            )
            self._zombie_rescue_time[str(symbol).upper()] = time.time()

            # Naked MIS at the broker needs an exchange SL, not only a local monitor.
            sl_oid = str(prev.get("sl_order_id") or "") or None
            super_oid = str(prev.get("super_order_id") or "") or None
            pending = []
            try:
                pending = self._pending_bracket_ids(symbol) if self.paper is None else []
            except Exception:
                pending = []
            if (
                self.paper is None
                and getattr(self, "dhan", None)
                and not sl_oid
                and not super_oid
                and not pending
            ):
                try:
                    sl_oid = self.place_stoploss_order(symbol, qty, sl)
                    if sl_oid:
                        self.last_sl_order_id = str(sl_oid)
                        self._set_sl_tp_status(
                            symbol, "ACTIVE", sl_order_id=str(sl_oid)
                        )
                        if risk_mgr is not None:
                            meta = getattr(risk_mgr, "_trade_meta", {}).get(symbol)
                            if meta is not None:
                                meta["sl_order_id"] = str(sl_oid)
                                risk_mgr.persist_state()
                        logger.warning(
                            f"[ZOMBIE] {symbol}: broker SL armed {sl_oid} @ {sl:.2f}"
                        )
                    else:
                        logger.error(
                            f"[ZOMBIE] {symbol}: broker SL place failed — software monitor only"
                        )
                except Exception as se:
                    logger.error(f"[ZOMBIE] {symbol}: broker SL place error: {se}")

            if strategy is not None and hasattr(strategy, "mark_day_fired"):
                try:
                    strategy.mark_day_fired(symbol, "BUY")
                except Exception as me:
                    logger.debug(f"[ZOMBIE] {symbol}: mark_day_fired skip: {me}")

            try:
                import trade_journal

                if not any(
                    str(r.get("symbol") or "").upper() == str(symbol).upper()
                    for r in trade_journal.list_open_trades("INDIA")
                ):
                    trade_journal.record_entry(
                        "INDIA",
                        symbol,
                        qty,
                        float(prev.get("entry") or entry),
                        stop_price=sl,
                        take_profit=tp,
                        reason="zombie_rescue",
                        strategy=getattr(strategy, "name", "") if strategy else "",
                        meta={
                            "source": "zombie_rescue",
                            "ltp": ltp,
                            "fill_status": "ZOMBIE RESCUED",
                        },
                    )
            except Exception as je:
                logger.debug(f"[ZOMBIE] {symbol}: journal skip: {je}")

            bot_state.note_zombie_rescued(symbol, ltp=ltp, sl=sl, tp=tp, qty=qty)
            rescued.append(symbol)
            logger.warning(f"[ZOMBIE RESCUED] {symbol} SL={sl} TP={tp}")

        return rescued

    def check_sl_tp(self, risk_mgr) -> list[str]:
        closed_symbols = []
        positions = self.get_open_positions()
        if positions is None:
            logger.warning("[INDIA SL/TP] skip — positions snapshot unavailable")
            return closed_symbols

        for symbol, pos in positions.items():
            sl_row = (getattr(self, "sl_tp_meta", None) or {}).get(symbol) or {}
            monitor = str(sl_row.get("status") or "").upper()
            if monitor in ("COOLDOWN", "PENDING"):
                logger.info(
                    f"[INDIA SL/TP] {symbol}: skip — monitor {monitor} "
                    f"(waiting on reconcile_partial_fill / zombie rescue)"
                )
                continue
            entry_price = float(pos["avg_entry_price"])
            current_price = float(pos.get("current_price") or 0)
            # Prefer a fresh quote snapshot for risk decisions to avoid exits on stale marks.
            try:
                q = self.get_latest_quote(symbol, force_fresh=True)
                ltp = float((q or {}).get("ltp") or 0)
                if ltp > 0:
                    current_price = ltp
            except Exception:
                pass
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

            stored_sl = sl_row.get("stop_loss_price")
            try:
                if stored_sl is None or float(stored_sl) <= 0:
                    stored_sl = pos.get("stop_loss")
            except (TypeError, ValueError):
                stored_sl = pos.get("stop_loss")
            stored_tp = sl_row.get("target_price")
            try:
                if stored_tp is None or float(stored_tp) <= 0:
                    stored_tp = pos.get("take_profit")
            except (TypeError, ValueError):
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

            side = str(pos.get("side") or "BUY").upper()
            reason = None
            if side == "SELL":
                # For shorts: stop is above entry, target below entry.
                if stored_sl is None:
                    if atr is not None and atr > 0:
                        sl_price = entry_price + (risk_mgr.atr_stop_mult * atr)
                    else:
                        sl_price = entry_price * (1 + risk_mgr.stop_loss_pct)
                if stored_tp is None:
                    risk = max(sl_price - entry_price, 0.0)
                    if risk > 0:
                        tp_price = entry_price - (risk_mgr.take_profit_r * risk)
                    else:
                        tp_price = entry_price * (1 - risk_mgr.take_profit_pct)

                if current_price >= sl_price:
                    reason = "stop_loss"
                    logger.warning(
                        f"[INDIA SL][SHORT] {symbol} hit stop! "
                        f"Entry={entry_price:.2f} Px={current_price:.2f} SL={sl_price:.2f}"
                    )
                elif current_price <= tp_price:
                    reason = "take_profit"
                    logger.info(
                        f"[INDIA TP][SHORT] {symbol} hit target! "
                        f"Entry={entry_price:.2f} Px={current_price:.2f} TP={tp_price:.2f}"
                    )
            else:
                if symbol not in getattr(risk_mgr, "_trade_meta", {}):
                    risk_mgr.register_trade(symbol, entry_price, sl_price, atr)

                trailed = risk_mgr.update_trailing_stop(symbol, current_price, atr)
                if trailed is not None and trailed > sl_price:
                    prev = sl_price
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
                        if sl_row:
                            sl_row["stop_loss_price"] = sl_price
                            sl_row["status"] = "ACTIVE"
                    else:
                        meta = getattr(risk_mgr, "_trade_meta", {}).get(symbol) or {}
                        sync_enabled = bool(getattr(config, "SYNC_BROKER_STOPS", False))
                        synced = True
                        if sync_enabled:
                            synced = self.sync_broker_stop(
                                symbol,
                                sl_price,
                                qty=int(pos.get("qty") or meta.get("qty") or 0),
                                sl_order_id=meta.get("sl_order_id"),
                                super_order_id=meta.get("super_order_id"),
                                prev_stop=prev,
                            )
                        if synced:
                            if meta:
                                meta["stop"] = sl_price
                                risk_mgr.persist_state()
                            if sl_row:
                                sl_row["stop_loss_price"] = sl_price
                                sl_row["status"] = "ACTIVE"
                        else:
                            logger.warning(
                                f"{symbol}: trail {sl_price:.2f} held in software only "
                                f"(broker SL still {prev:.2f}) — retry next tick"
                            )

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
                if order_guards.is_exit_pending_stuck(symbol):
                    logger.info(f"{symbol}: SL/TP skip — EXIT_PENDING_STUCK")
                    continue
                if order_guards.is_exit_inflight(symbol):
                    logger.info(f"{symbol}: SL/TP skip — exit already in flight")
                    continue
                fill = self.close_position(symbol)
                if fill is not None:
                    closed_symbols.append(symbol)
                    risk_mgr.clear_trade(symbol)
                    try:
                        risk_mgr.release_margin(symbol)
                    except Exception:
                        pass
                    try:
                        import bot_state as _bs

                        _bs.clear_zombie_rescue(symbol)
                    except Exception:
                        pass
                    meta_map = getattr(self, "sl_tp_meta", None)
                    if isinstance(meta_map, dict):
                        meta_map.pop(symbol, None)
                        self.save_sl_tp_meta()
                    try:
                        import trade_journal

                        qty = int(getattr(self, "last_fill_qty", 0) or pos.get("qty") or 0)
                        trade_journal.record_exit(
                            "INDIA",
                            symbol,
                            float(fill),
                            reason=reason,
                            qty=qty if qty > 0 else None,
                        )
                    except Exception as je:
                        logger.debug(f"Journal exit skip: {je}")

        # Detect bracket SL / broker-side exits: tracked positions that
        # vanished from broker positions since last check.
        try:
            import trade_journal as _tj

            tracked = set((getattr(self, "sl_tp_meta", None) or {}).keys())
            tracked |= set((getattr(risk_mgr, "_trade_meta", None) or {}).keys())
            broker_open = set(positions.keys())
            vanished = tracked - broker_open
            for sym in vanished:
                if not sym:
                    continue
                # Only act if journal still has an open row for this symbol
                open_rows = _tj.list_open_trades("INDIA")
                if not any(str(r.get("symbol") or "").upper() == sym for r in open_rows):
                    continue
                exit_px = 0.0
                try:
                    exit_px = float(self.latest_sell_fill_price(sym) or 0)
                except Exception:
                    pass
                if exit_px <= 0:
                    q = self.get_latest_quote(sym)
                    if q and q.get("ltp"):
                        exit_px = float(q["ltp"])
                if exit_px <= 0:
                    logger.warning(
                        f"[BRACKET EXIT] {sym}: vanished from broker but no usable exit price"
                    )
                    continue
                bpnl = None
                try:
                    bpnl = self.get_closed_position_pnl(sym)
                except Exception:
                    pass
                out = _tj.record_exit("INDIA", sym, exit_px, reason="bracket_sl", broker_pnl=bpnl)
                if out:
                    logger.warning(
                        f"[BRACKET EXIT] {sym}: journaled broker-side exit @ {exit_px:.2f}"
                    )
                    closed_symbols.append(sym)
                    risk_mgr.clear_trade(sym)
                    try:
                        risk_mgr.release_margin(sym)
                    except Exception:
                        pass
                    meta_map = getattr(self, "sl_tp_meta", None)
                    if isinstance(meta_map, dict):
                        meta_map.pop(sym, None)
                        self.save_sl_tp_meta()
        except Exception as be:
            logger.debug(f"[BRACKET EXIT] detection skip: {be}")

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
            method = getattr(self.dhan, "place_super_order", None)
            if method:
                tx = getattr(dhanhq, transaction_type.upper(), dhanhq.BUY)
                _rate_limiter.acquire()
                raw = method(
                    security_id=str(sec_id),
                    exchange_segment=exch_seg,
                    transaction_type=tx,
                    quantity=int(qty),
                    order_type=dhanhq.LIMIT,
                    product_type=dhan_ptype,
                    price=float(limit_price),
                    targetPrice=float(target_price),
                    stopLossPrice=float(stop_loss_price),
                    trailingJump=float(trailing_jump),
                )
                oid = self._extract_order_id(raw) if raw is not None else None
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
                            margin = (
                                data.get("totalMargin")
                                or data.get("margin_required")
                                or data.get("total_margin")
                            )
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

    @staticmethod
    def _is_mis_cutoff_reject(err: str) -> bool:
        msg = str(err or "").lower()
        return (
            "intraday orders cannot be placed" in msg
            or "rms:" in msg and "intraday" in msg
            or "after market" in msg
            or "market closed" in msg
        )

    @staticmethod
    def _is_rate_limit_err(err: str) -> bool:
        msg = str(err or "").upper()
        hit = (
            "429" in msg
            or "DH-904" in msg
            or "RATE LIMIT" in msg
            or "TOO MANY REQUESTS" in msg
        )
        if hit:
            _rate_limiter.report_rate_limit()
        return hit

    def square_off_all_positions(self) -> list[str]:
        return self.square_off_intraday_positions()

    def square_off_intraday_positions(self) -> list[str]:
        """
        Auto square-off for INTRADAY product before broker cutoff (~15:15 IST).
        Live MIS is also squared off by the broker; this covers paper + soft closes.
        Verifies broker is flat after each close attempt.
        On RMS/cutoff REJECT: does NOT journal closed; records last_squareoff_failed.
        """
        self.last_squareoff_failed = []
        if (config.INDIA_PRODUCT_TYPE or "CNC").upper() not in ("INTRADAY", "INTRA", "MIS"):
            return []
        if getattr(self, "_squareoff_done_today", False):
            return []

        closed = []
        failed: list[str] = []
        gap = float(getattr(config, "FLATTEN_API_GAP_SEC", 1.5) or 0)
        backoff = float(getattr(config, "FLATTEN_RATE_LIMIT_BACKOFF_SEC", 8) or 8)

        positions = self.get_open_positions()
        if positions is None:
            err = str(getattr(self, "last_error", "") or "")
            if self._is_rate_limit_err(err):
                logger.warning(
                    f"[INDIA INTRADAY] square-off positions 429/DH-904 — sleep {backoff:.0f}s"
                )
                time.sleep(backoff)
                positions = self.get_open_positions()
            if positions is None:
                logger.warning("[INDIA INTRADAY] square-off skipped: positions unavailable")
                return closed

        if len(positions) == 0:
            logger.info("[SQUAREOFF SKIP] No open positions")
            logger.info("[SQUAREOFF] No open positions — skipping")
            self._squareoff_done_today = True
            return []

        if getattr(self, "dhan", None) is not None or getattr(self, "paper", None) is not None:
            try:
                self.cancel_all_open_orders()
                logger.info("[SQUAREOFF QUEUE] cancelled resting orders/brackets before flatten")
            except Exception as ce:
                logger.warning(f"[SQUAREOFF QUEUE] cancel-all before flatten skip: {ce}")

        for i, symbol in enumerate(list(positions.keys())):
            if gap > 0 and i > 0:
                lo = float(getattr(config, "FLATTEN_STAGGER_MIN_SEC", 1.0) or 1.0)
                hi = float(getattr(config, "FLATTEN_STAGGER_MAX_SEC", 2.5) or 2.5)
                if hi < lo:
                    hi = lo
                stagger = random.uniform(lo, hi)
                logger.info(
                    f"[SQUAREOFF QUEUE] {symbol}: stagger {stagger:.2f}s before flatten "
                    f"(429 / DH-904 guard)"
                )
                time.sleep(stagger)
            elif gap > 0:
                logger.info(f"[SQUAREOFF QUEUE] {symbol}: first name — flatten now")
            entry = float((positions.get(symbol) or {}).get("avg_entry_price") or 0)
            self.last_error = ""
            fill = self.close_position(symbol)
            err = str(getattr(self, "last_error", "") or "")

            if fill is None and self._is_rate_limit_err(err):
                logger.warning(
                    f"[INDIA INTRADAY] {symbol} close hit rate limit — backoff {backoff:.0f}s"
                )
                time.sleep(backoff)
                self.last_error = ""
                fill = self.close_position(symbol)
                err = str(getattr(self, "last_error", "") or "")

            # Verify flat even if fill unknown (broker may have closed)
            if gap > 0:
                time.sleep(min(gap, 1.0))
            verify = self.get_open_positions()
            if verify is None and self._is_rate_limit_err(
                str(getattr(self, "last_error", "") or "")
            ):
                time.sleep(backoff)
                verify = self.get_open_positions()
            still_open = bool(verify is not None and symbol in verify)

            if fill is not None and not still_open:
                px = float(fill)
                # Prefer broker fill history if close returned entry-like mark
                if entry > 0 and abs(px - entry) < 1e-6:
                    try:
                        hist = float(self.latest_sell_fill_price(symbol) or 0)
                        if hist > 0 and abs(hist - entry) > 1e-6:
                            px = hist
                    except Exception:
                        pass
                if entry > 0 and abs(px - entry) < 1e-9:
                    logger.error(
                        f"[INDIA INTRADAY] {symbol} flat but refuse journal @ entry "
                        f"{entry:.2f} — leaving journal open for fill resync"
                    )
                    closed.append(symbol)
                    continue
                closed.append(symbol)
                try:
                    import trade_journal

                    qty = int(getattr(self, "last_fill_qty", 0) or 0) or None
                    trade_journal.record_exit(
                        "INDIA", symbol, float(px), reason="squareoff", qty=qty
                    )
                except Exception as je:
                    logger.debug(f"Journal squareoff skip: {je}")
                logger.warning(f"[INDIA INTRADAY] Auto square-off {symbol} @ {px:.2f}")
            elif still_open:
                if self._is_mis_cutoff_reject(err):
                    logger.critical(
                        f"[INDIA INTRADAY] SQUARE-OFF REJECTED (RMS/cutoff) for {symbol}: "
                        f"{err} — NOT marking journal closed; broker may auto-flat"
                    )
                    try:
                        import alerts

                        alerts.notify(
                            f"SQUAREOFF REJECTED {symbol}: {err[:180]}",
                            event="squareoff_reject",
                        )
                    except Exception:
                        pass
                    failed.append(symbol)
                    continue
                logger.error(
                    f"[INDIA INTRADAY] square-off VERIFY FAILED — {symbol} still open "
                    f"err={err or 'n/a'}"
                )
                # one retry with gap
                time.sleep(max(gap, 1.0))
                fill2 = self.close_position(symbol)
                err2 = str(getattr(self, "last_error", "") or "")
                if self._is_rate_limit_err(err2):
                    time.sleep(backoff)
                    fill2 = self.close_position(symbol)
                    err2 = str(getattr(self, "last_error", "") or "")
                time.sleep(min(gap, 1.0) if gap > 0 else 0.5)
                verify2 = self.get_open_positions()
                if verify2 is not None and symbol not in verify2 and fill2 is not None:
                    px2 = float(fill2)
                    if entry > 0 and abs(px2 - entry) < 1e-9:
                        logger.error(
                            f"[INDIA INTRADAY] retry flat {symbol} but refuse journal @ entry"
                        )
                        closed.append(symbol)
                    else:
                        closed.append(symbol)
                        try:
                            import trade_journal

                            trade_journal.record_exit(
                                "INDIA", symbol, px2, reason="squareoff"
                            )
                        except Exception:
                            pass
                        logger.warning(
                            f"[INDIA INTRADAY] Auto square-off retry OK {symbol} @ {px2:.2f}"
                        )
                else:
                    if self._is_mis_cutoff_reject(err2):
                        logger.critical(
                            f"[INDIA INTRADAY] SQUARE-OFF RETRY REJECTED (RMS) {symbol}: {err2}"
                        )
                        try:
                            import alerts

                            alerts.notify(
                                f"SQUAREOFF RETRY REJECTED {symbol}: {err2[:180]}",
                                event="squareoff_reject",
                            )
                        except Exception:
                            pass
                    failed.append(symbol)
            elif fill is not None and verify is None:
                logger.warning(
                    f"[INDIA INTRADAY] {symbol} close submitted @ {fill:.2f} "
                    f"but positions unverifiable"
                )
            elif fill is None and not still_open and verify is not None:
                # Already flat (broker RMS beat us) — reconcile via journal helper later
                logger.warning(
                    f"[INDIA INTRADAY] {symbol} already flat at broker (no bot fill)"
                )
                closed.append(symbol)

        self.last_squareoff_failed = failed
        if failed:
            logger.critical(
                f"[INDIA INTRADAY] square-off incomplete — still open/rejected: {failed}"
            )
        else:
            self._squareoff_done_today = True
            logger.info("[SQUAREOFF DONE] All positions flattened")
        return closed

    def request_intraday_squareoff(self, risk_mgr=None) -> list[str]:
        """
        Non-blocking flatten: start a daemon worker that staggers close_position
        1.0–2.5s apart. Returns immediately so run_india_loop can skip the 5m scan.
        """
        if getattr(self, "_squareoff_done_today", False):
            logger.info("[SQUAREOFF QUEUE] already done today — skip")
            return []
        if not hasattr(self, "_flatten_lock"):
            self._flatten_lock = threading.Lock()
        with self._flatten_lock:
            t = getattr(self, "_flatten_thread", None)
            if t is not None and t.is_alive():
                logger.info("[SQUAREOFF QUEUE] worker already active — not stacking another flatten")
                return []
            self._flatten_risk_mgr = risk_mgr
            self._flatten_thread = threading.Thread(
                target=self._run_squareoff_worker,
                daemon=True,
                name="IndiaSquareoffWorker",
            )
            self._flatten_thread.start()
        logger.warning(
            "[SQUAREOFF QUEUE] non-blocking flatten worker started "
            "(stagger 1.0–2.5s/symbol, cancel-verify before each SELL)"
        )
        return []

    def _run_squareoff_worker(self) -> None:
        try:
            closed = self.square_off_intraday_positions()
            rm = getattr(self, "_flatten_risk_mgr", None)
            if rm is not None:
                for sym in closed or []:
                    try:
                        rm.release_margin(sym)
                        rm.clear_trade(sym)
                    except Exception as re:
                        logger.debug(f"[SQUAREOFF QUEUE] {sym} risk clear skip: {re}")
            failed = getattr(self, "last_squareoff_failed", None) or []
            if failed:
                logger.critical(
                    f"[SQUAREOFF QUEUE] worker finished with still-open: {failed}"
                )
            else:
                logger.info(f"[SQUAREOFF QUEUE] worker finished closed={closed}")
        except Exception as e:
            logger.critical(f"[SQUAREOFF QUEUE] worker crashed: {e}", exc_info=True)

    def reconcile_stuck_exits(self) -> list[str]:
        """
        Health check: release EXIT_PENDING_STUCK when the exit order is
        TRADED/CANCELLED/REJECTED or the broker is already flat.
        """
        released: list[str] = []
        rows = order_guards.list_exit_pending_stuck()
        if not rows:
            return released
        positions = None
        try:
            positions = self.get_open_positions()
        except Exception as e:
            logger.debug(f"[EXIT HEALTH] positions fetch skip: {e}")

        for row in rows:
            symbol = str(row.get("symbol") or "")
            oid = str(row.get("order_id") or "")
            terminal = False
            reason = ""
            if oid and self.paper is None:
                try:
                    status, detail = self.get_order_status(oid)
                    status_u = str(status or "").upper()
                    if status_u in _FILL_STATUSES or status_u in _REJECT_STATUSES:
                        terminal = True
                        reason = f"order {oid} {status_u}"
                    logger.info(
                        f"[EXIT HEALTH] {symbol}: stuck order {oid} status={status_u} "
                        f"detail={str(detail)[:80]}"
                    )
                except Exception as e:
                    logger.warning(f"[EXIT HEALTH] {symbol}: status poll failed: {e}")
            if not terminal and positions is not None and symbol not in positions:
                terminal = True
                reason = "broker_flat"
            if terminal:
                order_guards.clear_exit_pending_stuck(symbol, reason=reason)
                released.append(symbol)
                logger.warning(f"[EXIT HEALTH] {symbol}: unlocked ({reason})")
        return released

    def start_exit_health_monitor(self) -> None:
        """Daemon: poll stuck exit orders every 2s for TRADED/CANCELLED."""
        if getattr(self, "_exit_health_thread", None) and self._exit_health_thread.is_alive():
            return
        self._exit_health_stop = getattr(self, "_exit_health_stop", None) or threading.Event()

        def _loop():
            logger.info("[EXIT HEALTH] monitor thread started (2s poll)")
            while not self._exit_health_stop.wait(2.0):
                try:
                    self.reconcile_stuck_exits()
                except Exception as e:
                    logger.debug(f"[EXIT HEALTH] cycle skip: {e}")

        self._exit_health_thread = threading.Thread(
            target=_loop, daemon=True, name="ExitHealthMonitor"
        )
        self._exit_health_thread.start()


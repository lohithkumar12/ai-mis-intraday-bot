"""
india_broker.py — Angel One SmartAPI Broker Module
=====================================================
Wraps Angel One's SmartAPI for Indian stock trading (NSE).

Handles:
  - Auto-login with TOTP generation (no manual OTP needed)
  - Session refresh on token expiry
  - Login cooldown after Angel rate-limit responses
  - Shared singleton so dashboard + trading loop share one session
  - Historical candle data retrieval → pandas DataFrame (cached)
  - Real-time LTP (Last Traded Price) quotes
  - Order placement (NORMAL variety with DELIVERY product)
  - Position tracking and closing

Uses smartapi-python SDK + pyotp for TOTP auto-generation.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import pyotp
from SmartApi import SmartConnect

import config
from india_instruments import (
    INDIA_INSTRUMENTS,
    get_token,
    get_trading_symbol,
    get_exchange,
)
from india_paper import IndiaPaperPortfolio

logger = logging.getLogger(__name__)

# Angel One login rate limits are aggressive — back off hard after a deny.
LOGIN_COOLDOWN_BASE_SEC = 120.0
LOGIN_COOLDOWN_MAX_SEC = 900.0  # 15 minutes

_shared_broker: "IndiaBroker | None" = None
_shared_lock = threading.Lock()


def get_shared_india_broker(auto_login: bool = True) -> "IndiaBroker":
    """Process-wide singleton — bot loop and dashboard must share one session."""
    global _shared_broker
    with _shared_lock:
        if _shared_broker is None:
            _shared_broker = IndiaBroker(auto_login=auto_login)
        return _shared_broker


def _is_rate_limit_message(msg: str) -> bool:
    m = (msg or "").lower()
    return any(
        k in m
        for k in (
            "exceeding access rate",
            "access rate",
            "access denied",
            "too many",
            "rate limit",
            "rate-limit",
            "429",
        )
    )


class IndiaBroker:
    """
    Angel One SmartAPI broker client for Indian stock trading.

    When config.INDIA_PAPER is True:
      - Market data (candles/LTP) comes from LIVE Angel One APIs
      - Buys/sells update a virtual INR portfolio (no real orders)
    When LIVE_CONFIRMED:
      - Real CNC orders hit your Angel One account
    """

    def __init__(self, auto_login: bool = True):
        """Initialize the Angel One SmartAPI client."""
        self.api_key = config.ANGEL_API_KEY
        self.client_id = config.ANGEL_CLIENT_ID
        self.pin = config.ANGEL_PIN
        self.totp_secret = config.ANGEL_TOTP_SECRET

        self.smart_api = SmartConnect(api_key=self.api_key)
        self.auth_token = None
        self.feed_token = None
        self._session_time = None
        self._logged_in = False
        self.last_error = ""
        self._last_candle_call_time = 0
        self._candle_cache = {}  # {symbol: (timestamp, dataframe)}
        self._login_cooldown_until = 0.0
        self._login_cooldown_sec = LOGIN_COOLDOWN_BASE_SEC
        self._login_lock = threading.Lock()

        self.paper = IndiaPaperPortfolio() if config.INDIA_PAPER else None
        mode = "PAPER SIM (live NSE data, fake INR)" if self.paper else "LIVE REAL MONEY"
        logger.info(f"IndiaBroker mode: {mode}")

        if auto_login:
            self.login()

    # -----------------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------------
    def _arm_login_cooldown(self, reason: str) -> None:
        self._login_cooldown_until = time.time() + self._login_cooldown_sec
        logger.warning(
            f"Angel One login cooldown {self._login_cooldown_sec:.0f}s "
            f"(until rate window clears): {reason}"
        )
        # Escalate for repeated hits
        self._login_cooldown_sec = min(
            LOGIN_COOLDOWN_MAX_SEC, self._login_cooldown_sec * 1.5
        )

    def _clear_login_cooldown(self) -> None:
        self._login_cooldown_until = 0.0
        self._login_cooldown_sec = LOGIN_COOLDOWN_BASE_SEC

    def login(self) -> bool:
        with self._login_lock:
            now = time.time()
            if now < self._login_cooldown_until:
                remaining = int(self._login_cooldown_until - now)
                self.last_error = (
                    f"Angel One login cooling down ({remaining}s left after rate limit)"
                )
                logger.info(self.last_error)
                self._logged_in = False
                return False

            try:
                if not self.totp_secret or not self.client_id or not self.pin:
                    self.last_error = "Missing Angel One credentials in Environment"
                    self._logged_in = False
                    return False

                clean_secret = self.totp_secret.replace(" ", "").upper()
                totp = pyotp.TOTP(clean_secret).now()

                session_data = self.smart_api.generateSession(
                    self.client_id, self.pin, totp
                )

                if session_data and session_data.get("status"):
                    self.auth_token = session_data["data"]["jwtToken"]
                    self.feed_token = session_data["data"].get("feedToken")
                    self._session_time = datetime.now()
                    self._logged_in = True
                    self.last_error = ""
                    self._clear_login_cooldown()

                    logger.info(
                        f"Angel One LOGIN SUCCESS | Client: {self.client_id} | "
                        f"Session active"
                    )
                    return True

                msg = (
                    session_data.get("message", "Unknown login error")
                    if session_data
                    else "No response from Angel One"
                )
                self.last_error = msg
                logger.error(f"Angel One LOGIN FAILED: {msg}")
                self._logged_in = False
                if _is_rate_limit_message(msg):
                    self._arm_login_cooldown(msg)
                return False

            except Exception as e:
                err_msg = str(e)
                self.last_error = f"Login Exception: {err_msg}"
                logger.error(f"Angel One LOGIN ERROR: {e}", exc_info=True)
                self._logged_in = False
                if _is_rate_limit_message(err_msg):
                    self._arm_login_cooldown(err_msg)
                return False

    def ensure_session(self):
        """Re-login on missing session or ~12h expiry — respects login cooldown."""
        if time.time() < self._login_cooldown_until:
            return

        if not self._logged_in:
            self.login()
            return

        if self._session_time:
            elapsed = datetime.now() - self._session_time
            if elapsed > timedelta(hours=12):
                logger.info("Angel One session expired (~12h) — re-authenticating...")
                self.login()

    def _handle_api_error(self, context: str, err) -> bool:
        """
        Detect session/rate-limit issues. Returns True if caller should retry once.
        """
        msg = str(err).lower()
        # Rate limits first — never treat as auth and spam generateSession
        if _is_rate_limit_message(msg):
            wait = min(30.0, self._login_cooldown_sec)
            logger.warning(f"{context}: Angel rate limited — backing off {wait:.0f}s")
            self._arm_login_cooldown(str(err))
            time.sleep(wait)
            return False
        if any(
            k in msg
            for k in ("token", "jwt", "unauthorized", "session", "login", "auth")
        ):
            logger.warning(f"{context}: session issue — re-login")
            self._logged_in = False
            return self.login()
        return False

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    # -----------------------------------------------------------------------
    # Account Information
    # -----------------------------------------------------------------------
    def get_account_info(self) -> dict | None:
        # Paper mode: virtual cash + live marks
        if self.paper is not None:
            marks = self._live_marks_for_positions(self.paper.positions.keys())
            info = self.paper.get_account_info(marks)
            logger.info(
                f"Angel One PAPER | Equity=Rs {info['equity']:,.2f} | "
                f"Cash=Rs {info['available_cash']:,.2f}"
            )
            return info

        self.ensure_session()
        try:
            rms_data = self.smart_api.rmsLimit()
            if rms_data and rms_data.get("status"):
                data = rms_data["data"]
                net = float(data.get("net", 0) or 0)
                available_cash = float(data.get("availablecash", 0) or 0)
                used_margin = float(data.get("utiliseddebits", 0) or 0)

                info = {
                    "equity": net if net > 0 else available_cash,
                    "available_cash": available_cash,
                    "used_margin": used_margin,
                    "net": net,
                    "paper": False,
                }
                logger.info(
                    f"Angel One LIVE | Net={net:,.2f} | "
                    f"Cash={available_cash:,.2f} | Used={used_margin:,.2f}"
                )
                return info
            else:
                msg = rms_data.get("message", "Unknown") if rms_data else "No response"
                self.last_error = f"Account limit error: {msg}"
                logger.error(f"Failed to fetch Angel One account info: {msg}")
                return None

        except Exception as e:
            self.last_error = f"RMS Error: {e}"
            logger.error(f"Angel One account info error: {e}", exc_info=True)
            return None

    def _live_marks_for_positions(self, symbols) -> dict[str, float]:
        marks = {}
        for symbol in symbols:
            quote = self.get_latest_quote(symbol)
            if quote and quote.get("ltp"):
                marks[symbol] = float(quote["ltp"])
        return marks

    # -----------------------------------------------------------------------
    # Historical Data (with 30s Caching & Rate-Limiting Protection)
    # -----------------------------------------------------------------------
    def get_historical_bars(self, symbol: str, days: int = 300) -> pd.DataFrame | None:
        self.ensure_session()
        token = get_token(symbol)
        exchange = get_exchange(symbol)

        if not token:
            logger.error(f"No instrument token found for {symbol}")
            return None

        # Return cached dataframe if less than 30 seconds old to prevent rate limits
        now_ts = time.time()
        if symbol in self._candle_cache:
            cache_time, cached_df = self._candle_cache[symbol]
            if now_ts - cache_time < 30.0:
                return cached_df

        # Rate-limiting: Ensure 400ms gap between Angel One API calls
        time_since_last = now_ts - self._last_candle_call_time
        if time_since_last < 0.40:
            time.sleep(0.40 - time_since_last)
        self._last_candle_call_time = time.time()

        try:
            to_date = datetime.now()
            from_date = to_date - timedelta(days=days)

            historic_params = {
                "exchange": exchange,
                "symboltoken": token,
                "interval": "ONE_HOUR",
                "fromdate": from_date.strftime("%Y-%m-%d 09:15"),
                "todate": to_date.strftime("%Y-%m-%d 15:30"),
            }

            result = self.smart_api.getCandleData(historic_params)

            if result and result.get("status"):
                candles = result["data"]
                if not candles:
                    logger.warning(f"{symbol}: No candle data returned")
                    return None

                df = pd.DataFrame(
                    candles,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df.set_index("timestamp", inplace=True)
                df = df.astype(float)

                self._candle_cache[symbol] = (time.time(), df)
                logger.debug(f"{symbol}: Fetched {len(df)} candles from Angel One")
                return df
            else:
                msg = result.get("message", "Unknown") if result else "No response"
                logger.warning(f"{symbol}: Failed to fetch candles — {msg}")
                # If cached version exists, return it as fallback
                if symbol in self._candle_cache:
                    return self._candle_cache[symbol][1]
                return None

        except Exception as e:
            if self._handle_api_error(f"{symbol} candles", e):
                try:
                    result = self.smart_api.getCandleData(historic_params)
                    if result and result.get("status") and result.get("data"):
                        candles = result["data"]
                        df = pd.DataFrame(
                            candles,
                            columns=["timestamp", "open", "high", "low", "close", "volume"],
                        )
                        df["timestamp"] = pd.to_datetime(df["timestamp"])
                        df.set_index("timestamp", inplace=True)
                        df = df.astype(float)
                        self._candle_cache[symbol] = (time.time(), df)
                        return df
                except Exception as e2:
                    logger.error(f"{symbol}: candle retry failed: {e2}")
            logger.error(f"{symbol}: Angel One historical data error: {e}", exc_info=True)
            if symbol in self._candle_cache:
                return self._candle_cache[symbol][1]
            return None

    # -----------------------------------------------------------------------
    # Real-time Quote (LTP)
    # -----------------------------------------------------------------------
    def get_latest_quote(self, symbol: str) -> dict | None:
        self.ensure_session()
        token = get_token(symbol)
        trading_symbol = get_trading_symbol(symbol)
        exchange = get_exchange(symbol)

        if not token or not trading_symbol:
            logger.error(f"No instrument info for {symbol}")
            return None

        try:
            ltp_data = self.smart_api.ltpData(exchange, trading_symbol, token)

            if ltp_data and ltp_data.get("status"):
                data = ltp_data["data"]
                ltp = float(data.get("ltp", 0))

                return {
                    "ltp": ltp,
                    "ask_price": ltp,
                    "symbol": symbol,
                    "exchange": exchange,
                }
            else:
                msg = ltp_data.get("message", "Unknown") if ltp_data else "No response"
                logger.warning(f"{symbol}: LTP fetch failed — {msg}")
                return None

        except Exception as e:
            logger.error(f"{symbol}: LTP error: {e}", exc_info=True)
            return None

    # -----------------------------------------------------------------------
    # Live Trading Gate
    # -----------------------------------------------------------------------
    def _assert_live_allowed(self, action: str) -> bool:
        """Allow paper sim always; block real orders unless LIVE_CONFIRMED."""
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
        """Normalize Angel One placeOrder response to an order id string."""
        if result is None:
            return None
        if isinstance(result, str) and result.strip():
            return result.strip()
        if isinstance(result, dict):
            if result.get("status") is False:
                return None
            data = result.get("data")
            if isinstance(data, dict):
                oid = data.get("orderid") or data.get("orderId")
                if oid:
                    return str(oid)
            if result.get("orderid"):
                return str(result["orderid"])
        return str(result) if result else None

    # -----------------------------------------------------------------------
    # Position Tracking (paper OR live positions + CNC holdings)
    # -----------------------------------------------------------------------
    def get_open_positions(self) -> dict:
        if self.paper is not None:
            marks = self._live_marks_for_positions(self.paper.positions.keys())
            pos_dict = self.paper.get_open_positions(marks)
            if pos_dict:
                logger.info(f"India PAPER positions: {list(pos_dict.keys())}")
            return pos_dict

        self.ensure_session()
        pos_dict: dict = {}

        try:
            position_data = self.smart_api.position()
            if position_data and position_data.get("status"):
                for pos in position_data.get("data") or []:
                    net_qty = int(float(pos.get("netqty", 0) or 0))
                    if net_qty == 0:
                        continue

                    symbol_raw = pos.get("tradingsymbol", "")
                    symbol = symbol_raw.replace("-EQ", "")
                    buy_price = float(pos.get("averageprice", 0) or 0)
                    ltp = float(pos.get("ltp", 0) or 0)
                    pnl = float(pos.get("pnl", 0) or 0)
                    pnl_pct = ((ltp - buy_price) / buy_price) if buy_price > 0 else 0

                    pos_dict[symbol] = {
                        "qty": abs(net_qty),
                        "avg_entry_price": buy_price,
                        "current_price": ltp,
                        "market_value": abs(net_qty) * ltp,
                        "unrealized_pl": pnl,
                        "unrealized_plpc": pnl_pct,
                        "trading_symbol": symbol_raw,
                        "token": pos.get("symboltoken", ""),
                        "source": "position",
                    }
        except Exception as e:
            logger.error(f"Angel One positions error: {e}", exc_info=True)

        try:
            holding_data = self.smart_api.holding()
            if holding_data and holding_data.get("status"):
                for h in holding_data.get("data") or []:
                    qty = int(float(h.get("quantity", 0) or 0))
                    if qty <= 0:
                        continue

                    symbol_raw = h.get("tradingsymbol", "")
                    symbol = symbol_raw.replace("-EQ", "")
                    if symbol in pos_dict:
                        continue

                    buy_price = float(
                        h.get("averageprice")
                        or h.get("avgprice")
                        or 0
                    )
                    ltp = float(h.get("ltp", 0) or 0)
                    pnl = float(h.get("profitandloss") or h.get("pnl") or 0)
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
                        "trading_symbol": symbol_raw,
                        "token": h.get("symboltoken", ""),
                        "source": "holding",
                    }
        except Exception as e:
            logger.warning(f"Angel One holdings fetch warning: {e}")

        if pos_dict:
            logger.info(f"India positions/holdings: {list(pos_dict.keys())}")
        return pos_dict

    # -----------------------------------------------------------------------
    # Order Placement
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
    ) -> str | None:
        if not self._assert_live_allowed(f"BUY {symbol}"):
            return None

        if qty <= 0 or limit_price <= 0:
            logger.error(f"Invalid buy params for {symbol}: qty={qty} price={limit_price}")
            return None

        # Resolve SL for paper + live
        if stop_loss_price is None:
            sl_pct = stop_loss_pct if stop_loss_pct is not None else config.STOP_LOSS_PCT
            stop_loss_price = round(limit_price * (1 - sl_pct), 2)

        # ----- PAPER SIM (live LTP, fake fill) -----
        if self.paper is not None:
            quote = self.get_latest_quote(symbol)
            fill = float(quote["ltp"]) if quote else float(limit_price)
            return self.paper.buy(
                symbol,
                qty,
                fill,
                stop_loss=stop_loss_price,
                take_profit=take_profit_price,
                atr=atr,
            )

        # ----- LIVE REAL ORDERS -----
        self.ensure_session()
        token = get_token(symbol)
        trading_symbol = get_trading_symbol(symbol)
        exchange = get_exchange(symbol)

        if not token or not trading_symbol:
            logger.error(f"Cannot place order — no instrument info for {symbol}")
            return None

        try:
            entry_price = round(limit_price * 1.001, 2)

            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": trading_symbol,
                "symboltoken": token,
                "transactiontype": "BUY",
                "exchange": exchange,
                "ordertype": "LIMIT",
                "producttype": "CNC",
                "duration": "DAY",
                "price": str(entry_price),
                "quantity": str(qty),
            }

            raw = self.smart_api.placeOrder(order_params)
            order_id = self._extract_order_id(raw)

            if not order_id:
                logger.error(f"BUY rejected for {symbol}: {raw}")
                self.last_error = f"Buy rejected: {raw}"
                return None

            logger.warning(
                f"LIVE BUY ORDER | {symbol} | Qty={qty} | "
                f"Limit={entry_price:.2f} | Order ID={order_id}"
            )

            if place_stoploss and stop_loss_price:
                self.place_stoploss_order(symbol, qty, stop_loss_price)

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
                    )
                finally:
                    self._buy_retrying = False
            logger.error(f"Failed to place BUY order for {symbol}: {e}", exc_info=True)
            return None

    def place_stoploss_order(
        self,
        symbol: str,
        qty: int,
        trigger_price: float,
    ) -> str | None:
        """Broker-side day SL — skipped in paper mode (software SL handles it)."""
        if self.paper is not None:
            logger.info(
                f"[PAPER] Soft stop-loss armed for {symbol} @ {trigger_price:.2f}"
            )
            return f"PAPER-SL-{symbol}"

        if not self._assert_live_allowed(f"SL {symbol}"):
            return None

        self.ensure_session()
        token = get_token(symbol)
        trading_symbol = get_trading_symbol(symbol)
        exchange = get_exchange(symbol)

        if not token or not trading_symbol or qty <= 0 or trigger_price <= 0:
            return None

        try:
            limit_price = round(trigger_price * 0.995, 2)

            order_params = {
                "variety": "STOPLOSS",
                "tradingsymbol": trading_symbol,
                "symboltoken": token,
                "transactiontype": "SELL",
                "exchange": exchange,
                "ordertype": "STOPLOSS_LIMIT",
                "producttype": "CNC",
                "duration": "DAY",
                "price": str(limit_price),
                "triggerprice": str(round(trigger_price, 2)),
                "quantity": str(qty),
            }

            raw = self.smart_api.placeOrder(order_params)
            order_id = self._extract_order_id(raw)

            if order_id:
                logger.warning(
                    f"LIVE STOP-LOSS | {symbol} | Qty={qty} | "
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
    ) -> str | None:
        if not self._assert_live_allowed(f"SELL {symbol}"):
            return None

        # ----- PAPER SIM -----
        if self.paper is not None:
            quote = self.get_latest_quote(symbol)
            fill = float(quote["ltp"]) if quote else float(limit_price or 0)
            if fill <= 0:
                logger.error(f"[PAPER] No LTP to sell {symbol}")
                return None
            return self.paper.sell(symbol, qty, fill)

        # ----- LIVE -----
        self.ensure_session()
        token = get_token(symbol)
        trading_symbol = get_trading_symbol(symbol)
        exchange = get_exchange(symbol)

        if not token or not trading_symbol:
            logger.error(f"Cannot place sell — no instrument info for {symbol}")
            return None

        try:
            if order_type == "MARKET" and limit_price <= 0:
                quote = self.get_latest_quote(symbol)
                if quote:
                    limit_price = round(quote["ltp"] * 0.995, 2)
                    order_type = "LIMIT"

            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": trading_symbol,
                "symboltoken": token,
                "transactiontype": "SELL",
                "exchange": exchange,
                "ordertype": order_type if order_type != "MARKET" else "LIMIT",
                "producttype": "CNC",
                "duration": "DAY",
                "quantity": str(qty),
            }

            if order_params["ordertype"] == "LIMIT":
                if limit_price <= 0:
                    logger.error(f"Sell needs a limit price for {symbol}")
                    return None
                order_params["price"] = str(round(limit_price, 2))

            raw = self.smart_api.placeOrder(order_params)
            order_id = self._extract_order_id(raw)

            if not order_id:
                logger.error(f"SELL rejected for {symbol}: {raw}")
                return None

            logger.warning(
                f"LIVE SELL ORDER | {symbol} | Qty={qty} | "
                f"Type={order_params['ordertype']} | Order ID={order_id}"
            )
            return order_id

        except Exception as e:
            logger.error(f"Failed to place SELL order for {symbol}: {e}", exc_info=True)
            return None

    def close_position(self, symbol: str) -> bool:
        positions = self.get_open_positions()
        if symbol not in positions:
            logger.warning(f"{symbol}: No open position to close")
            return False

        qty = positions[symbol]["qty"]
        order_id = self.place_sell_order(symbol, qty, order_type="MARKET")

        if order_id:
            logger.info(f"Position CLOSED for {symbol} (Qty={qty})")
            return True
        return False

    def check_sl_tp(self, risk_mgr) -> list[str]:
        """
        Software SL/TP monitor (critical for paper; backup for live).
        Uses stored ATR levels when present, updates trailing stops via risk_mgr.
        """
        closed_symbols = []
        positions = self.get_open_positions()

        for symbol, pos in positions.items():
            entry_price = float(pos["avg_entry_price"])
            current_price = float(pos["current_price"])
            atr = pos.get("atr")
            if atr is not None:
                try:
                    atr = float(atr)
                except (TypeError, ValueError):
                    atr = None

            # Prefer stored levels; else derive from risk manager
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

            # Register + trail
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

            if reason and self.close_position(symbol):
                closed_symbols.append(symbol)
                risk_mgr.clear_trade(symbol)
                try:
                    import trade_journal

                    trade_journal.record_exit(
                        "INDIA", symbol, current_price, reason=reason
                    )
                except Exception as je:
                    logger.debug(f"Journal exit skip: {je}")

        return closed_symbols

    def cancel_all_open_orders(self) -> bool:
        if self.paper is not None:
            logger.info("[PAPER] No broker orders to cancel")
            return True

        self.ensure_session()
        try:
            order_book = self.smart_api.orderBook()
            if not order_book or not order_book.get("status"):
                return True

            orders = order_book.get("data", [])
            if not orders:
                return True

            cancelled = 0
            for order in orders:
                status = order.get("orderstatus", "").lower()
                if status in ("open", "pending", "trigger pending"):
                    try:
                        self.smart_api.cancelOrder(
                            order["orderid"], order.get("variety", "NORMAL")
                        )
                        cancelled += 1
                    except Exception as e:
                        logger.error(f"Failed to cancel order {order['orderid']}: {e}")

            if cancelled > 0:
                logger.info(f"Cancelled {cancelled} open India orders")
            return True

        except Exception as e:
            logger.error(f"Angel One cancel orders error: {e}", exc_info=True)
            return False

    def logout(self):
        try:
            self.smart_api.terminateSession(self.client_id)
            self._logged_in = False
            logger.info("Angel One session terminated")
        except Exception as e:
            logger.warning(f"Angel One logout error: {e}")

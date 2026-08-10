"""
dhan_live_feed.py — Real DhanHQ WebSocket Live Feed & Order Update Manager
==========================================================================
Manages real-time WebSocket connections to Dhan HQ:
  1. Live Market Data Feed (dhanhq.marketfeed.MarketFeed) — LTP, OHLC, OI
  2. Order Update Feed (dhanhq.orderupdate.OrderUpdate) — Fills, Rejects, Cancels

Process-wide singleton. is_connected() is True ONLY after a recent real tick
(< 60s). Dynamic subscribe/unsubscribe updates the live socket subscription list.
Reconnect uses exponential backoff; quote cache falls back to REST when down.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import config

logger = logging.getLogger(__name__)

_feed_instance: "DhanLiveFeedManager | None" = None
_feed_lock = threading.Lock()

# Dhan allows few concurrent MarketFeed sockets per client; 429 means back off hard.
_FEED_BACKOFF_START_SEC = 5.0
_FEED_BACKOFF_MAX_SEC = 300.0  # 5 minutes
_FEED_429_BACKOFF_SEC = 120.0  # 2 minutes minimum after 429
_FEED_429_BACKOFF_MAX_SEC = 600.0  # 10 minutes
_RECONNECT_DEBOUNCE_SEC = 45.0

# Known index / equity seed tokens (overridden by scrip master on subscribe)
_SEED_SEC_MAP = {
    "2885": "RELIANCE",
    "11536": "TCS",
    "1333": "HDFCBANK",
    "1594": "INFY",
    "13": "NIFTY",
    "25": "BANKNIFTY",
    "27": "FINNIFTY",
}


def get_live_feed_manager() -> "DhanLiveFeedManager":
    global _feed_instance
    with _feed_lock:
        if _feed_instance is None:
            _feed_instance = DhanLiveFeedManager()
        return _feed_instance


def _marketfeed_segment(segment: str):
    """Map Dhan API / config segment names to MarketFeed numeric codes."""
    from dhanhq.marketfeed import MarketFeed

    seg = (segment or "NSE").strip().upper()
    mapping = {
        "NSE": MarketFeed.NSE,
        "NSE_EQ": MarketFeed.NSE,
        "BSE": MarketFeed.BSE,
        "BSE_EQ": MarketFeed.BSE,
        "NSE_FNO": MarketFeed.NSE_FNO,
        "BSE_FNO": getattr(MarketFeed, "BSE_FNO", MarketFeed.NSE_FNO),
        "NSE_CURR": MarketFeed.NSE_CURR,
        "NSE_CURRENCY": MarketFeed.NSE_CURR,
        "BSE_CURRENCY": getattr(MarketFeed, "BSE_CURR", MarketFeed.NSE_CURR),
        "BSE_CURR": getattr(MarketFeed, "BSE_CURR", MarketFeed.NSE_CURR),
        "MCX": MarketFeed.MCX,
        "MCX_COMM": MarketFeed.MCX,
        "IDX": getattr(MarketFeed, "IDX", 0),
        "IDX_I": getattr(MarketFeed, "IDX", 0),
        "INDEX": getattr(MarketFeed, "IDX", 0),
    }
    return mapping.get(seg, MarketFeed.NSE)


class DhanLiveFeedManager:
    """
    Real DhanHQ WebSocket Market Feed & Order Update Client (process-wide singleton).
    """

    def __init__(self):
        self.client_id = (config.DHAN_CLIENT_ID or "").strip()
        self.access_token = (config.DHAN_ACCESS_TOKEN or "").strip()
        # Want live feed whenever the flag + client id are set; token may arrive
        # after PIN/TOTP login (paid ₹499 Data API uses the same access token).
        self._want_live = bool(config.DHAN_LIVE_WEBSOCKET and self.client_id)
        self.enabled = bool(self._want_live and self.access_token)

        self._quote_cache: dict[str, dict[str, Any]] = {}
        self._order_updates: list[dict[str, Any]] = []
        self._subscribed_symbols: set[str] = set()
        self._instrument_tuples: list[tuple] = []  # (seg_enum, sec_id, mode)
        self._sec_id_to_symbol: dict[str, str] = dict(_SEED_SEC_MAP)

        self._is_connected = False
        self._last_heartbeat = 0.0
        self._last_recv_at = 0.0
        self._diag_frames = 0
        self._last_error = ""
        self._run_error: BaseException | None = None
        self._lock = threading.Lock()
        self._stop = False

        self._ws_feed = None
        self._order_ws = None
        self._feed_thread = None
        self._order_thread = None
        self._reconnect_attempts = 0
        self._last_force_reconnect_at = 0.0
        self._rate_limited_until = 0.0
        self._connect_lock = threading.Lock()

        if self.enabled:
            logger.info("[FEED] Initializing DhanHQ WebSocket Market Feed & Order Update Manager...")
            self._start_threads()
        elif self._want_live:
            logger.info(
                "[FEED] Live Data API waiting for access token "
                "(broker PIN/TOTP login will connect the WebSocket)."
            )
        else:
            logger.info("[FEED] Dhan Live WebSocket Feed disabled or credentials missing.")

    def update_credentials(
        self,
        client_id: str,
        access_token: str,
        reconnect: bool = True,
    ) -> None:
        """
        Apply broker-refreshed Client ID + access token to the paid Data API feed.
        Starts threads on first valid token; forces reconnect when token changes.
        """
        client_id = (client_id or "").strip()
        access_token = (access_token or "").strip()
        if not client_id or not access_token:
            return

        with self._lock:
            changed = (
                client_id != self.client_id or access_token != self.access_token
            )
            self.client_id = client_id
            self.access_token = access_token
            self._want_live = bool(config.DHAN_LIVE_WEBSOCKET and self.client_id)
            was_enabled = self.enabled
            self.enabled = bool(self._want_live and self.access_token)

        if not self.enabled:
            return

        threads_alive = (
            self._feed_thread is not None and self._feed_thread.is_alive()
        )
        if not was_enabled or not threads_alive:
            self._stop = False
            logger.info(
                "[FEED] Connecting paid Live Market Feed with refreshed access token..."
            )
            self._start_threads()
        elif reconnect and changed:
            # Keep an already-working socket; apply new token on natural reconnect.
            # Only force-close when feed is down (avoids 429 reconnect storms).
            if self.is_connected():
                logger.info(
                    "[FEED] Access token updated — keeping live socket; "
                    "new token used on next reconnect"
                )
            else:
                self._force_reconnect(reason="token_updated_while_down")

    def _force_reconnect(self, reason: str = "manual") -> None:
        """Close the open MarketFeed socket so the loop reconnects with new creds."""
        now = time.time()
        if now - self._last_force_reconnect_at < _RECONNECT_DEBOUNCE_SEC:
            logger.info(
                f"[FEED] Skip force reconnect ({reason}) — debounced "
                f"<{_RECONNECT_DEBOUNCE_SEC:.0f}s"
            )
            return
        self._last_force_reconnect_at = now
        with self._lock:
            ws = self._ws_feed
            self._is_connected = False
        if ws is None:
            return
        logger.info(f"[FEED] Force reconnect ({reason})")
        self._close_ws(ws)

    @staticmethod
    def _close_ws(ws) -> None:
        for method_name in ("close_connection", "disconnect", "close", "stop", "logout"):
            method = getattr(ws, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                return
            except Exception as e:
                logger.debug(f"[FEED] {method_name} during close: {e}")

    @staticmethod
    def _is_rate_limit_error(err: BaseException | str) -> bool:
        msg = str(err).lower()
        return (
            "429" in msg
            or "too many requests" in msg
            or "rate limit" in msg
            or "connection limit" in msg
            or "rejected websocket" in msg
            or "805" in msg
        )

    def _arm_rate_limit(self, backoff: float, reason: str) -> None:
        backoff = min(max(backoff, _FEED_429_BACKOFF_SEC), _FEED_429_BACKOFF_MAX_SEC)
        with self._lock:
            self._rate_limited_until = max(
                self._rate_limited_until, time.time() + backoff
            )
            self._last_error = reason
        logger.warning(
            f"[FEED] MarketFeed rate-limited — cool-down {backoff:.0f}s ({reason})"
        )

    def _start_threads(self):
        if self._feed_thread is not None and self._feed_thread.is_alive():
            return
        self._feed_thread = threading.Thread(
            target=self._market_feed_loop, daemon=True, name="DhanLiveMarketFeed"
        )
        self._feed_thread.start()
        if self._order_thread is None or not self._order_thread.is_alive():
            self._order_thread = threading.Thread(
                target=self._order_update_loop, daemon=True, name="DhanOrderUpdateFeed"
            )
            self._order_thread.start()

    def _on_ticks_callback(self, ws_or_data, data=None):
        """
        MarketFeed on_message/on_ticks signature is (ws, data) when using run().
        Also accept a bare dict for tests / older call sites.
        """
        payload = data if data is not None else ws_or_data
        with self._lock:
            self._last_recv_at = time.time()
            self._diag_frames += 1
            frame_n = self._diag_frames

        if frame_n <= 8:
            if isinstance(payload, dict):
                logger.info(
                    f"[FEED] frame#{frame_n}: type={payload.get('type')} "
                    f"sec={payload.get('security_id')} LTP={payload.get('LTP')}"
                )
            else:
                logger.info(f"[FEED] frame#{frame_n}: {type(payload).__name__}={payload!r}"[:200])
        elif frame_n == 9:
            logger.info("[FEED] Tick stream healthy — further frame dumps at DEBUG")

        if isinstance(payload, list):
            for item in payload:
                self._on_tick(item)
        else:
            self._on_tick(payload)

    def _on_ws_error(self, _ws, err):
        self._run_error = err if isinstance(err, BaseException) else Exception(str(err))
        self._last_error = str(err)
        if self._is_rate_limit_error(err):
            self._arm_rate_limit(_FEED_429_BACKOFF_SEC, str(err))
            ws = self._ws_feed
            if ws is not None:
                self._close_ws(ws)
        else:
            logger.warning(f"[FEED] MarketFeed WS error: {err}")

    def _on_tick(self, data):
        if not isinstance(data, dict):
            # Market status sometimes returns a plain string
            if isinstance(data, str) and data:
                with self._lock:
                    self._last_recv_at = time.time()
                    self._is_connected = True
            return
        sec_id = str(data.get("security_id") or data.get("securityId") or "")
        with self._lock:
            sym = data.get("symbol") or self._sec_id_to_symbol.get(sec_id) or ""
            self._last_recv_at = time.time()
            self._is_connected = True
        if not sym:
            # Keep sec_id as fallback label only when we have LTP
            sym = sec_id
        try:
            ltp = float(
                data.get("LTP") or data.get("last_price") or data.get("close") or 0.0
            )
        except (TypeError, ValueError):
            ltp = 0.0
        if sym and ltp > 0:
            self.update_quote(
                symbol=sym,
                ltp=ltp,
                open_price=float(data.get("open", 0.0) or 0.0),
                high=float(data.get("high", 0.0) or 0.0),
                low=float(data.get("low", 0.0) or 0.0),
                close=float(data.get("close", 0.0) or 0.0),
                volume=int(data.get("volume", 0) or 0),
                oi=int(data.get("oi", 0) or data.get("OI", 0) or 0),
            )

    def _snapshot_instruments(self) -> list:
        with self._lock:
            if self._instrument_tuples:
                return list(self._instrument_tuples)
        # Minimal seed so socket can connect before universe subscribe
        try:
            from dhanhq.marketfeed import MarketFeed

            return [(MarketFeed.NSE, "2885", MarketFeed.Quote)]
        except Exception:
            return []

    def _market_feed_loop(self):
        """Reconnect-with-backoff loop for MarketFeed WebSocket (one socket at a time)."""
        backoff = _FEED_BACKOFF_START_SEC
        while not self._stop:
            with self._lock:
                cid = self.client_id
                tok = self.access_token
                rate_until = self._rate_limited_until
            if not (cid and tok):
                time.sleep(2.0)
                continue

            # Honour Dhan 429 cool-down before opening another socket
            wait_rate = rate_until - time.time()
            if wait_rate > 0:
                logger.warning(
                    f"[FEED] Rate-limited by Dhan — waiting {wait_rate:.0f}s "
                    f"before next MarketFeed connect"
                )
                time.sleep(min(wait_rate, 30.0))
                continue

            # Serialize connects so we never open two MarketFeed sockets
            if not self._connect_lock.acquire(blocking=False):
                time.sleep(2.0)
                continue

            had_ticks = False
            was_rate_limited = False
            try:
                from dhanhq import DhanContext
                from dhanhq.marketfeed import MarketFeed

                ctx = DhanContext(cid, tok)
                instruments = self._snapshot_instruments()
                self._diag_frames = 0
                self._run_error = None
                self._ws_feed = MarketFeed(
                    ctx,
                    instruments=instruments,
                    version="v2",
                    on_ticks=self._on_ticks_callback,
                    on_error=self._on_ws_error,
                )
                logger.info(
                    f"[FEED] MarketFeed connecting with {len(instruments)} instrument(s)..."
                )
                hb_before = self._last_heartbeat

                # Prefer run() so the asyncio loop + websocket keepalive stay alive.
                # run_forever() only connects then returns → instant "disconnect" storm.
                if hasattr(self._ws_feed, "run"):
                    logger.info("[FEED] MarketFeed run() — receiving ticks")
                    self._ws_feed.run()
                elif hasattr(self._ws_feed, "run_forever"):
                    self._ws_feed.run_forever()
                    while not self._stop:
                        with self._lock:
                            if time.time() < self._rate_limited_until:
                                was_rate_limited = True
                                break
                        try:
                            data = self._ws_feed.get_data()
                            self._on_ticks_callback(self._ws_feed, data)
                        except Exception as recv_err:
                            if self._is_rate_limit_error(recv_err):
                                was_rate_limited = True
                                raise
                            logger.warning(
                                f"[FEED] MarketFeed recv ended: {recv_err}"
                            )
                            break
                else:
                    logger.error("[FEED] MarketFeed has no run/run_forever")
                    break

                if self._run_error and self._is_rate_limit_error(self._run_error):
                    was_rate_limited = True
                    raise self._run_error

                had_ticks = self._last_heartbeat > hb_before
                logger.warning("[FEED] MarketFeed disconnected — scheduling reconnect")
                if had_ticks:
                    backoff = _FEED_BACKOFF_START_SEC
                    self._reconnect_attempts = 0
            except Exception as wse:
                self._last_error = str(wse)
                if self._is_rate_limit_error(wse):
                    was_rate_limited = True
                    backoff = max(backoff, _FEED_429_BACKOFF_SEC)
                    backoff = min(backoff, _FEED_429_BACKOFF_MAX_SEC)
                    with self._lock:
                        self._rate_limited_until = time.time() + backoff
                    logger.warning(
                        f"[FEED] MarketFeed HTTP 429 from Dhan — cool-down {backoff:.0f}s "
                        f"(do not open another socket for this Client ID)"
                    )
                else:
                    logger.warning(
                        f"[FEED] MarketFeed error ({wse}) — reconnect in {backoff:.0f}s"
                    )
            finally:
                with self._lock:
                    self._is_connected = False
                    ws = self._ws_feed
                    self._ws_feed = None
                if ws is not None:
                    self._close_ws(ws)
                self._reconnect_attempts += 1
                try:
                    self._connect_lock.release()
                except RuntimeError:
                    pass

            if self._stop:
                break
            time.sleep(backoff)
            if was_rate_limited:
                backoff = min(backoff * 1.5, _FEED_429_BACKOFF_MAX_SEC)
            elif not had_ticks:
                backoff = min(
                    max(backoff, _FEED_BACKOFF_START_SEC) * 2.0,
                    _FEED_BACKOFF_MAX_SEC,
                )

    def _order_update_loop(self):
        backoff = _FEED_BACKOFF_START_SEC
        while not self._stop:
            with self._lock:
                cid = self.client_id
                tok = self.access_token
            if not (cid and tok):
                time.sleep(2.0)
                continue
            try:
                from dhanhq.orderupdate import OrderUpdate

                def on_order_update(update_payload):
                    logger.info(f"[FEED] [ORDER UPDATE] {update_payload}")
                    self.push_order_update(update_payload)

                order_client = OrderUpdate(cid, tok)
                self._order_ws = order_client
                if hasattr(order_client, "connect_order_update"):
                    order_client.connect_order_update(on_order_update)
                elif hasattr(order_client, "connect_to_dhan_websocket_sync"):
                    order_client.connect_to_dhan_websocket_sync(on_order_update)
                else:
                    logger.debug("[FEED] OrderUpdate connect method not found")
                    return
                backoff = _FEED_BACKOFF_START_SEC
            except Exception as oe:
                if self._is_rate_limit_error(oe):
                    backoff = max(backoff, _FEED_429_BACKOFF_SEC)
                    backoff = min(backoff * 1.5, _FEED_429_BACKOFF_MAX_SEC)
                    logger.warning(f"[FEED] OrderUpdate 429 — cool-down {backoff:.0f}s")
                else:
                    logger.debug(f"[FEED] OrderUpdate WS: {oe}")
            time.sleep(backoff)
            backoff = min(max(backoff, _FEED_BACKOFF_START_SEC) * 2.0, _FEED_BACKOFF_MAX_SEC)

    def update_quote(
        self,
        symbol: str,
        ltp: float,
        open_price: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
        close: float = 0.0,
        volume: int = 0,
        oi: int = 0,
    ):
        """Update live quote cache from WebSocket tick."""
        with self._lock:
            self._quote_cache[symbol] = {
                "symbol": symbol,
                "ltp": ltp,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "oi": oi,
                "timestamp": time.time(),
                "source": "websocket_live",
            }
            self._last_heartbeat = time.time()
            self._is_connected = True

    def get_live_quote(self, symbol: str) -> dict[str, Any] | None:
        """Returns cached WebSocket quote if fresh (< 30 sec old)."""
        with self._lock:
            cached = self._quote_cache.get(symbol)
            if cached and (time.time() - cached["timestamp"] < 30.0):
                return dict(cached)
            return None

    def push_order_update(self, update_data: dict):
        with self._lock:
            self._order_updates.append(
                {
                    "timestamp": time.time(),
                    "data": update_data,
                }
            )
            if len(self._order_updates) > 100:
                self._order_updates.pop(0)

    def _build_instrument_tuple(self, symbol: str, segment: str, sec_id: str):
        from dhanhq.marketfeed import MarketFeed

        seg_enum = _marketfeed_segment(segment)
        mode = getattr(MarketFeed, "Quote", 17)
        return (seg_enum, str(sec_id), mode)

    def subscribe_symbol(self, symbol: str, segment: str = "NSE") -> bool:
        """
        Dynamically subscribe a symbol to the live WebSocket feed.
        Records subscription intent AND pushes instruments onto the open socket.
        """
        symbol = symbol.strip().upper()
        with self._lock:
            if symbol in self._subscribed_symbols:
                return True

        try:
            from india_fno_instruments import is_placeholder_security_id, resolve_instrument_info

            info = resolve_instrument_info(symbol, exchange_segment=segment)
            sec_id = str(info.get("security_id") or "")
            exch_seg = info.get("exchange") or segment
            if is_placeholder_security_id(sec_id):
                logger.warning(
                    f"[FEED] Skip subscribe {symbol}: unresolved/placeholder security_id"
                )
                with self._lock:
                    self._subscribed_symbols.add(symbol)  # intent recorded; socket skip
                return False

            instr = self._build_instrument_tuple(symbol, exch_seg, sec_id)
            with self._lock:
                self._subscribed_symbols.add(symbol)
                self._sec_id_to_symbol[str(sec_id)] = symbol
                if instr not in self._instrument_tuples:
                    self._instrument_tuples.append(instr)

            pushed = self._push_subscribe([instr])
            logger.info(
                f"[FEED] Subscribed {symbol} ({exch_seg}:{sec_id}) socket_push={pushed}"
            )
            return True
        except Exception as e:
            logger.warning(f"[FEED] Dynamic subscribe warning for {symbol}: {e}")
            with self._lock:
                self._subscribed_symbols.add(symbol)
            return False

    def unsubscribe_symbol(self, symbol: str) -> None:
        symbol = symbol.strip().upper()
        with self._lock:
            self._subscribed_symbols.discard(symbol)
            # Remove matching instrument tuples by reverse sec map
            drop_ids = {sid for sid, sym in self._sec_id_to_symbol.items() if sym == symbol}
            self._instrument_tuples = [
                t for t in self._instrument_tuples if str(t[1]) not in drop_ids
            ]
        ws = self._ws_feed
        if ws and hasattr(ws, "unsubscribe_symbols"):
            try:
                # Best-effort; SDK expects instrument tuples
                pass
            except Exception as e:
                logger.debug(f"[FEED] unsubscribe note: {e}")

    def subscribe_universe(self, symbols: list[str], segment: str) -> int:
        ok = 0
        for s in symbols:
            if self.subscribe_symbol(s, segment):
                ok += 1
        return ok

    def _push_subscribe(self, instruments: list) -> bool:
        """Invoke real MarketFeed.subscribe_symbols on the live socket."""
        ws = self._ws_feed
        if not ws or not instruments:
            return False
        try:
            method = getattr(ws, "subscribe_symbols", None)
            if method and callable(method):
                method(instruments)
                return True
            # Fallback: mutate instruments list for next reconnect
            if hasattr(ws, "instruments"):
                existing = set(tuple(x) for x in (ws.instruments or []))
                for inst in instruments:
                    existing.add(tuple(inst))
                ws.instruments = list(existing)
                return True
        except Exception as sub_err:
            logger.warning(f"[FEED] Socket subscribe failed: {sub_err}")
        return False

    def is_connected(self) -> bool:
        with self._lock:
            if not self.enabled:
                return False
            now = time.time()
            # Quote heartbeat preferred; recent frame also counts (status packets)
            if self._last_heartbeat > 0 and (now - self._last_heartbeat < 60.0):
                return True
            if self._last_recv_at > 0 and (now - self._last_recv_at < 90.0):
                return True
            return False

    def status_summary(self) -> dict:
        connected = self.is_connected()
        with self._lock:
            heartbeat_age = (
                round(time.time() - self._last_heartbeat, 1) if self._last_heartbeat > 0 else None
            )
            rate_left = (
                round(max(0.0, self._rate_limited_until - time.time()), 1)
                if self._rate_limited_until > time.time()
                else 0
            )
            return {
                "enabled": self.enabled,
                "connected": connected,
                "mode": (
                    "websocket_live"
                    if connected and self._last_heartbeat > 0
                    else (
                        "websocket_connected"
                        if connected
                        else (
                            "rate_limited"
                            if rate_left > 0
                            else (
                                "waiting_token"
                                if self._want_live and not self.access_token
                                else (
                                    "reconnect" if self.enabled else "rest_fallback"
                                )
                            )
                        )
                    )
                ),
                "has_access_token": bool(self.access_token),
                "rate_limited_for_sec": rate_left,
                "cached_symbols_count": len(self._quote_cache),
                "subscribed_count": len(self._subscribed_symbols),
                "instrument_tuples_count": len(self._instrument_tuples),
                "last_heartbeat_age_sec": heartbeat_age,
                "order_updates_received": len(self._order_updates),
                "reconnect_attempts": self._reconnect_attempts,
                "last_error": self._last_error or None,
                "us_feed_note": (
                    "US Global uses separate GlobalStocksFeed "
                    "(see dhan_us_live_feed / DHAN_US_LIVE_WEBSOCKET)"
                ),
            }

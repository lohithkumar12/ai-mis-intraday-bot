"""
dhan_us_live_feed.py — DhanHQ Global Stocks Live Feed (US equities)
===================================================================
Process-wide singleton wrapping dhanhq.GlobalStocksFeed (INX_EQ) for US LTP.

Separate from India MarketFeed (api-feed.dhan.co). Uses:
  wss://global-stocks-api-feed.dhan.co/

Requires dhanhq >= 2.3.0rc1 (GlobalStocksFeed). Same DHAN client id + access
token as India; credentials synced on broker login/token refresh.

Hardening (parity with India feed):
  - One socket per process
  - Exponential backoff; hard cool-down on HTTP 429 / connection-limit
  - is_connected() only after a recent real tick
  - No invented prices — cache only real Trade/OHLC packets
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Any

import config

logger = logging.getLogger(__name__)

# Wire sizes observed from global-stocks-api-feed (byte[9] = TOTAL packet length).
# SDK docs claimed Trade=37 / PrevClose=19; live feed sends Trade=27 / PrevClose=15.
_GS_PACKET_MIN_LEN = {
    1: 27,   # Trade (header 11 + LTP/qty/times)
    3: 27,   # OHLC
    29: 18,  # Market Status
    32: 15,  # Previous Close (header 11 + one float32)
    33: 19,  # Circuit Limit
    36: 19,  # 52 Week High/Low
}

_feed_instance: "DhanUSLiveFeedManager | None" = None
_feed_lock = threading.Lock()

_FEED_BACKOFF_START_SEC = 5.0
_FEED_BACKOFF_MAX_SEC = 300.0
_FEED_429_BACKOFF_SEC = 120.0
_FEED_429_BACKOFF_MAX_SEC = 600.0
_RECONNECT_DEBOUNCE_SEC = 45.0

# Seed from official US scrip master (SCRIP_CODE) — refreshed at runtime
_SEED_SEC_MAP = {
    "10000025": "AAPL",
    "10006754": "MSFT",
    "10004393": "GOOGL",
    "10000496": "AMZN",
    "10007290": "NVDA",
    "10006471": "META",
    "10010311": "TSLA",
    "10005648": "JPM",
    "10010679": "V",
    "10010532": "UNH",
}


def _parse_india_compatible_packet(first: int, data: bytes) -> dict | None:
    """Some Global gateways reuse India MarketFeed binary layouts (first byte = type)."""
    try:
        if first == 2 and len(data) >= 16:
            # Ticker: <BHBIfI
            _t, _len, exch, sec_id, ltp, ltt = struct.unpack("<BHBIfI", data[0:16])
            return {
                "type": "Trade",
                "exchange_segment": exch,
                "security_id": sec_id,
                "LTP": "{:.2f}".format(ltp),
                "LTT": str(ltt),
            }
        if first == 4 and len(data) >= 50:
            u = struct.unpack("<BHBIfHIfIIIffff", data[0:50])
            return {
                "type": "Trade",
                "exchange_segment": u[2],
                "security_id": u[3],
                "LTP": "{:.2f}".format(u[4]),
                "LTQ": u[5],
                "volume": u[8],
                "open": "{:.2f}".format(u[11]),
                "close": "{:.2f}".format(u[12]),
                "high": "{:.2f}".format(u[13]),
                "low": "{:.2f}".format(u[14]),
            }
        if first == 8 and len(data) >= 62:
            # Full packet — LTP at same offset as quote header
            u = struct.unpack("<BHBIfHIfIIII", data[0:42])
            return {
                "type": "Trade",
                "exchange_segment": u[2],
                "security_id": u[3],
                "LTP": "{:.2f}".format(u[4]),
                "LTQ": u[5],
                "volume": u[8],
            }
        if first == 6 and len(data) >= 16:
            _t, _len, exch, sec_id, prev_close, prev_oi = struct.unpack(
                "<BHBIfI", data[0:16]
            )
            return {
                "type": "Previous Close",
                "exchange_segment": exch,
                "security_id": sec_id,
                "prev_close": prev_close,
                "prev_OI": prev_oi,
            }
    except (struct.error, ValueError, TypeError):
        return None
    return None


def _parse_trade_partial(feed_self, packet: bytes) -> dict | None:
    """Trade parse for live 27-byte frames (SDK's 37-byte layout is wrong on wire)."""
    if len(packet) >= 37:
        try:
            return feed_self.process_trade(packet)
        except Exception:
            pass
    if len(packet) < 15:
        return None
    try:
        exch_seg, scrip_id = feed_self._parse_header(packet)
        ltp = struct.unpack("<f", packet[11:15])[0]
        if not (0 < ltp < 1_000_000):
            return None
        out = {
            "type": "Trade",
            "exchange_segment": exch_seg,
            "security_id": scrip_id,
            "LTP": "{:.2f}".format(ltp),
        }
        # Live Trade body after LTP appears as int32 fields (qty / epochs)
        if len(packet) >= 19:
            out["LTQ"] = struct.unpack("<i", packet[15:19])[0]
        if len(packet) >= 23:
            out["LTT"] = struct.unpack("<i", packet[19:23])[0]
        if len(packet) >= 27:
            out["LUT"] = struct.unpack("<i", packet[23:27])[0]
        return out
    except (struct.error, ValueError, TypeError):
        return None


def _parse_prev_close_partial(feed_self, packet: bytes) -> dict | None:
    """Previous Close is 15 bytes on wire (one float), not SDK's 19."""
    if len(packet) >= 19:
        try:
            return feed_self.process_prev_close(packet)
        except Exception:
            pass
    if len(packet) < 15:
        return None
    try:
        exch_seg, scrip_id = feed_self._parse_header(packet)
        prev_close = struct.unpack("<f", packet[11:15])[0]
        if not (0 < prev_close < 1_000_000):
            return None
        return {
            "type": "Previous Close",
            "exchange_segment": exch_seg,
            "security_id": scrip_id,
            "prev_close": prev_close,
            "prev_OI": 0,
        }
    except (struct.error, ValueError, TypeError):
        return None


def _harden_global_stocks_feed(feed_cls_or_inst) -> None:
    """
    Patch GlobalStocksFeed.process_data so short/malformed frames are skipped
    instead of crashing the recv loop (SDK bug: body-only msg_length).
    Also accept India-compatible first-byte packet layouts.
    """
    target = feed_cls_or_inst
    if getattr(target, "_ns_process_data_hardened", False):
        return

    def process_data_safe(self, data):
        if not data:
            return []
        if isinstance(data, str):
            try:
                data = data.encode("latin-1")
            except Exception:
                return []
        if isinstance(data, memoryview):
            data = data.tobytes()
        if not isinstance(data, (bytes, bytearray)):
            return []
        data = bytes(data)

        # Standalone error packet (MsgCode 50 at offset 0)
        if data[0] == 50:
            try:
                return self.process_error(data)
            except Exception as e:
                logger.debug(f"[US FEED] error packet parse: {e}")
                return {"type": "Error", "message": str(e)}

        # India-compatible layouts (first byte = packet type)
        if data[0] in (2, 4, 6, 8):
            india = _parse_india_compatible_packet(data[0], data)
            if india is not None:
                return india

        packets = []
        offset = 0
        total = len(data)
        while offset + 11 <= total:
            msg_length = int(data[offset + 9])
            msg_code = int(data[offset + 10])
            need = _GS_PACKET_MIN_LEN.get(msg_code, max(msg_length, 11))
            take = msg_length if msg_length >= need else need
            if take <= 0:
                break

            if offset + take > total:
                # Incomplete vs declared — try partial parsers on remainder
                rem = bytes(data[offset:total])
                if msg_code == 1:
                    partial = _parse_trade_partial(self, rem)
                    if partial is not None:
                        packets.append(partial)
                elif msg_code == 32:
                    partial = _parse_prev_close_partial(self, rem)
                    if partial is not None:
                        packets.append(partial)
                elif msg_code in _GS_PACKET_MIN_LEN and len(rem) >= 11:
                    try:
                        parsed = self.process_packet(msg_code, rem)
                        if parsed is not None:
                            packets.append(parsed)
                    except Exception:
                        pass
                break

            packet = bytes(data[offset : offset + take])
            try:
                if msg_code == 1 and len(packet) < 37:
                    parsed = _parse_trade_partial(self, packet)
                elif msg_code == 32 and len(packet) < 19:
                    parsed = _parse_prev_close_partial(self, packet)
                else:
                    parsed = self.process_packet(msg_code, packet)
                if parsed is not None:
                    packets.append(parsed)
            except (struct.error, ValueError, TypeError) as pe:
                if msg_code == 1:
                    partial = _parse_trade_partial(self, packet)
                    if partial is not None:
                        packets.append(partial)
                    else:
                        logger.debug(
                            f"[US FEED] skip packet code={msg_code} len={len(packet)} "
                            f"declared={msg_length}: {pe}"
                        )
                elif msg_code == 32:
                    partial = _parse_prev_close_partial(self, packet)
                    if partial is not None:
                        packets.append(partial)
                else:
                    logger.debug(
                        f"[US FEED] skip packet code={msg_code} len={len(packet)} "
                        f"declared={msg_length}: {pe}"
                    )
            offset += take
        if len(packets) == 1:
            return packets[0]
        return packets

    # Patch on the class so all instances benefit
    cls = target if isinstance(target, type) else type(target)
    cls.process_data = process_data_safe  # type: ignore[method-assign]
    cls._ns_process_data_hardened = True  # type: ignore[attr-defined]
    logger.info("[US FEED] Hardened GlobalStocksFeed.process_data (short-packet safe)")


def get_us_live_feed_manager() -> "DhanUSLiveFeedManager":
    global _feed_instance
    with _feed_lock:
        if _feed_instance is None:
            _feed_instance = DhanUSLiveFeedManager()
        return _feed_instance


def _import_global_stocks_feed():
    """Import GlobalStocksFeed; returns None if SDK too old."""
    try:
        from dhanhq import GlobalStocksFeed

        _harden_global_stocks_feed(GlobalStocksFeed)
        return GlobalStocksFeed
    except ImportError:
        try:
            from dhanhq.global_stocks_feed import GlobalStocksFeed

            _harden_global_stocks_feed(GlobalStocksFeed)
            return GlobalStocksFeed
        except ImportError:
            return None


class DhanUSLiveFeedManager:
    """Process-wide Dhan Global Stocks WebSocket feed for US equities."""

    def __init__(self):
        self.client_id = (config.DHAN_CLIENT_ID or "").strip()
        self.access_token = (config.DHAN_ACCESS_TOKEN or "").strip()
        self._want_live = bool(
            getattr(config, "DHAN_US_LIVE_WEBSOCKET", False) and self.client_id
        )
        self.enabled = bool(self._want_live and self.access_token)

        self._quote_cache: dict[str, dict[str, Any]] = {}
        self._subscribed_symbols: set[str] = set()
        self._instrument_tuples: list[tuple] = []
        self._sec_id_to_symbol: dict[str, str] = dict(_SEED_SEC_MAP)

        self._is_connected = False
        self._last_heartbeat = 0.0
        self._last_recv_at = 0.0
        self._diag_frames = 0
        self._lock = threading.Lock()
        self._stop = False
        self._run_error: BaseException | None = None

        self._ws_feed = None
        self._feed_thread = None
        self._reconnect_attempts = 0
        self._last_force_reconnect_at = 0.0
        self._rate_limited_until = 0.0
        self._connect_lock = threading.Lock()
        self._sdk_available = _import_global_stocks_feed() is not None
        self._last_error = ""

        if not self._sdk_available and self._want_live:
            logger.warning(
                "[US FEED] GlobalStocksFeed not in installed dhanhq — "
                "upgrade to dhanhq>=2.3.0rc1 (pip install --pre 'dhanhq>=2.3.0rc1')"
            )
        if self.enabled and self._sdk_available:
            logger.info(
                "[US FEED] Initializing Dhan GlobalStocksFeed (INX_EQ) manager..."
            )
            self._start_threads()
        elif self._want_live and self._sdk_available:
            logger.info(
                "[US FEED] Waiting for access token "
                "(broker PIN/TOTP login will connect Global Stocks WebSocket)."
            )
        elif not self._want_live:
            logger.info("[US FEED] DHAN_US_LIVE_WEBSOCKET disabled or missing client id.")

    def update_credentials(
        self,
        client_id: str,
        access_token: str,
        reconnect: bool = True,
    ) -> None:
        """Apply broker-refreshed Client ID + access token to the US live feed."""
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
            self._want_live = bool(
                getattr(config, "DHAN_US_LIVE_WEBSOCKET", False) and self.client_id
            )
            was_enabled = self.enabled
            self.enabled = bool(
                self._want_live and self.access_token and self._sdk_available
            )

        if not self.enabled:
            return

        threads_alive = (
            self._feed_thread is not None and self._feed_thread.is_alive()
        )
        if not was_enabled or not threads_alive:
            self._stop = False
            logger.info(
                "[US FEED] Connecting GlobalStocksFeed with refreshed access token..."
            )
            self._start_threads()
        elif reconnect and changed:
            if self.is_connected():
                logger.info(
                    "[US FEED] Access token updated — keeping live socket; "
                    "new token used on next reconnect"
                )
            else:
                self._force_reconnect(reason="token_updated_while_down")

    def _force_reconnect(self, reason: str = "manual") -> None:
        now = time.time()
        if now - self._last_force_reconnect_at < _RECONNECT_DEBOUNCE_SEC:
            logger.info(
                f"[US FEED] Skip force reconnect ({reason}) — debounced "
                f"<{_RECONNECT_DEBOUNCE_SEC:.0f}s"
            )
            return
        self._last_force_reconnect_at = now
        with self._lock:
            ws = self._ws_feed
            self._is_connected = False
        if ws is None:
            return
        logger.info(f"[US FEED] Force reconnect ({reason})")
        self._close_ws(ws)

    @staticmethod
    def _close_ws(ws) -> None:
        for method_name in ("close_connection", "disconnect", "close", "stop"):
            method = getattr(ws, method_name, None)
            if not callable(method):
                continue
            try:
                method()
                return
            except Exception as e:
                logger.debug(f"[US FEED] {method_name} during close: {e}")

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

    def _start_threads(self):
        if not self._sdk_available:
            return
        if self._feed_thread is not None and self._feed_thread.is_alive():
            return
        self._feed_thread = threading.Thread(
            target=self._market_feed_loop,
            daemon=True,
            name="DhanUSGlobalStocksFeed",
        )
        self._feed_thread.start()

    def _on_ticks_callback(self, _ws, data):
        """GlobalStocksFeed on_message / on_ticks — may be dict or list."""
        with self._lock:
            self._last_recv_at = time.time()
            self._diag_frames += 1
            frame_n = self._diag_frames

        if frame_n <= 8:
            summary = data
            if isinstance(data, list):
                summary = f"list[{len(data)}] types={[ (x.get('type') if isinstance(x, dict) else type(x).__name__) for x in data[:5] ]}"
            elif isinstance(data, dict):
                summary = (
                    f"type={data.get('type')} sec={data.get('security_id')} "
                    f"LTP={data.get('LTP') or data.get('close') or data.get('prev_close')}"
                )
            elif data in ([], "", None):
                summary = f"empty({data!r})"
            logger.info(f"[US FEED] frame#{frame_n}: {summary}")
        elif frame_n == 9:
            logger.info("[US FEED] Tick stream healthy — further frame dumps at DEBUG")

        if isinstance(data, list):
            for item in data:
                self._on_tick(item)
        else:
            self._on_tick(data)

    def _on_ws_error(self, _ws, err):
        self._run_error = err if isinstance(err, BaseException) else Exception(str(err))
        if self._is_rate_limit_error(err):
            with self._lock:
                self._rate_limited_until = max(
                    self._rate_limited_until,
                    time.time() + _FEED_429_BACKOFF_SEC,
                )
            logger.warning(f"[US FEED] WS error rate-limit: {err}")
            ws = self._ws_feed
            if ws is not None:
                self._close_ws(ws)
        else:
            logger.warning(f"[US FEED] WS error: {err}")

    def _on_tick(self, data):
        if not isinstance(data, dict):
            return

        # Feed-level error packets (auth / connection limit)
        if str(data.get("type") or "").lower() == "error":
            err_code = data.get("error_code")
            msg = data.get("message") or str(data)
            self._last_error = f"{err_code}: {msg}"
            logger.warning(f"[US FEED] Error packet: {self._last_error}")
            if err_code in (805, 806, 807, 808, 809) or self._is_rate_limit_error(msg):
                with self._lock:
                    self._rate_limited_until = max(
                        self._rate_limited_until,
                        time.time() + _FEED_429_BACKOFF_SEC,
                    )
                ws = self._ws_feed
                if ws is not None:
                    self._close_ws(ws)
            return

        sec_id = str(
            data.get("security_id")
            or data.get("securityId")
            or data.get("scrip_id")
            or ""
        )
        with self._lock:
            sym = data.get("symbol") or self._sec_id_to_symbol.get(sec_id) or ""
            # Any non-error packet proves the socket path works
            self._last_recv_at = time.time()
            self._is_connected = True

        if not sym:
            if sec_id and self._diag_frames <= 30:
                logger.info(f"[US FEED] tick for unmapped security_id={sec_id} keys={list(data.keys())}")
            return

        ltp_raw = (
            data.get("LTP")
            or data.get("last_price")
            or data.get("ltp")
            or data.get("close")
            or data.get("prev_close")
        )
        try:
            ltp = float(ltp_raw) if ltp_raw is not None else 0.0
        except (TypeError, ValueError):
            ltp = 0.0

        open_p = high = low = close = 0.0
        try:
            open_p = float(data.get("open") or 0.0)
            high = float(data.get("high") or 0.0)
            low = float(data.get("low") or 0.0)
            close = float(data.get("close") or data.get("prev_close") or 0.0)
        except (TypeError, ValueError):
            pass

        volume = 0
        try:
            volume = int(data.get("volume") or data.get("LTQ") or 0)
        except (TypeError, ValueError):
            volume = 0

        # OHLC / prev_close packets may not have LTP — use close
        if ltp <= 0 and close > 0:
            ltp = close
        if sym and ltp > 0:
            self.update_quote(
                symbol=sym,
                ltp=ltp,
                open_price=open_p,
                high=high,
                low=low,
                close=close or ltp,
                volume=volume,
            )

    def _snapshot_instruments(self) -> list:
        with self._lock:
            if self._instrument_tuples:
                return list(self._instrument_tuples)
        GlobalStocksFeed = _import_global_stocks_feed()
        if GlobalStocksFeed is None:
            return []
        # Minimal seed so socket can connect before universe subscribe
        return [(GlobalStocksFeed.INX_EQ, "10000025")]

    def _market_feed_loop(self):
        """Reconnect-with-backoff loop — one GlobalStocksFeed socket at a time."""
        backoff = _FEED_BACKOFF_START_SEC
        while not self._stop:
            with self._lock:
                cid = self.client_id
                tok = self.access_token
                rate_until = self._rate_limited_until
            if not (cid and tok):
                time.sleep(2.0)
                continue

            wait_rate = rate_until - time.time()
            if wait_rate > 0:
                logger.warning(
                    f"[US FEED] Rate-limited — waiting {wait_rate:.0f}s "
                    f"before next GlobalStocksFeed connect"
                )
                time.sleep(min(wait_rate, 30.0))
                continue

            if not self._connect_lock.acquire(blocking=False):
                time.sleep(2.0)
                continue

            had_ticks = False
            was_rate_limited = False
            try:
                from dhanhq import DhanContext

                GlobalStocksFeed = _import_global_stocks_feed()
                if GlobalStocksFeed is None:
                    self._last_error = "GlobalStocksFeed unavailable"
                    logger.error(f"[US FEED] {self._last_error}")
                    break

                ctx = DhanContext(cid, tok)
                instruments = self._snapshot_instruments()
                self._diag_frames = 0
                self._run_error = None
                self._ws_feed = GlobalStocksFeed(
                    ctx,
                    instruments=instruments,
                    auth_type=getattr(GlobalStocksFeed, "AUTH_SELF", 2),
                    on_ticks=self._on_ticks_callback,
                    on_error=self._on_ws_error,
                )
                # Log raw binary for first frames (diagnose layout mismatches)
                if hasattr(self._ws_feed, "get_instrument_data"):
                    _orig_get = self._ws_feed.get_instrument_data

                    async def _get_logged():
                        raw = await self._ws_feed.ws.recv()
                        with self._lock:
                            n = self._diag_frames
                        if n < 8:
                            raw_b = raw if isinstance(raw, (bytes, bytearray)) else str(raw)[:80]
                            if isinstance(raw_b, (bytes, bytearray)):
                                logger.info(
                                    f"[US FEED] raw#{n + 1} len={len(raw_b)} "
                                    f"hex={raw_b[:32].hex()} first={raw_b[0] if raw_b else None}"
                                )
                            else:
                                logger.info(f"[US FEED] raw#{n + 1} text={raw_b!r}")
                        self._ws_feed.data = self._ws_feed.process_data(raw)
                        return self._ws_feed.data

                    self._ws_feed.get_instrument_data = _get_logged  # type: ignore[method-assign]
                    _ = _orig_get  # keep reference silence

                logger.info(
                    f"[US FEED] GlobalStocksFeed connecting with "
                    f"{len(instruments)} instrument(s)..."
                )
                hb_before = self._last_heartbeat

                # Use run() so the asyncio loop stays alive (keepalive).
                # run_forever()+get_data() destroys keepalive tasks and drops the socket.
                if hasattr(self._ws_feed, "run"):
                    logger.info("[US FEED] GlobalStocksFeed run() — receiving ticks")
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
                                f"[US FEED] GlobalStocksFeed recv ended: {recv_err}"
                            )
                            break
                else:
                    logger.error("[US FEED] GlobalStocksFeed has no run/run_forever")
                    break

                if self._run_error and self._is_rate_limit_error(self._run_error):
                    was_rate_limited = True
                    raise self._run_error

                had_ticks = self._last_heartbeat > hb_before
                if had_ticks:
                    backoff = _FEED_BACKOFF_START_SEC
                    self._reconnect_attempts = 0
                logger.warning("[US FEED] GlobalStocksFeed disconnected — scheduling reconnect")
            except Exception as wse:
                self._last_error = str(wse)
                if self._is_rate_limit_error(wse):
                    was_rate_limited = True
                    backoff = max(backoff, _FEED_429_BACKOFF_SEC)
                    backoff = min(backoff, _FEED_429_BACKOFF_MAX_SEC)
                    with self._lock:
                        self._rate_limited_until = time.time() + backoff
                    logger.warning(
                        f"[US FEED] GlobalStocksFeed HTTP 429 / connection limit — "
                        f"cool-down {backoff:.0f}s (do not open another socket)"
                    )
                else:
                    logger.warning(
                        f"[US FEED] GlobalStocksFeed error ({wse}) — "
                        f"reconnect in {backoff:.0f}s"
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
                "exchange": "GLOBAL",
            }
            self._last_heartbeat = time.time()
            self._is_connected = True

    def get_live_quote(self, symbol: str) -> dict[str, Any] | None:
        """Cached WebSocket quote if fresh (< 30 sec)."""
        symbol = (symbol or "").strip().upper()
        with self._lock:
            cached = self._quote_cache.get(symbol)
            if cached and (time.time() - cached["timestamp"] < 30.0):
                return dict(cached)
            return None

    def _build_instrument_tuple(self, sec_id: str) -> list[tuple]:
        """Trade + OHLC subscriptions for one SCRIP_CODE."""
        GlobalStocksFeed = _import_global_stocks_feed()
        if GlobalStocksFeed is None:
            return []
        trade = getattr(GlobalStocksFeed, "SubscribeTrade", 15)
        ohlc = getattr(GlobalStocksFeed, "SubscribeOHLC", 17)
        return [
            (GlobalStocksFeed.INX_EQ, str(sec_id), trade),
            (GlobalStocksFeed.INX_EQ, str(sec_id), ohlc),
        ]

    def subscribe_symbol(self, symbol: str) -> bool:
        """Subscribe a US ticker using SCRIP_CODE from us_instruments master."""
        symbol = (symbol or "").strip().upper()
        with self._lock:
            if symbol in self._subscribed_symbols:
                return True

        try:
            from us_instruments import get_us_security_id

            sec_id = get_us_security_id(symbol)
            if not sec_id:
                logger.warning(f"[US FEED] Skip subscribe {symbol}: no security_id")
                with self._lock:
                    self._subscribed_symbols.add(symbol)
                return False

            instrs = self._build_instrument_tuple(sec_id)
            if not instrs:
                return False

            with self._lock:
                self._subscribed_symbols.add(symbol)
                self._sec_id_to_symbol[str(sec_id)] = symbol
                for instr in instrs:
                    if instr not in self._instrument_tuples:
                        self._instrument_tuples.append(instr)

            pushed = self._push_subscribe(instrs)
            logger.info(
                f"[US FEED] Subscribed {symbol} (INX_EQ:{sec_id}) socket_push={pushed}"
            )
            return True
        except Exception as e:
            logger.warning(f"[US FEED] Dynamic subscribe warning for {symbol}: {e}")
            with self._lock:
                self._subscribed_symbols.add(symbol)
            return False

    def subscribe_universe(self, symbols: list[str]) -> int:
        ok = 0
        for s in symbols:
            if self.subscribe_symbol(s):
                ok += 1
        return ok

    def _push_subscribe(self, instruments: list) -> bool:
        ws = self._ws_feed
        if not ws or not instruments:
            return False
        try:
            method = getattr(ws, "subscribe_symbols", None)
            if method and callable(method):
                method(instruments)
                return True
            if hasattr(ws, "instruments"):
                existing = set(tuple(x) for x in (ws.instruments or []))
                for inst in instruments:
                    existing.add(tuple(inst))
                ws.instruments = list(existing)
                return True
        except Exception as sub_err:
            logger.warning(f"[US FEED] Socket subscribe failed: {sub_err}")
        return False

    def is_connected(self) -> bool:
        with self._lock:
            if not self.enabled:
                return False
            # Quote heartbeat OR recent raw frame (socket alive even before LTP map)
            now = time.time()
            if self._last_heartbeat > 0 and (now - self._last_heartbeat < 60.0):
                return True
            if self._last_recv_at > 0 and (now - self._last_recv_at < 90.0):
                return True
            return False

    def status_summary(self) -> dict:
        connected = self.is_connected()
        with self._lock:
            heartbeat_age = (
                round(time.time() - self._last_heartbeat, 1)
                if self._last_heartbeat > 0
                else None
            )
            return {
                "enabled": self.enabled,
                "want_live": self._want_live,
                "sdk_available": self._sdk_available,
                "connected": connected,
                "mode": (
                    "websocket_live"
                    if connected and self._last_heartbeat > 0
                    else (
                        "websocket_connected"
                        if connected
                        else (
                            "sdk_missing"
                            if self._want_live and not self._sdk_available
                            else (
                                "waiting_token"
                                if self._want_live and not self.access_token
                                else (
                                    "reconnect"
                                    if self.enabled
                                    else "rest_yahoo_fallback"
                                )
                            )
                        )
                    )
                ),
                "has_access_token": bool(self.access_token),
                "rate_limited_for_sec": (
                    round(max(0.0, self._rate_limited_until - time.time()), 1)
                    if self._rate_limited_until > time.time()
                    else 0
                ),
                "cached_symbols_count": len(self._quote_cache),
                "subscribed_count": len(self._subscribed_symbols),
                "instrument_tuples_count": len(self._instrument_tuples),
                "last_heartbeat_age_sec": heartbeat_age,
                "reconnect_attempts": self._reconnect_attempts,
                "last_error": self._last_error or None,
                "feed": "GlobalStocksFeed",
                "segment": "INX_EQ",
            }

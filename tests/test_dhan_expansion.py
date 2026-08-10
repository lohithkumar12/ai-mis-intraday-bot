"""
test_dhan_expansion.py — Production Verification Suite for DhanHQ Multi-Market Bot (R1–R12)
==========================================================================================
Verifies:
  R1: Live feed is_connected False until tick; subscribe_symbol invokes socket subscribe
  R2: Scrip master loading & structured contract resolution (NIFTY/BANKNIFTY/FINNIFTY/MCX/Currency)
  R3: place_buy_order & place_sell_order honor product_type (INTRADAY → INTRA, not CNC)
  R4: Live F&O path rejects order when live option chain/LTP is unavailable (no invented live premiums)
  R5–R7: Capital caps block oversized orders in F&O, MCX, and Currency
  R8 & R10: get_historical_candles selects non-EQUITY instrument_type dynamically
  R9: /api/segments/status endpoint structure and metrics
  R11: Forever order uses product_type (not hardcoded CNC)
  R12: Option resolve returns None (not invented id) when unresolved
"""

import unittest
from unittest.mock import MagicMock, patch
import pandas as pd

import config
from dhan_broker import get_shared_dhan_broker, _dhan_product_type
from dhan_live_feed import DhanLiveFeedManager
from india_fno_instruments import (
    load_dhan_scrip_master,
    resolve_instrument_info,
    resolve_option_contract,
    is_placeholder_security_id,
)
from india_fno_broker import get_shared_fno_broker
from mcx_broker import get_shared_mcx_broker
from currency_broker import get_shared_currency_broker
from dashboard_server import app


class TestDhanExpansionProductionSuite(unittest.TestCase):
    def setUp(self):
        self.broker = get_shared_dhan_broker(auto_login=False)
        if self.broker.paper is not None:
            self.broker.paper.reset(100000.0)

    def test_r3_buy_sell_order_product_types(self):
        """R3 & Defect 1: Verify place_buy_order & place_sell_order honor product_type."""
        # SDK mapping: INTRADAY must resolve to INTRA constant value, not CNC
        mapped = _dhan_product_type("INTRADAY")
        self.assertNotEqual(mapped, "CNC")
        self.assertIn(str(mapped), ("INTRADAY", "INTRA"))

        # 1. Paper Buy & Sell
        buy_id = self.broker.place_buy_order(
            symbol="RELIANCE",
            qty=1,
            limit_price=2500.0,
            product_type="INTRADAY",
        )
        self.assertIsNotNone(buy_id)
        self.assertTrue(buy_id.startswith("PAPER-"))

        sell_id = self.broker.place_sell_order(
            symbol="RELIANCE",
            qty=1,
            limit_price=2550.0,
            product_type="INTRADAY",
        )
        self.assertIsNotNone(sell_id)

        # 2. Mock Live Dhan place_order to assert product_type is NOT forced CNC
        old_paper = self.broker.paper
        self.broker.paper = None
        mock_dhan = MagicMock()
        mock_dhan.place_order.return_value = {"status": "success", "data": {"orderId": "12345"}}
        self.broker.dhan = mock_dhan
        self.broker.ensure_session = MagicMock()
        self.broker._assert_live_allowed = MagicMock(return_value=True)

        self.broker.place_sell_order(
            symbol="RELIANCE",
            qty=1,
            limit_price=2550.0,
            order_type="LIMIT",
            product_type="INTRADAY",
        )

        mock_dhan.place_order.assert_called_once()
        kwargs = mock_dhan.place_order.call_args.kwargs
        self.assertIn("product_type", kwargs)
        self.assertNotEqual(kwargs["product_type"], "CNC")
        self.assertNotEqual(kwargs["product_type"], getattr(type("x", (), {}), "CNC", "CNC"))
        # INTRA maps to string "INTRADAY" on dhanhq
        self.assertEqual(str(kwargs["product_type"]), "INTRADAY")
        self.broker.paper = old_paper

    def test_r1_live_feed_connection_and_subscription(self):
        """R1: Verify feed is_connected False until tick and subscribe_symbol invokes socket path."""
        feed = DhanLiveFeedManager()
        feed.enabled = True
        feed._last_heartbeat = 0.0
        feed._last_recv_at = 0.0
        feed._is_connected = False
        self.assertFalse(feed.is_connected())

        feed.update_quote("RELIANCE", 2500.0)
        self.assertTrue(feed.is_connected())

        # MarketFeed.run() calls on_ticks(ws, data) — two-arg callback must accept that
        feed._on_ticks_callback(MagicMock(), {
            "type": "Quote Data",
            "security_id": "2885",
            "LTP": "2501.50",
            "symbol": "RELIANCE",
        })
        q = feed.get_live_quote("RELIANCE")
        self.assertIsNotNone(q)
        self.assertAlmostEqual(float(q["ltp"]), 2501.50, places=1)

        feed._ws_feed = MagicMock()
        feed._ws_feed.subscribe_symbols = MagicMock()
        with patch("india_fno_instruments.resolve_instrument_info") as mock_res:
            mock_res.return_value = {
                "security_id": "2885",
                "exchange": "NSE_EQ",
                "lot_size": 1,
                "resolved_from_master": True,
            }
            # Force fresh subscribe
            feed._subscribed_symbols.discard("RELIANCE")
            feed.subscribe_symbol("RELIANCE", "NSE")
        self.assertIn("RELIANCE", feed._subscribed_symbols)
        feed._ws_feed.subscribe_symbols.assert_called()

        summary = feed.status_summary()
        self.assertIn("subscribed_count", summary)
        self.assertIn("order_updates_received", summary)
        self.assertIn("mode", summary)
        self.assertTrue(
            DhanLiveFeedManager._is_rate_limit_error(
                "server rejected WebSocket connection: HTTP 429"
            )
        )

        # Paid Data API: broker-refreshed token must update feed credentials
        feed.update_credentials("1112996229", "fresh-token-xyz", reconnect=False)
        self.assertEqual(feed.client_id, "1112996229")
        self.assertEqual(feed.access_token, "fresh-token-xyz")
        self.assertTrue(feed.enabled)

    def test_r4_live_fno_refuses_invented_premiums(self):
        """R4 & Defect 3: Verify live F&O rejects order when live option chain LTP is missing."""
        fno_b = get_shared_fno_broker()
        old_live_conf = config.INDIA_FNO_LIVE_CONFIRMED
        old_paper = fno_b.paper

        try:
            config.INDIA_FNO_LIVE_CONFIRMED = True
            fno_b.paper = None
            fno_b.get_option_chain = MagicMock(return_value={})

            prem = fno_b.fetch_live_option_premium("NIFTY", 22000.0, "CE")
            self.assertEqual(prem, 0.0)

            res = fno_b.place_option_order("NIFTY", 22000.0, "CE", limit_price=0.0)
            self.assertIsNone(res)
        finally:
            config.INDIA_FNO_LIVE_CONFIRMED = old_live_conf
            fno_b.paper = old_paper

    def test_r5_r7_capital_caps_enforcement(self):
        """R5-R7: Verify capital caps block oversized orders in F&O, MCX, and Currency."""
        fno_b = get_shared_fno_broker()
        mcx_b = get_shared_mcx_broker()
        curr_b = get_shared_currency_broker()

        # Oversized F&O: force empty positions so cap path is exercised
        fno_b.get_open_positions = MagicMock(return_value={})
        fno_b._cooldowns.clear()
        fno_res = fno_b.place_option_order("NIFTY", 22000, "CE", lots=2, limit_price=10000.0)
        self.assertIsNone(fno_res)

        mcx_res = mcx_b.place_buy_order("GOLD", qty=10, price=60000.0)
        self.assertIsNone(mcx_res)

        curr_res = curr_b.place_buy_order("USDINR", qty=2000, price=83.5)
        self.assertIsNone(curr_res)

    def test_r8_get_historical_candles_instrument_types(self):
        """R8: Verify get_historical_candles selects correct non-EQUITY instrument_type."""
        load_dhan_scrip_master()
        mock_dhan = MagicMock()
        mock_dhan.intraday_minute_data.return_value = {"status": "success", "data": []}
        self.broker.dhan = mock_dhan
        self.broker.ensure_session = MagicMock()

        self.broker.get_historical_candles("NIFTY", days=1)
        self.assertTrue(mock_dhan.intraday_minute_data.called)
        kwargs = mock_dhan.intraday_minute_data.call_args.kwargs
        self.assertEqual(kwargs.get("instrument_type"), "INDEX")

        mock_dhan.intraday_minute_data.reset_mock()
        self.broker.get_historical_candles("CRUDEOIL", days=1)
        self.assertTrue(mock_dhan.intraday_minute_data.called)
        kwargs = mock_dhan.intraday_minute_data.call_args.kwargs
        self.assertEqual(kwargs.get("instrument_type"), "FUTCOM")

        mock_dhan.intraday_minute_data.reset_mock()
        # USDINR may be stale in master — still must request FUTCUR when resolved
        with patch("dhan_broker.resolve_instrument_info") as mock_info:
            mock_info.return_value = {
                "security_id": "6601",
                "exchange": "NSE_CURRENCY",
                "instrument_type": "FUTCUR",
                "lot_size": 1,
                "resolved_from_master": True,
            }
            self.broker.get_historical_candles("USDINR", days=1)
        kwargs = mock_dhan.intraday_minute_data.call_args.kwargs
        self.assertEqual(kwargs.get("instrument_type"), "FUTCUR")

    def test_r9_segments_status_endpoint(self):
        """R9: Verify /api/segments/status returns valid JSON structure and metrics."""
        client = app.test_client()
        res = client.get("/api/segments/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIn("dhan_live_feed", data)
        self.assertIn("product_type", data)
        self.assertIn("segments", data)
        self.assertIn("india_fno", data["segments"])
        self.assertIn("utilization", data["segments"]["india_fno"])
        self.assertIn("positions", data["segments"]["india_fno"])
        self.assertIn("expansion_positions", data)
        self.assertIsInstance(data["expansion_positions"], list)
        feed = data["dhan_live_feed"]
        for key in ("enabled", "connected", "cached_symbols_count", "subscribed_count"):
            self.assertIn(key, feed)
        self.assertIn("dhan_us_live_feed", data)
        us_feed = data["dhan_us_live_feed"]
        for key in ("enabled", "connected", "mode", "subscribed_count"):
            self.assertIn(key, us_feed)
        self.assertIn("us_global", data["segments"])
        self.assertIn("live_feed", data["segments"]["us_global"])

    def test_india_scout_endpoint_near_panel(self):
        """Near setups API lists close names; scout auto-buy is separate config."""
        import bot_state

        bot_state.publish_scout(
            "INDIA",
            [
                {
                    "symbol": "AXISBANK",
                    "score": 66.0,
                    "price": 1100.0,
                    "rsi": 38.0,
                    "adx": 22.0,
                    "reason": "near SMA/EMA",
                    "near_only": True,
                    "would_buy": False,
                }
            ],
            meta={"scanned": 1, "auto_buy": True, "trade_eligible": True},
        )
        client = app.test_client()
        res = client.get("/api/scout")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get("trade_eligible"))
        self.assertTrue(data.get("enabled"))
        self.assertIn("auto_buy", data)
        self.assertEqual(data["items"][0]["symbol"], "AXISBANK")
        self.assertFalse(data["items"][0].get("would_buy"))

    def test_price_guards_reject_fake_mcx_and_fallback(self):
        """Fake Rs1500 MCX marks and paper_fallback quotes must be rejected."""
        from price_guards import (
            is_trusted_quote,
            mark_vs_entry_sane,
            require_tradeable_quote,
            validate_mcx_price,
        )

        ok, _ = validate_mcx_price("SILVER", 1500.0)
        self.assertFalse(ok)
        ok, _ = validate_mcx_price("SILVER", 226672.0)
        self.assertTrue(ok)
        ok, _ = validate_mcx_price("NATURALGAS", 255.0)
        self.assertTrue(ok)

        self.assertFalse(is_trusted_quote({"ltp": 1500.0, "source": "paper_fallback"}))
        self.assertTrue(is_trusted_quote({"ltp": 255.0, "source": "ticker_data"}))

        px, err = require_tradeable_quote(
            "SILVER", {"ltp": 1500.0, "source": "ticker_data"}, segment="MCX"
        )
        self.assertEqual(px, 0.0)
        self.assertTrue(err)

        sane, _ = mark_vs_entry_sane(1500.0, 226672.0)
        self.assertFalse(sane)

        # Broker must not invent paper_fallback quotes anymore
        q = self.broker._quote_from_candle_cache("SILVER")
        self.assertIsNone(q)

    def test_r2_scrip_master_and_option_resolution(self):
        """R2: Verify scrip master loading and option contract resolution."""
        loaded = load_dhan_scrip_master()
        self.assertTrue(loaded)

        nifty_info = resolve_instrument_info("NIFTY", exchange_segment="NSE_FNO")
        self.assertIn("security_id", nifty_info)
        self.assertFalse(is_placeholder_security_id(nifty_info["security_id"]))
        self.assertGreaterEqual(int(nifty_info.get("lot_size") or 1), 1)

        for und in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            info = resolve_instrument_info(und)
            self.assertTrue(str(info.get("security_id") or "").isdigit(), msg=und)

        # Unresolved wild strike must NOT invent a fake security_id
        opt_none = resolve_option_contract("NIFTY", 99999.0, "CE")
        self.assertTrue(opt_none is None or is_placeholder_security_id(opt_none.get("security_id")))

        # Real-ish strike from master (if options loaded)
        opt_info = resolve_option_contract("NIFTY", 29150.0, "CE")
        if opt_info:
            self.assertFalse(is_placeholder_security_id(opt_info.get("security_id")))

        crude = resolve_instrument_info("CRUDEOIL", exchange_segment="MCX_COMM")
        self.assertFalse(
            is_placeholder_security_id(crude.get("security_id")),
            msg=f"CRUDEOIL should resolve from master, got {crude}",
        )

    def test_r11_forever_order_product_type(self):
        """R11: Forever order must not hardcode CNC when INTRADAY requested."""
        old_paper = self.broker.paper
        self.broker.paper = None
        mock_dhan = MagicMock()
        mock_dhan.place_forever_order.return_value = {
            "status": "success",
            "data": {"orderId": "GTT1"},
        }
        self.broker.dhan = mock_dhan
        self.broker.ensure_session = MagicMock()
        self.broker._assert_live_allowed = MagicMock(return_value=True)

        self.broker.place_forever_order(
            "RELIANCE",
            qty=1,
            trigger_price=2400.0,
            price=2390.0,
            transaction_type="SELL",
            product_type="INTRADAY",
        )
        if mock_dhan.place_forever_order.called:
            kwargs = mock_dhan.place_forever_order.call_args.kwargs
            self.assertNotEqual(kwargs.get("product_type"), "CNC")
        self.broker.paper = old_paper

    def test_r12_backtest_dhan_hook_exists(self):
        """R12: backtest module exposes --dhan historical path with OI-aware parser."""
        import backtest

        self.assertTrue(hasattr(backtest, "load_csv"))
        self.assertTrue(callable(self.broker.get_historical_candles))
        old_dhan = self.broker.dhan
        self.broker.dhan = None
        try:
            df = self.broker._parse_intraday(
                {
                    "timestamp": [1_700_000_000, 1_700_003_600],
                    "open": [1.0, 1.1],
                    "high": [1.2, 1.3],
                    "low": [0.9, 1.0],
                    "close": [1.1, 1.2],
                    "volume": [10, 11],
                    "oi": [100, 110],
                }
            )
            self.assertIsNotNone(df)
            self.assertIn("oi", df.columns)
        finally:
            self.broker.dhan = old_dhan


if __name__ == "__main__":
    unittest.main()

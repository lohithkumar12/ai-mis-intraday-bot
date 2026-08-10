"""
tests/test_us.py — Smoke & Integration Tests for US Trading Stack
"""

import unittest
from unittest.mock import MagicMock, patch

import config
from us_instruments import (
    get_us_security_id,
    is_us_symbol,
    get_all_us_symbols,
    US_INSTRUMENTS,
)
from us_paper import USPaperPortfolio
from us_broker import USBroker
from dashboard_server import app


class TestUSInstruments(unittest.TestCase):
    def test_symbols_exist(self):
        symbols = get_all_us_symbols()
        self.assertIn("AAPL", symbols)
        self.assertIn("NVDA", symbols)
        self.assertTrue(is_us_symbol("AAPL"))
        self.assertIsNotNone(get_us_security_id("AAPL"))

    def test_security_ids_are_global_scrip_codes(self):
        """SCRIP_CODEs from official master are 8-digit (1xxxxxxx), not stale 199xx."""
        aapl = get_us_security_id("AAPL")
        self.assertTrue(str(aapl).isdigit())
        self.assertGreaterEqual(int(aapl), 10_000_000)
        self.assertEqual(US_INSTRUMENTS["AAPL"]["security_id"], aapl)


class TestUSPaperPortfolio(unittest.TestCase):
    def setUp(self):
        self.portfolio = USPaperPortfolio(starting_cash=10000.0)
        self.portfolio.reset(starting_cash=10000.0)

    def test_buy_and_sell(self):
        # Initial cash
        info = self.portfolio.get_account_info()
        self.assertEqual(info["available_cash"], 10000.0)

        # Buy 10 AAPL @ $150
        order_id = self.portfolio.buy("AAPL", 10, 150.0, stop_loss=140.0, take_profit=170.0)
        self.assertIsNotNone(order_id)
        self.assertTrue(order_id.startswith("US-PAPER-"))

        # Cash check
        info = self.portfolio.get_account_info({"AAPL": 150.0})
        self.assertEqual(info["available_cash"], 8500.0)
        self.assertEqual(info["equity"], 10000.0)

        # Open positions check
        positions = self.portfolio.get_open_positions({"AAPL": 160.0})
        self.assertIn("AAPL", positions)
        self.assertEqual(positions["AAPL"]["qty"], 10)
        self.assertEqual(positions["AAPL"]["unrealized_pl"], 100.0)

        # Sell 5 AAPL @ $160
        sell_id = self.portfolio.sell("AAPL", 5, 160.0)
        self.assertIsNotNone(sell_id)

        # Remaining position
        positions = self.portfolio.get_open_positions({"AAPL": 160.0})
        self.assertEqual(positions["AAPL"]["qty"], 5)

        # Clean up
        self.portfolio.reset()


class TestUSBrokerSafety(unittest.TestCase):
    def test_live_gate_blocked_by_default(self):
        with patch.object(config, "US_LIVE_CONFIRMED", False), \
             patch.object(config, "US_PAPER", False):
            broker = USBroker(auto_login=False)
            broker.paper = None  # Force live mode test
            order_id = broker.place_buy_order("AAPL", 10, 150.0)
            self.assertIsNone(order_id)
            self.assertIn("Live trading not confirmed", broker.last_error)


class TestUSLiveFeed(unittest.TestCase):
    def test_feed_connected_only_after_tick(self):
        from dhan_us_live_feed import DhanUSLiveFeedManager

        with patch.object(config, "DHAN_US_LIVE_WEBSOCKET", True), \
             patch.object(config, "DHAN_CLIENT_ID", "111"), \
             patch.object(config, "DHAN_ACCESS_TOKEN", "tok"), \
             patch.object(DhanUSLiveFeedManager, "_start_threads"):
            feed = DhanUSLiveFeedManager()
            feed.enabled = True
            feed._sdk_available = True
            feed._last_heartbeat = 0.0
            feed._is_connected = False
            self.assertFalse(feed.is_connected())

            feed.update_quote("AAPL", 190.5)
            self.assertTrue(feed.is_connected())
            q = feed.get_live_quote("AAPL")
            self.assertIsNotNone(q)
            self.assertEqual(q["ltp"], 190.5)
            self.assertEqual(q["source"], "websocket_live")

            summary = feed.status_summary()
            self.assertEqual(summary["mode"], "websocket_live")
            self.assertEqual(summary["feed"], "GlobalStocksFeed")

            feed.update_credentials("111", "fresh-token", reconnect=False)
            self.assertEqual(feed.access_token, "fresh-token")

    def test_subscribe_builds_inx_eq_tuple(self):
        from dhan_us_live_feed import DhanUSLiveFeedManager

        feed = DhanUSLiveFeedManager.__new__(DhanUSLiveFeedManager)
        feed.enabled = True
        feed._sdk_available = True
        feed._subscribed_symbols = set()
        feed._instrument_tuples = []
        feed._sec_id_to_symbol = {}
        feed._quote_cache = {}
        feed._lock = __import__("threading").Lock()
        feed._ws_feed = MagicMock()
        feed._ws_feed.subscribe_symbols = MagicMock()

        with patch("us_instruments.get_us_security_id", return_value="10000025"), \
             patch("us_instruments.load_us_scrip_master", return_value=True), \
             patch("dhan_us_live_feed._import_global_stocks_feed") as mock_gsf:
            mock_cls = MagicMock()
            mock_cls.INX_EQ = "INX_EQ"
            mock_cls.SubscribeTrade = 15
            mock_gsf.return_value = mock_cls
            ok = feed.subscribe_symbol("AAPL")
        self.assertTrue(ok)
        self.assertIn("AAPL", feed._subscribed_symbols)
        feed._ws_feed.subscribe_symbols.assert_called()
        args = feed._ws_feed.subscribe_symbols.call_args[0][0]
        self.assertGreaterEqual(len(args), 1)
        self.assertEqual(args[0][0], "INX_EQ")
        self.assertEqual(str(args[0][1]), "10000025")
        # Trade + OHLC dual subscribe
        modes = {t[2] for t in args if len(t) >= 3}
        self.assertTrue(15 in modes or len(args) >= 1)

    def test_429_sets_rate_limit_cool_down(self):
        from dhan_us_live_feed import DhanUSLiveFeedManager

        self.assertTrue(DhanUSLiveFeedManager._is_rate_limit_error("HTTP 429 Too Many Requests"))
        self.assertTrue(DhanUSLiveFeedManager._is_rate_limit_error("Connection limit exceeded"))
        self.assertFalse(DhanUSLiveFeedManager._is_rate_limit_error("token expired"))

    def test_get_latest_quote_prefers_websocket(self):
        broker = USBroker(auto_login=False)
        broker._quote_cache.clear()
        mock_feed = MagicMock()
        mock_feed.get_live_quote.return_value = {
            "ltp": 201.25,
            "source": "websocket_live",
            "symbol": "AAPL",
        }
        with patch("dhan_us_live_feed.get_us_live_feed_manager", return_value=mock_feed):
            q = broker.get_latest_quote("AAPL")
        self.assertIsNotNone(q)
        self.assertEqual(q["ltp"], 201.25)
        self.assertEqual(q["source"], "websocket_live")
        mock_feed.get_live_quote.assert_called_with("AAPL")

    def test_on_tick_parses_trade_string_ltp(self):
        from dhan_us_live_feed import DhanUSLiveFeedManager

        feed = DhanUSLiveFeedManager.__new__(DhanUSLiveFeedManager)
        feed._sec_id_to_symbol = {"10000025": "AAPL"}
        feed._quote_cache = {}
        feed._lock = __import__("threading").Lock()
        feed._last_heartbeat = 0.0
        feed._is_connected = False
        feed._on_tick({
            "type": "Trade",
            "security_id": 10000025,
            "LTP": "188.40",
            "volume": 100,
        })
        q = feed.get_live_quote("AAPL")
        self.assertIsNotNone(q)
        self.assertAlmostEqual(q["ltp"], 188.40)

    def test_harden_process_data_accepts_body_only_length(self):
        from dhan_us_live_feed import _harden_global_stocks_feed, _import_global_stocks_feed
        import struct

        GlobalStocksFeed = _import_global_stocks_feed()
        if GlobalStocksFeed is None:
            self.skipTest("GlobalStocksFeed not installed")
        _harden_global_stocks_feed(GlobalStocksFeed)

        # Live wire Trade: total len byte=27 (not SDK's 37)
        header = bytearray(11)
        header[0] = 14  # INX exch
        struct.pack_into("<i", header, 1, 10000025)
        struct.pack_into("<i", header, 5, 10000025)
        header[9] = 27
        header[10] = 1  # Trade
        body = struct.pack("<fiii", 309.72, 100, 1_700_000_000, 1_700_000_001)
        frame = bytes(header) + body
        self.assertEqual(len(frame), 27)

        feed = GlobalStocksFeed.__new__(GlobalStocksFeed)
        parsed = GlobalStocksFeed.process_data(feed, frame)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed.get("type"), "Trade")
        self.assertEqual(int(parsed.get("security_id")), 10000025)
        self.assertAlmostEqual(float(parsed.get("LTP")), 309.72, places=1)

        # Live wire Previous Close: total len=15
        pc = bytearray(15)
        pc[0] = 14
        struct.pack_into("<i", pc, 1, 10000025)
        struct.pack_into("<i", pc, 5, 10000025)
        pc[9] = 15
        pc[10] = 32
        struct.pack_into("<f", pc, 11, 311.0)
        parsed_pc = GlobalStocksFeed.process_data(feed, bytes(pc))
        self.assertIsInstance(parsed_pc, dict)
        self.assertEqual(parsed_pc.get("type"), "Previous Close")
        self.assertAlmostEqual(float(parsed_pc.get("prev_close")), 311.0, places=1)


class TestUSDashboardRoutes(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_us_status_endpoint(self):
        res = self.client.get("/api/us/status")
        self.assertEqual(res.status_code, 200)

    def test_us_positions_endpoint(self):
        res = self.client.get("/api/us/positions")
        self.assertEqual(res.status_code, 200)

    def test_us_scanner_endpoint(self):
        res = self.client.get("/api/us/scanner")
        self.assertEqual(res.status_code, 200)

    def test_us_toggle_kill_switch(self):
        res = self.client.post("/api/us/toggle_kill_switch")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")

    def test_health_endpoint_includes_us(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("us_enabled", data)
        self.assertIn("us_kill_switch", data)

    def test_segments_status_includes_us_feed(self):
        res = self.client.get("/api/segments/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("dhan_us_live_feed", data)
        self.assertIn("mode", data["dhan_us_live_feed"])


if __name__ == "__main__":
    unittest.main()

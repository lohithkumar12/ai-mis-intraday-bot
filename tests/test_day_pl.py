"""Today's P&L KPI = realized (closed today) + unrealized (open MTM)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import trade_journal
from dashboard_server import _broker_style_day_pl
from dhan_broker import DhanBroker


class TestBrokerStyleDayPl(unittest.TestCase):
    def test_unrealized_from_marks(self):
        broker = MagicMock(spec=["get_open_positions"])
        broker.get_open_positions.return_value = {
            "DIVISLAB": {
                "qty": 2,
                "avg_entry_price": 8280.0,
                "current_price": 8287.0,
                "unrealized_pl": 0.0,
            }
        }
        with patch.object(trade_journal, "realized_pnl_today", return_value=0.0):
            day_pl, day_pct, detail = _broker_style_day_pl("INDIA", broker, 125_000.0)
        self.assertAlmostEqual(day_pl, 14.0)
        self.assertAlmostEqual(detail["unrealized"], 14.0)
        self.assertAlmostEqual(day_pct, 14.0 / (2 * 8280.0) * 100)

    def test_kpi_includes_journal_realized(self):
        broker = MagicMock(spec=["get_open_positions"])
        broker.get_open_positions.return_value = {
            "DIVISLAB": {
                "qty": 1,
                "avg_entry_price": 8280.0,
                "current_price": 8291.0,
            }
        }
        with patch.object(trade_journal, "realized_pnl_today", return_value=-23.5):
            day_pl, _, detail = _broker_style_day_pl("INDIA", broker, 100_000.0)
        self.assertAlmostEqual(day_pl, 11.0 - 23.5)
        self.assertAlmostEqual(detail["unrealized"], 11.0)
        self.assertAlmostEqual(detail["realized"], -23.5)

    def test_flat_book_shows_realized_only(self):
        broker = MagicMock(spec=["get_open_positions"])
        broker.get_open_positions.return_value = {}
        with patch.object(trade_journal, "realized_pnl_today", return_value=-84.8):
            day_pl, day_pct, detail = _broker_style_day_pl("INDIA", broker, 229_000.0)
        self.assertAlmostEqual(day_pl, -84.8)
        self.assertAlmostEqual(detail["realized"], -84.8)
        self.assertAlmostEqual(detail["unrealized"], 0.0)
        self.assertAlmostEqual(day_pct, -84.8 / 229_000.0 * 100)

    def test_falls_back_to_unrealized_pl_when_ltp_missing(self):
        broker = MagicMock(spec=["get_open_positions"])
        broker.get_open_positions.return_value = {
            "RELIANCE": {
                "qty": 2,
                "avg_entry_price": 1000.0,
                "current_price": 0.0,
                "unrealized_pl": 7.5,
            }
        }
        with patch.object(trade_journal, "realized_pnl_today", return_value=0.0):
            day_pl, _, detail = _broker_style_day_pl("INDIA", broker, 50_000.0)
        self.assertAlmostEqual(day_pl, 7.5)
        self.assertAlmostEqual(detail["unrealized"], 7.5)

    def test_short_position_unrealized_uses_side_aware_formula(self):
        broker = MagicMock(spec=["get_open_positions"])
        broker.get_open_positions.return_value = {
            "MARUTI": {
                "qty": 16,
                "side": "SELL",
                "avg_entry_price": 13948.444,
                "current_price": 13929.0,
                "unrealized_pl": -311.1,  # broker raw sign can differ
            }
        }
        with patch.object(trade_journal, "realized_pnl_today", return_value=0.0):
            day_pl, _, detail = _broker_style_day_pl("INDIA", broker, 300_000.0)
        self.assertAlmostEqual(day_pl, (13948.444 - 13929.0) * 16, places=3)
        self.assertGreater(detail["unrealized"], 0.0)

    def test_day_pl_resynces_bad_exits_from_broker_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = config.TRADE_JOURNAL_PATH
            config.TRADE_JOURNAL_PATH = str(Path(tmp) / "journal.db")
            try:
                trade_journal.init_db()
                # SBIN false +8.80 from stale LTP; INFY +0 from entry-as-exit
                trade_journal.record_entry("INDIA", "SBIN", 22, 1082.0)
                trade_journal.record_exit("INDIA", "SBIN", 1082.4, reason="signal_sell")
                trade_journal.record_entry("INDIA", "INFY", 20, 1189.10)
                trade_journal.record_exit("INDIA", "INFY", 1189.10, reason="broker_flat")

                fills = {"SBIN": 1081.0, "INFY": 1186.40}
                broker = MagicMock()
                broker.get_open_positions.return_value = {}
                broker.latest_sell_fill_price.side_effect = lambda s: fills.get(
                    str(s).upper(), 0.0
                )

                day_pl, _, detail = _broker_style_day_pl("INDIA", broker, 229_000.0)
                expected = (1081.0 - 1082.0) * 22 + (1186.40 - 1189.10) * 20
                self.assertAlmostEqual(day_pl, expected, places=2)
                self.assertAlmostEqual(detail["realized"], round(expected, 2))
            finally:
                config.TRADE_JOURNAL_PATH = old


class TestRealizedPnlToday(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.TRADE_JOURNAL_PATH
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "journal.db")
        trade_journal.init_db()

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old
        self._tmp.cleanup()

    def test_sums_today_and_skips_zero_exit(self):
        trade_journal.record_entry("INDIA", "AAA", 1, 100.0)
        trade_journal.record_exit("INDIA", "AAA", 110.0, reason="tp")

        trade_journal.record_entry("INDIA", "BBB", 1, 100.0)
        # Bogus zero exit should be ignored by realized_pnl_today
        with trade_journal._lock, trade_journal._conn() as conn:
            conn.execute(
                """
                UPDATE trades SET exit_price=0, pnl=-100, status='closed',
                closed_at=? WHERE symbol='BBB'
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )

        total = trade_journal.realized_pnl_today("INDIA", tz_name="Asia/Kolkata")
        self.assertAlmostEqual(total, 10.0)

    def test_reconcile_broker_flats(self):
        trade_journal.record_entry("INDIA", "SBIN", 22, 1082.0)
        trade_journal.record_entry("INDIA", "INFY", 20, 1189.0)
        closed = trade_journal.reconcile_broker_flats(
            "INDIA",
            {"INFY"},
            price_lookup=lambda s: 1081.0 if s == "SBIN" else 0.0,
            reason="broker_flat",
        )
        self.assertEqual(closed, ["SBIN"])
        open_rows = trade_journal.list_open_trades("INDIA")
        self.assertEqual([r["symbol"] for r in open_rows], ["INFY"])
        realized = trade_journal.realized_pnl_today("INDIA", tz_name="Asia/Kolkata")
        self.assertAlmostEqual(realized, (1081.0 - 1082.0) * 22)

    def test_reconcile_uses_fill_when_ltp_missing_no_entry_fallback(self):
        """INFY broker SL: missing LTP must use fill, never entry (+0)."""
        trade_journal.record_entry("INDIA", "INFY", 20, 1189.10)
        fills = {"INFY": 1186.40}
        closed = trade_journal.reconcile_broker_flats(
            "INDIA",
            set(),
            price_lookup=lambda s: fills.get(s, 0.0),  # no LTP
            reason="broker_flat",
        )
        self.assertEqual(closed, ["INFY"])
        realized = trade_journal.realized_pnl_today("INDIA", tz_name="Asia/Kolkata")
        self.assertAlmostEqual(realized, (1186.40 - 1189.10) * 20)

    def test_reconcile_skips_when_no_fill_and_no_ltp(self):
        trade_journal.record_entry("INDIA", "INFY", 20, 1189.10)
        closed = trade_journal.reconcile_broker_flats(
            "INDIA",
            set(),
            price_lookup=lambda s: 0.0,
            reason="broker_flat",
        )
        self.assertEqual(closed, [])
        open_rows = trade_journal.list_open_trades("INDIA")
        self.assertEqual([r["symbol"] for r in open_rows], ["INFY"])

    def test_resync_corrects_stale_and_entry_exits(self):
        trade_journal.record_entry("INDIA", "SBIN", 22, 1082.0)
        trade_journal.record_exit("INDIA", "SBIN", 1082.4, reason="signal_sell")
        trade_journal.record_entry("INDIA", "INFY", 20, 1189.10)
        trade_journal.record_exit("INDIA", "INFY", 1189.10, reason="broker_flat")

        fills = {"SBIN": 1081.0, "INFY": 1186.40}
        fixed = trade_journal.resync_today_exits_from_fills(
            "INDIA",
            lambda s: fills.get(s, 0.0),
            tz_name="Asia/Kolkata",
        )
        self.assertEqual({r["symbol"] for r in fixed}, {"SBIN", "INFY"})
        realized = trade_journal.realized_pnl_today("INDIA", tz_name="Asia/Kolkata")
        self.assertAlmostEqual(
            realized, (1081.0 - 1082.0) * 22 + (1186.40 - 1189.10) * 20
        )


class TestLatestSellFillPrice(unittest.TestCase):
    def setUp(self):
        self.broker = DhanBroker.__new__(DhanBroker)
        self.broker.paper = None
        self.broker.dhan = MagicMock()
        self.broker.ensure_session = MagicMock()

    def test_prefers_trade_book_latest_sell(self):
        self.broker._list_trade_book_rows = MagicMock(
            return_value=[
                {
                    "transactionType": "SELL",
                    "tradingSymbol": "INFY-EQ",
                    "securityId": "1594",
                    "tradedPrice": 1187.0,
                    "exchangeTime": "2026-08-12 10:00:00",
                },
                {
                    "transactionType": "SELL",
                    "tradingSymbol": "INFY-EQ",
                    "securityId": "1594",
                    "tradedPrice": 1186.40,
                    "exchangeTime": "2026-08-12 11:30:00",
                },
                {
                    "transactionType": "BUY",
                    "tradingSymbol": "INFY-EQ",
                    "tradedPrice": 1189.10,
                    "exchangeTime": "2026-08-12 09:30:00",
                },
            ]
        )
        self.broker._list_order_rows = MagicMock(return_value=[])
        self.assertAlmostEqual(self.broker.latest_sell_fill_price("INFY"), 1186.40)

    def test_falls_back_to_order_list_traded_sell(self):
        self.broker._list_trade_book_rows = MagicMock(return_value=[])
        self.broker._list_order_rows = MagicMock(
            return_value=[
                {
                    "transactionType": "SELL",
                    "orderStatus": "TRADED",
                    "tradingSymbol": "SBIN-EQ",
                    "averageTradedPrice": 1081.0,
                    "filledQty": 22,
                    "updateTime": "2026-08-12 10:15:00",
                }
            ]
        )
        self.assertAlmostEqual(self.broker.latest_sell_fill_price("SBIN"), 1081.0)

    def test_resolve_exit_prefers_history_over_ltp(self):
        self.broker.get_order_fill_price = MagicMock(return_value=0.0)
        self.broker.latest_sell_fill_price = MagicMock(return_value=1186.40)
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 1190.0})
        px = self.broker.resolve_exit_fill_price("INFY", None, fallback=1189.10)
        self.assertAlmostEqual(px, 1186.40)

    def test_latest_buy_fill_from_trade_book(self):
        self.broker._list_trade_book_rows = MagicMock(
            return_value=[
                {
                    "transactionType": "BUY",
                    "tradingSymbol": "SBIN-EQ",
                    "tradedPrice": 1082.15,
                    "exchangeTime": "2026-08-12 09:45:00",
                },
                {
                    "transactionType": "SELL",
                    "tradingSymbol": "SBIN-EQ",
                    "tradedPrice": 1081.0,
                    "exchangeTime": "2026-08-12 10:15:00",
                },
            ]
        )
        self.broker._list_order_rows = MagicMock(return_value=[])
        self.assertAlmostEqual(self.broker.latest_buy_fill_price("SBIN"), 1082.15)

    def test_resolve_entry_prefers_order_fill(self):
        self.broker.get_order_fill_price = MagicMock(return_value=1082.25)
        self.broker.latest_buy_fill_price = MagicMock(return_value=1082.0)
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 1083.0})
        px = self.broker.resolve_entry_fill_price("SBIN", "oid-1", fallback=1080.0)
        self.assertAlmostEqual(px, 1082.25)


class TestOrbFireAfterFill(unittest.TestCase):
    def _orb_breakout_df(self):
        """Minimal IST 5m bars: OR 09:15–09:30, then close above OR high."""
        import pandas as pd
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        # 09:15, 09:20, 09:25 = OR (high=100); then 09:30+ breakout closes at 101
        times = [
            datetime(2026, 8, 12, 9, 15, tzinfo=ist),
            datetime(2026, 8, 12, 9, 20, tzinfo=ist),
            datetime(2026, 8, 12, 9, 25, tzinfo=ist),
            datetime(2026, 8, 12, 9, 30, tzinfo=ist),
            datetime(2026, 8, 12, 9, 35, tzinfo=ist),
        ]
        # Need enough history for ATR/EMA — prepend flat bars
        pre = [datetime(2026, 8, 11, 12, 0, tzinfo=ist) + timedelta(minutes=5 * i) for i in range(40)]
        idx = pre + times
        n = len(idx)
        close = [99.0] * 40 + [99.0, 99.5, 100.0, 101.0, 101.5]
        high = [c + 0.2 for c in close]
        # OR bars need high peaking at 100
        high[40] = 100.0
        high[41] = 100.0
        high[42] = 100.0
        low = [c - 0.2 for c in close]
        open_ = list(close)
        vol = [2_000_000.0] * n
        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
            index=pd.DatetimeIndex(idx),
        )

    def test_signal_does_not_lock_until_mark_day_fired(self):
        from strategy import OpeningRangeBreakoutStrategy, params_for_market
        import config as cfg

        old_htf = cfg.ORB_USE_HTF_FILTER
        old_vol = cfg.VOLUME_MULT
        old_confirm = cfg.CONFIRM_BARS
        try:
            cfg.ORB_USE_HTF_FILTER = False
            cfg.VOLUME_MULT = 0.0
            cfg.CONFIRM_BARS = 1
            strat = OpeningRangeBreakoutStrategy(params_for_market("INDIA"))
            strat.use_htf = False
            strat.volume_mult = 0.0
            strat._fired = {}
            df = strat.compute_indicators(self._orb_breakout_df())

            sig1 = strat.generate_signal(df, "RELIANCE")
            self.assertEqual(sig1, "BUY")
            self.assertFalse(strat.has_day_fired("RELIANCE", "BUY"))

            # Still BUY on next call — not locked by bare signal
            sig2 = strat.generate_signal(df, "RELIANCE")
            self.assertEqual(sig2, "BUY")

            strat.mark_day_fired("RELIANCE", "BUY")
            self.assertTrue(strat.has_day_fired("RELIANCE", "BUY"))
            sig3 = strat.generate_signal(df, "RELIANCE")
            self.assertEqual(sig3, "HOLD")
        finally:
            cfg.ORB_USE_HTF_FILTER = old_htf
            cfg.VOLUME_MULT = old_vol
            cfg.CONFIRM_BARS = old_confirm


if __name__ == "__main__":
    unittest.main()

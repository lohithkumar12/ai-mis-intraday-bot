"""Safety / execution fixes: pending fills, ORB completed bars, risk latch, etc."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

import bot_state
import config
import order_guards
import trade_journal
from dhan_broker import DhanBroker
from risk_manager import RiskManager
from strategy import OpeningRangeBreakoutStrategy, params_for_market


class TestConfirmPendingNotFill(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()

    def test_pending_timeout_is_failure(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.last_fill_qty = 0
        broker.get_order_status = MagicMock(return_value=("PENDING", "PENDING"))
        ok, detail = broker.confirm_live_order("oid-p", "INFY", timeout_sec=1.0)
        self.assertFalse(ok)
        self.assertEqual(detail, "PENDING")
        blocked, reason = order_guards.is_buy_blocked("INFY")
        self.assertTrue(blocked)
        self.assertIn("pending", reason.lower())

    def test_traded_ok(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.last_fill_qty = 0
        broker.get_order_status = MagicMock(return_value=("TRADED", "TRADED"))
        broker.get_order_filled_qty = MagicMock(return_value=25)
        ok, detail = broker.confirm_live_order("oid-t", "SBIN", timeout_sec=1.0)
        self.assertTrue(ok)
        self.assertEqual(detail, "TRADED")
        self.assertEqual(broker.last_fill_qty, 25)

    def test_part_traded_uses_filled_qty(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.last_fill_qty = 0
        broker.get_order_status = MagicMock(return_value=("PART_TRADED", "PART"))
        broker.get_order_filled_qty = MagicMock(return_value=10)
        ok, detail = broker.confirm_live_order("oid-pt", "TCS", timeout_sec=1.0)
        self.assertTrue(ok)
        self.assertEqual(detail, "PART_TRADED")
        self.assertEqual(broker.last_fill_qty, 10)

    def test_part_traded_zero_qty_fails(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.last_fill_qty = 0
        broker.get_order_status = MagicMock(return_value=("PART_TRADED", "PART"))
        broker.get_order_filled_qty = MagicMock(return_value=0)
        ok, detail = broker.confirm_live_order("oid-z", "WIPRO", timeout_sec=1.0)
        self.assertFalse(ok)
        self.assertEqual(detail, "PART_TRADED_ZERO_FILL")

    def test_cancelled_fails(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.get_order_status = MagicMock(return_value=("CANCELLED", "user cancel"))
        ok, detail = broker.confirm_live_order("oid-c", "ITC", timeout_sec=1.0)
        self.assertFalse(ok)
        self.assertIn("CANCELLED", detail)
        blocked, _ = order_guards.is_buy_blocked("ITC")
        self.assertTrue(blocked)


class TestCompletedCandleOrb(unittest.TestCase):
    def _df(self, last_incomplete: bool):
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        now = datetime.now(ist)
        # Align last bar start to current 5m bucket
        bucket = now.replace(second=0, microsecond=0) - timedelta(
            minutes=now.minute % 5, seconds=now.second, microseconds=now.microsecond
        )
        if not last_incomplete:
            # Last bar finished an hour ago
            end = now - timedelta(hours=1)
            bucket = end.replace(second=0, microsecond=0) - timedelta(minutes=end.minute % 5)

        times = [
            datetime(2026, 8, 12, 9, 15, tzinfo=ist),
            datetime(2026, 8, 12, 9, 20, tzinfo=ist),
            datetime(2026, 8, 12, 9, 25, tzinfo=ist),
            datetime(2026, 8, 12, 9, 30, tzinfo=ist),
            datetime(2026, 8, 12, 9, 35, tzinfo=ist),
        ]
        if last_incomplete:
            times[-1] = bucket  # forming
        pre = [
            datetime(2026, 8, 11, 12, 0, tzinfo=ist) + timedelta(minutes=5 * i)
            for i in range(40)
        ]
        idx = pre + times
        close = [99.0] * 40 + [99.0, 99.5, 100.0, 101.0, 105.0]
        high = [c + 0.2 for c in close]
        high[40] = high[41] = high[42] = 100.0
        low = [c - 0.2 for c in close]
        vol = [2_000_000.0] * len(idx)
        return pd.DataFrame(
            {"open": close, "high": high, "low": low, "close": close, "volume": vol},
            index=pd.DatetimeIndex(idx),
        )

    def test_forming_bar_dropped(self):
        strat = OpeningRangeBreakoutStrategy(params_for_market("INDIA"))
        df = self._df(last_incomplete=True)
        out = strat.completed_bars_only(df)
        self.assertLess(len(out), len(df))
        self.assertNotEqual(out.index[-1], df.index[-1])

    def test_completed_bars_kept(self):
        strat = OpeningRangeBreakoutStrategy(params_for_market("INDIA"))
        df = self._df(last_incomplete=False)
        out = strat.completed_bars_only(df)
        # Historical completed session bars should remain
        self.assertGreaterEqual(len(out), len(df) - 1)


class TestCoreScoutDuplicate(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()

    def test_reservation_blocks_second_book(self):
        ok1, _ = order_guards.try_reserve_buy("RELIANCE", owner="core")
        self.assertTrue(ok1)
        ok2, why = order_guards.try_reserve_buy("RELIANCE", owner="scout")
        self.assertFalse(ok2)
        self.assertIn("reserved", why)
        order_guards.release_buy_reservation("RELIANCE")
        ok3, _ = order_guards.try_reserve_buy("RELIANCE", owner="scout")
        self.assertTrue(ok3)
        order_guards.release_buy_reservation("RELIANCE")


class TestPortfolioRisk(unittest.TestCase):
    def test_blocks_when_budget_exceeded(self):
        rm = RiskManager(market="INDIA")
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000):
            with patch.object(config, "DAILY_DRAWDOWN_LIMIT", 0.02):
                # budget = 6000
                rm.register_trade("AAA", 100.0, 90.0, atr=1.0, qty=400)  # risk 4000
                ok, why = rm.can_add_trade_risk(300_000, 3000, {"AAA": {"qty": 400}})
                self.assertFalse(ok)
                self.assertIn("portfolio_risk", why)

    def test_allows_within_budget(self):
        rm = RiskManager(market="INDIA")
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000):
            with patch.object(config, "DAILY_DRAWDOWN_LIMIT", 0.02):
                ok, why = rm.can_add_trade_risk(300_000, 1000, {})
                self.assertTrue(ok)
                self.assertEqual(why, "")


class TestDailyDrawdownLatch(unittest.TestCase):
    def setUp(self):
        bot_state.reset_kill_for_tests()

    def test_latches_until_reset(self):
        rm = RiskManager(market="INDIA")
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000):
            with patch.object(config, "DAILY_DRAWDOWN_LIMIT", 0.02):
                self.assertTrue(
                    rm.check_daily_drawdown(300_000, 300_000, day_pl=-7000)
                )
                self.assertTrue(
                    rm.check_daily_drawdown(300_000, 300_000, day_pl=-100)
                )
                rm.reset_kill_switch()
                self.assertFalse(
                    rm.check_daily_drawdown(300_000, 300_000, day_pl=-100)
                )


class TestRestartReconcile(unittest.TestCase):
    def test_stale_meta_dropped_without_broker_position(self):
        rm = RiskManager(market="INDIA")
        rm.register_trade("GHOST", 100.0, 95.0, atr=1.0, qty=10)
        rm.reconcile_meta_with_broker({})  # broker flat
        self.assertNotIn("GHOST", rm._trade_meta)

    def test_broker_open_reconstructs_meta(self):
        rm = RiskManager(market="INDIA")
        rm.reconcile_meta_with_broker(
            {"INFY": {"qty": 20, "avg_entry_price": 1500.0, "atr": 5.0}}
        )
        self.assertIn("INFY", rm._trade_meta)
        self.assertEqual(rm._trade_meta["INFY"]["qty"], 20)
        self.assertGreater(rm._trade_meta["INFY"]["entry"], 0)


class TestDuplicateExits(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()

    def test_exit_inflight_blocks_second(self):
        self.assertTrue(order_guards.try_begin_exit("MARUTI"))
        self.assertFalse(order_guards.try_begin_exit("MARUTI"))
        order_guards.end_exit("MARUTI")
        self.assertTrue(order_guards.try_begin_exit("MARUTI"))
        order_guards.end_exit("MARUTI")

    def test_close_position_skips_when_inflight(self):
        order_guards.try_begin_exit("SBIN")
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.get_open_positions = MagicMock(
            return_value={"SBIN": {"qty": 10, "avg_entry_price": 100, "side": "BUY"}}
        )
        self.assertIsNone(broker.close_position("SBIN"))
        order_guards.end_exit("SBIN")


class TestJournalSource(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.TRADE_JOURNAL_PATH
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "j.db")
        trade_journal.init_db()

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old
        self._tmp.cleanup()

    def test_entry_source_survives_exit(self):
        trade_journal.record_entry(
            "INDIA",
            "CIPLA",
            10,
            100.0,
            reason="scout_signal_buy",
            strategy="opening_range_breakout",
            meta={"source": "scout"},
        )
        out = trade_journal.record_exit("INDIA", "CIPLA", 99.0, reason="stop_loss")
        self.assertEqual(out["source"], "scout")
        self.assertEqual(out["entry_reason"], "scout_signal_buy")
        self.assertEqual(out["reason"], "stop_loss")


class TestSyncBrokerStopsGuard(unittest.TestCase):
    def test_never_moves_stop_backwards(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.dhan = MagicMock()
        with patch.object(config, "SYNC_BROKER_STOPS", True):
            ok = broker.sync_broker_stop(
                "INFY",
                90.0,
                prev_stop=100.0,
                sl_order_id="sl1",
                qty=10,
            )
            self.assertFalse(ok)
            broker.dhan.modify_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()

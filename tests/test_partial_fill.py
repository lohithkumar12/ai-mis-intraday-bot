"""PART_TRADED reconcile: resize RiskManager SL/TP to actual fill, activate monitor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import order_guards
import trade_journal
from dhan_broker import DhanBroker
from risk_manager import RiskManager
from strategy import OpeningRangeBreakoutStrategy, params_for_market, reset_orb_fired_for_tests


class TestApplyPartialFill(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.TRADE_JOURNAL_PATH
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "j.db")

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old
        self._tmp.cleanup()

    def test_keeps_plan_pct_and_uses_filled_qty(self):
        rm = RiskManager(market="INDIA")
        rm.register_trade("TCS", 100.0, 98.0, qty=25, take_profit=104.0, order_id="oid-1")
        meta = rm.apply_partial_fill(
            "TCS",
            filled_qty=10,
            average_price=99.50,
            planned_entry=100.0,
            planned_sl=98.0,
            planned_tp=104.0,
            order_id="oid-1",
        )
        self.assertEqual(meta["qty"], 10)
        self.assertAlmostEqual(meta["entry"], 99.50, places=2)
        # 2% SL / 4% TP of fill, NSE 0.01 tick
        self.assertAlmostEqual(meta["stop"], 97.51, places=2)
        self.assertAlmostEqual(meta["take_profit"], 103.48, places=2)
        self.assertAlmostEqual(rm._trail_peaks["TCS"], 99.50, places=2)
        # Open risk must use reduced qty, not intended 25
        risk = rm.open_risk_rupees()
        self.assertAlmostEqual(risk, (99.50 - 97.51) * 10, places=2)
        self.assertLess(risk, (100.0 - 98.0) * 25)

    def test_trail_does_not_fire_from_stale_intended_peak(self):
        rm = RiskManager(market="INDIA")
        rm.register_trade("INFY", 100.0, 98.0, qty=20, take_profit=104.0)
        rm._trail_peaks["INFY"] = 103.0  # stale peak from intended plan
        rm.apply_partial_fill(
            "INFY",
            filled_qty=8,
            average_price=99.50,
            planned_entry=100.0,
            planned_sl=98.0,
            planned_tp=104.0,
        )
        # 1R from new fill is ~1.99; price 100.2 is not yet 1R
        stop = rm.update_trailing_stop("INFY", 100.20, atr=None)
        self.assertAlmostEqual(stop, rm._trade_meta["INFY"]["initial_stop"], places=2)


class TestReconcilePartialFill(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()
        reset_orb_fired_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.TRADE_JOURNAL_PATH
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "j.db")
        trade_journal.init_db()
        self.broker = DhanBroker.__new__(DhanBroker)
        self.broker.paper = None
        self.broker.dhan = None
        self.broker.last_error = ""
        self.broker.last_fill_qty = 0
        self.broker.last_fill_price = 0.0
        self.broker.last_order_status = "PART_TRADED"
        self.broker.last_sl_order_id = None
        self.broker.last_super_order_id = None
        self.broker.sl_tp_meta = {}
        self.rm = RiskManager(market="INDIA")
        self.strat = OpeningRangeBreakoutStrategy(params_for_market("INDIA"))

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old
        reset_orb_fired_for_tests()
        order_guards.reset_for_tests()
        self._tmp.cleanup()

    def _snap(self, **over):
        base = {
            "order_id": "oid-pt",
            "symbol": "TCS",
            "status": "PART_TRADED",
            "filled_qty": 10,
            "average_price": 99.50,
            "quantity": 25,
            "remaining_qty": 15,
        }
        base.update(over)
        return base

    def test_activates_monitor_and_clears_cooldown(self):
        order_guards.block_buy("TCS", 600, reason="order_pending:oid-pt")
        self.broker.get_order_fill_snapshot = MagicMock(return_value=self._snap())
        rec = self.broker.reconcile_partial_fill(
            "oid-pt",
            risk_mgr=self.rm,
            strategy=self.strat,
            intended_qty=25,
            planned_entry=100.0,
            planned_sl=98.0,
            planned_tp=104.0,
            symbol="TCS",
        )
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["monitor"], "ACTIVE")
        self.assertEqual(rec["filled_qty"], 10)
        self.assertEqual(self.broker.sl_tp_meta["TCS"]["status"], "ACTIVE")
        self.assertEqual(self.broker.sl_tp_meta["TCS"]["qty"], 10)
        blocked, _ = order_guards.is_buy_blocked("TCS")
        self.assertFalse(blocked)
        self.assertTrue(self.strat.has_day_fired("TCS", "BUY"))
        self.assertEqual(self.rm._trade_meta["TCS"]["qty"], 10)

    def test_does_not_overwrite_existing_journal(self):
        trade_journal.record_entry(
            "INDIA", "TCS", 25, 100.0, stop_price=98.0, take_profit=104.0
        )
        self.broker.get_order_fill_snapshot = MagicMock(return_value=self._snap())
        rec = self.broker.reconcile_partial_fill(
            "oid-pt",
            risk_mgr=self.rm,
            strategy=self.strat,
            intended_qty=25,
            planned_entry=100.0,
            planned_sl=98.0,
            planned_tp=104.0,
            symbol="TCS",
        )
        self.assertEqual(rec["journal"], "kept")
        open_rows = trade_journal.list_open_trades("INDIA")
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(int(open_rows[0]["qty"]), 25)
        self.assertAlmostEqual(float(open_rows[0]["entry_price"]), 100.0, places=2)
        # Local risk still uses the actual fill
        self.assertEqual(self.rm._trade_meta["TCS"]["qty"], 10)

    def test_journals_once_when_missing(self):
        self.broker.get_order_fill_snapshot = MagicMock(return_value=self._snap())
        rec = self.broker.reconcile_partial_fill(
            "oid-pt",
            risk_mgr=self.rm,
            strategy=self.strat,
            intended_qty=25,
            planned_entry=100.0,
            planned_sl=98.0,
            planned_tp=104.0,
            symbol="TCS",
        )
        self.assertEqual(rec["journal"], "recorded")
        open_rows = trade_journal.list_open_trades("INDIA")
        self.assertEqual(len(open_rows), 1)
        self.assertEqual(int(open_rows[0]["qty"]), 10)
        self.assertAlmostEqual(float(open_rows[0]["entry_price"]), 99.50, places=2)

    def test_complete_cancel_voids_journal_keeps_day_fired(self):
        self.strat.mark_day_fired("TCS", "BUY")
        trade_journal.record_entry("INDIA", "TCS", 25, 100.0)
        self.broker.get_order_fill_snapshot = MagicMock(
            return_value=self._snap(status="CANCELLED", filled_qty=0, average_price=0.0)
        )
        rec = self.broker.reconcile_partial_fill(
            "oid-pt",
            risk_mgr=self.rm,
            strategy=self.strat,
            intended_qty=25,
            symbol="TCS",
        )
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["journal"], "voided")
        self.assertEqual(trade_journal.list_open_trades("INDIA"), [])
        self.assertTrue(self.strat.has_day_fired("TCS", "BUY"))
        self.assertEqual(self.broker.sl_tp_meta["TCS"]["status"], "COOLDOWN")

    def test_check_sl_tp_skips_cooldown_then_monitors_active(self):
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 97.40})
        self.broker.close_position = MagicMock(return_value=97.40)
        self.broker.get_open_positions = MagicMock(
            return_value={
                "TCS": {
                    "qty": 10,
                    "avg_entry_price": 99.50,
                    "current_price": 97.40,
                    "side": "BUY",
                }
            }
        )
        self.rm.register_trade("TCS", 99.50, 97.51, qty=10, take_profit=103.48)
        self.broker.sl_tp_meta["TCS"] = {
            "status": "COOLDOWN",
            "qty": 10,
            "entry": 99.50,
            "stop_loss_price": 97.51,
            "target_price": 103.48,
        }
        closed = self.broker.check_sl_tp(self.rm)
        self.assertEqual(closed, [])
        self.broker.close_position.assert_not_called()

        self.broker.sl_tp_meta["TCS"]["status"] = "ACTIVE"
        closed = self.broker.check_sl_tp(self.rm)
        self.assertEqual(closed, ["TCS"])
        self.broker.close_position.assert_called_once_with("TCS")

    def test_live_fill_qty_never_uses_intended_on_part_traded(self):
        self.broker.last_fill_qty = 0
        self.broker.get_order_filled_qty = MagicMock(return_value=0)
        self.assertEqual(self.broker._live_fill_qty("oid-z", 25, "PART_TRADED"), 0)
        self.broker.get_order_filled_qty = MagicMock(return_value=10)
        self.assertEqual(self.broker._live_fill_qty("oid-pt", 25, "PART_TRADED"), 10)
        # Full TRADED may fall back to intended if lookup failed
        self.broker.last_fill_qty = 0
        self.broker.get_order_filled_qty = MagicMock(return_value=0)
        self.assertEqual(self.broker._live_fill_qty("oid-t", 25, "TRADED"), 25)


if __name__ == "__main__":
    unittest.main()

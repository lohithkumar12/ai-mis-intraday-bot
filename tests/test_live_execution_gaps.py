"""Live-risk gaps: PART_TRADED remainder, PENDING cancel, trail sync, circuit, zombie SL."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import order_guards
from dhan_broker import DhanBroker
from price_guards import india_buy_locked
from risk_manager import RiskManager
from strategy import OpeningRangeBreakoutStrategy, params_for_market, reset_orb_fired_for_tests


class TestCancelUnfilledRemainder(unittest.TestCase):
    def setUp(self):
        self.broker = DhanBroker.__new__(DhanBroker)
        self.broker.paper = None
        self.broker.dhan = MagicMock()
        self.broker.last_super_order_id = None
        self.broker.dhan.cancel_super_order = MagicMock(return_value={"status": "success"})
        self.broker.dhan.cancel_order = MagicMock(return_value={"status": "success"})

    def test_super_cancels_entry_leg_only(self):
        self.broker.last_super_order_id = "super-1"
        ok = self.broker.cancel_unfilled_remainder(
            "super-1",
            symbol="TCS",
            filled_qty=40,
            remaining_qty=60,
            super_order_id="super-1",
        )
        self.assertTrue(ok)
        self.broker.dhan.cancel_super_order.assert_called_once_with("super-1", "ENTRY_LEG")
        self.broker.dhan.cancel_order.assert_not_called()

    def test_regular_order_cancels_remainder(self):
        ok = self.broker.cancel_unfilled_remainder(
            "oid-1", symbol="INFY", filled_qty=10, remaining_qty=15
        )
        self.assertTrue(ok)
        self.broker.dhan.cancel_order.assert_called()

    def test_skips_when_fully_filled(self):
        ok = self.broker.cancel_unfilled_remainder(
            "oid-1", symbol="INFY", filled_qty=25, remaining_qty=0
        )
        self.assertTrue(ok)
        self.broker.dhan.cancel_order.assert_not_called()
        self.broker.dhan.cancel_super_order.assert_not_called()


class TestReconcileCancelsRemainder(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()
        reset_orb_fired_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.TRADE_JOURNAL_PATH
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "j.db")
        import trade_journal

        trade_journal.init_db()
        self.broker = DhanBroker.__new__(DhanBroker)
        self.broker.paper = None
        self.broker.dhan = None
        self.broker.last_error = ""
        self.broker.last_fill_qty = 0
        self.broker.last_fill_price = 0.0
        self.broker.last_order_status = "PART_TRADED"
        self.broker.last_sl_order_id = None
        self.broker.last_super_order_id = "super-pt"
        self.broker.sl_tp_meta = {}
        self.broker.cancel_unfilled_remainder = MagicMock(return_value=True)
        self.rm = RiskManager(market="INDIA")
        self.strat = OpeningRangeBreakoutStrategy(params_for_market("INDIA"))

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old
        reset_orb_fired_for_tests()
        order_guards.reset_for_tests()
        self._tmp.cleanup()

    def test_reconcile_cancels_remaining(self):
        self.broker.get_order_fill_snapshot = MagicMock(
            return_value={
                "order_id": "oid-pt",
                "symbol": "TCS",
                "status": "PART_TRADED",
                "filled_qty": 40,
                "average_price": 99.50,
                "quantity": 100,
                "remaining_qty": 60,
            }
        )
        rec = self.broker.reconcile_partial_fill(
            "oid-pt",
            risk_mgr=self.rm,
            strategy=self.strat,
            intended_qty=100,
            planned_entry=100.0,
            planned_sl=98.0,
            planned_tp=104.0,
            symbol="TCS",
        )
        self.assertTrue(rec["ok"])
        self.broker.cancel_unfilled_remainder.assert_called()
        kwargs = self.broker.cancel_unfilled_remainder.call_args.kwargs
        self.assertEqual(kwargs["filled_qty"], 40)
        self.assertEqual(kwargs["remaining_qty"], 60)
        self.assertEqual(self.rm._trade_meta["TCS"]["qty"], 40)


class TestPendingTimeoutCancel(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()

    def tearDown(self):
        order_guards.reset_for_tests()

    def test_zero_fill_pending_is_cancelled(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.last_fill_qty = 0
        broker.last_super_order_id = "s1"
        broker.get_order_status = MagicMock(return_value=("PENDING", "PENDING"))
        broker.get_order_fill_snapshot = MagicMock(
            return_value={
                "filled_qty": 0,
                "remaining_qty": 100,
                "average_price": 0.0,
                "status": "PENDING",
            }
        )
        broker.cancel_unfilled_remainder = MagicMock(return_value=True)
        ok, detail = broker.confirm_live_order("oid-p", "INFY", timeout_sec=0.8)
        self.assertFalse(ok)
        self.assertEqual(detail, "PENDING_CANCELLED")
        broker.cancel_unfilled_remainder.assert_called()
        self.assertTrue(broker.cancel_unfilled_remainder.call_args.kwargs.get("force"))

    def test_late_fill_during_timeout_is_adopted(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.last_fill_qty = 0
        broker.last_super_order_id = None
        broker.get_order_status = MagicMock(return_value=("PENDING", "PENDING"))
        broker.get_order_fill_snapshot = MagicMock(
            return_value={
                "filled_qty": 12,
                "remaining_qty": 8,
                "average_price": 101.0,
                "status": "PART_TRADED",
            }
        )
        broker.cancel_unfilled_remainder = MagicMock(return_value=True)
        ok, detail = broker.confirm_live_order("oid-late", "SBIN", timeout_sec=0.8)
        self.assertTrue(ok)
        self.assertEqual(broker.last_fill_qty, 12)
        broker.cancel_unfilled_remainder.assert_called()


class TestTrailSyncRetry(unittest.TestCase):
    def test_failed_modify_does_not_advance_sl_row(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.dhan = MagicMock()
        broker.sync_broker_stop = MagicMock(return_value=False)
        broker.close_position = MagicMock(return_value=None)
        broker.get_latest_quote = MagicMock(return_value={"ltp": 103.0})
        broker.get_open_positions = MagicMock(
            return_value={
                "INFY": {
                    "qty": 10,
                    "avg_entry_price": 100.0,
                    "current_price": 103.0,
                    "side": "BUY",
                    "atr": 1.0,
                }
            }
        )
        rm = RiskManager(market="INDIA")
        rm.atr_trail_mult = 1.5
        rm.register_trade("INFY", 100.0, 98.0, atr=1.0, qty=10, take_profit=104.0)
        broker.sl_tp_meta = {
            "INFY": {
                "status": "ACTIVE",
                "stop_loss_price": 98.0,
                "target_price": 104.0,
                "qty": 10,
            }
        }
        with patch.object(config, "SYNC_BROKER_STOPS", True):
            closed = broker.check_sl_tp(rm)
        self.assertEqual(closed, [])
        broker.sync_broker_stop.assert_called()
        # Broker row stays at old stop so the next tick retries modify.
        self.assertAlmostEqual(broker.sl_tp_meta["INFY"]["stop_loss_price"], 98.0)
        # Software trail still ratcheted for this-tick exit comparison.
        self.assertGreater(rm._trade_meta["INFY"]["stop"], 98.0)

    def test_modify_retries_then_succeeds(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.dhan = MagicMock()
        broker.dhan.modify_super_order = MagicMock(
            side_effect=[
                {"status": "failure"},
                {"status": "success"},
            ]
        )
        broker.ensure_session = MagicMock()
        with patch.object(config, "SYNC_BROKER_STOPS", True), patch.object(
            config, "SYNC_BROKER_STOP_RETRIES", 3
        ), patch("dhan_broker.time.sleep"):
            ok = broker.sync_broker_stop(
                "TCS",
                101.0,
                prev_stop=98.0,
                super_order_id="sup-1",
                qty=10,
            )
        self.assertTrue(ok)
        self.assertEqual(broker.dhan.modify_super_order.call_count, 2)

    def test_sl_modify_uses_entry_leg(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.dhan = MagicMock()
        broker.dhan.modify_order.return_value = {"status": "success"}
        broker.ensure_session = MagicMock()
        with patch.object(config, "SYNC_BROKER_STOPS", True), patch.object(
            config, "SYNC_BROKER_STOP_RETRIES", 1
        ):
            ok = broker.sync_broker_stop(
                "RELIANCE",
                1321.82,
                prev_stop=1315.40,
                sl_order_id="321260818137101",
                qty=815,
            )
        self.assertTrue(ok)
        kwargs = broker.dhan.modify_order.call_args.kwargs
        self.assertEqual(kwargs.get("leg_name"), "ENTRY_LEG")
        self.assertEqual(int(kwargs.get("quantity") or 0), 815)
        self.assertGreater(float(kwargs.get("trigger_price") or 0), 0)

    def test_super_order_maps_camel_case_target(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.dhan = MagicMock()
        broker.ensure_session = MagicMock()
        broker._assert_live_allowed = MagicMock(return_value=True)

        def sdk_place(
            security_id,
            exchange_segment,
            transaction_type,
            quantity,
            order_type,
            product_type,
            price,
            targetPrice,
            stopLossPrice,
            trailingJump=0.0,
        ):
            self.assertGreater(float(targetPrice), 0)
            self.assertGreater(float(stopLossPrice), 0)
            return {"status": "success", "data": {"orderId": "super-99"}}

        broker.dhan.place_super_order = sdk_place
        with patch("dhan_broker._resolved_security_id", return_value=("2885", "NSE_EQ")):
            oid = broker.place_super_order(
                "RELIANCE", 10, 1321.10, 1332.0, 1315.40, product_type="INTRADAY"
            )
        self.assertEqual(oid, "super-99")

    def test_super_order_unknown_kwargs_falls_back_to_limit(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.dhan = MagicMock()
        broker.ensure_session = MagicMock()
        broker._assert_live_allowed = MagicMock(return_value=True)
        broker.place_stoploss_order = MagicMock(return_value="sl-1")

        def sdk_place(security_id, exchange_segment, transaction_type, quantity, price):
            raise AssertionError("should not be called without target mapping")

        broker.dhan.place_super_order = sdk_place
        broker.dhan.place_order.return_value = {
            "status": "success",
            "data": {"orderId": "limit-1"},
        }
        with patch("dhan_broker._resolved_security_id", return_value=("2885", "NSE_EQ")):
            oid = broker.place_super_order(
                "RELIANCE", 10, 1321.10, 1332.0, 1315.40, product_type="INTRADAY"
            )
        self.assertEqual(oid, "limit-1")
        broker.dhan.place_order.assert_called()
        broker.place_stoploss_order.assert_called()


class TestIndiaBuyLocked(unittest.TestCase):
    def test_no_ask_qty_blocks(self):
        locked, why = india_buy_locked("TCS", {"ltp": 100.0, "ask_qty": 0})
        self.assertTrue(locked)
        self.assertIn("no sellers", why)

    def test_near_upper_circuit_blocks(self):
        locked, why = india_buy_locked(
            "INFY", {"ltp": 1500.0, "upper_circuit": 1500.0}
        )
        self.assertTrue(locked)
        self.assertIn("upper circuit", why)

    def test_synthetic_ask_eq_ltp_does_not_block(self):
        locked, why = india_buy_locked(
            "SBIN", {"ltp": 800.0, "ask_price": 800.0, "source": "ticker_data"}
        )
        self.assertFalse(locked)
        self.assertEqual(why, "")

    def test_try_buy_skips_circuit(self):
        from main import _india_try_buy
        import bot_state
        import trade_journal

        bot_state.reset_tide_for_tests()
        risk = RiskManager(market="INDIA")
        broker = MagicMock()
        broker.get_latest_quote.return_value = {
            "ltp": 200.0,
            "upper_circuit": 200.0,
        }
        strat = MagicMock()
        strat.has_day_fired.return_value = False
        strat.name = "mis_regime"
        with patch.object(trade_journal, "has_entry_today", return_value=False), patch.object(
            trade_journal, "count_entries_today", return_value=0
        ), patch("main.order_guards") as og, patch("main.bot_state") as bs:
            bs.is_tide_bearish.return_value = False
            og.is_buy_blocked.return_value = (False, "")
            og.is_exit_pending_stuck.return_value = False
            og.is_exit_inflight.return_value = False
            ok = _india_try_buy(
                india_broker=broker,
                risk_mgr=risk,
                strategy=strat,
                symbol="WIPRO",
                snap={"signal": "BUY", "price": 200.0},
                atr=2.0,
                current_equity=300_000.0,
                account={"available_cash": 150_000.0},
                current_positions={},
                tradable_window=True,
                regime_ok=True,
            )
        self.assertFalse(ok)
        broker.place_buy_order.assert_not_called()


class TestZombiePlacesBrokerSl(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()
        reset_orb_fired_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.TRADE_JOURNAL_PATH
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "j.db")
        import trade_journal

        trade_journal.init_db()
        self.broker = DhanBroker.__new__(DhanBroker)
        self.broker.paper = None
        self.broker.dhan = MagicMock()
        self.broker.sl_tp_meta = {}
        self.broker._zombie_rescue_time = {}
        self.broker.close_position = MagicMock(return_value=None)
        self.broker._pending_bracket_ids = MagicMock(return_value=[])
        self.broker.place_stoploss_order = MagicMock(return_value="sl-oid-1")
        self.broker.save_sl_tp_meta = MagicMock()
        self.rm = RiskManager(market="INDIA")

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old
        reset_orb_fired_for_tests()
        order_guards.reset_for_tests()
        self._tmp.cleanup()

    def test_places_sl_when_naked(self):
        self.broker.get_open_positions = MagicMock(
            return_value={
                "INFY": {
                    "qty": 12,
                    "avg_entry_price": 1500.0,
                    "current_price": 1490.0,
                    "side": "BUY",
                }
            }
        )
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 1490.0})
        rescued = self.broker.rescue_zombie_positions(self.rm, None)
        self.assertEqual(rescued, ["INFY"])
        self.broker.place_stoploss_order.assert_called_once()
        self.assertEqual(self.broker.sl_tp_meta["INFY"].get("sl_order_id"), "sl-oid-1")
        self.assertEqual(self.rm._trade_meta["INFY"].get("sl_order_id"), "sl-oid-1")

    def test_skips_sl_when_brackets_already_pending(self):
        self.broker._pending_bracket_ids = MagicMock(return_value=["super-live"])
        self.broker.get_open_positions = MagicMock(
            return_value={
                "TCS": {"qty": 5, "avg_entry_price": 100.0, "current_price": 101.0}
            }
        )
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 101.0})
        rescued = self.broker.rescue_zombie_positions(self.rm, None)
        self.assertEqual(rescued, ["TCS"])
        self.broker.place_stoploss_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()

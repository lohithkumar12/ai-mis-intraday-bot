"""Tests for buy cooldown + order confirmation helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import order_guards
from dhan_broker import DhanBroker
from risk_manager import RiskManager


class TestOrderGuards(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()

    def test_reject_cooldown_blocks_then_expires(self):
        order_guards.block_buy("DIVISLAB", seconds=1.0, reason="test")
        blocked, reason = order_guards.is_buy_blocked("DIVISLAB")
        self.assertTrue(blocked)
        self.assertIn("test", reason)

        order_guards.block_buy("DIVISLAB", seconds=0.0, reason="gone")
        # until <= now → cleared on check
        import time

        time.sleep(0.02)
        blocked, _ = order_guards.is_buy_blocked("DIVISLAB")
        self.assertFalse(blocked)


class TestConfirmLiveOrder(unittest.TestCase):
    def test_reject_status_fails_and_cools_down(self):
        order_guards.reset_for_tests()
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.get_order_status = MagicMock(
            return_value=("REJECTED", "EXCH:16283 tick size")
        )
        ok, detail = broker.confirm_live_order("oid1", "DIVISLAB", timeout_sec=1.0)
        self.assertFalse(ok)
        self.assertIn("REJECTED", detail)
        blocked, _ = order_guards.is_buy_blocked("DIVISLAB")
        self.assertTrue(blocked)

    def test_traded_status_ok(self):
        order_guards.reset_for_tests()
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.last_error = ""
        broker.get_order_status = MagicMock(return_value=("TRADED", "TRADED"))
        ok, detail = broker.confirm_live_order("oid2", "INFY", timeout_sec=1.0)
        self.assertTrue(ok)
        self.assertEqual(detail, "TRADED")
        blocked, _ = order_guards.is_buy_blocked("INFY")
        self.assertFalse(blocked)


class TestIndiaRiskTick(unittest.TestCase):
    def test_india_sl_on_tick_for_high_price(self):
        rm = RiskManager(market="INDIA")
        sl = rm.get_stop_loss_price(8308.30, atr=50.0)
        # 2*ATR → 8208.30 → floor to 0.50 tick
        self.assertAlmostEqual(sl % 0.50, 0.0, places=6)
        self.assertLess(sl, 8308.30)


if __name__ == "__main__":
    unittest.main()

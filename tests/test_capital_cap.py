"""INDIA_CAPITAL_CAP / US_CAPITAL_CAP sizing + drawdown sleeve tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import bot_state
from risk_manager import RiskManager
import config


class TestIndiaCapitalCap(unittest.TestCase):
    def setUp(self):
        bot_state.reset_kill_for_tests()

    def test_effective_equity_caps(self):
        rm = RiskManager(market="INDIA")
        with patch.object(config, "INDIA_CAPITAL_CAP", 150_000):
            self.assertEqual(rm.effective_equity(300_000), 150_000)
            self.assertEqual(rm.effective_equity(100_000), 100_000)

    def test_sizing_uses_cap_not_full_book(self):
        rm = RiskManager(market="INDIA")
        # Isolate from .env overrides captured at RiskManager.__init__
        rm.risk_per_trade = 0.006
        rm.max_position_pct = 1.0
        with patch.object(config, "INDIA_CAPITAL_CAP", 150_000):
            with patch.object(config, "MAX_SHARES_PER_ORDER", 10_000):
                with patch.object(config, "MAX_POSITION_PCT", 1.0):
                    # risk 0.6% of 150k = 900; stop ₹10 → 90 shares
                    # if uncapped 300k → 180 shares
                    qty_capped = rm.calculate_position_size(
                        300_000, price=100.0, stop_distance=10.0
                    )
                    self.assertEqual(qty_capped, 90)

    def test_drawdown_vs_sleeve_base(self):
        rm = RiskManager(market="INDIA")
        with patch.object(config, "INDIA_CAPITAL_CAP", 150_000):
            # ₹3k loss on ₹3L book → 3000/150000 = 2%
            dd = rm.drawdown_vs_cap(297_000, 300_000)
            self.assertAlmostEqual(dd, 0.02, places=5)

    def test_zero_cap_disables_sleeve(self):
        rm = RiskManager(market="INDIA")
        with patch.object(config, "INDIA_CAPITAL_CAP", 0):
            self.assertEqual(rm.effective_equity(300_000), 300_000)

    def test_kill_switch_uses_mis_day_pl_not_equity(self):
        rm = RiskManager(market="INDIA")
        rm.daily_drawdown_limit = 0.02
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000):
                # Huge equity drop but small journal day P&L → must NOT trip
                tripped = rm.check_daily_drawdown(
                    326_000, 355_000, day_pl=-401.0
                )
                self.assertFalse(tripped)
                self.assertFalse(rm.is_kill_switch_active)

                # ₹6.5k journal loss on ₹3L sleeve = 2.17% → trip
                tripped2 = rm.check_daily_drawdown(
                    355_000, 355_000, day_pl=-6500.0
                )
                self.assertTrue(tripped2)
                self.assertTrue(rm.is_kill_switch_active)

                # Recovery under limit must NOT auto-clear daily_drawdown latch
                tripped3 = rm.check_daily_drawdown(
                    300_000, 355_000, day_pl=-500.0
                )
                self.assertTrue(tripped3)
                self.assertTrue(rm.is_kill_switch_active)
                # Explicit next-day reset clears it
                rm.reset_kill_switch()
                self.assertFalse(rm.is_kill_switch_active)

    def test_kill_switch_shared_loop_and_dashboard(self):
        rm_loop = RiskManager(market="INDIA")
        rm_dash = RiskManager(market="INDIA")
        rm_loop.activate_kill_switch("manual")
        self.assertTrue(rm_dash.is_kill_switch_active)
        rm_dash.reset_kill_switch()
        self.assertFalse(rm_loop.is_kill_switch_active)


class TestUSCapitalCap(unittest.TestCase):
    def setUp(self):
        bot_state.reset_kill_for_tests()

    def test_us_effective_equity_caps_at_500(self):
        rm = RiskManager(market="US")
        with patch.object(config, "US_CAPITAL_CAP", 500.0):
            self.assertEqual(rm.effective_equity(1000.0), 500.0)
            self.assertEqual(rm.effective_equity(400.0), 400.0)

    def test_us_cash_sizing_uses_cap_not_leverage(self):
        rm = RiskManager(market="US")
        rm.risk_per_trade = 0.01
        rm.max_position_pct = 0.80
        with patch.object(config, "US_CAPITAL_CAP", 500.0):
            with patch.object(config, "US_MAX_SHARES_PER_ORDER", 50):
                # risk 1% of $500 = $5; stop $1 → 5 shares
                # max_by_pct = 500*0.8/100 = 4 shares → final 4
                qty = rm.calculate_position_size(
                    2000.0, price=100.0, stop_distance=1.0
                )
                self.assertEqual(qty, 4)

    def test_us_drawdown_vs_sleeve(self):
        rm = RiskManager(market="US")
        rm.daily_drawdown_limit = 0.05
        with patch.object(config, "US_CAPITAL_CAP", 500.0):
            # $25 loss on $1000 book with $500 sleeve = 5% → trip
            tripped = rm.check_daily_drawdown(975.0, 1000.0)
            self.assertTrue(tripped)


if __name__ == "__main__":
    unittest.main()

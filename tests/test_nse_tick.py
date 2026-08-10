"""Tests for NSE equity tick rounding (Apr 2025 bands)."""

from __future__ import annotations

import unittest

from nse_tick import nse_equity_tick_size, round_buy_limit, round_sell_limit, round_to_nse_tick


class TestNseTick(unittest.TestCase):
    def test_tick_bands(self):
        self.assertEqual(nse_equity_tick_size(100), 0.01)
        self.assertEqual(nse_equity_tick_size(500), 0.05)
        self.assertEqual(nse_equity_tick_size(2000), 0.10)
        self.assertEqual(nse_equity_tick_size(8308.30), 0.50)
        self.assertEqual(nse_equity_tick_size(15000), 1.00)
        self.assertEqual(nse_equity_tick_size(25000), 5.00)

    def test_divislab_reject_case(self):
        # Exact exchange reject: 8308.30 is not a multiple of ₹0.50
        fixed = round_buy_limit(8308.30)
        self.assertEqual(fixed, 8308.5)
        self.assertAlmostEqual(fixed % 0.50, 0.0, places=6)

        fixed2 = round_buy_limit(8320.81)
        self.assertEqual(fixed2, 8321.0)
        self.assertAlmostEqual(fixed2 % 0.50, 0.0, places=6)

    def test_entry_bump_stays_on_tick(self):
        base = round_buy_limit(8308.30)
        bumped = round_buy_limit(base * 1.001)
        self.assertAlmostEqual(bumped % 0.50, 0.0, places=6)

    def test_sell_floor(self):
        self.assertEqual(round_sell_limit(8308.30), 8308.0)

    def test_already_on_tick(self):
        self.assertEqual(round_to_nse_tick(100.05, mode="nearest"), 100.05)
        self.assertEqual(round_to_nse_tick(8308.50, mode="nearest"), 8308.5)


if __name__ == "__main__":
    unittest.main()

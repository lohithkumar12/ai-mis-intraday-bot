"""INDIA_CAPITAL_CAP sizing + drawdown sleeve tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from risk_manager import RiskManager
import config


class TestIndiaCapitalCap(unittest.TestCase):
    def test_effective_equity_caps(self):
        rm = RiskManager(market="INDIA")
        with patch.object(config, "INDIA_CAPITAL_CAP", 150_000):
            self.assertEqual(rm.effective_equity(300_000), 150_000)
            self.assertEqual(rm.effective_equity(100_000), 100_000)

    def test_sizing_uses_cap_not_full_book(self):
        rm = RiskManager(market="INDIA")
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


if __name__ == "__main__":
    unittest.main()

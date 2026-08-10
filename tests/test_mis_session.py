"""MIS session cutoff / square-off helpers."""

from __future__ import annotations

import unittest
from datetime import datetime

from risk_manager import RiskManager


class TestMisSessionGates(unittest.TestCase):
    def test_entry_cutoff_blocks(self):
        rm = RiskManager(market="INDIA")
        # Monday-like datetime; cutoff default 14:45
        after = datetime(2026, 8, 10, 14, 45)
        before = datetime(2026, 8, 10, 14, 30)
        self.assertTrue(rm._past_entry_cutoff(after))
        self.assertFalse(rm._past_entry_cutoff(before))
        # Full session gate with India hours
        self.assertFalse(
            rm.is_tradable_session(after, market_open_hm=(9, 15), market_close_hm=(15, 30))
        )
        self.assertTrue(
            rm.is_tradable_session(before, market_open_hm=(9, 15), market_close_hm=(15, 30))
        )

    def test_squareoff_hm(self):
        rm = RiskManager(market="INDIA")
        hh, mm = rm.squareoff_hm()
        self.assertEqual((hh, mm), (15, 10))
        self.assertTrue(rm.past_squareoff(datetime(2026, 8, 10, 15, 10)))
        self.assertFalse(rm.past_squareoff(datetime(2026, 8, 10, 15, 0)))


if __name__ == "__main__":
    unittest.main()

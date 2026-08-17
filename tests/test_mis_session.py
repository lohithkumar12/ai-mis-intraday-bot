"""MIS session cutoff / square-off helpers."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

import config
from risk_manager import RiskManager


class TestMisSessionGates(unittest.TestCase):
    def test_entry_cutoff_blocks(self):
        rm = RiskManager(market="INDIA")
        # Default ENTRY_CUTOFF=14:15
        after = datetime(2026, 8, 10, 14, 15)
        before = datetime(2026, 8, 10, 14, 0)
        with patch.object(config, "ENTRY_CUTOFF", "14:15"):
            self.assertTrue(rm._past_entry_cutoff(after))
            self.assertFalse(rm._past_entry_cutoff(before))
            # Full session gate with India hours
            self.assertFalse(
                rm.is_tradable_session(
                    after, market_open_hm=(9, 15), market_close_hm=(15, 30)
                )
            )
            self.assertTrue(
                rm.is_tradable_session(
                    before, market_open_hm=(9, 15), market_close_hm=(15, 30)
                )
            )

    def test_squareoff_hm(self):
        rm = RiskManager(market="INDIA")
        with patch.object(config, "SQUAREOFF_TIME", "15:00"):
            hh, mm = rm.squareoff_hm()
            self.assertEqual((hh, mm), (15, 0))
            self.assertTrue(rm.past_squareoff(datetime(2026, 8, 10, 15, 0)))
            self.assertFalse(rm.past_squareoff(datetime(2026, 8, 10, 14, 59)))

    def test_squareoff_default_is_1455(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1].joinpath("config.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('SQUAREOFF_TIME", "14:55"', src)
        rm = RiskManager(market="INDIA")
        with patch.object(config, "SQUAREOFF_TIME", "14:55"):
            self.assertTrue(rm.past_squareoff(datetime(2026, 8, 10, 14, 55)))
            self.assertFalse(rm.past_squareoff(datetime(2026, 8, 10, 14, 54)))


if __name__ == "__main__":
    unittest.main()

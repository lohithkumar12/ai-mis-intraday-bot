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

    def test_us_entry_cutoff_uses_us_setting_not_india(self):
        rm = RiskManager(market="US")
        # 10:00 ET is before US_ENTRY_CUTOFF=15:15, even if India ENTRY_CUTOFF=14:15
        mid_morning = datetime(2026, 8, 20, 10, 0)
        late = datetime(2026, 8, 20, 15, 20)
        with patch.object(config, "ENTRY_CUTOFF", "14:15"), patch.object(
            config, "US_ENTRY_CUTOFF", "15:15"
        ), patch.object(config, "AVOID_OPEN_MINUTES", 0), patch.object(
            config, "AVOID_CLOSE_MINUTES", 45
        ), patch.object(config, "ALLOW_OPEN_CLOSE_WINDOW", False):
            self.assertFalse(rm._past_entry_cutoff(mid_morning))
            self.assertTrue(
                rm.is_tradable_session(
                    mid_morning, market_open_hm=(9, 30), market_close_hm=(16, 0)
                )
            )
            self.assertTrue(rm._past_entry_cutoff(late))
            self.assertFalse(
                rm.is_tradable_session(
                    late, market_open_hm=(9, 30), market_close_hm=(16, 0)
                )
            )

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

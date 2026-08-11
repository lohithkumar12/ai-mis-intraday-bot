"""Regression: sold-from-holding CNC rows must not count as open MIS trades."""

from __future__ import annotations

import unittest

from dhan_broker import should_include_open_position


class TestOpenPositionFilter(unittest.TestCase):
    def test_mis_skips_sold_from_holding_cnc(self):
        # Dhan "Sold From Holding" style: negative CNC netQty
        self.assertFalse(
            should_include_open_position(
                net_qty=-7, product="CNC", trading_is_mis=True
            )
        )
        self.assertFalse(
            should_include_open_position(
                net_qty=-21, product="DELIVERY", trading_is_mis=True
            )
        )

    def test_mis_keeps_intraday_long_and_short(self):
        self.assertTrue(
            should_include_open_position(
                net_qty=10, product="INTRADAY", trading_is_mis=True
            )
        )
        self.assertTrue(
            should_include_open_position(
                net_qty=-5, product="MIS", trading_is_mis=True
            )
        )
        self.assertTrue(
            should_include_open_position(
                net_qty=3, product="INTRA", trading_is_mis=True
            )
        )

    def test_mis_skips_cnc_long_and_flat(self):
        self.assertFalse(
            should_include_open_position(
                net_qty=50, product="CNC", trading_is_mis=True
            )
        )
        self.assertFalse(
            should_include_open_position(
                net_qty=0, product="INTRADAY", trading_is_mis=True
            )
        )
        self.assertFalse(
            should_include_open_position(
                net_qty=10, product="", trading_is_mis=True
            )
        )

    def test_cnc_mode_skips_negative_day_sells(self):
        self.assertFalse(
            should_include_open_position(
                net_qty=-7, product="CNC", trading_is_mis=False
            )
        )
        self.assertTrue(
            should_include_open_position(
                net_qty=7, product="CNC", trading_is_mis=False
            )
        )


if __name__ == "__main__":
    unittest.main()

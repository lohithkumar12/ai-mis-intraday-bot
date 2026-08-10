"""Regression: zero LTP must not trip stop-loss; backfill LTP for UI."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from dhan_broker import DhanBroker


class TestZeroLtpStopGuard(unittest.TestCase):
    def test_check_sl_tp_skips_zero_mark(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.paper = None
        broker.get_open_positions = MagicMock(
            return_value={
                "DIVISLAB": {
                    "qty": 2,
                    "avg_entry_price": 8290.0,
                    "current_price": 0.0,
                    "stop_loss": 8124.0,
                    "take_profit": 8705.0,
                }
            }
        )
        broker.close_position = MagicMock(return_value=True)
        risk = MagicMock()
        closed = broker.check_sl_tp(risk)
        self.assertEqual(closed, [])
        broker.close_position.assert_not_called()


class TestBackfillZeroLtp(unittest.TestCase):
    def test_backfill_from_quote(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.get_latest_quote = MagicMock(
            return_value={"ltp": 8287.5, "source": "ticker_data"}
        )
        pos = {
            "DIVISLAB": {
                "qty": 2,
                "avg_entry_price": 8287.25,
                "current_price": 0.0,
                "market_value": 0.0,
                "unrealized_pl": -13.5,
                "unrealized_plpc": -1.0,
            }
        }
        broker._backfill_zero_ltp_from_quotes(pos)
        row = pos["DIVISLAB"]
        self.assertEqual(row["current_price"], 8287.5)
        self.assertEqual(row["market_value"], 2 * 8287.5)
        self.assertAlmostEqual(row["unrealized_pl"], (8287.5 - 8287.25) * 2)
        self.assertAlmostEqual(row["unrealized_plpc"], (8287.5 - 8287.25) / 8287.25)
        self.assertEqual(row["ltp_source"], "ticker_data")

    def test_backfill_skips_when_ltp_present(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.get_latest_quote = MagicMock(return_value={"ltp": 9000.0})
        pos = {
            "RELIANCE": {
                "qty": 1,
                "avg_entry_price": 1400.0,
                "current_price": 1410.0,
                "market_value": 1410.0,
            }
        }
        broker._backfill_zero_ltp_from_quotes(pos)
        broker.get_latest_quote.assert_not_called()
        self.assertEqual(pos["RELIANCE"]["current_price"], 1410.0)

    def test_backfill_noop_when_quote_missing(self):
        broker = DhanBroker.__new__(DhanBroker)
        broker.get_latest_quote = MagicMock(return_value=None)
        pos = {
            "DIVISLAB": {
                "qty": 2,
                "avg_entry_price": 8287.25,
                "current_price": 0.0,
                "market_value": 0.0,
            }
        }
        broker._backfill_zero_ltp_from_quotes(pos)
        self.assertEqual(pos["DIVISLAB"]["current_price"], 0.0)


if __name__ == "__main__":
    unittest.main()

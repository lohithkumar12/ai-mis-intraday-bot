"""Today's P&L KPI = open unrealized only (Dhan-style; journal realized is debug-only)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import trade_journal
from dashboard_server import _broker_style_day_pl


class TestBrokerStyleDayPl(unittest.TestCase):
    def test_unrealized_from_marks(self):
        broker = MagicMock()
        broker.get_open_positions.return_value = {
            "DIVISLAB": {
                "qty": 2,
                "avg_entry_price": 8280.0,
                "current_price": 8287.0,
                "unrealized_pl": 0.0,
            }
        }
        with patch.object(trade_journal, "realized_pnl_today", return_value=0.0):
            day_pl, day_pct, detail = _broker_style_day_pl("INDIA", broker, 125_000.0)
        self.assertAlmostEqual(day_pl, 14.0)
        self.assertAlmostEqual(detail["unrealized"], 14.0)
        self.assertAlmostEqual(day_pct, 14.0 / (2 * 8280.0) * 100)

    def test_kpi_ignores_journal_realized(self):
        """Poisoned journal closes must not drag Today's P&L away from Dhan MTM."""
        broker = MagicMock()
        broker.get_open_positions.return_value = {
            "DIVISLAB": {
                "qty": 1,
                "avg_entry_price": 8280.0,
                "current_price": 8291.0,
            }
        }
        with patch.object(trade_journal, "realized_pnl_today", return_value=-23.5):
            day_pl, _, detail = _broker_style_day_pl("INDIA", broker, 100_000.0)
        self.assertAlmostEqual(day_pl, 11.0)
        self.assertAlmostEqual(detail["unrealized"], 11.0)
        self.assertAlmostEqual(detail["realized"], -23.5)

    def test_falls_back_to_unrealized_pl_when_ltp_missing(self):
        broker = MagicMock()
        broker.get_open_positions.return_value = {
            "RELIANCE": {
                "qty": 2,
                "avg_entry_price": 1000.0,
                "current_price": 0.0,
                "unrealized_pl": 7.5,
            }
        }
        with patch.object(trade_journal, "realized_pnl_today", return_value=0.0):
            day_pl, _, detail = _broker_style_day_pl("INDIA", broker, 50_000.0)
        self.assertAlmostEqual(day_pl, 7.5)
        self.assertAlmostEqual(detail["unrealized"], 7.5)


class TestRealizedPnlToday(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.TRADE_JOURNAL_PATH
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "journal.db")
        trade_journal.init_db()

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old
        self._tmp.cleanup()

    def test_sums_today_and_skips_zero_exit(self):
        trade_journal.record_entry("INDIA", "AAA", 1, 100.0)
        trade_journal.record_exit("INDIA", "AAA", 110.0, reason="tp")

        trade_journal.record_entry("INDIA", "BBB", 1, 100.0)
        # Bogus zero exit should be ignored by realized_pnl_today
        with trade_journal._lock, trade_journal._conn() as conn:
            conn.execute(
                """
                UPDATE trades SET exit_price=0, pnl=-100, status='closed',
                closed_at=? WHERE symbol='BBB'
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )

        total = trade_journal.realized_pnl_today("INDIA", tz_name="Asia/Kolkata")
        self.assertAlmostEqual(total, 10.0)


if __name__ == "__main__":
    unittest.main()

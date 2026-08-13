"""Nifty/BankNifty 5m tide circuit breaker — blocks new entries only."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import bot_state
import config
import filters
from filters import (
    TIDE_LOCK_REASON,
    apply_tide_lock_to_signal,
    market_tide_filter,
    _index_5m_pct_change,
)


def _bars(prev: float, cur: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [prev, cur],
            "high": [prev, cur],
            "low": [prev, cur],
            "close": [prev, cur],
            "volume": [1e6, 1e6],
        }
    )


class TestIndex5mPct(unittest.TestCase):
    def test_pct_from_last_two_closes(self):
        df = _bars(100.0, 99.30)  # -0.70%
        self.assertAlmostEqual(_index_5m_pct_change(df), -0.70, places=4)

    def test_needs_two_bars(self):
        df = pd.DataFrame({"close": [100.0]})
        self.assertIsNone(_index_5m_pct_change(df))


class TestMarketTideFilter(unittest.TestCase):
    def setUp(self):
        bot_state.reset_tide_for_tests()
        filters.TIDE_BEARISH = False

    def tearDown(self):
        bot_state.reset_tide_for_tests()
        filters.TIDE_BEARISH = False

    def _broker(self, nifty_df, bank_df):
        broker = MagicMock()

        def _bars_for(symbol, days=3, interval=5):
            return nifty_df if str(symbol).upper() == "NIFTY" else bank_df

        broker.get_historical_bars.side_effect = _bars_for
        return broker

    def test_locks_when_nifty_dumps(self):
        broker = self._broker(_bars(25000.0, 24830.0), _bars(52000.0, 52050.0))  # -0.68%
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            locked = market_tide_filter(broker)
        self.assertTrue(locked)
        self.assertTrue(filters.TIDE_BEARISH)
        self.assertTrue(bot_state.is_tide_bearish())
        self.assertEqual(bot_state.get_tide_state()["reason"], TIDE_LOCK_REASON)

    def test_locks_when_banknifty_dumps(self):
        broker = self._broker(_bars(25000.0, 25010.0), _bars(52000.0, 51650.0))  # -0.67%
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            self.assertTrue(market_tide_filter(broker))

    def test_trigger_is_inclusive(self):
        # exactly -0.65%
        broker = self._broker(_bars(10000.0, 9935.0), _bars(10000.0, 10000.0))
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            with patch.object(config, "TIDE_TRIGGER_PCT", -0.65):
                self.assertTrue(market_tide_filter(broker))

    def test_quiet_tape_stays_unlocked(self):
        broker = self._broker(_bars(25000.0, 24980.0), _bars(52000.0, 51950.0))  # ~-0.08/-0.10
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            self.assertFalse(market_tide_filter(broker))
        self.assertFalse(filters.TIDE_BEARISH)

    def test_hysteresis_needs_both_above_clear(self):
        dump = self._broker(_bars(25000.0, 24800.0), _bars(52000.0, 52000.0))
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            self.assertTrue(market_tide_filter(dump))
            # Nifty still -0.30%, Bank recovered — stay locked
            half = self._broker(_bars(25000.0, 24925.0), _bars(52000.0, 52100.0))
            self.assertTrue(market_tide_filter(half))
            # Both > -0.20% → unlock
            clear = self._broker(_bars(25000.0, 24960.0), _bars(52000.0, 51950.0))
            self.assertFalse(market_tide_filter(clear))
        self.assertFalse(bot_state.is_tide_bearish())

    def test_exactly_clear_threshold_stays_locked(self):
        dump = self._broker(_bars(10000.0, 9930.0), _bars(10000.0, 10000.0))
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            self.assertTrue(market_tide_filter(dump))
            # both exactly -0.20% is NOT greater than -0.20
            edge = self._broker(_bars(10000.0, 9980.0), _bars(10000.0, 9980.0))
            self.assertTrue(market_tide_filter(edge))

    def test_missing_data_does_not_false_lock(self):
        broker = MagicMock()
        broker.get_historical_bars.return_value = None
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            self.assertFalse(market_tide_filter(broker))

    def test_missing_data_does_not_unlock(self):
        dump = self._broker(_bars(25000.0, 24800.0), _bars(52000.0, 52000.0))
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            self.assertTrue(market_tide_filter(dump))
            dead = MagicMock()
            dead.get_historical_bars.return_value = None
            self.assertTrue(market_tide_filter(dead))

    def test_disabled_clears_latch(self):
        dump = self._broker(_bars(25000.0, 24800.0), _bars(52000.0, 51600.0))
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            self.assertTrue(market_tide_filter(dump))
        with patch.object(config, "TIDE_FILTER_ENABLED", False):
            self.assertFalse(market_tide_filter(dump))
        self.assertFalse(filters.TIDE_BEARISH)

    def test_signal_reason_is_tide_lock(self):
        snap = apply_tide_lock_to_signal(
            {"symbol": "INFY", "signal": "BUY", "reason": "OPEN_DRIVE orb"}
        )
        self.assertEqual(snap["signal"], "HOLD")
        self.assertEqual(snap["reason"], "TIDE LOCK")

    def test_does_not_touch_positions(self):
        broker = self._broker(_bars(25000.0, 24800.0), _bars(52000.0, 51600.0))
        broker.get_open_positions = MagicMock()
        broker.square_off_intraday_positions = MagicMock()
        with patch.object(config, "TIDE_FILTER_ENABLED", True):
            market_tide_filter(broker)
        broker.get_open_positions.assert_not_called()
        broker.square_off_intraday_positions.assert_not_called()


class TestIndiaTryBuyTideGuard(unittest.TestCase):
    def setUp(self):
        bot_state.reset_tide_for_tests()

    def tearDown(self):
        bot_state.reset_tide_for_tests()

    def test_try_buy_skipped_when_tide_locked(self):
        from main import _india_try_buy

        bot_state.set_tide_state(bearish=True, reason=TIDE_LOCK_REASON)
        risk = MagicMock()
        risk.is_kill_switch_active = False
        ok = _india_try_buy(
            india_broker=MagicMock(),
            risk_mgr=risk,
            strategy=MagicMock(),
            symbol="INFY",
            snap={"signal": "BUY", "price": 1500.0},
            atr=10.0,
            current_equity=150000.0,
            account={"available_cash": 150000.0},
            current_positions={},
            tradable_window=True,
            regime_ok=True,
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()

"""Second ORB BUY after day-fired: +0.5% above prior 5m high and higher volume."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import config
import trade_journal
from strategy import (
    PLAYBOOK_ORB,
    OpeningRangeBreakoutStrategy,
    _normalize_fired,
    _orb_fired,
    create_strategy,
    params_for_market,
    reset_orb_fired_for_tests,
    snapshot_signal,
)

IST = ZoneInfo("Asia/Kolkata")


def _orb_breakout_df(extra_close=None, extra_high=None, extra_vol=None):
    """OR 09:15–09:30 (high=100), then breakout closes above ORH."""
    now = datetime.now(IST)
    today = (
        now.date()
        if (now.hour * 60 + now.minute) >= 10 * 60
        else (now.date() - timedelta(days=1))
    )
    yesterday = today - timedelta(days=1)
    times = [
        datetime(today.year, today.month, today.day, 9, 15, tzinfo=IST),
        datetime(today.year, today.month, today.day, 9, 20, tzinfo=IST),
        datetime(today.year, today.month, today.day, 9, 25, tzinfo=IST),
        datetime(today.year, today.month, today.day, 9, 30, tzinfo=IST),
        datetime(today.year, today.month, today.day, 9, 35, tzinfo=IST),
    ]
    close = [99.0, 99.5, 100.0, 101.0, 101.5]
    if extra_close is not None:
        times.append(datetime(today.year, today.month, today.day, 9, 40, tzinfo=IST))
        close.append(float(extra_close))
    pre = [
        datetime(yesterday.year, yesterday.month, yesterday.day, 12, 0, tzinfo=IST)
        + timedelta(minutes=5 * i)
        for i in range(40)
    ]
    idx = pre + times
    n = len(idx)
    high = [c + 0.2 for c in close]
    high = [100.0, 100.0, 100.0] + high[3:]
    if extra_high is not None:
        high[-1] = float(extra_high)
    # prepend warmup highs
    high = [99.2] * 40 + high
    full_close = [99.0] * 40 + close
    low = [c - 0.2 for c in full_close]
    vol = [2_000_000.0] * n
    if extra_vol is not None:
        vol[-1] = float(extra_vol)
    return pd.DataFrame(
        {
            "open": full_close,
            "high": high,
            "low": low,
            "close": full_close,
            "volume": vol,
        },
        index=pd.DatetimeIndex(idx),
    )


class TestOrbContinuation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = {
            "journal": config.TRADE_JOURNAL_PATH,
            "htf": config.ORB_USE_HTF_FILTER,
            "vol": config.VOLUME_MULT,
            "confirm": config.CONFIRM_BARS,
            "enabled": getattr(config, "ORB_CONTINUATION_ENABLED", True),
            "pct": getattr(config, "ORB_CONTINUATION_BREAK_PCT", 0.005),
            "max_n": getattr(config, "ORB_CONTINUATION_MAX", 2),
        }
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "j.db")
        config.ORB_USE_HTF_FILTER = False
        config.VOLUME_MULT = 0.0
        config.CONFIRM_BARS = 1
        config.ORB_CONTINUATION_ENABLED = True
        config.ORB_CONTINUATION_BREAK_PCT = 0.005
        config.ORB_CONTINUATION_MAX = 2
        reset_orb_fired_for_tests()

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old["journal"]
        config.ORB_USE_HTF_FILTER = self._old["htf"]
        config.VOLUME_MULT = self._old["vol"]
        config.CONFIRM_BARS = self._old["confirm"]
        config.ORB_CONTINUATION_ENABLED = self._old["enabled"]
        config.ORB_CONTINUATION_BREAK_PCT = self._old["pct"]
        config.ORB_CONTINUATION_MAX = self._old["max_n"]
        reset_orb_fired_for_tests()
        self._tmp.cleanup()

    def _strat(self) -> OpeningRangeBreakoutStrategy:
        strat = OpeningRangeBreakoutStrategy(params_for_market("INDIA"))
        strat.use_htf = False
        strat.volume_mult = 0.0
        strat.confirm_bars = 1
        return strat

    def _fire_first(self, strat, df):
        df = strat.compute_indicators(df)
        self.assertEqual(strat.generate_signal(df, "RELIANCE"), "BUY")
        strat.mark_day_fired("RELIANCE", "BUY")
        self.assertTrue(strat.has_day_fired("RELIANCE", "BUY"))
        return df

    def test_same_bars_after_fill_still_hold(self):
        strat = self._strat()
        df = self._fire_first(strat, _orb_breakout_df())
        self.assertEqual(strat.generate_signal(df, "RELIANCE"), "HOLD")
        self.assertFalse(strat.last_orb_continuation)

    def test_continuation_buy_when_break_and_higher_volume(self):
        strat = self._strat()
        self._fire_first(strat, _orb_breakout_df())
        # Prior completed high is 101.7; need close >= 101.7 * 1.005 = 102.2085
        df = strat.compute_indicators(
            _orb_breakout_df(extra_close=102.30, extra_high=102.50, extra_vol=3_000_000)
        )
        sig = strat.generate_signal(df, "RELIANCE")
        self.assertEqual(sig, "BUY")
        self.assertTrue(strat.last_orb_continuation)
        snap = snapshot_signal(strat, df, "RELIANCE")
        self.assertTrue(snap.get("orb_continuation"))
        self.assertEqual(snap.get("signal"), "BUY")

    def test_hold_when_break_but_volume_not_higher(self):
        strat = self._strat()
        self._fire_first(strat, _orb_breakout_df())
        df = strat.compute_indicators(
            _orb_breakout_df(extra_close=102.30, extra_high=102.50, extra_vol=1_500_000)
        )
        self.assertEqual(strat.generate_signal(df, "RELIANCE"), "HOLD")
        self.assertFalse(strat.last_orb_continuation)

    def test_hold_when_volume_equal(self):
        strat = self._strat()
        self._fire_first(strat, _orb_breakout_df())
        df = strat.compute_indicators(
            _orb_breakout_df(extra_close=102.30, extra_high=102.50, extra_vol=2_000_000)
        )
        self.assertEqual(strat.generate_signal(df, "RELIANCE"), "HOLD")

    def test_hold_when_no_half_pct_break(self):
        strat = self._strat()
        self._fire_first(strat, _orb_breakout_df())
        df = strat.compute_indicators(
            _orb_breakout_df(extra_close=101.80, extra_high=102.00, extra_vol=3_000_000)
        )
        self.assertEqual(strat.generate_signal(df, "RELIANCE"), "HOLD")

    def test_second_fill_locks_again(self):
        strat = self._strat()
        self._fire_first(strat, _orb_breakout_df())
        df = strat.compute_indicators(
            _orb_breakout_df(extra_close=102.30, extra_high=102.50, extra_vol=3_000_000)
        )
        self.assertEqual(strat.generate_signal(df, "RELIANCE"), "BUY")
        strat.mark_day_fired("RELIANCE", "BUY", continuation=True)
        self.assertEqual(strat.generate_signal(df, "RELIANCE"), "HOLD")
        rec = _normalize_fired(next(iter(_orb_fired.values())))
        self.assertEqual(rec["count"], 2)

    def test_idempotent_remark_does_not_consume_continuation(self):
        strat = self._strat()
        self._fire_first(strat, _orb_breakout_df())
        strat.mark_day_fired("RELIANCE", "BUY")
        strat.mark_day_fired("RELIANCE", "BUY")
        rec = _normalize_fired(next(iter(_orb_fired.values())))
        self.assertEqual(rec["count"], 1)
        df = strat.compute_indicators(
            _orb_breakout_df(extra_close=102.30, extra_high=102.50, extra_vol=3_000_000)
        )
        self.assertEqual(strat.generate_signal(df, "RELIANCE"), "BUY")

    def test_legacy_string_payload_keeps_hold_without_first_vol(self):
        strat = self._strat()
        key = strat._fire_key("RELIANCE")
        path = Path(self._tmp.name) / "orb_day_fired.json"
        path.write_text(json.dumps({key: "BUY"}), encoding="utf-8")
        reset_orb_fired_for_tests()
        strat2 = self._strat()
        self.assertTrue(strat2.has_day_fired("RELIANCE", "BUY"))
        df = strat2.compute_indicators(
            _orb_breakout_df(extra_close=102.30, extra_high=102.50, extra_vol=3_000_000)
        )
        self.assertEqual(strat2.generate_signal(df, "RELIANCE"), "HOLD")

    def test_mis_regime_continuation_outside_open_drive(self):
        strat = create_strategy("INDIA", name="mis_regime")
        strat._orb.use_htf = False
        strat._orb.volume_mult = 0.0
        strat._orb.confirm_bars = 1
        df = strat._orb.compute_indicators(_orb_breakout_df())
        self.assertEqual(strat._orb.generate_signal(df, "RELIANCE"), "BUY")
        strat.mark_day_fired("RELIANCE", "BUY")
        df2 = strat.compute_indicators(
            _orb_breakout_df(extra_close=102.30, extra_high=102.50, extra_vol=3_000_000)
        )
        session_day = df2.index[-1].date()
        now_after = datetime(
            session_day.year, session_day.month, session_day.day, 11, 0, tzinfo=IST
        )
        with patch("strategy.now_ist", return_value=now_after), patch.object(
            config, "LOSS_REENTRY_COOLDOWN_MIN", 0
        ), patch.object(config, "ORB_USE_HTF_FILTER", False):
            sig2 = strat.generate_signal(df2, "RELIANCE")
        self.assertEqual(sig2, "BUY")
        self.assertTrue(strat.last_decision.get("orb_continuation"))
        self.assertEqual(strat.last_decision.get("playbook"), PLAYBOOK_ORB)


class TestIndiaTryBuyOrbContinuation(unittest.TestCase):
    def _run(self, continuation: bool, journal: bool = True, fired: bool = True):
        from main import _india_try_buy
        import bot_state

        bot_state.reset_tide_for_tests()
        risk = MagicMock()
        risk.is_kill_switch_active = False
        risk.check_max_trades_per_day.return_value = (True, "")
        risk.effective_equity.side_effect = lambda x: x
        risk.get_stop_loss_price.return_value = 1490.0
        risk.get_take_profit_price.return_value = 1520.0
        risk.passes_cost_floor.return_value = (True, "")
        risk.calculate_position_size.return_value = 10
        risk.is_position_allowed.return_value = True
        risk.can_add_trade_risk.return_value = (True, "")

        broker = MagicMock()
        broker.get_latest_quote.return_value = {"ltp": 1500.0}
        broker.place_buy_order.return_value = None
        broker.last_error = "test_stop"

        strat = MagicMock()
        strat.has_day_fired.return_value = fired
        strat.name = "opening_range_breakout"

        with patch.object(trade_journal, "has_entry_today", return_value=journal), patch(
            "main.order_guards"
        ) as og, patch("main.bot_state") as bs:
            bs.is_tide_bearish.return_value = False
            og.is_buy_blocked.return_value = (False, "")
            og.try_reserve_buy.return_value = (True, "")
            ok = _india_try_buy(
                india_broker=broker,
                risk_mgr=risk,
                strategy=strat,
                symbol="INFY",
                snap={
                    "signal": "BUY",
                    "price": 1500.0,
                    "orb_continuation": continuation,
                },
                atr=10.0,
                current_equity=150000.0,
                account={"available_cash": 150000.0},
                current_positions={},
                tradable_window=True,
                regime_ok=True,
            )
        return ok, broker

    def test_continuation_bypasses_journal_and_day_fired(self):
        ok, broker = self._run(continuation=True)
        self.assertFalse(ok)  # order itself is mocked to fail
        broker.place_buy_order.assert_called()

    def test_without_flag_journal_still_blocks(self):
        ok, broker = self._run(continuation=False)
        self.assertFalse(ok)
        broker.place_buy_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()

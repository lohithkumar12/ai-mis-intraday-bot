"""Tests for mis_regime XOR selector, VWAP, regimes, and factory aliases."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config
from strategy import (
    PLAYBOOK_MOMENTUM,
    PLAYBOOK_MR,
    PLAYBOOK_NONE,
    PLAYBOOK_ORB,
    PLAYBOOK_PULLBACK,
    REGIME_OPEN_DRIVE,
    REGIME_RANGE,
    REGIME_TREND_DOWN,
    REGIME_TREND_UP,
    MisRegimeStrategy,
    OpeningRangeBreakoutStrategy,
    calc_session_vwap,
    completed_bars_only,
    create_strategy,
    in_open_drive_window,
    params_for_market,
    reset_orb_fired_for_tests,
)

IST = ZoneInfo("Asia/Kolkata")
NOW_NOON = datetime(2026, 8, 12, 12, 0, tzinfo=IST)
NOW_OPEN = datetime(2026, 8, 12, 9, 45, tzinfo=IST)
NOW_AFTER_WINDOW = datetime(2026, 8, 12, 11, 0, tzinfo=IST)
NOW_CUTOFF = datetime(2026, 8, 12, 14, 15, tzinfo=IST)


def _session_df(
    n: int = 48,
    start: datetime | None = None,
    close: np.ndarray | None = None,
    volume: float = 1_000_000.0,
    wide_last: bool = False,
    warmup: int = 40,
) -> pd.DataFrame:
    """Prior-day warmup bars + today's 09:15 session so OPEN_DRIVE still has ADX history."""
    today_start = start or datetime(2026, 8, 12, 9, 15, tzinfo=IST)
    pre_start = datetime(2026, 8, 11, 10, 0, tzinfo=IST)
    pre = [pre_start + timedelta(minutes=5 * i) for i in range(warmup)]
    today = [today_start + timedelta(minutes=5 * i) for i in range(n)]
    idx = pd.DatetimeIndex(pre + today)
    total = len(idx)
    if close is None:
        close = np.linspace(100.0, 110.0, total)
    close = np.asarray(close, dtype=float)
    if len(close) != total:
        close = np.linspace(float(close[0]), float(close[-1]), total)
    high = close * 1.004
    low = close * 0.996
    if wide_last:
        high[-1] = close[-1] * 1.03
        low[-1] = close[-1] * 0.97
    vol = np.full(total, float(volume))
    return pd.DataFrame(
        {
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
        },
        index=idx,
    )


def _ready_strat(df: pd.DataFrame | None = None) -> tuple[MisRegimeStrategy, pd.DataFrame]:
    strat = create_strategy("INDIA", name="mis_regime")
    assert isinstance(strat, MisRegimeStrategy)
    if df is None:
        df = _session_df()
    df = strat.compute_indicators(df)
    return strat, df


class TestSessionVwap(unittest.TestCase):
    def test_exact_cumulative_vwap(self):
        df = pd.DataFrame(
            {
                "high": [10.0, 12.0, 11.0],
                "low": [8.0, 10.0, 9.0],
                "close": [9.0, 11.0, 10.0],
                "volume": [100.0, 200.0, 100.0],
            }
        )
        vwap = calc_session_vwap(df)
        # tp = [9, 11, 10]; tpv = [900, 2200, 1000]
        self.assertAlmostEqual(float(vwap.iloc[0]), 9.0)
        self.assertAlmostEqual(float(vwap.iloc[1]), 3100.0 / 300.0)
        self.assertAlmostEqual(float(vwap.iloc[2]), 4100.0 / 400.0)

    def test_session_vwap_ignores_preopen(self):
        idx = pd.DatetimeIndex(
            [
                datetime(2026, 8, 12, 9, 5, tzinfo=IST),
                datetime(2026, 8, 12, 9, 15, tzinfo=IST),
                datetime(2026, 8, 12, 9, 20, tzinfo=IST),
                datetime(2026, 8, 12, 9, 25, tzinfo=IST),
            ]
        )
        df = pd.DataFrame(
            {
                "high": [10.0, 12.0, 14.0, 16.0],
                "low": [8.0, 10.0, 12.0, 14.0],
                "close": [9.0, 11.0, 13.0, 15.0],
                "volume": [100.0, 100.0, 100.0, 100.0],
            },
            index=idx,
        )
        vwap = calc_session_vwap(df)
        self.assertTrue(pd.isna(vwap.iloc[0]))  # 09:05
        self.assertFalse(pd.isna(vwap.iloc[1]))  # 09:15


class TestXorAndPrecedence(unittest.TestCase):
    def setUp(self):
        reset_orb_fired_for_tests()

    def test_xor_momentum_and_mean_reversion_one_buy(self):
        strat, df = _ready_strat()
        with patch("strategy.now_ist", return_value=NOW_AFTER_WINDOW), patch.object(
            config, "ORB_USE_HTF_FILTER", False
        ), patch.object(config, "LOSS_REENTRY_COOLDOWN_MIN", 0), patch.object(
            strat, "classify_regime", return_value=(REGIME_TREND_UP, "forced")
        ), patch.object(
            strat, "_trend_up_clear", return_value=True
        ), patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ) as mom, patch.object(
            strat, "_playbook_ema_pullback", return_value=("BUY", "pb")
        ) as pb, patch.object(
            strat, "_playbook_vwap_mr", return_value=("BUY", "mr")
        ) as mr, patch.object(
            strat, "_playbook_orb", return_value=("BUY", "orb")
        ) as orb:
            sig = strat.generate_signal(df, "INFY")
        self.assertEqual(sig, "BUY")
        self.assertEqual(strat.last_decision["playbook"], PLAYBOOK_MOMENTUM)
        buys = [
            e
            for e in strat.last_decision["evaluations"]
            if e["signal"] == "BUY"
        ]
        self.assertEqual(len(buys), 1)
        mom.assert_called()
        pb.assert_not_called()
        mr.assert_not_called()
        orb.assert_not_called()

    def test_trend_up_momentum_blocks_pullback(self):
        strat, df = _ready_strat()
        with patch("strategy.now_ist", return_value=NOW_AFTER_WINDOW), patch.object(
            config, "ORB_USE_HTF_FILTER", False
        ), patch.object(config, "LOSS_REENTRY_COOLDOWN_MIN", 0), patch.object(
            strat, "classify_regime", return_value=(REGIME_TREND_UP, "forced")
        ), patch.object(
            strat, "_trend_up_clear", return_value=True
        ), patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ), patch.object(
            strat, "_playbook_ema_pullback", return_value=("BUY", "pb")
        ) as pb:
            sig = strat.generate_signal(df, "INFY")
        self.assertEqual(sig, "BUY")
        self.assertEqual(strat.last_decision["playbook"], PLAYBOOK_MOMENTUM)
        pb.assert_not_called()

    def test_one_order_decision_per_symbol_cycle(self):
        strat, df = _ready_strat()
        with patch("strategy.now_ist", return_value=NOW_AFTER_WINDOW), patch.object(
            config, "ORB_USE_HTF_FILTER", False
        ), patch.object(config, "LOSS_REENTRY_COOLDOWN_MIN", 0), patch.object(
            strat, "classify_regime", return_value=(REGIME_RANGE, "forced")
        ), patch.object(
            strat, "_playbook_vwap_mr", return_value=("BUY", "mr")
        ), patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ) as mom, patch.object(
            strat, "_playbook_orb", return_value=("BUY", "orb")
        ) as orb:
            sig = strat.generate_signal(df, "SBIN")
        self.assertEqual(sig, "BUY")
        self.assertEqual(strat.last_decision["playbook"], PLAYBOOK_MR)
        self.assertEqual(
            sum(1 for e in strat.last_decision["evaluations"] if e["signal"] == "BUY"),
            1,
        )
        mom.assert_not_called()
        orb.assert_not_called()


class TestOpenDrive(unittest.TestCase):
    def test_window_helper(self):
        self.assertTrue(in_open_drive_window(NOW_OPEN, 60))
        self.assertFalse(in_open_drive_window(NOW_AFTER_WINDOW, 60))
        self.assertFalse(in_open_drive_window(datetime(2026, 8, 12, 9, 14, tzinfo=IST), 60))

    def test_inside_window_orb_may_run(self):
        df = _session_df(wide_last=True)
        # Rising ADX so OPEN_DRIVE expansion is true
        strat, df = _ready_strat(df)
        df["ADX"] = np.linspace(10, 30, len(df))
        with patch("strategy.now_ist", return_value=NOW_OPEN), patch.object(
            config, "ORB_USE_HTF_FILTER", False
        ), patch.object(config, "LOSS_REENTRY_COOLDOWN_MIN", 0), patch.object(
            strat, "_playbook_orb", return_value=("BUY", "orb")
        ) as orb, patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ) as mom:
            sig = strat.generate_signal(df, "INFY")
        self.assertEqual(sig, "BUY")
        self.assertEqual(strat.last_decision["regime"], REGIME_OPEN_DRIVE)
        self.assertEqual(strat.last_decision["playbook"], PLAYBOOK_ORB)
        orb.assert_called()
        mom.assert_not_called()

    def test_outside_window_orb_must_not_run(self):
        strat, df = _ready_strat(_session_df(wide_last=True))
        df["ADX"] = np.linspace(10, 30, len(df))
        with patch("strategy.now_ist", return_value=NOW_AFTER_WINDOW), patch.object(
            config, "ORB_USE_HTF_FILTER", False
        ), patch.object(config, "LOSS_REENTRY_COOLDOWN_MIN", 0), patch.object(
            strat, "classify_regime", return_value=(REGIME_TREND_UP, "forced")
        ), patch.object(
            strat, "_trend_up_clear", return_value=True
        ), patch.object(
            strat, "_playbook_orb", return_value=("BUY", "orb")
        ) as orb, patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("HOLD", "no")
        ), patch.object(
            strat, "_playbook_ema_pullback", return_value=("HOLD", "no")
        ):
            sig = strat.generate_signal(df, "LT")
        self.assertEqual(sig, "HOLD")
        orb.assert_not_called()

    def test_open_drive_orb_fail_no_automatic_fallthrough(self):
        strat, df = _ready_strat(_session_df(wide_last=True))
        df["ADX"] = np.linspace(10, 30, len(df))
        with patch("strategy.now_ist", return_value=NOW_OPEN), patch.object(
            config, "ORB_USE_HTF_FILTER", False
        ), patch.object(config, "LOSS_REENTRY_COOLDOWN_MIN", 0), patch.object(
            strat, "_trend_up_clear", return_value=False
        ), patch.object(
            strat, "_playbook_orb", return_value=("HOLD", "no_orb")
        ), patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ) as mom, patch.object(
            strat, "_playbook_vwap_mr", return_value=("BUY", "mr")
        ) as mr, patch.object(
            strat, "_playbook_ema_pullback", return_value=("BUY", "pb")
        ) as pb:
            sig = strat.generate_signal(df, "SBIN")
        self.assertEqual(sig, "HOLD")
        self.assertEqual(strat.last_decision["regime"], REGIME_OPEN_DRIVE)
        self.assertEqual(strat.last_decision["playbook"], PLAYBOOK_NONE)
        mom.assert_not_called()
        mr.assert_not_called()
        pb.assert_not_called()


class TestRangeAndTrendDown(unittest.TestCase):
    def test_range_no_orb_momentum_chase(self):
        strat, df = _ready_strat()
        with patch("strategy.now_ist", return_value=NOW_AFTER_WINDOW), patch.object(
            config, "ORB_USE_HTF_FILTER", False
        ), patch.object(config, "LOSS_REENTRY_COOLDOWN_MIN", 0), patch.object(
            strat, "classify_regime", return_value=(REGIME_RANGE, "low_adx")
        ), patch.object(
            strat, "_playbook_orb", return_value=("BUY", "orb")
        ) as orb, patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ) as mom, patch.object(
            strat, "_playbook_vwap_mr", return_value=("HOLD", "weak")
        ):
            sig = strat.generate_signal(df, "ITC")
        self.assertEqual(sig, "HOLD")
        self.assertEqual(strat.last_decision["playbook"], PLAYBOOK_NONE)
        orb.assert_not_called()
        mom.assert_not_called()

    def test_trend_down_no_long_buy(self):
        strat, df = _ready_strat()
        with patch("strategy.now_ist", return_value=NOW_AFTER_WINDOW), patch.object(
            config, "LOSS_REENTRY_COOLDOWN_MIN", 0
        ), patch.object(
            strat, "classify_regime", return_value=(REGIME_TREND_DOWN, "forced")
        ), patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ) as mom, patch.object(
            strat, "_playbook_vwap_mr", return_value=("BUY", "mr")
        ) as mr, patch.object(
            strat, "_playbook_orb", return_value=("BUY", "orb")
        ) as orb:
            sig = strat.generate_signal(df, "SBIN")
        self.assertEqual(sig, "HOLD")
        self.assertEqual(strat.last_decision["regime"], REGIME_TREND_DOWN)
        self.assertEqual(strat.last_decision["playbook"], PLAYBOOK_NONE)
        mom.assert_not_called()
        mr.assert_not_called()
        orb.assert_not_called()


class TestRelativeStrengthPlaybook(unittest.TestCase):
    def test_non_leader_rejected_leader_can_pass(self):
        strat, df = _ready_strat()
        n = len(df)
        leader = df.copy()
        leader["close"] = np.linspace(100, 150, n)
        laggard = df.copy()
        laggard["close"] = np.linspace(100, 90, n)
        mid = df.copy()
        strat.update_universe({"LEADER": leader, "LAGGARD": laggard, "INFY": mid})
        strat._rs.top_n = 1
        self.assertFalse(strat._rs_is_leader("LAGGARD"))
        self.assertTrue(strat._rs_is_leader("LEADER"))
        work = df.copy()
        prior_high = float(work["high"].iloc[:-1].max())
        work.iloc[-1, work.columns.get_loc("close")] = prior_high + 1.0
        work.iloc[-1, work.columns.get_loc("high")] = prior_high + 1.2
        work["VOL_AVG"] = 1_000.0
        work.iloc[-1, work.columns.get_loc("volume")] = 1_000_000.0
        hold_sig, hold_why = strat._playbook_vwap_momentum_rs(work, "LAGGARD", vwap=50.0)
        self.assertEqual(hold_sig, "HOLD")
        self.assertIn("not_rs_top", hold_why)
        buy_sig, buy_why = strat._playbook_vwap_momentum_rs(work, "LEADER", vwap=50.0)
        self.assertEqual(buy_sig, "BUY")
        self.assertIn("rs_leader", buy_why)


class TestEmaPullbackPlaybook(unittest.TestCase):
    def test_extended_without_pullback_is_hold(self):
        strat, df = _ready_strat()
        vwap_series = calc_session_vwap(df)
        vwap = float(vwap_series.iloc[-1])
        # Force last close far above VWAP/EMA
        df.iloc[-1, df.columns.get_loc("close")] = vwap + 50.0
        df.iloc[-1, df.columns.get_loc("high")] = vwap + 51.0
        df["ATR"] = 1.0
        with patch.object(strat._trend, "generate_signal", return_value="BUY") as inner:
            sig, why = strat._playbook_ema_pullback(df, "INFY", vwap, vwap_series)
        self.assertEqual(sig, "HOLD")
        self.assertIn("extended", why)
        inner.assert_not_called()

    def test_valid_pullback_plus_momentum_is_buy(self):
        strat, df = _ready_strat()
        ema_c = f"EMA_{strat.p.ema_pullback}"
        vwap_series = calc_session_vwap(df)
        vwap = float(vwap_series.iloc[-1])
        # Last few closes near EMA so pullback is recognized
        ema_val = float(df[ema_c].iloc[-1])
        df.loc[df.index[-3:], "close"] = ema_val
        df.loc[df.index[-3:], "high"] = ema_val * 1.001
        df.loc[df.index[-3:], "low"] = ema_val * 0.999
        df["ATR"] = 2.0
        with patch.object(strat._trend, "generate_signal", return_value="BUY"):
            sig, why = strat._playbook_ema_pullback(df, "INFY", vwap, vwap_series)
        self.assertEqual(sig, "BUY")
        self.assertIn("pullback", why)


class TestVwapMeanReversion(unittest.TestCase):
    def test_weak_stretch_is_hold(self):
        strat, df = _ready_strat()
        df["ADX"] = 15.0
        df["ATR"] = 2.0
        rsi_c = f"RSI_{strat.p.rsi_period}"
        df[rsi_c] = 25.0
        vwap = float(calc_session_vwap(df).iloc[-1])
        # Close sits on VWAP — not stretched
        df.iloc[-1, df.columns.get_loc("close")] = vwap
        sig, why = strat._playbook_vwap_mr(df, "ITC", vwap)
        self.assertEqual(sig, "HOLD")
        self.assertIn("weak_stretch", why)

    def test_stretch_rsi_reclaim_is_buy(self):
        strat, df = _ready_strat()
        df["ADX"] = 15.0
        df["ATR"] = 2.0
        rsi_c = f"RSI_{strat.p.rsi_period}"
        df[rsi_c] = 25.0
        vwap = float(calc_session_vwap(df).iloc[-1])
        # Previous high below reclaim close
        df.iloc[-2, df.columns.get_loc("high")] = vwap - 3.5
        df.iloc[-1, df.columns.get_loc("close")] = vwap - 3.0
        df.iloc[-1, df.columns.get_loc("high")] = vwap - 2.8
        df.iloc[-1, df.columns.get_loc("low")] = vwap - 3.2
        sig, why = strat._playbook_vwap_mr(df, "ITC", vwap)
        self.assertEqual(sig, "BUY")
        self.assertIn("reclaim", why)

    def test_us_looser_stretch_and_rsi_vs_india(self):
        """US uses US_VWAP_STRETCH_ATR / US_RSI_OVERSOLD; India stays strict."""
        from strategy import create_strategy

        india, df_i = _ready_strat()
        us = create_strategy("US", name="mis_regime")
        df_u = df_i.copy()
        for df in (df_i, df_u):
            df["ADX"] = 15.0
            df["ATR"] = 2.0
        rsi_i = f"RSI_{india.p.rsi_period}"
        rsi_u = f"RSI_{us.p.rsi_period}"
        df_i[rsi_i] = 38.0
        df_u[rsi_u] = 38.0
        vwap = float(calc_session_vwap(df_i).iloc[-1])
        # Mild stretch: passes US 0.7*ATR, fails India 1.5*ATR (when India env=1.5)
        mild = vwap - 1.6
        for df in (df_i, df_u):
            df.iloc[-2, df.columns.get_loc("high")] = mild - 0.5
            df.iloc[-1, df.columns.get_loc("close")] = mild
            df.iloc[-1, df.columns.get_loc("high")] = mild + 0.1
            df.iloc[-1, df.columns.get_loc("low")] = mild - 0.2
        with patch.object(config, "VWAP_STRETCH_ATR", 1.5), patch.object(
            config, "RSI_OVERSOLD", 30
        ), patch.object(config, "US_VWAP_STRETCH_ATR", 0.70), patch.object(
            config, "US_RSI_OVERSOLD", 42
        ):
            sig_i, why_i = india._playbook_vwap_mr(df_i, "ITC", vwap)
            sig_u, why_u = us._playbook_vwap_mr(df_u, "INTC", vwap)
        self.assertEqual(sig_i, "HOLD")
        self.assertTrue("weak_stretch" in why_i or "rsi=" in why_i)
        self.assertEqual(sig_u, "BUY", msg=why_u)


class TestEntryCutoffAndCompletedBars(unittest.TestCase):
    def test_entry_cutoff_blocks_all_playbook_buys(self):
        strat, df = _ready_strat()
        with patch("strategy.now_ist", return_value=NOW_CUTOFF), patch.object(
            config, "ENTRY_CUTOFF", "14:15"
        ), patch.object(config, "LOSS_REENTRY_COOLDOWN_MIN", 0), patch.object(
            strat, "classify_regime", return_value=(REGIME_TREND_UP, "forced")
        ), patch.object(
            strat, "_trend_up_clear", return_value=True
        ), patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ) as mom, patch.object(
            strat, "_playbook_orb", return_value=("BUY", "orb")
        ) as orb, patch.object(
            strat, "_playbook_vwap_mr", return_value=("BUY", "mr")
        ) as mr:
            sig = strat.generate_signal(df, "INFY")
        self.assertEqual(sig, "HOLD")
        self.assertIn("ENTRY_CUTOFF", strat.last_decision["reason"])
        mom.assert_not_called()
        orb.assert_not_called()
        mr.assert_not_called()

    def test_us_ignores_india_ist_cutoff_during_nyse_morning(self):
        """7:30 PM IST / 10:00 AM ET must NOT be blocked by India ENTRY_CUTOFF=14:15."""
        et = ZoneInfo("America/New_York")
        now_et = datetime(2026, 8, 20, 10, 0, tzinfo=et)
        strat = create_strategy("US", name="mis_regime")
        # Completed ET bars only (end before now_et=10:00)
        start = datetime(2026, 8, 19, 10, 0, tzinfo=et)
        idx = pd.date_range(start=start, periods=80, freq="5min", tz=et)
        idx = idx[idx + pd.Timedelta(minutes=5) <= now_et]
        df = pd.DataFrame(
            {
                "open": np.linspace(100, 110, len(idx)),
                "high": np.linspace(101, 111, len(idx)),
                "low": np.linspace(99, 109, len(idx)),
                "close": np.linspace(100.5, 110.5, len(idx)),
                "volume": np.full(len(idx), 1_000_000.0),
            },
            index=idx,
        )
        df = strat.compute_indicators(df)
        with patch("strategy.now_et", return_value=now_et), patch.object(
            config, "ENTRY_CUTOFF", "14:15"
        ), patch.object(config, "US_ENTRY_CUTOFF", "15:15"), patch.object(
            config, "LOSS_REENTRY_COOLDOWN_MIN", 0
        ), patch.object(
            strat, "classify_regime", return_value=(REGIME_TREND_UP, "forced")
        ), patch.object(strat, "_trend_up_clear", return_value=True), patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ), patch.object(
            strat, "_playbook_ema_pullback", return_value=("HOLD", "x")
        ):
            sig = strat.generate_signal(df, "AAPL")
        self.assertEqual(sig, "BUY", msg=str(strat.last_decision))
        self.assertNotIn("ENTRY_CUTOFF", strat.last_decision.get("reason", ""))

    def test_forming_candle_never_used_for_buy(self):
        strat = create_strategy("INDIA", name="mis_regime")
        now = datetime.now(IST)
        bucket = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        times = [
            datetime(2026, 8, 12, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
            for i in range(40)
        ]
        times[-1] = bucket  # forming
        close = np.linspace(100, 110, 40)
        close[-1] = 999.0  # spike only on forming bar
        df = pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": np.full(40, 1e6),
            },
            index=pd.DatetimeIndex(times),
        )
        df = strat.compute_indicators(df)
        forming_ts = df.index[-1]

        seen = {}

        def _spy(df_in, symbol, vwap):
            seen["has_forming"] = forming_ts in df_in.index
            seen["last_close"] = float(df_in["close"].iloc[-1])
            return "HOLD", "spy"

        with patch("strategy.now_ist", return_value=now), patch.object(
            config, "LOSS_REENTRY_COOLDOWN_MIN", 0
        ), patch.object(strat, "classify_regime", return_value=(REGIME_TREND_UP, "x")), patch.object(
            strat, "_trend_up_clear", return_value=True
        ), patch.object(
            strat, "_playbook_vwap_momentum_rs", side_effect=_spy
        ), patch.object(
            strat, "_playbook_ema_pullback", return_value=("HOLD", "x")
        ):
            sig = strat.generate_signal(df, "INFY")
        self.assertEqual(sig, "HOLD")
        if "has_forming" in seen:
            self.assertFalse(seen["has_forming"])
            self.assertNotAlmostEqual(seen["last_close"], 999.0)

        dropped = completed_bars_only(df, now=now)
        self.assertNotEqual(dropped.index[-1], forming_ts)


class TestFactory(unittest.TestCase):
    def test_mis_regime_aliases_and_legacy(self):
        for name in ("mis_regime", "regime_mis", "vwap_regime"):
            s = create_strategy("INDIA", name=name)
            self.assertIsInstance(s, MisRegimeStrategy)
            self.assertEqual(s.name, "mis_regime")
        orb = create_strategy("INDIA", name="opening_range_breakout")
        self.assertIsInstance(orb, OpeningRangeBreakoutStrategy)
        self.assertEqual(orb.name, "opening_range_breakout")
        tp = create_strategy("INDIA", name="trend_pullback")
        self.assertEqual(tp.name, "trend_pullback")
        mr = create_strategy("INDIA", name="mean_reversion")
        self.assertEqual(mr.name, "mean_reversion")
        ra = create_strategy("INDIA", name="regime_adaptive")
        self.assertEqual(ra.name, "regime_adaptive")
        br = create_strategy("INDIA", name="breakout")
        self.assertEqual(br.name, "breakout")

    def test_india_default_is_mis_regime(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1].joinpath("config.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('os.getenv("STRATEGY_NAME", "mis_regime")', src)

    def test_mis_regime_not_wrapped_by_global_rs(self):
        from strategy import FilteredStrategy

        with patch.object(config, "USE_RELATIVE_STRENGTH", True):
            s = create_strategy("INDIA", name="mis_regime")
        self.assertIsInstance(s, MisRegimeStrategy)
        self.assertNotIsInstance(s, FilteredStrategy)

    def test_core_loop_still_scans_universe_and_refresh_rs(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("config.INDIA_STOCK_UNIVERSE", src)
        self.assertIn("_refresh_strategy_universe", src)
        self.assertIn("_india_try_buy", src)

    def test_mis_regime_orb_uses_stricter_defaults_without_changing_legacy(self):
        mis = create_strategy("INDIA", name="mis_regime")
        self.assertEqual(mis._orb.confirm_bars, config.MIS_REGIME_CONFIRM_BARS)
        self.assertAlmostEqual(mis._orb.volume_mult, config.MIS_REGIME_VOLUME_MULT)
        legacy = create_strategy("INDIA", name="opening_range_breakout")
        self.assertEqual(legacy.confirm_bars, config.CONFIRM_BARS)
        self.assertAlmostEqual(legacy.volume_mult, config.VOLUME_MULT)
        self.assertNotEqual(config.CONFIRM_BARS, config.MIS_REGIME_CONFIRM_BARS)


class TestBuyLogging(unittest.TestCase):
    def test_buy_and_hold_log_regime_playbook_reason(self):
        strat, df = _ready_strat()
        with patch("strategy.now_ist", return_value=NOW_AFTER_WINDOW), patch.object(
            config, "LOSS_REENTRY_COOLDOWN_MIN", 0
        ), patch.object(
            strat, "classify_regime", return_value=(REGIME_TREND_UP, "forced")
        ), patch.object(
            strat, "_trend_up_clear", return_value=True
        ), patch.object(
            strat, "_playbook_vwap_momentum_rs", return_value=("BUY", "mom")
        ):
            sig = strat.generate_signal(df, "INFY")
        self.assertEqual(sig, "BUY")
        d = strat.last_decision
        self.assertEqual(d["symbol"], "INFY")
        self.assertEqual(d["regime"], REGIME_TREND_UP)
        self.assertEqual(d["playbook"], PLAYBOOK_MOMENTUM)
        self.assertTrue(d["reason"])

        with patch("strategy.now_ist", return_value=NOW_AFTER_WINDOW), patch.object(
            config, "LOSS_REENTRY_COOLDOWN_MIN", 0
        ), patch.object(
            strat, "classify_regime", return_value=(REGIME_TREND_DOWN, "down")
        ):
            sig2 = strat.generate_signal(df, "SBIN")
        self.assertEqual(sig2, "HOLD")
        self.assertEqual(strat.last_decision["playbook"], PLAYBOOK_NONE)
        self.assertIn("trend_down", strat.last_decision["reason"])


if __name__ == "__main__":
    unittest.main()

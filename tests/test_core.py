"""Basic unit tests for risk sizing, filters, bot_state, and US stack."""

from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from risk_manager import RiskManager
from filters import is_uptrend_df
import bot_state
from strategy import calc_sma, create_strategy, snapshot_signal, TrendPullbackStrategy, params_for_market
from india_scout import (
    DEFAULT_INDIA_SCOUT_UNIVERSE,
    rank_near_setups,
    resolve_scout_universe,
    score_near_setup,
)
import config


def _ohlcv(close: np.ndarray, volume: float | np.ndarray = 1e6) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    vol = (
        np.full(len(close), float(volume), dtype=float)
        if np.isscalar(volume)
        else np.asarray(volume, dtype=float)
    )
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": vol,
        }
    )


class TestCoreBotLogic(unittest.TestCase):
    def test_position_size_respects_risk_and_caps(self):
        rm = RiskManager(market="US")
        qty = rm.calculate_position_size(100_000, price=100.0, stop_distance=2.0)
        self.assertGreater(qty, 0)
        self.assertLessEqual(qty, config.MAX_SHARES_PER_ORDER)

    def test_is_uptrend_df(self):
        idx = pd.date_range("2024-01-01", periods=250, freq="D")
        close = pd.Series(np.linspace(100, 150, 250), index=idx)
        df = pd.DataFrame({"close": close, "high": close * 1.01, "low": close * 0.99, "open": close, "volume": 1e6})
        self.assertTrue(is_uptrend_df(df, sma_len=200))
        df2 = df.copy()
        df2["close"] = np.linspace(150, 100, 250)
        self.assertFalse(is_uptrend_df(df2, sma_len=200))

    def test_bot_state_signals_and_sod(self):
        bot_state.reset_sod_for_tests()
        bot_state.publish_signals("US", [{"symbol": "SPY", "signal": "HOLD", "price": 1.0, "reason": "x"}])
        rows = bot_state.get_signals("US", max_age_sec=60)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["symbol"], "SPY")
        sod = bot_state.india_sod_equity(100000)
        self.assertEqual(bot_state.india_sod_equity(101000), sod)
        sod_us = bot_state.us_sod_equity(10000)
        self.assertEqual(bot_state.us_sod_equity(10500), sod_us)

    def test_india_sod_rebases_on_large_deposit(self):
        bot_state.reset_sod_for_tests()
        self.assertEqual(bot_state.india_sod_equity(25_000), 25_000)
        # ₹50k deposit → equity 75k should not look like +200% Daily P&L
        self.assertEqual(bot_state.india_sod_equity(75_000), 75_000)

    def test_bot_state_scout_publish(self):
        bot_state.publish_scout(
            "INDIA",
            [{"symbol": "INFY", "score": 72.5, "reason": "near MA", "near_only": True}],
            meta={"scanned": 40, "auto_buy": True},
        )
        blob = bot_state.get_scout("INDIA", max_age_sec=60)
        self.assertIsNotNone(blob)
        self.assertTrue(blob["trade_eligible"])
        self.assertEqual(blob["list"][0]["symbol"], "INFY")
        self.assertEqual(blob["meta"]["scanned"], 40)

    def test_trend_strategy_snapshot_on_synthetic(self):
        rng = np.random.default_rng(0)
        n = 260
        close = 100 * np.cumprod(1 + rng.normal(0.001, 0.01, n))
        df = pd.DataFrame({
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": rng.integers(1e5, 1e6, n).astype(float),
        })
        strat = create_strategy("US", name="trend_pullback")
        df = strat.compute_indicators(df)
        snap = snapshot_signal(strat, df, "TEST")
        self.assertIn(snap["signal"], ("BUY", "SELL", "HOLD"))
        self.assertIn("reason", snap)

    def test_calc_sma_length(self):
        s = pd.Series(range(10), dtype=float)
        out = calc_sma(s, 5)
        self.assertTrue(pd.isna(out.iloc[3]))
        self.assertEqual(out.iloc[4], 2.0)


class TestIndiaScoutScoring(unittest.TestCase):
    def setUp(self):
        self.params = params_for_market("INDIA")
        self.strat = TrendPullbackStrategy(self.params)

    def test_resolve_scout_universe_nonempty(self):
        univ = resolve_scout_universe()
        self.assertGreaterEqual(len(univ), 12)
        self.assertIn("RELIANCE", univ)

    def test_scout_rs_top_n_wider_than_core_default(self):
        import config
        from strategy import RelativeStrengthFilter

        self.assertGreaterEqual(config.INDIA_SCOUT_RS_TOP_N, 8)
        self.assertLessEqual(config.INDIA_SCOUT_INTERVAL_SEC, 600)
        filt = RelativeStrengthFilter(top_n=config.INDIA_SCOUT_RS_TOP_N)
        self.assertEqual(filt.top_n, config.INDIA_SCOUT_RS_TOP_N)

    def test_downtrend_scores_low(self):
        # Steady decline — below SMA200
        close = np.linspace(200, 80, 260)
        df = self.strat.compute_indicators(_ohlcv(close, volume=5e5))
        row = score_near_setup(df, self.params, symbol="WEAK")
        self.assertLess(row["score"], 35)
        self.assertFalse(row["would_buy"])
        self.assertTrue(row["near_only"])

    def test_uptrend_pullback_scores_higher_than_extended(self):
        # Climb then soft pullback toward MA with elevated volume
        n = 260
        base = np.linspace(100, 160, n - 8)
        # Last bars: mild dip toward SMA20 while still above SMA200
        pull = np.array([159.5, 158.8, 158.2, 157.6, 157.0, 156.5, 156.2, 156.0])
        close = np.concatenate([base, pull])
        vol = np.full(n, 8e5)
        vol[-5:] = 1.6e6  # volume confirmation
        df = self.strat.compute_indicators(_ohlcv(close, volume=vol))
        near = score_near_setup(df, self.params, symbol="NEAR")

        # Same trend but last bar extended far above fast MAs
        close_ext = close.copy()
        close_ext[-1] = float(close[-1]) * 1.08
        df_ext = self.strat.compute_indicators(_ohlcv(close_ext, volume=vol))
        ext = score_near_setup(df_ext, self.params, symbol="EXT")

        self.assertGreater(near["score"], 40)
        self.assertGreater(near["score"], ext["score"])
        self.assertIn("components", near)
        self.assertEqual(near["symbol"], "NEAR")

    def test_rank_near_setups_excludes_confirmed(self):
        rows = [
            {"symbol": "A", "score": 80, "would_buy": True},
            {"symbol": "B", "score": 70, "would_buy": False},
            {"symbol": "C", "score": 55, "would_buy": False},
            {"symbol": "D", "score": 90, "would_buy": False},
        ]
        top = rank_near_setups(rows, top_n=3, min_score=50, exclude_confirmed=True)
        self.assertEqual([r["symbol"] for r in top], ["D", "B", "C"])
        self.assertTrue(all(not r["would_buy"] for r in top))

    def test_rank_near_setups_top_n_and_floor(self):
        rows = [
            {"symbol": "A", "score": 80, "would_buy": False},
            {"symbol": "B", "score": 20, "would_buy": False},
            {"symbol": "C", "score": 55, "would_buy": False},
            {"symbol": "D", "score": 90, "would_buy": False},
        ]
        top = rank_near_setups(rows, top_n=2, min_score=50)
        self.assertEqual([r["symbol"] for r in top], ["D", "A"])

    def test_scout_only_excludes_core(self):
        from india_scout import scout_only_symbols

        only = scout_only_symbols()
        for sym in config.INDIA_STOCK_UNIVERSE:
            self.assertNotIn(sym, only)
        self.assertIn("AXISBANK", only)

    def test_default_scout_universe_unique_nifty50_sized(self):
        univ = [s.strip().upper() for s in DEFAULT_INDIA_SCOUT_UNIVERSE if str(s).strip()]
        self.assertEqual(len(univ), len(set(univ)))
        self.assertEqual(len(univ), 50)
        self.assertIn("RELIANCE", univ)
        self.assertIn("TRENT", univ)

    def test_inter_symbol_fetch_gap_skips_last(self):
        self.assertEqual(config.inter_symbol_fetch_gap_sec(0.4, 0, 50), 0.4)
        self.assertEqual(config.inter_symbol_fetch_gap_sec(0.4, 48, 50), 0.4)
        self.assertEqual(config.inter_symbol_fetch_gap_sec(0.4, 49, 50), 0.0)
        self.assertEqual(config.inter_symbol_fetch_gap_sec(0, 0, 50), 0.0)
        self.assertEqual(config.inter_symbol_fetch_gap_sec(0.4, 0, 1), 0.0)

    def test_core_loop_scans_stock_universe_with_fetch_gap(self):
        src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8")
        start = src.index("def run_india_loop")
        end = src.index("def run_india_scout_loop")
        body = src[start:end]
        self.assertIn("config.INDIA_STOCK_UNIVERSE", body)
        self.assertIn("INDIA_LOOP_FETCH_GAP_SEC", body)
        self.assertIn("inter_symbol_fetch_gap_sec", body)
        self.assertNotIn("resolve_scout_universe", body)
        self.assertNotIn("INDIA_SCOUT_UNIVERSE", body)
        kill_idx = body.index("if risk_mgr.is_kill_switch_active:")
        tide_idx = body.index("market_tide_filter(")
        pos_idx = body.index("current_positions = india_broker.get_open_positions()")
        self.assertLess(kill_idx, tide_idx)
        self.assertLess(tide_idx, pos_idx)
        self.assertIn("apply_tide_lock_to_signal", body)

    def test_max_trades_and_loop_interval_defaults_in_source(self):
        src = Path(__file__).resolve().parents[1].joinpath("config.py").read_text(encoding="utf-8")
        self.assertIn('MAX_TRADES_PER_DAY", "0"', src)
        self.assertIn('INDIA_LOOP_INTERVAL_SEC", "60"', src)
        self.assertIn("opening_range_breakout", src)

    def test_rotating_file_handler_in_main(self):
        src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("RotatingFileHandler", src)
        self.assertIn("50 * 1024 * 1024", src)
        self.assertIn("backupCount=5", src)
        self.assertNotIn("logging.FileHandler(", src)

    def test_insufficient_bars(self):
        df = _ohlcv(np.linspace(100, 110, 20))
        df = self.strat.compute_indicators(df)
        row = score_near_setup(df, self.params, symbol="SHORT")
        self.assertEqual(row["score"], 0.0)
        self.assertIn("need", row["reason"])


if __name__ == "__main__":
    unittest.main()

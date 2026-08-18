"""MIN_STOP_PCT floor on SL/TP + larger MIS sizing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import config
from risk_manager import RiskManager


class TestMinStopFloor(unittest.TestCase):
    def test_tiny_atr_stop_widened_to_min_pct(self):
        rm = RiskManager(market="INDIA")
        rm.atr_stop_mult = 1.5
        entry = 266.85
        atr = 0.5  # 1.5*0.5 = ₹0.75 → ~0.28%
        with patch.object(config, "MIN_STOP_PCT", 0.0045):
            sl = rm.get_stop_loss_price(entry, atr=atr)
        min_sl = entry * (1 - 0.0045)
        self.assertLessEqual(sl, min_sl + 0.05)
        stop_pct = (entry - sl) / entry
        self.assertGreaterEqual(stop_pct, 0.0045 - 1e-6)

    def test_wide_atr_stop_not_tightened_by_floor(self):
        rm = RiskManager(market="INDIA")
        rm.atr_stop_mult = 1.5
        entry = 100.0
        atr = 0.67  # 1.5*0.67 ≈ 1.005 → ~1% stop
        with patch.object(config, "MIN_STOP_PCT", 0.0045):
            sl = rm.get_stop_loss_price(entry, atr=atr)
        self.assertAlmostEqual(sl, entry - rm.atr_stop_mult * atr, delta=0.10)

    def test_min_stop_pct_zero_preserves_old_behavior(self):
        rm = RiskManager(market="INDIA")
        rm.atr_stop_mult = 1.5
        entry = 266.85
        atr = 0.5
        with patch.object(config, "MIN_STOP_PCT", 0.0):
            sl_off = rm.get_stop_loss_price(entry, atr=atr)
        sl_raw = entry - rm.atr_stop_mult * atr
        self.assertAlmostEqual(sl_off, sl_raw, delta=0.06)

    def test_tp_uses_floored_stop_not_raw_atr(self):
        rm = RiskManager(market="INDIA")
        rm.atr_stop_mult = 1.5
        rm.take_profit_r = 1.75
        entry = 266.85
        atr = 0.5
        with patch.object(config, "MIN_STOP_PCT", 0.0045):
            sl = rm.get_stop_loss_price(entry, atr=atr)
            tp = rm.get_take_profit_price(entry, stop_loss_price=sl, atr=atr)
        risk = entry - sl
        self.assertAlmostEqual(tp, entry + rm.take_profit_r * risk, delta=0.10)


class TestMinStopSizing(unittest.TestCase):
    def test_risk_budget_hits_max_shares_at_2000(self):
        rm = RiskManager(market="INDIA")
        rm.risk_per_trade = 0.008
        rm.max_position_pct = 5.0
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000), patch.object(
            config, "MAX_SHARES_PER_ORDER", 2000
        ), patch.object(config, "MAX_POSITION_PCT", 5.0), patch.object(
            config, "MIN_STOP_PCT", 0.0045
        ):
            qty = rm.calculate_position_size(
                300_000,
                266.0,
                stop_distance=1.20,
                symbol="POWERGRID",
                available_cash=300_000,
            )
        self.assertEqual(qty, 2000)

    def test_expensive_name_risk_binds_not_share_cap(self):
        rm = RiskManager(market="INDIA")
        rm.risk_per_trade = 0.008
        rm.max_position_pct = 5.0
        price = 1098.0
        stop = price * 0.0045
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000), patch.object(
            config, "MAX_SHARES_PER_ORDER", 2000
        ), patch.object(config, "MAX_POSITION_PCT", 5.0), patch.object(
            config, "MIN_STOP_PCT", 0.0045
        ):
            qty = rm.calculate_position_size(
                300_000,
                price,
                stop_distance=stop,
                symbol="EXPENSIVE",
                available_cash=300_000,
            )
        self.assertAlmostEqual(qty, 2400 / stop, delta=2)
        self.assertLess(qty, 2000)

    def test_max_position_pct_ceiling_not_full_5x_notional(self):
        rm = RiskManager(market="INDIA")
        rm.risk_per_trade = 0.008
        rm.max_position_pct = 5.0
        price = 266.0
        stop = price * 0.0045
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000), patch.object(
            config, "MAX_SHARES_PER_ORDER", 2000
        ), patch.object(config, "MAX_POSITION_PCT", 5.0), patch.object(
            config, "MIN_STOP_PCT", 0.0045
        ):
            qty = rm.calculate_position_size(
                300_000,
                price,
                stop_distance=stop,
                symbol="POWERGRID",
                available_cash=300_000,
            )
        notional = qty * price
        self.assertAlmostEqual(notional, 532_000, delta=600)
        self.assertLess(notional, 300_000 * 5.0)


class TestFull5xPreset(unittest.TestCase):
    """RISK_PER_TRADE = MAX_POSITION_PCT * MIN_STOP_PCT is what unlocks full 5x."""

    SLEEVE = 300_000
    PRICE = 266.0

    def _size(self, risk_per_trade: float, max_shares: int) -> int:
        rm = RiskManager(market="INDIA")
        rm.risk_per_trade = risk_per_trade
        rm.max_position_pct = 5.0
        stop = self.PRICE * 0.0045
        with patch.object(config, "INDIA_CAPITAL_CAP", self.SLEEVE), patch.object(
            config, "MAX_SHARES_PER_ORDER", max_shares
        ), patch.object(config, "MAX_POSITION_PCT", 5.0), patch.object(
            config, "MIN_STOP_PCT", 0.0045
        ):
            return rm.calculate_position_size(
                self.SLEEVE,
                self.PRICE,
                stop_distance=stop,
                symbol="POWERGRID",
                available_cash=self.SLEEVE,
            )

    def test_risk_2p25_pct_reaches_5x_notional(self):
        qty = self._size(0.0225, 6000)
        notional = qty * self.PRICE
        self.assertAlmostEqual(notional, self.SLEEVE * 5.0, delta=2_000)

    def test_old_risk_budget_cannot_reach_5x(self):
        qty = self._size(0.008, 6000)
        notional = qty * self.PRICE
        self.assertLess(notional, self.SLEEVE * 2.0)

    def test_share_cap_2000_blocks_5x_notional(self):
        qty = self._size(0.0225, 2000)
        self.assertEqual(qty, 2000)
        self.assertLess(qty * self.PRICE, self.SLEEVE * 2.0)

    def test_wide_stop_keeps_risk_but_lowers_leverage(self):
        rm = RiskManager(market="INDIA")
        rm.risk_per_trade = 0.0225
        rm.max_position_pct = 5.0
        price, stop = 1000.0, 10.0  # 1% ATR stop, wider than the floor
        with patch.object(config, "INDIA_CAPITAL_CAP", self.SLEEVE), patch.object(
            config, "MAX_SHARES_PER_ORDER", 6000
        ), patch.object(config, "MAX_POSITION_PCT", 5.0), patch.object(
            config, "MIN_STOP_PCT", 0.0045
        ):
            qty = rm.calculate_position_size(
                self.SLEEVE, price, stop_distance=stop, available_cash=self.SLEEVE
            )
        self.assertAlmostEqual(qty * stop, 6_750, delta=15)
        self.assertAlmostEqual(qty * price, 675_000, delta=1_500)


class TestPortfolioRiskGateAtFull5x(unittest.TestCase):
    """DAILY_DRAWDOWN_LIMIT x sleeve = 15k open+pending risk caps concurrency."""

    def test_only_two_full_size_trades_fit_the_risk_budget(self):
        rm = RiskManager(market="INDIA")
        rm.daily_drawdown_limit = 0.05
        per_trade_risk = 6_750.0
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000):
            self.assertEqual(rm.portfolio_risk_budget_rupees(300_000), 15_000)

            ok1, _ = rm.can_add_trade_risk(300_000, per_trade_risk, {})
            self.assertTrue(ok1)
            rm.set_pending_risk("A", per_trade_risk)

            ok2, _ = rm.can_add_trade_risk(300_000, per_trade_risk, {})
            self.assertTrue(ok2)
            rm.set_pending_risk("B", per_trade_risk)

            ok3, why3 = rm.can_add_trade_risk(300_000, per_trade_risk, {})
        self.assertFalse(ok3)
        self.assertIn("portfolio_risk", why3)


class TestMinTrailPctIndependentOfStopFloor(unittest.TestCase):
    def test_min_stop_pct_does_not_pin_trail_at_breakeven(self):
        rm = RiskManager(market="INDIA")
        rm.atr_trail_mult = 0.75
        entry, sl, peak = 1321.47, 1315.40, 1327.80
        atr = (entry - sl) / 1.5  # ~4.05 if stop was 1.5x ATR before MIN_STOP floor
        rm.register_trade("RELIANCE", entry, sl, atr=atr, qty=1125, take_profit=1332.0)
        with patch.object(config, "MIN_STOP_PCT", 0.0045), patch.object(
            config, "MIN_TRAIL_PCT", 0.0020
        ):
            stop = rm.update_trailing_stop("RELIANCE", peak, atr=atr)
        # Must sit well above entry (today's bug sat at 1321.82).
        self.assertGreater(stop, entry + 2.0)
        self.assertLess(stop, peak)

    def test_two_slot_preset_uses_half_sleeve(self):
        rm = RiskManager(market="INDIA")
        rm.risk_per_trade = 0.01125
        rm.max_position_pct = 2.50
        price, stop = 1321.10, 6.00
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000), patch.object(
            config, "MAX_SHARES_PER_ORDER", 6000
        ), patch.object(config, "MIN_STOP_PCT", 0.0045):
            qty = rm.calculate_position_size(
                300_000, price, stop_distance=stop, available_cash=300_000
            )
        self.assertAlmostEqual(qty, 562, delta=2)
        self.assertAlmostEqual(qty * price / 5.0, 148_600, delta=2_000)


if __name__ == "__main__":
    unittest.main()

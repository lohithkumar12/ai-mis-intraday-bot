"""5x MIS sizing: MAX_POSITION_PCT, cluster=1, margin free-cash gate."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import config
from risk_manager import RiskManager


class TestLeverageSizing(unittest.TestCase):
    def test_qty_is_min_of_risk_pct_and_share_cap(self):
        rm = RiskManager(market="INDIA")
        rm.risk_per_trade = 0.008
        rm.max_position_pct = 1.00
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000), patch.object(
            config, "MAX_SHARES_PER_ORDER", 500
        ):
            # equity 300k, price 100, stop 2 → risk_budget=2400, shares_risk=1200
            # max_by_pct = 3000, max_shares=500 → qty=500
            qty = rm.calculate_position_size(
                400_000,
                100.0,
                stop_distance=2.0,
                symbol="INFY",
                available_cash=350_000,
            )
        self.assertEqual(qty, 500)

    def test_risk_budget_binds_when_stop_is_wide(self):
        rm = RiskManager(market="INDIA")
        rm.risk_per_trade = 0.008
        rm.max_position_pct = 1.00
        with patch.object(config, "INDIA_CAPITAL_CAP", 300_000), patch.object(
            config, "MAX_SHARES_PER_ORDER", 500
        ):
            # stop 20 → shares_risk = 2400/20 = 120
            qty = rm.calculate_position_size(
                300_000, 100.0, stop_distance=20.0, symbol="TCS", available_cash=300_000
            )
        self.assertEqual(qty, 120)

    def test_cluster_open_count(self):
        rm = RiskManager(market="INDIA")
        cluster, n = rm.cluster_open_count("INFY", {"TCS": {"qty": 1}})
        self.assertEqual(cluster, "in_it")
        self.assertEqual(n, 1)

    def test_margin_book_add_release(self):
        rm = RiskManager(market="INDIA")
        rm.add_margin("INFY", 12_000)
        rm.add_margin("SBIN", 8_000)
        self.assertAlmostEqual(rm.total_margin_used(), 20_000)
        rm.release_margin("INFY")
        self.assertAlmostEqual(rm.total_margin_used(), 8_000)
        rm.reset_margin_book()
        self.assertEqual(rm.total_margin_used(), 0)

    def test_margin_rejected_when_free_cash_short(self):
        from main import _india_try_buy
        import bot_state
        import trade_journal

        bot_state.reset_tide_for_tests()
        risk = RiskManager(market="INDIA")
        risk.risk_per_trade = 0.008
        risk.max_position_pct = 1.0
        risk.add_margin("TCS", 140_000)
        broker = MagicMock()
        broker.get_latest_quote.return_value = {"ltp": 100.0}
        broker.get_margin_required.return_value = 50_000
        broker.place_buy_order.return_value = None
        strat = MagicMock()
        strat.has_day_fired.return_value = False
        strat.name = "mis_regime"
        with patch.object(trade_journal, "has_entry_today", return_value=False), patch.object(
            trade_journal, "count_entries_today", return_value=0
        ), patch("main.order_guards") as og, patch("main.bot_state") as bs:
            bs.is_tide_bearish.return_value = False
            og.is_buy_blocked.return_value = (False, "")
            ok = _india_try_buy(
                india_broker=broker,
                risk_mgr=risk,
                strategy=strat,
                symbol="INFY",
                snap={"signal": "BUY", "price": 100.0},
                atr=2.0,
                current_equity=300_000.0,
                account={"available_cash": 150_000.0},
                current_positions={},
                tradable_window=True,
                regime_ok=True,
            )
        self.assertFalse(ok)
        broker.place_buy_order.assert_not_called()

    def test_free_cash_skips_open_book_when_broker_cash_already_netted(self):
        """After a fill, Dhan available_cash is already net of blocked margin."""
        rm = RiskManager(market="INDIA")
        rm.add_margin("RELIANCE", 297_247)
        # Next cycle: broker cash ~₹3.8k, local book still 297k.
        free = rm.free_cash_for_entry(
            3_844.12,
            current_positions={"RELIANCE": {"qty": 1125}},
            broker_used_margin=297_247,
        )
        self.assertGreater(free, 0)
        self.assertLess(free, 5_000)

    def test_same_cycle_still_subtracts_local_book_before_broker_updates(self):
        rm = RiskManager(market="INDIA")
        rm.add_margin("RELIANCE", 297_247)
        free = rm.free_cash_for_entry(
            300_000,
            current_positions={"RELIANCE": {"qty": 1125}},
            broker_used_margin=0,
        )
        self.assertAlmostEqual(free, 2_753, delta=1)

    def test_pending_symbol_still_reserves_cash(self):
        rm = RiskManager(market="INDIA")
        rm.add_margin("TCS", 140_000)
        free = rm.free_cash_for_entry(
            150_000,
            current_positions={},
            broker_used_margin=0,
        )
        self.assertAlmostEqual(free, 10_000, delta=1)

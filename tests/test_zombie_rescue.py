"""Zombie MIS rescue: broker-open with no sl_tp_meta gets local SL/TP, not flattened."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import bot_state
import config
import order_guards
import trade_journal
from dhan_broker import DhanBroker
from risk_manager import RiskManager
from strategy import OpeningRangeBreakoutStrategy, params_for_market, reset_orb_fired_for_tests


class TestRescueZombiePositions(unittest.TestCase):
    def setUp(self):
        order_guards.reset_for_tests()
        reset_orb_fired_for_tests()
        bot_state.reset_zombies_for_tests()
        self._tmp = tempfile.TemporaryDirectory()
        self._old = config.TRADE_JOURNAL_PATH
        config.TRADE_JOURNAL_PATH = str(Path(self._tmp.name) / "j.db")
        trade_journal.init_db()
        self.broker = DhanBroker.__new__(DhanBroker)
        self.broker.paper = None
        self.broker.dhan = None
        self.broker.sl_tp_meta = {}
        self.broker.close_position = MagicMock(return_value=None)
        self.rm = RiskManager(market="INDIA")
        self.strat = OpeningRangeBreakoutStrategy(params_for_market("INDIA"))

    def tearDown(self):
        config.TRADE_JOURNAL_PATH = self._old
        reset_orb_fired_for_tests()
        bot_state.reset_zombies_for_tests()
        order_guards.reset_for_tests()
        self._tmp.cleanup()

    def test_rescues_naked_mis_position_from_ltp(self):
        self.broker.get_open_positions = MagicMock(
            return_value={
                "INFY": {
                    "qty": 12,
                    "avg_entry_price": 1500.0,
                    "current_price": 1490.0,
                    "side": "BUY",
                }
            }
        )
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 100.0})
        with patch.object(config, "ZOMBIE_SL_PCT", 0.0045):
            with patch.object(config, "ZOMBIE_TP_PCT", 0.008):
                rescued = self.broker.rescue_zombie_positions(self.rm, self.strat)
        self.assertEqual(rescued, ["INFY"])
        meta = self.broker.sl_tp_meta["INFY"]
        self.assertEqual(meta["status"], "ACTIVE")
        self.assertEqual(meta["qty"], 12)
        self.assertAlmostEqual(meta["stop_loss_price"], 99.55, places=2)
        self.assertAlmostEqual(meta["target_price"], 100.80, places=2)
        self.assertEqual(self.rm._trade_meta["INFY"]["qty"], 12)
        self.assertTrue(self.strat.has_day_fired("INFY", "BUY"))
        self.broker.close_position.assert_not_called()
        rows = bot_state.get_signals("INDIA", max_age_sec=60)
        self.assertTrue(any(r.get("reason") == "ZOMBIE RESCUED" for r in (rows or [])))
        open_j = trade_journal.list_open_trades("INDIA")
        self.assertEqual(len(open_j), 1)
        self.assertEqual(open_j[0]["symbol"], "INFY")

    def test_skips_already_active_monitor(self):
        self.broker.sl_tp_meta["TCS"] = {
            "status": "ACTIVE",
            "stop_loss_price": 90.0,
            "target_price": 110.0,
            "qty": 5,
        }
        self.broker.get_open_positions = MagicMock(
            return_value={"TCS": {"qty": 5, "avg_entry_price": 100.0, "current_price": 101.0}}
        )
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 101.0})
        rescued = self.broker.rescue_zombie_positions(self.rm, self.strat)
        self.assertEqual(rescued, [])
        self.assertAlmostEqual(self.broker.sl_tp_meta["TCS"]["stop_loss_price"], 90.0)

    def test_cooldown_with_live_qty_is_zombie(self):
        self.broker.sl_tp_meta["SBIN"] = {
            "status": "COOLDOWN",
            "stop_loss_price": 0,
            "qty": 0,
        }
        self.broker.get_open_positions = MagicMock(
            return_value={"SBIN": {"qty": 8, "avg_entry_price": 800.0, "current_price": 798.0}}
        )
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 798.0})
        rescued = self.broker.rescue_zombie_positions(self.rm, self.strat)
        self.assertEqual(rescued, ["SBIN"])
        self.assertEqual(self.broker.sl_tp_meta["SBIN"]["status"], "ACTIVE")
        self.assertGreater(self.broker.sl_tp_meta["SBIN"]["stop_loss_price"], 0)

    def test_does_not_overwrite_journal(self):
        trade_journal.record_entry("INDIA", "WIPRO", 20, 250.0, stop_price=248.0)
        self.broker.get_open_positions = MagicMock(
            return_value={"WIPRO": {"qty": 20, "avg_entry_price": 250.0, "current_price": 249.0}}
        )
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 249.0})
        self.broker.rescue_zombie_positions(self.rm, self.strat)
        open_j = trade_journal.list_open_trades("INDIA")
        self.assertEqual(len(open_j), 1)
        self.assertEqual(int(open_j[0]["qty"]), 20)
        self.assertAlmostEqual(float(open_j[0]["entry_price"]), 250.0, places=2)

    def test_zero_ltp_does_not_invent_stop(self):
        self.broker.get_open_positions = MagicMock(
            return_value={"ITC": {"qty": 10, "avg_entry_price": 400.0, "current_price": 0.0}}
        )
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 0.0})
        rescued = self.broker.rescue_zombie_positions(self.rm, self.strat)
        self.assertEqual(rescued, [])
        self.assertNotIn("ITC", self.broker.sl_tp_meta)

    def test_uses_persisted_risk_stop_when_present(self):
        self.rm.register_trade("RELIANCE", 1400.0, 1380.0, qty=4, take_profit=1450.0)
        self.broker.get_open_positions = MagicMock(
            return_value={
                "RELIANCE": {"qty": 4, "avg_entry_price": 1400.0, "current_price": 1395.0}
            }
        )
        self.broker.get_latest_quote = MagicMock(return_value={"ltp": 1395.0})
        rescued = self.broker.rescue_zombie_positions(self.rm, self.strat)
        self.assertEqual(rescued, ["RELIANCE"])
        self.assertAlmostEqual(self.broker.sl_tp_meta["RELIANCE"]["stop_loss_price"], 1380.0)
        self.assertAlmostEqual(self.broker.sl_tp_meta["RELIANCE"]["target_price"], 1450.0)


class TestLoopPlacement(unittest.TestCase):
    def test_rescue_sits_after_check_sl_tp(self):
        src = Path(__file__).resolve().parents[1].joinpath("main.py").read_text(
            encoding="utf-8"
        )
        start = src.index("def run_india_loop")
        end = src.index("def run_india_scout_loop")
        body = src[start:end]
        sl_idx = body.index("india_broker.check_sl_tp(risk_mgr)")
        z_idx = body.index("rescue_zombie_positions")
        buy_idx = body.index("_india_try_buy(")
        self.assertLess(sl_idx, z_idx)
        self.assertLess(z_idx, buy_idx)


if __name__ == "__main__":
    unittest.main()

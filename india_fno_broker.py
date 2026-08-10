"""
india_fno_broker.py — DhanHQ Real NSE F&O Derivatives Broker Client
===================================================================
Wraps Dhan Option Chain API, Greeks, Open Interest (OI), and Futures/Options
order execution with strict risk rules and proper NSE_FNO exchange segment parameters.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import config
from dhan_broker import get_shared_dhan_broker
from india_fno_instruments import (
    get_fno_lot_size,
    is_placeholder_security_id,
    resolve_instrument_info,
    resolve_option_contract,
)
from india_fno_paper import IndiaFnoPaperPortfolio
from price_guards import require_tradeable_quote, validate_option_premium
from risk_manager import RiskManager

try:
    from dhanhq import dhanhq
except ImportError:
    dhanhq = None  # type: ignore

logger = logging.getLogger(__name__)

_shared_fno_broker: "IndiaFnoBroker | None" = None
_fno_lock = threading.Lock()


def get_shared_fno_broker() -> "IndiaFnoBroker":
    global _shared_fno_broker
    with _fno_lock:
        if _shared_fno_broker is None:
            _shared_fno_broker = IndiaFnoBroker()
        return _shared_fno_broker


class IndiaFnoBroker:
    """
    Real NSE F&O broker client for DhanHQ.
    """

    def __init__(self):
        self.dhan_broker = get_shared_dhan_broker(auto_login=True)
        self.paper = IndiaFnoPaperPortfolio() if config.INDIA_FNO_PAPER else None
        self.risk_mgr = RiskManager(market="FNO")
        self.risk_mgr.max_open_positions = max(2, int(getattr(config, "INDIA_FNO_MAX_LOTS", 2)))
        self._cooldowns: dict[str, float] = {}
        mode = "PAPER SIM (live option quotes, fake INR)" if self.paper else "LIVE F&O REAL MONEY"
        logger.info(f"[FNO] IndiaFnoBroker mode: {mode}")

    def get_option_chain(self, symbol: str, expiry_date: str | None = None) -> dict[str, Any]:
        """Fetch option chain (strikes, premiums, OI, IV, Delta, Theta) from Dhan API."""
        if not self.dhan_broker.dhan:
            return {}

        sec_info = resolve_instrument_info(symbol, exchange_segment="NSE_FNO")
        # Option chain wants INDEX underlying id (NIFTY=13), not a futures token
        if symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            underlying_scrip = {"NIFTY": "13", "BANKNIFTY": "25", "FINNIFTY": "27"}.get(
                symbol, sec_info.get("security_id", symbol)
            )
        else:
            underlying_scrip = sec_info.get("security_id", symbol)

        try:
            method = getattr(self.dhan_broker.dhan, "option_chain", None) or getattr(
                self.dhan_broker.dhan, "get_option_chain", None
            )
            if method:
                # Probe common kwarg shapes
                try:
                    resp = method(
                        under_security_id=int(underlying_scrip),
                        under_exchange_segment="NSE_FNO",
                        expiry=expiry_date or "",
                    )
                except TypeError:
                    resp = method(
                        underlying_scrip=underlying_scrip,
                        underlying_seg="NSE_FNO",
                        expiry=expiry_date or "",
                    )
                if self.dhan_broker._ok(resp):
                    return self.dhan_broker._data(resp) or {}
        except Exception as e:
            logger.debug(f"[FNO] Option chain fetch for {symbol}: {e}")
        return {}

    def get_atm_strike(self, symbol: str, spot_price: float, chain: dict | None = None) -> float:
        """ATM / near-ATM strike from chain when available, else step rounding."""
        if spot_price <= 0:
            return 0.0
        if chain and isinstance(chain, dict):
            strikes_block = chain.get("oc") or chain.get("data") or chain.get("strikes") or {}
            if isinstance(strikes_block, dict) and strikes_block:
                try:
                    keys = []
                    for k in strikes_block.keys():
                        try:
                            keys.append(float(k))
                        except (TypeError, ValueError):
                            continue
                    if keys:
                        return min(keys, key=lambda s: abs(s - spot_price))
                except Exception:
                    pass
        step = 50.0 if symbol in ("NIFTY", "FINNIFTY") else (100.0 if symbol == "BANKNIFTY" else 20.0)
        return round(round(spot_price / step) * step, 2)

    def fetch_live_option_premium(self, symbol: str, strike: float, option_type: str) -> float:
        """Fetches live option premium from Option Chain. Never invents premiums for LIVE."""
        chain = self.get_option_chain(symbol)
        if chain and isinstance(chain, dict):
            strikes = chain.get("oc") or chain.get("data") or {}
            if isinstance(strikes, dict):
                strike_str = str(int(strike)) if float(strike).is_integer() else str(strike)
                st_data = strikes.get(strike_str) or strikes.get(str(strike)) or strikes.get(str(float(strike)))
                if isinstance(st_data, dict):
                    opt_info = st_data.get(option_type.lower()) or st_data.get(option_type.upper())
                    if isinstance(opt_info, dict):
                        prem = float(opt_info.get("last_price") or opt_info.get("ltp") or 0.0)
                        if prem > 0:
                            return prem

        # LIVE: strictly NEVER invent premiums
        if config.INDIA_FNO_LIVE_CONFIRMED or not config.INDIA_FNO_PAPER:
            logger.error(
                f"[LIVE F&O] Option chain LTP unavailable for {symbol} {strike} {option_type} — rejecting."
            )
            return 0.0

        # Paper estimates only when explicitly allowed (unsafe for live rehearsal)
        if not getattr(config, "ALLOW_PAPER_PRICE_ESTIMATES", False):
            logger.warning(
                f"[FNO] No live premium for {symbol} {strike} {option_type} — "
                f"refusing paper estimate (set ALLOW_PAPER_PRICE_ESTIMATES=true to override)"
            )
            return 0.0

        logger.info(
            f"[PAPER F&O ESTIMATE] Using paper estimation mark for {symbol} {strike} {option_type}"
        )
        return round(max(50.0, strike * 0.008), 2)

    def _cooldown_ok(self, key: str, cooldown_sec: float = 1800.0) -> bool:
        last = self._cooldowns.get(key, 0.0)
        if time.time() - last < cooldown_sec:
            logger.info(f"[FNO] Cooldown active for {key} — skip")
            return False
        return True

    def place_option_order(
        self,
        symbol: str,
        strike: float,
        option_type: str,
        transaction_type: str = "BUY",
        lots: int = 1,
        limit_price: float = 0.0,
        stop_loss: float = 0.0,
        take_profit: float = 0.0,
    ) -> str | None:
        """Place an option order (paper or live with proper NSE_FNO exchange segment)."""
        if self.risk_mgr.is_kill_switch_active:
            logger.warning("[FNO] Kill switch active — order blocked")
            return None

        # Spreads / CSP gated off unless explicitly enabled
        strategy = (config.INDIA_FNO_STRATEGY or "directional_options").lower()
        if strategy not in ("directional_options", "directional", ""):
            logger.warning(f"[FNO] Strategy '{strategy}' gated off — only directional_options enabled")
            return None

        lot_size = get_fno_lot_size(symbol)
        max_lots = getattr(config, "INDIA_FNO_MAX_LOTS", 2)
        lots = min(max(1, lots), max_lots)
        qty = lots * lot_size

        contract_key = f"{symbol}-{int(strike)}-{option_type.upper()}"
        if not self._cooldown_ok(contract_key):
            return None

        positions = self.get_open_positions()
        if not self.risk_mgr.can_open_position(contract_key, positions):
            return None

        estimated = False
        if limit_price <= 0:
            limit_price = self.fetch_live_option_premium(symbol, strike, option_type)
            # fetch_live_option_premium already refuses estimates unless allowed
            estimated = False

        ok_prem, prem_err = validate_option_premium(limit_price, estimated=estimated)
        if not ok_prem:
            logger.error(f"[FNO] Cannot place order for {symbol} {strike} {option_type}: {prem_err}")
            return None

        if limit_price <= 0:
            logger.error(f"[FNO] Cannot place order for {symbol} {strike} {option_type}: premium is 0")
            return None

        total_cost = qty * limit_price
        if total_cost > config.INDIA_FNO_CAPITAL_CAP:
            logger.warning(
                f"[FNO CAPITAL CAP] Order cost (Rs{total_cost:,.2f}) exceeds "
                f"segment cap (Rs{config.INDIA_FNO_CAPITAL_CAP:,.2f})"
            )
            return None

        # Shared INR wallet soft check via equity broker funds when live
        if not self.paper and self.dhan_broker:
            try:
                acct = self.dhan_broker.get_account_info()
                if acct and float(acct.get("available_cash") or 0) < total_cost * 0.2:
                    # Options buy requires premium; margin approx — block if cash tiny
                    logger.warning("[FNO] Available funds appear insufficient — blocking live order")
                    return None
            except Exception:
                pass

        if self.paper is not None:
            oid = self.paper.buy_option(
                symbol=symbol,
                strike=strike,
                option_type=option_type,
                lots=lots,
                lot_size=lot_size,
                premium=limit_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            if oid and oid != "REJECTED_FUNDS":
                self._cooldowns[contract_key] = time.time()
            return oid if oid != "REJECTED_FUNDS" else None

        if not config.INDIA_FNO_LIVE_CONFIRMED:
            logger.critical("[FNO] Live order blocked: INDIA_FNO_LIVE_CONFIRMED is False.")
            return None

        if estimated:
            logger.error("[LIVE F&O] Refusing invented/estimated premium")
            return None

        sec_info = resolve_option_contract(symbol, strike, option_type)
        if not sec_info:
            logger.error(f"[LIVE F&O] Missing option contract for {symbol} {strike} {option_type}")
            return None
        sec_id = sec_info.get("security_id")
        if is_placeholder_security_id(sec_id) or not self.dhan_broker.dhan:
            logger.error(f"[LIVE F&O] Invalid security_id for {symbol} {strike} {option_type}")
            return None

        # Margin check before live
        try:
            margin = self.dhan_broker.get_margin_required(symbol, qty, limit_price, "MARGIN")
            if margin > config.INDIA_FNO_CAPITAL_CAP:
                logger.warning(f"[FNO] Margin Rs{margin:,.2f} exceeds capital cap — block")
                return None
        except Exception:
            pass

        logger.warning(
            f"[LIVE F&O] ORDER | {symbol} {strike} {option_type} | Lots={lots} (Qty={qty}) @ Rs{limit_price:.2f}"
        )

        try:
            raw = self.dhan_broker.dhan.place_order(
                security_id=str(sec_id),
                exchange_segment=getattr(dhanhq, "NSE_FNO", "NSE_FNO"),
                transaction_type=getattr(dhanhq, transaction_type.upper(), "BUY"),
                quantity=int(qty),
                order_type=getattr(dhanhq, "LIMIT", "LIMIT"),
                product_type=getattr(dhanhq, "MARGIN", "MARGIN"),
                price=float(limit_price),
                trigger_price=0,
                validity=getattr(dhanhq, "DAY", "DAY"),
                tag=f"BOT-FNO-{symbol}"[:20],
            )
            oid = self.dhan_broker._extract_order_id(raw)
            if oid:
                self._cooldowns[contract_key] = time.time()
            return oid
        except Exception as e:
            logger.error(f"[FNO] Failed to place live order: {e}", exc_info=True)
            return None

    def check_exits(self) -> list[str]:
        """Paper SL/TP exit check for open F&O positions."""
        closed = []
        if self.paper is None:
            return closed
        positions = dict(self.paper.positions)
        for key, pos in positions.items():
            mark = float(pos.get("avg_price") or 0)
            # Prefer live premium mark
            try:
                prem = self.fetch_live_option_premium(
                    pos["symbol"], float(pos["strike"]), pos["option_type"]
                )
                if prem > 0:
                    mark = prem
            except Exception:
                pass
            sl = float(pos.get("stop_loss") or 0)
            tp = float(pos.get("take_profit") or 0)
            reason = None
            if sl > 0 and mark <= sl:
                reason = "stop_loss"
            elif tp > 0 and mark >= tp:
                reason = "take_profit"
            if reason and self.paper.close_position(key, mark, reason=reason):
                closed.append(key)
        return closed

    def get_open_positions(self) -> dict:
        if self.paper is not None:
            marks = {}
            for k, pos in self.paper.positions.items():
                marks[k] = pos["avg_price"]
            return self.paper.get_open_positions(marks)
        return self.dhan_broker.get_open_positions()

    def capital_utilization(self) -> dict:
        positions = self.get_open_positions()
        used = sum(float(p.get("market_value") or 0) for p in positions.values())
        cap = float(config.INDIA_FNO_CAPITAL_CAP)
        cash = float(self.paper.cash) if self.paper else None
        return {
            "used": used,
            "cap": cap,
            "utilization_pct": round((used / cap) * 100, 2) if cap else 0,
            "cash": cash,
            "positions": len(positions),
            "kill_switch": self.risk_mgr.is_kill_switch_active,
        }

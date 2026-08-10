"""
bot_state.py — Shared in-process state for bot loops + dashboard
================================================================
Thread-safe signal cache, health timestamps, and India SOD equity.
"""

from __future__ import annotations

import threading
import time
from datetime import date
from typing import Any

_lock = threading.Lock()

_signals: dict[str, dict[str, Any]] = {}  # market -> {symbol: payload}
_scout: dict[str, Any] = {}  # market -> near-setups blob (display only)
_health: dict[str, Any] = {
    "us_last_cycle": None,
    "india_last_cycle": None,
    "india_scout_last_cycle": None,
    "us_last_error": None,
    "india_last_error": None,
    "india_scout_last_error": None,
    "alpaca_429_count": 0,
    "started_at": time.time(),
}
_india_sod: dict[str, Any] = {"date": None, "equity": None}
_us_sod: dict[str, Any] = {"date": None, "equity": None}


def publish_signals(market: str, items: list[dict]) -> None:
    """items: [{symbol, signal, price, rsi, reason, ...}]"""
    key = market.upper()
    by_sym = {str(i["symbol"]): i for i in items if i.get("symbol")}
    with _lock:
        _signals[key] = {
            "updated_at": time.time(),
            "items": by_sym,
            "list": list(by_sym.values()),
        }


def get_signals(market: str, max_age_sec: float = 600.0) -> list[dict] | None:
    key = market.upper()
    with _lock:
        blob = _signals.get(key)
        if not blob:
            return None
        if time.time() - blob["updated_at"] > max_age_sec:
            return None
        return list(blob["list"])


def publish_scout(market: str, items: list[dict], *, meta: dict | None = None) -> None:
    """Publish Near Setups rows (close-but-not-confirmed; scout BUYs go through trade path)."""
    key = market.upper()
    with _lock:
        _scout[key] = {
            "updated_at": time.time(),
            "list": list(items),
            "meta": dict(meta or {}),
            "trade_eligible": True,
        }


def get_scout(market: str, max_age_sec: float = 3600.0) -> dict | None:
    """Return scout blob {list, meta, updated_at, trade_eligible} or None if stale/missing."""
    key = market.upper()
    with _lock:
        blob = _scout.get(key)
        if not blob:
            return None
        if time.time() - blob["updated_at"] > max_age_sec:
            return None
        return {
            "list": list(blob["list"]),
            "meta": dict(blob.get("meta") or {}),
            "updated_at": blob["updated_at"],
            "trade_eligible": bool(blob.get("trade_eligible", True)),
        }


def mark_cycle(market: str, error: str | None = None) -> None:
    m = market.upper()
    if m in ("INDIA_SCOUT", "SCOUT"):
        key = "india_scout"
    else:
        key = "us" if m == "US" else "india"
    with _lock:
        _health[f"{key}_last_cycle"] = time.time()
        if error:
            _health[f"{key}_last_error"] = error
        elif error is None and f"{key}_last_error" in _health:
            # clear only when explicitly healthy
            pass


def mark_healthy(market: str) -> None:
    m = market.upper()
    if m in ("INDIA_SCOUT", "SCOUT"):
        key = "india_scout"
    else:
        key = "us" if m == "US" else "india"
    with _lock:
        _health[f"{key}_last_cycle"] = time.time()
        _health[f"{key}_last_error"] = None


def note_alpaca_429() -> None:
    with _lock:
        _health["alpaca_429_count"] = int(_health.get("alpaca_429_count") or 0) + 1


def get_health() -> dict:
    with _lock:
        h = dict(_health)
        now = time.time()
        us_age = (now - h["us_last_cycle"]) if h.get("us_last_cycle") else None
        in_age = (now - h["india_last_cycle"]) if h.get("india_last_cycle") else None
        sc_age = (
            (now - h["india_scout_last_cycle"]) if h.get("india_scout_last_cycle") else None
        )
        h["us_cycle_age_sec"] = round(us_age, 1) if us_age is not None else None
        h["india_cycle_age_sec"] = round(in_age, 1) if in_age is not None else None
        h["india_scout_cycle_age_sec"] = round(sc_age, 1) if sc_age is not None else None
        h["uptime_sec"] = round(now - h.get("started_at", now), 1)
        return h


def reset_sod_for_tests() -> None:
    """Clear SOD baselines (unit tests only)."""
    with _lock:
        _india_sod["date"] = None
        _india_sod["equity"] = None
        _us_sod["date"] = None
        _us_sod["equity"] = None


def _rebaseline_sod_if_needed(
    market: str, stored: float, cur: float, sod: dict[str, Any]
) -> None:
    """
    Adjust sticky SOD when equity jumps for non-trading reasons.

    - Large drop (>15%): heal inflated marks / bad quotes
    - Large rise (>15%): treat as deposit / fund transfer (not Daily P&L)
    Normal trading moves stay sticky so Daily P&L still works.
    """
    if stored <= 0 or cur <= 0:
        return
    logger = __import__("logging").getLogger(__name__)
    drop_pct = (stored - cur) / stored
    rise_pct = (cur - stored) / stored
    if drop_pct > 0.15:
        logger.warning(
            f"[{market}] Rebaselining start-of-day equity "
            f"{stored:,.2f} → {cur:,.2f} (inflated mark heal)"
        )
        sod["equity"] = cur
    elif rise_pct > 0.15:
        logger.info(
            f"[{market}] Rebaselining start-of-day equity "
            f"{stored:,.2f} → {cur:,.2f} (deposit / funding detected)"
        )
        sod["equity"] = cur


def india_sod_equity(current_equity: float) -> float:
    """Return start-of-day equity for India (sticky per calendar day)."""
    today = date.today().isoformat()
    with _lock:
        if _india_sod.get("date") != today or _india_sod.get("equity") is None:
            _india_sod["date"] = today
            _india_sod["equity"] = float(current_equity)
        else:
            _rebaseline_sod_if_needed(
                "INDIA",
                float(_india_sod["equity"]),
                float(current_equity),
                _india_sod,
            )
        return float(_india_sod["equity"])


def us_sod_equity(current_equity: float) -> float:
    """Return start-of-day equity for US (sticky per America/New_York date)."""
    from datetime import datetime as dt
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    today_et = dt.now(ZoneInfo("America/New_York")).date().isoformat()
    with _lock:
        if _us_sod.get("date") != today_et or _us_sod.get("equity") is None:
            _us_sod["date"] = today_et
            _us_sod["equity"] = float(current_equity)
        else:
            _rebaseline_sod_if_needed(
                "US",
                float(_us_sod["equity"]),
                float(current_equity),
                _us_sod,
            )
        return float(_us_sod["equity"])

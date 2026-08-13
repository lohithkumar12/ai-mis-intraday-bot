"""
bot_state.py — Shared in-process state for bot loops + dashboard
================================================================
Thread-safe signal cache, health timestamps, and India SOD equity.
Kill-switch is persisted under the journal data dir so Docker restarts
do not clear a same-IST-day daily_drawdown latch.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()
_log = logging.getLogger(__name__)

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
# Shared kill-switch (loop + dashboard must use the same flag)
_kill: dict[str, dict[str, Any]] = {}
_kill_loaded = False
# Nifty/BankNifty 5m tide latch (entries only — not persisted)
_tide: dict[str, Any] = {
    "bearish": False,
    "nifty_pct": None,
    "banknifty_pct": None,
    "reason": "",
    "updated_at": None,
}
# Broker-open / no local SL-TP (zombie) rescues — dashboard reason overlay
_zombies: dict[str, dict[str, Any]] = {}


def _trading_day_iso(market: str) -> str:
    """IST calendar day for India; NY for US; local date otherwise."""
    key = str(market or "").upper()
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    if key == "INDIA":
        return datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    if key == "US":
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    return date.today().isoformat()


def _kill_state_path() -> Path:
    try:
        import config

        journal = Path(str(getattr(config, "TRADE_JOURNAL_PATH", "trade_journal.db")))
    except Exception:
        journal = Path("trade_journal.db")
    return journal.expanduser().resolve().parent / "kill_switch.json"


def _persist_kill_unlocked() -> None:
    """Write _kill to disk (caller holds _lock)."""
    try:
        path = _kill_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            k: {
                "active": bool(v.get("active")),
                "reason": str(v.get("reason") or "")[:200],
                "day": v.get("day"),
            }
            for k, v in _kill.items()
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        _log.warning(f"kill_switch persist failed: {e}")


def _load_kill_from_disk_unlocked() -> None:
    """Merge disk state for today's trading day into memory (caller holds _lock)."""
    global _kill_loaded
    path = _kill_state_path()
    if not path.is_file():
        _kill_loaded = True
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            _kill_loaded = True
            return
        for market, row in raw.items():
            if not isinstance(row, dict):
                continue
            key = str(market).upper()
            day = str(row.get("day") or "")
            today = _trading_day_iso(key)
            if day != today:
                # Stale prior-day latch — leave inactive in memory; prune on next persist
                continue
            if bool(row.get("active")):
                _kill[key] = {
                    "active": True,
                    "reason": str(row.get("reason") or "")[:200],
                    "day": day,
                }
        _kill_loaded = True
        active = [k for k, v in _kill.items() if v.get("active")]
        if active:
            _log.warning(
                f"Restored kill-switch from disk for today: {active} "
                f"({path})"
            )
    except Exception as e:
        _log.warning(f"kill_switch load failed: {e}")
        _kill_loaded = True


def ensure_kill_loaded() -> None:
    """Idempotent disk hydrate (call on startup / before checks)."""
    with _lock:
        if not _kill_loaded:
            _load_kill_from_disk_unlocked()


def publish_signals(market: str, items: list[dict]) -> None:
    """items: [{symbol, signal, price, rsi, reason, ...}]"""
    key = market.upper()
    by_sym = {str(i["symbol"]): i for i in items if i.get("symbol")}
    with _lock:
        if key == "INDIA":
            for sym, z in _zombies.items():
                row = dict(by_sym.get(sym) or {"symbol": sym, "signal": "HOLD", "price": 0})
                row["reason"] = str(z.get("reason") or "ZOMBIE RESCUED")
                row["signal"] = "HOLD"
                if z.get("ltp"):
                    row["price"] = z.get("ltp")
                by_sym[sym] = row
        _signals[key] = {
            "updated_at": time.time(),
            "items": by_sym,
            "list": list(by_sym.values()),
        }


def get_signals(market: str, max_age_sec: float = 600.0) -> list[dict] | None:
    key = market.upper()
    with _lock:
        blob = _signals.get(key)
        zombies = dict(_zombies) if key == "INDIA" else {}
        if not blob:
            if not zombies:
                return None
            return [
                {
                    "symbol": sym,
                    "signal": "HOLD",
                    "price": z.get("ltp") or 0,
                    "reason": str(z.get("reason") or "ZOMBIE RESCUED"),
                }
                for sym, z in zombies.items()
            ]
        if time.time() - blob["updated_at"] > max_age_sec and not zombies:
            return None
        rows = [dict(r) for r in blob["list"]]
        have = {str(r.get("symbol") or "").upper() for r in rows}
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            z = zombies.get(sym)
            if z:
                row["reason"] = str(z.get("reason") or "ZOMBIE RESCUED")
                row["signal"] = "HOLD"
        for sym, z in zombies.items():
            if sym in have:
                continue
            rows.append(
                {
                    "symbol": sym,
                    "signal": "HOLD",
                    "price": z.get("ltp") or 0,
                    "reason": str(z.get("reason") or "ZOMBIE RESCUED"),
                }
            )
        return rows


def note_zombie_rescued(
    symbol: str,
    *,
    ltp: float = 0.0,
    sl: float = 0.0,
    tp: float = 0.0,
    qty: int = 0,
) -> None:
    sym = str(symbol or "").upper()
    if not sym:
        return
    with _lock:
        _zombies[sym] = {
            "reason": "ZOMBIE RESCUED",
            "ltp": float(ltp or 0),
            "sl": float(sl or 0),
            "tp": float(tp or 0),
            "qty": int(qty or 0),
            "at": time.time(),
        }
        blob = _signals.get("INDIA")
        if blob is None:
            blob = {"updated_at": time.time(), "items": {}, "list": []}
            _signals["INDIA"] = blob
        row = dict(blob["items"].get(sym) or {"symbol": sym, "signal": "HOLD"})
        row["reason"] = "ZOMBIE RESCUED"
        row["signal"] = "HOLD"
        if ltp:
            row["price"] = float(ltp)
        blob["items"][sym] = row
        blob["list"] = list(blob["items"].values())
        blob["updated_at"] = time.time()


def list_zombie_rescues() -> list[dict[str, Any]]:
    with _lock:
        return [{"symbol": k, **dict(v)} for k, v in _zombies.items()]


def clear_zombie_rescue(symbol: str) -> None:
    with _lock:
        _zombies.pop(str(symbol or "").upper(), None)


def reset_zombies_for_tests() -> None:
    with _lock:
        _zombies.clear()


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


def reset_tide_for_tests() -> None:
    with _lock:
        _tide["bearish"] = False
        _tide["nifty_pct"] = None
        _tide["banknifty_pct"] = None
        _tide["reason"] = ""
        _tide["updated_at"] = None


def is_tide_bearish() -> bool:
    with _lock:
        return bool(_tide.get("bearish"))


def get_tide_state() -> dict[str, Any]:
    with _lock:
        return dict(_tide)


def set_tide_state(
    *,
    bearish: bool,
    nifty_pct: float | None = None,
    banknifty_pct: float | None = None,
    reason: str = "",
) -> None:
    with _lock:
        _tide["bearish"] = bool(bearish)
        _tide["nifty_pct"] = nifty_pct
        _tide["banknifty_pct"] = banknifty_pct
        _tide["reason"] = str(reason or "")[:200]
        _tide["updated_at"] = time.time()


def reset_kill_for_tests() -> None:
    global _kill_loaded
    with _lock:
        _kill.clear()
        _kill_loaded = True
        # Best-effort wipe of persist file so unit tests stay isolated
        try:
            path = _kill_state_path()
            if path.is_file():
                path.unlink()
        except Exception:
            pass


def is_kill_switch_active(market: str) -> bool:
    key = str(market or "").upper()
    with _lock:
        if not _kill_loaded:
            _load_kill_from_disk_unlocked()
        row = _kill.get(key) or {}
        if not row.get("active"):
            return False
        # Stale latch from a prior trading day → clear
        day = str(row.get("day") or "")
        today = _trading_day_iso(key)
        if day and day != today:
            _kill[key] = {"active": False, "reason": "", "day": None}
            _persist_kill_unlocked()
            return False
        return True


def kill_switch_reason(market: str) -> str:
    key = str(market or "").upper()
    with _lock:
        if not _kill_loaded:
            _load_kill_from_disk_unlocked()
        row = _kill.get(key) or {}
        return str(row.get("reason") or "")


def activate_kill_switch(market: str, reason: str = "manual") -> None:
    key = str(market or "").upper()
    with _lock:
        if not _kill_loaded:
            _load_kill_from_disk_unlocked()
        _kill[key] = {
            "active": True,
            "reason": str(reason or "manual")[:200],
            "day": _trading_day_iso(key),
        }
        _persist_kill_unlocked()
        _log.critical(
            f"[{key}] Kill switch ACTIVATED ({_kill[key]['reason']}) "
            f"day={_kill[key]['day']} — persisted"
        )


def reset_kill_switch(market: str) -> None:
    """Clear latch (caller should gate to new trading day only for daily_drawdown)."""
    key = str(market or "").upper()
    with _lock:
        if not _kill_loaded:
            _load_kill_from_disk_unlocked()
        _kill[key] = {"active": False, "reason": "", "day": None}
        _persist_kill_unlocked()


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

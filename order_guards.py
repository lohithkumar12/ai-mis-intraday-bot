"""
order_guards.py — Pre/post trade guards (cooldown after reject / pending)
Plus thread-safe buy reservation and exit-in-flight locks for Core+Scout.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# symbol -> {"until": epoch, "reason": str}
_buy_blocks: dict[str, dict[str, Any]] = {}
# symbol -> {"owner": str, "until": epoch}
_buy_reservations: dict[str, dict[str, Any]] = {}
# symbols currently having a sell/close in flight
_exit_inflight: set[str] = set()


def _cooldown_reject_sec() -> float:
    return float(getattr(config, "BUY_REJECT_COOLDOWN_SEC", 900))


def _cooldown_pending_sec() -> float:
    return float(getattr(config, "BUY_PENDING_COOLDOWN_SEC", 600))


def _reserve_ttl_sec() -> float:
    return float(getattr(config, "BUY_RESERVE_TTL_SEC", 45))


def block_buy(symbol: str, seconds: float | None = None, reason: str = "cooldown") -> None:
    """Prevent new BUY attempts for `symbol` until now+seconds."""
    sym = str(symbol).strip().upper()
    if not sym:
        return
    sec = float(seconds) if seconds is not None else _cooldown_reject_sec()
    until = time.time() + max(0.0, sec)
    with _lock:
        _buy_blocks[sym] = {"until": until, "reason": reason}
    logger.warning(f"{sym}: BUY blocked for {sec:.0f}s ({reason})")


def block_buy_after_reject(symbol: str, detail: str = "") -> None:
    reason = f"order_rejected{': ' + detail if detail else ''}"
    block_buy(symbol, _cooldown_reject_sec(), reason=reason[:120])


def block_buy_after_pending(symbol: str, order_id: str = "") -> None:
    reason = f"order_pending:{order_id}" if order_id else "order_pending"
    block_buy(symbol, _cooldown_pending_sec(), reason=reason[:120])


def clear_buy_block(symbol: str) -> None:
    sym = str(symbol).strip().upper()
    with _lock:
        _buy_blocks.pop(sym, None)


def is_buy_blocked(symbol: str) -> tuple[bool, str]:
    """Return (blocked, reason). Expired blocks are cleared."""
    sym = str(symbol).strip().upper()
    now = time.time()
    with _lock:
        row = _buy_blocks.get(sym)
        if not row:
            return False, ""
        until = float(row.get("until") or 0)
        if until <= now:
            _buy_blocks.pop(sym, None)
            return False, ""
        left = int(until - now)
        reason = str(row.get("reason") or "cooldown")
        return True, f"{reason} ({left}s left)"


def try_reserve_buy(symbol: str, owner: str = "core") -> tuple[bool, str]:
    """
    Atomically reserve symbol for one BUY attempt (Core/Scout duplicate guard).
    Returns (ok, reason). Caller must release_buy_reservation on failure/completion.
    """
    sym = str(symbol).strip().upper()
    if not sym:
        return False, "empty symbol"
    now = time.time()
    ttl = _reserve_ttl_sec()
    with _lock:
        # purge expired
        expired = [k for k, v in _buy_reservations.items() if float(v.get("until") or 0) <= now]
        for k in expired:
            _buy_reservations.pop(k, None)
        row = _buy_reservations.get(sym)
        if row and float(row.get("until") or 0) > now:
            return False, f"buy_reserved_by_{row.get('owner')}"
        _buy_reservations[sym] = {
            "owner": str(owner or "core"),
            "until": now + ttl,
        }
    return True, ""


def release_buy_reservation(symbol: str) -> None:
    sym = str(symbol).strip().upper()
    with _lock:
        _buy_reservations.pop(sym, None)


def try_begin_exit(symbol: str) -> bool:
    """Return True if this caller owns the exit; False if already in flight."""
    sym = str(symbol).strip().upper()
    if not sym:
        return False
    with _lock:
        if sym in _exit_inflight:
            return False
        _exit_inflight.add(sym)
        return True


def end_exit(symbol: str) -> None:
    sym = str(symbol).strip().upper()
    with _lock:
        _exit_inflight.discard(sym)


def is_exit_inflight(symbol: str) -> bool:
    sym = str(symbol).strip().upper()
    with _lock:
        return sym in _exit_inflight


def reset_for_tests() -> None:
    with _lock:
        _buy_blocks.clear()
        _buy_reservations.clear()
        _exit_inflight.clear()

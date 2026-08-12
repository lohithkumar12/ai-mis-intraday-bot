"""
trade_journal.py — Persistent trade log + performance metrics
==============================================================
SQLite journal of entries/exits with PnL, reason, and market.
Used by live loops and the dashboard for win rate / PF / max DD.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _db_path() -> Path:
    return Path(config.TRADE_JOURNAL_PATH).resolve()


@contextmanager
def _conn():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _lock, _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                entry_price REAL,
                exit_price REAL,
                stop_price REAL,
                take_profit REAL,
                pnl REAL,
                pnl_pct REAL,
                reason TEXT,
                strategy TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                meta_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                equity REAL NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
    logger.info(f"Trade journal ready at {_db_path()}")


def record_entry(
    market: str,
    symbol: str,
    qty: int,
    entry_price: float,
    stop_price: float | None = None,
    take_profit: float | None = None,
    reason: str = "",
    strategy: str = "",
    meta: dict | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                market, symbol, side, qty, entry_price, stop_price, take_profit,
                reason, strategy, status, opened_at, meta_json
            ) VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                market.upper(),
                symbol,
                qty,
                entry_price,
                stop_price,
                take_profit,
                reason,
                strategy,
                now,
                json.dumps(meta or {}),
            ),
        )
        trade_id = int(cur.lastrowid)
    logger.info(
        f"[JOURNAL] ENTRY #{trade_id} {market} {symbol} qty={qty} @ {entry_price:.2f}"
    )
    return trade_id


def record_exit(
    market: str,
    symbol: str,
    exit_price: float,
    reason: str = "",
    qty: int | None = None,
) -> Optional[dict]:
    """Close the most recent open trade for symbol/market. Returns closed row dict."""
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM trades
            WHERE market=? AND symbol=? AND status='open'
            ORDER BY id DESC LIMIT 1
            """,
            (market.upper(), symbol),
        ).fetchone()
        if not row:
            logger.warning(f"[JOURNAL] No open trade to close for {market} {symbol}")
            return None

        entry = float(row["entry_price"] or 0)
        q = int(qty if qty is not None else row["qty"])
        pnl = (exit_price - entry) * q
        pnl_pct = ((exit_price - entry) / entry) if entry else 0.0
        reason_final = reason or row["reason"] or "exit"

        conn.execute(
            """
            UPDATE trades SET
                exit_price=?, pnl=?, pnl_pct=?, reason=?,
                status='closed', closed_at=?, qty=?
            WHERE id=?
            """,
            (exit_price, pnl, pnl_pct, reason_final, now, q, row["id"]),
        )
        result = {
            "id": row["id"],
            "market": row["market"],
            "symbol": symbol,
            "qty": q,
            "entry_price": entry,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason_final,
        }
    logger.info(
        f"[JOURNAL] EXIT #{result['id']} {market} {symbol} "
        f"PnL={pnl:+.2f} ({pnl_pct:+.2%}) reason={reason_final}"
    )
    return result


def snapshot_equity(market: str, equity: float):
    now = datetime.now(timezone.utc).isoformat()
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO equity_snapshots (market, equity, recorded_at) VALUES (?,?,?)",
            (market.upper(), float(equity), now),
        )


def recent_trades(limit: int = 50, market: str | None = None) -> list[dict]:
    with _lock, _conn() as conn:
        if market:
            rows = conn.execute(
                "SELECT * FROM trades WHERE market=? ORDER BY id DESC LIMIT ?",
                (market.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def equity_curve(market: str | None = None, limit: int = 200) -> list[dict]:
    with _lock, _conn() as conn:
        if market:
            rows = conn.execute(
                """
                SELECT market, equity, recorded_at FROM equity_snapshots
                WHERE market=? ORDER BY id DESC LIMIT ?
                """,
                (market.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT market, equity, recorded_at FROM equity_snapshots
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    data = [dict(r) for r in rows]
    data.reverse()
    return data


def list_open_trades(market: str) -> list[dict]:
    """Open journal rows for a market (symbol, qty, entry_price, …)."""
    with _lock, _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM trades
            WHERE market=? AND status='open'
            ORDER BY id ASC
            """,
            (market.upper(),),
        ).fetchall()
    return [dict(r) for r in rows]


def reconcile_broker_flats(
    market: str,
    broker_open_symbols: set[str] | list[str] | None,
    *,
    price_lookup,
    reason: str = "broker_flat",
) -> list[str]:
    """
    Journal-exit any open trade whose symbol is no longer in broker open positions
    (broker SL/TP, manual close on Dhan, etc.).
    price_lookup(symbol) -> float exit mark (LTP / last known).
    """
    open_syms = {str(s).upper() for s in (broker_open_symbols or [])}
    closed: list[str] = []
    for row in list_open_trades(market):
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or symbol in open_syms:
            continue
        entry = float(row.get("entry_price") or 0)
        try:
            px = float(price_lookup(symbol) or 0)
        except Exception:
            px = 0.0
        if px <= 0:
            px = entry
        if px <= 0:
            logger.warning(
                f"[JOURNAL] skip broker_flat {market} {symbol}: no usable exit price"
            )
            continue
        out = record_exit(market, symbol, px, reason=reason)
        if out:
            closed.append(symbol)
            logger.warning(
                f"[JOURNAL] {market} {symbol} closed vs broker flat @ {px:.2f} "
                f"(reason={reason})"
            )
    return closed


def realized_pnl_today(market: str, *, tz_name: str) -> float:
    """Sum closed-trade PnL for the local calendar day in tz_name.

    Skips exits with exit_price <= 0 (bogus zero-LTP closes that corrupt the book).
    """
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        import pytz

        tz = pytz.timezone(tz_name)

    today = datetime.now(tz).date()
    with _lock, _conn() as conn:
        rows = conn.execute(
            """
            SELECT pnl, exit_price, closed_at FROM trades
            WHERE market=? AND status='closed' AND closed_at IS NOT NULL
            """,
            (market.upper(),),
        ).fetchall()

    total = 0.0
    for row in rows:
        exit_px = float(row["exit_price"] or 0)
        if exit_px <= 0:
            continue
        raw = row["closed_at"]
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt.astimezone(tz).date() != today:
                continue
        except Exception:
            continue
        total += float(row["pnl"] or 0)
    return total


def performance_stats(market: str | None = None) -> dict[str, Any]:
    """Win rate, profit factor, max drawdown from closed trades + equity snaps."""
    with _lock, _conn() as conn:
        if market:
            closed = conn.execute(
                "SELECT pnl FROM trades WHERE status='closed' AND market=?",
                (market.upper(),),
            ).fetchall()
            snaps = conn.execute(
                "SELECT equity FROM equity_snapshots WHERE market=? ORDER BY id",
                (market.upper(),),
            ).fetchall()
            open_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status='open' AND market=?",
                (market.upper(),),
            ).fetchone()
        else:
            closed = conn.execute(
                "SELECT pnl FROM trades WHERE status='closed'"
            ).fetchall()
            snaps = conn.execute(
                "SELECT equity FROM equity_snapshots ORDER BY id"
            ).fetchall()
            open_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status='open'"
            ).fetchone()

    pnls = [float(r["pnl"] or 0) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )
    win_rate = (len(wins) / len(pnls)) if pnls else 0.0
    net_pnl = sum(pnls)

    # Max drawdown from equity snapshots
    max_dd = 0.0
    peak = None
    for row in snaps:
        eq = float(row["equity"])
        if peak is None or eq > peak:
            peak = eq
        if peak and peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

    return {
        "trades": len(pnls),
        "open_trades": int(open_rows["n"]) if open_rows else 0,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "profit_factor_display": (
            "∞" if profit_factor == float("inf") else round(profit_factor, 2)
        ),
        "net_pnl": round(net_pnl, 2),
        "max_drawdown": round(max_dd, 4),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
    }


# Initialize on import so first write never fails
try:
    init_db()
except Exception as e:
    logger.error(f"Trade journal init failed: {e}")

"""
nse_tick.py — NSE cash-equity tick size helpers
==============================================
Price bands aligned with NSE CM tick revision (effective Apr 2025):

  < 250          → 0.01
  250 – 1,000    → 0.05
  1,001 – 5,000  → 0.10
  5,001 – 10,000 → 0.50
  10,001 – 20,000→ 1.00
  > 20,000       → 5.00

Bands are applied from the order price itself (practical for algos).
NSE also reviews monthly from prior-month close; price-based banding
covers DIVISLAB-style rejects (e.g. 8308.30 with 0.50 tick).
"""

from __future__ import annotations

import math
from typing import Literal


RoundMode = Literal["nearest", "ceil", "floor"]


def nse_equity_tick_size(price: float) -> float:
    """Return NSE equity tick size for a given price level."""
    p = float(price)
    if p <= 0:
        return 0.05
    if p < 250:
        return 0.01
    if p <= 1000:
        return 0.05
    if p <= 5000:
        return 0.10
    if p <= 10000:
        return 0.50
    if p <= 20000:
        return 1.00
    return 5.00


def round_to_nse_tick(
    price: float,
    mode: RoundMode = "nearest",
    tick: float | None = None,
) -> float:
    """
    Round `price` to a valid NSE tick multiple.

    mode:
      nearest — banker's-safe half-up via decimal steps
      ceil    — buy-side aggressive (never below raw)
      floor   — sell/SL side (never above raw for exits)
    """
    p = float(price)
    if p <= 0 or math.isnan(p) or math.isinf(p):
        return 0.0

    t = float(tick) if tick is not None else nse_equity_tick_size(p)
    if t <= 0:
        return round(p, 2)

    # Work in tick units to avoid float drift (8308.30 / 0.50)
    units = p / t
    if mode == "ceil":
        stepped = math.ceil(units - 1e-12) * t
    elif mode == "floor":
        stepped = math.floor(units + 1e-12) * t
    else:
        stepped = round(units) * t

    # Quantize display decimals from tick (0.01→2dp, 0.5→2dp, 1→2dp)
    decimals = 2 if t < 1 else (1 if t < 10 else 0)
    if t in (0.01, 0.05, 0.10, 0.50):
        decimals = 2
    elif t >= 1:
        decimals = 2
    out = round(stepped + 1e-12, decimals)
    # Guard: ensure still on-tick after float round
    if t >= 0.01:
        out = round(round(out / t) * t, decimals)
    return float(out)


def round_buy_limit(price: float) -> float:
    """Buy limit: nearest tick (slightly above raw via ceil if almost mid)."""
    # Prefer ceil so we don't undercut LTP into an unfillable level after 1.001 bump.
    return round_to_nse_tick(price, mode="nearest")


def round_sell_limit(price: float) -> float:
    """Sell / SL limit: floor to tick so we don't place an invalid aggressive sell."""
    return round_to_nse_tick(price, mode="floor")

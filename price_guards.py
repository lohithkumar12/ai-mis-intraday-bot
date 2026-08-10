"""
price_guards.py — Reject invented / absurd prices before paper or live orders
=============================================================================
Prevents fake fallbacks (e.g. Rs1500 for SILVER) from creating phantom P&L
or dangerous live orders.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Quotes that may be used for entries / mark-to-market exits
TRUSTED_QUOTE_SOURCES = frozenset(
    {
        "ticker_data",
        "ohlc_data",
        "websocket_live",
        "candle_close",
        "yahoo",
        "rest",
    }
)

# Hard bands for expansion underlyings (MCX / FX). Wide enough for rolls, tight
# enough to reject Rs1500 fake marks.
MCX_PRICE_BANDS: dict[str, tuple[float, float]] = {
    "GOLD": (30_000.0, 200_000.0),
    "SILVER": (40_000.0, 500_000.0),
    "CRUDEOIL": (2_000.0, 20_000.0),
    "NATURALGAS": (80.0, 1_000.0),
}

CURRENCY_PRICE_BANDS: dict[str, tuple[float, float]] = {
    "USDINR": (60.0, 120.0),
    "EURINR": (70.0, 150.0),
    "GBPINR": (80.0, 160.0),
    "JPYINR": (0.40, 1.50),
}

INDEX_SPOT_BANDS: dict[str, tuple[float, float]] = {
    "NIFTY": (10_000.0, 50_000.0),
    "BANKNIFTY": (20_000.0, 80_000.0),
    "FINNIFTY": (10_000.0, 50_000.0),
}

# If mark vs entry diverges beyond this ratio, refuse SL/TP exit (bad data).
MAX_MARK_ENTRY_RATIO = 5.0


def quote_source(quote: dict[str, Any] | None) -> str:
    if not quote:
        return ""
    return str(quote.get("source") or "").strip().lower()


def is_trusted_quote(quote: dict[str, Any] | None) -> bool:
    if not quote:
        return False
    try:
        ltp = float(quote.get("ltp") or 0)
    except (TypeError, ValueError):
        return False
    if ltp <= 0:
        return False
    src = quote_source(quote)
    if src in ("paper_fallback", "estimated", "invented", "synthetic"):
        return False
    if src and src not in TRUSTED_QUOTE_SOURCES:
        # Unknown but non-fake sources allowed if LTP looks real
        return True
    if not src:
        return True
    return src in TRUSTED_QUOTE_SOURCES


def price_in_band(symbol: str, price: float, bands: dict[str, tuple[float, float]]) -> bool:
    sym = (symbol or "").strip().upper()
    band = bands.get(sym)
    if not band:
        return price > 0
    lo, hi = band
    return lo <= price <= hi


def validate_mcx_price(symbol: str, price: float) -> tuple[bool, str]:
    if price <= 0:
        return False, "price<=0"
    if not price_in_band(symbol, price, MCX_PRICE_BANDS):
        lo, hi = MCX_PRICE_BANDS.get(symbol.upper(), (0, 0))
        return False, f"MCX {symbol} price {price:.2f} outside band [{lo:.0f}, {hi:.0f}]"
    return True, ""


def validate_currency_price(symbol: str, price: float) -> tuple[bool, str]:
    if price <= 0:
        return False, "price<=0"
    if not price_in_band(symbol, price, CURRENCY_PRICE_BANDS):
        lo, hi = CURRENCY_PRICE_BANDS.get(symbol.upper(), (0, 0))
        return False, f"FX {symbol} price {price:.4f} outside band [{lo}, {hi}]"
    return True, ""


def validate_index_spot(symbol: str, price: float) -> tuple[bool, str]:
    if price <= 0:
        return False, "price<=0"
    if not price_in_band(symbol, price, INDEX_SPOT_BANDS):
        lo, hi = INDEX_SPOT_BANDS.get(symbol.upper(), (0, 0))
        return False, f"Index {symbol} spot {price:.2f} outside band [{lo:.0f}, {hi:.0f}]"
    return True, ""


def validate_option_premium(premium: float, *, estimated: bool = False) -> tuple[bool, str]:
    if estimated:
        return False, "estimated option premium not allowed for trading"
    if premium <= 0:
        return False, "premium<=0"
    if premium > 50_000:
        return False, f"premium {premium:.2f} implausibly high"
    return True, ""


def mark_vs_entry_sane(entry: float, mark: float) -> tuple[bool, str]:
    if entry <= 0 or mark <= 0:
        return False, "entry/mark<=0"
    ratio = max(mark, entry) / min(mark, entry)
    if ratio > MAX_MARK_ENTRY_RATIO:
        return False, f"mark {mark:.2f} vs entry {entry:.2f} ratio {ratio:.1f}x > {MAX_MARK_ENTRY_RATIO}x"
    return True, ""


def require_tradeable_quote(
    symbol: str,
    quote: dict[str, Any] | None,
    *,
    segment: str,
) -> tuple[float, str]:
    """
    Returns (ltp, error). error empty => OK.
    """
    if not is_trusted_quote(quote):
        src = quote_source(quote) or "missing"
        return 0.0, f"{symbol}: untrusted/missing quote (source={src})"
    price = float(quote.get("ltp") or 0)
    seg = (segment or "").upper()
    if seg == "MCX":
        ok, err = validate_mcx_price(symbol, price)
    elif seg in ("FX", "CURRENCY"):
        ok, err = validate_currency_price(symbol, price)
    elif seg in ("FNO", "INDEX"):
        ok, err = validate_index_spot(symbol, price)
    else:
        ok, err = (price > 0, "price<=0")
    if not ok:
        return 0.0, err
    return price, ""

"""
mcx_instruments.py — MCX Commodities Contract Specifications
============================================================
Master specification for MCX Gold, Silver, Crude Oil, and Natural Gas contracts.
"""

from __future__ import annotations

MCX_INSTRUMENTS: dict[str, dict] = {
    "GOLD": {
        "symbol": "GOLD",
        "name": "Gold Futures (100g)",
        "exchange": "MCX",
        "lot_size": 1,
        "unit": "100 grams",
        "tick_size": 1.0,
    },
    "SILVER": {
        "symbol": "SILVER",
        "name": "Silver Futures (30kg)",
        "exchange": "MCX",
        "lot_size": 1,
        "unit": "30 kg",
        "tick_size": 1.0,
    },
    "CRUDEOIL": {
        "symbol": "CRUDEOIL",
        "name": "Crude Oil Futures (100 bbl)",
        "exchange": "MCX",
        "lot_size": 1,
        "unit": "100 barrels",
        "tick_size": 1.0,
    },
    "NATURALGAS": {
        "symbol": "NATURALGAS",
        "name": "Natural Gas Futures (1250 mmBtu)",
        "exchange": "MCX",
        "lot_size": 1,
        "unit": "1250 mmBtu",
        "tick_size": 0.1,
    },
}


def get_mcx_info(symbol: str) -> dict | None:
    sym_upper = symbol.upper()
    info = dict(MCX_INSTRUMENTS.get(sym_upper, {}))
    try:
        from india_fno_instruments import resolve_instrument_info
        resolved = resolve_instrument_info(sym_upper, exchange_segment="MCX_COMM")
        if resolved:
            info.update(resolved)
    except Exception:
        pass
    return info if info else None

"""
india_fno_instruments.py — DhanHQ Dynamic Master Instrument Resolver
=====================================================================
Dynamically fetches and resolves Dhan security IDs, exchange segments,
trading symbols, option strikes, expiries, and lot sizes for:
  - NSE Equities (NSE_EQ)
  - NSE Derivatives (NSE_FNO — NIFTY, BANKNIFTY, FINNIFTY, Stock Options)
  - MCX Commodities (MCX_COMM — GOLD, SILVER, CRUDEOIL, NATURALGAS)
  - Currency Derivatives (NSE_CURRENCY — USDINR)
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
_master_by_symbol: dict[str, dict[str, Any]] = {}
_master_options: list[dict[str, Any]] = []
_master_futures: dict[str, list[dict[str, Any]]] = {}
_master_lock = threading.Lock()
_last_download_time = 0.0
CACHE_TTL_SEC = 86400.0  # Refresh scrip master once every 24h

# Bot universe names → scrip-master underlying aliases
_FUTURES_ALIASES: dict[str, list[str]] = {
    "USDINR": ["USDINR", "USD-INR", "USD INR", "USD"],
    "CRUDEOIL": ["CRUDEOIL", "CRUDE OIL", "CRUDE"],
    "NATURALGAS": ["NATURALGAS", "NATURAL GAS", "NATGAS"],
    "GOLD": ["GOLD"],
    "SILVER": ["SILVER"],
}
_stale_reload_attempted: set[str] = set()


def _futures_lookup_keys(symbol: str) -> list[str]:
    sym = symbol.strip().upper()
    keys = list(_FUTURES_ALIASES.get(sym, [sym]))
    compact = sym.replace(" ", "").replace("-", "")
    for extra in (sym, compact):
        if extra and extra not in keys:
            keys.append(extra)
    out: list[str] = []
    for k in keys:
        if k and k not in out:
            out.append(k)
    return out


def _collect_futures_items(symbol: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for key in _futures_lookup_keys(symbol):
        for item in _master_futures.get(key) or []:
            sid = str(item.get("security_id") or "")
            if sid and sid in seen_ids:
                continue
            if sid:
                seen_ids.add(sid)
            items.append(item)
    return items


# Lot-size / segment metadata only. Placeholder security_ids are NEVER trusted
# for live orders when the scrip master has loaded (see is_placeholder_security_id).
STATIC_INSTRUMENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "NIFTY": {
        "security_id": "13",
        "exchange": "NSE_FNO",
        "lot_size": 65,
        "segment": "NSE_FNO",
        "name": "NIFTY 50",
        "instrument_type": "INDEX",
        "resolved_from_master": False,
    },
    "BANKNIFTY": {
        "security_id": "25",
        "exchange": "NSE_FNO",
        "lot_size": 30,
        "segment": "NSE_FNO",
        "name": "NIFTY BANK",
        "instrument_type": "INDEX",
        "resolved_from_master": False,
    },
    "FINNIFTY": {
        "security_id": "27",
        "exchange": "NSE_FNO",
        "lot_size": 40,
        "segment": "NSE_FNO",
        "name": "NIFTY FIN SERVICE",
        "instrument_type": "INDEX",
        "resolved_from_master": False,
    },
    "CRUDEOIL": {
        "security_id": "PLACEHOLDER_MCX_CRUDEOIL",
        "exchange": "MCX_COMM",
        "lot_size": 1,
        "segment": "MCX_COMM",
        "name": "CRUDE OIL",
        "instrument_type": "FUTCOM",
        "resolved_from_master": False,
    },
    "GOLD": {
        "security_id": "PLACEHOLDER_MCX_GOLD",
        "exchange": "MCX_COMM",
        "lot_size": 1,
        "segment": "MCX_COMM",
        "name": "GOLD 100G",
        "instrument_type": "FUTCOM",
        "resolved_from_master": False,
    },
    "SILVER": {
        "security_id": "PLACEHOLDER_MCX_SILVER",
        "exchange": "MCX_COMM",
        "lot_size": 1,
        "segment": "MCX_COMM",
        "name": "SILVER 30KG",
        "instrument_type": "FUTCOM",
        "resolved_from_master": False,
    },
    "NATURALGAS": {
        "security_id": "PLACEHOLDER_MCX_NATURALGAS",
        "exchange": "MCX_COMM",
        "lot_size": 1,
        "segment": "MCX_COMM",
        "name": "NATURAL GAS",
        "instrument_type": "FUTCOM",
        "resolved_from_master": False,
    },
    "USDINR": {
        "security_id": "PLACEHOLDER_CUR_USDINR",
        "exchange": "NSE_CURRENCY",
        "lot_size": 1,
        "segment": "NSE_CURRENCY",
        "name": "USDINR FUT",
        "instrument_type": "FUTCUR",
        "resolved_from_master": False,
    },
}

_PLACEHOLDER_PREFIXES = ("PLACEHOLDER_", "115000", "115001", "115002", "115003", "200001")


def is_placeholder_security_id(sec_id: str | None) -> bool:
    if not sec_id:
        return True
    s = str(sec_id).strip()
    if not s:
        return True
    if any(s.startswith(p) or s == p for p in _PLACEHOLDER_PREFIXES):
        return True
    # Non-numeric IDs (e.g. invented "NIFTY-22000-CE") are not valid Dhan tokens
    if not s.isdigit():
        return True
    return False


def _map_exchange_segment(exch_id: str, segment: str, instrument: str) -> str:
    exch = (exch_id or "").strip().upper()
    seg = (segment or "").strip().upper()
    inst = (instrument or "").strip().upper()

    if exch == "MCX" or seg == "M" or inst in ("FUTCOM", "OPTFUT"):
        return "MCX_COMM"
    if inst in ("FUTCUR", "OPTCUR") or seg == "C":
        return "NSE_CURRENCY" if exch in ("NSE", "") else "BSE_CURRENCY"
    if inst in ("OPTIDX", "OPTSTK", "FUTIDX", "FUTSTK") or seg == "D":
        return "NSE_FNO" if exch == "NSE" else "BSE_FNO"
    if inst == "INDEX" or seg == "I":
        return "IDX_I"
    if exch == "BSE":
        return "BSE_EQ"
    return "NSE_EQ"


def _parse_expiry(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw[:19] if len(raw) >= 19 else raw[:10], fmt if len(raw) > 10 else "%Y-%m-%d")
        except ValueError:
            continue
    return None


def _underlying_from_row(row: dict) -> str:
    sm = (row.get("SM_SYMBOL_NAME") or "").strip().upper()
    # Index options often have empty SM_SYMBOL_NAME — parse trading symbol prefix
    ts = (row.get("SEM_TRADING_SYMBOL") or "").strip().upper()
    if ts:
        # e.g. NIFTY-29Aug2026-25000-CE, CRUDEOIL-19Aug2026-FUT, USDINR-Jun2026-FUT
        prefix = ts.split("-")[0].strip()
        if prefix:
            # Prefer trading-symbol root for indices (SM may be BSXOPT etc.)
            if prefix in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"):
                return prefix
            if sm in ("", "BSXOPT", "BKXOPT", "BITOPT", "SX50OPT"):
                return prefix
    if sm and sm not in ("BSXOPT", "BKXOPT", "BITOPT", "SX50OPT"):
        return sm
    custom = (row.get("SEM_CUSTOM_SYMBOL") or "").strip().upper()
    if custom:
        return custom.split()[0]
    return ts.split("-")[0] if ts else ""


def _row_to_info(row: dict) -> dict[str, Any]:
    sec_id = (row.get("SEM_SMST_SECURITY_ID") or row.get("SEM_SECURITY_ID") or "").strip()
    exch_id = (row.get("SEM_EXM_EXCH_ID") or "").strip()
    segment = (row.get("SEM_SEGMENT") or "").strip()
    instrument = (row.get("SEM_INSTRUMENT_NAME") or "").strip().upper()
    lot_raw = row.get("SEM_LOT_UNITS") or "1"
    try:
        lot_size = int(float(lot_raw))
    except (TypeError, ValueError):
        lot_size = 1
    ts = (row.get("SEM_TRADING_SYMBOL") or "").strip()
    return {
        "security_id": sec_id,
        "exchange": _map_exchange_segment(exch_id, segment, instrument),
        "lot_size": max(lot_size, 1),
        "trading_symbol": ts,
        "option_type": (row.get("SEM_OPTION_TYPE") or "").strip().upper(),
        "strike": float(row.get("SEM_STRIKE_PRICE") or 0),
        "expiry": (row.get("SEM_EXPIRY_DATE") or "").strip(),
        "instrument_type": instrument,
        "underlying": _underlying_from_row(row),
        "exch_id": exch_id.upper(),
        "resolved_from_master": True,
        "name": (row.get("SEM_CUSTOM_SYMBOL") or ts).strip(),
    }


def load_dhan_scrip_master(force_download: bool = False) -> bool:
    """Downloads and indexes the Dhan official scrip master CSV."""
    global _last_download_time, _master_by_symbol, _master_options, _master_futures
    with _master_lock:
        now = time.time()
        if not force_download and _master_by_symbol and (now - _last_download_time < CACHE_TTL_SEC):
            return True

        try:
            logger.info(f"[FEED] Fetching Dhan Scrip Master from {DHAN_SCRIP_MASTER_URL}...")
            req = urllib.request.Request(
                DHAN_SCRIP_MASTER_URL,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                reader = csv.DictReader(io.StringIO(content))
                by_symbol: dict[str, dict] = {}
                options: list[dict] = []
                futures: dict[str, list[dict]] = {}
                count = 0

                for row in reader:
                    info = _row_to_info(row)
                    if not info["security_id"]:
                        continue
                    count += 1
                    inst = info["instrument_type"]
                    und = info["underlying"]
                    ts = (info.get("trading_symbol") or "").upper()

                    # Equities: prefer NSE EQ series
                    if inst == "EQUITY" and ts:
                        existing = by_symbol.get(ts)
                        if existing is None or (
                            info["exchange"] == "NSE_EQ" and existing.get("exchange") != "NSE_EQ"
                        ):
                            by_symbol[ts] = info

                    # Index underlyings (NIFTY=13 etc.)
                    if inst == "INDEX" and ts:
                        by_symbol[ts] = info
                        if und:
                            by_symbol.setdefault(und, info)

                    # Futures — keep all, pick nearest later
                    if inst in ("FUTCOM", "FUTCUR", "FUTIDX", "FUTSTK") and und:
                        futures.setdefault(und, []).append(info)
                        # Also key popular aliases
                        und_compact = und.replace(" ", "").replace("-", "")
                        if und_compact and und_compact != und:
                            futures.setdefault(und_compact, []).append(info)
                        if und == "CRUDE OIL" or und_compact == "CRUDEOIL":
                            futures.setdefault("CRUDEOIL", []).append(info)
                        if und_compact == "USDINR" or und in ("USD-INR", "USD INR", "USD"):
                            futures.setdefault("USDINR", []).append(info)
                        if und == "NATURAL GAS" or und_compact == "NATURALGAS":
                            futures.setdefault("NATURALGAS", []).append(info)

                    # Options
                    if info["option_type"] in ("CE", "PE") and und:
                        options.append(info)

                for und, items in futures.items():
                    items.sort(key=lambda x: x.get("expiry") or "")

                if count > 0:
                    _master_by_symbol = by_symbol
                    _master_options = options
                    _master_futures = futures
                    _last_download_time = now
                    logger.info(
                        f"[FEED] Loaded {count} scrips | symbols={len(by_symbol)} "
                        f"options={len(options)} fut_underlyings={len(futures)}"
                    )
                    return True
                logger.error("[FEED] Scrip master download returned 0 rows — keeping prior cache / static fallbacks")
        except Exception as e:
            logger.error(
                f"[FEED] Could not download Dhan Scrip Master ({e}). "
                f"Using prior cache / static fallbacks — LIVE orders for unresolved IDs will be blocked."
            )

        # Soft success if we still have any cache
        return bool(_master_by_symbol) or True


def _pick_nearest_future(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    now = datetime.now()
    future = []
    for item in items:
        exp = _parse_expiry(item.get("expiry") or "")
        if exp is None:
            continue
        if exp.date() >= now.date():
            future.append((exp, item))
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]
    # Master may be stale — use latest available with loud warning
    dated = []
    for item in items:
        exp = _parse_expiry(item.get("expiry") or "")
        if exp:
            dated.append((exp, item))
    if dated:
        dated.sort(key=lambda x: x[0], reverse=True)
        pick = dated[0][1]
        logger.warning(
            f"[FEED] No non-expired future in master for {pick.get('underlying')} — "
            f"using latest listed {pick.get('trading_symbol')} (master may be stale)"
        )
        return pick
    return items[-1]


def resolve_instrument_info(symbol: str, exchange_segment: str = "NSE") -> dict[str, Any]:
    """Resolves symbol info (security_id, lot_size, exchange_segment)."""
    sym_upper = symbol.strip().upper()
    seg_hint = (exchange_segment or "NSE").strip().upper()

    with _master_lock:
        # Direct equity / index hit
        cached = _master_by_symbol.get(sym_upper)
        if cached:
            # If caller wants MCX/currency, prefer futures book
            if seg_hint in ("MCX", "MCX_COMM") or cached.get("instrument_type") == "FUTCOM":
                pass
            elif seg_hint in ("NSE_CURR", "NSE_CURRENCY", "CURRENCY"):
                pass
            else:
                return dict(cached)

        # Futures underlyings (MCX / currency / index fut)
        items = _collect_futures_items(sym_upper)
        if items:
            # Prefer exchange matching hint
            filtered = items
            if seg_hint in ("MCX", "MCX_COMM"):
                filtered = [i for i in items if i.get("exchange") == "MCX_COMM"] or items
            elif seg_hint in ("NSE_CURR", "NSE_CURRENCY", "CURRENCY"):
                filtered = [
                    i for i in items if i.get("exchange") in ("NSE_CURRENCY", "BSE_CURRENCY")
                ] or items
            pick = _pick_nearest_future(filtered)
            if pick:
                exp = _parse_expiry(pick.get("expiry") or "")
                # One forced master refresh if we only have expired currency/MCX contracts
                if (
                    exp
                    and exp.date() < datetime.now().date()
                    and seg_hint in ("NSE_CURR", "NSE_CURRENCY", "CURRENCY", "MCX", "MCX_COMM")
                    and sym_upper not in _stale_reload_attempted
                ):
                    _stale_reload_attempted.add(sym_upper)
                    logger.warning(
                        f"[FEED] {sym_upper} nearest future expired ({pick.get('trading_symbol')}) "
                        f"— forcing scrip master reload"
                    )
                    # Release lock before download (load acquires its own lock)
                    pass
                else:
                    return dict(pick)

        # Index static known IDs from master equity/index map
        if cached:
            return dict(cached)

    # Stale currency/MCX: reload master outside lock then retry once
    if (
        sym_upper in _stale_reload_attempted
        and seg_hint in ("NSE_CURR", "NSE_CURRENCY", "CURRENCY", "MCX", "MCX_COMM")
    ):
        # Only trigger download if we just marked it and haven't reloaded yet —
        # use a second marker
        reload_key = f"{sym_upper}:reloaded"
        if reload_key not in _stale_reload_attempted:
            _stale_reload_attempted.add(reload_key)
            try:
                load_dhan_scrip_master(force_download=True)
            except Exception as e:
                logger.debug(f"[FEED] Forced master reload note: {e}")
            with _master_lock:
                items = _collect_futures_items(sym_upper)
                if seg_hint in ("MCX", "MCX_COMM"):
                    items = [i for i in items if i.get("exchange") == "MCX_COMM"] or items
                elif seg_hint in ("NSE_CURR", "NSE_CURRENCY", "CURRENCY"):
                    items = [
                        i for i in items if i.get("exchange") in ("NSE_CURRENCY", "BSE_CURRENCY")
                    ] or items
                pick = _pick_nearest_future(items)
                if pick:
                    return dict(pick)

    static_info = STATIC_INSTRUMENT_DEFAULTS.get(sym_upper)
    if static_info:
        out = dict(static_info)
        if is_placeholder_security_id(out.get("security_id")):
            logger.warning(
                f"[FEED] {sym_upper}: using static metadata only — "
                f"security_id unresolved from master (live orders must be blocked)"
            )
        return out

    # Equity fallback via angel/dhan token map (never invent numeric IDs)
    try:
        from india_instruments import get_token, get_exchange

        tok = get_token(sym_upper)
        if tok:
            ex = get_exchange(sym_upper)
            return {
                "security_id": str(tok),
                "exchange": "NSE_EQ" if ex.upper() == "NSE" else "BSE_EQ",
                "lot_size": 1,
                "name": sym_upper,
                "trading_symbol": sym_upper,
                "instrument_type": "EQUITY",
                "resolved_from_master": False,
            }
    except Exception:
        pass

    logger.warning(f"[FEED] Unresolved instrument {sym_upper} — no valid security_id")
    return {
        "security_id": "",
        "exchange": seg_hint if seg_hint else "NSE_EQ",
        "lot_size": 1,
        "name": sym_upper,
        "resolved_from_master": False,
    }


def get_fno_lot_size(symbol: str) -> int:
    und = symbol.strip().upper()
    # Prefer lot from a live option row in master (INDEX rows often have lot=1)
    with _master_lock:
        for item in _master_options:
            if item.get("underlying") == und and item.get("exch_id") == "NSE":
                lot = int(item.get("lot_size") or 0)
                if lot > 1:
                    return lot
    static = STATIC_INSTRUMENT_DEFAULTS.get(und)
    if static and int(static.get("lot_size") or 0) > 1:
        return int(static["lot_size"])
    info = resolve_instrument_info(und, exchange_segment="NSE_FNO")
    return max(int(info.get("lot_size") or 1), 1)


def resolve_option_contract(
    underlying: str,
    strike: float,
    option_type: str,
    expiry: str | None = None,
) -> dict[str, Any] | None:
    """
    Structured lookup for option contract security_id from loaded scrip master.
    Matches underlying, strike price, and option_type (CE/PE).
    Returns None when unresolved (never invents fake security IDs).
    """
    und_upper = underlying.strip().upper()
    opt_upper = option_type.strip().upper()
    now = datetime.now()

    with _master_lock:
        candidates: list[tuple[datetime, dict]] = []
        for item in _master_options:
            if item.get("underlying") != und_upper:
                continue
            if item.get("option_type") != opt_upper:
                continue
            if item.get("exch_id") not in ("NSE", ""):
                # Prefer NSE index/stock options
                if und_upper in ("NIFTY", "BANKNIFTY", "FINNIFTY") and item.get("exch_id") != "NSE":
                    continue
            item_strike = float(item.get("strike", 0) or 0)
            if abs(item_strike - float(strike)) > 0.51:
                continue
            exp = _parse_expiry(item.get("expiry") or "")
            if expiry:
                if not (item.get("expiry") or "").startswith(expiry[:10]):
                    continue
            if exp and exp.date() < now.date():
                continue
            if exp:
                candidates.append((exp, item))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            return dict(candidates[0][1])

    logger.warning(
        f"[FNO] Option contract unresolved: {und_upper} {strike} {opt_upper} "
        f"(expiry={expiry or 'nearest'}) — refuse invented security_id"
    )
    return None


def master_status() -> dict[str, Any]:
    with _master_lock:
        return {
            "loaded": bool(_master_by_symbol),
            "symbols_count": len(_master_by_symbol),
            "options_count": len(_master_options),
            "futures_underlyings": len(_master_futures),
            "last_download_age_sec": (
                round(time.time() - _last_download_time, 1) if _last_download_time else None
            ),
        }

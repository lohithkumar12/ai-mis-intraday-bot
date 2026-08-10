"""
us_instruments.py — US Stock Instrument Mapping (Dhan Global Stocks)
=====================================================================
Maps US stock tickers to DhanHQ Global Stocks security IDs (SCRIP_CODE).

Official instrument master:
  https://api-global-stocks.dhan.co/api-data/us-stock-scrip-master.csv

Security IDs differ from NSE/BSE. Prefer runtime refresh from the CSV;
static US_INSTRUMENTS is a seed for offline / first boot.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

US_SCRIP_MASTER_URL = (
    "https://api-global-stocks.dhan.co/api-data/us-stock-scrip-master.csv"
)
CACHE_TTL_SEC = 6 * 3600  # 6 hours
FETCH_FAIL_COOLDOWN_SEC = 3600  # don't hammer CDN after 403

# ---------------------------------------------------------------------------
# Default US Universe — SCRIP_CODE from official Global Stocks master
# ---------------------------------------------------------------------------
US_INSTRUMENTS: dict[str, dict] = {
    "AAPL": {
        "security_id": "10000025",
        "name": "Apple Inc.",
        "exchange": "GLOBAL",
    },
    "MSFT": {
        "security_id": "10006754",
        "name": "Microsoft Corporation",
        "exchange": "GLOBAL",
    },
    "GOOGL": {
        "security_id": "10004393",
        "name": "Alphabet Inc. (Class A)",
        "exchange": "GLOBAL",
    },
    "AMZN": {
        "security_id": "10000496",
        "name": "Amazon.com Inc.",
        "exchange": "GLOBAL",
    },
    "NVDA": {
        "security_id": "10007290",
        "name": "NVIDIA Corporation",
        "exchange": "GLOBAL",
    },
    "META": {
        "security_id": "10006471",
        "name": "Meta Platforms Inc.",
        "exchange": "GLOBAL",
    },
    "TSLA": {
        "security_id": "10010311",
        "name": "Tesla Inc.",
        "exchange": "GLOBAL",
    },
    "JPM": {
        "security_id": "10005648",
        "name": "JPMorgan Chase & Co.",
        "exchange": "GLOBAL",
    },
    "V": {
        "security_id": "10010679",
        "name": "Visa Inc.",
        "exchange": "GLOBAL",
    },
    "UNH": {
        "security_id": "10010532",
        "name": "UnitedHealth Group Inc.",
        "exchange": "GLOBAL",
    },
}

_master_lock = threading.Lock()
_master_by_symbol: dict[str, dict] = {}
_last_download_time = 0.0
_master_row_count = 0
_fetch_failed_until = 0.0
_last_fetch_error = ""


def _download_scrip_csv() -> str:
    """Fetch CSV text; try requests then urllib. Raises on failure."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/csv,text/plain,*/*",
    }
    try:
        import config

        token = (getattr(config, "DHAN_ACCESS_TOKEN", None) or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["access-token"] = token
        cid = (getattr(config, "DHAN_CLIENT_ID", None) or "").strip()
        if cid:
            headers["client-id"] = cid
    except Exception:
        pass

    try:
        import requests

        resp = requests.get(US_SCRIP_MASTER_URL, headers=headers, timeout=45)
        if resp.status_code == 200 and resp.text and "SCRIP_CODE" in resp.text[:500]:
            return resp.text
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    except Exception as req_err:
        req = urllib.request.Request(US_SCRIP_MASTER_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=45) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
        if "SCRIP_CODE" not in content[:500]:
            raise RuntimeError(f"unexpected CSV after urllib fallback ({req_err})")
        return content


def load_us_scrip_master(force_download: bool = False) -> bool:
    """Download and index the official US Global Stocks scrip master CSV."""
    global _last_download_time, _master_by_symbol, _master_row_count
    global _fetch_failed_until, _last_fetch_error
    with _master_lock:
        now = time.time()
        if (
            not force_download
            and _master_by_symbol
            and (now - _last_download_time < CACHE_TTL_SEC)
        ):
            return True
        if not force_download and now < _fetch_failed_until:
            # Seed map remains usable; avoid 403 spam from GCP/CDN blocks
            return bool(_master_by_symbol)

        try:
            logger.info(
                f"[US] Fetching Global Stocks scrip master from {US_SCRIP_MASTER_URL}..."
            )
            content = _download_scrip_csv()

            reader = csv.DictReader(io.StringIO(content))
            by_symbol: dict[str, dict] = {}
            count = 0
            for row in reader:
                count += 1
                sym = (
                    (row.get("SYMBOL") or row.get("EXCH_SYMBOL") or row.get("TRADING_SYMBOL") or "")
                    .strip()
                    .upper()
                )
                sec_id = str(row.get("SCRIP_CODE") or "").strip()
                if not sym or not sec_id:
                    continue
                name = (
                    row.get("CUSTOM_SYMBOL")
                    or row.get("SYMBOL_NAME")
                    or sym
                ).strip()
                exch = (row.get("CUSTOM_EXCH") or row.get("EXCHANGE") or "GLOBAL").strip()
                by_symbol[sym] = {
                    "security_id": sec_id,
                    "name": name,
                    "exchange": "GLOBAL",
                    "custom_exch": exch,
                    "resolved_from_master": True,
                }

            if by_symbol:
                _master_by_symbol = by_symbol
                _master_row_count = count
                _last_download_time = now
                _fetch_failed_until = 0.0
                _last_fetch_error = ""
                for ticker, info in US_INSTRUMENTS.items():
                    master = by_symbol.get(ticker)
                    if master and master.get("security_id"):
                        info["security_id"] = str(master["security_id"])
                        if master.get("name"):
                            info["name"] = master["name"]
                logger.info(
                    f"[US] Loaded Global scrip master: {count} rows | "
                    f"symbols={len(by_symbol)}"
                )
                return True

            logger.warning("[US] Global scrip master download returned no symbols")
            _fetch_failed_until = now + FETCH_FAIL_COOLDOWN_SEC
            return False
        except Exception as e:
            _last_fetch_error = str(e)
            _fetch_failed_until = now + FETCH_FAIL_COOLDOWN_SEC
            logger.warning(
                f"[US] Global scrip master fetch failed: {e} — "
                f"using seeded SCRIP_CODEs for {FETCH_FAIL_COOLDOWN_SEC}s"
            )
            return bool(_master_by_symbol)


def master_status() -> dict:
    with _master_lock:
        age = (
            round(time.time() - _last_download_time, 1)
            if _last_download_time > 0
            else None
        )
        return {
            "loaded": bool(_master_by_symbol),
            "symbol_count": len(_master_by_symbol),
            "row_count": _master_row_count,
            "age_sec": age,
            "url": US_SCRIP_MASTER_URL,
            "using_seed": not bool(_master_by_symbol),
            "last_error": _last_fetch_error or None,
            "cooldown_sec": (
                round(max(0.0, _fetch_failed_until - time.time()), 1)
                if _fetch_failed_until > time.time()
                else 0
            ),
        }


def resolve_us_instrument(symbol: str) -> dict | None:
    """Resolve ticker → security_id (master preferred, then static seed)."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    with _master_lock:
        hit = _master_by_symbol.get(sym)
        if hit:
            return dict(hit)
    seed = US_INSTRUMENTS.get(sym)
    if seed:
        return {
            "security_id": str(seed["security_id"]),
            "name": seed.get("name", sym),
            "exchange": seed.get("exchange", "GLOBAL"),
            "resolved_from_master": False,
        }
    return None


def get_us_security_id(symbol: str) -> str | None:
    """Get the Dhan Global Stocks security_id (SCRIP_CODE) for a US ticker."""
    info = resolve_us_instrument(symbol)
    if not info:
        return None
    sec = str(info.get("security_id") or "").strip()
    return sec or None


def get_us_exchange(symbol: str) -> str:
    """Get exchange segment for a US symbol. Always 'GLOBAL'."""
    return "GLOBAL"


def get_us_name(symbol: str) -> str:
    """Get human-readable company name."""
    info = resolve_us_instrument(symbol)
    if info and info.get("name"):
        return str(info["name"])
    return symbol


def get_all_us_symbols() -> list[str]:
    """Return list of all tracked US stock symbols (static universe seed)."""
    return list(US_INSTRUMENTS.keys())


def is_us_symbol(symbol: str) -> bool:
    """Check if a symbol is in the US instruments map or loaded master."""
    sym = (symbol or "").strip().upper()
    if sym in US_INSTRUMENTS:
        return True
    with _master_lock:
        return sym in _master_by_symbol

"""
india_instruments.py — NSE Instrument Token Mapping
=====================================================
Maps NSE stock symbols to numeric security IDs used by Dhan (security_id)
and Angel One (symboltoken). These IDs align for common NSE equities
(e.g. HDFCBANK=1333).
"""

import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Nifty 50 Large-Cap Stocks — Symbol Token Mapping
# ---------------------------------------------------------------------------
# Format: "DISPLAY_NAME": {"token": "...", "symbol": "SYMBOL-EQ", "exchange": "NSE"}
#
# These tokens are stable and rarely change. Verified against Angel One
# master instrument file.
# ---------------------------------------------------------------------------

INDIA_INSTRUMENTS: dict[str, dict] = {
    "RELIANCE": {
        "token": "2885",
        "symbol": "RELIANCE-EQ",
        "exchange": "NSE",
        "name": "Reliance Industries Ltd.",
    },
    "TCS": {
        "token": "11536",
        "symbol": "TCS-EQ",
        "exchange": "NSE",
        "name": "Tata Consultancy Services Ltd.",
    },
    "HDFCBANK": {
        "token": "1333",
        "symbol": "HDFCBANK-EQ",
        "exchange": "NSE",
        "name": "HDFC Bank Ltd.",
    },
    "INFY": {
        "token": "1594",
        "symbol": "INFY-EQ",
        "exchange": "NSE",
        "name": "Infosys Ltd.",
    },
    "ICICIBANK": {
        "token": "4963",
        "symbol": "ICICIBANK-EQ",
        "exchange": "NSE",
        "name": "ICICI Bank Ltd.",
    },
    "HINDUNILVR": {
        "token": "1394",
        "symbol": "HINDUNILVR-EQ",
        "exchange": "NSE",
        "name": "Hindustan Unilever Ltd.",
    },
    "ITC": {
        "token": "1660",
        "symbol": "ITC-EQ",
        "exchange": "NSE",
        "name": "ITC Ltd.",
    },
    "SBIN": {
        "token": "3045",
        "symbol": "SBIN-EQ",
        "exchange": "NSE",
        "name": "State Bank of India",
    },
    "BHARTIARTL": {
        "token": "10604",
        "symbol": "BHARTIARTL-EQ",
        "exchange": "NSE",
        "name": "Bharti Airtel Ltd.",
    },
    "LT": {
        "token": "11483",
        "symbol": "LT-EQ",
        "exchange": "NSE",
        "name": "Larsen & Toubro Ltd.",
    },
    "KOTAKBANK": {
        "token": "1922",
        "symbol": "KOTAKBANK-EQ",
        "exchange": "NSE",
        "name": "Kotak Mahindra Bank Ltd.",
    },
    "WIPRO": {
        "token": "3787",
        "symbol": "WIPRO-EQ",
        "exchange": "NSE",
        "name": "Wipro Ltd.",
    },
    # --- Expanded liquid names (scout / optional trade universe) ---
    "AXISBANK": {
        "token": "5900",
        "symbol": "AXISBANK-EQ",
        "exchange": "NSE",
        "name": "Axis Bank Ltd.",
    },
    "BAJFINANCE": {
        "token": "317",
        "symbol": "BAJFINANCE-EQ",
        "exchange": "NSE",
        "name": "Bajaj Finance Ltd.",
    },
    "MARUTI": {
        "token": "10999",
        "symbol": "MARUTI-EQ",
        "exchange": "NSE",
        "name": "Maruti Suzuki India Ltd.",
    },
    "SUNPHARMA": {
        "token": "3351",
        "symbol": "SUNPHARMA-EQ",
        "exchange": "NSE",
        "name": "Sun Pharmaceutical Industries Ltd.",
    },
    "TITAN": {
        "token": "3506",
        "symbol": "TITAN-EQ",
        "exchange": "NSE",
        "name": "Titan Company Ltd.",
    },
    "ASIANPAINT": {
        "token": "236",
        "symbol": "ASIANPAINT-EQ",
        "exchange": "NSE",
        "name": "Asian Paints Ltd.",
    },
    "ULTRACEMCO": {
        "token": "11532",
        "symbol": "ULTRACEMCO-EQ",
        "exchange": "NSE",
        "name": "UltraTech Cement Ltd.",
    },
    "NESTLEIND": {
        "token": "17963",
        "symbol": "NESTLEIND-EQ",
        "exchange": "NSE",
        "name": "Nestle India Ltd.",
    },
    "POWERGRID": {
        "token": "14977",
        "symbol": "POWERGRID-EQ",
        "exchange": "NSE",
        "name": "Power Grid Corporation of India Ltd.",
    },
    "NTPC": {
        "token": "11630",
        "symbol": "NTPC-EQ",
        "exchange": "NSE",
        "name": "NTPC Ltd.",
    },
    "ONGC": {
        "token": "2475",
        "symbol": "ONGC-EQ",
        "exchange": "NSE",
        "name": "Oil & Natural Gas Corporation Ltd.",
    },
    "TATAMOTORS": {
        "token": "3456",
        "symbol": "TATAMOTORS-EQ",
        "exchange": "NSE",
        "name": "Tata Motors Ltd.",
    },
    "TATASTEEL": {
        "token": "3499",
        "symbol": "TATASTEEL-EQ",
        "exchange": "NSE",
        "name": "Tata Steel Ltd.",
    },
    "JSWSTEEL": {
        "token": "11723",
        "symbol": "JSWSTEEL-EQ",
        "exchange": "NSE",
        "name": "JSW Steel Ltd.",
    },
    "M&M": {
        "token": "2031",
        "symbol": "M&M-EQ",
        "exchange": "NSE",
        "name": "Mahindra & Mahindra Ltd.",
    },
    "TECHM": {
        "token": "13538",
        "symbol": "TECHM-EQ",
        "exchange": "NSE",
        "name": "Tech Mahindra Ltd.",
    },
    "HCLTECH": {
        "token": "7229",
        "symbol": "HCLTECH-EQ",
        "exchange": "NSE",
        "name": "HCL Technologies Ltd.",
    },
    "ADANIENT": {
        "token": "25",
        "symbol": "ADANIENT-EQ",
        "exchange": "NSE",
        "name": "Adani Enterprises Ltd.",
    },
    "ADANIPORTS": {
        "token": "15083",
        "symbol": "ADANIPORTS-EQ",
        "exchange": "NSE",
        "name": "Adani Ports and SEZ Ltd.",
    },
    "COALINDIA": {
        "token": "20374",
        "symbol": "COALINDIA-EQ",
        "exchange": "NSE",
        "name": "Coal India Ltd.",
    },
    "BPCL": {
        "token": "526",
        "symbol": "BPCL-EQ",
        "exchange": "NSE",
        "name": "Bharat Petroleum Corporation Ltd.",
    },
    "CIPLA": {
        "token": "694",
        "symbol": "CIPLA-EQ",
        "exchange": "NSE",
        "name": "Cipla Ltd.",
    },
    "DRREDDY": {
        "token": "881",
        "symbol": "DRREDDY-EQ",
        "exchange": "NSE",
        "name": "Dr. Reddy's Laboratories Ltd.",
    },
    "APOLLOHOSP": {
        "token": "157",
        "symbol": "APOLLOHOSP-EQ",
        "exchange": "NSE",
        "name": "Apollo Hospitals Enterprise Ltd.",
    },
    "EICHERMOT": {
        "token": "910",
        "symbol": "EICHERMOT-EQ",
        "exchange": "NSE",
        "name": "Eicher Motors Ltd.",
    },
    "HEROMOTOCO": {
        "token": "1348",
        "symbol": "HEROMOTOCO-EQ",
        "exchange": "NSE",
        "name": "Hero MotoCorp Ltd.",
    },
    "INDUSINDBK": {
        "token": "5258",
        "symbol": "INDUSINDBK-EQ",
        "exchange": "NSE",
        "name": "IndusInd Bank Ltd.",
    },
    "BAJAJFINSV": {
        "token": "16675",
        "symbol": "BAJAJFINSV-EQ",
        "exchange": "NSE",
        "name": "Bajaj Finserv Ltd.",
    },
    "BAJAJ-AUTO": {
        "token": "16669",
        "symbol": "BAJAJ-AUTO-EQ",
        "exchange": "NSE",
        "name": "Bajaj Auto Ltd.",
    },
    "BRITANNIA": {
        "token": "547",
        "symbol": "BRITANNIA-EQ",
        "exchange": "NSE",
        "name": "Britannia Industries Ltd.",
    },
    "GRASIM": {
        "token": "1232",
        "symbol": "GRASIM-EQ",
        "exchange": "NSE",
        "name": "Grasim Industries Ltd.",
    },
    "HINDALCO": {
        "token": "1363",
        "symbol": "HINDALCO-EQ",
        "exchange": "NSE",
        "name": "Hindalco Industries Ltd.",
    },
    "DIVISLAB": {
        "token": "10940",
        "symbol": "DIVISLAB-EQ",
        "exchange": "NSE",
        "name": "Divi's Laboratories Ltd.",
    },
    "HDFCLIFE": {
        "token": "467",
        "symbol": "HDFCLIFE-EQ",
        "exchange": "NSE",
        "name": "HDFC Life Insurance Company Ltd.",
    },
    "SBILIFE": {
        "token": "21808",
        "symbol": "SBILIFE-EQ",
        "exchange": "NSE",
        "name": "SBI Life Insurance Company Ltd.",
    },
    "TATACONSUM": {
        "token": "3432",
        "symbol": "TATACONSUM-EQ",
        "exchange": "NSE",
        "name": "Tata Consumer Products Ltd.",
    },
    "BEL": {
        "token": "383",
        "symbol": "BEL-EQ",
        "exchange": "NSE",
        "name": "Bharat Electronics Ltd.",
    },
    "TRENT": {
        "token": "1964",
        "symbol": "TRENT-EQ",
        "exchange": "NSE",
        "name": "Trent Ltd.",
    },
}


def get_token(symbol: str) -> str | None:
    """Get the Angel One symbol token for a given stock symbol."""
    info = INDIA_INSTRUMENTS.get(symbol)
    return info["token"] if info else None


def get_trading_symbol(symbol: str) -> str | None:
    """Get the full trading symbol (e.g., 'RELIANCE-EQ') for a given stock."""
    info = INDIA_INSTRUMENTS.get(symbol)
    return info["symbol"] if info else None


def get_exchange(symbol: str) -> str:
    """Get exchange for a given symbol. Defaults to NSE."""
    info = INDIA_INSTRUMENTS.get(symbol)
    return info["exchange"] if info else "NSE"


def get_all_symbols() -> list[str]:
    """Return list of all tracked India stock symbols."""
    return list(INDIA_INSTRUMENTS.keys())

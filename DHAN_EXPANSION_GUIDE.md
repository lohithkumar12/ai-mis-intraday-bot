# DhanHQ Advanced Expansion Guide

This guide documents what **actually works** in the expanded DhanHQ multi-market bot:
**India equities**, **India F&O**, **Super & Forever Orders**, **Paid WebSocket Live Feed**,
**MCX Commodities**, **NSE Currency FX**, and **Product Types (CNC / INTRADAY / MTF)**.

> **Do not arm any LIVE flags until paper multi-segment is stable for several sessions.**

---

## 1. Safe all-segment PAPER `.env`

```env
# Shared Dhan Credentials
DHAN_CLIENT_ID=your_client_id
DHAN_ACCESS_TOKEN=your_access_token
# Prefer PIN+TOTP for auto token refresh
DHAN_PIN=your_pin
DHAN_TOTP_SECRET=your_totp

# Paid Market Data API (WebSocket) — falls back to REST when disconnected
DHAN_LIVE_WEBSOCKET=true

# India equity cash book
INDIA_PAPER=true
INDIA_PRODUCT_TYPE=CNC
LIVE_TRADING=false
LIVE_CONFIRM=

# US Global (REST/Yahoo for quotes — MarketFeed is India/MCX only)
US_PAPER=true
US_LIVE_TRADING=false
US_LIVE_CONFIRM=

# India F&O
INDIA_FNO_ENABLED=true
INDIA_FNO_PAPER=true
INDIA_FNO_LIVE_TRADING=false
INDIA_FNO_LIVE_CONFIRM=
INDIA_FNO_UNIVERSE=NIFTY,BANKNIFTY,FINNIFTY
INDIA_FNO_MAX_LOTS=2
INDIA_FNO_CAPITAL_CAP=200000
INDIA_FNO_STRATEGY=directional_options

# MCX
MCX_ENABLED=true
MCX_PAPER=true
MCX_LIVE_TRADING=false
MCX_LIVE_CONFIRM=
MCX_CAPITAL_CAP=500000

# Currency
CURRENCY_ENABLED=true
CURRENCY_PAPER=true
CURRENCY_LIVE_TRADING=false
CURRENCY_LIVE_CONFIRM=
CURRENCY_CAPITAL_CAP=100000
```

---

## 2. Active Trading Hours & Dead Zones

| Segment | Exchange | Active Hours (IST) | Notes |
| :--- | :--- | :--- | :--- |
| **India Equities** | NSE / BSE | 09:15 – 15:30 | Cash / CNC / INTRADAY / MTF |
| **India F&O** | NSE F&O | 09:15 – 15:30 | Index/stock options (defined-risk directional) |
| **Currency FX** | NSE Currency | 09:00 – 17:00 | USDINR |
| **MCX Commodities** | MCX | 09:00 – 23:30 | GOLD, SILVER, CRUDEOIL, NATURALGAS (contract windows may vary) |
| **US Global Stocks** | US | ~19:00 – 01:30 | Via Dhan Global; quotes REST/Yahoo if WS unsupported |
| 🛑 **Dead Zone** | All | ~01:30 – 09:00 IST weekdays | Bot/dashboard stay up; **no trading** |
| 🛑 **Weekend / holidays** | All | Sat–Sun + exchange holidays | Closed — **NOT 24×7 trading** |

---

## 3. How each segment is enabled

1. Set `*_ENABLED=true` and `*_PAPER=true` (defaults).
2. Start: `python main.py` — loads scrip master, starts WS feed, subscribes universes, runs parallel loops.
3. Dashboard: `http://localhost:5000` → segment strip polls `/api/segments/status` every 5s (feed connected vs REST fallback, paper balances/positions, cap utilization, kill switch).

### Live arming (per segment — both flags required)

| Segment | Flags |
| :--- | :--- |
| India equity | `LIVE_TRADING=true` + `LIVE_CONFIRM=YES_REAL_MONEY` |
| India F&O | `INDIA_FNO_LIVE_TRADING=true` + `INDIA_FNO_LIVE_CONFIRM=YES_REAL_MONEY` |
| MCX | `MCX_LIVE_TRADING=true` + `MCX_LIVE_CONFIRM=YES_REAL_MONEY` |
| Currency | `CURRENCY_LIVE_TRADING=true` + `CURRENCY_LIVE_CONFIRM=YES_REAL_MONEY` |
| US | `US_LIVE_TRADING=true` + `US_LIVE_CONFIRM=YES_REAL_MONEY` |

Live F&O/MCX/Currency **refuse** placeholder security IDs and invented premiums. If scrip master or option chain is missing → order skipped + loud log. Soft-fail if account entitlement (MCX/Global) is inactive.

---

## 4. Order types & product types

- **CNC**: Delivery swing. Super Order preferred when SL+TP present; Forever/GTT armed for multi-day SL backup.
- **INTRADAY (MIS)**: Same-day; paper auto square-off ~15:15 IST; live also squared by broker.
- **MTF**: Leveraged delivery (margin calculator used when API available).
- **Super Orders**: Entry + Target + SL (+ trail) when SDK supports it.
- **Forever Orders (GTT)**: Wired for CNC swing exits (not CNC-hardcoded; uses `INDIA_PRODUCT_TYPE`).

---

## 5. Live Market Feed (paid Data API)

- Process-wide singleton (`dhan_live_feed.get_live_feed_manager`).
- `is_connected()` is true only after a real tick within 60s.
- Dynamic `subscribe_symbol` updates the live `MarketFeed.subscribe_symbols` list.
- Reconnect with exponential backoff; quotes fall back to REST/candle cache (throttled).
- **US symbols**: not on India MarketFeed — status_summary documents REST/Yahoo fallback.

---

## 6. Margin & shared INR wallet

- Per-segment capital caps: `INDIA_FNO_CAPITAL_CAP`, `MCX_CAPITAL_CAP`, `CURRENCY_CAPITAL_CAP`.
- Live orders call margin calculator when available; oversized / over-cap blocked.
- Dashboard shows cap utilization when positions exist.

---

## 7. Safety notes

> [!IMPORTANT]
> - Whitelist your server **static public IP** in the Dhan Developer Console for live order APIs.
> - US Global funding may involve **LRS / TCS** — confirm with your bank/Dhan before live US trading.
> - Paper may log `[PAPER F&O ESTIMATE]` when option chain is closed; **live never uses invented premiums**.

---

## 8. Verify

```bash
python -m unittest tests.test_dhan_expansion -v
python main.py
# open http://localhost:5000 — confirm feed + segment statuses update
python backtest.py --dhan --market FNO --symbol NIFTY
```

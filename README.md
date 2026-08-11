# AI MIS Intraday Bot (India · NSE · Dhan)

Same **architecture** as [`New_StartUp`](../New_StartUp) (CNC swing), dedicated to **MIS / INTRADAY**.

**CNC bot is separate** — wind down New_StartUp CNC buys on its own; this MIS bot sizes only against **`INDIA_CAPITAL_CAP`** (default ₹1,50,000) even if the Dhan account holds more. **Paper first**, then arm with `LIVE_TRADING` + `LIVE_CONFIRM=YES_REAL_MONEY`.

| Same as New_StartUp | Different for MIS |
|---------------------|-------------------|
| Flat modules: `main.py`, `config.py`, `dhan_broker.py`, … | `INDIA_PRODUCT_TYPE=INTRADAY` |
| Double live gate | `opening_range_breakout` + `TIMEFRAME=5` |
| Flask dashboard + journal + scout | `ENTRY_CUTOFF=14:45`, `SQUAREOFF_TIME=15:10` |
| | `INDIA_CAPITAL_CAP` sleeve for sizing + daily DD |
| | Docker: `ai_mis_intraday_bot` on host **port 81** (CNC often uses 80) |

## Paper run

```bash
cd ai-mis-intraday-bot
python -m venv .venv
.\.venv\Scripts\activate
pip install --pre -r requirements.txt
# .env already has paper + INDIA_CAPITAL_CAP=150000 — keep LIVE_* off
python main.py
```

Local dashboard: http://localhost:5000 (or `$PORT`)

## Live arming (only after paper checklist)

```env
LIVE_TRADING=true
LIVE_CONFIRM=YES_REAL_MONEY
INDIA_PRODUCT_TYPE=INTRADAY
INDIA_CAPITAL_CAP=150000
```

## Docker (GCP VM) — isolated from CNC

**Deploy MIS on Dhan primary IP only** (`35.200.221.125`). Secondary IP (`35.200.249.211`) can hit **DH-905 Invalid IP** on order APIs. See [`MIGRATE_PRIMARY_VM.md`](MIGRATE_PRIMARY_VM.md).

```bash
cd ai-mis-intraday-bot
docker compose up -d --build
docker compose logs -f bot
```

- Container: **`ai_mis_intraday_bot`**
- Dashboard: **`http://35.200.221.125:81/`** (maps `81→8080`)
- CNC on same host: port **80** / `ai_stock_trading_bot` — do not collide
- Run **one** live MIS instance only (stop secondary VM compose)

## Key knobs

| Env | Role |
|-----|------|
| `INDIA_CAPITAL_CAP` | Max equity used for MIS sizing + daily DD base |
| `INDIA_PAPER` / `LIVE_*` | Paper until double-armed |
| `ENTRY_CUTOFF` / `SQUAREOFF_TIME` | No new entries after 14:45; flatten ~15:10 |
| `INDIA_SCOUT_AUTO_BUY` | `false` by default (quieter first runs) |

## Tests

```bash
python -m unittest tests.test_mis_session tests.test_nse_tick tests.test_order_guards tests.test_zero_ltp_sl -v
```

No fixed daily profit — expectancy + risk caps only.

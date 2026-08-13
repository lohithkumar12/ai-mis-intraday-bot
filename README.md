# AI MIS Intraday Bot (India · NSE · Dhan)

Same **architecture** as [`New_StartUp`](../New_StartUp) (CNC swing), dedicated to **MIS / INTRADAY**.

**CNC bot is separate** — wind down New_StartUp CNC buys on its own; this MIS bot sizes only against **`INDIA_CAPITAL_CAP`** (default ₹1,50,000) even if the Dhan account holds more. **Paper first**, then arm with `LIVE_TRADING` + `LIVE_CONFIRM=YES_REAL_MONEY`.

| Same as New_StartUp | Different for MIS |
|---------------------|-------------------|
| Flat modules: `main.py`, `config.py`, `dhan_broker.py`, … | `INDIA_PRODUCT_TYPE=INTRADAY` |
| Double live gate | `mis_regime` + `TIMEFRAME=5` (legacy `opening_range_breakout` still works) |
| Flask dashboard + journal + scout | `ENTRY_CUTOFF=14:15`, `SQUAREOFF_TIME=15:00` |
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
| `ENTRY_CUTOFF` / `SQUAREOFF_TIME` | No new entries after **14:15**; flatten from **15:00** (before broker RMS ~15:15) |
| `STRATEGY_NAME` | Default **`mis_regime`** (aliases `regime_mis`, `vwap_regime`). Legacy: `opening_range_breakout`, `trend_pullback`, `mean_reversion`, `regime_adaptive`, `breakout` |
| `ORB_WINDOW_MINUTES` | OPEN_DRIVE window from 09:15 IST (default **60**). Independent of `OR_MINUTES` (range construction) |
| `INDIA_SCOUT_ENABLED` | Optional second loop. **`false`** = scan `INDIA_STOCK_UNIVERSE` only |
| `INDIA_SCOUT_AUTO_BUY` | Keep **`false`** unless you explicitly want scout auto-fills |
| `MAX_TRADES_PER_DAY` | Journal-entry cap/day (**`0` = unlimited**). Not a default-on hard cap |
| `MAX_DAILY_LOSS_INR` | Optional absolute ₹ kill (0=off); DD% still applies |
| `INDIA_LOOP_INTERVAL_SEC` | Core scan cadence. **60s** is enough for 5-min ORB on ~50 names (not 20s) |
| `INDIA_LOOP_FETCH_GAP_SEC` | Pause between core candle fetches (default **0.4s**) to avoid HTTP 429 |
| `COST_FLOOR_*` | Skip entries whose TP move cannot cover RT fees + min edge |

**Ops:** do **not** mid-session redeploy / `docker compose up --build` while MIS positions are open — kill-switch is now disk-persisted for the IST day, but ORB/meta races and RMS square-off windows still hurt. Restart only when **flat** or after market.

## mis_regime (India default)

Regime selector — **not** OR-combined signals. For each symbol and each cycle, **at most one** playbook may BUY (hard XOR). First qualifier wins; later playbooks are not executed. If none qualify → HOLD. Sitting out is valid.

| Regime | When | Playbook |
|--------|------|----------|
| `OPEN_DRIVE` | `09:15 ≤ t < 09:15 + ORB_WINDOW_MINUTES` **and** expansion (`ADX` rising over `ADX_RISING_LOOKBACK` **or** bar range ≥ `RANGE_EXPANSION_MULT` × recent mean range) | Improved ORB first. If ORB fails → **HOLD** unless `TREND_UP` is independently true (then VWAP momentum / EMA pullback). **No** fall-through to mean reversion. |
| `TREND_UP` | `ADX ≥ ADX_RANGE_MAX` and close > session VWAP and trend confirmed (optional HTF EMA via `ORB_USE_HTF_FILTER`) | 1) VWAP + momentum + **own** RS top-`RS_TOP_N` 2) EMA pullback + momentum. Extended price → HOLD. |
| `RANGE` | `ADX < ADX_RANGE_MAX` | VWAP mean reversion only (stretch + RSI oversold + reclaim). No ORB / momentum chase. |
| `TREND_DOWN` | `ADX ≥ ADX_RANGE_MAX` and close < VWAP and momentum falling | **HOLD** (long-only). `ORB_ALLOW_SHORT` stays false unless the caller enables it later. |

**Completed 5-minute bars only** — the forming candle is never used for a decision (same `completed_bars_only` rule as ORB). Session VWAP is cumulative typical-price×volume from today's 09:15 IST completed bars.

**Entry cutoff:** no playbook may BUY when `current_time ≥ ENTRY_CUTOFF` (still `.env`-controlled). `SQUAREOFF_TIME` is unchanged and not overridden in strategy code.

**Scout is optional.** `INDIA_SCOUT_ENABLED=false` → core loop scans `INDIA_STOCK_UNIVERSE` (12 or ~50 names) with `mis_regime`. Scout must not bypass XOR or create a second independent BUY path.

**Risk / broker:** sizing still goes through `RiskManager`. No Dhan calls from the strategy. Do not raise `INDIA_CAPITAL_CAP`, `RISK_PER_TRADE`, or `MAX_OPEN_POSITIONS` for this strategy.

### mis_regime knobs

| Env | Default | Role |
|-----|---------|------|
| `ORB_WINDOW_MINUTES` | 60 | OPEN_DRIVE clock window |
| `ADX_RISING_LOOKBACK` | 2 | ADX expansion lookback (bars) |
| `RANGE_EXPANSION_MULT` | 1.2 | Range-expansion vs recent mean range |
| `MIS_REGIME_CONFIRM_BARS` | 2 | Stricter ORB confirm (legacy `CONFIRM_BARS` unchanged) |
| `MIS_REGIME_VOLUME_MULT` | 1.2 | Volume gate for mis_regime playbooks (legacy `VOLUME_MULT` unchanged) |
| `MOMENTUM_LOOKBACK_BARS` | 20 | Prior-N high for VWAP momentum |
| `EMA_EXTENSION_ATR` | 1.0 | HOLD Playbook 2 if extended above VWAP/EMA |
| `VWAP_STRETCH_ATR` | 1.0 | Mean-reversion stretch (`close ≤ VWAP − k×ATR`) |
| `RSI_OVERSOLD` | 30 | Mean-reversion RSI gate |
| `VWAP_RECLAIM_BARS` | 1 | Reclaim: close > prior completed high |
| `LOSS_REENTRY_COOLDOWN_MIN` | 30 | mis_regime-only pause after a same-day losing exit (`0` = off) |
| `RS_TOP_N` / `RS_LOOKBACK_BARS` | 5 / 60 | Playbook 1 relative-strength leadership (independent of `USE_RELATIVE_STRENGTH`) |

## Single India universe (no scout)

The core loop scans **`INDIA_STOCK_UNIVERSE` only**. Scout stays optional via `INDIA_SCOUT_ENABLED`. Default strategy is **`mis_regime`** (legacy `opening_range_breakout` still works via `STRATEGY_NAME`). 5-minute bars do **not** need a 20s loop — **60s** covers ~50 names; `INDIA_LOOP_FETCH_GAP_SEC=0.4` spaces REST candle calls.

### GCP VM `.env` knobs

Apply only when **MIS positions = 0**, then `docker compose up -d [--build]`. Do not put secrets in git.

```env
INDIA_SCOUT_ENABLED=false
INDIA_STOCK_UNIVERSE=RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK,HINDUNILVR,ITC,SBIN,BHARTIARTL,LT,KOTAKBANK,WIPRO,AXISBANK,BAJFINANCE,MARUTI,SUNPHARMA,TITAN,ASIANPAINT,ULTRACEMCO,NESTLEIND,POWERGRID,NTPC,ONGC,TATAMOTORS,TATASTEEL,JSWSTEEL,M&M,TECHM,HCLTECH,ADANIENT,ADANIPORTS,COALINDIA,BPCL,CIPLA,DRREDDY,APOLLOHOSP,EICHERMOT,HEROMOTOCO,INDUSINDBK,BAJAJFINSV,BAJAJ-AUTO,BRITANNIA,GRASIM,HINDALCO,DIVISLAB,HDFCLIFE,SBILIFE,TATACONSUM,BEL,TRENT
INDIA_LOOP_INTERVAL_SEC=60
INDIA_LOOP_FETCH_GAP_SEC=0.4
MAX_POSITION_PCT=0.20
RISK_PER_TRADE=0.008
MAX_OPEN_POSITIONS=2
MAX_TRADES_PER_DAY=0
MAX_DAILY_LOSS_INR=5000
TAKE_PROFIT_R=1.75
VOLUME_MULT=1.2
ENTRY_CUTOFF=14:45
SQUAREOFF_TIME=15:00
```

`INDIA_STOCK_UNIVERSE` above is the unique list from `india_scout.DEFAULT_INDIA_SCOUT_UNIVERSE` (50 names). Do **not** raise `INDIA_CAPITAL_CAP` / docker memory to “scale capital”.

## Tests

```bash
python -m unittest tests.test_mis_session tests.test_safety_fixes tests.test_day_pl tests.test_nse_tick tests.test_order_guards tests.test_zero_ltp_sl tests.test_core tests.test_mis_regime -v
```

No fixed daily profit — expectancy + risk caps only.

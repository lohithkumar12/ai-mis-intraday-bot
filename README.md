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


## Tests

```bash
python -m unittest tests.test_mis_session tests.test_safety_fixes tests.test_day_pl tests.test_nse_tick tests.test_order_guards tests.test_zero_ltp_sl tests.test_core tests.test_mis_regime -v
```

No fixed daily profit — expectancy + risk caps only.

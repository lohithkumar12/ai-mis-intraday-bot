# Paper checklist — MIS bot (New_StartUp architecture)

- [ ] Separate from `New_StartUp` CNC (different container/port)
- [ ] `.env`: `INDIA_PRODUCT_TYPE=INTRADAY`, `INDIA_CAPITAL_CAP=150000`
- [ ] `INDIA_PAPER=true`, `LIVE_TRADING=false`, `LIVE_CONFIRM=` empty
- [ ] Strategy default `STRATEGY_NAME=mis_regime` (aliases `regime_mis` / `vwap_regime`). Scout is optional and not required.
- [ ] Scout optional: `INDIA_SCOUT_ENABLED=false` for a single `INDIA_STOCK_UNIVERSE`; auto-buy off (`INDIA_SCOUT_AUTO_BUY=false`) unless intentional
- [ ] `MAX_TRADES_PER_DAY=0` means unlimited (not a default-on cap of 8)
- [ ] No new entries after `ENTRY_CUTOFF` (14:15)
- [ ] Positions flatten at/after `SQUAREOFF_TIME` (15:00) — before broker RMS
- [ ] Sizing logs show equity ≤ capital cap (not full ₹3L+ book)
- [ ] Docker: `ai_mis_intraday_bot` on port **81**
- [ ] No mid-session redeploy while open; restart only when flat / after market
- [ ] Only then arm: `LIVE_TRADING=true` + `LIVE_CONFIRM=YES_REAL_MONEY`

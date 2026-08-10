# Paper checklist — MIS bot (New_StartUp architecture)

- [ ] Separate from `New_StartUp` CNC (different container/port)
- [ ] `.env`: `INDIA_PRODUCT_TYPE=INTRADAY`, `INDIA_CAPITAL_CAP=150000`
- [ ] `INDIA_PAPER=true`, `LIVE_TRADING=false`, `LIVE_CONFIRM=` empty
- [ ] Scout auto-buy off (`INDIA_SCOUT_AUTO_BUY=false`) for quieter first runs
- [ ] No new entries after `ENTRY_CUTOFF` (14:45)
- [ ] Positions flatten at/after `SQUAREOFF_TIME` (15:10)
- [ ] Sizing logs show equity ≤ capital cap (not full ₹3L+ book)
- [ ] Docker: `ai_mis_intraday_bot` on port **81**
- [ ] Only then arm: `LIVE_TRADING=true` + `LIVE_CONFIRM=YES_REAL_MONEY`

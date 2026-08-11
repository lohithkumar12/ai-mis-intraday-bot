# Migrate MIS bot → primary VM (`35.200.221.125`)

Dhan rejects orders from secondary IP with **DH-905 Invalid IP**.
Run MIS only on the VM whose public IP is **primary** `35.200.221.125`.
Stop MIS on secondary `35.200.249.211` so you never double-fire live orders.

CNC (New_StartUp) can stay on the same primary VM: **port 80**. MIS uses **port 81**.

---

## A) On SECONDARY VM (`35.200.249.211`) — stop MIS

```bash
curl -4 ifconfig.me   # expect 35.200.249.211
cd ~/ai-mis-intraday-bot
sudo docker compose down
# optional: keep a copy of .env before you leave
cp -n .env ~/mis-bot.env.backup
```

Confirm container is gone:

```bash
sudo docker ps -a | grep ai_mis_intraday_bot || echo "MIS stopped"
```

---

## B) On PRIMARY VM (`35.200.221.125`) — deploy MIS

```bash
curl -4 ifconfig.me   # MUST print 35.200.221.125
```

### 1. Get code

```bash
cd ~
if [ -d ai-mis-intraday-bot ]; then
  cd ai-mis-intraday-bot && git pull
else
  git clone https://github.com/lohithkumar12/ai-mis-intraday-bot.git
  cd ai-mis-intraday-bot
fi
```

### 2. Env (do not overwrite CNC `.env`)

Copy `.env` from secondary (scp / paste), or restore from backup. Required live flags:

```env
LIVE_TRADING=true
LIVE_CONFIRM=YES_REAL_MONEY
INDIA_PAPER=false
INDIA_PRODUCT_TYPE=INTRADAY
INDIA_CAPITAL_CAP=150000
```

Same `DHAN_CLIENT_ID` / token as the API app that whitelists `35.200.221.125`.

### 3. Firewall (GCP)

Allow **TCP 81** to this VM (dashboard). Port **80** stays CNC.

### 4. Start MIS (CNC untouched)

```bash
cd ~/ai-mis-intraday-bot
sudo docker compose up -d --build
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:81/
sudo docker compose logs --tail=50 bot | grep -E 'REAL MONEY|Ready|Invalid IP|error'
```

Dashboard: **http://35.200.221.125:81/**

### 5. Prove egress is primary

```bash
curl -4 ifconfig.me
sudo docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('https://ifconfig.me/ip', timeout=10).read().decode())"
```

Both must show `35.200.221.125`.

---

## C) Next session check

```bash
sudo docker compose logs -f bot | grep -E 'BUY SIGNAL|Sizing:|LIVE BUY|DH-905|BUY order failed'
```

Success = `LIVE BUY ORDER` (not `DH-905 Invalid IP`).

---

## Do not

- Run live MIS on **both** VMs
- SNAT / iptables spoof of the other VM’s IP
- Change CNC compose ports or reuse `ai_stock_trading_bot` for MIS

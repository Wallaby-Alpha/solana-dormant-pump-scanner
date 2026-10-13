# Solana Dormant Token Scanner & Revival Radar

Detects "dead coin wakes up -> pump -> dump" setups on Solana and alerts instantly via Telegram & Discord.

## Operating Modes

### 1. `radar` [RECOMMENDED - Token-First Mode]
Streams DexScreener's live token boost feed, profile updates, and sudden volume spikes:
- **Filters strictly for old coins**: Pool created over 180 days ago (`pairCreatedAt >= 180d`).
- **Skips new launches**: Pump.fun and fresh tokens are automatically discarded.
- **Zero Helius credit drain**: 100% free DexScreener/DexPaprika streaming.
- **Immune to burner wallets**: Detects the coin regardless of which wallet triggers the pump.
- **Optional wallet enrichment**: If `HELIUS_API_KEY` is present, it auto-profiles the top 3 buyers in the Telegram alert.

```bash
python dormant_pump_scanner.py radar --min-age 180 --max-mcap 75000 --interval 12
```

### 2. `scan` [Pool Screener]
Screens older pools with recent price spikes on DexPaprika, verifies candlestick dormancy, and stores early buyers in SQLite.
```bash
python dormant_pump_scanner.py scan --min-age 30 --loop 60
```

### 3. `analyze` [Single Pool Deep-Dive]
Profiles a specific Solana pool address (candles, dormancy status, and buyer cohort).
```bash
python dormant_pump_scanner.py analyze <POOL_ADDRESS>
```

### 4. `wallets` [Leaderboard]
View wallets that have repeatedly traded this playbook across multiple tokens.
```bash
python dormant_pump_scanner.py wallets --min-tokens 2
```

### 5. `watch` [Wallet Watcher]
Polls Helius for transactions from wallets on your leaderboard.
```bash
python dormant_pump_scanner.py watch --interval 45
```

### 6. `outcomes` [Historical Pattern Analyzer]
Tracks what happens to every coin found by the scanner over the next 4 hours:
- Automatically tracks checkpoints at **5m, 15m, 30m, 1h, 2h, 4h**.
- Records peak multiple, peak market cap, and time to peak.
- Classifies tokens as **Big Runners ($\ge$ 3x)**, **Modest Runs (1.5x - 3x)**, or **Immediate Dumps (< 1.5x)**.
- Compares entry liquidity, 5m volume, social presence, and buyer counts between runners and dumpers.
- Exports complete dataset to CSV for Excel / Pandas analysis.

```bash
# View summary table and comparative patterns
python dormant_pump_scanner.py outcomes

# Export all tracked data to CSV for Excel/Pandas
python dormant_pump_scanner.py outcomes --csv outcomes.csv
```

---

## Deploying to a DigitalOcean Droplet

### 1. Connect to Droplet
```bash
ssh root@YOUR_DROPLET_IP

# Install dependencies
apt update && apt install -y python3 python3-pip git

# Clone repository
git clone https://github.com/Wallaby-Alpha/solana-dormant-pump-scanner.git
cd solana-dormant-pump-scanner

# Install Python requirements
pip3 install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
nano .env
# Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (HELIUS_API_KEY optional)
```

### 3. Run 24/7 as a Background Service
```bash
cp solana-scanner.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable solana-scanner
systemctl start solana-scanner

# Check status and live alerts
systemctl status solana-scanner
journalctl -u solana-scanner -f
```

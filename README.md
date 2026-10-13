# Solana Dormant Pump Scanner & Wallet Tracker

Detects "dead coin wakes up -> pump -> dump" playbooks on Solana and profiles organized teams of buyers.

## Key Features
- **Candidate Screener (`scan`)**: Identifies old pools ripping with sudden volume and confirms historical dormancy (candle gap detection).
- **Deep Single Pool Analysis (`analyze`)**: Backtests and breaks down any specific Solana pool.
- **Repeat Wallet Leaderboard (`wallets`)**: Aggregates wallets executing this strategy across multiple tokens.
- **Real-Time Watcher (`watch`)**: Polls the wallet leaderboard in real time and sends instant Telegram & Discord alerts when an orchestrator enters a dormant coin.
- **Telegram & Discord Alerts**: Formats alerts with instant links to DexScreener, Photon, BullX, and Solscan.

---

## Quickstart (Local)

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
   ```

3. **Run Commands**:
   - Screen active pools:
     ```bash
     python dormant_pump_scanner.py scan --loop 60
     ```
   - Analyze a known pool:
     ```bash
     python dormant_pump_scanner.py analyze <POOL_ADDRESS>
     ```
   - View top repeating wallets:
     ```bash
     python dormant_pump_scanner.py wallets --min-tokens 2
     ```
   - Watch wallets and alert via Telegram:
     ```bash
     python dormant_pump_scanner.py watch --interval 20
     ```

---

## Deploying to a DigitalOcean Droplet

### 1. Create Droplet
- OS: Ubuntu 22.04 LTS or 24.04 LTS
- Plan: Basic ($4-$6/mo droplet is plenty)

### 2. Connect and Clone
```bash
ssh root@YOUR_DROPLET_IP

# Update packages and install python/pip/git
apt update && apt install -y python3 python3-pip git

# Clone repo
git clone https://github.com/Wallaby-Alpha/solana-dormant-pump-scanner.git
cd solana-dormant-pump-scanner

# Install requirements
pip3 install -r requirements.txt
```

### 3. Setup Environment Variables
```bash
cp .env.example .env
nano .env
# Fill in your HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

### 4. Run 24/7 as a Background Service (systemd)
```bash
# Copy service file to systemd directory
cp solana-scanner.service /etc/systemd/system/

# Reload systemd and enable service
systemctl daemon-reload
systemctl enable solana-scanner
systemctl start solana-scanner

# Check status and live logs
systemctl status solana-scanner
journalctl -u solana-scanner -f
```

*(Alternatively, run inside `tmux` if you prefer an interactive terminal: `tmux new -s scanner`, run `python3 dormant_pump_scanner.py watch`, then detach with `Ctrl+B then D`).*

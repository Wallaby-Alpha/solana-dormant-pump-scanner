#!/usr/bin/env python3
"""
dormant_pump_scanner.py
Detects "dead coin wakes up -> pump -> dump" setups on Solana.

Modes:
  radar     [RECOMMENDED] Zero-cost DexScreener & DexPaprika stream.
            Catches old tokens (>6mo) that are newly active or boosted.
            Immune to burner wallets, zero Helius credit drain.
  scan      Screen old pools ripping on DexPaprika and profile early buyers.
  analyze   Deep dive into one pool address (candles + buyer profiling).
  wallets   Leaderboard of wallets that repeat the playbook across tokens.
  watch     Poll Helius for transactions from leaderboard wallets.

Env:
  TELEGRAM_BOT_TOKEN  (optional; instant HTML Telegram alerts)
  TELEGRAM_CHAT_ID    (optional; target chat/group for Telegram)
  DISCORD_WEBHOOK     (optional; secondary Discord alerts)
  HELIUS_API_KEY      (optional for radar; required for wallet profiling / watch)
  DEXPAPRIKA_API_KEY  (optional; switches to api-pro host)
"""
import argparse, html, json, os, sqlite3, statistics, time
from collections import defaultdict
from datetime import datetime, timezone

import requests

HELIUS_KEY = os.getenv("HELIUS_API_KEY")
PAPRIKA_KEY = os.getenv("DEXPAPRIKA_API_KEY")
PAPRIKA = "https://api-pro.dexpaprika.com" if PAPRIKA_KEY else "https://api.dexpaprika.com"
HELIUS = "https://api.helius.xyz/v0"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

NET = "solana"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNyFmmbJk3dkLCXbqt",  # USDT
}
BASE_MINTS = STABLES | {WSOL}
S = requests.Session()


# ----------------------------------------------------------------- http
class Forbidden(Exception):
    pass


def http_get(url, params=None, headers=None, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** i)
                continue
            if r.status_code == 403:
                raise Forbidden(r.text[:200])
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1 + i)
    raise RuntimeError("rate limited: " + url)


def paprika(path, **params):
    h = {"Authorization": PAPRIKA_KEY} if PAPRIKA_KEY else None
    return http_get(PAPRIKA + path, params, h)


def ts(v):
    if v is None:
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())


def fmt(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")


# ----------------------------------------------------------------- dexpaprika & dexscreener
def sol_price():
    try:
        r = S.get(f"https://api.dexscreener.com/latest/dex/tokens/{WSOL}", timeout=10).json()
        pairs = r.get("pairs") or []
        if pairs:
            return float(pairs[0].get("priceUsd") or 150.0)
    except Exception:
        pass
    try:
        return float(paprika(f"/networks/{NET}/tokens/{WSOL}")["summary"]["price_usd"])
    except Exception:
        return 150.0


def split_tokens(row):
    """-> (target_mint, symbol) from a pool row, ignoring SOL/stables."""
    for t in row.get("tokens") or []:
        mint = t.get("id") or t.get("address")
        if mint and mint not in BASE_MINTS:
            return mint, t.get("symbol")
    return None, None


def pool_info(pool):
    return split_tokens(paprika(f"/networks/{NET}/pools/{pool}"))


def find_candidates(a):
    """
    Finds dormant pools waking up RIGHT NOW.
    Prioritizes 5-minute price surges with low transaction thresholds to catch the first buy.
    """
    found = {}
    # Scan 5m first (earliest possible signal), then 1h
    fields = ("price_change_percentage_5m_min", "price_change_percentage_1h_min")
    for field in fields:
        cursor = None
        min_change = a.min_change_5m if "5m" in field else a.min_change
        for _ in range(a.pages):
            p = {
                "created_before": f"-{a.min_age}d",
                "txns_24h_min": a.min_txns,
                "volume_usd_24h_min": a.min_vol,
                field: min_change,
                "order_by": field.replace("_min", ""),
                "sort": "desc",
                "limit": 50,
            }
            if cursor:
                p["cursor"] = cursor
            try:
                d = paprika(f"/networks/{NET}/pools/search", **p)
                for row in d.get("results", []):
                    found[row["id"]] = row
                cursor = d.get("next_cursor")
                if not d.get("has_next_page"):
                    break
            except Exception:
                break
    return list(found.values())


def get_ohlcv(pool, start, interval, limit):
    d = paprika(f"/networks/{NET}/pools/{pool}/ohlcv", start=start, interval=interval, limit=limit)
    if isinstance(d, dict):
        d = d.get("ohlcv") or d.get("data") or []
    out = [
        dict(
            t=ts(c.get("time_open")),
            o=float(c["open"]),
            h=float(c["high"]),
            l=float(c["low"]),
            c=float(c["close"]),
            v=float(c.get("volume") or 0),
        )
        for c in d
    ]
    return sorted(out, key=lambda c: c["t"])


# ----------------------------------------------------------------- detection
def analyze_candles(daily, hourly, a, now=None):
    """Pure function: decide dormancy + spike. Candle gaps (no trades) count as inactive."""
    now = now or int(time.time())
    cutoff = now - a.hours * 3600
    base = [c for c in daily if c["t"] < cutoff]
    active_days = sum(1 for c in base if c["v"] > 0)
    span = max(a.lookback - a.hours / 24, 1)
    active_frac = active_days / span
    dormant = active_frac <= a.max_active_frac

    baseline = statistics.median([c["c"] for c in base]) if base else (hourly[0]["o"] if hourly else 0)
    ev = dict(
        dormant=dormant,
        active_frac=active_frac,
        baseline=baseline,
        spiked=False,
        peak=0,
        mult=0,
        retrace=1,
        start=None,
        peak_t=None,
        status="-",
    )
    if not hourly or baseline <= 0:
        return ev
    pk = max(hourly, key=lambda c: c["h"])
    cur = hourly[-1]["c"]
    ev.update(peak=pk["h"], peak_t=pk["t"], mult=pk["h"] / baseline, retrace=cur / pk["h"])
    ev["start"] = next((c["t"] for c in hourly if c["h"] >= 1.5 * baseline), hourly[0]["t"])
    ev["spiked"] = ev["mult"] >= a.spike_mult
    ev["status"] = "DUMPED" if ev["retrace"] <= a.dumped_below else "LIVE"
    return ev


def evaluate_pool(pool, a):
    now = int(time.time())
    try:
        daily = get_ohlcv(pool, f"-{a.lookback}d", "24h", a.lookback + 1)
    except Forbidden:
        daily = get_ohlcv(pool, "-30d", "24h", 31)
        a.lookback = 30
    hourly = get_ohlcv(pool, f"-{a.hours}h", "1h", a.hours + 1)
    return analyze_candles(daily, hourly, a, now)


# ----------------------------------------------------------------- helius
def helius_swaps(addr, since_ts, max_pages=10, limit=100):
    if not HELIUS_KEY:
        return []
    out, before = [], None
    for _ in range(max_pages):
        p = {"api-key": HELIUS_KEY, "type": "SWAP", "limit": limit}
        if before:
            p["before"] = before
        try:
            batch = http_get(f"{HELIUS}/addresses/{addr}/transactions", p)
        except Exception:
            break
        if not batch:
            break
        out.extend(batch)
        before = batch[-1]["signature"]
        if batch[-1]["timestamp"] < since_ts:
            break
    return [t for t in out if t["timestamp"] >= since_ts]


def parse_swap(tx, mint):
    payer = tx.get("feePayer")
    tok = wsol = stable = 0.0
    for t in tx.get("tokenTransfers") or []:
        amt = float(t.get("tokenAmount") or 0)
        to_acc = t.get("toUserAccount")
        from_acc = t.get("fromUserAccount")
        sign = (to_acc == payer) - (from_acc == payer)
        m = t.get("mint")
        if m == mint:
            tok += sign * amt
        elif m == WSOL:
            wsol += sign * amt
        elif m in STABLES:
            stable += sign * amt
    native = 0.0
    for ad in tx.get("accountData") or []:
        if ad.get("account") == payer:
            native = (ad.get("nativeBalanceChange") or 0) / 1e9
    sol = native if abs(native) >= abs(wsol) else wsol
    return payer, tok, sol, stable


def aggregate_wallets(txs, mint, sol_usd, min_usd, early_mins):
    w = defaultdict(lambda: dict(buy=0.0, sell=0.0, tb=0.0, ts=0.0, first=None, side=None))
    for tx in sorted(txs, key=lambda t: t["timestamp"]):
        payer, tok, sol, stable = parse_swap(tx, mint)
        if not payer or tok == 0:
            continue
        usd = abs(sol) * sol_usd + abs(stable)
        x = w[payer]
        if x["first"] is None:
            x["first"], x["side"] = tx["timestamp"], "buy" if tok > 0 else "sell"
        if tok > 0:
            x["buy"] += usd
            x["tb"] += tok
        else:
            x["sell"] += usd
            x["ts"] += -tok
    rows = []
    for addr, x in w.items():
        if x["side"] != "buy" or x["buy"] < min_usd:
            continue
        rows.append(
            dict(
                wallet=addr,
                first=x["first"],
                buy=x["buy"],
                sell=x["sell"],
                pnl=x["sell"] - x["buy"],
                exit_frac=min(x["ts"] / x["tb"], 9.99) if x["tb"] else 0,
            )
        )
    if rows:
        t0 = min(r["first"] for r in rows)
        for r in rows:
            r["early"] = int(r["first"] - t0 <= early_mins * 60)
    return sorted(rows, key=lambda r: r["first"])


# ----------------------------------------------------------------- alerts
def send_telegram(text):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        S.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        print(f"Telegram error: {e}")


def send_discord(text):
    if not DISCORD_WEBHOOK:
        return
    try:
        S.post(DISCORD_WEBHOOK, json={"content": text}, timeout=10)
    except requests.RequestException:
        pass


def notify_radar_alert(pair_data, trigger="Radar", early_buyers=None):
    sym = pair_data.get("baseToken", {}).get("symbol", "UNKNOWN")
    name = pair_data.get("baseToken", {}).get("name", "")
    mint = pair_data.get("baseToken", {}).get("address", "")
    pool = pair_data.get("pairAddress", "")
    age_days = pair_data.get("_age_days", 0)
    mcap = float(pair_data.get("fdv") or pair_data.get("marketCap") or 0)
    liq = float(pair_data.get("liquidity", {}).get("usd") or 0)
    vol_h1 = float(pair_data.get("volume", {}).get("h1") or 0)
    vol_m5 = float(pair_data.get("volume", {}).get("m5") or 0)
    change_5m = float(pair_data.get("priceChange", {}).get("m5") or 0)
    change_h1 = float(pair_data.get("priceChange", {}).get("h1") or 0)
    txns_5m = pair_data.get("txns", {}).get("m5", {}).get("buys", 0) + pair_data.get("txns", {}).get("m5", {}).get("sells", 0)

    # Determine alert urgency and stage
    if "5m" in trigger or "Early" in trigger or txns_5m <= 5:
        stage_header = "🟢 <b>STAGE 1: FIRST WAKE-UP (MINUTE 0-5)</b>\n<i>Team is beginning initial entry on dead pool!</i>"
    elif "Boost" in trigger:
        stage_header = "⚠️ <b>STAGE 3: LATE / BOOST PAID (DUMP RISK)</b>\n<i>Team paid for DexScreener boost; possible exit liquidity!</i>"
    else:
        stage_header = f"🚨 <b>DORMANT COIN WAKES UP ({trigger})</b>"

    buyers_text = ""
    if early_buyers:
        buyers_text = "\n<b>Early Big Buyers:</b>\n"
        for b in early_buyers[:3]:
            w_short = f"{b['wallet'][:4]}...{b['wallet'][-4:]}"
            buyers_text += (
                f"• <a href='https://solscan.io/account/{b['wallet']}'>{w_short}</a>: "
                f"in ${b['buy']:,.0f} | pnl ${b['pnl']:+,.0f}\n"
            )

    tg_text = (
        f"{stage_header}\n\n"
        f"<b>Token:</b> ${html.escape(sym)} ({html.escape(name)})\n"
        f"<b>Age:</b> <b>{age_days:.0f} days old</b> (Dormant ~{age_days/30:.1f} months)\n"
        f"<b>Market Cap:</b> ${mcap:,.0f}\n"
        f"<b>Liquidity:</b> ${liq:,.0f}\n"
        f"<b>5m Change:</b> <b>{change_5m:+.1f}%</b> | <b>1h Change:</b> {change_h1:+.1f}%\n"
        f"<b>Volume:</b> 5m: ${vol_m5:,.0f} | 1h: ${vol_h1:,.0f}\n"
        f"{buyers_text}\n"
        f"<b>Mint:</b> <code>{mint}</code>\n\n"
        f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a> | "
        f"<a href='https://photon-sol.tinyastro.io/en/lp/{pool}'>Photon</a> | "
        f"<a href='https://bullx.io/terminal?chainId=1399811149&address={mint}'>BullX</a> | "
        f"<a href='https://solscan.io/token/{mint}'>Solscan</a>"
    )

    plain = (
        f"[{trigger}] ${sym} waking up! Age: {age_days:.0f}d, Mcap: ${mcap:,.0f}, "
        f"5m: {change_5m:+.1f}%, 1h Vol: ${vol_h1:,.0f}, Pool: {pool}"
    )
    print("\n" + "="*60 + "\n*** RADAR ALERT *** " + plain + "\n" + "="*60)
    send_telegram(tg_text)
    send_discord(plain)


def notify_watch_alert(wallet, mint, pool, info, buy_usd=None):
    plain = (
        f"{wallet} bought {mint} (pool {pool}), dormant coin: only {info['active_frac']:.0%} of prior days "
        f"had trades; now {info['mult']:.1f}x baseline"
    )
    print("\n*** WATCH ALERT ***", plain)

    buy_str = f"<b>Buy:</b> ~${buy_usd:,.0f}\n" if buy_usd else ""
    tg_text = (
        f"🚨 <b>Tracked Wallet Bought Dormant Coin</b>\n\n"
        f"<b>Wallet:</b> <code>{wallet}</code> (<a href='https://solscan.io/account/{wallet}'>Solscan</a>)\n"
        f"<b>Token:</b> <code>{mint}</code>\n"
        f"<b>Pool:</b> <code>{pool}</code>\n"
        f"{buy_str}"
        f"<b>Dormancy:</b> {info['active_frac']:.0%} of past {info.get('lookback', 30)}d had volume\n"
        f"<b>Spike:</b> {info['mult']:.1f}x from baseline\n\n"
        f"🔗 <a href='https://dexscreener.com/solana/{mint}'>DexScreener</a> | "
        f"<a href='https://photon-sol.tinyastro.io/en/lp/{pool}'>Photon</a> | "
        f"<a href='https://bullx.io/terminal?chainId=1399811149&address={mint}'>BullX</a>"
    )

    send_telegram(tg_text)
    send_discord(plain)


# ----------------------------------------------------------------- storage
def db():
    c = sqlite3.connect("scanner.db")
    c.executescript(
        """
    CREATE TABLE IF NOT EXISTS events(pool TEXT, token TEXT, symbol TEXT, seen INT, baseline REAL,
        peak REAL, mult REAL, retrace REAL, status TEXT, PRIMARY KEY(pool, seen));
    CREATE TABLE IF NOT EXISTS wallet_events(wallet TEXT, pool TEXT, token TEXT, first INT, buy REAL,
        sell REAL, pnl REAL, exit_frac REAL, early INT, PRIMARY KEY(wallet, pool));
    CREATE TABLE IF NOT EXISTS radar_events(token TEXT PRIMARY KEY, pool TEXT, symbol TEXT, seen INT,
        age_days REAL, mcap REAL, trigger TEXT);
    CREATE TABLE IF NOT EXISTS token_outcomes(
        token TEXT PRIMARY KEY, symbol TEXT, pool TEXT, alerted_at INT, age_days REAL,
        entry_price REAL, entry_mcap REAL, entry_liquidity REAL, entry_vol_5m REAL,
        entry_vol_1h REAL, entry_txns_5m INT, entry_change_5m REAL, trigger TEXT,
        has_socials INT, has_boost INT, early_buyers_count INT, early_buyers_usd REAL,
        mult_5m REAL, mult_15m REAL, mult_30m REAL, mult_1h REAL, mult_2h REAL, mult_4h REAL,
        peak_mult REAL, peak_mcap REAL, peak_time_mins REAL, current_mult REAL,
        outcome_class TEXT, last_updated INT, is_finished INT);"""
    )
    return c


def process_pool(con, pool, mint, sym, ev, a, sol_usd):
    con.execute(
        "INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?)",
        (pool, mint, sym, int(time.time()), ev["baseline"], ev["peak"], ev["mult"], ev["retrace"], ev["status"]),
    )
    print(
        f"\n[{ev['status']}] {sym or mint}  pool={pool}\n   dormant: only {ev['active_frac']:.0%} of prior days had trades"
        f" | peak {ev['mult']:.1f}x baseline at {fmt(ev['peak_t'])} | now {ev['retrace']:.0%} of peak"
    )
    if not HELIUS_KEY or getattr(a, "no_wallets", False):
        return
    txs = helius_swaps(pool, ev["start"] - 3600, a.max_pages)
    rows = aggregate_wallets(txs, mint, sol_usd, a.min_wallet_usd, a.early_mins)
    print(f"   {len(txs)} swaps fetched, {len(rows)} wallets bought >= ${a.min_wallet_usd:,.0f} as their first action")
    for r in rows[: getattr(a, "top", 15)]:
        tag = "EARLY" if r["early"] else "     "
        print(
            f"   {tag} {r['wallet']}  in ${r['buy']:>8,.0f}  out ${r['sell']:>8,.0f}  pnl ${r['pnl']:>+8,.0f}"
            f"  exited {r['exit_frac']:.0%}  first buy {fmt(r['first'])}"
        )
    for r in rows:
        con.execute(
            "INSERT OR REPLACE INTO wallet_events VALUES(?,?,?,?,?,?,?,?,?)",
            (r["wallet"], pool, mint, r["first"], r["buy"], r["sell"], r["pnl"], r["exit_frac"], r["early"]),
        )
    con.commit()


# ----------------------------------------------------------------- outcome tracker
def record_initial_outcome(con, p, trigger, early_buyers=None):
    mint = p.get("baseToken", {}).get("address")
    if not mint:
        return
    sym = p.get("baseToken", {}).get("symbol", "")
    pool = p.get("pairAddress", "")
    now = int(time.time())
    entry_price = float(p.get("priceUsd") or 0.0)
    entry_mcap = float(p.get("fdv") or p.get("marketCap") or 0.0)
    entry_liq = float(p.get("liquidity", {}).get("usd") or 0.0)
    vol_5m = float(p.get("volume", {}).get("m5") or 0.0)
    vol_1h = float(p.get("volume", {}).get("h1") or 0.0)
    txns_5m = int(p.get("txns", {}).get("m5", {}).get("buys", 0) + p.get("txns", {}).get("m5", {}).get("sells", 0))
    change_5m = float(p.get("priceChange", {}).get("m5") or 0.0)
    age_days = float(p.get("_age_days") or 0.0)
    has_socials = int(bool(p.get("info", {}).get("socials") or p.get("info", {}).get("websites")))
    has_boost = int("Boost" in trigger or (p.get("boosts", {}).get("active", 0) > 0))

    buyers_count = len(early_buyers) if early_buyers else 0
    buyers_usd = sum(b.get("buy", 0.0) for b in early_buyers) if early_buyers else 0.0

    con.execute("""
        INSERT OR IGNORE INTO token_outcomes (
            token, symbol, pool, alerted_at, age_days, entry_price, entry_mcap,
            entry_liquidity, entry_vol_5m, entry_vol_1h, entry_txns_5m, entry_change_5m,
            trigger, has_socials, has_boost, early_buyers_count, early_buyers_usd,
            peak_mult, peak_mcap, peak_time_mins, current_mult, outcome_class,
            last_updated, is_finished
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        mint, sym, pool, now, age_days, entry_price, entry_mcap,
        entry_liq, vol_5m, vol_1h, txns_5m, change_5m,
        trigger, has_socials, has_boost, buyers_count, buyers_usd,
        1.0, entry_mcap, 0.0, 1.0, "ACTIVE",
        now, 0
    ))
    con.commit()


def update_tracked_outcomes(con):
    """
    Checks on-chain prices for all alerted tokens across the last 4 hours.
    Records checkpoints at 5m, 15m, 30m, 1h, 2h, 4h and registers peak multipliers.
    """
    now = int(time.time())
    rows = con.execute("""
        SELECT token, pool, alerted_at, entry_price, entry_mcap, peak_mult, peak_mcap, peak_time_mins,
               mult_5m, mult_15m, mult_30m, mult_1h, mult_2h, mult_4h
        FROM token_outcomes
        WHERE is_finished = 0
    """).fetchall()

    if not rows:
        return

    mints = [r[0] for r in rows]
    token_map = {r[0]: r for r in rows}

    for i in range(0, len(mints), 30):
        batch = mints[i : i + 30]
        pairs = fetch_dexscreener_pairs(batch)
        for p in pairs:
            mint = p.get("baseToken", {}).get("address")
            if mint not in token_map:
                continue
            r = token_map[mint]
            alerted_at, entry_price, entry_mcap = r[2], r[3], r[4]
            peak_mult, peak_mcap, peak_time_mins = r[5], r[6], r[7]
            mult_5m, mult_15m, mult_30m, mult_1h, mult_2h, mult_4h = r[8], r[9], r[10], r[11], r[12], r[13]

            cur_price = float(p.get("priceUsd") or 0.0)
            cur_mcap = float(p.get("fdv") or p.get("marketCap") or 0.0)
            cur_liq = float(p.get("liquidity", {}).get("usd") or 0.0)
            elapsed_mins = (now - alerted_at) / 60.0

            cur_mult = cur_price / entry_price if entry_price > 0 else 1.0

            # Update peak multiple & peak market cap
            if cur_mult > (peak_mult or 1.0):
                peak_mult = cur_mult
                peak_mcap = cur_mcap
                peak_time_mins = round(elapsed_mins, 1)

            # Checkpoints
            if mult_5m is None and elapsed_mins >= 5:
                mult_5m = cur_mult
            if mult_15m is None and elapsed_mins >= 15:
                mult_15m = cur_mult
            if mult_30m is None and elapsed_mins >= 30:
                mult_30m = cur_mult
            if mult_1h is None and elapsed_mins >= 60:
                mult_1h = cur_mult
            if mult_2h is None and elapsed_mins >= 120:
                mult_2h = cur_mult
            if mult_4h is None and elapsed_mins >= 240:
                mult_4h = cur_mult

            # Outcome classification
            is_finished = 0
            if elapsed_mins >= 240 or (cur_liq < 300 and elapsed_mins >= 10):
                is_finished = 1
                if peak_mult >= 3.0:
                    outcome_class = "BIG_RUNNER"
                elif peak_mult >= 1.5:
                    outcome_class = "MODEST_RUN"
                else:
                    outcome_class = "DUMP_IMMEDIATE"
            else:
                if peak_mult >= 3.0:
                    outcome_class = "BIG_RUNNER"
                elif elapsed_mins >= 25 and peak_mult < 1.35 and cur_mult < 0.75:
                    outcome_class = "DUMP_IMMEDIATE"
                else:
                    outcome_class = "ACTIVE"

            con.execute("""
                UPDATE token_outcomes SET
                    mult_5m=?, mult_15m=?, mult_30m=?, mult_1h=?, mult_2h=?, mult_4h=?,
                    peak_mult=?, peak_mcap=?, peak_time_mins=?, current_mult=?,
                    outcome_class=?, last_updated=?, is_finished=?
                WHERE token=?
            """, (
                mult_5m, mult_15m, mult_30m, mult_1h, mult_2h, mult_4h,
                peak_mult, peak_mcap, peak_time_mins, cur_mult,
                outcome_class, now, is_finished, mint
            ))
    con.commit()


# ----------------------------------------------------------------- radar (dexscreener token-first)
def fetch_dexscreener_pairs(token_addresses):
    """Batch fetch up to 30 token addresses in a single request."""
    if not token_addresses:
        return []
    url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(token_addresses[:30])
    try:
        r = S.get(url, timeout=10).json()
        return r.get("pairs") or []
    except Exception as e:
        print(f"DexScreener fetch error: {e}")
        return []


def check_candidate_pair(p, a, trigger):
    if p.get("chainId") != "solana":
        return None
    created = p.get("pairCreatedAt")
    if not created:
        return None

    now_ms = time.time() * 1000
    age_days = (now_ms - created) / (86400 * 1000)
    if age_days < a.min_age:
        return None

    mcap = float(p.get("fdv") or p.get("marketCap") or 0)
    if mcap > a.max_mcap:
        return None

    liq = float(p.get("liquidity", {}).get("usd") or 0)
    if a.max_liquidity and liq > a.max_liquidity:
        return None

    vol_h1 = float(p.get("volume", {}).get("h1") or 0)
    vol_m5 = float(p.get("volume", {}).get("m5") or 0)
    change_5m = float(p.get("priceChange", {}).get("m5") or 0)

    # In radar mode, require either a boost, recent 5m price pop, or volume surge
    if "5m" in trigger:
        if change_5m < getattr(a, "min_change_5m", 10.0) and vol_m5 < getattr(a, "min_vol_m5", 100.0):
            return None
    elif trigger == "Volume Spike" and vol_h1 < a.min_vol_h1 and vol_m5 < a.min_vol_m5:
        return None

    p["_age_days"] = age_days
    return p


def cmd_radar(a):
    """
    Real-time token radar. Prioritizes 5-minute price surges and sudden early buys,
    while watching DexScreener boosts for late-stage warnings.
    Requires ZERO Helius credits to discover tokens.
    """
    con = db()
    sol_usd = sol_price()
    print(f"{datetime.now():%H:%M:%S} [RADAR] Starting early wake-up token scanner:")
    print(f"   Age >= {a.min_age}d | 5m Change >= +{a.min_change_5m}% | Mcap <= ${a.max_mcap:,.0f} | Max Liq <= ${a.max_liquidity:,.0f}")
    print(f"   Alerts will go to Telegram: {'Enabled' if TELEGRAM_TOKEN else 'Disabled (Set TELEGRAM_BOT_TOKEN)'}")

    # Load previously seen tokens from the last 12 hours
    seen_tokens = set(
        r[0]
        for r in con.execute(
            "SELECT token FROM radar_events WHERE seen > ?", (int(time.time()) - 12 * 3600,)
        ).fetchall()
    )

    last_paprika_scan = 0
    last_outcome_check = 0

    while True:
        try:
            # 1. PRIORITY: Check for 5-minute price rips on dormant pools (MINUTE ZERO DETECTION)
            if time.time() - last_paprika_scan >= a.interval:
                last_paprika_scan = time.time()
                try:
                    cands = find_candidates(a)
                    if cands:
                        p_mints = [split_tokens(row)[0] for row in cands if split_tokens(row)[0]]
                        p_mints = [m for m in p_mints if m not in seen_tokens]
                        if p_mints:
                            pairs = fetch_dexscreener_pairs(p_mints[:30])
                            for p in pairs:
                                matched = check_candidate_pair(p, a, "5m Price Spike")
                                if matched:
                                    mint = matched["baseToken"]["address"]
                                    pool = matched["pairAddress"]
                                    sym = matched["baseToken"].get("symbol", "")
                                    seen_tokens.add(mint)
                                    con.execute(
                                        "INSERT OR REPLACE INTO radar_events VALUES(?,?,?,?,?,?,?)",
                                        (mint, pool, sym, int(time.time()), matched["_age_days"], float(matched.get("fdv") or 0), "5m Price Spike"),
                                    )
                                    con.commit()

                                    early_buyers = None
                                    if HELIUS_KEY and not a.no_helius:
                                        txs = helius_swaps(pool, int(time.time()) - 3600, max_pages=5)
                                        early_buyers = aggregate_wallets(txs, mint, sol_usd, a.min_wallet_usd, early_mins=30)

                                    notify_radar_alert(matched, trigger="5m Price Spike", early_buyers=early_buyers)
                                    record_initial_outcome(con, matched, "5m Price Spike", early_buyers=early_buyers)
                except Exception as e:
                    print(f"[Radar] 5m sweep error: {e}")

            # 2. Check DexScreener Token Boosts & Profile Updates (Secondary Stream)
            boost_tokens = []
            try:
                r = S.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=8).json()
                boost_tokens = [x["tokenAddress"] for x in r if x.get("chainId") == "solana"]
            except Exception as e:
                pass

            profile_tokens = []
            try:
                r = S.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=8).json()
                profile_tokens = [x["tokenAddress"] for x in r if x.get("chainId") == "solana"]
            except Exception as e:
                pass

            candidate_mints = [
                m for m in set(boost_tokens + profile_tokens) if m not in seen_tokens and m not in BASE_MINTS
            ]

            if candidate_mints:
                for i in range(0, len(candidate_mints), 30):
                    batch = candidate_mints[i : i + 30]
                    pairs = fetch_dexscreener_pairs(batch)
                    for p in pairs:
                        trigger = "DexScreener Boost" if p.get("baseToken", {}).get("address") in boost_tokens else "Profile Update"
                        matched = check_candidate_pair(p, a, trigger)
                        if matched:
                            mint = matched["baseToken"]["address"]
                            pool = matched["pairAddress"]
                            sym = matched["baseToken"].get("symbol", "")
                            seen_tokens.add(mint)
                            con.execute(
                                "INSERT OR REPLACE INTO radar_events VALUES(?,?,?,?,?,?,?)",
                                (mint, pool, sym, int(time.time()), matched["_age_days"], float(matched.get("fdv") or 0), trigger),
                            )
                            con.commit()

                            early_buyers = None
                            if HELIUS_KEY and not a.no_helius:
                                txs = helius_swaps(pool, int(time.time()) - 3600, max_pages=5)
                                early_buyers = aggregate_wallets(txs, mint, sol_usd, a.min_wallet_usd, early_mins=30)

                            notify_radar_alert(matched, trigger=trigger, early_buyers=early_buyers)
                            record_initial_outcome(con, matched, trigger=trigger, early_buyers=early_buyers)

            # 3. Track performance & price checkpoints of all active alerted coins (every 30s)
            if time.time() - last_outcome_check >= 30:
                last_outcome_check = time.time()
                try:
                    update_tracked_outcomes(con)
                except Exception as e:
                    print(f"[Outcomes] Update error: {e}")

        except Exception as e:
            print(f"[Radar] Unexpected loop error: {e}")

        time.sleep(a.interval)


# ----------------------------------------------------------------- commands
def cmd_scan(a):
    con, sol_usd = db(), sol_price()
    while True:
        cands = find_candidates(a)
        print(f"{datetime.now():%H:%M:%S}  {len(cands)} old pools moving; checking dormancy...")
        for row in cands:
            pool = row["id"]
            if con.execute("SELECT 1 FROM events WHERE pool=? AND seen>?", (pool, time.time() - 12 * 3600)).fetchone():
                continue
            try:
                ev = evaluate_pool(pool, a)
                if not (ev["dormant"] and ev["spiked"]):
                    continue
                mint, sym = split_tokens(row)
                if not mint:
                    mint, sym = pool_info(pool)
                process_pool(con, pool, mint, sym, ev, a, sol_usd)
            except Exception as e:
                print(f"  skip {pool}: {e}")
            time.sleep(0.3)
        if not a.loop:
            break
        time.sleep(a.loop)


def cmd_analyze(a):
    con = db()
    ev = evaluate_pool(a.pool, a)
    mint, sym = pool_info(a.pool)
    print(json.dumps({k: v for k, v in ev.items()}, default=str, indent=1))
    process_pool(con, a.pool, mint, sym, ev, a, sol_price())


def leaderboard(con, min_tokens):
    return con.execute(
        """SELECT wallet, COUNT(DISTINCT pool) n, SUM(early) early, SUM(buy) buy, SUM(pnl) pnl
        FROM wallet_events GROUP BY wallet HAVING n >= ? ORDER BY n DESC, pnl DESC""",
        (min_tokens,),
    ).fetchall()


def cmd_wallets(a):
    rows = leaderboard(db(), a.min_tokens)
    print(f"{'wallet':46} {'tokens':>6} {'early':>5} {'total in':>10} {'total pnl':>10}")
    for w, n, e, b, p in rows:
        print(f"{w:46} {n:>6} {e:>5} {b:>10,.0f} {p:>+10,.0f}")


def cmd_watch(a):
    con = db()
    wallets = [r[0] for r in leaderboard(con, a.min_tokens)]
    if a.wallets:
        wallets += [l.strip() for l in open(a.wallets) if l.strip()]
    wallets = sorted(set(wallets))
    print(f"watching {len(wallets)} wallets")
    last, cache = {}, {}
    while True:
        for w in wallets:
            try:
                txs = http_get(
                    f"{HELIUS}/addresses/{w}/transactions",
                    {"api-key": HELIUS_KEY, "type": "SWAP", "limit": 10},
                )
            except Exception as e:
                print("helius error", e)
                continue

            if w in last:
                new = []
                for t in txs:
                    if t["signature"] == last[w]:
                        break
                    new.append(t)
            else:
                new = []

            if txs:
                last[w] = txs[0]["signature"]

            for t in new:
                for tt in t.get("tokenTransfers") or []:
                    m = tt.get("mint")
                    if tt.get("toUserAccount") != w or m in BASE_MINTS:
                        continue
                    if m not in cache or time.time() - cache[m][0] > 600:
                        cache[m] = (time.time(), check_token(m, a))
                    info = cache[m][1]
                    if info and info["dormant"]:
                        info["lookback"] = a.lookback
                        notify_watch_alert(w, m, info["pool"], info)
            time.sleep(0.4)
        time.sleep(a.interval)


def check_token(mint, a):
    try:
        d = paprika(
            f"/networks/{NET}/pools/search",
            token_address=mint,
            order_by="volume_usd_24h",
            sort="desc",
            limit=1,
        )
        res = d.get("results") or []
        if not res:
            return None
        ev = evaluate_pool(res[0]["id"], a)
        ev["pool"] = res[0]["id"]
        return ev
    except Exception:
        return None


def cmd_outcomes(a):
    con = db()
    query = """
        SELECT token, symbol, alerted_at, age_days, entry_mcap, entry_liquidity,
               entry_vol_5m, entry_txns_5m, trigger, has_socials, has_boost,
               early_buyers_count, early_buyers_usd, mult_5m, mult_15m, mult_30m,
               mult_1h, peak_mult, peak_mcap, peak_time_mins, outcome_class, is_finished
        FROM token_outcomes
    """
    params = []
    if getattr(a, "min_mult", None):
        query += " WHERE peak_mult >= ?"
        params.append(a.min_mult)
    query += " ORDER BY alerted_at DESC LIMIT ?"
    params.append(getattr(a, "limit", 50))

    rows = con.execute(query, params).fetchall()
    if not rows:
        print("No tracked token outcomes found yet. Run `radar` or `scan` to start tracking tokens.")
        return

    print("=" * 110)
    print(f"{'TIME':12} {'SYMBOL':10} {'AGE':>6} {'ENTRY MCAP':>11} {'LIQ':>8} {'5m VOL':>8} {'TRIGGER':16} {'PEAK':>7} {'TIME':>6} {'OUTCOME':14}")
    print("-" * 110)

    for r in rows:
        t_str = datetime.fromtimestamp(r[2], timezone.utc).strftime("%m-%d %H:%M")
        sym = (r[1] or "UNKNOWN")[:9]
        age = f"{r[3]:.0f}d"
        mcap = f"${r[4]:,.0f}" if r[4] else "-"
        liq = f"${r[5]:,.0f}" if r[5] else "-"
        vol5 = f"${r[6]:,.0f}" if r[6] else "-"
        trig = (r[8] or "")[:15]
        peak = f"{r[17]:.1f}x" if r[17] else "-"
        pk_time = f"{r[19]:.0f}m" if r[19] else "-"
        outcome = r[20] or "ACTIVE"

        print(f"{t_str:12} {sym:10} {age:>6} {mcap:>11} {liq:>8} {vol5:>8} {trig:16} {peak:>7} {pk_time:>6} {outcome:14}")

    print("=" * 110)

    # Statistical Breakdown of Runners vs Dumpers
    all_tokens = con.execute("""
        SELECT outcome_class, peak_mult, peak_time_mins, entry_liquidity, entry_vol_5m,
               entry_mcap, has_socials, has_boost, early_buyers_count, early_buyers_usd
        FROM token_outcomes
    """).fetchall()

    runners = [x for x in all_tokens if (x[1] or 0) >= 3.0]
    modest = [x for x in all_tokens if 1.5 <= (x[1] or 0) < 3.0]
    dumpers = [x for x in all_tokens if (x[1] or 0) < 1.5 and x[0] != "ACTIVE"]

    n_tot = len(all_tokens)
    print("\n" + "=" * 70)
    print(f"OUTCOME PATTERN ANALYSIS ({n_tot} Total Tokens Tracked)")
    print("=" * 70)
    print(f"* Big Runners (>= 3.0x):     {len(runners):>3} ({len(runners)/max(n_tot,1):>5.1%}) | Avg Peak: {statistics.mean([x[1] for x in runners]):.1f}x" if runners else "* Big Runners (>= 3.0x):       0")
    print(f"* Modest Runs (1.5x - 3.0x): {len(modest):>3} ({len(modest)/max(n_tot,1):>5.1%}) | Avg Peak: {statistics.mean([x[1] for x in modest]):.1f}x" if modest else "* Modest Runs (1.5x - 3.0x):   0")
    print(f"* Immediate Dumps (< 1.5x):  {len(dumpers):>3} ({len(dumpers)/max(n_tot,1):>5.1%}) | Avg Peak: {statistics.mean([x[1] for x in dumpers]):.1f}x" if dumpers else "* Immediate Dumps (< 1.5x):    0")

    if runners and dumpers:
        avg_liq_run = statistics.mean([x[3] for x in runners if x[3]]) if any(x[3] for x in runners) else 0
        avg_liq_dmp = statistics.mean([x[3] for x in dumpers if x[3]]) if any(x[3] for x in dumpers) else 0

        avg_vol_run = statistics.mean([x[4] for x in runners if x[4]]) if any(x[4] for x in runners) else 0
        avg_vol_dmp = statistics.mean([x[4] for x in dumpers if x[4]]) if any(x[4] for x in dumpers) else 0

        soc_run = statistics.mean([x[6] for x in runners]) * 100
        soc_dmp = statistics.mean([x[6] for x in dumpers]) * 100

        pk_time_run = statistics.mean([x[2] for x in runners if x[2]]) if any(x[2] for x in runners) else 0

        print("\nKEY COMPARATIVE METRICS:")
        print(f"{'Metric':<25} {'Runners (>=3x)':<20} {'Immediate Dumpers (<1.5x)':<25}")
        print("-" * 70)
        print(f"{'Avg Entry Liquidity':<25} ${avg_liq_run:>10,.0f}         ${avg_liq_dmp:>10,.0f}")
        print(f"{'Avg 5m Volume':<25} ${avg_vol_run:>10,.0f}         ${avg_vol_dmp:>10,.0f}")
        print(f"{'Social Presence %':<25} {soc_run:>10.1f}%         {soc_dmp:>10.1f}%")
        print(f"{'Avg Time to Peak':<25} {pk_time_run:>10.1f} mins       Immediate / <5 mins")
        print("=" * 70)

    # Export to CSV if requested
    csv_file = getattr(a, "csv", None)
    if csv_file:
        import csv
        headers = [
            "token", "symbol", "alerted_at", "age_days", "entry_mcap", "entry_liquidity",
            "entry_vol_5m", "entry_txns_5m", "trigger", "has_socials", "has_boost",
            "early_buyers_count", "early_buyers_usd", "mult_5m", "mult_15m", "mult_30m",
            "mult_1h", "peak_mult", "peak_mcap", "peak_time_mins", "outcome_class", "is_finished"
        ]
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in rows:
                writer.writerow(r)
        print(f"\n[Export] Saved {len(rows)} rows to {csv_file}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--lookback", type=int, default=30, help="days used to judge dormancy (default: 30)")
        sp.add_argument("--hours", type=int, default=72, help="recent window used to look for the pump")
        sp.add_argument("--max-active-frac", type=float, default=0.15, help="dormant if <= this share of prior days had trades")
        sp.add_argument("--spike-mult", type=float, default=1.35, help="peak must exceed baseline price by this multiple (default: 1.35x)")
        sp.add_argument("--dumped-below", type=float, default=0.5, help="DUMPED if price <= this share of peak")
        sp.add_argument("--min-wallet-usd", type=float, default=500.0, help="minimum USD buy to profile wallet (default: 500)")
        sp.add_argument("--early-mins", type=int, default=30, help="wallets buying within this of the first big buy are EARLY")
        sp.add_argument("--max-pages", type=int, default=30, help="Helius pages (100 swaps each) per pool")
        sp.add_argument("--top", type=int, default=15)
        sp.add_argument("--no-wallets", action="store_true")

    # 1. RADAR: DexScreener & DexPaprika early wake-up stream (RECOMMENDED)
    r = sub.add_parser("radar", help="Real-time 5m wake-up stream for dormant tokens (zero Helius credits)")
    r.add_argument("--min-age", type=int, default=60, help="minimum pool age in days (default: 60 = 2 months)")
    r.add_argument("--max-mcap", type=float, default=75000, help="maximum market cap USD (default: 75,000)")
    r.add_argument("--max-liquidity", type=float, default=25000, help="maximum liquidity USD (default: 25,000)")
    r.add_argument("--min-change-5m", type=float, default=10.0, help="min 5m price change percent (default: 10.0)")
    r.add_argument("--min-change", type=float, default=25.0, help="1h price change percent for sweep (default: 25.0)")
    r.add_argument("--min-vol-h1", type=float, default=250, help="min 1h volume (default: 250)")
    r.add_argument("--min-vol-m5", type=float, default=100, help="min 5m volume (default: 100)")
    r.add_argument("--min-wallet-usd", type=float, default=500.0, help="minimum USD buy to profile early buyers")
    r.add_argument("--min-txns", type=int, default=1, help="minimum transactions (default: 1)")
    r.add_argument("--min-vol", type=float, default=100, help="minimum 24h volume (default: 100)")
    r.add_argument("--pages", type=int, default=2)
    r.add_argument("--interval", type=int, default=12, help="polling interval in seconds (default: 12)")
    r.add_argument("--no-helius", action="store_true", help="skip Helius buyer profiling to preserve completely free operation")
    r.set_defaults(f=cmd_radar)

    # 2. SCAN: DexPaprika candidate screener
    s = sub.add_parser("scan"); common(s)
    s.add_argument("--min-age", type=int, default=30, help="pool age in days (default: 30)")
    s.add_argument("--min-change-5m", type=float, default=10.0, help="min 5m price change percent (default: 10.0)")
    s.add_argument("--min-change", type=float, default=25.0, help="min 1h price change percent (default: 25.0)")
    s.add_argument("--min-txns", type=int, default=1, help="minimum 24h transactions (default: 1)")
    s.add_argument("--min-vol", type=float, default=100, help="minimum 24h volume USD (default: 100)")
    s.add_argument("--pages", type=int, default=3)
    s.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    s.set_defaults(f=cmd_scan)

    # 3. ANALYZE
    s = sub.add_parser("analyze"); common(s); s.add_argument("pool"); s.set_defaults(f=cmd_analyze)

    # 4. WALLETS
    s = sub.add_parser("wallets"); s.add_argument("--min-tokens", type=int, default=2); s.set_defaults(f=cmd_wallets)

    # 5. WATCH: Helius wallet poller
    s = sub.add_parser("watch"); common(s)
    s.add_argument("--min-tokens", type=int, default=2)
    s.add_argument("--wallets", help="file with extra wallet addresses, one per line")
    s.add_argument("--interval", type=int, default=20)
    s.set_defaults(f=cmd_watch)

    # 6. OUTCOMES: Historical pattern analyzer and performance tracker
    s = sub.add_parser("outcomes", help="Analyze what happened to coins found by the scanner (runners vs dumpers)")
    s.add_argument("--limit", type=int, default=50, help="maximum tokens to display (default: 50)")
    s.add_argument("--min-mult", type=float, help="filter by minimum peak multiple (e.g. 2.0)")
    s.add_argument("--csv", help="export complete outcome dataset to CSV file (e.g. outcomes.csv)")
    s.set_defaults(f=cmd_outcomes)

    a = p.parse_args()
    if a.cmd in ("watch",) and not HELIUS_KEY:
        p.error("HELIUS_API_KEY is required for watch")
    a.f(a)


if __name__ == "__main__":
    main()

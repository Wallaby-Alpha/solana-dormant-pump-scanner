#!/usr/bin/env python3
"""
dormant_pump_scanner.py
Detects "dead coin wakes up -> pump -> dump" setups on Solana and profiles the wallets behind them.

  scan      screen old pools that are suddenly ripping, confirm dormancy, profile wallets
  analyze   run the full check on one pool address (good for backtesting known examples)
  wallets   leaderboard of wallets that repeat the playbook across different tokens
  watch     poll the wallets on that leaderboard and alert when one buys a dormant coin

Env:  HELIUS_API_KEY (required for wallet profiling / watch)
      TELEGRAM_BOT_TOKEN (optional; for Telegram alerts)
      TELEGRAM_CHAT_ID   (optional; target chat/channel for Telegram alerts)
      DISCORD_WEBHOOK    (optional; alert target for Discord)
      DEXPAPRIKA_API_KEY (optional; switches to api-pro host)
Deps: pip install -r requirements.txt
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


# ----------------------------------------------------------------- dexpaprika
def sol_price():
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
    """Old pools whose price is ripping right now (server-side filter)."""
    found = {}
    for field in ("price_change_percentage_1h_min", "price_change_percentage_6h_min"):
        cursor = None
        for _ in range(a.pages):
            p = {
                "created_before": f"-{a.min_age}d",
                "txns_24h_min": a.min_txns,
                "volume_usd_24h_min": a.min_vol,
                field: a.min_change,
                "order_by": field.replace("_min", ""),
                "sort": "desc",
                "limit": 50,
            }
            if cursor:
                p["cursor"] = cursor
            d = paprika(f"/networks/{NET}/pools/search", **p)
            for row in d.get("results", []):
                found[row["id"]] = row
            cursor = d.get("next_cursor")
            if not d.get("has_next_page"):
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
    except Forbidden:  # free plans cap history at 30d
        daily = get_ohlcv(pool, "-30d", "24h", 31)
        a.lookback = 30
    hourly = get_ohlcv(pool, f"-{a.hours}h", "1h", a.hours + 1)
    return analyze_candles(daily, hourly, a, now)


# ----------------------------------------------------------------- helius
def helius_swaps(addr, since_ts, max_pages, limit=100):
    out, before = [], None
    for _ in range(max_pages):
        p = {"api-key": HELIUS_KEY, "type": "SWAP", "limit": limit}
        if before:
            p["before"] = before
        batch = http_get(f"{HELIUS}/addresses/{addr}/transactions", p)
        if not batch:
            break
        out.extend(batch)
        before = batch[-1]["signature"]
        if batch[-1]["timestamp"] < since_ts:
            break
    return [t for t in out if t["timestamp"] >= since_ts]


def parse_swap(tx, mint):
    """Net flows for the fee payer: (payer, token_delta, sol_delta, stable_delta)."""
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


def notify_watch_alert(wallet, mint, pool, info, buy_usd=None):
    plain = (
        f"{wallet} bought {mint} (pool {pool}), dormant coin: only {info['active_frac']:.0%} of prior days "
        f"had trades; now {info['mult']:.1f}x baseline"
    )
    print("\n*** ALERT ***", plain)

    buy_str = f"<b>Buy:</b> ~${buy_usd:,.0f}\n" if buy_usd else ""
    tg_text = (
        f"🚨 <b>Dormant Coin Wake-Up Alert</b>\n\n"
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
        sell REAL, pnl REAL, exit_frac REAL, early INT, PRIMARY KEY(wallet, pool));"""
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
    if not HELIUS_KEY or a.no_wallets:
        return
    txs = helius_swaps(pool, ev["start"] - 3600, a.max_pages)
    rows = aggregate_wallets(txs, mint, sol_usd, a.min_wallet_usd, a.early_mins)
    print(f"   {len(txs)} swaps fetched, {len(rows)} wallets bought >= ${a.min_wallet_usd:,.0f} as their first action")
    for r in rows[:a.top]:
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

            # Poll fix: Stop at previously seen signature to prevent re-processing old txs
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--lookback", type=int, default=30, help="days used to judge dormancy (default: 30)")
        sp.add_argument("--hours", type=int, default=72, help="recent window used to look for the pump")
        sp.add_argument("--max-active-frac", type=float, default=0.15, help="dormant if <= this share of prior days had trades")
        sp.add_argument("--spike-mult", type=float, default=5, help="peak must exceed baseline price by this multiple")
        sp.add_argument("--dumped-below", type=float, default=0.5, help="DUMPED if price <= this share of peak")
        sp.add_argument("--min-wallet-usd", type=float, default=500.0, help="minimum USD buy to profile wallet (default: 500)")
        sp.add_argument("--early-mins", type=int, default=30, help="wallets buying within this of the first big buy are EARLY")
        sp.add_argument("--max-pages", type=int, default=30, help="Helius pages (100 swaps each) per pool")
        sp.add_argument("--top", type=int, default=15)
        sp.add_argument("--no-wallets", action="store_true")

    s = sub.add_parser("scan"); common(s)
    s.add_argument("--min-age", type=int, default=30, help="pool age in days (default: 30)")
    s.add_argument("--min-change", type=float, default=100, help="1h/6h price change %% to even consider")
    s.add_argument("--min-txns", type=int, default=20)
    s.add_argument("--min-vol", type=float, default=2000)
    s.add_argument("--pages", type=int, default=3)
    s.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    s.set_defaults(f=cmd_scan)
    s = sub.add_parser("analyze"); common(s); s.add_argument("pool"); s.set_defaults(f=cmd_analyze)
    s = sub.add_parser("wallets"); s.add_argument("--min-tokens", type=int, default=2); s.set_defaults(f=cmd_wallets)
    s = sub.add_parser("watch"); common(s)
    s.add_argument("--min-tokens", type=int, default=2)
    s.add_argument("--wallets", help="file with extra wallet addresses, one per line")
    s.add_argument("--interval", type=int, default=20)
    s.set_defaults(f=cmd_watch)
    a = p.parse_args()
    if a.cmd in ("watch",) and not HELIUS_KEY:
        p.error("HELIUS_API_KEY is required")
    a.f(a)


if __name__ == "__main__":
    main()

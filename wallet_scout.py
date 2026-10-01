#!/usr/bin/env python3
"""
wallet_scout.py — rank Polymarket wallets by how COPYABLE their edge is.
=======================================================================
A leaderboard ranks wallets by profit, which mostly measures size and luck.
This script asks four harder questions about each candidate:

  1. IS THE EDGE REAL?   Bootstrap the wallet's closed positions 2000 times
                         and keep the 5th-percentile ROI (roi_lo). A wallet
                         whose profit could be one or two lucky bets gets a
                         roi_lo at or below zero.
  2. IS IT LUCK?         top3_share = share of profit from the 3 best bets;
                         roi_ex_top3 = ROI with those 3 removed.
  3. DOES IT PERSIST?    Split positions in time: ROI on the older half vs
                         the newer half, plus % of profitable months.
                         Picking on the past and checking the future is the
                         only test of whether copying would have worked.
  4. CAN YOU COPY IT?    For recent BUYs, read the price 60s / 300s after the
                         trade (CLOB price history). A follower pays that
                         later price, so ROI is re-estimated as
                             copy_roi = (1 + roi) * p_theirs / p_follower - 1
                         Wallets that hold both sides of a market (market
                         makers / hedgers) are flagged: copying one leg of a
                         hedge is not copying their strategy.

Losing positions that were never redeemed sit in /positions (redeemable,
curPrice 0), not in /closed-positions, so they are pulled in too. Leaving
them out makes every wallet look better than it is.

Read-only, public endpoints, no key.

    python3 wallet_scout.py                 # scan leaderboard, ~150 wallets
    python3 wallet_scout.py --pool 60       # smaller / faster
    python3 wallet_scout.py --wallets 0xabc,0xdef   # score specific wallets

Outputs: scout_report.txt (paste-able), scout_wallets.csv (all metrics).
"""

import argparse
import csv
import random
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

MAX_CLOSED = 1500        # newest closed positions per wallet (30 pages)
BOOTSTRAP = 2000
MIN_POSITIONS = 30       # fewer than this and no statistic means anything
MIN_MONTHS = 3
MAX_TOP3_SHARE = 0.60
SLIP_SAMPLES = 15        # recent buys per finalist checked for price drift
FINALISTS = 25

session = requests.Session()


def get(path, base=DATA_API, **params):
    for attempt in range(6):
        try:
            r = session.get(base + path, params=params, timeout=25)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                return None
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(1.5 * (attempt + 1))
    return None


# ── candidates ──────────────────────────────────────────────────────────
def leaderboard_pool(size):
    """Union of all-time, monthly and weekly PnL leaderboards."""
    seen = {}
    per_period = max(50, size // 2)
    for period in ("ALL", "MONTH", "WEEK"):
        for offset in range(0, per_period, 50):
            rows = get("/v1/leaderboard", timePeriod=period, orderBy="PNL",
                       limit=50, offset=offset) or []
            for r in rows:
                w = r.get("proxyWallet")
                if w and w not in seen:
                    seen[w] = r.get("userName") or ""
            if len(rows) < 50:
                break
    return list(seen.items())[:size]


# ── per-wallet data ─────────────────────────────────────────────────────
def fetch_positions(wallet):
    """Return (resolved, open) lists of {ts, cost, pnl, entry, cid, side}."""
    resolved, open_ = [], []
    capped = False
    for offset in range(0, MAX_CLOSED, 50):
        rows = get("/closed-positions", user=wallet, limit=50, offset=offset,
                   sortBy="TIMESTAMP", sortDirection="DESC")
        if not rows:
            break
        for p in rows:
            entry = float(p.get("avgPrice") or 0)
            cost = entry * float(p.get("totalBought") or 0)
            if cost <= 0:
                continue
            resolved.append({"ts": int(p.get("timestamp") or 0), "cost": cost,
                             "pnl": float(p.get("realizedPnl") or 0),
                             "entry": entry, "cid": p.get("conditionId"),
                             "side": p.get("outcomeIndex")})
        if len(rows) < 50:
            break
    else:
        capped = True

    # /closed-positions is capped to the newest MAX_CLOSED rows but
    # /positions holds unredeemed losers from all time; mixing the two
    # windows charges old losses against only recent wins. Keep both
    # sources on the same window and drop overlaps.
    window_start = min((r["ts"] for r in resolved), default=0) if capped else 0
    seen = {(r["cid"], r["side"]) for r in resolved}

    offset = 0
    while offset < 5000:
        rows = get("/positions", user=wallet, limit=500, offset=offset,
                   sizeThreshold=0)
        if not rows:
            break
        for p in rows:
            cost = float(p.get("initialValue") or 0)
            if cost <= 0:
                continue
            rec = {"ts": 0, "cost": cost, "pnl": float(p.get("cashPnl") or 0),
                   "entry": float(p.get("avgPrice") or 0),
                   "cid": p.get("conditionId"), "side": p.get("outcomeIndex")}
            cur = float(p.get("curPrice") or 0)
            # resolved-but-unredeemed: the market is over, the money is gone
            # (or won and simply not claimed yet)
            if p.get("redeemable") or cur <= 0.001 or cur >= 0.999:
                rec["ts"] = _date_ts(p.get("endDate"))
                if (rec["cid"], rec["side"]) not in seen and rec["ts"] >= window_start:
                    resolved.append(rec)
            else:
                open_.append(rec)
        if len(rows) < 500:
            break
        offset += 500
    return resolved, open_


def _date_ts(s):
    try:
        return int(datetime.fromisoformat(str(s)[:10]).replace(
            tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


# ── statistics ──────────────────────────────────────────────────────────
def roi(rows):
    c = sum(r["cost"] for r in rows)
    return sum(r["pnl"] for r in rows) / c if c else 0.0


def bootstrap_lo(rows, seed, q=0.05, n=BOOTSTRAP):
    rng = random.Random(seed)       # per-wallet seed: reruns give the same number
    k = len(rows)
    sims = sorted(roi([rows[rng.randrange(k)] for _ in range(k)])
                  for _ in range(n))
    return sims[int(q * n)]


def score_wallet(wallet, name):
    resolved, open_ = fetch_positions(wallet)
    m = {"wallet": wallet, "name": name, "n": len(resolved)}
    if len(resolved) < MIN_POSITIONS:
        m["verdict"] = f"too few positions ({len(resolved)})"
        return m

    pnl = sum(r["pnl"] for r in resolved)
    by_pnl = sorted(resolved, key=lambda r: r["pnl"], reverse=True)
    dated = sorted((r for r in resolved if r["ts"]), key=lambda r: r["ts"])
    half = len(dated) // 2
    months = defaultdict(float)
    for r in dated:
        months[datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m")] += r["pnl"]
    sides = defaultdict(set)
    for r in resolved + open_:
        sides[r["cid"]].add(r["side"])

    m.update({
        "pnl": pnl,
        "cost": sum(r["cost"] for r in resolved),
        "roi": roi(resolved),
        "roi_lo": bootstrap_lo(resolved, int(wallet, 16)),
        "win_rate": sum(r["pnl"] > 0 for r in resolved) / len(resolved),
        "top3_share": (sum(r["pnl"] for r in by_pnl[:3]) / pnl) if pnl > 0 else 1.0,
        "roi_ex_top3": roi(by_pnl[3:]),
        "roi_old": roi(dated[:half]) if half >= 10 else None,
        "roi_new": roi(dated[half:]) if half >= 10 else None,
        "months": len(months),
        "pos_months": (sum(v > 0 for v in months.values()) / len(months)) if months else 0,
        "avg_entry": statistics.mean(r["entry"] for r in resolved),
        "both_sides": sum(len(s) > 1 for s in sides.values()) / max(1, len(sides)),
        "open_cost": sum(r["cost"] for r in open_),
        "open_pnl": sum(r["pnl"] for r in open_),
    })

    fails = []
    if m["roi_lo"] <= 0:
        fails.append("edge not significant")
    if m["top3_share"] > MAX_TOP3_SHARE:
        fails.append("profit from a few bets")
    if m["months"] < MIN_MONTHS:
        fails.append("short history")
    if m["roi_old"] is not None and (m["roi_old"] <= 0 or m["roi_new"] <= 0):
        fails.append("not persistent")
    if m["both_sides"] > 0.25:
        fails.append("hedger/market maker")
    m["verdict"] = "; ".join(fails) or "PASS"
    return m


def copy_check(m):
    """Price drift after the wallet's recent BUYs = what a follower pays."""
    trades = get("/activity", user=m["wallet"], type="TRADE", limit=500,
                 sortBy="TIMESTAMP", sortDirection="DESC") or []
    if len(trades) >= 2:
        span_days = max(1e-9, (trades[0]["timestamp"] - trades[-1]["timestamp"]) / 86400)
        m["trades_per_day"] = len(trades) / span_days
        m["median_usd"] = statistics.median(float(t.get("usdcSize") or 0) for t in trades)
    if trades:
        m["days_since_trade"] = (time.time() - trades[0]["timestamp"]) / 86400
    buys = [t for t in trades if t.get("side") == "BUY"
            and 0.01 < float(t.get("price") or 0) < 0.99][:SLIP_SAMPLES * 2]
    ratios = {60: [], 300: []}
    for t in buys[:SLIP_SAMPLES]:
        ts, p0 = int(t["timestamp"]), float(t["price"])
        hist = (get("/prices-history", base=CLOB, market=t["asset"],
                    startTs=ts, endTs=ts + 900, fidelity=1) or {}).get("history") or []
        for lag in ratios:
            later = [h["p"] for h in hist if h["t"] >= ts + lag]
            if later and later[0] > 0:
                ratios[lag].append(p0 / later[0])   # <1 means follower pays more
    for lag, rs in ratios.items():
        if rs:
            r = statistics.median(rs)
            m[f"copy_roi_{lag}s"] = (1 + m["roi"]) * r - 1
    return m


# ── report ──────────────────────────────────────────────────────────────
def pct(x):
    return "   n/a" if x is None else f"{100 * x:+6.1f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=150)
    ap.add_argument("--wallets", help="comma-separated wallets to score")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    if args.wallets:
        pool = [(w.strip(), "") for w in args.wallets.split(",") if w.strip()]
    else:
        print("building candidate pool from leaderboards...")
        pool = leaderboard_pool(args.pool)
    print(f"scoring {len(pool)} wallets...")

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(lambda wn: score_wallet(*wn), pool))

    scored = [m for m in results if "roi_lo" in m]
    scored.sort(key=lambda m: (m["verdict"] == "PASS", m["roi_lo"]), reverse=True)
    finalists = scored[:FINALISTS]
    print(f"copy-checking top {len(finalists)}...")
    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(copy_check, finalists))

    cols = ["wallet", "name", "verdict", "n", "pnl", "cost", "roi", "roi_lo",
            "win_rate", "top3_share", "roi_ex_top3", "roi_old", "roi_new",
            "months", "pos_months", "avg_entry", "both_sides", "open_cost",
            "open_pnl", "trades_per_day", "median_usd", "days_since_trade", "copy_roi_60s",
            "copy_roi_300s"]
    with open("scout_wallets.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    lines = [f"wallet_scout  {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
             f"pool={len(pool)} scored={len(scored)} "
             f"pass={sum(m['verdict'] == 'PASS' for m in scored)}", "",
             f"{'name':<18}{'n':>5}{'pnl$':>11}{'roi':>8}{'roi_lo':>8}"
             f"{'old':>8}{'new':>8}{'top3':>6}{'+mo':>5}{'copy60':>8}"
             f"{'copy300':>8}  verdict"]
    for m in finalists:
        lines.append(
            f"{(m['name'] or m['wallet'][:10])[:17]:<18}{m['n']:>5}"
            f"{m['pnl']:>11,.0f}{pct(m['roi'])}{pct(m['roi_lo'])}"
            f"{pct(m['roi_old'])}{pct(m['roi_new'])}{min(m['top3_share'], 9.99):>6.2f}"
            f"{100 * m['pos_months']:>4.0f}%{pct(m.get('copy_roi_60s'))}"
            f"{pct(m.get('copy_roi_300s'))}  {m['verdict']}")
    lines += ["", "wallets (finalists):"] + [f"  {m['name'] or '-':<20} {m['wallet']}"
                                          for m in finalists]
    open("scout_report.txt", "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

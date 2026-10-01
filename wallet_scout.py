#!/usr/bin/env python3
"""
wallet_scout.py — rank Polymarket wallets by how COPYABLE their edge is.
=======================================================================
A leaderboard ranks wallets by profit, which mostly measures size and luck.
This script asks harder questions about each candidate:

  1. IS THE EDGE REAL?   Bootstrap the wallet's resolved positions 2000 times
                         and keep the 5th-percentile ROI (roi_lo). Positions
                         on the same EVENT are resampled together: four
                         over/under lines on one game win or lose together,
                         so counting them as four coin flips overstates
                         confidence.
  2. IS IT LUCK?         top3_share = share of profit from the 3 best bets.
  3. DOES IT PERSIST?    ROI on the older half vs the newer half of positions,
                         plus % of profitable months.
  4. CAN YOU COPY IT?    a) taker_share = fees the wallet actually paid /
                            fees it would have paid buying with market
                            orders. Fee = shares * rate * p * (1-p), rate read
                            per market (docs.polymarket.com/trading/fees).
                            ~0 means it rests limit orders (maker): it gets
                            prices a copier, who must take, never sees.
                         b) Price 60s / 300s after its recent buys (one per
                            market) = what a late follower pays, plus the
                            follower's own taker fee:
                              copy_roi = (1+roi) * p_theirs/p_follower
                                         / (1 + fee_frac) - 1

The wallet's own PnL excludes its fees (closed-position realizedPnl equals
shares*(1-p) on wins and -shares*p on losses exactly), so roi is gross and
the follower's fee is charged once, in copy_roi.

Losing positions that were never redeemed sit in /positions (redeemable,
curPrice 0), not /closed-positions, so they are pulled in on the same time
window as the closed history.

BACKTEST (--asof YYYY-MM-DD): score every wallet using only positions
resolved before that date, then measure what PASS wallets and all other
scored wallets did AFTER it, gross and net of taker fees. If passing the
filter does not predict a better future, the filter is worthless.

Read-only, public endpoints, no key.

    python3 wallet_scout.py                       # scan PnL leaderboards
    python3 wallet_scout.py --wallets 0xabc,0xdef # score specific wallets
    python3 wallet_scout.py --asof 2026-04-01,2026-06-01 --order-by VOL   # backtest

Outputs: scout_report.txt, scout_wallets.csv  (backtest_report.txt/backtest_wallets.csv for --asof)
"""

import argparse
import csv
import random
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

MAX_CLOSED = 1500        # newest closed positions per wallet (30 pages)
BOOTSTRAP = 2000
MIN_POSITIONS = 30       # fewer than this and no statistic means anything
MIN_MONTHS = 3
MAX_TOP3_SHARE = 0.60
MAX_BOTH_SIDES = 0.25    # share of markets where it held YES and NO
MIN_TAKER_SHARE = 0.30   # below this the wallet is mostly a maker
MIN_FEE_EVIDENCE = 20.0  # $ of expected taker fees needed to judge taker_share
MIN_FEE_POSITIONS = 5    # ...spread over at least this many fee-charging positions
FEE_RECENT_DAYS = 45     # only recent markets: a market's fee flag is read as of
                         # TODAY, so an old position may predate its fees
FEE_SAMPLE = 100         # biggest recent /positions rows used for taker_share
SLIP_MARKETS = 30        # distinct recent markets checked for price drift
FINALISTS = 25
MAX_IDLE_DAYS = 21       # a wallet that stopped trading cannot be copied

_local = threading.local()


def session():
    # requests.Session is not guaranteed thread-safe: one per worker thread
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def get(path, base=DATA_API, params=None, **kw):
    """JSON body, or None on error. An empty list is a real empty page."""
    for attempt in range(6):
        try:
            r = session().get(base + path, params=params or kw, timeout=25)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                return None
            return r.json()
        except (requests.RequestException, ValueError):
            time.sleep(1.5 * (attempt + 1))
    return None


# ── market fee parameters (Gamma) ───────────────────────────────────────
_fee_cache = {}
_fee_lock = threading.Lock()


def fee_rates(cids):
    """{conditionId: taker fee rate} (0.0 if fees disabled). Unknown ids
    are left out. Gamma only returns closed markets when closed=true, so
    each batch is asked both ways."""
    with _fee_lock:
        todo = [c for c in dict.fromkeys(cids) if c and c not in _fee_cache]
    for i in range(0, len(todo), 20):
        batch = [("condition_ids", c) for c in todo[i:i + 20]]
        for closed in ("true", "false"):
            rows = get("/markets", base=GAMMA,
                       params=batch + [("closed", closed), ("limit", 50)]) or []
            with _fee_lock:
                for m in rows:
                    sched = m.get("feeSchedule") or {}
                    _fee_cache[m.get("conditionId")] = (
                        float(sched.get("rate") or 0) if m.get("feesEnabled") else 0.0)
    with _fee_lock:
        return {c: _fee_cache[c] for c in cids if c in _fee_cache}


def fee_frac(rate, p):
    """Taker fee as a fraction of money spent: shares*rate*p*(1-p) / (shares*p)."""
    return rate * (1 - p)


def fee_drag(rows):
    """Cost-weighted follower fee fraction over rows that have a known rate."""
    rates = fee_rates([r["cid"] for r in rows])
    known = [r for r in rows if r["cid"] in rates]
    cost = sum(r["cost"] for r in known)
    if not cost:
        return None
    return sum(fee_frac(rates[r["cid"]], r["entry"]) * r["cost"] for r in known) / cost


# ── candidates ──────────────────────────────────────────────────────────
def leaderboard_pool(size, order_by="PNL", periods=("ALL", "MONTH", "WEEK")):
    seen = {}
    per_period = max(50, size // len(periods) + 50)
    for period in periods:
        for offset in range(0, per_period, 50):
            rows = get("/v1/leaderboard", timePeriod=period, orderBy=order_by,
                       limit=50, offset=offset) or []
            for r in rows:
                w = r.get("proxyWallet")
                if w and w not in seen:
                    seen[w] = r.get("userName") or ""
            if len(rows) < 50:
                break
    return list(seen.items())[:size]


# ── per-wallet data ─────────────────────────────────────────────────────
def fetch_positions(wallet, max_closed=MAX_CLOSED):
    """Return (resolved, open_, fee_rows, complete).

    resolved/open_: [{ts, cost, pnl, entry, cid, side, event}]
    fee_rows:       /positions rows as {cid, entry, cost, fee_paid}
    complete:       False if the closed history was cut short (cap or error)
    """
    resolved, open_, fee_rows = [], [], []
    complete = False
    for offset in range(0, max_closed, 50):
        rows = get("/closed-positions", user=wallet, limit=50, offset=offset,
                   sortBy="TIMESTAMP", sortDirection="DESC")
        if rows is None:              # error mid-history: keep what we have,
            break                     # but it is NOT the full history
        for p in rows:
            entry = float(p.get("avgPrice") or 0)
            cost = entry * float(p.get("totalBought") or 0)
            if cost <= 0:
                continue
            resolved.append({"ts": int(p.get("timestamp") or 0), "cost": cost,
                             "pnl": float(p.get("realizedPnl") or 0),
                             "entry": entry, "cid": p.get("conditionId"),
                             "side": p.get("outcomeIndex"),
                             "event": p.get("eventSlug") or p.get("conditionId")})
        if len(rows) < 50:
            complete = True
            break

    # /positions holds unredeemed losers from all time; if the closed list is
    # partial, mixing windows would charge old losses against recent wins.
    window_start = 0 if complete else min((r["ts"] for r in resolved), default=0)
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
                   "cid": p.get("conditionId"), "side": p.get("outcomeIndex"),
                   "event": p.get("eventSlug") or p.get("conditionId")}
            fee_rows.append({"cid": rec["cid"], "entry": rec["entry"],
                             "cost": cost, "end": _date_ts(p.get("endDate")),
                             "fee_paid": float(p.get("entryFeesUsdc") or 0)})
            cur = float(p.get("curPrice") or 0)
            if p.get("redeemable") or cur <= 0.001 or cur >= 0.999:
                rec["ts"] = _date_ts(p.get("endDate"))
                if (rec["cid"], rec["side"]) not in seen and rec["ts"] >= window_start:
                    resolved.append(rec)
            else:
                open_.append(rec)
        if len(rows) < 500:
            break
        offset += 500
    return resolved, open_, fee_rows, complete


def _date_ts(s):
    try:
        return int(datetime.fromisoformat(str(s)[:10]).replace(
            tzinfo=timezone.utc).timestamp())
    except ValueError:
        return 0


def taker_share(fee_rows):
    """Fees actually paid / fees a pure taker would have paid. None if the
    sample holds too little fee-charging volume to judge."""
    cutoff = time.time() - FEE_RECENT_DAYS * 86400
    recent = [r for r in fee_rows if r["end"] >= cutoff]
    sample = sorted(recent, key=lambda r: r["cost"], reverse=True)[:FEE_SAMPLE]
    rates = fee_rates([r["cid"] for r in sample])
    paid = expected = 0.0
    n = 0
    for r in sample:
        rate = rates.get(r["cid"])
        if not rate:
            continue
        n += 1
        paid += r["fee_paid"]
        expected += fee_frac(rate, r["entry"]) * r["cost"]
    if n < MIN_FEE_POSITIONS or expected < MIN_FEE_EVIDENCE:
        return None
    return paid / expected


# ── statistics ──────────────────────────────────────────────────────────
def roi(rows):
    c = sum(r["cost"] for r in rows)
    return sum(r["pnl"] for r in rows) / c if c else 0.0


def bootstrap_lo(rows, seed, q=0.05, n=BOOTSTRAP):
    """5th-percentile ROI, resampling whole events (correlated bets)."""
    rng = random.Random(seed)       # per-wallet seed: reruns give the same number
    by_event = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        by_event[r["event"]][0] += r["cost"]
        by_event[r["event"]][1] += r["pnl"]
    events = list(by_event.values())
    k = len(events)
    sims = []
    for _ in range(n):
        cost = pnl = 0.0
        for _ in range(k):
            c, p = events[rng.randrange(k)]
            cost += c
            pnl += p
        sims.append(pnl / cost if cost else 0.0)
    sims.sort()
    return sims[int(q * n)]


def score(wallet, name, resolved, open_, fee_rows):
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
        "events": len({r["event"] for r in resolved}),
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
        "taker_share": taker_share(fee_rows),
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
    if m["both_sides"] > MAX_BOTH_SIDES:
        fails.append("hedger/market maker")
    if m["taker_share"] is not None and m["taker_share"] < MIN_TAKER_SHARE:
        fails.append("maker (limit orders): not copyable")
    m["verdict"] = "; ".join(fails) or "PASS"
    return m


def score_wallet(wallet, name):
    resolved, open_, fee_rows, complete = fetch_positions(wallet)
    m = score(wallet, name, resolved, open_, fee_rows)
    m["complete"] = complete
    return m


def copy_check(m):
    """Price drift after recent BUYs (one per market) + follower taker fee."""
    trades = get("/activity", user=m["wallet"], type="TRADE", limit=500,
                 sortBy="TIMESTAMP", sortDirection="DESC") or []
    if len(trades) >= 2:
        span_days = max(1e-9, (trades[0]["timestamp"] - trades[-1]["timestamp"]) / 86400)
        m["trades_per_day"] = len(trades) / span_days
        m["median_usd"] = statistics.median(float(t.get("usdcSize") or 0) for t in trades)
    if trades:
        m["days_since_trade"] = (time.time() - trades[0]["timestamp"]) / 86400
    if m.get("days_since_trade", 1e9) > MAX_IDLE_DAYS:
        m["verdict"] = ("inactive" if m["verdict"] == "PASS"
                        else m["verdict"] + "; inactive")

    # one order fills in many pieces: keep each market's earliest recent buy
    first_buy = {}
    for t in trades:                                   # newest first
        if t.get("side") == "BUY" and 0.01 < float(t.get("price") or 0) < 0.99:
            first_buy[t["asset"]] = t                  # ends on the oldest
    buys = sorted(first_buy.values(), key=lambda t: -t["timestamp"])[:SLIP_MARKETS]
    m["slip_markets"] = len(buys)

    ratios = {60: [], 300: []}
    fees = []
    rates = fee_rates([t["conditionId"] for t in buys])
    for t in buys:
        ts, p0 = int(t["timestamp"]), float(t["price"])
        hist = (get("/prices-history", base=CLOB, market=t["asset"],
                    startTs=ts, endTs=ts + 900, fidelity=1) or {}).get("history") or []
        for lag in ratios:
            later = [h["p"] for h in hist if h["t"] >= ts + lag]
            if later and 0 < later[0] < 1:
                ratios[lag].append(p0 / later[0])      # <1: follower pays more
        if t["conditionId"] in rates:
            fees.append(fee_frac(rates[t["conditionId"]], p0))
    m["follower_fee"] = statistics.median(fees) if fees else None
    ff = m["follower_fee"] or 0.0
    for lag, rs in ratios.items():
        if len(rs) >= 5:
            m[f"copy_roi_{lag}s"] = (1 + m["roi"]) * statistics.median(rs) / (1 + ff) - 1
    return m


# ── backtest ────────────────────────────────────────────────────────────
def backtest_wallet(wallet, name, asofs, max_closed):
    """Fetch once, then split at each asof date. Returns [(m, after_rows)]."""
    resolved, _open, fee_rows, complete = fetch_positions(wallet, max_closed)
    oldest = min((r["ts"] for r in resolved if r["ts"]), default=0)
    out = []
    for asof_ts in asofs:
        before = [r for r in resolved if r["ts"] and r["ts"] < asof_ts]
        after = [r for r in resolved if r["ts"] >= asof_ts]
        # the picker may only see the past: no open positions, no current fees
        m = score(wallet, name, before, [], [])
        # newest-first history that was cut short may not reach far enough
        # back to show the before-period in full
        m["before_complete"] = complete or oldest < asof_ts - 90 * 86400
        m["n_after"] = len(after)
        m["cost_after"] = sum(r["cost"] for r in after)
        m["pnl_after"] = sum(r["pnl"] for r in after)
        m["roi_after"] = roi(after) if after else None
        out.append((m, after))
    return out


def group_stats(ms):
    ms = [m for m in ms if m["n_after"] >= 10]
    cost = sum(m["cost_after"] for m in ms)
    pnl = sum(m["pnl_after"] for m in ms)
    rois = [m["roi_after"] for m in ms]
    return {"wallets": len(ms),
            "pooled_roi": pnl / cost if cost else None,
            "median_roi": statistics.median(rois) if rois else None,
            "share_profitable": (sum(r > 0 for r in rois) / len(rois)) if rois else None,
            "pooled_net": (sum(m["pnl_net"] for m in ms) / cost) if cost else None}


def run_backtest(args):
    dates = [d.strip() for d in args.asof.split(",") if d.strip()]
    asofs = [_date_ts(d) for d in dates]
    pool = (leaderboard_pool(args.pool, order_by=args.order_by)
            if not args.wallets else
            [(w.strip(), "") for w in args.wallets.split(",") if w.strip()])
    print(f"backtest {dates}: {len(pool)} wallets (orderBy={args.order_by})")
    with ThreadPoolExecutor(args.workers) as ex:
        per_wallet = list(ex.map(
            lambda wn: backtest_wallet(*wn, asofs, args.max_closed), pool))

    def fmt(x):
        return "   n/a" if x is None else f"{100 * x:+6.1f}%"

    def add_net(ma):
        m, after = ma
        top = sorted(after, key=lambda r: r["cost"], reverse=True)[:300]
        m["fee_after"] = fee_drag(top)
        # a follower pays fee_frac on every dollar staked
        m["pnl_net"] = ((1 + m["roi_after"]) / (1 + (m["fee_after"] or 0)) - 1) * m["cost_after"]
        return m

    lines = [f"walk-forward backtest  pool={len(pool)} (orderBy={args.order_by})  "
             f"run {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
             "Picked using ONLY positions resolved before the date; graded on "
             "positions resolved after it.", ""]
    rows_out = []
    for i, date in enumerate(dates):
        usable = [pw[i] for pw in per_wallet
                  if "roi_lo" in pw[i][0] and pw[i][0]["n_after"] >= 10]
        with ThreadPoolExecutor(args.workers) as ex:
            ms = list(ex.map(add_net, usable))
        for m in ms:
            m["asof"] = date
        rows_out += ms
        passed = [m for m in ms if m["verdict"] == "PASS"]
        failed = [m for m in ms if m["verdict"] != "PASS"]
        lines += [f"== asof {date}: {len(ms)} wallets with >=30 positions before "
                  f"and >=10 after",
                  f"{'group':<8}{'wallets':>8}{'pooled':>9}{'net fee':>9}"
                  f"{'median':>9}{'%prof':>7}"]
        for label, g in (("PASS", group_stats(passed)), ("FAIL", group_stats(failed)),
                         ("ALL", group_stats(ms))):
            prof = ("   n/a" if g["share_profitable"] is None
                    else f"{100 * g['share_profitable']:6.0f}%")
            lines.append(f"{label:<8}{g['wallets']:>8}{fmt(g['pooled_roi'])}  "
                         f"{fmt(g['pooled_net'])}{fmt(g['median_roi'])}{prof}")
        for m in sorted(passed, key=lambda m: -m["roi_lo"]):
            lines.append(
                f"   {(m['name'] or m['wallet'][:10])[:18]:<19} before roi "
                f"{fmt(m['roi'])} lo {fmt(m['roi_lo'])} | after n={m['n_after']:<4} "
                f"roi {fmt(m['roi_after'])} net {fmt(m['pnl_net'] / m['cost_after'])}"
                f"{'' if m['before_complete'] else '  (partial before-history)'}")
        lines.append("")

    open("backtest_report.txt", "w").write("\n".join(lines) + "\n")
    cols = ["asof", "wallet", "name", "verdict", "n", "roi", "roi_lo", "top3_share",
            "months", "before_complete", "n_after", "cost_after", "pnl_after",
            "roi_after", "fee_after", "pnl_net"]
    with open("backtest_wallets.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows_out)
    print("\n".join(lines))


# ── report ──────────────────────────────────────────────────────────────
def pct(x):
    return "   n/a" if x is None else f"{100 * x:+6.1f}%"


def run_scan(args):
    if args.wallets:
        pool = [(w.strip(), "") for w in args.wallets.split(",") if w.strip()]
    else:
        print("building candidate pool from leaderboards...")
        pool = leaderboard_pool(args.pool, order_by=args.order_by)
    print(f"scoring {len(pool)} wallets...")

    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(lambda wn: score_wallet(*wn), pool))

    scored = [m for m in results if "roi_lo" in m]
    scored.sort(key=lambda m: (m["verdict"] == "PASS", m["roi_lo"]), reverse=True)
    finalists = scored[:FINALISTS]
    print(f"copy-checking top {len(finalists)}...")
    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(copy_check, finalists))

    cols = ["wallet", "name", "verdict", "complete", "n", "events", "pnl", "cost",
            "roi", "roi_lo", "win_rate", "top3_share", "roi_ex_top3", "roi_old",
            "roi_new", "months", "pos_months", "avg_entry", "both_sides",
            "taker_share", "open_cost", "open_pnl", "trades_per_day", "median_usd",
            "days_since_trade", "slip_markets", "follower_fee", "copy_roi_60s",
            "copy_roi_300s"]
    with open("scout_wallets.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)

    def ts(x):
        return "  n/a" if x is None else f"{x:5.2f}"
    lines = [f"wallet_scout  {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC  "
             f"pool={len(pool)} scored={len(scored)} "
             f"pass={sum(m['verdict'] == 'PASS' for m in scored)}", "",
             f"{'name':<18}{'n':>5}{'pnl$':>11}{'roi':>8}{'roi_lo':>8}"
             f"{'old':>8}{'new':>8}{'top3':>6}{'+mo':>5}{'taker':>6}{'fee':>6}"
             f"{'copy60':>8}{'copy300':>8}{'idle_d':>7}  verdict"]
    for m in finalists:
        idle = m.get("days_since_trade")
        fee = m.get("follower_fee")
        fee_s = "  n/a" if fee is None else f"{100 * fee:4.1f}%"
        idle_s = "    n/a" if idle is None else f"{idle:7.1f}"
        lines.append(
            f"{(m['name'] or m['wallet'][:10])[:17]:<18}{m['n']:>5}"
            f"{m['pnl']:>11,.0f}{pct(m['roi'])}{pct(m['roi_lo'])}"
            f"{pct(m['roi_old'])}{pct(m['roi_new'])}{min(m['top3_share'], 9.99):>6.2f}"
            f"{100 * m['pos_months']:>4.0f}%{ts(m['taker_share'])} {fee_s}"
            f"{pct(m.get('copy_roi_60s'))}{pct(m.get('copy_roi_300s'))}"
            f"{idle_s}  {m['verdict']}")
    lines += ["", "wallets (finalists):"] + [f"  {m['name'] or '-':<20} {m['wallet']}"
                                          for m in finalists]
    open("scout_report.txt", "w").write("\n".join(lines) + "\n")
    print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=150)
    ap.add_argument("--wallets", help="comma-separated wallets to score")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--order-by", default="PNL", choices=["PNL", "VOL"],
                    help="leaderboard used for the candidate pool")
    ap.add_argument("--asof", help="walk-forward backtest date(s), YYYY-MM-DD[,YYYY-MM-DD...]")
    ap.add_argument("--max-closed", type=int, default=3000,
                    help="closed positions per wallet in backtest mode")
    args = ap.parse_args()
    if args.asof:
        run_backtest(args)
    else:
        run_scan(args)


if __name__ == "__main__":
    main()

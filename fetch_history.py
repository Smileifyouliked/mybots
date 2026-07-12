#!/usr/bin/env python3
"""
fetch_history.py — download a wallet's FULL Polymarket trade history and
reconstruct per-market wins/losses from raw cash flows.

    python3 fetch_history.py
    python3 fetch_history.py --target 0xSOMEONE_ELSE

What it produces (in the current folder):
  history_summary.txt   <- small, PASTE THIS TO CLAUDE directly
  markets.csv           <- one row per market: money in/out, net, result
  trades_raw.csv        <- every individual trade (big; upload if asked)

How win/loss is decided (no dashboard tricks):
  For each market, add up what he SPENT buying, what he GOT back selling,
  and what he REDEEMED at resolution. net = (sold + redeemed) - bought.
  Markets he still holds (per the positions API) are marked OPEN and kept
  out of the win/loss stats.
"""

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

DATA_API = "https://data-api.polymarket.com"
DEFAULT_TARGET = "0x6297b93ea37ff92a57fd636410f3b71ebf74517e"   # neobrother
PAGE = 500              # rows per request
MAX_ROWS = 20000        # safety cap
LONGSHOT_MAX = 0.20     # avg entry <= this counts as a longshot market


def fetch_all(target, activity_type):
    """Page through /activity until exhausted. Returns a list of raw events."""
    out, offset = [], 0
    while offset < MAX_ROWS:
        params = {"user": target, "type": activity_type, "limit": PAGE,
                  "offset": offset, "sortBy": "TIMESTAMP",
                  "sortDirection": "DESC"}  # newest first — the API refuses
                                            # offsets past ~3500, so DESC keeps
                                            # the capped window on RECENT form
        for attempt in range(5):
            try:
                r = requests.get(f"{DATA_API}/activity", params=params, timeout=20)
                if r.status_code == 429:            # rate limited -> back off
                    time.sleep(3 * (attempt + 1)); continue
                if r.status_code == 400 and offset > 0:
                    # server's hard offset cap — not an error, just the end
                    print(f"  {activity_type}: API offset cap at {offset}; "
                          f"keeping the newest {len(out)} rows")
                    return out
                r.raise_for_status()
                batch = r.json()
                break
            except Exception as e:
                print(f"  retry {attempt+1}: {e}")
                time.sleep(2 ** attempt)
        else:
            print("  giving up on this page; continuing with what we have")
            break
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        print(f"  {activity_type}: fetched {len(out)} so far...")
        if len(batch) < PAGE:
            break
        offset += PAGE
        time.sleep(0.4)                              # be polite to the API
    return out


def fetch_positions(target):
    try:
        r = requests.get(f"{DATA_API}/positions",
                         params={"user": target, "limit": 500}, timeout=20)
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        print(f"  positions fetch failed ({e}); OPEN detection will be weaker")
        return []


def aggregate(trades, redeems, open_condition_ids):
    """
    Collapse raw events into one record per market (conditionId).
    Returns list of dicts with cash in/out and a WIN/LOSS/OPEN result.
    """
    m = defaultdict(lambda: {"title": "", "bought": 0.0, "sold": 0.0,
                             "redeemed": 0.0, "buy_usd_x_price": 0.0,
                             "first_ts": None, "last_ts": None, "n_trades": 0})
    def touch(rec, ts):
        if ts:
            rec["first_ts"] = ts if rec["first_ts"] is None else min(rec["first_ts"], ts)
            rec["last_ts"] = ts if rec["last_ts"] is None else max(rec["last_ts"], ts)

    for t in trades:
        cid = t.get("conditionId") or "?"
        rec = m[cid]
        rec["title"] = rec["title"] or (t.get("title") or "")
        usd = float(t.get("usdcSize", 0) or 0)
        px = float(t.get("price", 0) or 0)
        ts = t.get("timestamp")
        rec["n_trades"] += 1
        touch(rec, ts)
        if (t.get("side") or "BUY") == "BUY":
            rec["bought"] += usd
            rec["buy_usd_x_price"] += usd * px      # for avg entry price
        else:
            rec["sold"] += usd

    for rd in redeems:
        cid = rd.get("conditionId") or "?"
        rec = m[cid]
        rec["title"] = rec["title"] or (rd.get("title") or "")
        rec["redeemed"] += float(rd.get("usdcSize", 0) or 0)
        touch(rec, rd.get("timestamp"))

    rows = []
    for cid, rec in m.items():
        got_back = rec["sold"] + rec["redeemed"]
        net = got_back - rec["bought"]
        avg_entry = (rec["buy_usd_x_price"] / rec["bought"]) if rec["bought"] > 0 else 0.0
        if cid in open_condition_ids:
            result = "OPEN"
        elif rec["bought"] == 0:
            result = "NO_BUY"          # sell/redeem only (odd edge case)
        else:
            result = "WIN" if net > 0 else "LOSS"
        rows.append({"conditionId": cid, "title": rec["title"][:60],
                     "n_trades": rec["n_trades"],
                     "bought": round(rec["bought"], 2),
                     "sold": round(rec["sold"], 2),
                     "redeemed": round(rec["redeemed"], 2),
                     "net": round(net, 2),
                     "avg_entry": round(avg_entry, 4),
                     "result": result,
                     "first_ts": rec["first_ts"],
                     "last_ts": rec["last_ts"]})
    def _t(v):
        try: return float(v)
        except (TypeError, ValueError): return 0.0
    rows.sort(key=lambda r: _t(r["last_ts"]))        # chronological by close
    return rows


def summarize(rows, n_raw_trades, n_redeems, target):
    closed = [r for r in rows if r["result"] in ("WIN", "LOSS")]
    wins = [r for r in closed if r["result"] == "WIN"]
    open_rows = [r for r in rows if r["result"] == "OPEN"]
    with_buys = [r for r in rows if r["bought"] > 0]

    def _f(v):
        try: return float(v)
        except (TypeError, ValueError): return None
    starts = [x for x in (_f(r.get("first_ts")) for r in with_buys) if x]
    ends = [x for x in (_f(r.get("last_ts")) for r in with_buys) if x]

    def _d(x):
        if x > 1e12: x = x / 1000          # ms -> s if needed
        try:
            return datetime.fromtimestamp(x, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return "?"
    window = f"{_d(min(starts))} -> {_d(max(ends))}" if starts and ends else "unknown"
    total_in = sum(r["bought"] for r in rows)
    total_net_closed = sum(r["net"] for r in closed)

    longshots = [r for r in closed if 0 < r["avg_entry"] <= LONGSHOT_MAX]
    ls_wins = [r for r in longshots if r["result"] == "WIN"]
    others = [r for r in closed if r["avg_entry"] > LONGSHOT_MAX]

    # streaks + payout multiples on the longshot subset (chronological)
    worst_streak, cur = 0, 0
    multiples = []
    for r in longshots:
        if r["result"] == "LOSS":
            cur += 1; worst_streak = max(worst_streak, cur)
        else:
            cur = 0
        if r["bought"] > 0:
            multiples.append((r["sold"] + r["redeemed"]) / r["bought"])
    multiples.sort()

    def med(xs): return xs[len(xs)//2] if xs else 0

    L = []
    L.append("=" * 58)
    L.append(f"WALLET HISTORY SUMMARY  {target[:10]}…")
    L.append(f"generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    L.append("=" * 58)
    L.append(f"raw trades fetched : {n_raw_trades}   redeems: {n_redeems}")
    L.append(f"trade window       : {window}   <- check this covers RECENT months")
    L.append(f"markets w/ buys    : {len(with_buys)}  (closed {len(closed)}, open {len(open_rows)}; "
             f"+{len(rows) - len(with_buys)} redeem-only rows ignored)")
    L.append(f"total ever spent   : ${total_in:,.2f}")
    L.append(f"net on closed mkts : ${total_net_closed:,.2f}")
    L.append("")
    L.append(f"ALL closed markets : win {len(wins)}/{len(closed)} "
             f"({100*len(wins)/len(closed):.0f}%)" if closed else "no closed markets")
    L.append("")
    L.append(f"LONGSHOT subset (avg entry <= {LONGSHOT_MAX}):")
    if longshots:
        ls_net = sum(r['net'] for r in longshots)
        ls_in = sum(r['bought'] for r in longshots)
        L.append(f"  markets {len(longshots)} | win {len(ls_wins)} "
                 f"({100*len(ls_wins)/len(longshots):.0f}%) | net ${ls_net:,.2f} "
                 f"on ${ls_in:,.2f} in ({100*ls_net/ls_in:+.1f}%)")
        L.append(f"  worst losing streak: {worst_streak}")
        if multiples:
            L.append(f"  payout multiple (got/spent): median {med(multiples):.2f}x | "
                     f"best {multiples[-1]:.1f}x")
        top = sorted(longshots, key=lambda r: -r["bought"])[:5]
        L.append("  biggest longshot markets by $ in:")
        for r in top:
            L.append(f"    {r['result']:4s} ${r['bought']:>8.2f} in / net {r['net']:>+9.2f}  "
                     f"{r['title'][:38]}")
    else:
        L.append("  none found")
    L.append("")
    L.append(f"NON-longshot closed markets: {len(others)} | "
             f"net ${sum(r['net'] for r in others):,.2f}")
    L.append("=" * 58)
    L.append("PASTE EVERYTHING ABOVE TO CLAUDE. If asked, also share markets.csv")
    L.append("(push to your mybots repo and give Claude the raw URL).")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=DEFAULT_TARGET)
    args = ap.parse_args()
    t = args.target

    print(f"Fetching TRADE history for {t[:12]}… (this can take a minute)")
    trades = fetch_all(t, "TRADE")
    print("Fetching REDEEM history…")
    redeems = fetch_all(t, "REDEEM")
    print("Fetching current open positions…")
    positions = fetch_positions(t)
    open_cids = {p.get("conditionId") for p in positions if p.get("conditionId")}

    print("Aggregating per market…")
    rows = aggregate(trades, redeems, open_cids)

    # write outputs
    with open("trades_raw.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "side", "price", "usdcSize", "outcome",
                    "conditionId", "asset", "title"])
        for x in trades:
            w.writerow([x.get("timestamp"), x.get("side"), x.get("price"),
                        x.get("usdcSize"), x.get("outcome"), x.get("conditionId"),
                        x.get("asset"), (x.get("title") or "")[:80]])

    with open("markets.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["conditionId"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    summary = summarize(rows, len(trades), len(redeems), t)
    with open("history_summary.txt", "w") as f:
        f.write(summary + "\n")

    print("\n" + summary)
    print(f"\nfiles written: history_summary.txt, markets.csv ({len(rows)} markets), "
          f"trades_raw.csv ({len(trades)} trades)")


if __name__ == "__main__":
    main()

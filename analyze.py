#!/usr/bin/env python3
"""
analyze.py — read copybot_log.jsonl and decide if a wallet is worth going live on.

    python3 analyze.py                 # offline report
    python3 analyze.py --mark          # price open positions (resolved = ~$1 / ~$0)
    python3 analyze.py --simulate      # Monte-Carlo risk-of-ruin from HIS real longshots

The go/no-go, in one line:
  Longshot-class realized P&L positive  AND  ruin probability low  ->  consider live.
  Either one fails  ->  $50 can't safely ride this wallet's fat-tailed edge.
"""

import argparse
import json
import random
from collections import defaultdict

CLOB_PRICE = "https://clob.polymarket.com/price"


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return [r for r in rows if r.get("kind") == "target_trade"]


def shares_of_buy(ev):
    stake = float(ev.get("my_stake", 0) or 0)
    fill = float(ev.get("est_fill", ev.get("target_price", 0)) or 0)
    return (stake / fill) if fill > 0 else 0.0, stake, fill


def mark_price(token_id):
    import requests
    try:
        r = requests.get(CLOB_PRICE, params={"token_id": token_id, "side": "SELL"}, timeout=10)
        r.raise_for_status()
        p = r.json()
        return float(p["price"]) if isinstance(p, dict) else float(p)
    except Exception:
        return None


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def money(x):
    return f"${x:,.2f}"


def run(path, do_mark, do_sim):
    events = load(path)
    if not events:
        print("No target_trade events yet. Let the bot run first.")
        return

    by_class = defaultdict(int)
    actions = defaultdict(int)
    skips = defaultdict(int)
    slip_by_class = defaultdict(list)
    lots = defaultdict(list)                 # token -> [[shares, cost, class, title], ...]
    round_trips = []
    outcome_sequence = []                    # chronological return-multiples, longshots only

    for ev in events:
        klass = (ev.get("intent") or {}).get("class", "unknown")
        action = ev.get("action", "")
        by_class[klass] += 1
        actions[action] += 1
        token = ev.get("token_id")
        title = (ev.get("title") or "")[:46]

        if action == "skip":
            skips[ev.get("reason", "?")] += 1
        elif action in ("WOULD_BUY", "BOUGHT"):
            sh, cost, fill = shares_of_buy(ev)
            if sh > 0:
                lots[token].append([sh, cost, klass, title])
                slip_by_class[klass].append(fill - float(ev.get("target_price", fill) or fill))
        elif action in ("WOULD_SELL", "SOLD"):
            sh_sell = float(ev.get("shares", 0) or 0)
            pxp = float(ev.get("now_price", 0) or 0)
            remaining = sh_sell
            while remaining > 1e-9 and lots.get(token):
                lot = lots[token][0]
                take = min(lot[0], remaining)
                cost_portion = lot[1] * (take / lot[0]) if lot[0] else 0.0
                proceeds = take * pxp
                rt = {"class": lot[2], "cost": cost_portion,
                      "pnl": proceeds - cost_portion, "title": lot[3]}
                round_trips.append(rt)
                if lot[2] == "longshot_lottery" and cost_portion > 0:
                    outcome_sequence.append(proceeds / cost_portion)   # return multiple
                lot[0] -= take; lot[1] -= cost_portion; remaining -= take
                if lot[0] <= 1e-9:
                    lots[token].pop(0)

    # ---------- report ----------
    print("=" * 62)
    print(f"COPYBOT REPORT  ({len(events)} target trades observed)")
    print("=" * 62)

    print("\nTarget activity by inferred class:")
    for k, n in sorted(by_class.items(), key=lambda x: -x[1]):
        print(f"  {k:18s} {n:4d}")

    print("\nBot actions:")
    for a in ("WOULD_BUY", "BOUGHT", "WOULD_SELL", "SOLD", "skip", "note_only"):
        if actions.get(a):
            print(f"  {a:12s} {actions[a]:4d}")
    if skips:
        print("\nSkip reasons:")
        for r, n in sorted(skips.items(), key=lambda x: -x[1]):
            print(f"  {n:4d}  {r}")

    print("\nAvg entry slippage vs his fill (edge lost to lag):")
    for k, xs in sorted(slip_by_class.items()):
        if xs:
            print(f"  {k:18s} {sum(xs)/len(xs)*100:+5.2f}c  (n={len(xs)})")

    # realized P&L
    print("\n" + "-" * 62)
    if round_trips:
        total = sum(r["pnl"] for r in round_trips)
        cost = sum(r["cost"] for r in round_trips)
        wins = sum(1 for r in round_trips if r["pnl"] > 0)
        print(f"REALIZED P&L: {money(total)} on {money(cost)} deployed "
              f"({pct(total,cost):+.1f}%)")
        print(f"  {len(round_trips)} closed | win rate {wins}/{len(round_trips)} "
              f"({pct(wins,len(round_trips)):.0f}%)")
        agg = defaultdict(lambda: [0, 0, 0.0, 0.0])
        for r in round_trips:
            a = agg[r["class"]]
            a[0] += 1; a[1] += 1 if r["pnl"] > 0 else 0
            a[2] += r["pnl"]; a[3] += r["cost"]
        print("\n  by class:")
        for k, (n, w, pnl, c) in sorted(agg.items(), key=lambda x: x[1][2]):
            print(f"    {k:18s} {money(pnl):>10s}  win {w}/{n} ({pct(w,n):.0f}%)  "
                  f"ROI {pct(pnl,c):+.1f}%")
    else:
        print("REALIZED P&L: no closed round trips yet.")

    # longshot streak stats (from the chronological outcome sequence)
    if outcome_sequence:
        worst_streak, cur = 0, 0
        for m in outcome_sequence:
            if m < 1.0:            # lost money on that bet
                cur += 1
                worst_streak = max(worst_streak, cur)
            else:
                cur = 0
        wins = sum(1 for m in outcome_sequence if m >= 1.0)
        print("\n" + "-" * 62)
        print(f"LONGSHOT PROFILE ({len(outcome_sequence)} closed longshots):")
        print(f"  win rate {wins}/{len(outcome_sequence)} ({pct(wins,len(outcome_sequence)):.0f}%)  "
              f"| worst losing streak observed: {worst_streak}")
        best = max(outcome_sequence)
        print(f"  best hit: {best:.1f}x  |  avg return multiple: "
              f"{sum(outcome_sequence)/len(outcome_sequence):.2f}x")

    # open positions
    open_pos = [(t, lot) for t, ls in lots.items() for lot in ls
                if lot[0] > 1e-6 and lot[1] > 0.01]
    if open_pos:
        print("\n" + "-" * 62)
        print(f"OPEN POSITIONS: {len(open_pos)}")
        for token, lot in open_pos:
            sh, cost, klass, title = lot
            line = f"  {title:48s} {klass:16s} cost {money(cost)}"
            if do_mark:
                mp = mark_price(token)
                if mp is not None:
                    val = sh * mp
                    tag = ("RESOLVED-WIN" if mp > 0.95 else
                           "RESOLVED-LOSS" if mp < 0.05 else f"@{mp:.2f}")
                    line += f"  -> {money(val)} {tag} ({money(val-cost):+})"
            print(line)

    # ---------- risk-of-ruin simulator ----------
    if do_sim:
        print("\n" + "=" * 62)
        print("RISK-OF-RUIN SIMULATION")
        print("=" * 62)
        simulate_ruin(outcome_sequence)

    print("\n" + "=" * 62)
    print("GO LIVE only if: longshot realized P&L is positive AND ruin prob is low.")
    print("=" * 62)


def simulate_ruin(outcomes, bankroll=50.0, bet_fraction=0.03, min_order=1.0,
                  n_bets=300, n_sims=5000):
    """
    Resample HIS observed longshot outcomes to estimate whether $50 survives.
    Each bet risks bet_fraction of CURRENT bankroll; outcome is a resampled
    return multiple (proceeds/cost). 'Ruin' = bankroll falls below min_order.
    """
    if len(outcomes) < 10:
        print(f"  Only {len(outcomes)} closed longshots — need ~10+ for a meaningful sim.")
        print("  Keep the dry run going; come back when more have resolved.")
        return

    ends, ruins, min_lows = [], 0, []
    for _ in range(n_sims):
        bank = bankroll
        low = bank
        ruined = False
        for _ in range(n_bets):
            stake = min(bank * bet_fraction, 4.0)
            if stake < min_order:
                ruined = True
                break
            mult = random.choice(outcomes)          # bootstrap from his real results
            bank = bank - stake + stake * mult
            low = min(low, bank)
            if bank < min_order:
                ruined = True
                break
        ends.append(bank)
        min_lows.append(low)
        if ruined:
            ruins += 1

    ends.sort()
    median = ends[len(ends) // 2]
    p10 = ends[int(len(ends) * 0.10)]
    p90 = ends[int(len(ends) * 0.90)]
    grew = sum(1 for e in ends if e > bankroll)

    print(f"  Inputs: bankroll ${bankroll:.0f}, {bet_fraction*100:.0f}% per bet, "
          f"{n_bets} bets, {n_sims} sims, sampling {len(outcomes)} real outcomes.")
    print(f"  P(ruin — bankroll drops below min order): {pct(ruins, n_sims):.1f}%")
    print(f"  P(end above starting $50):               {pct(grew, n_sims):.1f}%")
    print(f"  Ending bankroll — median {money(median)} | "
          f"10th %ile {money(p10)} | 90th %ile {money(p90)}")
    if ruins / n_sims > 0.25:
        print("  READ: high bust rate. The edge may be real but $50 is too thin to ride it.")
    elif median > bankroll and ruins / n_sims < 0.10:
        print("  READ: survives and grows in most paths. Worth a small live test.")
    else:
        print("  READ: marginal. Grinds sideways; not a clear green light.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="copybot_log.jsonl")
    ap.add_argument("--mark", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    args = ap.parse_args()
    run(args.log, args.mark, args.simulate)

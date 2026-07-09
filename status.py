#!/usr/bin/env python3
"""
status.py — one-command health snapshot of the copybot.

Run this any time something looks off (or just to check in):

    python3 status.py

It prints a compact report you can copy-paste straight to Claude. It pulls
everything needed to diagnose a problem in one place: config in effect, the
bankroll curve over time, trade/skip/error counts, the most recent errors, and
open positions. Nothing sensitive (no keys) is included.
"""

import json
import os
from collections import defaultdict, Counter
from datetime import datetime, timezone

LOG_PATH = "copybot_log.jsonl"
STATE_PATH = "copybot_state.json"


def load_log(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_age(dt):
    if not dt:
        return "?"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    h = secs / 3600
    if h < 1:
        return f"{int(secs/60)}m ago"
    if h < 48:
        return f"{h:.1f}h ago"
    return f"{h/24:.1f}d ago"


def main():
    rows = load_log(LOG_PATH)
    state = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
        except Exception:
            pass

    print("```")
    print("=" * 60)
    print("COPYBOT STATUS SNAPSHOT  (paste this to Claude)")
    print("=" * 60)

    if not rows:
        print("\nNo log entries found. Bot may not have started, or is in a")
        print("different directory. Check that copybot_log.jsonl exists here.")
        print("```")
        return

    # session span
    first_ts = parse_ts(rows[0].get("ts", ""))
    last_ts = parse_ts(rows[-1].get("ts", ""))
    startup = next((r for r in rows if r.get("kind") == "startup"), {})

    print(f"\nSession: started {fmt_age(first_ts)}, last activity {fmt_age(last_ts)}")
    print(f"Target : {startup.get('target', '?')}")
    print(f"Mode   : {startup.get('mode', '?')} | "
          f"{'LIVE' if startup.get('live') else 'PAPER'}")

    # current equity from state
    if state:
        bank = state.get("bankroll")
        holds = {k: v for k, v in state.get("holdings", {}).items() if v > 1e-9}
        invested = None
        equity_events = [r for r in rows if r.get("kind") == "equity"]
        if bank is not None:
            print(f"\nBankroll (cash): ${bank:,.2f}")
            net = bank - 50.0
            print(f"Net cash flow vs $50 start: {'+' if net >= 0 else ''}${net:,.2f}  "
                  f"(realized only — run 'analyze.py --mark' for full P&L incl. open bets)")
        else:
            print("\nBankroll: ?")
        print(f"Open positions : {len(holds)}")

    # counts
    kinds = Counter(r.get("kind") for r in rows)
    actions = Counter(r.get("action") for r in rows if r.get("kind") == "target_trade")
    print(f"\nEvent counts: "
          + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))
    if actions:
        print("Actions: " + ", ".join(f"{k}={v}" for k, v in actions.most_common() if k))

    # skip reasons (grouped)
    skip_reasons = Counter()
    for r in rows:
        if r.get("action") == "skip":
            reason = r.get("reason", "?")
            # collapse the parametrized ones
            if reason.startswith("entry "):
                reason = "entry price outside band"
            elif reason.startswith("not a longshot"):
                reason = "not a longshot"
            skip_reasons[reason] += 1
    if skip_reasons:
        print("\nWhy trades were skipped:")
        for reason, n in skip_reasons.most_common():
            print(f"  {n:4d}  {reason}")

    # errors — surface these prominently
    errors = [r for r in rows if r.get("kind") == "api_error"
              or r.get("action") in ("buy_error", "sell_error", "price_error")]
    print(f"\nErrors logged: {len(errors)}")
    if errors:
        print("Most recent errors:")
        for r in errors[-5:]:
            what = r.get("action") or r.get("kind")
            msg = str(r.get("err", ""))[:120]
            print(f"  [{fmt_age(parse_ts(r.get('ts','')))}] {what}: {msg}")

    # equity curve samples over time
    eq = [r for r in rows if r.get("kind") == "equity"]
    if len(eq) >= 2:
        print("\nBankroll cash over time (heartbeats):")
        step = max(1, len(eq) // 8)   # ~8 samples
        for r in eq[::step]:
            ts = parse_ts(r.get("ts", ""))
            print(f"  {ts.strftime('%m-%d %H:%M') if ts else '?':<12} "
                  f"${r.get('bankroll_cash', 0):>8,.2f}  "
                  f"({r.get('open_positions', 0)} open)")

    # last few actions for context
    trades = [r for r in rows if r.get("kind") == "target_trade"
              and r.get("action") in ("WOULD_BUY", "BOUGHT", "WOULD_SELL", "SOLD")]
    if trades:
        print("\nLast 5 copied actions:")
        for r in trades[-5:]:
            a = r.get("action")
            px = r.get("est_fill", r.get("now_price", r.get("target_price", "?")))
            amt = r.get("my_stake", r.get("shares", "?"))
            title = (r.get("title") or "")[:36]
            print(f"  [{fmt_age(parse_ts(r.get('ts','')))}] {a:11s} {title:38s} "
                  f"px={px} amt={amt}")

    # quick auto-diagnosis hints
    print("\nAuto-flags:")
    flags = []
    if last_ts and (datetime.now(timezone.utc) - last_ts).total_seconds() > 7200:
        flags.append("No activity in 2h+ — bot may be stopped or target is quiet.")
    if len(errors) > 10:
        flags.append(f"{len(errors)} errors — likely an API/auth/network issue.")
    if state.get("bankroll", 999) < 5:
        flags.append("Bankroll under $5 — near the floor; strategy is losing.")
    total_buys = actions.get("WOULD_BUY", 0) + actions.get("BOUGHT", 0)
    if kinds.get("target_trade", 0) > 50 and total_buys == 0:
        flags.append("Seeing his trades but copying none — check the gates/filters.")
    if not flags:
        flags.append("Nothing obviously wrong.")
    for f in flags:
        print(f"  - {f}")

    print("=" * 60)
    print("```")


if __name__ == "__main__":
    main()

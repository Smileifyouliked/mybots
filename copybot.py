#!/usr/bin/env python3
"""
copybot.py — Polymarket LONGSHOT copy-trader (VPS edition).

Strategy decision (see the scatter-plot analysis):
  The target runs two books with opposite signs. His cheap YES "unlikely outcome"
  bets are the profitable cluster; his near-lock NO grinds are the money pit.
  This bot copies ONLY the cheap longshots and ignores everything else.

Why the guardrails matter:
  His longshots win ~125 : lose ~243. Profit comes from rare 5-9x hits, not
  steady wins -> fat-tailed. On a $50 stack that's dangerous, so:
    * cheap-entry gate  : only copy buys at <= MAX_ENTRY_PRICE, and skip if the
                          price already ran past PAY_MULT x his fill (edge gone).
    * fractional sizing : each bet is BET_FRACTION of CURRENT bankroll, so bets
                          shrink after losses and you can't hit exactly zero.
    * dry-run default   : measure first. Go live only after analyze.py's ruin
                          simulator says $50 can survive his loss streaks.

Deploy: push to your `mybots` repo, wget onto the EC2 box. State persists to
disk so a restart doesn't re-copy old trades or lose position tracking.
See DEPLOY.md for the systemd unit.

    pip install requests py-clob-client-v2
    python3 copybot.py --target 0x6297b93ea37ff92a57fd636410f3b71ebf74517e   # dry run
    LIVE=1 POLY_PK=0x.. POLY_FUNDER=0x.. python3 copybot.py --target 0x..     # real money
"""

import argparse
import json
import os
import random
import sys
import time
from collections import deque, defaultdict
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
DATA_API = "https://data-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

POLL_SECONDS = 8
HEARTBEAT_SECONDS = 3600         # log an equity snapshot at least this often
STARTING_BANKROLL = 50.0        # your capital in pUSD

# --- longshot strategy gates ---
LONGSHOTS_ONLY   = True
MAX_ENTRY_PRICE  = 0.20         # only copy his cheap bets (his avg fill ~0.11)
MIN_ENTRY_PRICE  = 0.02         # skip near-zero dust (usually illiquid junk)
PAY_MULT         = 1.5          # skip if we'd pay > 1.5x what he paid (edge ran away)

# --- risk-of-ruin-aware sizing (fraction of CURRENT bankroll) ---
BET_FRACTION     = 0.03         # 3% of current bankroll per longshot
MIN_ORDER        = 1.0          # Polymarket ~$1 min
MAX_PER_TRADE    = 4.0          # hard ceiling on any single bet

COPY_SELLS       = True         # mirror his exits on tokens we hold
STATE_PATH       = "copybot_state.json"
LOG_PATH         = "copybot_log.jsonl"

# --- paper-mode fill realism (makes dry-run pessimistic, closer to live) ---
# Only applied when NOT live. Set REALISTIC_FILLS=False for frictionless paper.
REALISTIC_FILLS  = True
PAPER_FILL_MISS  = 0.30         # 30% of cheap-bet orders "don't fill" and are skipped
PAPER_EXTRA_SLIP = 0.15         # pay 15% worse than shown price on entry (thin book)
LOW_LIQ_PRICE    = 0.20         # below this price, treat market as thin (apply penalties)

# --- live execution (off until the ruin sim convinces you) ---
LIVE = os.environ.get("LIVE", "0") == "1"
POLY_PK = os.environ.get("POLY_PK")
POLY_FUNDER = os.environ.get("POLY_FUNDER")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "3"))  # 3 = POLY_1271 deposit wallet
CHAIN_ID = 137


# ----------------------------------------------------------------------------
# Persistence — survive VPS restarts
# ----------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                s = json.load(f)
            s["seen"] = set(s.get("seen", []))
            s["holdings"] = defaultdict(float, s.get("holdings", {}))
            return s
        except Exception as e:
            print(f"[warn] state unreadable ({e}); starting fresh", file=sys.stderr)
    return {"seen": set(), "holdings": defaultdict(float),
            "bankroll": STARTING_BANKROLL, "initialized": False}


def save_state(s):
    try:
        out = {"seen": list(s["seen"])[-5000:],   # cap file size
               "holdings": {k: v for k, v in s["holdings"].items() if v > 1e-9},
               "bankroll": s["bankroll"], "initialized": s["initialized"]}
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print(f"[warn] could not save state: {e}", file=sys.stderr)


def log(event):
    event["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(event, default=str)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Data API
# ----------------------------------------------------------------------------
def fetch_activity(target, limit=50):
    params = {"user": target, "type": "TRADE", "limit": limit,
              "sortBy": "TIMESTAMP", "sortDirection": "DESC"}
    for attempt in range(4):
        try:
            r = requests.get(f"{DATA_API}/activity", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            wait = 2 ** attempt
            log({"kind": "api_error", "err": str(e), "retry_in": wait})
            time.sleep(wait)
    return []


# ----------------------------------------------------------------------------
# Strategy inference (kept simple; longshot is the class we act on)
# ----------------------------------------------------------------------------
def classify(price, usd, med_size, side):
    if side == "SELL":
        return "exit"
    if price >= 0.85:  return "grind_near_lock"
    if price >= 0.62:  return "favorite"
    if price >= 0.40:  return "tossup"
    if price >= 0.18:  return "value_contrarian"
    return "longshot_lottery"


# ----------------------------------------------------------------------------
# CLOB (read-only always; authed only when LIVE)
# ----------------------------------------------------------------------------
def make_clob():
    from py_clob_client_v2 import ClobClient
    if LIVE:
        if not (POLY_PK and POLY_FUNDER):
            print("LIVE=1 but POLY_PK / POLY_FUNDER unset. Aborting.", file=sys.stderr)
            sys.exit(1)
        c = ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=POLY_PK,
                       signature_type=SIGNATURE_TYPE, funder=POLY_FUNDER)
        creds = c.create_or_derive_api_key()
        return ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID, key=POLY_PK, creds=creds,
                          signature_type=SIGNATURE_TYPE, funder=POLY_FUNDER)
    return ClobClient(host=CLOB_HOST, chain_id=CHAIN_ID)


def cur_price(clob, token_id, side):
    try:
        p = clob.get_price(token_id, side="BUY" if side == "BUY" else "SELL")
        return float(p["price"]) if isinstance(p, dict) else float(p)
    except Exception:
        return None


def place(clob, token_id, side, amount, worst_price, tick="0.01"):
    from py_clob_client_v2 import MarketOrderArgs, OrderType, Side, PartialCreateOrderOptions
    args = MarketOrderArgs(token_id=token_id, amount=amount,
                           side=Side.BUY if side == "BUY" else Side.SELL,
                           order_type=OrderType.FOK, price=worst_price)
    signed = clob.create_market_order(args, options=PartialCreateOrderOptions(tick_size=tick))
    return clob.post_order(signed)


# ----------------------------------------------------------------------------
# Sizing — fraction of CURRENT bankroll
# ----------------------------------------------------------------------------
def size_bet(bankroll):
    raw = bankroll * BET_FRACTION
    raw = min(raw, MAX_PER_TRADE)
    return round(raw, 2) if raw >= MIN_ORDER else 0.0


def tkey(t):
    return f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('side')}"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def run(target):
    clob = make_clob()
    st = load_state()
    sizes = deque(maxlen=50)

    # Baseline: on first ever run, mark existing history as seen so we only copy NEW.
    if not st["initialized"]:
        hist = fetch_activity(target, limit=100)
        for t in hist:
            try: sizes.append(float(t.get("usdcSize", 0)))
            except (TypeError, ValueError): pass
            st["seen"].add(tkey(t))
        st["initialized"] = True
        save_state(st)
        log({"kind": "startup", "target": target, "live": LIVE,
             "bankroll": st["bankroll"], "mode": "longshots_only" if LONGSHOTS_ONLY else "all",
             "note": "baselined history; will copy only new trades"})

    last_beat = 0.0
    while True:
        # equity heartbeat — records the bankroll curve over time for troubleshooting
        if time.time() - last_beat >= HEARTBEAT_SECONDS:
            open_positions = sum(1 for v in st["holdings"].values() if v > 1e-9)
            invested = round(STARTING_BANKROLL - st["bankroll"], 2)
            log({"kind": "equity", "bankroll_cash": round(st["bankroll"], 2),
                 "open_positions": open_positions, "cash_invested_in_open": invested,
                 "mode": "live" if LIVE else "paper",
                 "realistic_fills": (not LIVE) and REALISTIC_FILLS})
            last_beat = time.time()

        for t in reversed(fetch_activity(target, limit=50)):
            k = tkey(t)
            if k in st["seen"]:
                continue
            st["seen"].add(k)

            side  = t.get("side", "BUY")
            price = float(t.get("price", 0) or 0)
            usd   = float(t.get("usdcSize", 0) or 0)
            token = t.get("asset")
            title = t.get("title", "")
            sizes.append(usd)
            med = sorted(sizes)[len(sizes) // 2] if sizes else 0
            klass = classify(price, usd, med, side)

            base = {"kind": "target_trade", "title": title, "token_id": token,
                    "condition_id": t.get("conditionId"), "outcome": t.get("outcome"),
                    "side": side, "target_price": price, "target_usd": usd,
                    "intent": {"class": klass}}

            # ---- SELL: exit tokens we hold ----
            if side == "SELL":
                held = st["holdings"].get(token, 0)
                if not COPY_SELLS or held <= 0:
                    log({**base, "action": "note_only", "reason": "no position"})
                    continue
                now = cur_price(clob, token, side)
                proceeds_price = now or price
                if LIVE:
                    try:
                        resp = place(clob, token, "SELL", held,
                                     worst_price=max(0.01, proceeds_price - 0.03))
                        log({**base, "action": "SOLD", "shares": held, "resp": resp})
                    except Exception as e:
                        log({**base, "action": "sell_error", "err": str(e)}); continue
                else:
                    log({**base, "action": "WOULD_SELL", "shares": held,
                         "now_price": proceeds_price})
                st["bankroll"] += held * proceeds_price
                st["holdings"][token] = 0
                save_state(st)
                continue

            # ---- BUY: longshot gate ----
            if LONGSHOTS_ONLY and klass != "longshot_lottery":
                log({**base, "action": "skip", "reason": f"not a longshot ({klass})"})
                continue
            if not (MIN_ENTRY_PRICE <= price <= MAX_ENTRY_PRICE):
                log({**base, "action": "skip",
                     "reason": f"entry {price:.3f} outside [{MIN_ENTRY_PRICE},{MAX_ENTRY_PRICE}]"})
                continue

            stake = size_bet(st["bankroll"])
            if stake <= 0:
                log({**base, "action": "skip", "reason": "bankroll too low for min order",
                     "bankroll": round(st["bankroll"], 2)})
                continue

            now = cur_price(clob, token, side)
            fill = now or price
            if fill > price * PAY_MULT:      # edge ran away — cheap fill is the whole edge
                log({**base, "action": "skip", "reason": "price ran past pay-mult",
                     "his_price": price, "now_price": now, "my_stake": stake})
                continue

            # paper-mode realism: thin cheap markets don't always fill, and fill worse
            if not LIVE and REALISTIC_FILLS and fill <= LOW_LIQ_PRICE:
                if random.random() < PAPER_FILL_MISS:
                    log({**base, "action": "skip", "reason": "paper: simulated fill miss",
                         "his_price": price, "est_fill": fill, "my_stake": stake})
                    continue
                fill = min(0.99, fill * (1 + PAPER_EXTRA_SLIP))   # pay worse than shown

            worst = min(0.99, fill * PAY_MULT)
            if LIVE:
                try:
                    resp = place(clob, token, "BUY", stake, worst_price=worst)
                    st["holdings"][token] += stake / max(fill, 0.01)
                    st["bankroll"] -= stake
                    log({**base, "action": "BOUGHT", "my_stake": stake, "est_fill": fill,
                         "worst_price": worst, "resp": resp,
                         "bankroll": round(st["bankroll"], 2)})
                except Exception as e:
                    log({**base, "action": "buy_error", "err": str(e)})
                    continue
            else:
                st["holdings"][token] += stake / max(fill, 0.01)
                st["bankroll"] -= stake
                log({**base, "action": "WOULD_BUY", "my_stake": stake, "est_fill": fill,
                     "worst_price": worst, "bankroll": round(st["bankroll"], 2)})
            save_state(st)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="target wallet address (0x...)")
    ap.add_argument("--reset", action="store_true", help="wipe saved state and re-baseline")
    args = ap.parse_args()
    if args.reset and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
        print("state reset.")
    try:
        run(args.target)
    except KeyboardInterrupt:
        log({"kind": "shutdown"})

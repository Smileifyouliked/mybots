#!/usr/bin/env python3
"""
copybot.py — Polymarket LONGSHOT copy-trader (VPS edition).

Copies ONLY the target's cheap "unlikely outcome" YES bets (his profitable
cluster) and ignores his near-lock grinds (his money pit). Dry-run/paper by
default; set LIVE=1 + keys for real orders.

v3 changes (fixes from code review):
  * REDEEM/settlement: winning bets that resolve to ~$1 now credit the paper
    bankroll, instead of only losses draining it. (Fixes the "bankroll looks
    falsely bad" bug.)
  * seen-trades memory is now ORDER-PRESERVING, so a restart keeps the truly
    most-recent trades instead of a random slice.
  * removed dead code (unused size params).

    pip install --break-system-packages requests py-clob-client-v2
    python3 copybot.py --target 0x6297b93ea37ff92a57fd636410f3b71ebf74517e   # paper
    LIVE=1 POLY_PK=0x.. POLY_FUNDER=0x.. python3 copybot.py --target 0x..     # real money
"""

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
DATA_API = "https://data-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

POLL_SECONDS = 8
HEARTBEAT_SECONDS = 3600         # log equity + check for resolved bets this often
STARTING_BANKROLL = 50.0

# --- longshot strategy gates ---
LONGSHOTS_ONLY   = True
MAX_ENTRY_PRICE  = 0.20
MIN_ENTRY_PRICE  = 0.02
PAY_MULT         = 1.2          # measured edge is ~1.64x per $1 staked; paying
                                 # 1.5x his price left only ~9% — 1.2x keeps ~37%
MAX_MARKET_FRAC  = 0.06         # max share of bankroll staked into ONE market
                                 # (he stacks into single cities; streaks cluster)

# --- risk-of-ruin-aware sizing (fraction of CURRENT bankroll) ---
BET_FRACTION     = 0.03
MIN_ORDER        = 1.0
MAX_PER_TRADE    = 4.0

# --- resolution settlement (paper mode) ---
RESOLVE_WIN      = 0.97          # held token priced >= this -> treat as resolved WIN
RESOLVE_LOSS     = 0.03          # held token priced <= this -> treat as resolved LOSS

COPY_SELLS       = True
STATE_PATH       = "copybot_state.json"
LOG_PATH         = "copybot_log.jsonl"
SEEN_KEEP        = 8000          # how many recent trade-keys to remember across restarts

# --- paper-mode fill realism (makes dry-run pessimistic, closer to live) ---
REALISTIC_FILLS  = True
PAPER_FILL_MISS  = 0.30
PAPER_EXTRA_SLIP = 0.15
LOW_LIQ_PRICE    = 0.20

# --- live execution ---
LIVE = os.environ.get("LIVE", "0") == "1"
POLY_PK = os.environ.get("POLY_PK")
POLY_FUNDER = os.environ.get("POLY_FUNDER")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "3"))
CHAIN_ID = 137


# ----------------------------------------------------------------------------
# Persistence — survive VPS restarts
# ----------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                s = json.load(f)
            # seen is an ORDERED dict-as-set: insertion order == chronological order.
            s["seen"] = dict.fromkeys(s.get("seen", []))
            s["holdings"] = defaultdict(float, s.get("holdings", {}))
            s["per_market"] = defaultdict(float, s.get("per_market", {}))
            return s
        except Exception as e:
            print(f"[warn] state unreadable ({e}); starting fresh", file=sys.stderr)
    return {"seen": {}, "holdings": defaultdict(float),
            "per_market": defaultdict(float),
            "bankroll": STARTING_BANKROLL, "initialized": False}


def save_state(s):
    try:
        # keys() preserves insertion order, so [-SEEN_KEEP:] is genuinely the most recent
        out = {"seen": list(s["seen"].keys())[-SEEN_KEEP:],
               "holdings": {k: v for k, v in s["holdings"].items() if v > 1e-9},
               "per_market": dict(list(s["per_market"].items())[-2000:]),
               "bankroll": s["bankroll"], "initialized": s["initialized"]}
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print(f"[warn] could not save state: {e}", file=sys.stderr)


def remember(st, key):
    st["seen"][key] = None           # add to the ordered set


# ----------------------------------------------------------------------------
# Pretty console output (colored for terminals, plain when piped to journald)
# ----------------------------------------------------------------------------
class C:
    GRY = "\033[90m"; DIM = "\033[2m"; RED = "\033[31m"; GRN = "\033[32m"
    BGRN = "\033[92m"; YEL = "\033[33m"; CYN = "\033[36m"; BOLD = "\033[1m"
    RESET = "\033[0m"


def _paint(s, color, use):
    return f"{color}{s}{C.RESET}" if use else s


def _short(x, n=32):
    x = str(x or "")
    return x if len(x) <= n else x[:n - 1] + "…"


def format_event(ev, use_color=True):
    """Turn one log event into a tidy one-line human summary."""
    ts = ev.get("ts", "")
    clock = ts[11:19] if len(ts) >= 19 else "--:--:--"
    kind = ev.get("kind")
    action = ev.get("action")
    g = lambda k, d=0: ev.get(k, d)

    # pick a symbol, short label, color, and detail string per event type
    if kind == "startup":
        sym, label, col = "▶", "start", C.BOLD
        detail = (f"watching {_short(g('target'), 12)} · {g('mode','?')} · "
                  f"{'LIVE' if g('live') else 'paper'} · bank ${float(g('bankroll',0)):.2f}")
    elif action in ("BOUGHT", "WOULD_BUY"):
        sym, label, col = "▲", "BUY", C.GRN
        detail = (f"{_short(g('title'))}  ${float(g('my_stake',0)):.2f} @ "
                  f"{float(g('est_fill',0)):.3f}  → bank ${float(g('bankroll',0)):.2f}")
    elif action in ("SOLD", "WOULD_SELL"):
        sym, label, col = "▼", "SELL", C.CYN
        detail = f"{_short(g('title'))}  {float(g('shares',0)):.1f}sh @ {float(g('now_price',0)):.3f}"
    elif kind == "settled":
        won = g("result") == "WIN"
        sym = "✓" if won else "✗"
        label = "WIN" if won else "LOSS"
        col = C.BGRN if won else C.RED
        tail = (f"+${float(g('credit',0)):.2f}" if won else "lost stake")
        detail = f"resolved {_short(g('token_id'), 10)}  {tail}  → bank ${float(g('bankroll',0)):.2f}"
    elif action == "skip":
        sym, label, col = "·", "skip", C.DIM
        detail = _paint(f"{_short(g('title'))}  — {g('reason','')}", C.DIM, use_color)
        return f"{_paint(clock, C.GRY, use_color)}  {_paint(f'{sym} {label:<6}', col, use_color)}  {detail}"
    elif action == "note_only":
        sym, label, col = "·", "note", C.DIM
        detail = _paint(f"{_short(g('title'))} — {g('reason','')}", C.DIM, use_color)
        return f"{_paint(clock, C.GRY, use_color)}  {_paint(f'{sym} {label:<6}', col, use_color)}  {detail}"
    elif kind == "equity":
        sym, label, col = "≡", "equity", C.CYN
        detail = (f"bank ${float(g('bankroll_cash',0)):.2f} · {g('open_positions',0)} open · "
                  f"{g('mode','?')}")
    elif kind == "api_error":
        sym, label, col = "⚠", "ERR", C.YEL
        detail = f"api: {_short(g('err'), 44)} (retry {g('retry_in','?')}s)"
    elif action in ("buy_error", "sell_error"):
        sym, label, col = "⚠", "ERR", C.RED
        detail = f"{action}: {_short(g('err'), 50)}"
    elif kind == "shutdown":
        sym, label, col = "■", "stop", C.DIM
        detail = "bot stopped"
    else:
        sym, label, col = "•", (kind or "?")[:6], C.GRY
        detail = _short(json.dumps({k: v for k, v in ev.items() if k != "ts"}), 60)

    return (f"{_paint(clock, C.GRY, use_color)}  "
            f"{_paint(f'{sym} {label:<6}', col, use_color)}  {detail}")


def log(event):
    event["ts"] = datetime.now(timezone.utc).isoformat()
    # console: pretty + colored when attached to a terminal, plain otherwise
    try:
        print(format_event(event, use_color=sys.stdout.isatty()), flush=True)
    except Exception:
        print(json.dumps(event, default=str), flush=True)
    # file: always compact JSON so analyze.py / status.py stay parseable
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
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
# Strategy inference
# ----------------------------------------------------------------------------
def classify(price, side):
    if side == "SELL":
        return "exit"
    if price >= 0.85:  return "grind_near_lock"
    if price >= 0.62:  return "favorite"
    if price >= 0.40:  return "tossup"
    if price >= 0.18:  return "value_contrarian"
    return "longshot_lottery"


# ----------------------------------------------------------------------------
# CLOB
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
# Sizing
# ----------------------------------------------------------------------------
def size_bet(bankroll):
    raw = min(bankroll * BET_FRACTION, MAX_PER_TRADE)
    return round(raw, 2) if raw >= MIN_ORDER else 0.0


def tkey(t):
    return f"{t.get('transactionHash')}:{t.get('asset')}:{t.get('side')}"


# ----------------------------------------------------------------------------
# Settlement — credit winning bets that resolved (paper mode only)
# ----------------------------------------------------------------------------
def settle_resolved(clob, st):
    """
    His winners usually REDEEM at resolution instead of being sold, so we never
    see a SELL for them. Without this, paper bankroll only ever loses. Here we
    check each held token's price: if it resolved (~1 win / ~0 loss), close it
    and credit the payout. Live mode gets real USDC on-chain, so we skip there.
    """
    if LIVE:
        return
    for token, shares in list(st["holdings"].items()):
        if shares <= 1e-9:
            continue
        p = cur_price(clob, token, "SELL")
        if p is None:
            continue
        if p >= RESOLVE_WIN or p <= RESOLVE_LOSS:
            credit = round(shares * p, 2)
            st["bankroll"] += credit
            st["holdings"][token] = 0
            log({"kind": "settled", "token_id": token, "shares": round(shares, 3),
                 "settle_price": p, "credit": credit,
                 "result": "WIN" if p >= RESOLVE_WIN else "LOSS",
                 "bankroll": round(st["bankroll"], 2)})


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def run(target):
    clob = make_clob()
    st = load_state()

    if not st["initialized"]:
        for t in fetch_activity(target, limit=100):
            remember(st, tkey(t))       # baseline: don't copy history
        st["initialized"] = True
        save_state(st)
        log({"kind": "startup", "target": target, "live": LIVE,
             "bankroll": st["bankroll"],
             "mode": "longshots_only" if LONGSHOTS_ONLY else "all",
             "note": "baselined history; will copy only new trades"})

    last_beat = 0.0
    while True:
        # heartbeat: settle resolved bets, then log the equity snapshot
        if time.time() - last_beat >= HEARTBEAT_SECONDS:
            settle_resolved(clob, st)
            save_state(st)
            open_positions = sum(1 for v in st["holdings"].values() if v > 1e-9)
            log({"kind": "equity", "bankroll_cash": round(st["bankroll"], 2),
                 "open_positions": open_positions,
                 "mode": "live" if LIVE else "paper",
                 "realistic_fills": (not LIVE) and REALISTIC_FILLS})
            last_beat = time.time()

        for t in reversed(fetch_activity(target, limit=50)):
            k = tkey(t)
            if k in st["seen"]:
                continue
            remember(st, k)

            side  = t.get("side", "BUY")
            price = float(t.get("price", 0) or 0)
            usd   = float(t.get("usdcSize", 0) or 0)
            token = t.get("asset")
            title = t.get("title", "")
            klass = classify(price, side)

            base = {"kind": "target_trade", "title": title, "token_id": token,
                    "condition_id": t.get("conditionId"), "outcome": t.get("outcome"),
                    "side": side, "target_price": price, "target_usd": usd,
                    "intent": {"class": klass}}

            # ---- SELL ----
            if side == "SELL":
                held = st["holdings"].get(token, 0)
                if not COPY_SELLS or held <= 0:
                    log({**base, "action": "note_only", "reason": "no position"})
                    continue
                now = cur_price(clob, token, side)
                px = now or price
                if LIVE:
                    try:
                        resp = place(clob, token, "SELL", held, worst_price=max(0.01, px - 0.03))
                        log({**base, "action": "SOLD", "shares": held, "resp": resp})
                    except Exception as e:
                        log({**base, "action": "sell_error", "err": str(e)}); continue
                else:
                    log({**base, "action": "WOULD_SELL", "shares": held, "now_price": px})
                st["bankroll"] += held * px
                st["holdings"][token] = 0
                save_state(st)
                continue

            # ---- BUY: longshot gates ----
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

            # per-market exposure cap: don't stack copies into one market
            cid = t.get("conditionId") or token
            already = st["per_market"].get(cid, 0.0)
            if already + stake > st["bankroll"] * MAX_MARKET_FRAC:
                log({**base, "action": "skip", "reason": "per-market cap reached",
                     "already_in_market": round(already, 2)})
                continue

            now = cur_price(clob, token, side)
            fill = now or price
            if fill > price * PAY_MULT:
                log({**base, "action": "skip", "reason": "price ran past pay-mult",
                     "his_price": price, "now_price": now, "my_stake": stake})
                continue

            if not LIVE and REALISTIC_FILLS and fill <= LOW_LIQ_PRICE:
                if random.random() < PAPER_FILL_MISS:
                    log({**base, "action": "skip", "reason": "paper: simulated fill miss",
                         "est_fill": fill, "my_stake": stake})
                    continue
                fill = min(0.99, fill * (1 + PAPER_EXTRA_SLIP))

            worst = min(0.99, fill * PAY_MULT)
            if LIVE:
                try:
                    resp = place(clob, token, "BUY", stake, worst_price=worst)
                    st["holdings"][token] += stake / max(fill, 0.01)
                    st["per_market"][cid] = already + stake
                    st["bankroll"] -= stake
                    log({**base, "action": "BOUGHT", "my_stake": stake, "est_fill": fill,
                         "worst_price": worst, "resp": resp, "bankroll": round(st["bankroll"], 2)})
                except Exception as e:
                    log({**base, "action": "buy_error", "err": str(e)}); continue
            else:
                st["holdings"][token] += stake / max(fill, 0.01)
                st["per_market"][cid] = already + stake
                st["bankroll"] -= stake
                log({**base, "action": "WOULD_BUY", "my_stake": stake, "est_fill": fill,
                     "worst_price": worst, "bankroll": round(st["bankroll"], 2)})
            save_state(st)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--reset", action="store_true", help="wipe state and re-baseline")
    args = ap.parse_args()
    if args.reset and os.path.exists(STATE_PATH):
        os.remove(STATE_PATH); print("state reset.")
    try:
        run(args.target)
    except KeyboardInterrupt:
        log({"kind": "shutdown"})

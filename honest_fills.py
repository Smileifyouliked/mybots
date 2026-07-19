"""
honest_fills.py — make paper trading tell the truth
====================================================
bot_v2.py "fills" like this:  shares = size / ask
That assumes: infinite shares available at the displayed ask, zero
latency, and you can always exit at fair value. On thin weather books
none of that is true — which is how a sim prints +$1000 while the same
strategy loses live.

This module simulates what the LIVE market would actually have given
you, using Polymarket's public order book (read-only, no key, no auth —
it cannot trade and it cannot leak anything).

Three honesty rules it enforces:
  1. You only get the shares that were actually sitting on the book,
     walking DOWN the levels (paying more) as you go, and never taking
     more than a small fraction of visible depth.
  2. A position is worth what the BIDS will pay you right now
     (mark-to-bid), not the last price and not the mid.
  3. A signal must survive one full scan interval before you may fill
     it ("two-touch") — models the latency between seeing a price and
     your order landing. Prices that only existed for one snapshot were
     never really yours.
"""

import json
import time
from pathlib import Path

import requests

BOOK_URL = "https://clob.polymarket.com/book"   # public GET, read-only


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------

def get_book(token_id, timeout=(3, 10)):
    """
    Fetch the live order book for one outcome token.

    Where token_id comes from: the gamma-api market object has a field
    "clobTokenIds" — a JSON-encoded string like '["1234...","5678..."]'
    with one id per outcome, in the same order as the "outcomes" field
    (check which index is your YES/bucket outcome — don't assume).

    Returns {"bids": [(price, size), ...highest first],
             "asks": [(price, size), ...lowest first]}   or None.
    """
    try:
        r = requests.get(BOOK_URL, params={"token_id": token_id}, timeout=timeout)
        r.raise_for_status()
        raw = r.json()
    except Exception:
        return None

    def levels(side, reverse):
        out = []
        for lvl in raw.get(side, []) or []:
            try:
                out.append((float(lvl["price"]), float(lvl["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(out, key=lambda x: x[0], reverse=reverse)

    return {"bids": levels("bids", True), "asks": levels("asks", False)}


# ---------------------------------------------------------------------------
# Fills
# ---------------------------------------------------------------------------

def simulate_buy(book, usd_budget, limit_price=None,
                 max_book_frac=0.10, fee_bps=0):
    """
    Buy up to usd_budget by walking the asks, respecting reality:

      limit_price   — never pay above this (use your MAX_PRICE).
      max_book_frac — never take more than this fraction of the visible
                      depth (default 10%). Your $20 order on a $150 book
                      IS the market moving against you; this models it.
      fee_bps       — taker fee in basis points if the market charges one.

    Returns None if nothing fillable, else:
      {"shares", "spent", "fee", "avg_price", "requested", "filled_frac",
       "levels_used"}
    filled_frac < 1.0 is the honest sim saying: "live, the rest of your
    order would simply not have happened."
    """
    if not book or not book["asks"]:
        return None

    usable = [(p, s) for p, s in book["asks"]
              if limit_price is None or p <= limit_price]
    if not usable:
        return None

    visible_notional = sum(p * s for p, s in usable)
    budget = min(usd_budget, max_book_frac * visible_notional)
    if budget < 0.50:
        return None

    shares = spent = 0.0
    levels_used = 0
    remaining = budget
    for price, size in usable:
        if remaining <= 0.001:
            break
        take = min(size, remaining / price)
        shares += take
        spent += take * price
        remaining -= take * price
        levels_used += 1

    fee = spent * fee_bps / 10000.0
    return {
        "shares": round(shares, 2),
        "spent": round(spent, 2),
        "fee": round(fee, 2),
        "avg_price": round(spent / shares, 4) if shares else None,
        "requested": round(usd_budget, 2),
        "filled_frac": round(spent / usd_budget, 3) if usd_budget else 0,
        "levels_used": levels_used,
    }


def simulate_sell(book, shares, limit_price=None, fee_bps=0):
    """
    Sell by walking DOWN the bids — this is what any exit really costs
    (stop-loss, edge-gone exit, or taking profit before resolution).
    Returns {"proceeds", "fee", "avg_price", "sold", "sold_frac"} or None.
    """
    if not book or not book["bids"] or shares <= 0:
        return None

    usable = [(p, s) for p, s in book["bids"]
              if limit_price is None or p >= limit_price]
    if not usable:
        return None

    sold = proceeds = 0.0
    remaining = shares
    for price, size in usable:
        if remaining <= 0.001:
            break
        take = min(size, remaining)
        sold += take
        proceeds += take * price
        remaining -= take

    fee = proceeds * fee_bps / 10000.0
    return {
        "proceeds": round(proceeds, 2),
        "fee": round(fee, 2),
        "avg_price": round(proceeds / sold, 4) if sold else None,
        "sold": round(sold, 2),
        "sold_frac": round(sold / shares, 3),
    }


def mark_to_bid(book, shares):
    """Honest current value of a position = what the bids pay right now.
    Use THIS for unrealized PnL, never the ask, mid, or last trade."""
    res = simulate_sell(book, shares)
    return res["proceeds"] if res else 0.0


def round_trip_cost(book, usd):
    """Buy $usd then immediately sell it back. The % you lose doing
    nothing is the market's honesty tax — spread + depth in one number.
    If this is 8%, an edge smaller than 8% does not exist for you."""
    buy = simulate_buy(book, usd)
    if not buy or not buy["shares"]:
        return None
    sell = simulate_sell(book, buy["shares"])
    if not sell:
        return None
    return round(100.0 * (buy["spent"] - sell["proceeds"]) / buy["spent"], 2)


# ---------------------------------------------------------------------------
# Two-touch latency gate
# ---------------------------------------------------------------------------

class PendingSignals:
    """
    Persisted latency model. First time you see a signal -> record it,
    don't trade. If the SAME signal is still valid on the NEXT scan ->
    now you may fill, at the CURRENT book. Prices that existed for one
    snapshot were latency mirages; this filters them out.

        pending = PendingSignals("pending_signals.json")
        key = f"{market_id}:{t_low}:{t_high}"
        if pending.confirm(key, max_age_s=2 * SCAN_INTERVAL):
            fill = simulate_buy(get_book(token_id), size, MAX_PRICE)
    """

    def __init__(self, path="pending_signals.json"):
        self.path = Path(path)
        try:
            self.seen = json.loads(self.path.read_text())
        except Exception:
            self.seen = {}

    def confirm(self, key, max_age_s=7200):
        now = time.time()
        # prune stale entries
        self.seen = {k: t for k, t in self.seen.items() if now - t < max_age_s}
        ok = key in self.seen
        if ok:
            del self.seen[key]      # consumed — a fill needs a fresh streak
        else:
            self.seen[key] = now    # first sighting: wait one scan
        self.path.write_text(json.dumps(self.seen))
        return ok


# ---------------------------------------------------------------------------
# Self-test:  python3 honest_fills.py <token_id>     (run on the VPS)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python3 honest_fills.py <clob_token_id>")
        sys.exit(1)

    book = get_book(sys.argv[1])
    if not book:
        print("could not fetch book"); sys.exit(1)

    print("top of book:")
    for p, s in list(reversed(book["asks"][:3])):
        print(f"   ask {p:.2f} x {s:.0f}")
    for p, s in book["bids"][:3]:
        print(f"   bid {p:.2f} x {s:.0f}")

    buy = simulate_buy(book, 20.0)
    print(f"\n$20 buy -> {buy}")
    if buy and buy["shares"]:
        print(f"mark-to-bid value right after: ${mark_to_bid(book, buy['shares']):.2f}")
    print(f"round-trip honesty tax: {round_trip_cost(book, 20.0)}%")

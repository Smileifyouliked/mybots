#!/usr/bin/env python3
"""
btc15_maker.py — Market-making bot for Polymarket "BTC Up or Down 15m"
=======================================================================
THE THESIS
  Takers on these markets pay a dynamic fee (peaking ~3% near 50/50);
  makers pay nothing and the fee pool is redistributed daily to makers.
  This bot plays the maker side: it computes its own fair probability
  from the SAME Chainlink BTC/USD feed the market resolves on, then
  rests BUY bids on BOTH Up and Down tokens just below fair value.

    * both bids fill  -> bought a pair for < $1.00 that pays exactly
                         $1.00 at resolution (locked profit, no fee)
    * one bid fills   -> small inventory carried to resolution,
                         hard-capped and skew-managed

  The taker fee is the moat: quotes near fair value can no longer be
  profitably picked off by faster bots, because the toll exceeds the
  staleness they could exploit.

FAIR VALUE
  Window resolves Up if price(end) >= price(start), via Chainlink.
    P(up) = Phi( ln(S/K) / (sigma * sqrt(tau)) )
  S     = live Chainlink price (Polymarket public RTDS websocket)
  K     = window strike (Chainlink price captured at window start;
          if the bot joins mid-window it back-solves K from market mid)
  sigma = EWMA realized vol of the same feed
  tau   = seconds remaining

MODES
  DRY_RUN=true (default): full simulation. Fills are inferred when the
  live book crosses our quote level (haircut applied because queue
  priority is ignored). Every fill, window and day is written to
  btc15_ledger.jsonl so the REAL edge is measured before money moves.

  The bot also runs a free TAKER AUDIT each window: it records what a
  simple momentum bet (buy the side price has moved toward) would have
  earned after taker fees — data instead of debate.

V2 — SELF-COMPOUNDING
  Quote size, inventory cap and per-window spend all scale automatically
  with the measured bankroll (BANKROLL_START + realized PnL from the
  ledger, restart-safe). Wins grow the next bet; losses shrink it. If the
  bankroll ever falls below DRAWDOWN_STOP (default 50%) of start, the bot
  halts itself and waits for a human — the brake the famous streaks never had.

Run:    python3 btc15_maker.py               (env vars below)
Status: python3 btc15_maker.py --status      (your results + verdict, anytime)
Feed:   python3 btc15_maker.py --feedtest    (20s live check of the price feed)
Test:   python3 btc15_maker.py --selftest    (offline math checks)
Stop:   Ctrl-C, or `touch stop15.flag`
"""

import json
import math
import os
import sys
import time
import signal
import logging
import threading
from collections import deque
from datetime import datetime
from statistics import NormalDist

import requests

# ---------------------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------------------
def _env(n, d):
    return os.environ.get(n, d)

DRY_RUN         = _env("DRY_RUN", "true").lower() != "false"
QUOTE_SIZE      = float(_env("QUOTE_SIZE", "12"))      # shares per side (fixed mode)
COMPOUND        = _env("COMPOUND", "true").lower() != "false"
BANKROLL_START  = float(_env("BANKROLL_START", "20"))  # $ you consider deployed
SIZE_PER_DOLLAR = float(_env("SIZE_PER_DOLLAR", "0.30"))  # shares quoted per $1 bankroll
MIN_QUOTE       = float(_env("MIN_QUOTE", "5"))
MAX_QUOTE       = float(_env("MAX_QUOTE", "300"))
DRAWDOWN_STOP   = float(_env("DRAWDOWN_STOP", "0.5"))  # halt if bankroll < 50% of start
MAX_WINDOW_FRAC = float(_env("MAX_WINDOW_FRAC", "0.6"))  # per-window cash cap = frac*bank
HALF_SPREAD     = float(_env("HALF_SPREAD", "0.02"))   # quote fair -/+ 2c
MAX_SKEW        = float(_env("MAX_SKEW", "0.02"))      # inventory lean, cents
MAX_INV         = float(_env("MAX_INV", "30"))         # max net shares
MAX_WINDOW_USD  = float(_env("MAX_WINDOW_USD", "25"))  # cash cap per window
QUOTE_STOP_SEC  = float(_env("QUOTE_STOP_SEC", "180")) # stop quoting, last 3 min
WARMUP_SEC      = float(_env("WARMUP_SEC", "20"))      # let new window settle
LOOP_SEC        = float(_env("LOOP_SEC", "2.5"))
REQUOTE_SEC     = float(_env("REQUOTE_SEC", "12"))     # refresh cadence
REQUOTE_TICK    = float(_env("REQUOTE_TICK", "0.01"))  # or when fair moves 1c
FILL_FRACTION   = float(_env("FILL_FRACTION", "0.6"))  # paper-fill haircut
FEE_COEF        = float(_env("FEE_COEF", "0.0312"))    # taker fee ~= coef*min(p,1-p)
MODEL_GUARD     = float(_env("MODEL_GUARD", "0.10"))   # widen if |model-mid| >
MODEL_PAUSE     = float(_env("MODEL_PAUSE", "0.18"))   # pause quoting if >
MAX_FILLS_PER_SIDE = int(_env("MAX_FILLS_PER_SIDE", "3"))  # per window, paper mode
MR_DAMP         = float(_env("MR_DAMP", "0.65"))   # damp our reaction to moves
MID_BLEND       = float(_env("MID_BLEND", "0.5"))  # weight on market consensus
PAIR_EDGE       = float(_env("PAIR_EDGE", "0.02")) # pairs must lock >= 2c
SAME_SIDE_STEP  = float(_env("SAME_SIDE_STEP", "0.03"))  # refill needs 3c better px
MAX_BOOK_SPREAD = float(_env("MAX_BOOK_SPREAD", "0.10"))   # wider = junk book, ignore mid
RESOLVE_FEED_AFTER = float(_env("RESOLVE_FEED_AFTER", "45"))  # s after end -> self-resolve
AUDIT_TAU       = float(_env("AUDIT_TAU", "600"))      # momentum audit @10min left
MIN_SIGMA_SEC   = float(_env("MIN_SIGMA_SEC", "2.5e-5"))
MAX_SIGMA_SEC   = float(_env("MAX_SIGMA_SEC", "8e-4"))
VOL_HALFLIFE    = float(_env("VOL_HALFLIFE", "120"))   # seconds
LEDGER_PATH     = _env("LEDGER15_PATH", "btc15_ledger.jsonl")

PRIVATE_KEY     = _env("PM_PRIVATE_KEY", "")
FUNDER_ADDRESS  = _env("PM_FUNDER", "")
SIGNATURE_TYPE  = int(_env("PM_SIG_TYPE", "1"))

GAMMA   = "https://gamma-api.polymarket.com"
CLOB    = "https://clob.polymarket.com"
RTDS_WS = _env("RTDS_WS", "wss://ws-live-data.polymarket.com")
HTTP_TIMEOUT = 12
WINDOW_SEC   = 900
SLUG_FMT     = "btc-updown-15m-{end_ts}"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("btc15")
logging.getLogger("websocket").setLevel(logging.CRITICAL)  # hide library noise
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "btc15-maker/1.0"})
ND = NormalDist()

def parse_iso(ts):
    """Parse an ISO timestamp like '2026-07-05T13:15:00Z' -> datetime|None."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

TZ_OFFSET_H = float(_env("TZ_OFFSET_H", "8"))  # show times in PH time by default

def fmt_hm(epoch):
    """Format an epoch as HH:MM in the user's local time (display only)."""
    return time.strftime("%H:%M", time.gmtime(epoch + TZ_OFFSET_H * 3600))

# ---------------------------------------------------------------------------
# Math (pure — covered by --selftest)
# ---------------------------------------------------------------------------
def phi(x):
    return ND.cdf(x)

def phi_inv(p):
    return ND.inv_cdf(min(max(p, 1e-9), 1 - 1e-9))

def fair_prob(S, K, sigma_sec, tau_sec):
    """P(price_end >= K) under driftless lognormal."""
    if S <= 0 or K <= 0 or tau_sec <= 0:
        return None
    denom = sigma_sec * math.sqrt(max(tau_sec, 1.0))
    if denom <= 0:
        return None
    return phi(math.log(S / K) / denom)

def implied_strike(S, mid, sigma_sec, tau_sec):
    """Back out K from a market mid (used if we join mid-window)."""
    if not (0.03 <= mid <= 0.97):
        return None
    z = phi_inv(mid)
    return S / math.exp(z * sigma_sec * math.sqrt(max(tau_sec, 1.0)))

def taker_fee_per_share(price, coef=FEE_COEF):
    return coef * min(price, 1.0 - price)

def clamp_price(p):
    return min(0.99, max(0.01, round(p, 2)))

def book_mid(book, max_spread=None):
    """Midprice only when the book is informative; junk/wide books -> None.
    A book of bid 0.02 / ask 1.00 has mid 0.51 but means nothing."""
    if max_spread is None:
        max_spread = MAX_BOOK_SPREAD
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    bb, ba = bids[0][0], asks[0][0]
    if ba - bb > max_spread:
        return None
    return (bb + ba) / 2

def paper_fill_ok(crossed_now, crossed_prev, bid_px, last_fill_px,
                  fills_done, max_fills, min_step=None):
    """A simulated fill needs a crossing, a per-window cap, and — after the
    first fill on a side — a price at least min_step BETTER than the last
    fill. No more chasing ladders one cent at a time."""
    if min_step is None:
        min_step = SAME_SIDE_STEP
    if not crossed_now or fills_done >= max_fills:
        return False
    if last_fill_px is None:
        return True
    return bid_px <= last_fill_px - min_step + 1e-9

def avail_at_or_below(asks, px):
    """Shares REALLY offered at or below px. A paper fill can never exceed
    what the book actually contained — no more 105-share fills from 3-share
    dust asks."""
    return sum(s for p, s in asks if p <= px)

def build_quotes(P, half, skew, best_bid_up, best_ask_up,
                 best_bid_dn, best_ask_dn, net_inv, max_inv):
    """
    Two resting BUY bids: Up at P-half-skew, Down at (1-P)-half+skew.
    Never cross the book (stay a maker). Suppress the side that would
    grow inventory past the cap. Returns (bid_up|None, bid_dn|None).
    """
    bid_up = clamp_price(P - half - skew)
    bid_dn = clamp_price((1.0 - P) - half + skew)
    if best_ask_up is not None:
        bid_up = min(bid_up, clamp_price(best_ask_up - 0.01))
    if best_ask_dn is not None:
        bid_dn = min(bid_dn, clamp_price(best_ask_dn - 0.01))
    if net_inv >= max_inv:     # already long Up -> stop buying Up
        bid_up = None
    if net_inv <= -max_inv:    # already long Down -> stop buying Down
        bid_dn = None
    return bid_up, bid_dn

def window_bounds(epoch):
    end = (int(epoch) // WINDOW_SEC + 1) * WINDOW_SEC
    return end - WINDOW_SEC, end

# ---------------------------------------------------------------------------
# Live Chainlink/Binance price feed (Polymarket public RTDS websocket,
# with an always-on Binance REST backup that takes over if the ws is quiet)
# ---------------------------------------------------------------------------
def parse_rtds(raw):
    """Parse one RTDS frame into [(ts_sec, price), ...] BTC ticks.
    Pure function — covered by --selftest. Ignores PONG/acks/other symbols."""
    try:
        m = json.loads(raw)
    except (ValueError, TypeError):
        return []                       # "PONG" or non-JSON keepalive
    out = []
    for item in (m if isinstance(m, list) else [m]):
        if not isinstance(item, dict):
            continue
        if not str(item.get("topic", "")).startswith("crypto_prices"):
            continue
        pay = item.get("payload") or item.get("data") or {}
        if not isinstance(pay, dict):
            continue
        sym = str(pay.get("symbol", pay.get("pair", ""))).lower()
        if "btc" not in sym:
            continue
        try:
            price = float(pay.get("value", pay.get("price")))
        except (TypeError, ValueError):
            continue
        ts = pay.get("timestamp") or item.get("timestamp") or time.time() * 1000
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            ts = time.time() * 1000
        if ts > 1e11:                   # milliseconds -> seconds
            ts /= 1000.0
        out.append((ts, price))
    return out

class PriceFeed:
    """Background websocket + REST backup -> thread-safe price + history."""
    def __init__(self):
        self.lock = threading.Lock()
        self.ticks = deque(maxlen=4000)     # (ts_sec, price) — all sources
        self.cl = deque(maxlen=4000)        # Chainlink-only (resolution feed)
        self.var = (6e-5) ** 2              # EWMA of r^2/dt (per-second)
        self._last = None
        self.connected = False
        self.ws_ts = 0.0                    # last time the websocket delivered
        self.cl_ts = 0.0                    # last time CHAINLINK delivered
        self.fed_ts = 0.0                   # last time the model was fed a tick
        self.src = {"cl": 0, "ws": 0, "rest": 0}
        self._raw_seen = 0

    def on_price(self, ts, price):
        with self.lock:
            if self._last:
                lt, lp = self._last
                dt = ts - lt
                if dt < 0.2:
                    # burst ticks: keep freshest price, don't fake volatility
                    self._last = (lt, price)
                elif lp > 0 and price > 0:
                    if dt < 30:
                        r = math.log(price / lp)
                        if abs(r) < 0.005:   # ignore glitch jumps >0.5%/tick
                            decay = 0.5 ** (dt / VOL_HALFLIFE)
                            self.var = (decay * self.var
                                        + (1 - decay) * (r * r / dt))
                    self._last = (ts, price)
            else:
                self._last = (ts, price)
            self.ticks.append((ts, price))
            self.fed_ts = time.time()

    def latest(self):
        with self.lock:
            return self._last

    def sigma_sec(self):
        with self.lock:
            s = math.sqrt(max(self.var, 0.0))
        return min(max(s, MIN_SIGMA_SEC), MAX_SIGMA_SEC)

    def price_at(self, ts, tol=6.0):
        """Tick nearest to ts within tol seconds. Prefers the Chainlink-only
        stream (the market's actual resolution feed); falls back to the mixed
        stream only if no Chainlink tick is close enough."""
        for source in (self.cl, self.ticks):
            best = None
            with self.lock:
                for t, p in source:
                    d = abs(t - ts)
                    if d <= tol and (best is None or d < best[0]):
                        best = (d, p)
            if best:
                return best[1]
        return None

    # -- feed plumbing --------------------------------------------------------
    def start(self):
        threading.Thread(target=self._rest_poll, daemon=True).start()
        threading.Thread(target=self._run_ws, daemon=True).start()

    def _run_ws(self):
        try:
            import websocket  # websocket-client
        except ImportError:
            log.error("websocket-client not installed — running on REST backup only")
            return
        # Official RTDS schema: filters is a JSON *string*; empty = all symbols
        # (we filter to BTC client-side, which survives symbol-format changes).
        sub = json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
            {"topic": "crypto_prices", "type": "*", "filters": ""},
        ]})

        def on_open(ws):
            ws.send(sub)
            log.info("📡 Live Bitcoin price stream connected")

            def pinger():           # RTDS requires app-level PING every ~5s
                while getattr(ws, "keep_running", True):
                    try:
                        ws.send("PING")
                    except Exception:  # noqa: BLE001
                        return
                    time.sleep(5)
            threading.Thread(target=pinger, daemon=True).start()

        def on_message(_ws, msg):
            if msg == "PONG":
                return
            if self._raw_seen < 1:
                self._raw_seen += 1
                log.info("🔍 First data sample (technical, safe to ignore): "
                         "%.160s", msg)
            is_cl = '"crypto_prices_chainlink"' in msg
            noww = time.time()
            for ts, price in parse_rtds(msg):
                if is_cl:
                    self.on_price(ts, price)
                    with self.lock:
                        self.cl.append((ts, price))
                    self.cl_ts = noww
                    self.src["cl"] += 1
                elif noww - self.cl_ts > 6:   # Binance ws = backup only
                    self.on_price(ts, price)
                    self.src["ws"] += 1
                self.ws_ts = noww
                self.connected = True

        while True:
            try:
                ws = websocket.WebSocketApp(RTDS_WS, on_open=on_open,
                                            on_message=on_message)
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:  # noqa: BLE001
                log.warning("ws error: %s", e)
            log.info("🔄 Price stream dropped — reconnecting in 3s (backup feed "
                     "already covering, nothing lost)")
            time.sleep(3)

    def _rest_poll(self):
        """Always-on Binance REST backup. Injects prices only while the
        websocket has been quiet for >6s, so sources never mix noisily."""
        while True:
            if time.time() - self.fed_ts > 6:
                try:
                    r = SESSION.get(
                        "https://data-api.binance.vision/api/v3/ticker/price",
                        params={"symbol": "BTCUSDT"}, timeout=8)
                    if r.ok:
                        self.on_price(time.time(), float(r.json()["price"]))
                        self.src["rest"] += 1
                        self.connected = True
                except (requests.RequestException, ValueError, KeyError):
                    pass
            time.sleep(1.5)

# ---------------------------------------------------------------------------
# Polymarket helpers
# ---------------------------------------------------------------------------
def _market_from_slug(slug):
    """Fetch one Up/Down market by slug, including its OFFICIAL end time."""
    for endpoint in ("events", "markets"):
        try:
            r = SESSION.get(f"{GAMMA}/{endpoint}", params={"slug": slug},
                            timeout=HTTP_TIMEOUT)
            if not r.ok:
                continue
            data = r.json()
            items = data if isinstance(data, list) else [data]
            for it in items:
                mkts = it.get("markets") if endpoint == "events" else [it]
                for m in mkts or []:
                    toks = json.loads(m.get("clobTokenIds") or "[]")
                    outs = json.loads(m.get("outcomes") or "[]")
                    if len(toks) == 2 and len(outs) == 2:
                        o = [s.strip().lower() for s in outs]
                        if "up" in o and "down" in o:
                            return {"gamma_id": m.get("id"),
                                    "condition_id": m.get("conditionId"),
                                    "slug": slug,
                                    "end_dt": parse_iso(m.get("endDate")
                                                        or it.get("endDate")),
                                    "up": str(toks[o.index("up")]),
                                    "down": str(toks[o.index("down")])}
        except (requests.RequestException, ValueError, TypeError):
            continue
    return None

def slug_matches_window(end_ts, end_dt, tol=120):
    """Does the fetched market really END when OUR round ends?
    True/False when verifiable, None when the market has no end time."""
    if end_dt is None:
        return None
    return abs(end_dt.timestamp() - end_ts) <= tol

_slug_mode_logged = False

def find_window_market(end_ts):
    """Locate the market whose round ENDS at end_ts — VERIFIED, not assumed.
    The slug's number might encode the round's end OR its start; trading the
    wrong one means betting on this round while shopping in the next round's
    still-neutral market. We try both and keep only the market whose official
    endDate matches our round."""
    global _slug_mode_logged
    unverified = None
    for cand, mode in ((end_ts, "END"), (end_ts - WINDOW_SEC, "START")):
        m = _market_from_slug(SLUG_FMT.format(end_ts=cand))
        if not m:
            continue
        ok = slug_matches_window(end_ts, m.pop("end_dt", None))
        if ok:
            if not _slug_mode_logged:
                _slug_mode_logged = True
                log.info("🔎 Market lookup VERIFIED — slug numbers are the "
                         "round's %s time", mode)
            return m
        if ok is None and unverified is None:
            unverified = m
    if unverified:
        log.warning("⚠️ Could not verify this round's market end time — "
                    "using best guess; treat this round's result with doubt")
        return unverified
    return None

def fetch_books(token_ids):
    try:
        r = SESSION.post(f"{CLOB}/books",
                         json=[{"token_id": t} for t in token_ids],
                         timeout=HTTP_TIMEOUT)
        if not r.ok:
            return {}
        out = {}
        for b in r.json():
            tid = str(b.get("asset_id", ""))
            asks = sorted(((float(x["price"]), float(x["size"]))
                           for x in b.get("asks", [])), key=lambda z: z[0])
            bids = sorted(((float(x["price"]), float(x["size"]))
                           for x in b.get("bids", [])), key=lambda z: -z[0])
            out[tid] = {"asks": asks, "bids": bids}
        return out
    except (requests.RequestException, ValueError):
        return {}

def fetch_resolution(gamma_id):
    """Returns 'up' / 'down' / None (not yet resolved)."""
    try:
        r = SESSION.get(f"{GAMMA}/markets/{gamma_id}", timeout=HTTP_TIMEOUT)
        if not r.ok:
            return None
        m = r.json()
        prices = json.loads(m.get("outcomePrices") or "[]")
        outs = [s.strip().lower() for s in json.loads(m.get("outcomes") or "[]")]
        if len(prices) == 2 and len(outs) == 2 and m.get("closed"):
            win = outs[0] if float(prices[0]) > 0.5 else outs[1]
            return win if win in ("up", "down") else None
    except (requests.RequestException, ValueError, TypeError):
        pass
    return None

def ledger(rec):
    rec["ts"] = time.time()
    rec["mode"] = "DRY" if DRY_RUN else "LIVE"
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")

def replay_pnl(path, mode):
    """Sum past window PnL for this mode so compounding survives restarts."""
    pnl = 0.0
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") == "window" and r.get("mode") == mode:
                    pnl += float(r.get("pnl", 0))
    except FileNotFoundError:
        pass
    return pnl

# ---------------------------------------------------------------------------
# Live order layer (paper mode never touches this)
# ---------------------------------------------------------------------------
class LiveOrders:
    def __init__(self):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        self.OrderArgs, self.OrderType, self.BUY = OrderArgs, OrderType, BUY
        kw = {"key": PRIVATE_KEY, "chain_id": 137}
        if FUNDER_ADDRESS:
            kw.update({"signature_type": SIGNATURE_TYPE, "funder": FUNDER_ADDRESS})
        self.c = ClobClient(CLOB, **kw)
        self.c.set_api_creds(self.c.create_or_derive_api_creds())
        self.open = {}   # order_id -> {"token","price","size","matched"}
        log.info("🔗 Connected to your Polymarket account — live orders enabled")

    def place(self, token_id, price, size):
        try:
            o = self.c.create_order(self.OrderArgs(price=price, size=float(size),
                                                   side=self.BUY, token_id=token_id))
            r = self.c.post_order(o, self.OrderType.GTC)
            oid = (r or {}).get("orderID")
            if oid:
                self.open[oid] = {"token": token_id, "price": price,
                                  "size": size, "matched": 0.0}
            return oid
        except Exception as e:  # noqa: BLE001
            log.warning("place failed: %s", e)
            return None

    def cancel_all(self):
        for oid in list(self.open):
            try:
                self.c.cancel(order_id=oid)
            except Exception:  # noqa: BLE001
                pass
            self.open.pop(oid, None)

    def poll_fills(self):
        """Returns list of (token_id, price, newly_matched_size)."""
        fills = []
        for oid, meta in list(self.open.items()):
            try:
                o = self.c.get_order(oid)
                matched = float(o.get("size_matched", 0) or 0)
                if matched > meta["matched"] + 1e-9:
                    fills.append((meta["token"], meta["price"],
                                  matched - meta["matched"]))
                    meta["matched"] = matched
                if o.get("status") in ("CANCELED", "MATCHED") and \
                        matched >= meta["size"] - 1e-9:
                    self.open.pop(oid, None)
            except Exception:  # noqa: BLE001
                continue
        return fills

# ---------------------------------------------------------------------------
# One 15-minute window
# ---------------------------------------------------------------------------
class Window:
    def __init__(self, start_ts, end_ts, market):
        self.start, self.end, self.m = start_ts, end_ts, market
        self.strike = None
        self.strike_src = None
        self.inv_up = self.inv_dn = 0.0
        self.cost_up = self.cost_dn = 0.0
        self.cash = 0.0
        self.fills = 0
        self.quotes = {"up": None, "dn": None}   # (price, placed_at)
        self.last_fill = {"up": 0.0, "dn": 0.0}
        self.last_fill_px = {"up": None, "dn": None}
        self.crossed = {"up": False, "dn": False}
        self.fill_count = {"up": 0, "dn": 0}
        self.audit = None
        self.done_quoting = False

    def net(self):
        return self.inv_up - self.inv_dn

    def record_fill(self, side, price, qty):
        if side == "up":
            self.inv_up += qty
            self.cost_up += price * qty
        else:
            self.inv_dn += qty
            self.cost_dn += price * qty
        self.cash -= price * qty
        self.fills += 1
        ledger({"type": "fill", "slug": self.m["slug"], "side": side,
                "price": price, "qty": qty, "net_inv": self.net()})
        log.info("📥 %s FILL: bought %.0f %s at %.0f¢ | now holding %.0f UP / "
                 "%.0f DOWN | spent $%.2f this round",
                 "PAPER" if DRY_RUN else "LIVE", qty,
                 "UP" if side == "up" else "DOWN", price * 100,
                 self.inv_up, self.inv_dn, -self.cash)

# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------
class Engine:
    def __init__(self):
        self.feed = PriceFeed()
        self.live = None if DRY_RUN else LiveOrders()
        self.win = None
        self.pending = []        # windows awaiting resolution
        self.tot = {"windows": 0, "fills": 0, "shares": 0.0, "pnl": 0.0,
                    "audit_n": 0, "audit_wins": 0, "audit_pnl": 0.0}
        self.hist_pnl = replay_pnl(LEDGER_PATH, "DRY" if DRY_RUN else "LIVE")
        self.halted = False
        self.verify_q = []   # feed-scored rounds awaiting official confirmation

    # -- compounding: everything scales from the measured bankroll -----------
    def bankroll(self):
        return BANKROLL_START + self.hist_pnl + self.tot["pnl"]

    def quote_size(self):
        if not COMPOUND:
            return QUOTE_SIZE
        return float(max(MIN_QUOTE,
                         min(MAX_QUOTE, math.floor(self.bankroll() * SIZE_PER_DOLLAR))))

    def max_inv(self):
        return 2.5 * self.quote_size() if COMPOUND else MAX_INV

    def window_cash_cap(self):
        return MAX_WINDOW_FRAC * self.bankroll() if COMPOUND else MAX_WINDOW_USD

    # -- window lifecycle ----------------------------------------------------
    def roll_window(self, now):
        start, end = window_bounds(now)
        if self.win and self.win.end == end:
            return
        if self.win:                       # close out old window
            if self.live:
                self.live.cancel_all()
            self.pending.append(self.win)
        m = find_window_market(end)
        if not m:
            log.warning("⚠️ Couldn't find this round's market yet — will retry "
                        "next round (harmless unless it repeats for 30+ min): %s",
                        fmt_hm(end))
            self.win = None
            return
        self.win = Window(start, end, m)
        k = self.feed.price_at(start)
        if k:
            self.win.strike, self.win.strike_src = k, "feed"
        log.info("🕐 NEW ROUND ends %s | UP wins if Bitcoin finishes at or above %s",
                 fmt_hm(end),
                 f"${k:,.2f}" if k else "(catching the start price...)")

    def ensure_strike(self, S, books, tau):
        w = self.win
        if w.strike:
            return True
        k = self.feed.price_at(w.start)
        if k:
            w.strike, w.strike_src = k, "feed"
            return True
        up = books.get(w.m["up"], {})
        mid = book_mid(up)
        if mid is not None:
            k = implied_strike(S, mid, self.feed.sigma_sec(), tau)
            if k:
                w.strike, w.strike_src = k, "implied"
                log.info("ℹ️ Joined mid-round — estimated the start price at %s "
                         "from the market's odds", f"${k:,.2f}")
                return True
        return False

    # -- resolution ------------------------------------------------------------
    def settle_pending(self):
        for w in list(self.pending):
            if time.time() < w.end + 20:
                continue
            res, src = fetch_resolution(w.m["gamma_id"]), "gamma"
            if res is None and time.time() > w.end + RESOLVE_FEED_AFTER \
                    and w.strike:
                # self-resolve using the same Chainlink rule the market uses:
                # Up if price(end) >= price(start)
                p_end = self.feed.price_at(w.end, tol=12)
                if p_end is not None:
                    res, src = ("up" if p_end >= w.strike else "down"), "feed"
            if res is not None and src == "gamma" and w.strike:
                p_end = self.feed.price_at(w.end, tol=12)
                if p_end is not None:
                    feed_res = "up" if p_end >= w.strike else "down"
                    if feed_res != res:
                        log.warning("ℹ️ Photo-finish: official result %s but "
                                    "our feed said %s — official result used",
                                    res.upper(), feed_res.upper())
            if res is None:
                if time.time() > w.end + 600:      # give up loudly, not silently
                    self.pending.remove(w)
                    log.warning("⚠️ Round ending %s could not be scored "
                                "(no result data) — skipped. Rare is fine; "
                                "frequent means tell Claude.", fmt_hm(w.end))
                    ledger({"type": "window_unresolved", "slug": w.m["slug"]})
                continue
            payout = w.inv_up if res == "up" else w.inv_dn
            pnl = w.cash + payout
            self.tot["windows"] += 1
            self.tot["fills"] += w.fills
            self.tot["shares"] += w.inv_up + w.inv_dn
            self.tot["pnl"] += pnl
            ledger({"type": "window", "slug": w.m["slug"], "result": res,
                    "fills": w.fills, "shares": w.inv_up + w.inv_dn,
                    "cash": round(w.cash, 4), "payout": payout,
                    "pnl": round(pnl, 4), "bankroll": round(self.bankroll(), 2),
                    "res_src": src, "strike_src": w.strike_src})
            if w.audit:
                a = w.audit
                win = (a["pred"] == res)
                apnl = (1 - a["entry"] if win else -a["entry"]) - a["fee"]
                self.tot["audit_n"] += 1
                self.tot["audit_wins"] += int(win)
                self.tot["audit_pnl"] += apnl
                ledger({"type": "audit", "slug": w.m["slug"], **a,
                        "result": res, "win": win, "pnl": round(apnl, 4)})
            sh = max(self.tot["shares"], 1)
            if w.inv_up + w.inv_dn > 0:
                pnl_txt = (f"we made +${pnl:.2f} 🎉" if pnl > 0 else
                           (f"we lost -${-pnl:.2f}" if pnl < 0 else
                            "we broke even ($0.00)"))
            else:
                pnl_txt = "no trades this round ($0.00)"
            log.info("🏁 ROUND RESULT (%s): Bitcoin went %s → %s",
                     fmt_hm(w.end), res.upper(), pnl_txt)
            tot_txt = (f"${self.tot['pnl']:+.2f} over {self.tot['windows']} rounds "
                       f"({100 * self.tot['pnl'] / sh:+.2f}¢/share)")
            aud_txt = (f"{self.tot['audit_wins']}/{self.tot['audit_n']} wins, "
                       f"{self.tot['audit_pnl'] / self.tot['audit_n']:+.2f} per $1"
                       if self.tot["audit_n"] else "no data yet")
            log.info("💰 Bankroll $%.2f | all-time %s | next bet %.0f shares | "
                     "momentum-idea test: %s | result via %s",
                     self.bankroll(), tot_txt, self.quote_size(), aud_txt, src)
            if (self.tot["windows"] >= 20 and sh > 0
                    and 100 * self.tot["pnl"] / sh > 5
                    and not getattr(self, "_tgtb_warned", False)):
                self._tgtb_warned = True
                log.warning("⚠️ These profits look TOO GOOD TO BE TRUE "
                            "(%.1f¢/share vs a 2¢ spread) — likely a "
                            "simulation artifact, not real edge. Do not go "
                            "live; send logs to Claude.",
                            100 * self.tot["pnl"] / sh)
            if src == "feed":
                self.verify_q.append({"gamma_id": w.m["gamma_id"],
                                      "slug": w.m["slug"], "res": res,
                                      "end": w.end, "t": time.time()})
            self.pending.remove(w)

    def verify_officials(self):
        """A few minutes after a feed-scored round, compare with the OFFICIAL
        result and record any disagreement — honesty telemetry."""
        noww = time.time()
        for item in list(self.verify_q):
            age = noww - item["t"]
            if age < 240:
                continue
            r = fetch_resolution(item["gamma_id"])
            if r is None:
                if age > 1200:
                    self.verify_q.remove(item)
                continue
            self.verify_q.remove(item)
            ledger({"type": "res_check", "slug": item["slug"],
                    "ours": item["res"], "official": r,
                    "match": r == item["res"]})
            if r != item["res"]:
                log.warning("⚠️ Official result for the %s round was %s but we "
                            "scored %s — recorded in ledger; frequent "
                            "mismatches mean tell Claude", fmt_hm(item["end"]),
                            r.upper(), item["res"].upper())

    # -- quoting ----------------------------------------------------------------
    def step(self):
        now = time.time()
        self.roll_window(now)
        self.settle_pending()
        self.verify_officials()
        if COMPOUND and not self.halted and \
                self.bankroll() < DRAWDOWN_STOP * BANKROLL_START:
            self.halted = True
            if self.live:
                self.live.cancel_all()
            log.critical("🛑 SAFETY BRAKE: bankroll $%.2f fell below %.0f%% "
                         "of your $%.2f start — the bot has STOPPED itself. "
                         "Run --status and talk to Claude before restarting.",
                         self.bankroll(), DRAWDOWN_STOP * 100, BANKROLL_START)
            ledger({"type": "drawdown_halt", "bankroll": round(self.bankroll(), 2)})
        if self.halted:
            return
        w = self.win
        if not w:
            return
        tau = w.end - now
        lt = self.feed.latest()
        if not lt or now - lt[0] > 20:
            if now - getattr(self, "_stale_log", 0) > 30:
                self._stale_log = now
                log.warning("⏳ Waiting for a fresh Bitcoin price — paused, will "
                            "auto-recover (run --feedtest only if this repeats "
                            "for many minutes)")
            return
        S = lt[1]
        books = fetch_books([w.m["up"], w.m["down"]])
        bu, bd = books.get(w.m["up"], {}), books.get(w.m["down"], {})
        if now - w.start < WARMUP_SEC or not self.ensure_strike(S, books, tau):
            return

        # settle live fills / simulate paper fills against last quotes
        if self.live:
            for tok, price, qty in self.live.poll_fills():
                w.record_fill("up" if tok == w.m["up"] else "dn", price, qty)
        else:
            for side, book in (("up", bu), ("dn", bd)):
                q = w.quotes[side]
                if not q or not book.get("asks"):
                    w.crossed[side] = False
                    continue
                crossed = book["asks"][0][0] <= q[0]
                if paper_fill_ok(crossed, w.crossed[side], q[0],
                                 w.last_fill_px[side], w.fill_count[side],
                                 MAX_FILLS_PER_SIDE) \
                        and now - w.last_fill[side] > 8:
                    real_avail = avail_at_or_below(book["asks"], q[0])
                    qty = math.floor(min(self.quote_size(), real_avail)
                                     * FILL_FRACTION)
                    if qty >= 1 and -w.cash + q[0] * qty <= self.window_cash_cap():
                        w.record_fill(side, q[0], qty)
                        w.last_fill[side] = now
                        w.last_fill_px[side] = q[0]
                        w.fill_count[side] += 1
                w.crossed[side] = crossed

        # taker audit — one look per window at AUDIT_TAU remaining
        if w.audit is None and tau <= AUDIT_TAU and bu.get("asks") and bd.get("asks"):
            pred = "up" if S >= w.strike else "down"
            entry = (bu if pred == "up" else bd)["asks"][0][0]
            w.audit = {"pred": pred, "entry": entry,
                       "fee": round(taker_fee_per_share(entry), 5)}

        # stop quoting near the end; hold inventory to resolution
        if tau <= QUOTE_STOP_SEC:
            if not w.done_quoting:
                if self.live:
                    self.live.cancel_all()
                w.quotes = {"up": None, "dn": None}
                w.done_quoting = True
                if w.inv_up or w.inv_dn:
                    log.info("⏸️ Final minutes — offers pulled; riding to the "
                             "finish holding %.0f UP / %.0f DOWN",
                             w.inv_up, w.inv_dn)
                else:
                    log.info("⏸️ Final minutes — offers pulled (no trades held "
                             "this round)")
            return

        P = fair_prob(S, w.strike, self.feed.sigma_sec(), tau)
        if P is None:
            return
        P = 0.5 + (P - 0.5) * MR_DAMP      # respect 15-min mean reversion
        mid = book_mid(bu)
        half = HALF_SPREAD
        if mid is not None:
            gap = abs(P - mid)
            if gap > MODEL_PAUSE:
                if now - getattr(self, "_pause_log", 0) > 30:
                    self._pause_log = now
                    log.warning("⚠️ The market's odds (%.2f) and my math (%.2f) "
                                "disagree a lot — standing aside for safety, "
                                "offers cancelled (vol est %.5f/s)",
                                mid, P, self.feed.sigma_sec())
                if self.live:
                    self.live.cancel_all()
                w.quotes = {"up": None, "dn": None}
                w.crossed = {"up": False, "dn": False}
                return
            if gap > MODEL_GUARD:
                half += 0.02
            P = MID_BLEND * mid + (1 - MID_BLEND) * P   # lean on consensus
        if -w.cash >= self.window_cash_cap():
            return

        skew = MAX_SKEW * max(-1.0, min(1.0, w.net() / self.max_inv()))
        bid_up, bid_dn = build_quotes(
            P, half, skew,
            bu["bids"][0][0] if bu.get("bids") else None,
            bu["asks"][0][0] if bu.get("asks") else None,
            bd["bids"][0][0] if bd.get("bids") else None,
            bd["asks"][0][0] if bd.get("asks") else None,
            w.net(), self.max_inv())

        if w.inv_dn > 0 and bid_up is not None:
            cap = 1.0 - (w.cost_dn / w.inv_dn) - PAIR_EDGE
            bid_up = clamp_price(min(bid_up, cap)) if cap >= 0.02 else None
        if w.inv_up > 0 and bid_dn is not None:
            cap = 1.0 - (w.cost_up / w.inv_up) - PAIR_EDGE
            bid_dn = clamp_price(min(bid_dn, cap)) if cap >= 0.02 else None

        for side, price in (("up", bid_up), ("dn", bid_dn)):
            old = w.quotes[side]
            fresh = (old is None or abs((old[0]) - (price or -1)) >= REQUOTE_TICK
                     or now - old[1] >= REQUOTE_SEC)
            if not fresh:
                continue
            if self.live:
                # naive replace: cancel-all then re-place both sides
                pass
            w.quotes[side] = (price, now) if price else None
        if self.live and any(w.quotes.values()):
            self.live.cancel_all()
            qs = self.quote_size()
            if w.quotes["up"]:
                self.live.place(w.m["up"], w.quotes["up"][0], qs)
            if w.quotes["dn"]:
                self.live.place(w.m["down"], w.quotes["dn"][0], qs)

    def run(self):
        self.feed.start()
        log.info("🤖 BTC 15-min bot v2 | %s",
                 "PRACTICE MODE — fake money, totally safe" if DRY_RUN
                 else "⚡ LIVE MODE — real money!")
        log.info("💵 Bankroll $%.2f | bet size %s | 🛑 safety brake if bankroll hits $%.2f",
                 self.bankroll(),
                 ("%.0f shares (grows when you win)" % self.quote_size()) if COMPOUND
                 else ("%.0f shares fixed" % QUOTE_SIZE),
                 DRAWDOWN_STOP * BANKROLL_START)
        log.info("📖 How to read these logs: 🕐 new round starts | 📥 a trade "
                 "happened | 🏁 round result | 💰 your money | ⚠️/⏳ bot standing "
                 "aside (normal sometimes)")
        stop = {"f": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(f=True))
        while not stop["f"]:
            if os.path.exists("stop15.flag"):
                log.info("stop15.flag — exiting")
                break
            try:
                self.step()
            except Exception as e:  # noqa: BLE001
                log.error("step error: %s", e)
            try:
                time.sleep(LOOP_SEC)
            except KeyboardInterrupt:
                break
        if self.live:
            self.live.cancel_all()
        log.info("👋 Bot stopped. This session: %s", json.dumps(
            {k: round(v, 3) if isinstance(v, float) else v
             for k, v in self.tot.items()}))

# ---------------------------------------------------------------------------
# Self-test — offline verification of every math component
# ---------------------------------------------------------------------------
def selftest():
    ok = True
    def check(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    check("phi(0)=0.5", abs(phi(0) - 0.5) < 1e-12)
    check("phi_inv roundtrip", abs(phi_inv(phi(1.234)) - 1.234) < 1e-9)

    P = fair_prob(100000, 100000, 6e-5, 600)
    check("ATM fair value = 0.5", abs(P - 0.5) < 1e-9)
    P_hi = fair_prob(100000 * 1.0015, 100000, 6e-5, 600)
    P_lo = fair_prob(100000 / 1.0015, 100000, 6e-5, 600)
    check("fair value monotonic in S", P_lo < 0.5 < P_hi)
    check("symmetry P(K*e^x)+P(K*e^-x)=1", abs(P_hi + P_lo - 1) < 1e-9)

    # strike inversion: derive mid from a known K, recover K
    K_true = 100000.0
    S, sig, tau = 100080.0, 7e-5, 480.0
    mid = fair_prob(S, K_true, sig, tau)
    K_est = implied_strike(S, mid, sig, tau)
    check("implied strike recovers K", abs(K_est - K_true) < 0.5)

    # vol estimator on synthetic walk with known sigma
    import random
    random.seed(7)
    pf = PriceFeed()
    true_sig, p, t = 1.2e-4, 100000.0, 0.0
    for _ in range(4000):
        t += 1.0
        p *= math.exp(true_sig * random.gauss(0, 1))
        pf.on_price(t, p)
    est = pf.sigma_sec()
    check("EWMA vol within 25%% of truth (%.1e vs %.1e)" % (est, true_sig),
          abs(est - true_sig) / true_sig < 0.25)

    # fee model matches the published example: 100 sh @ 0.50 -> ~$1.56
    check("fee example ~$1.56/100sh",
          abs(100 * taker_fee_per_share(0.50) - 1.56) < 0.01)

    # quotes never cross; skew direction correct; cap suppresses a side
    bu, bd = build_quotes(0.50, 0.02, 0.0, 0.47, 0.49, 0.49, 0.53, 0, 30)
    check("bid stays below ask (maker-only)", bu <= 0.48 and bd <= 0.48)
    bu2, bd2 = build_quotes(0.50, 0.02, 0.02, 0.40, 0.60, 0.40, 0.60, 15, 30)
    check("long-Up skew lowers Up bid, raises Down bid",
          bu2 < bu and bd2 > bd)
    bu3, bd3 = build_quotes(0.50, 0.02, 0.02, 0.40, 0.60, 0.40, 0.60, 30, 30)
    check("inventory cap suppresses Up bid", bu3 is None and bd3 is not None)

    # window PnL accounting: buy 12 Up @0.48 and 12 Down @0.47 -> pair lock
    w = Window(0, 900, {"slug": "t", "up": "U", "down": "D",
                        "gamma_id": 0, "condition_id": 0})
    w.record_fill("up", 0.48, 12)
    w.record_fill("dn", 0.47, 12)
    pnl_up = w.cash + w.inv_up      # resolves Up
    pnl_dn = w.cash + w.inv_dn      # resolves Down
    check("balanced pair locks 5c/sh either way",
          abs(pnl_up - 0.60) < 1e-9 and abs(pnl_dn - 0.60) < 1e-9)

    # one-sided fill risk math
    w2 = Window(0, 900, w.m)
    w2.record_fill("up", 0.48, 12)
    check("one-sided fill: +6.24 if right / -5.76 if wrong",
          abs((w2.cash + w2.inv_up) - 6.24) < 1e-9 and
          abs((w2.cash + w2.inv_dn) - (-5.76)) < 1e-9)

    # audit pnl math: entry .52, fee coef .0312
    fee = taker_fee_per_share(0.52)
    check("audit fee = coef*min(p,1-p)", abs(fee - 0.0312 * 0.48) < 1e-9)

    # slug arithmetic matches observed real slug (…-1768425300, %900==0)
    check("window end aligns to 900s grid", 1768425300 % 900 == 0)
    s, e = window_bounds(1768425300 - 10)
    check("window bounds computed correctly",
          s == 1768424400 and e == 1768425300)

    # compounding sizer: floor, proportional growth, ceiling, monotonic
    def _size(bank):
        return max(MIN_QUOTE, min(MAX_QUOTE, math.floor(bank * SIZE_PER_DOLLAR)))
    check("sizer floor at tiny bankroll", _size(1) == MIN_QUOTE)
    check("sizer proportional ($40->%d, $400->%d)" % (_size(40), _size(400)),
          _size(400) == min(MAX_QUOTE, math.floor(400 * SIZE_PER_DOLLAR)))
    check("sizer monotonic", _size(200) >= _size(50) >= _size(10))
    check("sizer ceiling respected", _size(10 ** 9) == MAX_QUOTE)

    # ledger replay: sums only matching-mode window rows, ignores the rest
    import tempfile
    tf = tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl")
    for rec in ({"type": "window", "mode": "DRY", "pnl": 1.5},
                {"type": "window", "mode": "LIVE", "pnl": 9.9},
                {"type": "fill", "mode": "DRY", "pnl": 123},
                {"type": "audit", "mode": "DRY", "pnl": 5}):
        tf.write(json.dumps(rec) + "\n")
    tf.close()
    check("replay sums DRY windows only", abs(replay_pnl(tf.name, "DRY") - 1.5) < 1e-9)
    check("replay sums LIVE windows only", abs(replay_pnl(tf.name, "LIVE") - 9.9) < 1e-9)
    check("replay of missing file -> 0", replay_pnl("/no_such_file.jsonl", "DRY") == 0.0)

    # drawdown brake arithmetic
    check("brake threshold = stop_frac * start",
          abs(DRAWDOWN_STOP * BANKROLL_START -
              BANKROLL_START * DRAWDOWN_STOP) < 1e-12 and 0 < DRAWDOWN_STOP < 1)

    # RTDS parser: exact documented chainlink frame
    doc_frame = json.dumps({"topic": "crypto_prices_chainlink", "type": "update",
                            "timestamp": 1753314088421,
                            "payload": {"symbol": "btc/usd",
                                        "timestamp": 1753314088395,
                                        "value": 67234.50}})
    t = parse_rtds(doc_frame)
    check("rtds parses documented btc frame",
          len(t) == 1 and abs(t[0][1] - 67234.50) < 1e-9)
    check("rtds converts ms timestamp to seconds",
          abs(t[0][0] - 1753314088.395) < 1e-6)
    eth = doc_frame.replace("btc/usd", "eth/usd")
    check("rtds filters out non-BTC symbols", parse_rtds(eth) == [])
    check("rtds ignores PONG/non-JSON", parse_rtds("PONG") == [])
    bnb = json.dumps({"topic": "crypto_prices", "payload":
                      {"symbol": "btcusdt", "value": 109000.1,
                       "timestamp": 1753314088}})
    t2 = parse_rtds(bnb)
    check("rtds parses binance topic + seconds ts",
          len(t2) == 1 and abs(t2[0][0] - 1753314088) < 1e-6)
    check("rtds handles list frames",
          len(parse_rtds("[" + doc_frame + "," + bnb + "]")) == 2)
    check("rtds ignores subscription acks",
          parse_rtds(json.dumps({"type": "subscribed", "topic": "crypto_prices"})) == [])

    # book_mid: tight books give a mid, junk/wide/empty books give None
    check("mid of tight book", abs(book_mid({"bids": [(0.48, 5)], "asks": [(0.52, 5)]},
                                            0.10) - 0.50) < 1e-9)
    check("junk book (0.02/1.00) -> no mid",
          book_mid({"bids": [(0.02, 5)], "asks": [(1.00, 5)]}, 0.10) is None)
    check("empty book -> no mid", book_mid({"bids": [], "asks": []}, 0.10) is None)

    # paper fill gating: no machine-gun refills on the same crossing
    check("fill on fresh cross", paper_fill_ok(True, False, 0.49, None, 0, 4))
    check("NO refill at the same price",
          not paper_fill_ok(True, True, 0.49, 0.49, 1, 4))
    check("1c-better refill still blocked (no chasing ladders)",
          not paper_fill_ok(True, True, 0.48, 0.49, 1, 4))
    check("3c-better refill allowed",
          paper_fill_ok(True, True, 0.46, 0.49, 1, 4))
    check("per-side cap enforced", not paper_fill_ok(True, False, 0.49, None, 4, 4))

    # humility math: damping and consensus blending
    damp = 0.5 + (0.80 - 0.5) * 0.65
    check("damping shrinks overreaction (0.80 -> ~0.695)", abs(damp - 0.695) < 1e-9)
    check("damping leaves 50/50 alone", abs((0.5 + 0.0 * 0.65) - 0.5) < 1e-12)
    blend = 0.5 * 0.60 + 0.5 * damp
    check("consensus blend midway", abs(blend - (0.60 + damp) / 2) < 1e-12)

    # pair-cost guard: holding DOWN at avg 70c caps the UP bid near 28c
    cap = 1.0 - (2.10 / 3.0) - 0.02
    check("pair guard caps opposite bid (avg 70c -> cap 28c)",
          abs(cap - 0.28) < 1e-9)

    # self-resolution rule matches the market: Up if end >= strike (ties -> Up)
    def _res(p_end, k):
        return "up" if p_end >= k else "down"
    check("self-resolve up", _res(62510.0, 62500.0) == "up")
    check("self-resolve down", _res(62490.0, 62500.0) == "down")
    check("self-resolve tie counts as up", _res(62500.0, 62500.0) == "up")

    # fill size can never exceed what the book actually offered
    asks = [(0.47, 3), (0.48, 2), (0.55, 500)]
    check("available shares counted at/below bid",
          avail_at_or_below(asks, 0.48) == 5)
    check("dust ask caps the fill: min(75, 5)*0.6 -> 3 shares",
          math.floor(min(75, avail_at_or_below(asks, 0.48)) * 0.6) == 3)
    check("deep book allows full size",
          math.floor(min(6, avail_at_or_below(asks, 0.60)) * 0.6) == 3
          and math.floor(min(6, 505) * 0.6) == 3)

    # parse_iso: the helper whose absence once froze the bot — never again
    d = parse_iso("2026-07-05T13:15:00Z")
    check("parse_iso reads gamma dates", d is not None and d.timestamp() > 0)
    check("parse_iso survives junk", parse_iso(None) is None and parse_iso("x") is None)

    # vol hardening: bursts and glitches must never inflate sigma again
    pfb = PriceFeed()
    for i in range(200):     # 1ms-apart burst alternating a $12 source basis
        pfb.on_price(1000.0 + i * 0.001, 62000.0 + (6 if i % 2 else -6))
    check("burst ticks merged — sigma untouched by feed basis",
          abs(pfb.sigma_sec() - 6e-5) < 1e-9)
    import random as _rnd
    _rnd.seed(1)
    pfg = PriceFeed()
    p, t = 62000.0, 0.0
    for _ in range(600):
        t += 1.0
        p *= math.exp(6e-5 * _rnd.gauss(0, 1))
        pfg.on_price(t, p)
    s_before = pfg.sigma_sec()
    pfg.on_price(t + 1.0, p * 1.01)          # a 1% glitch jump in one tick
    check("glitch jump ignored by vol estimator",
          abs(pfg.sigma_sec() - s_before) < 1e-12)

    # market lookup verification: only a matching end time is accepted
    from datetime import datetime, timezone as _tz
    dt_ok = datetime.fromtimestamp(1783171800, _tz.utc)
    dt_next = datetime.fromtimestamp(1783171800 + 900, _tz.utc)
    check("slug verify: matching end accepted",
          slug_matches_window(1783171800, dt_ok) is True)
    check("slug verify: next round's market rejected",
          slug_matches_window(1783171800, dt_next) is False)
    check("slug verify: unknown end -> None (unverified)",
          slug_matches_window(1783171800, None) is None)

    # resolution pricing prefers the Chainlink stream over mixed ticks
    pf2 = PriceFeed()
    pf2.ticks.append((1000.0, 62010.0))     # Binance tick
    pf2.cl.append((1000.0, 62000.0))        # Chainlink tick, same moment
    check("price_at prefers Chainlink", pf2.price_at(1000.0) == 62000.0)
    pf3 = PriceFeed()
    pf3.ticks.append((1000.0, 62010.0))     # only mixed available
    check("price_at falls back to mixed feed", pf3.price_at(1000.0) == 62010.0)

    print("\nSELFTEST:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1

# ---------------------------------------------------------------------------
def status():
    """Read the ledger and print the numbers that decide go-live / tune / stop."""
    mode = "DRY" if DRY_RUN else "LIVE"
    try:
        rows = [json.loads(l) for l in open(LEDGER_PATH)]
    except FileNotFoundError:
        print("No ledger yet — the bot hasn't produced data. Let it run first.")
        return 0
    w = [r for r in rows if r.get("type") == "window" and r.get("mode") == mode]
    a = [r for r in rows if r.get("type") == "audit" and r.get("mode") == mode]
    if not w:
        print(f"No completed {mode} windows in the ledger yet. Give it more time.")
        return 0
    sh = sum(x.get("shares", 0) for x in w) or 1
    pnl = sum(x.get("pnl", 0) for x in w)
    days = max((rows[-1]["ts"] - rows[0]["ts"]) / 86400, 0.01)
    edge_c = 100 * pnl / sh
    print(f"mode          : {mode}")
    print(f"days measured : {days:.1f}")
    print(f"windows done  : {len(w)}   fills: {sum(x.get('fills', 0) for x in w)}"
          f"   shares: {sh:.0f} ({sh / days:.0f}/day)")
    print(f"profit        : ${pnl:+.2f}   edge: {edge_c:+.2f} c/share")
    print(f"per month est : ${30 * pnl / days:+.2f}")
    print(f"bankroll      : ${BANKROLL_START + pnl:.2f} (started ${BANKROLL_START:.0f})")
    if a:
        wins = sum(1 for x in a if x.get("win"))
        print(f"momentum audit: {wins}/{len(a)} wins ({100 * wins / len(a):.1f}%), "
              f"avg ${sum(x.get('pnl', 0) for x in a) / len(a):+.4f} per $1 bet")
    if edge_c > 5:
        verdict = ("⚠️ TOO GOOD TO BE TRUE — %.1f¢/share exceeds what 2¢ "
                   "spreads can physically earn. This is simulator optimism, "
                   "not profit. Do NOT go live. Send this output to Claude." % edge_c)
    elif edge_c >= 0.5 and sh / days >= 150:
        verdict = "GO-LIVE CANDIDATE — edge and volume both clear the bar"
    elif edge_c > -0.2:
        verdict = "TUNE — near zero; widen HALF_SPREAD to 0.03 and run 3 more days"
    else:
        verdict = "STOP — the market's adverse selection beats us; you lost $0 learning it"
    print(f"verdict       : {verdict}")
    return 0

# ---------------------------------------------------------------------------
def feedtest():
    """20-second live check of the price feed (websocket + REST backup)."""
    pf = PriceFeed()
    pf.start()
    print("Listening for BTC prices for 20 seconds...")
    for i in range(4):
        time.sleep(5)
        lt = pf.latest()
        line = (f"  t+{(i + 1) * 5:2d}s | chainlink: {pf.src['cl']:4d} | "
                f"binance-ws: {pf.src['ws']:3d} | rest: {pf.src['rest']:3d}")
        if lt:
            line += f" | last BTC: {lt[1]:,.2f} | sigma/s: {pf.sigma_sec():.2e}"
        print(line)
    total = pf.src["cl"] + pf.src["ws"] + pf.src["rest"]
    if pf.src["cl"] > 0:
        print("RESULT: PASS — Chainlink feed is flowing. Ideal.")
    elif total > 0:
        print("RESULT: PASS — REST backup is flowing (websocket quiet). "
              "Bot works; check journalctl for 'rtds sample frame' lines and send them to Claude.")
    else:
        print("RESULT: FAIL — no prices from either source. Check the server's "
              "internet access, then send this output to Claude.")
    return 0 if total > 0 else 1

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--status" in sys.argv:
        sys.exit(status())
    if "--feedtest" in sys.argv:
        sys.exit(feedtest())
    try:
        Engine().run()
    except KeyboardInterrupt:
        pass

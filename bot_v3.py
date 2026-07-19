#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_v3.py — Weather Trading Bot for Polymarket (HONEST EDITION)
================================================================
Fork of alteregoeth-ai/weatherbot bot_v2 (MIT) with five fixes that make
the paper results mean something:

  1. PROBABILITY BUG FIXED — v2 line 107 gave p=1.0 (certainty!) to any
     bucket containing the point forecast. v3 uses ~80 real ensemble
     members (ECMWF/GFS/ICON) so forecast uncertainty is IN the number.
  2. PRICE PARSING BUG FIXED — v2 read gamma's outcomePrices [YES,NO]
     as [bid,ask], so "ask" was actually the NO price. v3 uses real
     bestBid/bestAsk. (In v2 this bug and bug #1 cancelled each other.)
  3. HONEST FILLS — entries walk the real CLOB order book (read-only,
     no key): partial fills, depth caps, fees, and a two-touch latency
     gate. No more infinite liquidity at the displayed price.
  4. STOP-LOSS REMOVED — a 20% stop on an 8c position fired on 1.6c of
     normal spread bounce. Replaced with an EDGE EXIT (leave when our
     own p drops below the ask) and a BUCKET-BUSTED guard (daily max
     already above bucket ceiling per METAR -> position is dead, exit).
  5. TRUTH METRICS — every resolution records Brier scores for the bot
     vs the market price. If brier_bot >= brier_market, the bot knows
     less than the price and no execution tuning can save it.

Requires ensemble_prob.py and honest_fills.py in the same directory.
Still 100% paper trading: no wallet, no key, cannot place real orders.

Usage:
    python bot_v3.py          # main loop
    python bot_v3.py report   # full report
    python bot_v3.py status   # balance and open positions
"""

import re
import sys
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from ensemble_prob import fetch_ensemble, member_daily_maxes, bucket_probability, summarize
    from honest_fills import get_book, simulate_buy, simulate_sell, mark_to_bid, PendingSignals
except ImportError as e:
    sys.exit(f"bot_v3 needs ensemble_prob.py and honest_fills.py beside it ({e})")

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE          = _cfg.get("balance", 10000.0)
MAX_BET          = _cfg.get("max_bet", 20.0)        # max bet per trade
MIN_EV           = _cfg.get("min_ev", 0.10)
MAX_PRICE        = _cfg.get("max_price", 0.45)
MIN_VOLUME       = _cfg.get("min_volume", 500)
MIN_HOURS        = _cfg.get("min_hours", 2.0)
MAX_HOURS        = _cfg.get("max_hours", 72.0)
KELLY_FRACTION   = _cfg.get("kelly_fraction", 0.25)
MAX_SLIPPAGE     = _cfg.get("max_slippage", 0.03)  # max allowed ask-bid spread
SCAN_INTERVAL    = _cfg.get("scan_interval", 3600)   # every hour
CALIBRATION_MIN  = _cfg.get("calibration_min", 30)
VC_KEY           = _cfg.get("vc_key", "")
KERNEL_SIGMA     = _cfg.get("kernel_sigma", 1.0)    # deg F dressing per member
MAX_BOOK_FRAC    = _cfg.get("max_book_frac", 0.10)  # never take >10% of depth
FEE_BPS          = _cfg.get("fee_bps", 0)           # taker fee, basis points

SIGMA_F = 2.0
SIGMA_C = 1.2

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"
PENDING          = PendingSignals(str(DATA_DIR / "pending_signals.json"))

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    """FALLBACK point-forecast probability (used only if ensemble fetch fails).
    v2 BUG FIXED: middle buckets returned 1.0 whenever the forecast landed
    inside them — betting coin flips as certainties. Now every bucket gets
    a real probability from the normal CDF. Reported temps are integers,
    so bucket [56..57] covers the continuous range [55.5, 57.5)."""
    s = max(sigma or 2.0, 0.1)
    f = float(forecast)
    lo = -1e9 if t_low == -999 else (t_low - 0.5)
    hi = 1e9 if t_high == 999 else (t_high + 0.5)
    return norm_cdf((hi - f) / s) - norm_cdf((lo - f) / s)

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1: return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}

def load_cal():
    if CALIBRATION_FILE.exists():
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    return {}

def get_sigma(city_slug, source="ecmwf"):
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C

def run_calibration(markets):
    """Recalculates sigma from resolved markets."""
    resolved = [m for m in markets if m.get("resolved") and m.get("actual_temp") is not None]
    cal = load_cal()
    updated = []

    for source in ["ecmwf", "hrrr", "metar"]:
        for city in set(m["city"] for m in resolved):
            group = [m for m in resolved if m["city"] == city]
            errors = []
            for m in group:
                snap = next((s for s in reversed(m.get("forecast_snapshots", []))
                             if s["source"] == source), None)
                if snap and snap.get("temp") is not None:
                    errors.append(abs(snap["temp"] - m["actual_temp"]))
            if len(errors) < CALIBRATION_MIN:
                continue
            mae  = sum(errors) / len(errors)
            key  = f"{city}_{source}"
            old  = cal.get(key, {}).get("sigma", SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C)
            new  = round(mae, 3)
            cal[key] = {"sigma": new, "n": len(errors), "updated_at": datetime.now(timezone.utc).isoformat()}
            if abs(new - old) > 0.05:
                updated.append(f"{LOCATIONS[city]['name']} {source}: {old:.2f}->{new:.2f}")

    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal

# =============================================================================
# FORECASTS
# =============================================================================

def get_ecmwf(city_slug, dates):
    """ECMWF via Open-Meteo with bias correction. For all cities."""
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=ecmwf_ifs025&bias_correction=true"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [ECMWF] {city_slug}: {e}")
    return result

def get_hrrr(city_slug, dates):
    """HRRR via Open-Meteo. US cities only, up to 48h horizon."""
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&forecast_days=3&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=gfs_seamless"  # HRRR+GFS seamless — best option for US
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [HRRR] {city_slug}: {e}")
    return result

def get_metar(city_slug):
    """Current observed temperature from METAR station. D+0 only."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        data = requests.get(url, timeout=(5, 8)).json()
        if data and isinstance(data, list):
            temp_c = data[0].get("temp")
            if temp_c is not None:
                if unit == "F":
                    return round(float(temp_c) * 9/5 + 32)
                return round(float(temp_c), 1)
    except Exception as e:
        print(f"  [METAR] {city_slug}: {e}")
    return None

def get_actual_temp(city_slug, date_str):
    """Actual temperature via Visual Crossing for closed markets."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={VC_KEY}&include=days&elements=tempmax"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        days = data.get("days", [])
        if days and days[0].get("tempmax") is not None:
            return round(float(days[0]["tempmax"]), 1)
    except Exception as e:
        print(f"  [VC] {city_slug} {date_str}: {e}")
    return None

def check_market_resolved(market_id):
    """
    Checks if the market closed on Polymarket and who won.
    Returns: None (still open), True (YES won), False (NO won)
    """
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        closed = data.get("closed", False)
        if not closed:
            return None
        # Check YES price — if ~1.0 then WIN, if ~0.0 then LOSS
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True   # WIN
        elif yes_price <= 0.05:
            return False  # LOSS
        return None  # not yet determined
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None

# =============================================================================
# POLYMARKET
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def get_market_price(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def parse_temp_range(question):
    if not question: return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m: return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def in_bucket(forecast, t_low, t_high):
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high

# =============================================================================
# MARKET DATA STORAGE
# Each market is stored in a separate file: data/markets/{city}_{date}.json
# =============================================================================

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    p.write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return markets

def new_market(city_slug, date_str, event, hours):
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",           # open | closed | resolved
        "position":           None,             # filled when position opens
        "actual_temp":        None,             # filled after resolution
        "resolved_outcome":   None,             # win / loss / no_position
        "pnl":                None,
        "forecast_snapshots": [],               # list of forecast snapshots
        "market_snapshots":   [],               # list of market price snapshots
        "all_outcomes":       [],               # all market buckets
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE (balance and open positions)
# =============================================================================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates):
    """Fetches forecasts from all sources and returns a snapshot."""
    now_str = datetime.now(timezone.utc).isoformat()
    ecmwf   = get_ecmwf(city_slug, dates)
    hrrr    = get_hrrr(city_slug, dates)
    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        snap = {
            "ts":    now_str,
            "ecmwf": ecmwf.get(date),
            "hrrr":  hrrr.get(date) if date <= (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d") else None,
            "metar": get_metar(city_slug) if date == today else None,
        }
        # Best forecast: HRRR for US D+0/D+1, otherwise ECMWF
        loc = LOCATIONS[city_slug]
        if loc["region"] == "us" and snap["hrrr"] is not None:
            snap["best"] = snap["hrrr"]
            snap["best_source"] = "hrrr"
        elif snap["ecmwf"] is not None:
            snap["best"] = snap["ecmwf"]
            snap["best_source"] = "ecmwf"
        else:
            snap["best"] = None
            snap["best_source"] = None
        snapshots[date] = snap
    return snapshots

def scan_and_update():
    """Main function of one cycle: updates forecasts, opens/closes positions."""
    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = state["balance"]
    new_pos  = 0
    closed   = 0
    resolved = 0

    for city_slug, loc in LOCATIONS.items():
        unit = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
            snapshots = take_forecast_snapshot(city_slug, dates)
            time.sleep(0.3)
        except Exception as e:
            print(f"skipped ({e})")
            continue

        # --- ENSEMBLE: one fetch per city, ~80 member forecasts, all dates ---
        # Kernel sigma is quoted in deg F; shrink it for Celsius cities.
        kernel = KERNEL_SIGMA if unit == "F" else round(KERNEL_SIGMA * 5.0 / 9.0, 2)
        ens_maxes = {}
        try:
            ens = fetch_ensemble(loc["lat"], loc["lon"], days=4, unit=unit)
            for d in dates:
                ens_maxes[d] = member_daily_maxes(ens, d)
        except Exception as e:
            print(f"[ens fail: {e}]", end=" ")   # falls back to fixed bucket_prob

        for i, date in enumerate(dates):
            dt    = datetime.strptime(date, "%Y-%m-%d")
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)
            if not event:
                continue

            end_date = event.get("endDate", "")
            hours    = hours_to_resolution(end_date) if end_date else 0
            horizon  = f"D+{i}"

            # Load or create market record
            mkt = load_market(city_slug, date)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours)

            # Skip if market already resolved
            if mkt["status"] == "resolved":
                continue

            # Update outcomes list — prices taken directly from event
            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                # v2 BUG FIXED: outcomePrices is [YES price, NO price], NOT
                # [bid, ask]. v2's "ask" was really the NO price — the only
                # reason it ever traded was that p=1.0 beat any price.
                try:
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    yes_price = float(prices[0])
                except Exception:
                    continue
                bb, ba = market.get("bestBid"), market.get("bestAsk")
                bid = float(bb) if bb not in (None, "") else yes_price
                ask = float(ba) if ba not in (None, "") else yes_price
                # CLOB token id of the YES side — needed to read the real
                # order book. Order matches the "outcomes" field; these
                # bucket markets are ["Yes","No"], so index 0. Verify if
                # outcomes says otherwise.
                token_id = None
                try:
                    toks = json.loads(market.get("clobTokenIds", "[]"))
                    outs = json.loads(market.get("outcomes", '["Yes","No"]'))
                    yes_i = outs.index("Yes") if "Yes" in outs else 0
                    token_id = toks[yes_i] if len(toks) > yes_i else None
                except Exception:
                    pass
                outcomes.append({
                    "question":  question,
                    "market_id": mid,
                    "token_id":  token_id,
                    "range":     rng,
                    "bid":       round(bid, 4),
                    "ask":       round(ask, 4),
                    "price":     round(bid, 4),   # for compatibility
                    "spread":    round(ask - bid, 4),
                    "volume":    round(volume, 0),
                })

            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            # Forecast snapshot
            snap = snapshots.get(date, {})
            forecast_snap = {
                "ts":          snap.get("ts"),
                "horizon":     horizon,
                "hours_left":  round(hours, 1),
                "ecmwf":       snap.get("ecmwf"),
                "hrrr":        snap.get("hrrr"),
                "metar":       snap.get("metar"),
                "best":        snap.get("best"),
                "best_source": snap.get("best_source"),
            }
            mkt["forecast_snapshots"].append(forecast_snap)

            # Persist the day's RUNNING MAX (METAR gives current temp only;
            # the max so far is ours to track). Final max can only be >= it.
            if snap.get("metar") is not None:
                rm_city = state.setdefault("metar_max", {}).setdefault(city_slug, {})
                prev = rm_city.get(date)
                rm_city[date] = snap["metar"] if prev is None else max(prev, snap["metar"])

            # Market price snapshot
            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            market_snap = {
                "ts":       snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            }
            mkt["market_snapshots"].append(market_snap)

            forecast_temp = snap.get("best")
            best_source   = snap.get("best_source")

            # --- EDGE EXIT (replaces v2's stop-loss, trailing stop, and
            # forecast-shift close). A 20% stop on an 8c share fires on
            # 1.6c of normal spread bounce — v2 paid the spread twice to
            # lock in noise. v3 exits for exactly one reason: our OWN
            # probability fell below the ask, i.e. the edge is gone.
            # (A forecast shift lowers p, so the old rule is subsumed.)
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                cur = next((o for o in outcomes
                            if o["market_id"] == pos["market_id"]), None)
                if cur:
                    rmax = state.get("metar_max", {}).get(city_slug, {}).get(date)
                    maxes = ens_maxes.get(date) or []
                    if maxes:
                        p_now = bucket_probability(maxes, pos["bucket_low"],
                                                   pos["bucket_high"],
                                                   sigma=kernel, running_max=rmax)
                    elif forecast_temp is not None:
                        p_now = bucket_prob(forecast_temp, pos["bucket_low"],
                                            pos["bucket_high"],
                                            get_sigma(city_slug, best_source or "ecmwf"))
                    else:
                        p_now = None
                    pos["p_now"] = p_now

                    if p_now is not None and p_now < cur["ask"]:
                        # Edge gone — exit into the REAL bids, walking depth
                        proceeds, exit_px = None, None
                        if pos.get("token_id"):
                            book = get_book(pos["token_id"])
                            s = simulate_sell(book, pos["shares"], fee_bps=FEE_BPS) if book else None
                            if s and s["sold"] > 0:
                                proceeds = round(s["proceeds"] - s["fee"], 2)
                                exit_px  = s["avg_price"]
                        if proceeds is None:          # book unavailable: displayed bid
                            exit_px  = cur["bid"]
                            proceeds = round(exit_px * pos["shares"], 2)
                        pnl = round(proceeds - pos["cost"], 2)
                        balance += pos["cost"] + pnl
                        pos["closed_at"]    = snap.get("ts")
                        pos["close_reason"] = "edge_gone"
                        pos["exit_price"]   = exit_px
                        pos["pnl"]          = pnl
                        pos["status"]       = "closed"
                        closed += 1
                        print(f"  [EXIT] {loc['name']} {date} | p={p_now:.2f} < ask ${cur['ask']:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- OPEN POSITION (v3) ---
            # v2 only priced the ONE bucket containing the point forecast.
            # With honest probabilities the +EV bucket is often a NEIGHBOR
            # (e.g. p=0.14 at 4c), so v3 scans EVERY bucket and takes the
            # best expected value after real prices.
            have_ens = bool(ens_maxes.get(date))
            if not mkt.get("position") and hours >= MIN_HOURS and (have_ens or forecast_temp is not None):
                sigma = get_sigma(city_slug, best_source or "ecmwf")
                rmax  = state.get("metar_max", {}).get(city_slug, {}).get(date)
                best_signal, best_ev = None, MIN_EV

                for o in outcomes:
                    t_low, t_high = o["range"]
                    if o["volume"] < MIN_VOLUME:
                        continue
                    ask, bid = o["ask"], o["bid"]
                    if ask <= 0.01 or ask >= MAX_PRICE:
                        continue
                    if (ask - bid) > MAX_SLIPPAGE:
                        continue
                    if have_ens:
                        p = bucket_probability(ens_maxes[date], t_low, t_high,
                                               sigma=kernel, running_max=rmax)
                        p_source = "ens"
                    else:
                        p = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        p_source = best_source or "ecmwf"
                    ev = calc_ev(p, ask)
                    if ev >= best_ev:
                        best_ev, best_signal = ev, {
                            "market_id":    o["market_id"],
                            "question":     o["question"],
                            "token_id":     o.get("token_id"),
                            "bucket_low":   t_low,
                            "bucket_high":  t_high,
                            "bid_at_entry": bid,
                            "ask_at_signal": ask,
                            "spread":       o["spread"],
                            "p":            round(p, 4),
                            "p_source":     p_source,
                            "ens":          summarize(ens_maxes[date]) if have_ens else None,
                            "kelly":        round(calc_kelly(p, ask), 4),
                            "forecast_temp": forecast_temp,
                            "forecast_src": p_source,
                            "sigma":        kernel if have_ens else sigma,
                        }

                if best_signal:
                    size = bet_size(best_signal["kelly"], balance)
                    key  = f"{best_signal['market_id']}:{best_signal['bucket_low']}:{best_signal['bucket_high']}"
                    if size < 0.50:
                        pass
                    elif best_signal["token_id"] is None:
                        print(f"  [SKIP] {loc['name']} {date} — no token id, can't verify book")
                    elif not PENDING.confirm(key, max_age_s=int(2.5 * SCAN_INTERVAL)):
                        # TWO-TOUCH: a price must survive one full scan before
                        # we may fill — one-snapshot prices are latency mirages.
                        print(f"  [WAIT] {loc['name']} {date} | p={best_signal['p']:.2f} "
                              f"ask ${best_signal['ask_at_signal']:.3f} | confirm next scan")
                    else:
                        book = get_book(best_signal["token_id"])
                        fill = simulate_buy(book, size, limit_price=MAX_PRICE,
                                            max_book_frac=MAX_BOOK_FRAC,
                                            fee_bps=FEE_BPS) if book else None
                        ev_at_fill = (calc_ev(best_signal["p"], fill["avg_price"])
                                      if fill and fill["shares"] > 0 else -1)
                        if not fill or fill["shares"] <= 0 or fill["filled_frac"] < 0.5:
                            print(f"  [SKIP] {loc['name']} {date} — book too thin for ${size:.2f}")
                        elif ev_at_fill < MIN_EV:
                            print(f"  [SKIP] {loc['name']} {date} — EV died walking the book "
                                  f"(${fill['avg_price']:.3f})")
                        else:
                            best_signal.update({
                                "entry_price": fill["avg_price"],
                                "shares":      fill["shares"],
                                "cost":        round(fill["spent"] + fill["fee"], 2),
                                "fee":         fill["fee"],
                                "filled_frac": fill["filled_frac"],
                                "ev":          round(ev_at_fill, 4),
                                "opened_at":   snap.get("ts"),
                                "status":      "open",
                                "pnl":         None,
                                "exit_price":  None,
                                "close_reason": None,
                                "closed_at":   None,
                            })
                            balance -= best_signal["cost"]
                            mkt["position"] = best_signal
                            state["total_trades"] += 1
                            new_pos += 1
                            bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                            print(f"  [BUY]  {loc['name']} {horizon} {date} | {bucket_label} | "
                                  f"${best_signal['entry_price']:.3f} x {best_signal['shares']} "
                                  f"(fill {best_signal['filled_frac']:.0%}) | p={best_signal['p']:.2f} "
                                  f"EV {best_signal['ev']:+.2f} | ${best_signal['cost']:.2f} "
                                  f"({best_signal['forecast_src'].upper()})")

            # Market closed by time
            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # --- AUTO-RESOLUTION ---
    for mkt in load_all_markets():
        if mkt["status"] == "resolved":
            continue

        pos = mkt.get("position")
        if not pos or pos.get("status") != "open":
            continue

        market_id = pos.get("market_id")
        if not market_id:
            continue

        # Check if market closed on Polymarket
        won = check_market_resolved(market_id)
        if won is None:
            continue  # market still open

        # Market closed — record result
        price  = pos["entry_price"]
        size   = pos["cost"]
        shares = pos["shares"]
        pnl    = round(shares * (1 - price), 2) if won else round(-size, 2)

        balance += size + pnl
        # TRUTH METRICS: Brier score (lower = better calibrated).
        # brier_bot uses our p at entry; brier_market uses the price we
        # paid (the market's own probability). If mean(brier_bot) is not
        # clearly BELOW mean(brier_market) over the sample, the bot knows
        # less than the price does — no execution tuning can save it.
        outcome = 1.0 if won else 0.0
        if pos.get("p") is not None:
            pos["brier_bot"]    = round((pos["p"] - outcome) ** 2, 4)
            pos["brier_market"] = round((pos["entry_price"] - outcome) ** 2, 4)
        pos["exit_price"]   = 1.0 if won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = pnl
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if won else "loss"

        if won:
            state["wins"] += 1
        else:
            state["losses"] += 1

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        resolved += 1

        save_market(mkt)
        time.sleep(0.3)

    state["balance"]      = round(balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), balance)
    save_state(state)

    # Run calibration if enough data collected
    all_mkts = load_all_markets()
    resolved_count = len([m for m in all_mkts if m["status"] == "resolved"])
    if resolved_count >= CALIBRATION_MIN:
        global _cal
        _cal = run_calibration(all_mkts)

    return new_pos, closed, resolved

# =============================================================================
# REPORT
# =============================================================================

def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}" if total else "  No trades yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"

            # Current price from latest market snapshot
            current_price = pos["entry_price"]
            snaps = m.get("market_snapshots", [])
            if snaps:
                # Find our bucket price in all_outcomes
                for o in m.get("all_outcomes", []):
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {pos['forecast_src'].upper()}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — FULL REPORT")
    print(f"{'='*55}")

    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins      = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses    = [m for m in resolved if m["resolved_outcome"] == "loss"]

    print(f"\n  Total resolved: {len(resolved)}")
    print(f"  Wins:           {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate:       {len(wins)/len(resolved):.0%}")
    print(f"  Total PnL:      {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m["city"] for m in resolved)):
        group = [m for m in resolved if m["city"] == city]
        w     = len([m for m in group if m["resolved_outcome"] == "win"])
        pnl   = sum(m["pnl"] for m in group)
        name  = LOCATIONS[city]["name"]
        print(f"    {name:<16} {w}/{len(group)} ({w/len(group):.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Market details:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        result   = m["resolved_outcome"].upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {result} {pnl_str}")

    print(f"{'='*55}\n")

# =============================================================================
# MAIN LOOP
# =============================================================================

MONITOR_INTERVAL = 600  # monitor positions every 10 minutes

def monitor_positions():
    """Between scans: refresh honest marks and fire the bucket-busted guard."""
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]

        # Fetch real bestBid from Polymarket API — actual sell price
        current_price = None
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
            mdata = r.json()
            best_bid = mdata.get("bestBid")
            if best_bid is not None:
                current_price = float(best_bid)
        except Exception:
            pass

        # Fallback to cached price if API failed
        if current_price is None:
            for o in mkt.get("all_outcomes", []):
                if o["market_id"] == mid:
                    current_price = o.get("bid", o["price"])
                    break

        if current_price is None:
            continue

        city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])
        pos["last_bid"] = current_price

        # Honest mark: what the real bids would pay for our shares right now
        if pos.get("token_id"):
            book = get_book(pos["token_id"])
            if book:
                pos["mark_value"] = round(mark_to_bid(book, pos["shares"]), 2)

        # BUCKET-BUSTED GUARD (v3): the daily max only rises. If the
        # observed running max has already cleared our bucket's ceiling,
        # we are holding a guaranteed zero — exit into real bids NOW
        # instead of watching it grind to nothing at resolution.
        rmax = state.get("metar_max", {}).get(mkt["city"], {}).get(mkt["date"])
        busted = (rmax is not None and pos["bucket_high"] != 999
                  and rmax > pos["bucket_high"] + 0.5)
        if busted:
            proceeds, exit_px = None, None
            if pos.get("token_id"):
                book = get_book(pos["token_id"])
                s = simulate_sell(book, pos["shares"], fee_bps=FEE_BPS) if book else None
                if s and s["sold"] > 0:
                    proceeds = round(s["proceeds"] - s["fee"], 2)
                    exit_px  = s["avg_price"]
            if proceeds is None:
                exit_px  = current_price
                proceeds = round(current_price * pos["shares"], 2)
            pnl = round(proceeds - pos["cost"], 2)
            balance += pos["cost"] + pnl
            pos["closed_at"]    = datetime.now(timezone.utc).isoformat()
            pos["close_reason"] = "bucket_busted"
            pos["exit_price"]   = exit_px
            pos["pnl"]          = pnl
            pos["status"]       = "closed"
            closed += 1
            print(f"  [BUSTED] {city_name} {mkt['date']} | running max {rmax} > "
                  f"bucket top {pos['bucket_high']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        save_market(mkt)   # persist marks even when nothing closed

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    return closed


def run_loop():
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET v3 — HONEST MODE")
    print(f"{'='*55}")
    print(f"  Cities:     {len(LOCATIONS)}")
    print(f"  Balance:    ${BALANCE:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    Ensemble(~80 members) + METAR busted-guard + real order-book fills")
    print(f"  Data:       {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    last_full_scan = 0

    while True:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Full scan once per hour
        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, resolved = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                last_full_scan = time.time()
            except KeyboardInterrupt:
                print(f"\n  Stopping — saving state...")
                save_state(load_state())
                print(f"  Done. Bye!")
                break
            except requests.exceptions.ConnectionError:
                print(f"  Connection lost — waiting 60 sec")
                time.sleep(60)
                continue
            except Exception as e:
                print(f"  Error: {e} — waiting 60 sec")
                time.sleep(60)
                continue
        else:
            # Quick stop monitoring
            print(f"[{now_str}] monitoring positions...")
            try:
                stopped = monitor_positions()
                if stopped:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                print(f"  Monitor error: {e}")

        try:
            time.sleep(MONITOR_INTERVAL)
        except KeyboardInterrupt:
            print(f"\n  Stopping — saving state...")
            save_state(load_state())
            print(f"  Done. Bye!")
            break

# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_loop()
    elif cmd == "status":
        _cal = load_cal()
        print_status()
    elif cmd == "report":
        _cal = load_cal()
        print_report()
    else:
        print("Usage: python weatherbet.py [run|status|report]")

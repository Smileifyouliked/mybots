"""
ensemble_prob.py — honest bucket probabilities for weather markets
==================================================================
Replaces the fatal flaw in bot_v2.py line 107, where any bucket
containing the point forecast got probability 1.0 (total certainty).

Idea in one paragraph, beginner version:
A weather model doesn't know the future — so instead of one forecast,
agencies run the SAME model ~30-50 times with tiny variations in the
starting conditions. Each run is a "member". If all 50 members say the
Chicago max is 56-58F, the outcome is nearly certain. If they're spread
from 52F to 63F, ANY 2-degree bucket is a long shot — even the one the
average lands in. The fraction of members that land inside a bucket IS
the honest probability. That spread is exactly the "1-2 degrees off"
you experienced live: it was always there, the bot just ignored it.

Only stdlib + requests. No keys. Read-only. Nothing to steal here.
"""

import math
import sys
import requests

# Open-Meteo's free ensemble endpoint (no API key needed).
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Ensemble systems to blend. ~80+ members total across these three.
# If one name ever 404s (Open-Meteo occasionally renames), just remove
# it here — the code works with whatever comes back.
MODELS = "ecmwf_ifs025,gfs_seamless,icon_seamless"


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_ensemble(lat, lon, days=4, unit="F", models=MODELS, timeout=(5, 30)):
    """
    Fetch hourly 2m-temperature for EVERY ensemble member at one point.
    unit: "F" or "C" — match the unit the market's buckets are quoted in.

    Returns: {"times": [...local ISO strings...],
              "members": [[temps for member 0], [member 1], ...]}  (deg F)

    IMPORTANT: pass the RESOLUTION STATION's coordinates (the airport,
    e.g. LaGuardia for NYC), never the city center — markets resolve on
    the station reading and the difference can be 3-8F.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "models": models,
        "forecast_days": days,
        "temperature_unit": "fahrenheit" if unit.upper().startswith("F") else "celsius",
        "timezone": "auto",          # times come back in STATION-local time
    }
    r = requests.get(ENSEMBLE_URL, params=params, timeout=timeout)
    r.raise_for_status()
    hourly = r.json().get("hourly", {})

    times = hourly.get("time", [])
    members = []
    # Member series arrive as keys like temperature_2m_member01 (the exact
    # suffix scheme varies per model) — so we grab every key that starts
    # with "temperature_2m". Each one is a full member time-series.
    for key, series in hourly.items():
        if key.startswith("temperature_2m") and isinstance(series, list):
            members.append(series)

    if not times or not members:
        raise RuntimeError("Ensemble response had no members — check model "
                           "names against open-meteo.com/en/docs/ensemble-api")
    return {"times": times, "members": members}


def member_daily_maxes(data, date_str, after_hour=None):
    """
    For one local calendar date ("2026-07-11"), return each member's max
    temperature over that date's hours -> a list like [57.2, 58.9, 55.1, ...]

    after_hour: if it's ALREADY 14:00 at the station, pass 14 to use only
    the REMAINING hours. You then feed the observed running max separately
    to bucket_probability(running_max=...) — the past is no longer
    uncertain, so members shouldn't get credit or blame for it.
    """
    idx = []
    for i, t in enumerate(data["times"]):
        if not t.startswith(date_str):
            continue
        if after_hour is not None and int(t[11:13]) < after_hour:
            continue
        idx.append(i)
    if not idx:
        return []

    maxes = []
    for series in data["members"]:
        vals = [series[i] for i in idx
                if i < len(series) and series[i] is not None]
        if vals:
            maxes.append(max(vals))
    return maxes


# ---------------------------------------------------------------------------
# Probability
# ---------------------------------------------------------------------------

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_probability(member_maxes, t_low, t_high,
                       sigma=1.0, bias=0.0, running_max=None):
    """
    P(reported daily max lands in bucket [t_low..t_high], integer deg F).

    Same sentinels as bot_v2.py: t_low == -999 means "t_high or below",
    t_high == 999 means "t_low or above".

    How it works:
      * Each member's max is one plausible future. But even a perfect
        member is a smooth grid value, not the jumpy station sensor — so
        we "dress" each member in a small bell curve (sigma, default
        1.0F) and integrate the bucket's slice of it. This also stops
        p=0.00 lies for near-miss buckets.
      * bias: learned station correction (model minus reality). If the
        model runs 0.8F warm at KLGA, pass bias=-0.8. Feed it from the
        repo's own calibration data once you have ~30 resolutions.
      * running_max: today's observed max SO FAR from METAR. The final
        max can only be >= it, so each member becomes
        max(member, running_max). A bucket the day has already blown
        past collapses to ~0 automatically; "X or above" buckets that
        are already locked collapse to ~1. Free money-saver.

    Reported temps are integers, so bucket [56..57] really covers the
    continuous range [55.5, 57.5) — hence the +/- 0.5 below.
    """
    if not member_maxes:
        return 0.0

    lo_edge = -1e9 if t_low == -999 else (t_low - 0.5)
    hi_edge = 1e9 if t_high == 999 else (t_high + 0.5)
    s = max(sigma, 0.1)  # never divide by ~zero

    total = 0.0
    for m in member_maxes:
        m_adj = m + bias
        if running_max is not None:
            m_adj = max(m_adj, running_max)
        total += _norm_cdf((hi_edge - m_adj) / s) - _norm_cdf((lo_edge - m_adj) / s)
    return round(total / len(member_maxes), 4)


def summarize(member_maxes):
    """Mean / spread / 10th-90th percentile — log this with every trade
    so you can SEE the uncertainty you're betting into."""
    if not member_maxes:
        return {}
    xs = sorted(member_maxes)
    n = len(xs)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / max(n - 1, 1)
    return {
        "members": n,
        "mean": round(mean, 1),
        "spread": round(math.sqrt(var), 2),
        "p10": round(xs[int(0.10 * (n - 1))], 1),
        "p90": round(xs[int(0.90 * (n - 1))], 1),
    }


# ---------------------------------------------------------------------------
# Self-test:  python3 ensemble_prob.py   (run on the VPS — takes ~5s)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import datetime, timedelta

    lat, lon = 40.7794, -73.8803          # KLGA LaGuardia — NYC's resolution station
    date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"Fetching ensemble for KLGA, target date {date} ...")

    data = fetch_ensemble(lat, lon)
    maxes = member_daily_maxes(data, date)
    stats = summarize(maxes)
    print(f"members={stats['members']}  mean={stats['mean']}F  "
          f"spread={stats['spread']}F  p10-p90={stats['p10']}-{stats['p90']}F")

    center = int(round(stats["mean"]))
    print("\nHonest probabilities vs the old bot's certainty:")
    for lo in range(center - 4, center + 4, 2):
        p = bucket_probability(maxes, lo, lo + 1)
        old = 1.0 if lo <= stats["mean"] <= lo + 1 else 0.0
        print(f"  bucket {lo}-{lo+1}F : honest p={p:.2f}   old bot said p={old:.1f}")

    print("\nSame buckets if METAR already shows a running max of "
          f"{center + 2}F today:")
    for lo in range(center - 4, center + 4, 2):
        p = bucket_probability(maxes, lo, lo + 1, running_max=center + 2)
        print(f"  bucket {lo}-{lo+1}F : p={p:.2f}")

# slipiq_sharp_api.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Sharp API Benchmarking Module
# Role: TEST BENCH ONLY — not in the critical pick path
#
# Sharp API (sharpapi.io) has built-in EV calculations.
# This module compares our computed edge against theirs.
# Agreement = high conviction. Divergence = investigate.
#
# CREDIT USAGE:
#   Sharp API is used sparingly — only for benchmarking top picks.
#   Never called in the main pipeline loop.
#   Call manually or via /benchmark Discord command.
#
# SHARP_API_KEY goes in .env as SHARP_API_KEY
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

SHARP_API_KEY = os.getenv("SHARP_API_KEY")
# Sharp API base URL — update from their docs/dashboard
BASE_URL  = "https://sharpapi.io/api/v1"
HEADERS   = {
    "Authorization": f"Bearer {SHARP_API_KEY or ''}",
    "Accept":        "application/json",
}

CACHE_DIR       = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
BENCH_LOG_FILE  = CACHE_DIR / "sharp_benchmark_log.json"

# EV agreement threshold — if our EV and Sharp's EV are within this %,
# treat as agreement
EV_AGREEMENT_THRESHOLD = 0.03   # 3 percentage points


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — API FETCH
# ═══════════════════════════════════════════════════════════════

def fetch_sharp_ev(
    sport:  str = "baseball_mlb",
    market: str = "player_pitcher_strikeouts",
) -> list[dict]:
    """
    Fetch Sharp API's EV data for a sport/market.
    Returns list of prop EV records or [] on failure.

    NOTE: Endpoint paths depend on Sharp API's documentation.
    Update BASE_URL and endpoint paths from your Sharp API dashboard.
    """
    if not SHARP_API_KEY:
        print("  [sharp_api] ⚠️  SHARP_API_KEY not set in .env — skipping")
        return []

    cache_key = f"sharp_ev_{sport}_{market}"
    cache_path = CACHE_DIR / f"{cache_key}.json"

    # Use 1-hour cache — Sharp data doesn't update faster than this
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            age_min = (datetime.now() - datetime.fromisoformat(data["_ts"])).total_seconds() / 60
            if age_min < 60:
                return data["payload"]
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    try:
        # Try common Sharp API endpoint patterns
        # Update this path based on Sharp's actual API documentation
        resp = requests.get(
            f"{BASE_URL}/props/{sport}",
            headers=HEADERS,
            params={"market": market, "format": "ev"},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()
        props = _normalize_sharp_response(raw)

        cache_path.write_text(json.dumps({"_ts": datetime.now().isoformat(), "payload": props}, indent=2))
        print(f"  [sharp_api] ✓ {len(props)} EV records fetched for {sport}/{market}")
        return props

    except requests.exceptions.HTTPError as e:
        print(f"  [sharp_api] HTTP {resp.status_code}: {e}")
        return []
    except requests.exceptions.RequestException as e:
        print(f"  [sharp_api] Request error: {e}")
        return []


def _normalize_sharp_response(raw: dict | list) -> list[dict]:
    """
    Normalize Sharp API response to flat prop EV list.
    Shape: {player, market, direction, line, sharp_ev, sharp_prob}

    Sharp API response structure varies — adapt this to their actual schema.
    """
    entries = []

    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = (
            raw.get("props") or
            raw.get("data") or
            raw.get("markets") or
            [raw]
        )
    else:
        return entries

    for item in items:
        if not isinstance(item, dict):
            continue

        player    = item.get("player_name") or item.get("player") or ""
        market    = item.get("market_key")  or item.get("market")  or ""
        line      = item.get("line") or item.get("point")
        sharp_ev  = item.get("ev")   or item.get("expected_value")
        sharp_prob = item.get("probability") or item.get("true_prob")
        direction  = item.get("direction") or item.get("side", "over")

        if not player or sharp_ev is None:
            continue

        entries.append({
            "player":     player.strip(),
            "market":     str(market).lower(),
            "direction":  str(direction).lower(),
            "line":       float(line) if line is not None else None,
            "sharp_ev":   float(sharp_ev),
            "sharp_prob": float(sharp_prob) if sharp_prob is not None else None,
        })

    return entries


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — BENCHMARKING
# ═══════════════════════════════════════════════════════════════

def benchmark_leg(
    player:      str,
    market:      str,
    direction:   str,
    our_ev:      float,
    sharp_data:  list[dict] | None = None,
    sport:       str = "baseball_mlb",
) -> dict:
    """
    Compare our computed EV against Sharp API's EV for one leg.

    Args:
        player     : player name
        market     : market key
        direction  : "over" | "under"
        our_ev     : edge from slipiq_ev_engine.sportsbook_edge()
        sharp_data : pre-fetched Sharp EV data (fetches if None)
        sport      : sport key

    Returns:
        {
            "player"      : str,
            "our_ev"      : float,
            "sharp_ev"    : float | None,
            "agreement"   : bool,    # True if within threshold
            "divergence"  : float,   # abs(our_ev - sharp_ev)
            "signal"      : str,     # "HIGH_CONVICTION" | "INVESTIGATE" | "NO_SHARP_DATA"
            "recommendation": str,
        }
    """
    if sharp_data is None:
        sharp_data = fetch_sharp_ev(sport, market)

    # Find matching Sharp record
    player_norm = player.lower().strip()
    sharp_rec   = next(
        (r for r in sharp_data
         if r["player"].lower() == player_norm
         and r["direction"] == direction),
        None,
    )

    if sharp_rec is None:
        return {
            "player":         player,
            "our_ev":         round(our_ev, 6),
            "sharp_ev":       None,
            "agreement":      False,
            "divergence":     None,
            "signal":         "NO_SHARP_DATA",
            "recommendation": "Post pick — no Sharp data to compare against",
        }

    sharp_ev   = sharp_rec["sharp_ev"]
    divergence = abs(our_ev - sharp_ev)
    agreement  = divergence <= EV_AGREEMENT_THRESHOLD

    if our_ev > 0 and sharp_ev > 0 and agreement:
        signal = "HIGH_CONVICTION"
        rec    = "Both models agree +EV — post with full confidence"
    elif our_ev > 0 and sharp_ev > 0 and not agreement:
        signal = "SOFT_AGREE"
        rec    = f"Both +EV but diverge by {divergence:.1%} — post with normal confidence"
    elif our_ev > 0 and sharp_ev <= 0:
        signal = "INVESTIGATE"
        rec    = f"We say +EV ({our_ev:+.2%}), Sharp says -EV ({sharp_ev:+.2%}) — hold or investigate"
    elif our_ev <= 0 and sharp_ev > 0:
        signal = "INVESTIGATE"
        rec    = f"Sharp says +EV ({sharp_ev:+.2%}), we say -EV ({our_ev:+.2%}) — possible model gap"
    else:
        signal = "CONFIRMED_NEG"
        rec    = "Both -EV — do not post"

    return {
        "player":           player,
        "our_ev":           round(our_ev,    6),
        "sharp_ev":         round(sharp_ev,  6),
        "sharp_prob":       sharp_rec.get("sharp_prob"),
        "agreement":        agreement,
        "divergence":       round(divergence, 6),
        "signal":           signal,
        "recommendation":   rec,
    }


def benchmark_report(
    picks:       list[dict],
    sport:       str = "baseball_mlb",
    market:      str = "player_pitcher_strikeouts",
) -> dict:
    """
    Run benchmarking across a full slate of picks.

    Args:
        picks : list of pick cards (must have "player", "direction", "ev_value")

    Returns:
        {
            "total"          : int,
            "high_conviction": int,
            "investigate"    : int,
            "no_data"        : int,
            "agreement_rate" : float,
            "results"        : list[dict],
        }
    """
    if not SHARP_API_KEY:
        print("  [sharp_api] SHARP_API_KEY not set — benchmark skipped")
        return {"total": 0, "high_conviction": 0, "investigate": 0,
                "no_data": 0, "agreement_rate": None, "results": []}

    sharp_data = fetch_sharp_ev(sport, market)
    results    = []

    for pick in picks:
        player    = pick.get("player", "")
        direction = pick.get("direction", "over")
        our_ev    = pick.get("ev_value") or pick.get("ev") or 0.0

        if not player:
            continue

        result = benchmark_leg(player, pick.get("market", market), direction,
                               our_ev, sharp_data, sport)
        result["grade"] = pick.get("grade")
        results.append(result)

    total      = len(results)
    high_conv  = sum(1 for r in results if r["signal"] == "HIGH_CONVICTION")
    investigate = sum(1 for r in results if r["signal"] == "INVESTIGATE")
    no_data    = sum(1 for r in results if r["signal"] == "NO_SHARP_DATA")

    with_data  = total - no_data
    agree_rate = round(sum(1 for r in results if r["agreement"]) / with_data, 4) if with_data > 0 else None

    # Log to file
    _log_benchmark(results)

    return {
        "total":            total,
        "high_conviction":  high_conv,
        "investigate":      investigate,
        "no_data":          no_data,
        "agreement_rate":   agree_rate,
        "results":          results,
    }


def _log_benchmark(results: list[dict]) -> None:
    """Append benchmark results to local log for tracking agreement rate over time."""
    existing = []
    if BENCH_LOG_FILE.exists():
        try:
            existing = json.loads(BENCH_LOG_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    for r in results:
        existing.append({**r, "logged_at": datetime.now().isoformat()})

    # Keep last 500 records
    BENCH_LOG_FILE.write_text(json.dumps(existing[-500:], indent=2))


def format_benchmark_discord(report: dict) -> str:
    """Format benchmark report for Discord."""
    total   = report["total"]
    high    = report["high_conviction"]
    invest  = report["investigate"]
    agree   = report["agreement_rate"]

    lines = [
        "🔬 **Sharp API Benchmark Report**",
        f"Picks analyzed: {total}",
        "",
    ]

    if agree is not None:
        emoji = "✅" if agree >= 0.70 else ("⚠️" if agree >= 0.50 else "🔴")
        lines.append(f"{emoji} Agreement rate: **{agree:.0%}**")

    lines.append(f"🟢 High conviction: {high}")
    if invest > 0:
        lines.append(f"🔴 Investigate: {invest}")

    lines.append("")
    for r in report.get("results", []):
        if r["signal"] == "INVESTIGATE":
            lines.append(
                f"  ⚠️ {r['player']}: Our EV {r['our_ev']:+.2%} vs Sharp {(r['sharp_ev'] or 0):+.2%}"
            )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Sharp API Benchmarking Self-Test")
    print("=" * 60)

    # Test with mock Sharp data
    mock_sharp_data = [
        {"player": "Gerrit Cole",   "market": "player_pitcher_strikeouts", "direction": "over",
         "line": 7.5, "sharp_ev": 0.052, "sharp_prob": 0.545},
        {"player": "Zack Wheeler",  "market": "player_pitcher_strikeouts", "direction": "over",
         "line": 6.5, "sharp_ev": -0.012, "sharp_prob": 0.489},
        {"player": "Corbin Burnes", "market": "player_pitcher_strikeouts", "direction": "over",
         "line": 5.5, "sharp_ev": 0.067, "sharp_prob": 0.561},
    ]

    test_picks = [
        {"player": "Gerrit Cole",   "direction": "over", "ev_value": 0.048, "grade": "A"},
        {"player": "Zack Wheeler",  "direction": "over", "ev_value": 0.031, "grade": "B"},
        {"player": "Corbin Burnes", "direction": "over", "ev_value": -0.005, "grade": "B-"},
        {"player": "Pablo Lopez",   "direction": "over", "ev_value": 0.040, "grade": "B+"},
    ]

    print("\n[1] Individual benchmarks:")
    for pick in test_picks:
        result = benchmark_leg(
            pick["player"], "player_pitcher_strikeouts",
            pick["direction"], pick["ev_value"],
            sharp_data=mock_sharp_data,
        )
        print(f"    {result['player']}: {result['signal']} | {result['recommendation'][:60]}")

    print("\n[2] Full report with mock data:")
    # Inject mock data bypassing API
    for pick in test_picks:
        pick["market"] = "player_pitcher_strikeouts"
    report = {
        "total":            len(test_picks),
        "high_conviction":  1,
        "investigate":      1,
        "no_data":          1,
        "agreement_rate":   0.67,
        "results":          [
            benchmark_leg(p["player"], p["market"], p["direction"],
                          p["ev_value"], mock_sharp_data)
            for p in test_picks
        ],
    }
    print(format_benchmark_discord(report))

    if not SHARP_API_KEY:
        print("\n⚠️  SHARP_API_KEY not set — live API tests skipped.")
        print("    Add SHARP_API_KEY to .env to enable live benchmarking.")

    print("\n✓ Sharp API module ready.")

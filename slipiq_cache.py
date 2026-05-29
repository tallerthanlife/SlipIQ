# slipiq_cache.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ Cache Layer
#
# Caches responses from all three API tiers to disk.
# Cache expires based on ODDS_CACHE_HOURS env setting (default 6h).
# Lines cached once per day — second pipeline run reuses morning data.
#
# API TIER ORDER (matches supplement chain):
#   Tier 1: ParlayAPI       — primary, 3 cr/call, 1x daily
#   Tier 2: Prop-Line API   — Pinnacle supplement, 0 cr on cache hit
#   Tier 3: The Odds API    — last resort, 3 keys rotated, 5 events max
#
# Cache keys are namespaced by tier so they never collide:
#   parlayapi_{key}         → ParlayAPI responses
#   propline_{key}          → Prop-Line responses
#   odds_{event_id}_{mkt}   → Odds API per-event responses
#   mlb_events_{date}       → Odds API event list
# ═══════════════════════════════════════════════════════════════

import json
import os
from datetime import datetime, date

CACHE_DIR = "cache"

from slipiq_env import (
    ODDS_API_KEYS,       # list of Odds API keys (tier 3)
    ODDS_CACHE_HOURS,    # cache expiry in hours
    ODDS_MAX_EVENTS,     # max events to pull from Odds API
    PROPLINE_API_KEY,    # Prop-Line key (tier 2)
    PARLAY_API_KEY,      # ParlayAPI key (tier 1) — for status checks
)


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — CORE CACHE READ/WRITE
# ═══════════════════════════════════════════════════════════════

def _ensure_cache_dir():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)


def _cache_path(key: str) -> str:
    safe_key = key.replace("/", "_").replace(" ", "_")
    return os.path.join(CACHE_DIR, f"{safe_key}.json")


def cache_get(key: str):
    """
    Get cached value for key.
    Returns None if not found or expired.
    """
    path = _cache_path(key)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        cached_at = datetime.fromisoformat(data["cached_at"])
        age_hours = (datetime.now() - cached_at).total_seconds() / 3600

        if age_hours > ODDS_CACHE_HOURS:
            print(f"  [cache] Expired: {key} ({age_hours:.1f}h old)")
            os.remove(path)
            return None

        print(f"  [cache] Hit: {key} ({age_hours:.1f}h old)")
        return data["value"]

    except Exception as e:
        print(f"  [cache] Read error for {key}: {e}")
        return None


def cache_set(key: str, value) -> bool:
    """Store value in cache with timestamp."""
    _ensure_cache_dir()
    path = _cache_path(key)

    try:
        data = {
            "cached_at":  datetime.now().isoformat(),
            "cache_date": date.today().isoformat(),
            "key":        key,
            "value":      value,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"  [cache] Write error for {key}: {e}")
        return False


def cache_clear(key: str = None):
    """Clear one key or all cache."""
    _ensure_cache_dir()
    if key:
        path = _cache_path(key)
        if os.path.exists(path):
            os.remove(path)
            print(f"  [cache] Cleared: {key}")
    else:
        cleared = 0
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".json"):
                os.remove(os.path.join(CACHE_DIR, f))
                cleared += 1
        print(f"  [cache] Cleared {cleared} files")


def cache_status():
    """Show all cached keys and their age/tier."""
    _ensure_cache_dir()
    files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]

    if not files:
        print("  Cache is empty")
        return

    print(f"\n  Cache status ({len(files)} entries):")
    for f in sorted(files):
        path = os.path.join(CACHE_DIR, f)
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
            cached_at = datetime.fromisoformat(data["cached_at"])
            age_hours = (datetime.now() - cached_at).total_seconds() / 3600
            key       = data.get("key", f.replace(".json", ""))
            status    = "✅ FRESH" if age_hours <= ODDS_CACHE_HOURS else "❌ EXPIRED"

            # Tag by tier
            if key.startswith("parlayapi_") or key.startswith("props_") or key.startswith("period_"):
                tier = "[T1 ParlayAPI]"
            elif key.startswith("propline_"):
                tier = "[T2 PropLine]"
            elif key.startswith("odds_") or key.startswith("mlb_events_"):
                tier = "[T3 OddsAPI]"
            elif key.startswith("closing_"):
                tier = "[CLV]"
            else:
                tier = "[misc]"

            print(f"  {status} {tier:<16} {key} ({age_hours:.1f}h old)")
        except Exception:
            print(f"  ⚠️  Unreadable: {f}")


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — TIER 2: PROP-LINE CACHE WRAPPER
# ═══════════════════════════════════════════════════════════════

def get_propline_cached(endpoint_key: str, fetch_fn, max_age_hours: float = None):
    """
    Generic Prop-Line cache wrapper.
    Calls fetch_fn() only if cache is stale.
    Cache key namespaced under 'propline_'.

    Args:
        endpoint_key : unique key for this endpoint (e.g. 'props_mlb')
        fetch_fn     : callable that returns fresh data
        max_age_hours: override expiry (default: ODDS_CACHE_HOURS)
    """
    cache_key = f"propline_{endpoint_key}_{date.today().isoformat()}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    if not PROPLINE_API_KEY:
        print("  [cache] PROPLINE_API_KEY not set — skipping Prop-Line fetch")
        return None

    try:
        data = fetch_fn()
        if data:
            cache_set(cache_key, data)
        return data
    except Exception as e:
        print(f"  [cache] Prop-Line fetch error ({endpoint_key}): {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — TIER 3: ODDS API CACHE WRAPPERS
# ═══════════════════════════════════════════════════════════════

def get_events_cached(odds_api_key: str, base_url: str) -> list:
    """
    Tier 3: Get MLB events list from Odds API with daily caching.
    Falls back to next key in ODDS_API_KEYS if first key fails.
    """
    import requests

    cache_key = f"mlb_events_{date.today().isoformat()}"
    cached    = cache_get(cache_key)
    if cached is not None:
        return cached

    if not ODDS_API_KEYS:
        print("  [cache] No Odds API keys configured")
        return []

    # Try each key in rotation
    keys = list(ODDS_API_KEYS)
    if odds_api_key and odds_api_key not in keys:
        keys.insert(0, odds_api_key)

    for key in keys:
        try:
            r = requests.get(
                f"{base_url}/sports/baseball_mlb/events",
                params={"apiKey": key, "regions": "us"},
                timeout=10,
            )
            if r.status_code == 401:
                print(f"  [cache] Odds API key exhausted — trying next")
                continue
            r.raise_for_status()
            events    = r.json()
            remaining = r.headers.get("x-requests-remaining", "?")
            print(f"  [cache] Odds API events fetched — {remaining} requests remaining")
            cache_set(cache_key, events)
            return events
        except Exception as e:
            print(f"  [cache] Odds API events error: {e}")
            continue

    return []


def get_event_odds_cached(
    event_id:    str,
    markets:     str,
    odds_api_key: str = None,
    base_url:    str  = None,
    bookmakers:  str  = None,
) -> dict | None:
    """
    Tier 3: Get odds for one event with caching and key rotation.
    Filters to bookmakers=pinnacle by default (set in slipiq_odds_supplement.py).
    Only fires when Tier 1 and Tier 2 both had no Pinnacle data.
    """
    import requests

    base_url    = base_url or "https://api.the-odds-api.com/v4"
    markets_key = "_".join(sorted(markets.split(",")))
    bm_suffix   = f"_{bookmakers}" if bookmakers else ""
    cache_key   = f"odds_{event_id}_{markets_key}{bm_suffix}_{date.today().isoformat()}"

    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # Build key rotation list
    keys = list(ODDS_API_KEYS)
    if odds_api_key and odds_api_key not in keys:
        keys.insert(0, odds_api_key)

    if not keys:
        print("  [cache] No Odds API keys configured")
        return None

    for key in keys:
        try:
            params = {
                "apiKey":     key,
                "regions":    "us",
                "markets":    markets,
                "oddsFormat": "american",
            }
            if bookmakers:
                params["bookmakers"] = bookmakers

            r = requests.get(
                f"{base_url}/sports/baseball_mlb/events/{event_id}/odds",
                params  = params,
                timeout = 10,
            )

            if r.status_code == 401:
                print(f"  [cache] Odds API key 401 — trying next key")
                continue
            if r.status_code == 422:
                # Quota exhausted — this key is dead
                print(f"  [cache] Odds API 422 (quota exhausted) — trying next key")
                continue
            if r.status_code != 200:
                continue

            data = r.json()
            if not data.get("bookmakers"):
                continue

            cache_set(cache_key, data)
            remaining = r.headers.get("x-requests-remaining", "?")
            print(f"  [cache] Odds API odds cached "
                  f"({markets}, remaining={remaining})")
            return data

        except Exception as e:
            print(f"  [cache] Odds API error for {event_id}: {e}")
            continue

    return None


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — API STATUS CHECKER
# ═══════════════════════════════════════════════════════════════

def api_tier_status() -> dict:
    """
    Quick check of which API tiers are configured.
    Called by --status command in orchestrator.
    """
    return {
        "tier_1_parlayapi":  bool(PARLAY_API_KEY),
        "tier_2_propline":   bool(PROPLINE_API_KEY),
        "tier_3_odds_keys":  len(ODDS_API_KEYS),
        "odds_keys_detail": [
            f"key_{i+1}: {'SET' if k else 'MISSING'}"
            for i, k in enumerate(ODDS_API_KEYS)
        ],
    }


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if "--clear" in sys.argv:
        cache_clear()
    elif "--status" in sys.argv:
        cache_status()
        print("\n  API Tiers:")
        for k, v in api_tier_status().items():
            print(f"    {k}: {v}")
    else:
        cache_status()

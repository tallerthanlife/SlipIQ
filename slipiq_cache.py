"""
SlipIQ Cache
Caches Odds API responses to disk so we don't burn API calls
on repeat runs. Cache expires after N hours.
Lines cached once per day — second pipeline run reuses morning data.
"""

import json
import os
from datetime import datetime, date

CACHE_DIR = "cache"
from slipiq_env import ODDS_API_KEY, ODDS_CACHE_HOURS, ODDS_MAX_EVENTS


# ─── Setup ────────────────────────────────────────────────────

def _ensure_cache_dir():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)


def _cache_path(key):
    safe_key = key.replace("/", "_").replace(" ", "_")
    return os.path.join(CACHE_DIR, f"{safe_key}.json")


# ─── Read / Write ──────────────────────────────────────────────

def cache_get(key):
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
            print(f"Cache expired for {key} ({age_hours:.1f}h old)")
            os.remove(path)
            return None

        print(f"Cache hit for {key} ({age_hours:.1f}h old)")
        return data["value"]

    except Exception as e:
        print(f"Cache read error for {key}: {e}")
        return None


def cache_set(key, value):
    """Store value in cache with timestamp"""
    _ensure_cache_dir()
    path = _cache_path(key)

    try:
        data = {
            "cached_at": datetime.now().isoformat(),
            "cache_date": date.today().isoformat(),
            "key": key,
            "value": value,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Cache write error for {key}: {e}")
        return False


def cache_clear(key=None):
    """Clear one key or all cache"""
    _ensure_cache_dir()
    if key:
        path = _cache_path(key)
        if os.path.exists(path):
            os.remove(path)
            print(f"Cleared cache: {key}")
    else:
        cleared = 0
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".json"):
                os.remove(os.path.join(CACHE_DIR, f))
                cleared += 1
        print(f"Cleared {cleared} cache files")


def cache_status():
    """Show all cached keys and their age"""
    _ensure_cache_dir()
    files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json")]

    if not files:
        print("Cache is empty")
        return

    print(f"\nCache status ({len(files)} entries):")
    for f in sorted(files):
        path = os.path.join(CACHE_DIR, f)
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
            cached_at = datetime.fromisoformat(data["cached_at"])
            age_hours = (datetime.now() - cached_at).total_seconds() / 3600
            key = data.get("key", f.replace(".json", ""))
            status = "✅ FRESH" if age_hours <= ODDS_CACHE_HOURS else "❌ EXPIRED"
            print(f"  {status} {key} ({age_hours:.1f}h old)")
        except Exception:
            print(f"  ⚠️ Unreadable: {f}")


# ─── Cached API Wrappers ──────────────────────────────────────

def get_events_cached(odds_api_key, base_url):
    """Get MLB events with caching"""
    import requests

    cache_key = f"mlb_events_{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    print("Fetching fresh MLB events from Odds API...")
    try:
        r = requests.get(
            f"{base_url}/sports/baseball_mlb/events",
            params={"apiKey": odds_api_key, "regions": "us"},
            timeout=10,
        )
        r.raise_for_status()
        events = r.json()
        remaining = r.headers.get("x-requests-remaining", "?")
        print(f"  Odds API requests remaining: {remaining}")
        cache_set(cache_key, events)
        return events
    except Exception as e:
        print(f"Events fetch error: {e}")
        return []


def get_event_odds_cached(
    event_id,
    markets,
    odds_api_key=None,
    base_url=None,
    bookmakers=None,
):
    """Get odds for one event with caching and Odds API key rotation."""
    import requests

    from slipiq_env import ODDS_API_KEYS

    base_url = base_url or "https://api.the-odds-api.com/v4"
    markets_key = "_".join(sorted(markets.split(",")))
    bm_suffix = f"_{bookmakers}" if bookmakers else ""
    cache_key = f"odds_{event_id}_{markets_key}{bm_suffix}_{date.today().isoformat()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    keys = []
    if odds_api_key:
        keys.append(odds_api_key)
    keys.extend(k for k in ODDS_API_KEYS if k not in keys)

    for key in keys:
        try:
            params = {
                "apiKey": key,
                "regions": "us",
                "markets": markets,
                "oddsFormat": "american",
            }
            if bookmakers:
                params["bookmakers"] = bookmakers

            r = requests.get(
                f"{base_url}/sports/baseball_mlb/events/{event_id}/odds",
                params=params,
                timeout=10,
            )
            if r.status_code == 401:
                continue
            if r.status_code != 200:
                continue

            data = r.json()
            if not data.get("bookmakers"):
                continue

            cache_set(cache_key, data)
            remaining = r.headers.get("x-requests-remaining", "?")
            print(f"  [odds] event odds cached ({markets}, remaining={remaining})")
            return data

        except Exception as e:
            print(f"Odds fetch error for {event_id}: {e}")
            continue

    return None


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--clear" in sys.argv:
        cache_clear()
    elif "--status" in sys.argv:
        cache_status()
    else:
        cache_status()
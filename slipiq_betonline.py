# DISABLED
# This module is disabled. Import it safely; all functions are no-ops.
import sys as _sys
if False:
    pass

"""
BetOnline MLB prop scraper — direct JSON API (no browser needed).
Runs nightly at 10pm AZ, caches lines for morning pipeline.
BetOnline posts MLB props 12-14 hours before game time.
"""
import json
import os
import requests
from datetime import date
from pathlib import Path

CACHE_PATH = Path("cache/betonline_lines.json")


def get_cached_lines() -> list[dict]:
    """Return cached BetOnline lines if from today."""
    if not CACHE_PATH.exists():
        return []
    try:
        data = json.loads(CACHE_PATH.read_text())
        if data.get("date") == str(date.today()):
            lines = data.get("lines", [])
            print(f"  [betonline] Cache hit — {len(lines)} lines")
            return lines
    except Exception:
        pass
    return []


def scrape_betonline_mlb_props() -> list[dict]:
    """
    Fetch BetOnline MLB props via their internal JSON API.
    BetOnline loads odds via XHR — we hit the endpoint directly.
    No browser needed.
    """
    cached = get_cached_lines()
    if cached:
        return cached

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://www.betonline.ag/",
        "Origin": "https://www.betonline.ag",
    }

    # BetOnline internal odds API endpoints
    ENDPOINTS = [
        "https://www.betonline.ag/api/sport/get-events?sportId=4&leagueId=1&period=0",
        "https://www.betonline.ag/api/props/get-player-props?sportId=4",
    ]

    lines = []
    for url in ENDPOINTS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                parsed = _parse_betonline_json(data)
                lines.extend(parsed)
                print(f"  [betonline] ✓ {len(parsed)} lines from {url}")
            else:
                print(f"  [betonline] HTTP {r.status_code} from {url}")
        except Exception as e:
            print(f"  [betonline] Error {url}: {e}")

    if lines:
        CACHE_PATH.parent.mkdir(exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "date": str(date.today()),
            "lines": lines,
        }))

    return lines


def _parse_betonline_json(data) -> list[dict]:
    lines = []
    try:
        events = data if isinstance(data, list) else (
            data.get("events") or data.get("data") or []
        )
        for event in events:
            home = event.get("homeTeam") or event.get("home", "")
            away = event.get("awayTeam") or event.get("away", "")
            for market in (event.get("markets") or event.get("props") or []):
                market_name = market.get("name", "").lower()
                for outcome in market.get("outcomes") or market.get("selections") or []:
                    lines.append({
                        "source":    "betonline",
                        "home_team": home,
                        "away_team": away,
                        "market":    market_name,
                        "player":    outcome.get("participant") or outcome.get("name", ""),
                        "outcome":   outcome.get("name", ""),
                        "price":     outcome.get("price") or outcome.get("odds"),
                        "point":     outcome.get("points") or outcome.get("line"),
                    })
    except Exception:
        pass
    return lines


def get_pitcher_k_line_betonline(player_name: str) -> dict | None:
    """Get BetOnline strikeout line for a pitcher."""
    lines = get_cached_lines() or scrape_betonline_mlb_props()
    player_lower = player_name.lower()
    for line in lines:
        if player_lower in line.get("player", "").lower():
            if "strikeout" in line.get("market", "").lower():
                return line
    return None

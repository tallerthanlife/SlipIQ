# DISABLED
# This module is disabled. Import it safely; all functions are no-ops.
import sys as _sys
if False:
    pass

"""
Bookmaker.eu MLB main line scraper — direct JSON API (no browser needed).
Used as the truest fair line source — line origin benchmark.
Bookmaker.eu is where sharp syndicates hit first.
Runs nightly, caches lines for morning EV calculation.
"""
import json
import requests
from datetime import date
from pathlib import Path

CACHE_PATH = Path("cache/bookmaker_lines.json")


def get_cached_lines() -> list[dict]:
    if not CACHE_PATH.exists():
        return []
    try:
        data = json.loads(CACHE_PATH.read_text())
        if data.get("date") == str(date.today()):
            lines = data.get("lines", [])
            print(f"  [bookmaker] Cache hit — {len(lines)} lines")
            return lines
    except Exception:
        pass
    return []


def scrape_bookmaker_mlb() -> list[dict]:
    cached = get_cached_lines()
    if cached:
        return cached

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.bookmaker.eu/",
    }

    ENDPOINTS = [
        "https://www.bookmaker.eu/api/betting/events?sport=baseball&league=mlb",
        "https://www.bookmaker.eu/api/odds/player-props?sport=baseball",
    ]

    lines = []
    for url in ENDPOINTS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                parsed = _parse_bookmaker_response(data)
                lines.extend(parsed)
                print(f"  [bookmaker] ✓ {len(parsed)} lines from {url}")
            else:
                print(f"  [bookmaker] HTTP {r.status_code} from {url}")
        except Exception as e:
            print(f"  [bookmaker] Error {url}: {e}")

    if lines:
        CACHE_PATH.parent.mkdir(exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "date": str(date.today()),
            "lines": lines,
        }))

    return lines


def _parse_bookmaker_response(body: dict | list) -> list[dict]:
    """Parse Bookmaker.eu XHR JSON response into normalized lines."""
    lines = []
    try:
        events = body if isinstance(body, list) else body.get("events", [])
        for event in events:
            home = event.get("home") or event.get("homeTeam", "")
            away = event.get("away") or event.get("awayTeam", "")
            start = event.get("startTime") or event.get("start", "")
            for market in event.get("markets", []):
                market_name = market.get("name", "").lower()
                for outcome in market.get("outcomes", []):
                    lines.append({
                        "source": "bookmaker",
                        "home_team": home,
                        "away_team": away,
                        "start_time": start,
                        "market": market_name,
                        "outcome": outcome.get("name", ""),
                        "price": outcome.get("price"),
                        "point": outcome.get("point"),
                    })
    except Exception:
        pass
    return lines


def _parse_bookmaker_dom(page) -> list[dict]:
    """Fallback DOM parser for Bookmaker.eu."""
    lines = []
    try:
        rows = page.query_selector_all(
            "[class*='event'], [class*='game-row'], [class*='matchup']"
        )
        for row in rows:
            try:
                text = row.inner_text().strip()
                if text:
                    lines.append({"raw": text, "source": "bookmaker_dom"})
            except Exception:
                pass
    except Exception:
        pass
    return lines


def get_fair_line_bookmaker(home_team: str, away_team: str) -> dict | None:
    """
    Get Bookmaker.eu fair line for a specific game.
    Use this as the true probability anchor for EV calculation.
    """
    lines = get_cached_lines() or scrape_bookmaker_mlb()
    home_lower = home_team.lower()
    away_lower = away_team.lower()

    game_lines = [
        l for l in lines
        if (home_lower in l.get("home_team", "").lower() or
            away_lower in l.get("away_team", "").lower())
        and l.get("market") in ("moneyline", "h2h", "ml", "run line")
    ]

    if not game_lines:
        return None

    # Find over/under pair
    over = next((l for l in game_lines if "over" in l.get("outcome", "").lower()), None)
    under = next((l for l in game_lines if "under" in l.get("outcome", "").lower()), None)

    if over and under:
        try:
            from slipiq_novig import remove_vig
            novig = remove_vig(int(over["price"]), int(under["price"]))
            return {
                "source": "bookmaker",
                "home_team": home_team,
                "away_team": away_team,
                "over_odds": over["price"],
                "under_odds": under["price"],
                "fair_over_prob": novig["fair_over_prob"],
                "fair_under_prob": novig["fair_under_prob"],
            }
        except Exception:
            pass

    return None

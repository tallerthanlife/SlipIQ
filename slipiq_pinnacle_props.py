"""
SlipIQ Pinnacle Props
Pulls pitcher strikeout lines from Pinnacle via Odds API eu region
Pinnacle has the sharpest lines and widest coverage
No extra API key needed — just adds eu region to existing Odds API key
"""

import requests
import os
from dotenv import load_dotenv

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"


def get_pinnacle_pitcher_props(max_events=20):
    """
    Pull pitcher strikeout props from Pinnacle via Odds API
    Uses eu region which unlocks Pinnacle coverage
    Returns props in same format as slipiq_lines.py
    """
    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY not set")
        return []

    # Get events
    url = f"{BASE_URL}/sports/baseball_mlb/events"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us,eu",
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        events = response.json()

        if not events:
            return []

        props = []

        for event in events[:max_events]:
            event_id = event["id"]
            home = event["home_team"]
            away = event["away_team"]

            prop_url = f"{BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
            prop_params = {
                "apiKey": ODDS_API_KEY,
                "regions": "us,eu",
                "markets": "pitcher_strikeouts",
                "oddsFormat": "american",
                "bookmakers": "pinnacle",
            }

            prop_response = requests.get(prop_url, params=prop_params, timeout=10)

            if prop_response.status_code != 200:
                continue

            prop_data = prop_response.json()
            bookmakers = prop_data.get("bookmakers", [])

            if not bookmakers:
                continue

            for bookmaker in bookmakers:
                if bookmaker["key"] != "pinnacle":
                    continue

                for market in bookmaker.get("markets", []):
                    if market["key"] != "pitcher_strikeouts":
                        continue

                    for outcome in market["outcomes"]:
                        props.append({
                            "pitcher": outcome["description"],
                            "line": outcome["point"],
                            "direction": outcome["name"],
                            "odds": outcome["price"],
                            "home_team": home,
                            "away_team": away,
                            "bookmaker": "Pinnacle",
                        })

        pitchers = set(p["pitcher"] for p in props)
        print(f"Pinnacle: {len(pitchers)} pitchers found")
        return props

    except Exception as e:
        print(f"Pinnacle props error: {e}")
        return []


if __name__ == "__main__":
    props = get_pinnacle_pitcher_props()
    pitchers = set(p["pitcher"] for p in props if p["direction"] == "Over")
    print(f"\nPinnacle pitchers with Over lines: {len(pitchers)}")
    for p in sorted(pitchers):
        line = next(x["line"] for x in props if x["pitcher"] == p and x["direction"] == "Over")
        print(f"  {p}: {line} K")
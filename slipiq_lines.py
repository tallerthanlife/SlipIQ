"""
SlipIQ Lines Module
Fetches real pitcher strikeout prop lines from Odds API
Compares against model projection to generate picks
"""

import requests
import os
from dotenv import load_dotenv
from slipiq_pitcher_model import run_pitcher_model, get_recommendation

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"
MAX_EVENTS = int(os.getenv("ODDS_MAX_EVENTS", "15"))


# ─── Fetch Props ──────────────────────────────────────────────

def get_mlb_pitcher_props():
    """
    Fetch live pitcher strikeout props from Odds API
    Returns list of props with pitcher name and line
    """
    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY not set in .env file")
        return []

    url = f"{BASE_URL}/sports/baseball_mlb/events"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        events = response.json()

        if not events:
            print("No MLB events found today")
            return []

        print(f"Found {len(events)} MLB games today")
        props = []

        for event in events[:MAX_EVENTS]:
            event_id = event["id"]
            home = event["home_team"]
            away = event["away_team"]

            prop_url = f"{BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
            prop_params = {
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "pitcher_strikeouts",
                "oddsFormat": "american",
            }

            prop_response = requests.get(prop_url, params=prop_params, timeout=10)

            if prop_response.status_code != 200:
                continue

            prop_data = prop_response.json()
            bookmakers = prop_data.get("bookmakers", [])

            if not bookmakers:
                continue

            # Prefer DraftKings or FanDuel
            preferred = None
            for bm in bookmakers:
                if bm["title"] in ("DraftKings", "FanDuel"):
                    preferred = bm
                    break
            bookmaker = preferred or bookmakers[0]
            markets = bookmaker.get("markets", [])

            for market in markets:
                if market["key"] == "pitcher_strikeouts":
                    for outcome in market["outcomes"]:
                        props.append({
                            "pitcher": outcome["description"],
                            "line": outcome["point"],
                            "direction": outcome["name"],
                            "odds": outcome["price"],
                            "home_team": home,
                            "away_team": away,
                            "bookmaker": bookmaker["title"],
                        })

        return props

    except requests.exceptions.RequestException as e:
        print(f"Odds API error: {e}")
        return []


# ─── Match Props to Model ─────────────────────────────────────

def run_full_analysis():
    """
    Pull live lines + run model on each pitcher
    Output: ranked list of picks for today
    """
    print("=== SlipIQ Daily Lines Analysis ===\n")

    props = get_mlb_pitcher_props()

    if not props:
        print("No props available. Check your ODDS_API_KEY in .env")
        return []

    # Deduplicate — keep Over side only
    seen = set()
    unique_props = []
    for prop in props:
        if prop["pitcher"] not in seen and prop["direction"] == "Over":
            seen.add(prop["pitcher"])
            unique_props.append(prop)

    print(f"\nAnalyzing {len(unique_props)} pitchers...\n")

    picks = []

    for prop in unique_props:
        pitcher = prop["pitcher"]
        line = prop["line"]

        projection = run_pitcher_model(pitcher, line=line, verbose=False)

        if not projection:
            continue

        # Minimum confidence — 55% to cast wide net
        if projection["confidence"] < 55:
            continue

        # Minimum edge — projection must differ from line by 0.3+
        edge = abs(projection["projection"] - line)
        if edge < 0.3:
            continue

        rec = get_recommendation(projection, line)
        if "PASS" in rec:
            continue

        picks.append({
            "pitcher": pitcher,
            "line": line,
            "projection": projection["projection"],
            "recommendation": rec,
            "confidence": projection["confidence"],
            "trend": projection["trend"],
            "season_avg": projection["season_avg"],
            "last_3_avg": projection["last_3_avg"],
            "last_5_avg": projection["last_5_avg"],
            "bookmaker": prop["bookmaker"],
            "home_team": prop.get("home_team"),
            "away_team": prop.get("away_team"),
        })

    if not picks:
        print("No picks cleared the model threshold today")
        return []

    # Agentic confidence + hit-rate-aware grades
    print("\nRunning confidence agent on picks...")
    from slipiq_confidence_agent import enrich_picks
    picks = enrich_picks(picks)

    # Print full slate
    print("\n" + "="*50)
    print("SlipIQ PICKS OF THE DAY")
    print("="*50)

    if not picks:
        print("No high confidence picks today")
    else:
        for i, pick in enumerate(picks, 1):
            print(f"\n#{i} {pick['pitcher']}")
            print(f"  Line:       {pick['line']} K")
            print(f"  Projection: {pick['projection']} K")
            print(f"  Pick:       {pick['recommendation']}")
            print(f"  Model conf: {pick.get('model_confidence')}% -> Display: {pick.get('display_confidence')}%")
            print(f"  Track rec:  {pick.get('hit_rate_label', '—')}")
            print(f"  Trend:      {pick['trend']}")
            print(f"  Source:     {pick['bookmaker']}")

    return picks


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    run_full_analysis()
"""
SlipIQ Pitcher Props — Full Market Coverage
Pulls all pitcher prop markets from Odds API:
- Strikeouts (already in slipiq_lines.py — duplicated here for unified output)
- Outs recorded
- Hits allowed
- Walks allowed
- Pitches thrown
Runs projections on each and returns curated picks
"""

import requests
import os
from dotenv import load_dotenv
from slipiq_mlb_data import get_pitcher_id, get_pitcher_game_log
from slipiq_pitcher_model import parse_game_log

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"
MAX_EVENTS = int(os.getenv("ODDS_MAX_EVENTS", "15"))

# All pitcher prop markets
PITCHER_MARKETS = [
    "pitcher_strikeouts",
    "pitcher_outs",
]

MARKET_LABELS = {
    "pitcher_strikeouts": "Strikeouts",
    "pitcher_outs": "Outs Recorded",
    "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_walks": "Walks",
    "pitcher_pitches_thrown": "Pitches Thrown",
}


# ─── Fetch All Pitcher Props ──────────────────────────────────

def get_all_pitcher_props():
    """
    Pull all pitcher prop markets from Odds API
    Returns grouped by pitcher name
    """
    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY not set")
        return {}

    url = f"{BASE_URL}/sports/baseball_mlb/events"
    params = {"apiKey": ODDS_API_KEY, "regions": "us"}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        events = response.json()

        if not events:
            return {}

        print(f"Pulling full pitcher props for {min(len(events), MAX_EVENTS)} games...")
        pitcher_props = {}

        for event in events[:MAX_EVENTS]:
            event_id = event["id"]
            home = event["home_team"]
            away = event["away_team"]

            prop_url = f"{BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
            prop_params = {
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": ",".join(PITCHER_MARKETS),
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

            for market in bookmaker.get("markets", []):
                market_key = market["key"]
                if market_key not in PITCHER_MARKETS:
                    continue

                prop_label = MARKET_LABELS.get(market_key, market_key)

                for outcome in market["outcomes"]:
                    pitcher = outcome.get("description", "")
                    if not pitcher:
                        continue

                    if pitcher not in pitcher_props:
                        pitcher_props[pitcher] = {
                            "home_team": home,
                            "away_team": away,
                            "bookmaker": bookmaker["title"],
                            "props": {}
                        }

                    if prop_label not in pitcher_props[pitcher]["props"]:
                        pitcher_props[pitcher]["props"][prop_label] = {}

                    direction = outcome.get("name", "Over")
                    pitcher_props[pitcher]["props"][prop_label][direction] = {
                        "line": outcome.get("point", 0),
                        "odds": outcome.get("price", -110),
                    }

        return pitcher_props

    except requests.exceptions.RequestException as e:
        print(f"Pitcher props API error: {e}")
        return {}


# ─── Project Each Prop ────────────────────────────────────────

def project_pitcher_stat(df, stat_type):
    """
    Project a pitcher stat from game log data
    Returns projection dict or None
    """
    import numpy as np

    if df is None or df.empty or len(df) < 3:
        return None

    # Map stat type to dataframe column
    col_map = {
        "Strikeouts": "strikeouts",
        "Outs Recorded": "outs",
        "Hits Allowed": "hits",
        "Walks": "walks",
        "Pitches Thrown": "pitches",
    }

    col = col_map.get(stat_type)
    if not col or col not in df.columns:
        return None

    series = df[col].dropna()
    if len(series) < 3:
        return None

    season_avg = round(series.mean(), 1)
    last_5 = round(series.head(5).mean(), 1)
    last_3 = round(series.head(3).mean(), 1)
    std = series.std()

    projection = round(
        season_avg * 0.30 + last_5 * 0.40 + last_3 * 0.30, 1
    )

    confidence = max(40, min(90, 100 - (std * 8)))

    return {
        "projection": projection,
        "season_avg": season_avg,
        "last_5_avg": last_5,
        "last_3_avg": last_3,
        "std_dev": round(std, 2),
        "confidence": round(confidence, 1),
        "games_analyzed": len(series),
    }


def get_enhanced_game_log(pitcher_name):
    """
    Pull game log with all pitching stats
    Returns dataframe with outs, hits, walks, pitches columns added
    """
    import statsapi
    import pandas as pd

    pitcher_id, _ = get_pitcher_id(pitcher_name)
    if not pitcher_id:
        return None

    try:
        raw = statsapi.player_stat_data(
            pitcher_id,
            group="pitching",
            type="gameLog",
            sportId=1
        )

        splits = raw.get("stats", [])
        if not splits:
            return None

        rows = []
        for split in splits:
            stat = split.get("stats", {})
            if not stat:
                continue

            innings = float(stat.get("inningsPitched", 0) or 0)
            k_per_9 = float(stat.get("strikeoutsPer9Inn", 0) or 0)
            strikeouts = round((k_per_9 / 9) * innings, 0) if innings > 0 else 0
            outs = round(innings * 3, 0)
            hits = int(stat.get("hits", 0) or 0)
            walks = int(stat.get("baseOnBalls", 0) or 0)
            pitches = int(stat.get("numberOfPitches", 0) or 0)

            rows.append({
                "innings": innings,
                "strikeouts": int(strikeouts),
                "outs": int(outs),
                "hits": hits,
                "walks": walks,
                "pitches": pitches,
            })

        if not rows:
            return None

        return pd.DataFrame(rows)

    except Exception as e:
        print(f"Game log error for {pitcher_name}: {e}")
        return None


# ─── Full Analysis ────────────────────────────────────────────

def run_full_pitcher_props_analysis():
    """
    Pull all pitcher prop markets and run projections
    Returns list of picks across all prop types
    """
    print("\n=== SlipIQ Full Pitcher Props Analysis ===\n")

    pitcher_props = get_all_pitcher_props()

    if not pitcher_props:
        print("No pitcher props available")
        return []

    print(f"Found {len(pitcher_props)} pitchers with props\n")

    all_picks = []

    for pitcher_name, data in pitcher_props.items():
        df = get_enhanced_game_log(pitcher_name)
        if df is None or df.empty:
            continue

        bookmaker = data["bookmaker"]
        home_team = data["home_team"]
        away_team = data["away_team"]

        for prop_label, prop_sides in data["props"].items():
            over_data = prop_sides.get("Over", {})
            under_data = prop_sides.get("Under", {})

            if not over_data:
                continue

            line = over_data.get("line", 0)
            if not line:
                continue

            proj_data = project_pitcher_stat(df, prop_label)
            if not proj_data:
                continue

            proj = proj_data["projection"]
            confidence = proj_data["confidence"]
            edge = abs(proj - line)

            # Prop-specific thresholds
            min_edge = {
                "Strikeouts": 0.5,
                "Outs Recorded": 1.5,
                "Hits Allowed": 1.0,
                "Walks": 0.5,
                "Pitches Thrown": 5.0,
            }.get(prop_label, 0.5)

            min_conf = {
                "Strikeouts": 60,
                "Outs Recorded": 60,
                "Hits Allowed": 55,
                "Walks": 55,
                "Pitches Thrown": 55,
            }.get(prop_label, 60)

            if confidence < min_conf or edge < min_edge:
                continue

            direction = "OVER" if proj > line else "UNDER"

            # Grade
            if confidence >= 72 and edge >= min_edge * 1.5:
                grade = "A"
            elif confidence >= 65 and edge >= min_edge:
                grade = "B"
            else:
                grade = "C"

            all_picks.append({
                "pitcher": pitcher_name,
                "prop_type": prop_label,
                "line": line,
                "projection": proj,
                "direction": direction,
                "confidence": confidence,
                "grade": grade,
                "season_avg": proj_data["season_avg"],
                "last_3_avg": proj_data["last_3_avg"],
                "last_5_avg": proj_data["last_5_avg"],
                "games_analyzed": proj_data["games_analyzed"],
                "bookmaker": bookmaker,
                "home_team": home_team,
                "away_team": away_team,
                "recommendation": f"{direction} {line} | Grade: {grade} | Confidence: {confidence}%",
            })

    # Sort by confidence
    all_picks.sort(key=lambda x: x["confidence"], reverse=True)

    # Print results
    print("\n" + "="*50)
    print("SlipIQ PITCHER PROPS — ALL MARKETS")
    print("="*50)

    if not all_picks:
        print("No pitcher prop picks today")
    else:
        current_pitcher = None
        for pick in all_picks:
            if pick["pitcher"] != current_pitcher:
                current_pitcher = pick["pitcher"]
                print(f"\n🔵 {current_pitcher}")

            print(f"  {pick['prop_type']}: {pick['direction']} {pick['line']} "
                  f"| Proj: {pick['projection']} | Grade: {pick['grade']} "
                  f"| Conf: {pick['confidence']}%")

    print(f"\nTotal pitcher prop picks: {len(all_picks)}")
    return all_picks


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    run_full_pitcher_props_analysis()
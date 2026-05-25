"""
SlipIQ Batter Lines
Fetches live batter prop lines from Odds API
Runs batter model against each line and generates curated picks
"""

import requests
import os
from dotenv import load_dotenv
from slipiq_batter_model import run_batter_model, get_batter_recommendation

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"
MAX_EVENTS = int(os.getenv("ODDS_MAX_EVENTS", "15"))

BATTER_MARKETS = [
    "batter_hits",
    "batter_total_bases",
    "batter_rbis",
]

MARKET_TO_PROP = {
    "batter_hits": "hits",
    "batter_total_bases": "total_bases",
    "batter_rbis": "rbi",
}

MIN_GAMES = 10  # Minimum games analyzed before trusting projection


# ─── Fetch Batter Props ───────────────────────────────────────

def get_mlb_batter_props():
    """Fetch live batter props from Odds API"""
    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY not set")
        return []

    url = f"{BASE_URL}/sports/baseball_mlb/events"
    params = {"apiKey": ODDS_API_KEY, "regions": "us"}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        events = response.json()

        if not events:
            return []

        print(f"Fetching batter props for {min(len(events), MAX_EVENTS)} games...")
        props = []

        for event in events[:MAX_EVENTS]:
            event_id = event["id"]
            home = event["home_team"]
            away = event["away_team"]

            prop_url = f"{BASE_URL}/sports/baseball_mlb/events/{event_id}/odds"
            prop_params = {
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": ",".join(BATTER_MARKETS),
                "oddsFormat": "american",
            }

            prop_response = requests.get(prop_url, params=prop_params, timeout=10)
            if prop_response.status_code != 200:
                continue

            prop_data = prop_response.json()
            bookmakers = prop_data.get("bookmakers", [])
            if not bookmakers:
                continue

            preferred = None
            for bm in bookmakers:
                if bm["title"] in ("DraftKings", "FanDuel"):
                    preferred = bm
                    break
            bookmaker = preferred or bookmakers[0]

            for market in bookmaker.get("markets", []):
                market_key = market["key"]
                prop_type = MARKET_TO_PROP.get(market_key)
                if not prop_type:
                    continue

                for outcome in market["outcomes"]:
                    props.append({
                        "batter": outcome["description"],
                        "prop_type": prop_type,
                        "line": outcome["point"],
                        "direction": outcome["name"],
                        "odds": outcome["price"],
                        "home_team": home,
                        "away_team": away,
                        "bookmaker": bookmaker["title"],
                    })

        return props

    except requests.exceptions.RequestException as e:
        print(f"Odds API batter props error: {e}")
        return []


# ─── Curation Filter ──────────────────────────────────────────

def passes_curation(prop_type, line, proj, confidence, games_analyzed):
    """
    Strict curation — only keep meaningful edges.
    Returns True if pick should be included.
    """
    # Hard minimum — need real sample size
    if games_analyzed < MIN_GAMES:
        return False

    edge = abs(proj - line)

    if prop_type == "hits":
        # Skip OVER 0.5 hits — almost everyone averages above 0.5, no real edge
        if line < 1.0 and proj > line:
            return False
        # UNDER 0.5 needs projection well below line
        if line <= 0.5 and proj >= 0.35:
            return False
        if edge < 0.25:
            return False
        if confidence < 70:
            return False

    elif prop_type == "total_bases":
        if edge < 0.35:
            return False
        if confidence < 65:
            return False
        # OVER 1.5 TB needs clear projection above
        if line <= 1.5 and proj > line and edge < 0.4:
            return False

    elif prop_type == "rbi":
        if edge < 0.25:
            return False
        if confidence < 65:
            return False
        # Skip weak OVER 0.5 RBI
        if line <= 0.5 and proj > line and proj < 0.75:
            return False
        # Skip near-zero projections
        if proj < 0.05:
            return False

    return True


# ─── Run Batter Analysis ──────────────────────────────────────

def run_batter_analysis():
    """
    Pull live batter lines + run model + curate
    Output: tight list of high quality batter picks
    """
    print("\n=== SlipIQ Batter Props Analysis ===\n")

    props = get_mlb_batter_props()

    if not props:
        print("No batter props available today")
        return []

    # Group by batter
    batter_props = {}
    for prop in props:
        if prop["direction"] != "Over":
            continue
        batter = prop["batter"]
        if batter not in batter_props:
            batter_props[batter] = {}
        batter_props[batter][prop["prop_type"]] = {
            "line": prop["line"],
            "bookmaker": prop["bookmaker"],
            "home_team": prop["home_team"],
            "away_team": prop["away_team"],
        }

    print(f"Found {len(batter_props)} batters with props")
    print("Running models + curation...\n")

    picks = []

    for batter, prop_types in batter_props.items():
        hits_line = prop_types.get("hits", {}).get("line")
        tb_line = prop_types.get("total_bases", {}).get("line")
        rbi_line = prop_types.get("rbi", {}).get("line")

        profile = run_batter_model(
            batter,
            hits_line=hits_line,
            tb_line=tb_line,
            rbi_line=rbi_line,
            verbose=False,
        )

        if not profile:
            continue

        games_analyzed = profile.get("games_analyzed", 0)

        for prop_type, prop_data in prop_types.items():
            line = prop_data["line"]
            proj_data = profile.get(prop_type)

            if not proj_data:
                continue

            proj = proj_data["projection"]
            confidence = proj_data["confidence"]

            # Apply strict curation with games filter
            if not passes_curation(prop_type, line, proj, confidence, games_analyzed):
                continue

            rec = get_batter_recommendation(proj_data, line, prop_type)
            if "PASS" in rec:
                continue

            direction = "OVER" if proj > line else "UNDER"
            grade = rec.split("Grade: ")[-1].split(" |")[0].strip()

            picks.append({
                "batter": batter,
                "prop_type": prop_type,
                "line": line,
                "projection": proj,
                "recommendation": rec,
                "confidence": confidence,
                "season_avg": proj_data["season_avg"],
                "last_3_avg": proj_data["last_3_avg"],
                "last_5_avg": proj_data["last_5_avg"],
                "games_analyzed": games_analyzed,
                "direction": direction,
                "grade": grade,
                "bookmaker": prop_data["bookmaker"],
                "home_team": prop_data.get("home_team"),
                "away_team": prop_data.get("away_team"),
            })

    # Sort by confidence descending
    picks.sort(key=lambda x: x["confidence"], reverse=True)

    # Print results
    print("\n" + "="*50)
    print("SlipIQ BATTER PICKS")
    print("="*50)

    prop_labels = {
        "hits": "Hits",
        "total_bases": "Total Bases",
        "rbi": "RBI",
    }

    if not picks:
        print("No high confidence batter picks today")
    else:
        for i, pick in enumerate(picks, 1):
            label = prop_labels.get(pick["prop_type"], pick["prop_type"])
            direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
            print(f"\n#{i} {pick['batter']} — {label}")
            print(f"  Pick:       {direction} {pick['line']}")
            print(f"  Projection: {pick['projection']}")
            print(f"  Season Avg: {pick['season_avg']}")
            print(f"  Last 3:     {pick['last_3_avg']}")
            print(f"  Games:      {pick['games_analyzed']}")
            print(f"  Grade:      {pick['grade']}")
            print(f"  Confidence: {pick['confidence']}%")
            print(f"  Source:     {pick['bookmaker']}")

    print(f"\nTotal curated batter picks: {len(picks)}")
    return picks


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    run_batter_analysis()
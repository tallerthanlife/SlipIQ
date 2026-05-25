"""
SlipIQ SportsData.io Source
Primary prop line source — 1,451+ props per day, free tier
Covers: pitcher strikeouts, hits allowed, runs allowed
        batter hits, TB, RBI, runs, home runs
No rate limit issues — replaces Odds API as primary source
"""

import requests
import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

SPORTSDATA_KEY = os.getenv("SPORTSDATA_API_KEY")
SPORTSDATA_BASE = "https://api.sportsdata.io/v3/mlb/odds/json"
HEADERS = {"Ocp-Apim-Subscription-Key": SPORTSDATA_KEY}

# Map SportsData prop descriptions to SlipIQ prop types
PROP_MAP = {
    # Pitcher props
    "Pitching Strikeouts": "Strikeouts",
    "Pitching Hits":       "Hits Allowed",
    "Pitching Runs":       "Runs Allowed",

    # Batter props
    "Hits":                "hits",
    "Total Bases":         "total_bases",
    "Runs Batted In":      "rbi",
    "Runs":                "runs",
    "Home Runs":           "home_runs",
    "Strikeouts":          "batter_strikeouts",
}

# Which props are pitcher vs batter
PITCHER_PROPS = {"Strikeouts", "Hits Allowed", "Runs Allowed"}
BATTER_PROPS = {"hits", "total_bases", "rbi", "runs", "home_runs", "batter_strikeouts"}


# ─── Fetch Props ──────────────────────────────────────────────

def get_props_today():
    """
    Pull all player props for today from SportsData.io
    Returns raw list of prop dicts
    """
    if not SPORTSDATA_KEY:
        print("ERROR: SPORTSDATA_API_KEY not set in .env")
        return []

    today = date.today().strftime("%Y-%b-%d").upper()
    url = f"{SPORTSDATA_BASE}/PlayerPropsByDate/{today}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        props = response.json()
        print(f"SportsData.io: {len(props)} props fetched for {today}")
        return props
    except Exception as e:
        print(f"SportsData.io error: {e}")
        return []


# ─── Parse Pitcher Props ──────────────────────────────────────

def get_pitcher_props():
    """
    Parse pitcher props from SportsData.io
    Returns dict keyed by pitcher name with all prop lines
    """
    raw = get_props_today()
    if not raw:
        return {}

    pitcher_props = {}

    for item in raw:
        desc = item.get("Description", "")
        prop_type = PROP_MAP.get(desc)

        if not prop_type or prop_type not in PITCHER_PROPS:
            continue

        name = item.get("Name", "").strip()
        team = item.get("Team", "")
        opponent = item.get("Opponent", "")
        line = item.get("OverUnder")

        if not name or line is None:
            continue

        if name not in pitcher_props:
            pitcher_props[name] = {
                "team": team,
                "opponent": opponent,
                "bookmaker": "SportsData",
                "home_team": "",
                "away_team": "",
                "props": {}
            }

        if prop_type not in pitcher_props[name]["props"]:
            pitcher_props[name]["props"][prop_type] = {}

        # SportsData uses OverUnder as the line
        # Both Over and Under available at this line
        pitcher_props[name]["props"][prop_type]["Over"] = {
            "line": float(line),
            "odds": -110,
        }
        pitcher_props[name]["props"][prop_type]["Under"] = {
            "line": float(line),
            "odds": -110,
        }

    print(f"SportsData.io pitcher props: {len(pitcher_props)} pitchers")
    return pitcher_props


# ─── Parse Batter Props ───────────────────────────────────────

def get_batter_props():
    """
    Parse batter props from SportsData.io
    Returns list of prop dicts compatible with slipiq_batter_lines
    """
    raw = get_props_today()
    if not raw:
        return []

    props = []
    seen = set()

    for item in raw:
        desc = item.get("Description", "")
        prop_type = PROP_MAP.get(desc)

        if not prop_type or prop_type not in BATTER_PROPS:
            continue

        name = item.get("Name", "").strip()
        team = item.get("Team", "")
        opponent = item.get("Opponent", "")
        line = item.get("OverUnder")

        if not name or line is None:
            continue

        key = f"{name}_{prop_type}_{line}"
        if key in seen:
            continue
        seen.add(key)

        # Add both Over and Under
        for direction in ["Over", "Under"]:
            props.append({
                "batter": name,
                "prop_type": prop_type,
                "line": float(line),
                "direction": direction,
                "odds": -110,
                "home_team": "",
                "away_team": "",
                "bookmaker": "SportsData",
                "team": team,
                "opponent": opponent,
            })

    print(f"SportsData.io batter props: {len(set(p['batter'] for p in props))} batters")
    return props


# ─── Summary ──────────────────────────────────────────────────

def print_prop_summary():
    """Print a summary of all available props today"""
    raw = get_props_today()
    if not raw:
        return

    from collections import Counter
    desc_counts = Counter(item.get("Description", "") for item in raw)

    print("\n=== SportsData.io Props Summary ===")
    for desc, count in sorted(desc_counts.items()):
        prop_type = PROP_MAP.get(desc, "unmapped")
        category = "PITCHER" if prop_type in PITCHER_PROPS else "BATTER" if prop_type in BATTER_PROPS else "OTHER"
        print(f"  [{category}] {desc}: {count} props → {prop_type}")


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ SportsData.io Test ===\n")

    print_prop_summary()

    print("\n--- Pitcher Props ---")
    pitchers = get_pitcher_props()
    for name, data in list(pitchers.items())[:5]:
        print(f"\n{name} ({data['team']} vs {data['opponent']})")
        for prop, sides in data["props"].items():
            line = sides.get("Over", {}).get("line", "?")
            print(f"  {prop}: {line}")

    print("\n--- Batter Props (first 5) ---")
    batters = get_batter_props()
    seen_batters = set()
    count = 0
    for prop in batters:
        if prop["direction"] == "Over" and prop["batter"] not in seen_batters:
            seen_batters.add(prop["batter"])
            print(f"  {prop['batter']} — {prop['prop_type']} O/U {prop['line']}")
            count += 1
            if count >= 5:
                break
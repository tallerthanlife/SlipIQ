"""
SlipIQ SportsData.io Source
Primary prop line source — free tier, no quota issues
Only returns props for UPCOMING unplayed games (BetResult is None)
Filters out resolved/scrambled lines automatically
Minimum line thresholds enforce real betting lines only
Looks up to 3 days ahead to find upcoming props
"""

import requests
import os
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

SPORTSDATA_KEY = os.getenv("SPORTSDATA_API_KEY")
SPORTSDATA_BASE = "https://api.sportsdata.io/v3/mlb/odds/json"
HEADERS = {"Ocp-Apim-Subscription-Key": SPORTSDATA_KEY}

# Map SportsData descriptions to SlipIQ prop types
PROP_MAP = {
    "Pitching Strikeouts": "Strikeouts",
    "Pitching Hits":       "Hits Allowed",
    "Pitching Runs":       "Runs Allowed",
    "Hits":                "hits",
    "Total Bases":         "total_bases",
    "Runs Batted In":      "rbi",
    "Runs":                "runs",
    "Home Runs":           "home_runs",
}

PITCHER_PROPS = {"Strikeouts", "Hits Allowed", "Runs Allowed"}
BATTER_PROPS  = {"hits", "total_bases", "rbi", "runs", "home_runs"}

# Minimum realistic betting lines
# Anything below these is a probability not a real sportsbook line
MIN_LINES = {
    "Strikeouts":   2.0,
    "Hits Allowed": 2.0,
    "Runs Allowed": 1.0,
    "hits":         0.45,
    "total_bases":  1.2,
    "rbi":          0.45,
    "runs":         0.45,
    "home_runs":    0.35,
}


# ─── Fetch Props ──────────────────────────────────────────────

def _fetch_props_for_date(target_date):
    """Pull raw props from SportsData.io for a specific date"""
    if not SPORTSDATA_KEY:
        print("ERROR: SPORTSDATA_API_KEY not set in .env")
        return []

    date_str = target_date.strftime("%Y-%b-%d").upper()
    url = f"{SPORTSDATA_BASE}/PlayerPropsByDate/{date_str}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        props = response.json()
        print(f"SportsData.io: {len(props)} raw props for {date_str}")
        return props
    except Exception as e:
        print(f"SportsData.io fetch error for {date_str}: {e}")
        return []


def get_props_today():
    """
    Get upcoming props — tries today, tomorrow, day after
    Only returns props where BetResult is None (not yet resolved)
    Returns first date that has upcoming props
    """
    today = date.today()

    for days_ahead in range(3):
        target = today + timedelta(days=days_ahead)
        raw = _fetch_props_for_date(target)
        upcoming = [p for p in raw if p.get("BetResult") is None]

        if upcoming:
            label = ["today", "tomorrow", "day after tomorrow"][days_ahead]
            print(f"Upcoming props {label} ({target.strftime('%Y-%m-%d')}): {len(upcoming)}")
            return upcoming

        label = ["today", "tomorrow", "day after tomorrow"][days_ahead]
        print(f"No upcoming props {label} — checking next day...")

    print("No upcoming props found in next 3 days")
    return []


# ─── Parse Pitcher Props ──────────────────────────────────────

def get_pitcher_props():
    """
    Parse pitcher props — upcoming games, real lines only
    Returns dict keyed by pitcher name
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
        over_payout  = item.get("OverPayout") or -110
        under_payout = item.get("UnderPayout") or -110

        if not name or line is None:
            continue

        line = float(line)

        # Filter out probability-based resolved data
        min_line = MIN_LINES.get(prop_type, 0)
        if line < min_line:
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

        pitcher_props[name]["props"][prop_type] = {
            "Over":  {"line": line, "odds": float(over_payout)},
            "Under": {"line": line, "odds": float(under_payout)},
        }

    print(f"SportsData.io pitcher props: {len(pitcher_props)} pitchers")
    return pitcher_props


# ─── Parse Batter Props ───────────────────────────────────────

def get_batter_props():
    """
    Parse batter props — upcoming games, real lines only
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
        over_payout  = item.get("OverPayout") or -110
        under_payout = item.get("UnderPayout") or -110

        if not name or line is None:
            continue

        line = float(line)

        # Filter out probability-based data
        min_line = MIN_LINES.get(prop_type, 0)
        if line < min_line:
            continue

        key = f"{name}_{prop_type}_{line}"
        if key in seen:
            continue
        seen.add(key)

        props.append({
            "batter": name,
            "prop_type": prop_type,
            "line": line,
            "direction": "Over",
            "odds": float(over_payout),
            "home_team": "",
            "away_team": "",
            "bookmaker": "SportsData",
            "team": team,
            "opponent": opponent,
        })
        props.append({
            "batter": name,
            "prop_type": prop_type,
            "line": line,
            "direction": "Under",
            "odds": float(under_payout),
            "home_team": "",
            "away_team": "",
            "bookmaker": "SportsData",
            "team": team,
            "opponent": opponent,
        })

    batters = len(set(p["batter"] for p in props if p["direction"] == "Over"))
    print(f"SportsData.io batter props: {batters} batters")
    return props


# ─── Summary ──────────────────────────────────────────────────

def print_prop_summary():
    """Print summary of upcoming props with line validation"""
    raw = get_props_today()
    if not raw:
        print("No upcoming props found")
        return

    from collections import Counter, defaultdict
    desc_counts = Counter(item.get("Description", "") for item in raw)
    line_samples = defaultdict(list)
    for item in raw:
        desc = item.get("Description", "")
        line = item.get("OverUnder")
        if line:
            line_samples[desc].append(float(line))

    print("\n=== SportsData.io Upcoming Props ===")
    for desc, count in sorted(desc_counts.items()):
        prop_type = PROP_MAP.get(desc, "unmapped")
        category = "PITCHER" if prop_type in PITCHER_PROPS else "BATTER" if prop_type in BATTER_PROPS else "OTHER"
        lines = line_samples.get(desc, [])
        sample = f"{min(lines):.1f} - {max(lines):.1f}" if lines else "?"
        min_line = MIN_LINES.get(prop_type, 0)
        valid_count = sum(1 for l in lines if l >= min_line)
        print(f"  [{category}] {desc}: {count} props | lines: {sample} | valid: {valid_count}")


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ SportsData.io Test ===\n")

    print_prop_summary()

    print("\n--- Pitcher Props (first 5) ---")
    pitchers = get_pitcher_props()
    for name, data in list(pitchers.items())[:5]:
        print(f"\n{name} ({data['team']} vs {data['opponent']})")
        for prop, sides in data["props"].items():
            line = sides["Over"]["line"]
            over_odds = sides["Over"]["odds"]
            under_odds = sides["Under"]["odds"]
            print(f"  {prop}: {line} | O{over_odds} / U{under_odds}")

    print("\n--- Batter Props (first 5) ---")
    batters = get_batter_props()
    seen_batters = set()
    count = 0
    for prop in batters:
        if prop["direction"] == "Over" and prop["batter"] not in seen_batters:
            seen_batters.add(prop["batter"])
            print(f"  {prop['batter']} — {prop['prop_type']} {prop['line']} | O{prop['odds']}")
            count += 1
            if count >= 5:
                break
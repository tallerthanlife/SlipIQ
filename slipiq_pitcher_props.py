"""
SlipIQ Pitcher Props — Full Market Coverage
Primary source: SportsData.io (free, no quota)
Fallback: Odds API (cached, preserves quota)
Prop types: Strikeouts, Outs Recorded, Hits Allowed, Runs Allowed
"""

import os
from dotenv import load_dotenv

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
BASE_URL = "https://api.the-odds-api.com/v4"
MAX_EVENTS = int(os.getenv("ODDS_MAX_EVENTS", "15"))

PITCHER_MARKETS = [
    "pitcher_strikeouts",
    "pitcher_outs",
]

MARKET_LABELS = {
    "pitcher_strikeouts": "Strikeouts",
    "pitcher_outs": "Outs Recorded",
}


# ─── Fetch Pitcher Props ──────────────────────────────────────

def get_all_pitcher_props():
    """
    Pull pitcher props — SportsData.io primary, Odds API fallback
    Returns dict keyed by pitcher name in standard format
    """
    # 1. Try SportsData.io first (free, no quota limits)
    try:
        from slipiq_sportsdata import get_pitcher_props as sd_pitchers
        props = sd_pitchers()
        if props:
            print(f"Pitcher props from SportsData.io: {len(props)} pitchers")
            return props
    except Exception as e:
        print(f"SportsData.io pitcher props failed: {e}")

    # 2. Fallback to Odds API with cache
    print("Falling back to Odds API for pitcher props...")
    if not ODDS_API_KEY:
        print("ERROR: ODDS_API_KEY not set")
        return {}

    try:
        from slipiq_cache import get_events_cached, get_event_odds_cached
    except Exception as e:
        print(f"Cache error: {e}")
        return {}

    events = get_events_cached(ODDS_API_KEY, BASE_URL)
    if not events:
        return {}

    print(f"Pulling pitcher props for {min(len(events), MAX_EVENTS)} games...")

    # Supplement with Pinnacle
    pinnacle_props = {}
    try:
        from slipiq_pinnacle_props import get_pinnacle_pitcher_props
        p_props = get_pinnacle_pitcher_props()
        for pp in p_props:
            name = pp["pitcher"]
            if name not in pinnacle_props:
                pinnacle_props[name] = pp
    except Exception:
        pass

    pitcher_props = {}

    for event in events[:MAX_EVENTS]:
        event_id = event["id"]
        home = event["home_team"]
        away = event["away_team"]

        prop_data = get_event_odds_cached(
            event_id,
            ",".join(PITCHER_MARKETS),
            ODDS_API_KEY,
            BASE_URL,
        )
        if not prop_data:
            continue

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

    for name, pp in pinnacle_props.items():
        if name not in pitcher_props and pp.get("direction") == "Over":
            pitcher_props[name] = {
                "home_team": pp.get("home_team", ""),
                "away_team": pp.get("away_team", ""),
                "bookmaker": "Pinnacle",
                "props": {
                    "Strikeouts": {
                        "Over": {"line": pp["line"], "odds": pp.get("odds", -110)}
                    }
                }
            }

    return pitcher_props


# ─── Enhanced Game Log ────────────────────────────────────────

def get_enhanced_game_log(pitcher_name):
    """Pull game log with all pitching stats"""
    import statsapi
    import pandas as pd
    from slipiq_mlb_data import get_pitcher_id

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


# ─── Project Each Prop ────────────────────────────────────────

def project_pitcher_stat(df, stat_type):
    """Project a pitcher stat from game log data"""
    if df is None or df.empty or len(df) < 3:
        return None

    col_map = {
        "Strikeouts": "strikeouts",
        "Outs Recorded": "outs",
        "Hits Allowed": "hits",
        "Runs Allowed": "hits",
        "Walks": "walks",
        "Pitches Thrown": "pitches",
    }

    col = col_map.get(stat_type)
    if not col or col not in df.columns:
        return None

    series = df[col].dropna()
    if len(series) < 3:
        return None

    season_avg = round(float(series.mean()), 1)
    last_5 = round(float(series.head(5).mean()), 1)
    last_3 = round(float(series.head(3).mean()), 1)
    std = float(series.std())

    projection = round(season_avg * 0.30 + last_5 * 0.40 + last_3 * 0.30, 1)
    confidence = round(max(40, min(90, 100 - (std * 8))), 1)

    if last_3 > last_5 > season_avg:
        trend = "HOT"
    elif last_3 < last_5 < season_avg:
        trend = "COLD"
    else:
        trend = "NEUTRAL"

    return {
        "projection": projection,
        "season_avg": season_avg,
        "last_5_avg": last_5,
        "last_3_avg": last_3,
        "std_dev": round(std, 2),
        "confidence": confidence,
        "games_analyzed": len(series),
        "trend": trend,
    }


# ─── Full Analysis ────────────────────────────────────────────

def run_full_pitcher_props_analysis():
    """
    Pull all pitcher prop markets, run projections, enrich with confidence agent
    Returns list of picks sorted by confidence
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

        bookmaker = data.get("bookmaker", "Unknown")
        home_team = data.get("home_team", "")
        away_team = data.get("away_team", "")

        for prop_label, prop_sides in data["props"].items():
            over_data = prop_sides.get("Over", {})
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

            min_edge = {
                "Strikeouts": 0.5,
                "Outs Recorded": 1.5,
                "Hits Allowed": 0.5,
                "Runs Allowed": 0.5,
            }.get(prop_label, 0.5)

            min_conf = 55

            if confidence < min_conf or edge < min_edge:
                continue

            direction = "OVER" if proj > line else "UNDER"

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
                "trend": proj_data["trend"],
                "games_analyzed": proj_data["games_analyzed"],
                "bookmaker": bookmaker,
                "home_team": home_team,
                "away_team": away_team,
                "recommendation": f"{direction} {line} | Grade: {grade} | Confidence: {confidence}%",
            })

    all_picks.sort(key=lambda x: x["confidence"], reverse=True)

    # Run confidence agent on strikeout picks only
    try:
        from slipiq_confidence_agent import enrich_picks
        k_picks = [p for p in all_picks if p["prop_type"] == "Strikeouts"]
        other_picks = [p for p in all_picks if p["prop_type"] != "Strikeouts"]

        if k_picks:
            print("Running confidence agent on strikeout picks...")
            k_picks = enrich_picks(k_picks)
            all_picks = k_picks + other_picks
            all_picks.sort(key=lambda x: x["confidence"], reverse=True)
    except Exception as e:
        print(f"Confidence agent skipped: {e}")

    # Print results grouped by pitcher
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
                print(f"\n🔵 {current_pitcher} ({pick['bookmaker']})")
            print(f"  {pick['prop_type']}: {pick['direction']} {pick['line']} "
                  f"| Proj: {pick['projection']} | {pick['trend']} "
                  f"| Grade: {pick['grade']} | Conf: {pick['confidence']}%")

    print(f"\nTotal pitcher prop picks: {len(all_picks)}")
    return all_picks


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    run_full_pitcher_props_analysis()
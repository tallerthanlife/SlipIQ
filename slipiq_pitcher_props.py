"""
SlipIQ Pitcher Props — Full Market Coverage
Primary source: SportsData.io (free, no quota)
Fallback: Odds API (cached, preserves quota)
Prop types: Strikeouts, Outs Recorded, Hits Allowed, Runs Allowed

Strict curation:
- Min edge per prop type
- Min confidence 68%
- Grade A and B only
- Trend alignment enforced
- Min 5 games analyzed
- Hard cap 15 picks
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
    "pitcher_outs":       "Outs Recorded",
}

# Strict minimum edges per prop type
MIN_EDGE = {
    "Strikeouts":    1.0,
    "Outs Recorded": 3.0,
    "Hits Allowed":  1.5,
    "Runs Allowed":  1.0,
}

MIN_CONFIDENCE  = 68
MIN_GAMES       = 5
MAX_PICKS       = 15


# ─── Fetch Pitcher Props ──────────────────────────────────────

def get_all_pitcher_props():
    """
    Pull pitcher props — SportsData.io primary, Odds API fallback
    Returns dict keyed by pitcher name
    """
    # 1. SportsData.io first
    try:
        from slipiq_sportsdata import get_pitcher_props as sd_pitchers
        props = sd_pitchers()
        if props:
            print(f"Pitcher props from SportsData.io: {len(props)} pitchers")
            return props
    except Exception as e:
        print(f"SportsData.io pitcher props failed: {e}")

    # 2. Odds API fallback
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

    pinnacle_props = {}
    try:
        from slipiq_pinnacle_props import get_pinnacle_pitcher_props
        for pp in get_pinnacle_pitcher_props():
            name = pp["pitcher"]
            if name not in pinnacle_props:
                pinnacle_props[name] = pp
    except Exception:
        pass

    pitcher_props = {}

    for event in events[:MAX_EVENTS]:
        event_id  = event["id"]
        home      = event["home_team"]
        away      = event["away_team"]

        prop_data = get_event_odds_cached(
            event_id, ",".join(PITCHER_MARKETS), ODDS_API_KEY, BASE_URL
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
                        "Over":  {"line": pp["line"], "odds": pp.get("odds", -110)},
                        "Under": {"line": pp["line"], "odds": -110},
                    }
                }
            }

    return pitcher_props


# ─── Enhanced Game Log ────────────────────────────────────────

def get_enhanced_game_log(pitcher_name):
    """Pull game log with all pitching stats"""
    import pandas as pd
    from slipiq_mlb_data import get_pitcher_id
    import statsapi

    pitcher_id, _ = get_pitcher_id(pitcher_name)
    if not pitcher_id:
        return None

    try:
        raw    = statsapi.player_stat_data(
            pitcher_id, group="pitching", type="gameLog", sportId=1
        )
        splits = raw.get("stats", [])
        if not splits:
            return None

        rows = []
        for split in splits:
            stat = split.get("stats", {})
            if not stat:
                continue
            try:
                innings    = float(stat.get("inningsPitched", 0) or 0)
                k_per_9    = float(stat.get("strikeoutsPer9Inn", 0) or 0)
                strikeouts = round((k_per_9 / 9) * innings, 0) if innings > 0 else 0
                outs       = round(innings * 3, 0)
                hits       = int(stat.get("hits", 0) or 0)
                walks      = int(stat.get("baseOnBalls", 0) or 0)
                pitches    = int(stat.get("numberOfPitches", 0) or 0)
                rows.append({
                    "innings":    innings,
                    "strikeouts": int(strikeouts),
                    "outs":       int(outs),
                    "hits":       hits,
                    "walks":      walks,
                    "pitches":    pitches,
                })
            except (ValueError, TypeError):
                continue

        return pd.DataFrame(rows) if rows else None

    except Exception as e:
        print(f"Game log error for {pitcher_name}: {e}")
        return None


# ─── Project Each Prop ────────────────────────────────────────

def project_pitcher_stat(df, stat_type):
    """Project a pitcher stat from game log"""
    if df is None or df.empty or len(df) < 3:
        return None

    col_map = {
        "Strikeouts":    "strikeouts",
        "Outs Recorded": "outs",
        "Hits Allowed":  "hits",
        "Runs Allowed":  "hits",
        "Walks":         "walks",
        "Pitches Thrown":"pitches",
    }

    col = col_map.get(stat_type)
    if not col or col not in df.columns:
        return None

    series = df[col].dropna()
    if len(series) < 3:
        return None

    season_avg = round(float(series.mean()), 1)
    last_5     = round(float(series.head(5).mean()), 1)
    last_3     = round(float(series.head(3).mean()), 1)
    std        = float(series.std())

    projection = round(season_avg * 0.30 + last_5 * 0.40 + last_3 * 0.30, 1)
    confidence = round(max(40, min(90, 100 - (std * 8))), 1)

    if last_3 > last_5 > season_avg:
        trend = "HOT"
    elif last_3 < last_5 < season_avg:
        trend = "COLD"
    else:
        trend = "NEUTRAL"

    return {
        "projection":     projection,
        "season_avg":     season_avg,
        "last_5_avg":     last_5,
        "last_3_avg":     last_3,
        "std_dev":        round(std, 2),
        "confidence":     confidence,
        "games_analyzed": len(series),
        "trend":          trend,
    }


# ─── Strict Curation ─────────────────────────────────────────

def passes_curation(prop_label, line, proj, confidence, games_analyzed, trend):
    """
    Hard curation gates — all must pass
    Returns (passed, reason)
    """
    edge = abs(proj - line)
    min_edge = MIN_EDGE.get(prop_label, 1.0)

    if games_analyzed < MIN_GAMES:
        return False, f"insufficient games ({games_analyzed} < {MIN_GAMES})"

    if confidence < MIN_CONFIDENCE:
        return False, f"low confidence ({confidence}% < {MIN_CONFIDENCE}%)"

    if edge < min_edge:
        return False, f"edge too small ({edge:.1f} < {min_edge})"

    direction = "OVER" if proj > line else "UNDER"

    # Trend misalignment — block unless edge is exceptional (2x minimum)
    if trend == "HOT" and direction == "UNDER" and edge < min_edge * 2:
        return False, f"trend conflict (HOT pitcher, UNDER pick, edge {edge:.1f})"
    if trend == "COLD" and direction == "OVER" and edge < min_edge * 2:
        return False, f"trend conflict (COLD pitcher, OVER pick, edge {edge:.1f})"

    return True, "passed"


# ─── Full Analysis ────────────────────────────────────────────

def run_full_pitcher_props_analysis():
    """
    Pull pitcher props, run projections, apply strict curation
    Returns top 15 picks maximum sorted by confidence
    """
    print("\n=== SlipIQ Full Pitcher Props Analysis ===\n")

    pitcher_props = get_all_pitcher_props()
    if not pitcher_props:
        print("No pitcher props available")
        return []

    print(f"Found {len(pitcher_props)} pitchers with props\n")

    raw_picks     = []
    skipped_count = 0

    for pitcher_name, data in pitcher_props.items():
        df = get_enhanced_game_log(pitcher_name)
        if df is None or df.empty:
            continue

        bookmaker  = data.get("bookmaker", "Unknown")
        home_team  = data.get("home_team", "")
        away_team  = data.get("away_team", "")

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

            proj       = proj_data["projection"]
            confidence = proj_data["confidence"]
            trend      = proj_data["trend"]
            games      = proj_data["games_analyzed"]

            passed, reason = passes_curation(
                prop_label, line, proj, confidence, games, trend
            )

            if not passed:
                skipped_count += 1
                continue

            direction = "OVER" if proj > line else "UNDER"
            edge      = abs(proj - line)
            min_edge  = MIN_EDGE.get(prop_label, 1.0)

            # Grade — tighter thresholds now
            if confidence >= 76 and edge >= min_edge * 2:
                grade = "A"
            elif confidence >= 68 and edge >= min_edge:
                grade = "B"
            else:
                skipped_count += 1
                continue  # No Grade C in final slate

            raw_picks.append({
                "pitcher":       pitcher_name,
                "prop_type":     prop_label,
                "line":          line,
                "projection":    proj,
                "direction":     direction,
                "confidence":    confidence,
                "grade":         grade,
                "season_avg":    proj_data["season_avg"],
                "last_3_avg":    proj_data["last_3_avg"],
                "last_5_avg":    proj_data["last_5_avg"],
                "trend":         trend,
                "games_analyzed":games,
                "bookmaker":     bookmaker,
                "home_team":     home_team,
                "away_team":     away_team,
                "recommendation":f"{direction} {line} | Grade: {grade} | Confidence: {confidence}%",
            })

    print(f"Curation: {len(raw_picks)} passed, {skipped_count} filtered out\n")

    # Sort by confidence
    raw_picks.sort(key=lambda x: x["confidence"], reverse=True)

    # Run confidence agent on strikeout picks
    try:
        from slipiq_confidence_agent import enrich_picks
        k_picks    = [p for p in raw_picks if p["prop_type"] == "Strikeouts"]
        other_picks = [p for p in raw_picks if p["prop_type"] != "Strikeouts"]
        if k_picks:
            print("Running confidence agent on strikeout picks...")
            k_picks = enrich_picks(k_picks)
            raw_picks = k_picks + other_picks
            raw_picks.sort(key=lambda x: x["confidence"], reverse=True)
    except Exception as e:
        print(f"Confidence agent skipped: {e}")

    # Hard cap at MAX_PICKS
    final_picks = raw_picks[:MAX_PICKS]

    # Print results
    print("\n" + "="*50)
    print(f"SlipIQ PITCHER PROPS — TOP {len(final_picks)} PICKS")
    print("="*50)

    if not final_picks:
        print("No picks passed curation today")
    else:
        current_pitcher = None
        for pick in final_picks:
            if pick["pitcher"] != current_pitcher:
                current_pitcher = pick["pitcher"]
                print(f"\n🔵 {current_pitcher} ({pick['bookmaker']})")
            print(
                f"  {pick['prop_type']}: {pick['direction']} {pick['line']} "
                f"| Proj: {pick['projection']} | {pick['trend']} "
                f"| Grade: {pick['grade']} | Conf: {pick['confidence']}%"
            )

    print(f"\nFinal picks: {len(final_picks)} (cap: {MAX_PICKS})")
    return final_picks


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    run_full_pitcher_props_analysis()
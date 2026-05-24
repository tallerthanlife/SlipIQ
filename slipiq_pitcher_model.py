"""
SlipIQ Pitcher Strikeout Model
Phase 1 - Core projection engine
"""

import statsapi
import pandas as pd
import numpy as np
from slipiq_mlb_data import get_todays_games, get_pitcher_id, get_pitcher_game_log

# ─── Parse Game Log ───────────────────────────────────────────

def _parse_innings_pitched(value):
    """MLB IP strings: 6.0 = 6.0, 6.1 = 6⅓, 6.2 = 6⅔."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    if "." not in text:
        return float(text)
    whole, outs = text.split(".", 1)
    return float(whole) + (float(outs[0]) / 3.0 if outs else 0.0)


def parse_game_log(splits):
    """Build a game log DataFrame from MLB gameLog splits (newest first)."""
    try:
        if isinstance(splits, dict):
            # Legacy statsapi.player_stat_data payload
            legacy = splits.get("stats", [])
            if legacy and isinstance(legacy[0].get("stats"), dict):
                return pd.DataFrame()
            splits = legacy

        if not splits:
            return pd.DataFrame()

        rows = []
        for split in splits:
            stat = split.get("stat") or split.get("stats") or {}
            if not stat:
                continue

            strikeouts = int(stat.get("strikeOuts", stat.get("strikeouts", 0)) or 0)
            opponent = split.get("opponent", {})
            opponent_name = opponent.get("name", "") if isinstance(opponent, dict) else ""

            rows.append({
                "date": split.get("date", ""),
                "opponent": opponent_name,
                "strikeouts": strikeouts,
                "innings": _parse_innings_pitched(stat.get("inningsPitched")),
                "hits": int(stat.get("hits", 0) or 0),
                "walks": int(stat.get("baseOnBalls", 0) or 0),
                "home_away": bool(split.get("isHome", True)),
            })

        df = pd.DataFrame(rows)
        if df.empty:
            return df

        if "date" in df.columns:
            df = df.sort_values("date", ascending=False).reset_index(drop=True)
        return df

    except Exception as e:
        print(f"Error parsing game log: {e}")
        return pd.DataFrame()

# ─── Core Model ───────────────────────────────────────────────

def calculate_projection(df, pitcher_name):
    """
    Calculate strikeout projection from game log data
    Returns projection dict with over/under recommendation
    """
    if df.empty or len(df) < 3:
        return None

    # Season average
    season_avg = df["strikeouts"].mean()
    season_std = df["strikeouts"].std()

    # Last 5 starts trend
    last_5 = df.head(5)["strikeouts"].mean()

    # Last 3 starts momentum
    last_3 = df.head(3)["strikeouts"].mean()

    # Home/away split
    home_games = df[df["home_away"] == True]["strikeouts"].mean()
    away_games = df[df["home_away"] == False]["strikeouts"].mean()

    # Weighted projection
    # Weights: recent form matters more than season avg
    projection = (
        season_avg * 0.30 +
        last_5 * 0.40 +
        last_3 * 0.30
    )

    # Trend direction
    if last_3 > last_5 > season_avg:
        trend = "HOT"
    elif last_3 < last_5 < season_avg:
        trend = "COLD"
    else:
        trend = "NEUTRAL"

    # Confidence score (0-100)
    games_played = len(df)
    consistency = max(0, 100 - (season_std * 10))
    sample_bonus = min(20, games_played)
    confidence = round((consistency * 0.7) + (sample_bonus * 1.5), 1)
    confidence = min(95, max(40, confidence))

    return {
        "pitcher": pitcher_name,
        "projection": round(projection, 1),
        "season_avg": round(season_avg, 1),
        "last_5_avg": round(last_5, 1),
        "last_3_avg": round(last_3, 1),
        "home_avg": round(home_games, 1) if not np.isnan(home_games) else "N/A",
        "away_avg": round(away_games, 1) if not np.isnan(away_games) else "N/A",
        "trend": trend,
        "confidence": confidence,
        "games_analyzed": games_played,
        "std_dev": round(season_std, 2),
    }

def get_recommendation(projection, line):
    """
    Given a projection and a betting line, return OVER/UNDER/PASS
    """
    if projection is None:
        return "PASS - insufficient data"

    proj = projection["projection"]
    diff = proj - line
    confidence = projection["confidence"]

    if abs(diff) < 0.5:
        return f"PASS - projection ({proj}) too close to line ({line})"

    direction = "OVER" if diff > 0 else "UNDER"

    if confidence >= 70 and abs(diff) >= 1.0:
        grade = "A"
    elif confidence >= 60 and abs(diff) >= 0.75:
        grade = "B"
    else:
        grade = "C"

    return f"{direction} {line} | Grade: {grade} | Confidence: {confidence}%"

# ─── Main Runner ──────────────────────────────────────────────

def run_pitcher_model(pitcher_name, line=None, verbose=True):
    """
    Full pipeline for one pitcher.
    Set verbose=False when called from the daily lines batch job.
    """
    if verbose:
        print(f"\n{'='*50}")
        print(f"SlipIQ Strikeout Model: {pitcher_name}")
        print(f"{'='*50}")

    pitcher_id, full_name = get_pitcher_id(pitcher_name)
    if not pitcher_id:
        if verbose:
            print(f"Player not found: {pitcher_name}")
        return None

    splits = get_pitcher_game_log(pitcher_id)
    df = parse_game_log(splits)

    if df.empty:
        if verbose:
            print("No game log data available")
        return None

    if verbose:
        print(f"Games analyzed: {len(df)}")
        print(f"\nRecent starts:")
        print(df[["date", "opponent", "strikeouts", "innings"]].head(5).to_string(index=False))

    projection = calculate_projection(df, full_name)

    if projection and verbose:
        print(f"\n--- Projection ---")
        print(f"Season avg:    {projection['season_avg']} K")
        print(f"Last 5 starts: {projection['last_5_avg']} K")
        print(f"Last 3 starts: {projection['last_3_avg']} K")
        print(f"Trend:         {projection['trend']}")
        print(f"PROJECTION:    {projection['projection']} K")
        print(f"Confidence:    {projection['confidence']}%")

        if line:
            rec = get_recommendation(projection, line)
            print(f"\n--- Recommendation ---")
            print(f"Line: {line} strikeouts")
            print(f"Pick: {rec}")

    return projection

# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    # Test with today's pitchers
    print("=== SlipIQ Pitcher Model Test ===")

    # Test 1 - known pitcher with a line
    run_pitcher_model("Gerrit Cole", line=6.5)

    # Test 2 - today's slate
    print("\n\n=== Today's Slate ===")
    games = get_todays_games()
    for game in games[:3]:
        for pitcher in [game["home_pitcher"], game["away_pitcher"]]:
            if pitcher and pitcher != "TBD":
                run_pitcher_model(pitcher)
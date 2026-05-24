"""
SlipIQ MLB Data Pipeline
Pulls pitcher and game data from Baseball Savant + MLB Stats API
"""

import requests
import pandas as pd
import statsapi
from pybaseball import statcast_pitcher, pitching_stats
from datetime import datetime, timedelta
import time

# ─── MLB Stats API ───────────────────────────────────────────

def get_todays_games():
    """Get all MLB games scheduled for today"""
    today = datetime.now().strftime("%Y-%m-%d")
    schedule = statsapi.schedule(date=today)
    games = []
    for game in schedule:
        games.append({
            "game_id": game["game_id"],
            "home_team": game["home_name"],
            "away_team": game["away_name"],
            "status": game["status"],
            "home_pitcher": game.get("home_probable_pitcher", "TBD"),
            "away_pitcher": game.get("away_probable_pitcher", "TBD"),
        })
    return games

def get_pitcher_id(pitcher_name):
    """Look up MLB Stats API player ID by name"""
    results = statsapi.lookup_player(pitcher_name)
    if results:
        return results[0]["id"], results[0]["fullName"]
    return None, None

def get_pitcher_game_log(pitcher_id, season=2025):
    """Get pitcher's game log for the season"""
    stats = statsapi.player_stat_data(
        pitcher_id,
        group="pitching",
        type="gameLog",
        sportId=1
    )
    return stats

# ─── Baseball Savant ─────────────────────────────────────────

def get_savant_pitcher_stats(pitcher_name, season=2025):
    """Pull Statcast data for a pitcher from Baseball Savant"""
    try:
        start_date = f"{season}-03-01"
        end_date = datetime.now().strftime("%Y-%m-%d")
        data = pitching_stats(season, season, qual=0)
        pitcher_data = data[data["Name"].str.contains(
            pitcher_name.split()[-1], case=False, na=False
        )]
        if pitcher_data.empty:
            print(f"No Savant data found for {pitcher_name}")
            return None
        return pitcher_data
    except Exception as e:
        print(f"Savant error for {pitcher_name}: {e}")
        return None

def get_pitcher_statcast(pitcher_mlbam_id, season=2025):
    """Get pitch-level Statcast data for strikeout modeling"""
    try:
        start = f"{season}-03-01"
        end = datetime.now().strftime("%Y-%m-%d")
        data = statcast_pitcher(start, end, pitcher_mlbam_id)
        if data.empty:
            print(f"No Statcast data for player ID {pitcher_mlbam_id}")
            return None
        return data
    except Exception as e:
        print(f"Statcast error: {e}")
        return None

# ─── Combined Pipeline ────────────────────────────────────────

def get_pitcher_profile(pitcher_name):
    """
    Master function - pulls everything we need for
    the strikeout model for one pitcher
    """
    print(f"\n>>> Building profile for: {pitcher_name}")

    # Step 1 - Get MLB ID
    pitcher_id, full_name = get_pitcher_id(pitcher_name)
    if not pitcher_id:
        print(f"Could not find player ID for {pitcher_name}")
        return None

    print(f"Found: {full_name} | ID: {pitcher_id}")

    # Step 2 - Get season stats from Savant
    savant_stats = get_savant_pitcher_stats(pitcher_name)
    if savant_stats is not None:
        print(f"Savant stats loaded: {len(savant_stats)} rows")

    # Step 3 - Get game log
    game_log = get_pitcher_game_log(pitcher_id)
    print(f"Game log loaded")

    return {
        "name": full_name,
        "mlb_id": pitcher_id,
        "savant_stats": savant_stats,
        "game_log": game_log
    }

# ─── Test Run ─────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ MLB Data Pipeline Test ===\n")

    # Test 1 - Today's games
    print(">>> Fetching today's games...")
    games = get_todays_games()
    if games:
        for g in games[:3]:
            print(f"  {g['away_team']} @ {g['home_team']}")
            print(f"  Pitchers: {g['away_pitcher']} vs {g['home_pitcher']}")
            print()
    else:
        print("  No games today or API unavailable")

    # Test 2 - Pitcher profile
    profile = get_pitcher_profile("Gerrit Cole")
    if profile:
        print(f"\nProfile built for {profile['name']}")
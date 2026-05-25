"""
SlipIQ Batter Model
Projects hits, total bases, and RBI for batters
Uses MLB Stats API game logs — same approach as pitcher model
"""

import statsapi
import pandas as pd
import numpy as np
from slipiq_mlb_data import get_pitcher_id

# ─── Get Batter Data ──────────────────────────────────────────

def get_batter_id(batter_name):
    """Look up MLB Stats API player ID by name"""
    results = statsapi.lookup_player(batter_name)
    if results:
        return results[0]["id"], results[0]["fullName"]
    return None, None


def get_batter_game_log(batter_id, season=2025):
    """Get batter's game log for the season"""
    try:
        stats = statsapi.player_stat_data(
            batter_id,
            group="hitting",
            type="gameLog",
            sportId=1
        )
        return stats
    except Exception as e:
        print(f"Batter game log error: {e}")
        return {}


def parse_batter_game_log(game_log_data):
    """Extract hitting stats from MLB Stats API game log"""
    try:
        splits = game_log_data.get("stats", [])
        if not splits:
            return pd.DataFrame()

        rows = []
        for split in splits:
            stat = split.get("stats", {})
            if not stat:
                continue

            ab = int(stat.get("atBats", 0) or 0)
            hits = int(stat.get("hits", 0) or 0)
            doubles = int(stat.get("doubles", 0) or 0)
            triples = int(stat.get("triples", 0) or 0)
            hr = int(stat.get("homeRuns", 0) or 0)
            rbi = int(stat.get("rbi", 0) or 0)

            # Calculate total bases
            total_bases = hits + doubles + (triples * 2) + (hr * 3)

            rows.append({
                "ab": ab,
                "hits": hits,
                "total_bases": total_bases,
                "rbi": rbi,
                "hr": hr,
            })

        df = pd.DataFrame(rows)
        return df

    except Exception as e:
        print(f"Error parsing batter log: {e}")
        return pd.DataFrame()


# ─── Core Projections ─────────────────────────────────────────

def project_hits(df):
    """Project hits for today"""
    if df.empty or len(df) < 3:
        return None

    season_avg = df["hits"].mean()
    last_5 = df.head(5)["hits"].mean()
    last_3 = df.head(3)["hits"].mean()

    projection = (season_avg * 0.30) + (last_5 * 0.40) + (last_3 * 0.30)
    std = df["hits"].std()
    confidence = max(40, min(90, 100 - (std * 15)))

    return {
        "projection": round(projection, 2),
        "season_avg": round(season_avg, 2),
        "last_5_avg": round(last_5, 2),
        "last_3_avg": round(last_3, 2),
        "std_dev": round(std, 2),
        "confidence": round(confidence, 1),
        "games_analyzed": len(df),
    }


def project_total_bases(df):
    """Project total bases for today"""
    if df.empty or len(df) < 3:
        return None

    season_avg = df["total_bases"].mean()
    last_5 = df.head(5)["total_bases"].mean()
    last_3 = df.head(3)["total_bases"].mean()

    projection = (season_avg * 0.30) + (last_5 * 0.40) + (last_3 * 0.30)
    std = df["total_bases"].std()
    confidence = max(40, min(90, 100 - (std * 10)))

    return {
        "projection": round(projection, 2),
        "season_avg": round(season_avg, 2),
        "last_5_avg": round(last_5, 2),
        "last_3_avg": round(last_3, 2),
        "std_dev": round(std, 2),
        "confidence": round(confidence, 1),
        "games_analyzed": len(df),
    }


def project_rbi(df):
    """Project RBI for today"""
    if df.empty or len(df) < 3:
        return None

    season_avg = df["rbi"].mean()
    last_5 = df.head(5)["rbi"].mean()
    last_3 = df.head(3)["rbi"].mean()

    projection = (season_avg * 0.30) + (last_5 * 0.40) + (last_3 * 0.30)
    std = df["rbi"].std()
    confidence = max(40, min(85, 100 - (std * 20)))

    return {
        "projection": round(projection, 2),
        "season_avg": round(season_avg, 2),
        "last_5_avg": round(last_5, 2),
        "last_3_avg": round(last_3, 2),
        "std_dev": round(std, 2),
        "confidence": round(confidence, 1),
        "games_analyzed": len(df),
    }


# ─── Recommendation ───────────────────────────────────────────

def get_batter_recommendation(projection_data, line, prop_type):
    """Generate OVER/UNDER recommendation for a batter prop"""
    if not projection_data:
        return "PASS - insufficient data"

    proj = projection_data["projection"]
    diff = proj - line
    confidence = projection_data["confidence"]

    if abs(diff) < 0.15:
        return f"PASS - projection ({proj}) too close to line ({line})"

    direction = "OVER" if diff > 0 else "UNDER"

    if confidence >= 70 and abs(diff) >= 0.3:
        grade = "A"
    elif confidence >= 60 and abs(diff) >= 0.2:
        grade = "B"
    else:
        grade = "C"

    return f"{direction} {line} | Grade: {grade} | Confidence: {confidence}%"


# ─── Full Batter Profile ──────────────────────────────────────

def run_batter_model(batter_name, hits_line=None, tb_line=None, rbi_line=None, verbose=True):
    """
    Full pipeline for one batter
    Returns projections for all three prop types
    """
    if verbose:
        print(f"\n{'='*50}")
        print(f"SlipIQ Batter Model: {batter_name}")
        print(f"{'='*50}")

    batter_id, full_name = get_batter_id(batter_name)
    if not batter_id:
        if verbose:
            print(f"Player not found: {batter_name}")
        return None

    raw_log = get_batter_game_log(batter_id)
    df = parse_batter_game_log(raw_log)

    if df.empty:
        if verbose:
            print("No game log data available")
        return None

    if verbose:
        print(f"Games analyzed: {len(df)}")

    hits_proj = project_hits(df)
    tb_proj = project_total_bases(df)
    rbi_proj = project_rbi(df)

    result = {
        "name": full_name,
        "mlb_id": batter_id,
        "games_analyzed": len(df),
        "hits": hits_proj,
        "total_bases": tb_proj,
        "rbi": rbi_proj,
    }

    if verbose and hits_proj:
        print(f"\n--- Hits Projection ---")
        print(f"Season avg:    {hits_proj['season_avg']}")
        print(f"Last 5:        {hits_proj['last_5_avg']}")
        print(f"Last 3:        {hits_proj['last_3_avg']}")
        print(f"PROJECTION:    {hits_proj['projection']}")
        print(f"Confidence:    {hits_proj['confidence']}%")
        if hits_line:
            print(f"Recommendation: {get_batter_recommendation(hits_proj, hits_line, 'hits')}")

    if verbose and tb_proj:
        print(f"\n--- Total Bases Projection ---")
        print(f"Season avg:    {tb_proj['season_avg']}")
        print(f"PROJECTION:    {tb_proj['projection']}")
        print(f"Confidence:    {tb_proj['confidence']}%")
        if tb_line:
            print(f"Recommendation: {get_batter_recommendation(tb_proj, tb_line, 'total_bases')}")

    if verbose and rbi_proj:
        print(f"\n--- RBI Projection ---")
        print(f"Season avg:    {rbi_proj['season_avg']}")
        print(f"PROJECTION:    {rbi_proj['projection']}")
        print(f"Confidence:    {rbi_proj['confidence']}%")
        if rbi_line:
            print(f"Recommendation: {get_batter_recommendation(rbi_proj, rbi_line, 'rbi')}")

    return result


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ Batter Model Test ===\n")

    # Test with known hitters
    run_batter_model("Mookie Betts", hits_line=0.5, tb_line=1.5, rbi_line=0.5)
    run_batter_model("Aaron Judge", hits_line=0.5, tb_line=1.5, rbi_line=0.5)
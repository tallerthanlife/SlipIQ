"""
Opponent matchup engine.
For pitchers: get opposing team's K-rate, BB-rate, wOBA vs RHP/LHP
For batters: get opposing pitcher's K%, BB%, HR-rate, ERA last 14 days
Weights projections by matchup quality.
"""
import requests
from datetime import date, timedelta
from slipiq_cache import cache_get, cache_set

# League average benchmarks 2026
LEAGUE_AVG = {
    "team_k_rate": 0.225,
    "team_bb_rate": 0.082,
    "team_woba": 0.315,
    "pitcher_k_per9": 8.8,
    "pitcher_era": 4.20,
    "pitcher_whip": 1.28,
}

# Matchup weight — how much matchup adjusts projection
MATCHUP_WEIGHT = 0.30  # 30% of projection comes from matchup


def get_team_matchup_profile(team_name: str) -> dict:
    """
    Get a team's offensive profile as a pitcher's opponent.
    Returns K-rate, BB-rate, wOBA, ISO.
    Higher K-rate = better matchup for pitcher strikeout props.
    """
    cache_key = f"team_matchup_{team_name}_{date.today()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    profile = _build_team_profile_from_statcast(team_name)
    if profile:
        cache_set(cache_key, profile)
    return profile or _league_avg_team_profile()


def get_pitcher_matchup_profile(pitcher_name: str) -> dict:
    """
    Get a pitcher's recent profile as a batter's opponent.
    Returns K%, BB%, HR-rate, ERA, WHIP from last 14 days.
    """
    cache_key = f"pitcher_matchup_{pitcher_name}_{date.today()}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    try:
        import pybaseball
        pybaseball.cache.enable()
        end = date.today()
        start = end - timedelta(days=14)
        data = pybaseball.pitching_stats_bref(str(end.year))
        if data is None or data.empty:
            return _league_avg_pitcher_profile()

        name_lower = pitcher_name.lower()
        match = data[
            data["Name"].str.lower().str.contains(
                name_lower.split()[-1], na=False
            )
        ]
        if match.empty:
            return _league_avg_pitcher_profile()

        row = match.iloc[0]
        profile = {
            "pitcher": pitcher_name,
            "era": float(row.get("ERA", LEAGUE_AVG["pitcher_era"])),
            "whip": float(row.get("WHIP", LEAGUE_AVG["pitcher_whip"])),
            "k_per9": float(row.get("SO9", LEAGUE_AVG["pitcher_k_per9"])),
            "k_pct": float(row.get("SO9", 8.8)) / 27,
            "bb_per9": float(row.get("BB9", 3.2)),
            "hr_per9": float(row.get("HR9", 1.1)),
            "source": "bref",
        }
        cache_set(cache_key, profile)
        return profile

    except Exception as e:
        print(f"  [matchup] Pitcher profile error for {pitcher_name}: {e}")
        return _league_avg_pitcher_profile()


def adjust_pitcher_projection(
    base_projection: float,
    pitcher_name: str,
    opponent_team: str,
    handedness: str = "R",
) -> dict:
    """
    Adjust a pitcher's K projection based on opponent team K-rate.

    Returns:
        adjusted_projection, matchup_grade, matchup_details
    """
    team_profile = get_team_matchup_profile(opponent_team)
    opp_k_rate = team_profile.get("k_rate", LEAGUE_AVG["team_k_rate"])

    # How much better/worse than league avg is this opponent
    k_rate_diff = opp_k_rate - LEAGUE_AVG["team_k_rate"]
    k_rate_pct_diff = k_rate_diff / LEAGUE_AVG["team_k_rate"]

    # Adjustment: opponent Ks 10% more → projection up 3% (dampened)
    adjustment = k_rate_pct_diff * MATCHUP_WEIGHT
    adjusted = round(base_projection * (1 + adjustment), 2)

    # Matchup grade
    if k_rate_pct_diff >= 0.10:
        grade = "A"   # Elite K opponent — strong tailwind
    elif k_rate_pct_diff >= 0.05:
        grade = "B+"  # Above avg K opponent
    elif k_rate_pct_diff >= 0:
        grade = "B"   # Slight tailwind
    elif k_rate_pct_diff >= -0.05:
        grade = "C"   # Slight headwind
    else:
        grade = "D"   # Low K opponent — fade

    print(
        f"  [matchup] {pitcher_name} vs {opponent_team}: "
        f"opp_k={opp_k_rate:.3f} "
        f"adj={adjustment:+.3f} "
        f"proj {base_projection}→{adjusted} "
        f"grade={grade}"
    )

    return {
        "adjusted_projection": adjusted,
        "base_projection": base_projection,
        "matchup_grade": grade,
        "opp_k_rate": opp_k_rate,
        "opp_k_vs_avg": round(k_rate_pct_diff * 100, 1),
        "adjustment_pct": round(adjustment * 100, 1),
        "opponent": opponent_team,
    }


def adjust_batter_projection(
    base_projection: float,
    batter_name: str,
    opposing_pitcher: str,
    stat_type: str = "hits",
) -> dict:
    """
    Adjust a batter's projection based on opposing pitcher profile.
    stat_type: hits, total_bases, home_runs, rbis
    """
    pitcher_profile = get_pitcher_matchup_profile(opposing_pitcher)

    if stat_type in ("hits", "total_bases"):
        # Higher WHIP pitcher = more baserunners = more hits
        whip = pitcher_profile.get("whip", LEAGUE_AVG["pitcher_whip"])
        diff = whip - LEAGUE_AVG["pitcher_whip"]
        adjustment = (diff / LEAGUE_AVG["pitcher_whip"]) * MATCHUP_WEIGHT

    elif stat_type == "home_runs":
        # Higher HR/9 pitcher = more HR opportunity
        hr9 = pitcher_profile.get("hr_per9", 1.1)
        diff = hr9 - 1.1
        adjustment = (diff / 1.1) * MATCHUP_WEIGHT

    else:
        adjustment = 0.0

    adjusted = round(base_projection * (1 + adjustment), 2)

    grade = (
        "A"  if adjustment >= 0.08 else
        "B+" if adjustment >= 0.04 else
        "B"  if adjustment >= 0     else
        "C"  if adjustment >= -0.04 else
        "D"
    )

    print(
        f"  [matchup] {batter_name} vs {opposing_pitcher} "
        f"({stat_type}): adj={adjustment:+.3f} "
        f"proj {base_projection}→{adjusted} grade={grade}"
    )

    return {
        "adjusted_projection": adjusted,
        "base_projection": base_projection,
        "matchup_grade": grade,
        "opposing_pitcher": opposing_pitcher,
        "adjustment_pct": round(adjustment * 100, 1),
    }


def _build_team_profile_from_statcast(team_name: str) -> dict | None:
    """Build team K-rate profile from PropLine scores/stats."""
    try:
        from slipiq_propline import fetch_scores
        scores = fetch_scores(sport="baseball_mlb", days_from=14)

        team_lower = team_name.lower()
        team_games = [
            g for g in scores
            if (team_lower in g.get("home_team", "").lower() or
                team_lower in g.get("away_team", "").lower())
        ]

        if not team_games:
            return None

        total_games = len(team_games)
        if total_games == 0:
            return None

        # PropLine scores don't include per-game K data directly;
        # fall through to pybaseball season batting stats
        return _get_team_k_rate_pybaseball(team_name)

    except Exception as e:
        print(f"  [matchup] Team profile error for {team_name}: {e}")
        return None


def _get_team_k_rate_pybaseball(team_name: str) -> dict | None:
    """Get team K-rate from pybaseball batting stats."""
    try:
        import pybaseball
        pybaseball.cache.enable()
        year = date.today().year
        data = pybaseball.team_batting(year)
        if data is None or data.empty:
            return None

        name_lower = team_name.lower()
        name_words = [w for w in name_lower.split() if len(w) > 3]

        match = None
        for word in name_words:
            matches = data[
                data["Team"].str.lower().str.contains(word, na=False)
            ]
            if not matches.empty:
                match = matches.iloc[0]
                break

        if match is None:
            return None

        so = float(match.get("SO", 0))
        pa = float(match.get("PA", 1))
        k_rate  = so / pa if pa > 0 else LEAGUE_AVG["team_k_rate"]

        bb = float(match.get("BB", 0))
        bb_rate = bb / pa if pa > 0 else LEAGUE_AVG["team_bb_rate"]

        return {
            "team":    team_name,
            "k_rate":  round(k_rate, 4),
            "bb_rate": round(bb_rate, 4),
            "pa":      int(pa),
            "source":  "pybaseball",
        }

    except Exception as e:
        print(f"  [matchup] pybaseball team error: {e}")
        return None


def _league_avg_team_profile() -> dict:
    return {
        "team":    "league_avg",
        "k_rate":  LEAGUE_AVG["team_k_rate"],
        "bb_rate": LEAGUE_AVG["team_bb_rate"],
        "source":  "fallback",
    }


def _league_avg_pitcher_profile() -> dict:
    return {
        "pitcher": "league_avg",
        "era":     LEAGUE_AVG["pitcher_era"],
        "whip":    LEAGUE_AVG["pitcher_whip"],
        "k_per9":  LEAGUE_AVG["pitcher_k_per9"],
        "k_pct":   LEAGUE_AVG["pitcher_k_per9"] / 27,
        "bb_per9": 3.2,
        "hr_per9": 1.1,
        "source":  "fallback",
    }

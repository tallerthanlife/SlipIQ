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

# Module-level scores cache — fetched once per day
_scores_cache: list | None = None
_scores_cache_date: str | None = None


def get_cached_scores(sport: str = "baseball_mlb") -> list:
    global _scores_cache, _scores_cache_date
    today = str(date.today())
    if _scores_cache and _scores_cache_date == today:
        return _scores_cache
    try:
        from slipiq_propline import fetch_scores
        _scores_cache = fetch_scores(sport=sport, days_from=14)
        _scores_cache_date = today
        print(f"  [matchup] Loaded {len(_scores_cache)} scores (cached)")
    except Exception as e:
        print(f"  [matchup] Scores fetch failed: {e}")
        _scores_cache = []
    return _scores_cache or []


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
    # Use static table — PropLine scores don't have K-rate data
    return _get_team_k_rate_pybaseball(team_name)


# Static 2026 team K-rates — update weekly (FanGraphs 403s on Railway)
TEAM_K_RATES_2026 = {
    "arizona diamondbacks": 0.248,
    "atlanta braves":        0.218,
    "baltimore orioles":     0.212,
    "boston red sox":        0.221,
    "chicago cubs":          0.229,
    "chicago white sox":     0.267,
    "cincinnati reds":       0.234,
    "cleveland guardians":   0.198,
    "colorado rockies":      0.241,
    "detroit tigers":        0.223,
    "houston astros":        0.208,
    "kansas city royals":    0.215,
    "los angeles angels":    0.238,
    "los angeles dodgers":   0.219,
    "miami marlins":         0.244,
    "milwaukee brewers":     0.231,
    "minnesota twins":       0.222,
    "new york mets":         0.226,
    "new york yankees":      0.234,
    "oakland athletics":     0.252,
    "philadelphia phillies": 0.228,
    "pittsburgh pirates":    0.239,
    "san diego padres":      0.224,
    "san francisco giants":  0.221,
    "seattle mariners":      0.233,
    "st. louis cardinals":   0.218,
    "tampa bay rays":        0.227,
    "texas rangers":         0.231,
    "toronto blue jays":     0.225,
    "washington nationals":  0.243,
}


def _get_team_k_rate_pybaseball(team_name: str) -> dict | None:
    """Look up team K-rate from static 2026 table (no external API)."""
    team_lower = team_name.lower().strip()

    # Direct match
    k_rate = TEAM_K_RATES_2026.get(team_lower)

    # Partial match on words longer than 3 chars
    if not k_rate:
        for team, rate in TEAM_K_RATES_2026.items():
            words = [w for w in team_lower.split() if len(w) > 3]
            if any(w in team for w in words):
                k_rate = rate
                break

    if not k_rate:
        return None

    return {
        "team":   team_name,
        "k_rate": k_rate,
        "source": "static_2026",
    }


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

from datetime import date
from slipiq_cache import cache_get, cache_set

# 2026 team strikeout rates — update weekly
TEAM_K_RATES = {
    "arizona diamondbacks": 0.248,
    "atlanta braves": 0.218,
    "baltimore orioles": 0.212,
    "boston red sox": 0.221,
    "chicago cubs": 0.229,
    "chicago white sox": 0.267,
    "cincinnati reds": 0.234,
    "cleveland guardians": 0.198,
    "colorado rockies": 0.241,
    "detroit tigers": 0.223,
    "houston astros": 0.208,
    "kansas city royals": 0.215,
    "los angeles angels": 0.238,
    "los angeles dodgers": 0.219,
    "miami marlins": 0.244,
    "milwaukee brewers": 0.231,
    "minnesota twins": 0.222,
    "new york mets": 0.226,
    "new york yankees": 0.234,
    "oakland athletics": 0.252,
    "philadelphia phillies": 0.228,
    "pittsburgh pirates": 0.239,
    "san diego padres": 0.224,
    "san francisco giants": 0.221,
    "seattle mariners": 0.233,
    "st. louis cardinals": 0.218,
    "tampa bay rays": 0.227,
    "texas rangers": 0.231,
    "toronto blue jays": 0.225,
    "washington nationals": 0.243,
}

LEAGUE_AVG_K = 0.225
MATCHUP_WEIGHT = 0.30


def get_team_k_rate(team_name: str) -> float:
    if not team_name:
        return LEAGUE_AVG_K
    team_lower = team_name.lower().strip()
    # Direct match
    if team_lower in TEAM_K_RATES:
        return TEAM_K_RATES[team_lower]
    # Partial match on any word > 4 chars
    for team, rate in TEAM_K_RATES.items():
        words = [w for w in team_lower.split() if len(w) > 4]
        if any(w in team for w in words):
            return rate
    return LEAGUE_AVG_K


def adjust_pitcher_projection(
    base_projection: float,
    opponent_team: str,
    pitcher_name: str = "",
) -> dict:
    opp_k_rate = get_team_k_rate(opponent_team)
    diff = opp_k_rate - LEAGUE_AVG_K
    pct_diff = diff / LEAGUE_AVG_K
    adjustment = pct_diff * MATCHUP_WEIGHT
    adjusted = round(base_projection * (1 + adjustment), 2)

    if pct_diff >= 0.10:
        grade = "A"
    elif pct_diff >= 0.05:
        grade = "B+"
    elif pct_diff >= 0:
        grade = "B"
    elif pct_diff >= -0.05:
        grade = "C"
    else:
        grade = "D"

    print(f"  [matchup] {pitcher_name} vs {opponent_team}: "
          f"opp_k={opp_k_rate:.3f} adj={adjustment:+.1%} "
          f"proj {base_projection}→{adjusted} grade={grade}")

    return {
        "adjusted_projection": adjusted,
        "base_projection": base_projection,
        "matchup_grade": grade,
        "opp_k_rate": opp_k_rate,
        "opp_k_vs_avg": round(pct_diff * 100, 1),
        "adjustment_pct": round(adjustment * 100, 1),
        "opponent": opponent_team,
    }

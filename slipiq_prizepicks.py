# slipiq_prizepicks.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — PrizePicks Public API Client
#
# Fetches current projection lines from PrizePicks (no API key required).
# Used to cross-reference model projections against PP lines and
# identify edges worth queuing for the PP rolling entry scanner.
#
# PUBLIC API:
#   fetch_prizepicks_lines(sport)      → all current lines for a sport
#   get_pitcher_k_line(player, lines)  → K line for a specific pitcher
#   get_player_line(player, stat, lines) → any line for player+stat combo
# ═══════════════════════════════════════════════════════════════

import requests
from datetime import date

PP_URL = "https://api.prizepicks.com/projections"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://app.prizepicks.com/",
}

LEAGUE_IDS = {
    "baseball_mlb":    2,
    "basketball_nba":  7,
}


def fetch_prizepicks_lines(sport: str = "baseball_mlb") -> list[dict]:
    """
    Fetch all current PrizePicks projection lines for a sport.
    Returns flat list of dicts with player, stat_type, line_score.
    No API key required.
    """
    league_id = LEAGUE_IDS.get(sport, 2)
    params = {
        "league_id":   league_id,
        "per_page":    250,
        "single_stat": "true",
    }
    try:
        r = requests.get(PP_URL, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        projections = data.get("data", [])
        included = {
            item["id"]: item
            for item in data.get("included", [])
            if item.get("type") == "new_player"
        }

        lines = []
        for proj in projections:
            attrs     = proj.get("attributes", {})
            player_id = (
                proj.get("relationships", {})
                    .get("new_player", {})
                    .get("data", {})
                    .get("id")
            )
            player_data = included.get(player_id, {}).get("attributes", {})

            lines.append({
                "player":      player_data.get("display_name", ""),
                "team":        player_data.get("team", ""),
                "position":    player_data.get("position", ""),
                "stat_type":   attrs.get("stat_type", ""),
                "line_score":  attrs.get("line_score"),
                "start_time":  attrs.get("start_time"),
                "description": attrs.get("description", ""),
                "source":      "prizepicks",
            })

        print(f"  [prizepicks] ✓ {len(lines)} lines for {sport}")
        return lines

    except Exception as e:
        print(f"  [prizepicks] Error: {e}")
        return []


def get_pitcher_k_line(
    player_name: str,
    lines: list = None,
) -> dict | None:
    """Find PrizePicks K line for a specific pitcher."""
    if lines is None:
        lines = fetch_prizepicks_lines()
    player_lower = player_name.lower()
    for line in lines:
        if (player_lower in line["player"].lower() or
                line["player"].lower() in player_lower):
            stat = line["stat_type"].lower()
            if "strikeout" in stat or "pitcher strikeout" in stat:
                return line
    return None


def get_player_line(
    player_name: str,
    stat_type:   str,
    lines:       list = None,
) -> dict | None:
    """Find any PrizePicks line for a player + stat combo."""
    if lines is None:
        lines = fetch_prizepicks_lines()
    player_lower = player_name.lower()
    stat_lower   = stat_type.lower()
    for line in lines:
        name_match = (
            player_lower in line["player"].lower() or
            line["player"].lower() in player_lower
        )
        stat_match = stat_lower in line["stat_type"].lower()
        if name_match and stat_match:
            return line
    return None


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — PrizePicks API Self-Test")
    print("=" * 60)

    lines = fetch_prizepicks_lines("baseball_mlb")
    print(f"\n  Total lines: {len(lines)}")

    if lines:
        print("\n  Sample (first 5):")
        for ln in lines[:5]:
            print(f"    {ln['player']:<22} {ln['stat_type']:<30} {ln['line_score']}")

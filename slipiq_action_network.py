"""
SlipIQ ActionNetwork Source
Pulls game lines, ML, F5 ML, and team totals from ActionNetwork
Free, no key needed — supplements SportsData.io with game-level lines
Used for: F5 ML legs in slate parlay, team totals, moneylines
"""

import requests
from datetime import date
from dotenv import load_dotenv

load_dotenv()

ACTION_BASE = "https://api.actionnetwork.com/web/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# ActionNetwork book IDs for major sportsbooks
BOOK_IDS = {
    15: "DraftKings",
    30: "FanDuel",
    76: "Caesars",
    69: "BetMGM",
}


# ─── Fetch Games ──────────────────────────────────────────────

def get_mlb_games():
    """
    Get today's MLB games from ActionNetwork
    Returns list of game dicts with odds
    """
    try:
        r = requests.get(
            f"{ACTION_BASE}/scoreboard/mlb",
            headers=HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        games = data.get("games", [])
        print(f"ActionNetwork: {len(games)} MLB games found")
        return games
    except Exception as e:
        print(f"ActionNetwork error: {e}")
        return []


# ─── Parse Game Lines ─────────────────────────────────────────

def get_team_name(game, side):
    """Get team name from game dict"""
    teams = game.get("teams", [])
    for team in teams:
        if team.get("is_home") and side == "home":
            return team.get("full_name", team.get("display_name", "Home"))
        if not team.get("is_home") and side == "away":
            return team.get("full_name", team.get("display_name", "Away"))
    # Fallback
    if teams:
        if side == "home" and len(teams) > 1:
            return teams[1].get("full_name", "Home")
        elif side == "away":
            return teams[0].get("full_name", "Away")
    return side.capitalize()


def parse_game_lines(game):
    """
    Extract ML, F5 ML, totals, team totals from a game
    Returns structured dict
    """
    game_id = game.get("id")
    teams = game.get("teams", [])

    home_team = get_team_name(game, "home")
    away_team = get_team_name(game, "away")
    start_time = game.get("start_time", "")

    odds_list = game.get("odds", [])

    # Find best available lines (prefer DraftKings then FanDuel)
    best_odds = None
    for book_id in [15, 30, 76, 69]:
        for odds in odds_list:
            if odds.get("book_id") == book_id:
                best_odds = odds
                best_book = BOOK_IDS.get(book_id, "Unknown")
                break
        if best_odds:
            break

    if not best_odds and odds_list:
        best_odds = odds_list[0]
        best_book = "Unknown"

    if not best_odds:
        return None

    result = {
        "game_id": game_id,
        "home_team": home_team,
        "away_team": away_team,
        "start_time": start_time,
        "bookmaker": best_book,
        "full_game": {
            "ml_home": best_odds.get("ml_home"),
            "ml_away": best_odds.get("ml_away"),
            "total": best_odds.get("total"),
            "over": best_odds.get("over"),
            "under": best_odds.get("under"),
            "spread_home": best_odds.get("spread_home"),
            "spread_away": best_odds.get("spread_away"),
        },
        "team_totals": {
            "home_total": best_odds.get("home_total"),
            "home_over": best_odds.get("home_over"),
            "home_under": best_odds.get("home_under"),
            "away_total": best_odds.get("away_total"),
            "away_over": best_odds.get("away_over"),
            "away_under": best_odds.get("away_under"),
        },
        "f5": {},
    }

    # Look for F5 lines in odds list
    for odds in odds_list:
        if odds.get("type") == "f5" or odds.get("period") == "f5":
            result["f5"] = {
                "ml_home": odds.get("ml_home"),
                "ml_away": odds.get("ml_away"),
                "total": odds.get("total"),
                "bookmaker": BOOK_IDS.get(odds.get("book_id"), "Unknown"),
            }
            break

    return result


# ─── Full Pull ────────────────────────────────────────────────

def get_all_game_lines():
    """
    Get all MLB game lines for today
    Returns list of parsed game line dicts
    """
    games = get_mlb_games()
    if not games:
        return []

    lines = []
    for game in games:
        parsed = parse_game_lines(game)
        if parsed:
            lines.append(parsed)

    print(f"ActionNetwork: {len(lines)} games with lines")
    return lines


def get_f5_ml_lines():
    """
    Get F5 ML lines specifically for slate parlay
    Returns dict keyed by home_team
    """
    game_lines = get_all_game_lines()
    f5_lines = {}

    for game in game_lines:
        home = game["home_team"]
        away = game["away_team"]

        # Use full game ML as F5 ML proxy if no F5 specific line
        f5 = game.get("f5", {})
        full = game.get("full_game", {})

        ml_home = f5.get("ml_home") or full.get("ml_home")
        ml_away = f5.get("ml_away") or full.get("ml_away")

        if ml_home or ml_away:
            f5_lines[home] = {
                "ml_home": ml_home,
                "ml_away": ml_away,
                "home_team": home,
                "away_team": away,
                "bookmaker": game["bookmaker"],
            }

    print(f"ActionNetwork: {len(f5_lines)} games with ML lines")
    return f5_lines


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ ActionNetwork Test ===\n")

    lines = get_all_game_lines()

    if lines:
        print(f"\nSample game lines:\n")
        for game in lines[:3]:
            print(f"{game['away_team']} @ {game['home_team']}")
            fg = game["full_game"]
            tt = game["team_totals"]
            print(f"  ML: {game['away_team']} {fg['ml_away']} / {game['home_team']} {fg['ml_home']}")
            print(f"  Total: {fg['total']} (O{fg['over']} / U{fg['under']})")
            if tt.get("home_total"):
                print(f"  Team totals: {game['home_team']} {tt['home_total']} / {game['away_team']} {tt['away_total']}")
            if game.get("f5"):
                print(f"  F5: {game['f5']}")
            print()
    else:
        print("No game lines available")
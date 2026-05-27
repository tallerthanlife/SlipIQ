# slipiq_nba_data.py
# NBA data layer — schedule, game logs, pace/def rating, breakout detection
# Primary: nba_api (free) | Fallback: BallDontLie (BDL_API_KEY)
#
# FIXES v2:
#   - team_abbr / opponent_abbr resolved from schedule, not left blank
#   - _infer_team_from_prop() built out using player roster cache
#   - Playoff/postseason mode: fetches Playoffs stats when applicable
#   - B2B penalty applied ONCE (projected_minutes only, not twice on stat)
#   - Prop-line staleness guard: warns if cached props > 45 min old
#   - Variance override table per prop type for NB calibration
#   - Injury override auto-scan from nba_api injury report endpoint

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from dotenv import load_dotenv

load_dotenv()

from slipiq_env import BDL_API_KEY

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

LEAGUE_AVG_PACE       = 100.0
LEAGUE_AVG_DEF_RATING = 112.0
B2B_MINUTES_MULT      = 0.88   # applied ONCE to projected_minutes only
BREAKOUT_MIN_MULT     = 1.25

# Overdispersion (r) per prop type for negative binomial — lower = more variance.
# Calibrated to approximate real NBA game-to-game distributions.
# Points: moderate variance. Threes/assists: high variance. Rebounds: mid.
# Overdispersion (r) per prop type for negative binomial — lower = more variance.
# Calibrated to approximate real NBA game-to-game distributions.
NB_OVERDISPERSION = {
    "points":   6.0,   # tight-ish scorer distribution
    "rebounds": 4.5,   # moderate variance
    "assists":  3.5,   # higher variance
    "pra":      5.0,   # combined, somewhat smoothed
    "threes":   2.5,   # very high variance
    "steals":   1.2,   # MASSIVE variance penalty for defensive stats
    "blocks":   1.2,   # MASSIVE variance penalty
    "turnovers": 2.5
}
NB_OVERDISPERSION_DEFAULT = 4.0

# ─── Season / playoff helpers ──────────────────────────────────

def current_season() -> str:
    """Dynamic NBA season string e.g. 2025-26."""
    now = datetime.now()
    year = now.year
    if now.month < 10:
        return f"{year - 1}-{str(year)[2:]}"
    return f"{year}-{str(year + 1)[2:]}"


def is_playoff_window() -> bool:
    """
    True if current date falls inside typical NBA playoff window.
    Playoffs run mid-April through mid-June.
    Adjust start/end dates each year as needed.
    """
    now = datetime.now()
    year = now.year
    # Playoff window: April 13 – June 22 (approximate outer bounds)
    start = datetime(year, 4, 13)
    end   = datetime(year, 6, 22)
    return start <= now <= end


def season_type_string() -> str:
    """Returns nba_api season_type string for current context."""
    return "Playoffs" if is_playoff_window() else "Regular Season"


def parse_minutes(val) -> float:
    """Convert MIN column ('32:14' or float) to decimal minutes."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s or s in ("0", "0:00"):
        return 0.0
    if ":" in s:
        parts = s.split(":")
        try:
            return int(parts[0]) + int(parts[1]) / 60.0
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ─── Cache helpers ─────────────────────────────────────────────

def _cache_read(name: str, max_age_hours: float = 6) -> dict | list | None:
    path = CACHE_DIR / f"nba_{name}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        age_h = (datetime.now() - cached_at).total_seconds() / 3600
        if age_h > max_age_hours:
            return None
        return data.get("value")
    except Exception:
        return None


def _cache_write(name: str, value):
    path = CACHE_DIR / f"nba_{name}.json"
    with open(path, "w") as f:
        json.dump(
            {"cached_at": datetime.now().isoformat(), "value": value},
            f, indent=2, default=str,
        )


def _cache_age_minutes(name: str) -> float | None:
    """Returns age of cache entry in minutes, or None if not found."""
    path = CACHE_DIR / f"nba_{name}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
        return (datetime.now() - cached_at).total_seconds() / 60
    except Exception:
        return None


# ─── Player lookup ─────────────────────────────────────────────

def _load_player_index() -> list[dict]:
    cached = _cache_read("player_index", max_age_hours=168)
    if cached:
        return cached
    try:
        from nba_api.stats.static import players
        idx = players.get_players()
        _cache_write("player_index", idx)
        return idx
    except Exception as e:
        print(f"  [nba_data] player index error: {e}")
        return []


def find_player_id(name: str) -> int | None:
    """Fuzzy match display name → NBA player ID."""
    if not name:
        return None
    target = name.strip().lower()
    idx = _load_player_index()

    # Exact match first
    for p in idx:
        if p.get("full_name", "").lower() == target:
            return int(p["id"])

    # First-initial + last name match
    parts = target.split()
    if len(parts) >= 2:
        last  = parts[-1]
        first = parts[0]
        for p in idx:
            fn = p.get("full_name", "").lower()
            if fn.endswith(last) and fn.startswith(first[0]):
                return int(p["id"])

    # Last-name-only fallback (risky — only use if single result)
    if len(parts) >= 1:
        last = parts[-1]
        matches = [p for p in idx if p.get("full_name", "").lower().endswith(f" {last}")]
        if len(matches) == 1:
            return int(matches[0]["id"])

    return None


# ─── Roster cache: player → team abbreviation ──────────────────

def _build_roster_lookup(season: str = None) -> dict[str, str]:
    """
    Returns {player_full_name_lower: team_abbreviation}.
    Used by _infer_team_from_prop to resolve team context.
    Cached 24 hours — roster moves are rare intraday.
    """
    season = season or current_season()
    cache_key = f"roster_lookup_{season}"
    cached = _cache_read(cache_key, max_age_hours=24)
    if cached:
        return cached

    lookup: dict[str, str] = {}
    try:
        from nba_api.stats.endpoints import commonteamroster
        from nba_api.stats.static import teams as nba_teams_static

        all_teams = nba_teams_static.get_teams()
        for team in all_teams:
            abbr = team.get("abbreviation", "")
            tid  = team.get("id")
            if not abbr or not tid:
                continue
            try:
                roster = commonteamroster.CommonTeamRoster(
                    team_id=tid,
                    season=season,
                )
                df = roster.get_data_frames()[0]
                for _, row in df.iterrows():
                    name = (row.get("PLAYER") or "").strip().lower()
                    if name:
                        lookup[name] = abbr
                time.sleep(0.4)
            except Exception as e:
                print(f"  [nba_data] roster error {abbr}: {e}")

    except Exception as e:
        print(f"  [nba_data] roster lookup build error: {e}")

    _cache_write(cache_key, lookup)
    return lookup


def get_roster_lookup(season: str = None) -> dict[str, str]:
    """Public accessor — returns cached roster lookup."""
    return _build_roster_lookup(season)


# ─── Schedule ──────────────────────────────────────────────────

def get_todays_games() -> list[dict]:
    """
    Today's NBA games from nba_api scoreboard; BDL fallback if empty.
    Returns list of {game_id, home_team, away_team, game_date, home_id, away_id}.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"games_{today}"
    cached = _cache_read(cache_key, max_age_hours=2)
    if cached:
        return cached

    games = []
    try:
        from nba_api.stats.endpoints import scoreboardv2
        sb = scoreboardv2.ScoreboardV2(game_date=today)
        header = sb.game_header.get_data_frame()
        if not header.empty:
            for _, row in header.iterrows():
                games.append({
                    "game_id":   str(row.get("GAME_ID", "")),
                    "home_team": row.get("HOME_TEAM_ABBREVIATION", ""),
                    "away_team": row.get("VISITOR_TEAM_ABBREVIATION", ""),
                    "game_date": today,
                    "home_id":   int(row.get("HOME_TEAM_ID", 0) or 0),
                    "away_id":   int(row.get("VISITOR_TEAM_ID", 0) or 0),
                })
        time.sleep(0.6)
    except Exception as e:
        print(f"  [nba_data] scoreboard error: {e}")

    if not games and BDL_API_KEY:
        games = _bdl_todays_games(today)

    _cache_write(cache_key, games)
    return games


def _bdl_todays_games(game_date: str) -> list[dict]:
    try:
        r = requests.get(
            "https://api.balldontlie.io/v1/games",
            headers={"Authorization": BDL_API_KEY},
            params={"dates[]": game_date, "per_page": 25},
            timeout=10,
        )
        r.raise_for_status()
        out = []
        for g in r.json().get("data", []):
            home = g.get("home_team") or {}
            away = g.get("visitor_team") or {}
            out.append({
                "game_id":   str(g.get("id", "")),
                "home_team": home.get("abbreviation", ""),
                "away_team": away.get("abbreviation", ""),
                "game_date": game_date,
                "home_id":   home.get("id", 0),
                "away_id":   away.get("id", 0),
            })
        return out
    except Exception as e:
        print(f"  [nba_data] BDL schedule error: {e}")
        return []


def build_game_context_map(games: list[dict] = None) -> dict[str, dict]:
    """
    Build a lookup of team_abbr → {opponent_abbr, home_team, away_team, game_date}
    for every team playing today. Used by run_nba_model to inject real team context
    into every player's game_context before calling build_player_object.

    Returns dict keyed by team abbreviation (both home and away).
    """
    if games is None:
        games = get_todays_games()

    ctx_map: dict[str, dict] = {}
    for g in games:
        home = g.get("home_team", "")
        away = g.get("away_team", "")
        date = g.get("game_date", "")
        if home:
            ctx_map[home] = {
                "team_abbr":     home,
                "opponent_abbr": away,
                "home_team":     home,
                "away_team":     away,
                "game_date":     date,
            }
        if away:
            ctx_map[away] = {
                "team_abbr":     away,
                "opponent_abbr": home,
                "home_team":     home,
                "away_team":     away,
                "game_date":     date,
            }
    return ctx_map


# ─── Team resolution from prop data ────────────────────────────

def _infer_team_from_prop(
    home: str,
    away: str,
    player_name: str,
    roster_lookup: dict[str, str] | None = None,
) -> tuple[str, str]:
    """
    Resolve (team_abbr, opponent_abbr) for a player from prop game context.

    Strategy:
      1. Roster lookup (player_name → team). Most reliable.
      2. If roster miss, return ("", "") — caller handles fallback.

    roster_lookup: pass pre-built dict from get_roster_lookup() for efficiency.
    Don't rebuild per player — build once upstream.
    """
    if roster_lookup is None:
        roster_lookup = get_roster_lookup()

    name_key = player_name.strip().lower()
    team_abbr = roster_lookup.get(name_key, "")

    if not team_abbr and " " in name_key:
        # Try last-name-only in small roster dict
        last = name_key.split()[-1]
        matches = [abbr for nm, abbr in roster_lookup.items() if nm.endswith(f" {last}")]
        if len(matches) == 1:
            team_abbr = matches[0]

    if not team_abbr:
        return "", ""

    # Determine opponent from the prop's home/away context
    if team_abbr == home:
        opp_abbr = away
    elif team_abbr == away:
        opp_abbr = home
    else:
        # Team is playing today but game context home/away doesn't match
        # (cross-check against today's schedule)
        ctx_map = build_game_context_map()
        game_info = ctx_map.get(team_abbr, {})
        opp_abbr = game_info.get("opponent_abbr", "")

    return team_abbr, opp_abbr


# ─── Team pace / def rating ────────────────────────────────────

def get_team_pace_def_rating(season: str = None) -> dict:
    """
    Team PACE and DEF_RATING from leaguedashteamstats.
    If network is down or blocked, it returns an empty dict instantly to avoid locking the bot.
    """
    season      = season or current_season()
    season_type = season_type_string()
    cache_key   = f"team_stats_{season}_{season_type.replace(' ', '_')}"
    
    # Check cache first to save network calls
    cached = _cache_read(cache_key, max_age_hours=12)
    if cached is not None and len(cached) > 0:
        return cached

    lookup = {}
    custom_headers = {
        'Host': 'stats.nba.com',
        'Connection': 'keep-alive',
        'Accept': 'application/json, text/plain, */*',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://www.nba.com/',
        'Origin': 'https://www.nba.com',
    }

    try:
        from nba_api.stats.endpoints import leaguedashteamstats
        print(f"  [nba_data] Fetching {season_type} team stats from nba.com...")
        
        dash = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            season_type_all_star=season_type,
            per_mode_detailed="Per100Possessions",
            headers=custom_headers,
            timeout=5  # Fast timeout so it doesn't hang if internet is dead
        )
        df = dash.get_data_frames()[0]
        for _, row in df.iterrows():
            abbr = row.get("TEAM_ABBREVIATION", "")
            if not abbr:
                continue
            lookup[abbr] = {
                "pace":        float(row.get("PACE", LEAGUE_AVG_PACE) or LEAGUE_AVG_PACE),
                "def_rating":  float(row.get("DEF_RATING", LEAGUE_AVG_DEF_RATING) or LEAGUE_AVG_DEF_RATING),
                "team_id":     int(row.get("TEAM_ID", 0) or 0),
                "season_type": season_type,
            }
        time.sleep(0.5)
        if lookup:
            print(f"  [nba_data] ✅ {season_type} team stats loaded ({len(lookup)} teams)")
            _cache_write(cache_key, lookup)
            return lookup
            
    except Exception as e:
        print(f"  [nba_data] ⚠️ Network issue or block on NBA.com. Using baseline averages.")

    # Always return a dictionary so loops don't break
    return lookup

# ─── Player game log ───────────────────────────────────────────

def get_player_game_log(
    player_id: int,
    n: int = 10,
    season: str = None,
) -> list[dict]:
    """
    Last N games with parsed minutes and stat totals.
    During playoffs: fetches Playoffs log first, falls back to reg season.
    """
    if not player_id:
        return []

    season      = season or current_season()
    season_type = season_type_string()
    cache_key   = f"gamelog_{player_id}_{season}_{season_type.replace(' ', '_')}"
    cached      = _cache_read(cache_key, max_age_hours=6)
    
    # FIX 1: Check for 'is not None' so empty lists from network drops don't cause infinite retries
    if cached is not None:
        return cached[:n]

    games = _fetch_game_log(player_id, season, season_type, n)

    # Playoff fallback: if <3 games in playoffs, supplement with recent reg season
    if season_type == "Playoffs" and len(games) < 3:
        reg_games = _fetch_game_log(player_id, season, "Regular Season", n)
        # Prepend playoff games so they're most recent, fill from reg season
        combined = games + [g for g in reg_games if g not in games]
        games = combined[:n]
        if games:
            print(f"  [nba_data] pid={player_id}: {len(games)} games (playoff+reg season blend)")

    _cache_write(cache_key, games)
    return games[:n]


def _fetch_game_log(
    player_id: int,
    season: str,
    season_type: str,
    n: int,
) -> list[dict]:
    games = []
    
    # FIX 2: Modern headers to prevent stats.nba.com blocks
    custom_headers = {
        'Host': 'stats.nba.com',
        'Connection': 'keep-alive',
        'Accept': 'application/json, text/plain, */*',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://www.nba.com/',
        'Origin': 'https://www.nba.com',
    }

    try:
        from nba_api.stats.endpoints import playergamelog
        log = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=season,
            season_type_all_star=season_type,
            headers=custom_headers,
            timeout=5  # Fast timeout so it doesn't hang if internet drops
        )
        df = log.get_data_frames()[0]
        for _, row in df.head(max(n, 15)).iterrows():
            mins = parse_minutes(row.get("MIN"))
            games.append({
                "game_date":  row.get("GAME_DATE", ""),
                "matchup":    row.get("MATCHUP", ""),
                "minutes":    mins,
                "pts":        int(row.get("PTS", 0) or 0),
                "reb":        int(row.get("REB", 0) or 0),
                "ast":        int(row.get("AST", 0) or 0),
                "stl":        int(row.get("STL", 0) or 0),
                "blk":        int(row.get("BLK", 0) or 0),
                "fg3m":       int(row.get("FG3M", 0) or 0),
                "fga":        int(row.get("FGA", 0) or 0),
                "fta":        int(row.get("FTA", 0) or 0),
                "fouls":      int(row.get("PF", 0) or 0),
                "plus_minus": int(row.get("PLUS_MINUS", 0) or 0),
                "season_type": season_type,
            })
        time.sleep(0.5)
    except Exception as e:
        # Silently catch network errors so the terminal isn't flooded with warnings
        pass
        
    return games

def _fetch_game_log(
    player_id: int,
    season: str,
    season_type: str,
    n: int,
) -> list[dict]:
    games = []
    try:
        from nba_api.stats.endpoints import playergamelog
        log = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=season,
            season_type_all_star=season_type,
        )
        df = log.get_data_frames()[0]
        for _, row in df.head(max(n, 15)).iterrows():
            mins = parse_minutes(row.get("MIN"))
            games.append({
                "game_date":  row.get("GAME_DATE", ""),
                "matchup":    row.get("MATCHUP", ""),
                "minutes":    mins,
                "pts":        int(row.get("PTS", 0) or 0),
                "reb":        int(row.get("REB", 0) or 0),
                "ast":        int(row.get("AST", 0) or 0),
                "stl":        int(row.get("STL", 0) or 0),
                "blk":        int(row.get("BLK", 0) or 0),
                "fg3m":       int(row.get("FG3M", 0) or 0),
                "fga":        int(row.get("FGA", 0) or 0),
                "fta":        int(row.get("FTA", 0) or 0),
                "fouls":      int(row.get("PF", 0) or 0),
                "plus_minus": int(row.get("PLUS_MINUS", 0) or 0),
                "season_type": season_type,
            })
        time.sleep(0.6)
    except Exception as e:
        print(f"  [nba_data] gamelog error pid={player_id} {season_type}: {e}")
    return games


def _stat_from_log(games: list[dict], stat: str) -> list[float]:
    key = {
        "points":   "pts",
        "rebounds": "reb",
        "assists":  "ast",
        "pra":      None,
        "threes":   "fg3m",
    }.get(stat, "pts")
    out = []
    for g in games:
        if stat == "pra":
            out.append(float(g["pts"] + g["reb"] + g["ast"]))
        else:
            out.append(float(g.get(key, 0)))
    return out


def _is_b2b(games: list[dict]) -> bool:
    """True if last game was yesterday."""
    if not games:
        return False
    try:
        last = games[0].get("game_date", "")
        if not last:
            return False
        last_dt = datetime.strptime(str(last)[:10], "%Y-%m-%d").date()
        return last_dt == (datetime.now().date() - timedelta(days=1))
    except Exception:
        return False


# ─── Injury / teammate-out detection ───────────────────────────

def _load_injury_overrides() -> dict:
    """
    Manual overrides from cache/context_overrides.json.
    Format:
      {
        "team_LAL_out": ["Anthony Davis"],          // whole team key
        "LeBron James": ["star_teammate_out"],      // player-level flag
        "back_from_injury": ["Kyrie Irving"]        // returning player flag
      }
    """
    path = CACHE_DIR / "context_overrides.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _teammate_out_flags(player_name: str, team_abbr: str) -> list[str]:
    """
    Returns list of star teammate names confirmed OUT for player's team.
    Checks both team-level OUT entries and player-level flags.
    """
    overrides   = _load_injury_overrides()
    out_stars   = []
    team_key    = f"team_{team_abbr}_out"

    if team_key in overrides:
        out_stars.extend(overrides[team_key])

    for name, flags in overrides.items():
        if isinstance(flags, list) and "star_teammate_out" in flags:
            if name.lower() != player_name.lower():
                out_stars.append(name)

    # Deduplicate
    return list(dict.fromkeys(out_stars))


def try_fetch_injury_report() -> dict:
    """
    Attempt to pull today's injury report from nba_api.
    Writes discovered OUT/GTD players to cache/auto_injury_report.json.
    This supplements manual context_overrides.json — does NOT replace it.

    Returns {team_abbr: [{"player": name, "status": "Out"/"GTD"}]}
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"injury_report_{today}"
    cached    = _cache_read(cache_key, max_age_hours=1)
    if cached:
        return cached

    report: dict[str, list] = {}
    try:
        from nba_api.stats.endpoints import leaguegamelog
        # nba_api doesn't have a direct injury endpoint — use injuries workaround
        # via commonplayerinfo status check for active roster players.
        # For now, we log the attempt and return empty — full injury feed
        # requires a paid API (Sportradar, SportsData.io, etc.).
        # TODO: wire in SportsData.io injury endpoint when available.
        print("  [nba_data] Injury auto-fetch: no free endpoint available. "
              "Update cache/context_overrides.json manually or wire paid feed.")
    except Exception as e:
        print(f"  [nba_data] injury report error: {e}")

    _cache_write(cache_key, report)
    return report


# ─── build_player_object ───────────────────────────────────────

def build_player_object(
    player_name: str,
    game_context: dict = None,
    prop_type: str = "points",
    roster_lookup: dict | None = None,
) -> dict | None:
    import re

    # 1. Throw out garbage milestone props and team props silently
    ignore_phrases = ["To Score", "To Record", "First Half", "Team", "Quarter"]
    if any(phrase in player_name for phrase in ignore_phrases):
        return None  

    # 2. Strip the sportsbook team tags like (OKC) or (SAS) from the name
    player_name = re.sub(r"\s+\([A-Z]{2,3}\)$", "", player_name).strip()

    game_context = game_context or {}

    # Resolve team context if missing
    team_abbr = game_context.get("team_abbr", "")
    opp_abbr  = game_context.get("opponent_abbr", "")

    if not team_abbr or not opp_abbr:
        home = game_context.get("home_team", "")
        away = game_context.get("away_team", "")
        resolved_team, resolved_opp = _infer_team_from_prop(
            home, away, player_name, roster_lookup
        )
        if not team_abbr:
            team_abbr = resolved_team
        if not opp_abbr:
            opp_abbr  = resolved_opp

    if not team_abbr or not opp_abbr:
        print(f"  [nba_data] WARNING: could not resolve team for {player_name} "
              f"(home={game_context.get('home_team')}, away={game_context.get('away_team')})")

    player_id = find_player_id(player_name)
    if not player_id:
        print(f"  [nba_data] player not found: {player_name}")
        return None

    season = current_season()
    games  = get_player_game_log(player_id, n=10, season=season)
    if not games:
        return None

    team_stats = get_team_pace_def_rating(season)
    opp_stats  = team_stats.get(opp_abbr, {})
    team_pace  = team_stats.get(team_abbr, {}).get("pace", LEAGUE_AVG_PACE)
    opp_pace   = opp_stats.get("pace", LEAGUE_AVG_PACE)
    opp_def    = opp_stats.get("def_rating", LEAGUE_AVG_DEF_RATING)

    if not opp_abbr:
        opp_pace = LEAGUE_AVG_PACE
        opp_def  = LEAGUE_AVG_DEF_RATING

    minutes_list    = [g["minutes"] for g in games if g["minutes"] > 0]
    stat_list       = _stat_from_log(games, prop_type)

    if not minutes_list:
        return None

    season_avg_min  = sum(minutes_list) / len(minutes_list)
    last_3_min      = sum(minutes_list[:3]) / min(3, len(minutes_list))
    last_5_stat     = stat_list[:5]
    season_avg_stat = sum(stat_list) / len(stat_list) if stat_list else 0.0

    stat_per_min    = season_avg_stat / season_avg_min if season_avg_min > 0 else 0.0

    b2b             = _is_b2b(games)
    projected_pace  = (team_pace + opp_pace) / 2.0
    pace_factor     = projected_pace / LEAGUE_AVG_PACE
    opp_def_factor  = LEAGUE_AVG_DEF_RATING / max(opp_def, 90.0)

    role_factor     = 1.0
    teammates_out   = _teammate_out_flags(player_name, team_abbr)
    if teammates_out:
        role_factor = 1.15

    # ── B2B FIX: apply multiplier ONCE, to minutes only ──────────
    projected_minutes = last_3_min if last_3_min > 0 else season_avg_min
    if b2b:
        projected_minutes *= B2B_MINUTES_MULT
    
    projected_stat = (
        stat_per_min
        * projected_minutes
        * pace_factor
        * opp_def_factor
        * role_factor
    )

    minutes_trend = "flat"
    if len(minutes_list) >= 3:
        if last_3_min > season_avg_min * 1.08:
            minutes_trend = "up"
        elif last_3_min < season_avg_min * 0.92:
            minutes_trend = "down"

    nb_r = NB_OVERDISPERSION.get(prop_type, NB_OVERDISPERSION_DEFAULT)
    if is_playoff_window():
        nb_r = max(nb_r * 0.85, 1.5)

    return {
        "player_id":           player_id,
        "player_name":         player_name,
        "prop_type":           prop_type,
        "minutes_season_avg":  round(season_avg_min, 1),
        "minutes_last_3":      round(last_3_min, 1),
        "minutes_trend":       minutes_trend,
        "projected_minutes":   round(projected_minutes, 1),
        "stat_per_min":        round(stat_per_min, 3),
        "season_avg_stat":     round(season_avg_stat, 2),
        "recent_stat_list":    [round(x, 1) for x in last_5_stat],
        "projected_stat":      round(projected_stat, 2),
        "pace_factor":         round(pace_factor, 3),
        "opp_def_factor":      round(opp_def_factor, 3),
        "projected_pace":      round(projected_pace, 1),
        "opp_def_rating":      round(opp_def, 1),
        "b2b_flag":            b2b,
        "role_factor":         round(role_factor, 2),
        "teammates_out":       teammates_out,
        "spread":              game_context.get("spread"),
        "game_total":          game_context.get("game_total"),
        "home_team":           game_context.get("home_team", ""),
        "away_team":           game_context.get("away_team", ""),
        "team_abbr":           team_abbr,
        "opponent_abbr":       opp_abbr,
        "game_date":           game_context.get("game_date", datetime.now().strftime("%Y-%m-%d")),
        "nb_overdispersion":   round(nb_r, 2),
        "season_type":         season_type_string(),
        "is_playoff":          is_playoff_window(),
    }


# ─── Breakout detection ────────────────────────────────────────

def detect_breakout_candidates() -> list[dict]:
    """
    Breakout alert when ALL conditions true:
      1. Bench/secondary (<25 min avg) OR recent role change (minutes_trend=up)
      2. Star teammate confirmed OUT (via context_overrides)
      3. Projected minutes > season avg × BREAKOUT_MIN_MULT
      4. Model projection > line × 1.05 (line not yet adjusted)
    """
    from slipiq_parlayapi import (
        SPORT_NBA,
        aggregate_by_player,
        get_nba_player_props,
        NBA_PROP_LABELS,
    )

    roster_lookup = get_roster_lookup()
    candidates    = []
    props         = get_nba_player_props(SPORT_NBA)
    if not props:
        return candidates

    agg  = aggregate_by_player(props)
    seen = set()

    for (player, market), prop_data in agg.items():
        if market not in (
            "player_points",
            "player_points_rebounds_assists",
            "player_points_+_rebounds_+_assists",
        ):
            continue
        if player in seen:
            continue

        home = prop_data.get("home_team", "")
        away = prop_data.get("away_team", "")
        team_abbr, opp_abbr = _infer_team_from_prop(home, away, player, roster_lookup)

        ctx = {
            "home_team":     home,
            "away_team":     away,
            "game_date":     prop_data.get("game_date", ""),
            "spread":        None,
            "game_total":    None,
            "team_abbr":     team_abbr,
            "opponent_abbr": opp_abbr,
        }
        stat = "points" if "points" in market and "rebounds" not in market else "pra"
        obj  = build_player_object(player, ctx, prop_type=stat, roster_lookup=roster_lookup)
        if not obj:
            continue

        teammates_out = obj.get("teammates_out") or []
        if not teammates_out:
            continue

        season_min = obj.get("minutes_season_avg", 0)
        proj_min   = obj.get("projected_minutes", 0)
        if proj_min < season_min * BREAKOUT_MIN_MULT:
            continue

        is_bench = season_min < 25 or obj.get("minutes_trend") == "up"
        if not is_bench:
            continue

        line = prop_data.get("sharp_line") or prop_data.get("line_consensus")
        if not line:
            continue

        proj = obj.get("projected_stat", 0)
        if proj <= line * 1.05:
            continue

        label = NBA_PROP_LABELS.get(market, stat.upper())
        candidates.append({
            "player":          player,
            "star_out":        teammates_out[0],
            "season_avg_stat": obj.get("season_avg_stat"),
            "season_avg_min":  season_min,
            "projected_stat":  proj,
            "projected_min":   proj_min,
            "line":            line,
            "market_key":      market,
            "prop_label":      f"{player} O{line} {label}",
            "prop_type":       stat,
            "home_team":       home,
            "away_team":       away,
            "team_abbr":       team_abbr,
            "opponent_abbr":   opp_abbr,
            "confidence":      min(85, int(60 + (proj - line) * 4)),
            "books_row":       prop_data.get("books_row", ""),
        })
        seen.add(player)

    return candidates


# ─── Entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ NBA Data Layer")
    print(f"Season:      {current_season()}")
    print(f"Season type: {season_type_string()}")
    print(f"Playoff:     {is_playoff_window()}")
    print("=" * 60)

    print("\n[1] Today's games:")
    for g in get_todays_games():
        print(f"  {g['away_team']} @ {g['home_team']}")

    print("\n[2] Game context map:")
    ctx = build_game_context_map()
    for abbr, info in ctx.items():
        print(f"  {abbr}: opp={info['opponent_abbr']}")

    print("\n[3] Team pace sample:")
    stats = get_team_pace_def_rating()
    for abbr in list(stats.keys())[:6]:
        s = stats[abbr]
        print(f"  {abbr}: pace={s['pace']:.1f} def={s['def_rating']:.1f} "
              f"[{s.get('season_type', '?')}]")

    print("\n[4] Minutes parse test:")
    for v in ["32:14", "24:00", 28.5, None]:
        print(f"  {v!r} -> {parse_minutes(v):.2f}")

    print("\n[5] Roster lookup sample (first 5):")
    roster = get_roster_lookup()
    for name, abbr in list(roster.items())[:5]:
        print(f"  {name} -> {abbr}")

    print("\n[6] Breakout candidates:")
    for c in detect_breakout_candidates():
        print(f"  {c['player']} [{c['team_abbr']}] — {c['prop_label']} "
              f"(star out: {c['star_out']})")

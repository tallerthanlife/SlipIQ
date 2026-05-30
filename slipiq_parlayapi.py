# slipiq_parlayapi.py
# PRIMARY prop + odds source for SlipIQ
# Base URL: https://parlay-api.com/v1
# Auth: X-API-Key header
#
# CREDIT MODEL (lean — free tier safe):
#   Morning props    : /props        → 3 cr  (1x daily)
#   F5/period        : /period_markets → 2 cr (1x daily)
#   Pre-game refresh : /consensus    → 3 cr  (1x daily, top picks only)
#   Post-game CLV    : /historical   → ~5 cr (1x daily)
#   EV calculation   : internal      → 0 cr  (replaces /ev at 10 cr/call)
#   Midday refresh   : /props        → 3 cr  (only if line moves >0.5 pts)
#
#   Daily budget: ~8-13 credits
#   Monthly:      ~250-350 credits
#   Free tier:    1,000 cr/mo — covered indefinitely
#   Starter $5:   20,000 cr/mo — 57x headroom
#
# CONFIRMED MARKET KEYS (live API 2026-05-25):
#   Pitcher Ks   : player_pitcher_strikeouts, player_strike_outs
#   Pitcher outs : player_outs, player_pitcher_outs, player_pitching_outs
#   Batter props : player_hits, player_total_bases, player_home_runs,
#                  player_rbis, player_runs, player_singles, player_doubles,
#                  player_triples, player_stolen_bases, player_walks,
#                  player_hitter_strikeouts, player_hits_runs_rbis

import os
import json
import requests
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mode
from dotenv import load_dotenv

load_dotenv()

PARLAY_API_KEY = os.getenv("PARLAY_API_KEY")
BASE_URL = "https://parlay-api.com/v1"
HEADERS = {"X-API-Key": PARLAY_API_KEY}

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────
# SPORT KEYS
# ─────────────────────────────────────────
SPORT_MLB = "baseball_mlb"
SPORT_NBA = "basketball_nba"
SPORT_NFL = "americanfootball_nfl"

# ─────────────────────────────────────────
# BOOK TIERS
# ─────────────────────────────────────────
SHARP_BOOKS  = {"pinnacle", "novig"}
MARKET_BOOKS = {"draftkings", "fanduel", "betmgm", "caesars", "bet365",
                "betrivers", "fanatics", "bovada", "pointsbet"}
DFS_BOOKS    = {"prizepicks", "underdog"}

# Books blocked by state — not available in Arizona
AZ_BLOCKED_BOOKS = {"sleeper"}

# Pick cards — DK / Fanatics / PrizePicks only (Pinnacle is sharp reference, never on cards)
ACTION_BOOK_KEYS = ("draftkings", "fanatics", "prizepicks")
ACTION_BOOK_LABELS = {
    "draftkings": "DK",
    "fanatics":   "Fanatics",
    "prizepicks": "PrizePicks",
}

# Game-line display — broader AZ-available set (slipiq_game_lines.py)
DISPLAY_BOOK_KEYS = [
    "draftkings",
    "fanduel",
    "betmgm",
    "caesars",
    "bet365",
    "fanatics",
    "prizepicks",
    "underdog",
]

DISPLAY_BOOK_LABELS = {
    "draftkings":  "DK",
    "fanduel":     "FD",
    "betmgm":      "MGM",
    "caesars":     "CZR",
    "bet365":      "B365",
    "fanatics":    "Fan",
    "prizepicks":  "PP",
    "underdog":    "UD",
    "pinnacle":    "PIN",
    "novig":       "NOV",
}

# ─────────────────────────────────────────
# MARKET KEY GROUPS
# ─────────────────────────────────────────
PITCHER_STRIKEOUT_KEYS = {
    "player_pitcher_strikeouts",
    "player_strike_outs",
}

PITCHER_OUTS_KEYS = {
    "player_outs",
    "player_pitcher_outs",
    "player_pitching_outs",
}

BATTER_PROP_KEYS = {
    "player_hits",
    "player_total_bases",
    "player_home_runs",
    "player_rbis",
    "player_runs",
    "player_singles",
    "player_doubles",
    "player_triples",
    "player_stolen_bases",
    "player_walks",
    "player_hitter_strikeouts",
    "player_hits_runs_rbis",
    "player_hits_+_runs_+_rbis_milestones",
    "player_hits_milestones",
    "player_total_bases_milestones",
    "player_home_runs_milestones",
    "player_rbis_milestones",
}

# Normalize raw API market keys → internal canonical names
MARKET_KEY_MAP = {
    "player_strikeouts":            "pitcher_strikeouts",
    "pitcher_strikeouts":           "pitcher_strikeouts",
    "player_pitcher_strikeouts":    "pitcher_strikeouts",
    "player_strike_outs":           "pitcher_strikeouts",
    "batter_total_bases":           "batter_total_bases",
    "player_total_bases":           "batter_total_bases",
    "batter_hits":                  "batter_hits",
    "player_hits":                  "batter_hits",
    "batter_home_runs":             "batter_home_runs",
    "player_home_runs":             "batter_home_runs",
}

# Markets that belong exclusively to the pitcher model
PITCHER_ONLY_MARKETS = {
    "pitcher_strikeouts",
    "pitcher_hits_allowed",
    "pitcher_earned_runs",
    "pitcher_outs",
}

# ─────────────────────────────────────────
# NBA MARKET KEY GROUPS
# ─────────────────────────────────────────
NBA_PRIMARY_PROP_KEYS = {
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_points_rebounds_assists",
    "player_points_+_rebounds_+_assists",
    "player_threes",
    "player_3_pointers_made",
    "player_steals",
    "player_blocks",
    "player_steals_+_blocks",
}

NBA_HIGH_VARIANCE_PROP_KEYS = {
    "player_threes",
    "player_3_pointers_made",
    "player_steals",
    "player_blocks",
    "player_steals_+_blocks",
}

NBA_PROP_KEYS = NBA_PRIMARY_PROP_KEYS

NBA_PROP_LABELS = {
    "player_points":                      "PTS",
    "player_rebounds":                    "REB",
    "player_assists":                     "AST",
    "player_points_rebounds_assists":     "PRA",
    "player_points_+_rebounds_+_assists": "PRA",
    "player_threes":                      "3PM",
    "player_3_pointers_made":             "3PM",
    "player_steals":                      "STL",
    "player_blocks":                      "BLK",
    "player_steals_+_blocks":             "S+B",
}

_REJECT = {
    "@", "vs", "{option",
    "nationals", "guardians", "orioles", "rays", "yankees", "dodgers",
    "cubs", "mets", "reds", "astros", "rangers", "angels", "mariners",
    "athletics", "giants", "padres", "rockies", "diamondbacks",
    "cardinals", "brewers", "pirates", "phillies", "braves", "marlins",
    "tigers", "twins", "royals", "white sox", "red sox", "blue jays",
}


# ═════════════════════════════════════════
# CACHE LAYER
# ═════════════════════════════════════════

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"parlayapi_{key}.json"


def _cache_write(key: str, data: list | dict):
    payload = {
        "timestamp": datetime.utcnow().isoformat(),
        "data": data
    }
    with open(_cache_path(key), "w") as f:
        json.dump(payload, f)
    print(f"  [cache] wrote {key}")


def _cache_read(key: str, max_age_minutes: int = 240) -> list | dict | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    with open(path) as f:
        payload = json.load(f)
    ts = datetime.fromisoformat(payload["timestamp"])
    age = datetime.utcnow() - ts
    if age > timedelta(minutes=max_age_minutes):
        print(f"  [cache] {key} stale ({int(age.total_seconds()/60)} min old)")
        return None
    print(f"  [cache] {key} hit ({int(age.total_seconds()/60)} min old)")
    return payload["data"]


def _cache_age_minutes(key: str) -> float | None:
    """Return age of cache entry in minutes, or None if not exists."""
    path = _cache_path(key)
    if not path.exists():
        return None
    with open(path) as f:
        payload = json.load(f)
    ts = datetime.fromisoformat(payload["timestamp"])
    return (datetime.utcnow() - ts).total_seconds() / 60


# ═════════════════════════════════════════
# EV CALCULATOR (replaces /ev at 10 cr/call)
# ═════════════════════════════════════════

def american_to_prob(american: int) -> float:
    """Convert American odds to implied probability."""
    if american < 0:
        return (-american) / (-american + 100)
    return 100 / (american + 100)


def american_to_decimal(american: int) -> float:
    """Convert American odds to decimal odds."""
    if american < 0:
        return 1 + (100 / -american)
    return 1 + (american / 100)


def calculate_ev(book_price: int, pinnacle_price: int) -> float:
    """
    Calculate +EV vs Pinnacle sharp line.
    Uses Pinnacle as the fair-odds baseline — no /ev credits needed.

    book_price    : American odds from the book you're betting
    pinnacle_price: Pinnacle's American odds for the same side (sharp line)

    Returns: EV as decimal (0.04 = +4% edge, -0.02 = -2% edge)
    """
    fair_prob = american_to_prob(pinnacle_price)
    book_decimal = american_to_decimal(book_price)
    ev = (fair_prob * book_decimal) - 1
    return round(ev, 4)


def calculate_devig(over_price: int, under_price: int) -> tuple[float, float]:
    """
    Remove vig from a two-sided market to get true fair probabilities.
    Returns: (fair_over_prob, fair_under_prob)
    """
    raw_over  = american_to_prob(over_price)
    raw_under = american_to_prob(under_price)
    total = raw_over + raw_under
    return round(raw_over / total, 4), round(raw_under / total, 4)


def kelly_stake(ev: float, fair_prob: float, bankroll: float = 100) -> float:
    """
    Full Kelly criterion stake size.
    In practice use 0.25x Kelly (quarter Kelly) for safety.
    Returns: recommended stake in same units as bankroll
    """
    if ev <= 0:
        return 0.0
    decimal_odds = 1 + (ev / fair_prob) if fair_prob > 0 else 1
    kelly_fraction = (fair_prob * decimal_odds - 1) / (decimal_odds - 1)
    quarter_kelly = kelly_fraction * 0.25
    return round(bankroll * quarter_kelly, 2)


# ═════════════════════════════════════════
# CORE: RAW FETCHES
# ═════════════════════════════════════════

def fetch_props_raw(sport_key: str = SPORT_MLB, force: bool = False) -> list[dict]:
    """
    Fetch all props. 3 credits per call.
    Cached for 4 hours — use force=True to bypass.
    Midday refresh: only call if cache is stale AND a line has moved >0.5 pts.
    """
    cache_key = f"props_{sport_key}"

    if not force:
        cached = _cache_read(cache_key, max_age_minutes=240)
        if cached:
            return cached

    print(f"  [API] /props {sport_key} — 3 credits")
    r = requests.get(
        f"{BASE_URL}/sports/{sport_key}/props",
        headers=HEADERS,
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    _cache_write(cache_key, data)
    return data


def fetch_odds_raw(sport_key: str = SPORT_MLB, markets: list = None) -> list[dict]:
    """
    Fetch ML/spread/totals. Cost = markets x regions, floor 1.
    Game-level context only — not for props.
    """
    if markets is None:
        markets = ["h2h", "totals"]

    cache_key = f"odds_{sport_key}_{'_'.join(markets)}"
    cached = _cache_read(cache_key, max_age_minutes=120)
    if cached:
        return cached

    print(f"  [API] /odds {sport_key} — {len(markets)} credits")
    r = requests.get(
        f"{BASE_URL}/sports/{sport_key}/odds",
        headers=HEADERS,
        params={
            "regions": "us",
            "markets": ",".join(markets),
            "oddsFormat": "american",
        },
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    _cache_write(cache_key, data)
    return data


def fetch_consensus(sport_key: str = SPORT_MLB) -> list[dict]:
    """
    Best book per pick across all sources. 3 credits.
    Call once pre-game for top 2-3 picks only.
    """
    cache_key = f"consensus_{sport_key}"
    cached = _cache_read(cache_key, max_age_minutes=60)
    if cached:
        return cached

    print(f"  [API] /consensus {sport_key} — 3 credits")
    r = requests.get(
        f"{BASE_URL}/sports/{sport_key}/consensus",
        headers=HEADERS,
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    _cache_write(cache_key, data)
    return data


def fetch_period_markets(sport_key: str = SPORT_MLB) -> list[dict]:
    """
    F5 MLB / Q1 NBA / 1H soccer period markets. 2 credits.
    """
    cache_key = f"period_{sport_key}"
    cached = _cache_read(cache_key, max_age_minutes=120)
    if cached:
        return cached

    print(f"  [API] /live/period_markets {sport_key} — 2 credits")
    r = requests.get(
        f"{BASE_URL}/sports/{sport_key}/live/period_markets",
        headers=HEADERS,
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    _cache_write(cache_key, data)
    return data


def fetch_historical(sport_key: str = SPORT_MLB, date: str = None) -> list[dict]:
    """
    Closing line data for CLV tracking / Sharp Review.
    Call once post-game. Cost varies.
    """
    date = date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    cache_key = f"historical_{sport_key}_{date}"
    cached = _cache_read(cache_key, max_age_minutes=720)
    if cached:
        return cached

    print(f"  [API] /historical {sport_key} {date} — variable credits")
    r = requests.get(
        f"{BASE_URL}/sports/{sport_key}/historical",
        headers=HEADERS,
        params={"date": date},
        timeout=10
    )
    r.raise_for_status()
    data = r.json()
    _cache_write(cache_key, data)
    return data


# ═════════════════════════════════════════
# MIDDAY LINE MOVE CHECK
# ═════════════════════════════════════════

def check_line_movement(sport_key: str = SPORT_MLB, threshold: float = 0.5) -> bool:
    """
    Check if any cached prop lines have moved enough to warrant a refresh.
    Compares current /props response against cache WITHOUT spending credits.
    Only triggers a fresh /props call (3 cr) if movement > threshold.

    Returns True if refresh was triggered, False if cache is still valid.
    """
    cache_key = f"props_{sport_key}"
    age = _cache_age_minutes(cache_key)

    if age is None:
        print("  [line-check] No cache — fetching fresh")
        fetch_props_raw(sport_key, force=True)
        return True

    if age < 120:
        print(f"  [line-check] Cache only {int(age)} min old — skip refresh")
        return False

    # Cache is old enough to check — fetch fresh and compare
    print(f"  [line-check] Cache is {int(age)} min old — checking for movement")
    old_data = _cache_read(cache_key, max_age_minutes=9999)  # force read stale
    new_data = fetch_props_raw(sport_key, force=True)

    if not old_data or not new_data:
        return True

    # Build line lookup from old data
    old_lines = {
        (e.get("player"), e.get("market_key"), e.get("bookmaker")): e.get("line")
        for e in old_data
        if e.get("line") is not None
    }

    moved = 0
    for entry in new_data:
        key = (entry.get("player"), entry.get("market_key"), entry.get("bookmaker"))
        new_line = entry.get("line")
        old_line = old_lines.get(key)
        if old_line and new_line and abs(new_line - old_line) >= threshold:
            moved += 1

    print(f"  [line-check] {moved} lines moved ≥{threshold} pts")
    return moved > 0


# ═════════════════════════════════════════
# FILTERS
# ═════════════════════════════════════════

def _is_real_player(name: str) -> bool:
    if not name:
        return False
    low = name.lower()
    if any(pat in low for pat in _REJECT):
        return False
    parts = name.strip().split()
    if not (2 <= len(parts) <= 4):
        return False
    if "{" in name or "}" in name:
        return False
    return True


def _normalize(entry: dict) -> dict:
    book = entry.get("bookmaker", "")
    raw_market       = entry.get("market_key", "").lower()
    normalized_market = MARKET_KEY_MAP.get(raw_market, raw_market)
    return {
        "player":        entry.get("player", ""),
        "home_team":     entry.get("home_team"),
        "away_team":     entry.get("away_team"),
        "game_date":     entry.get("game_date"),
        "commence_time": entry.get("commence_time"),
        "event_id":      entry.get("event_id"),
        "market_key":    normalized_market,
        "market":        normalized_market,
        "book":          book,
        "book_title":    entry.get("bookmaker_title"),
        "line":          entry.get("line"),
        "over_price":    entry.get("over_price"),
        "under_price":   entry.get("under_price"),
        "implied_prob":  entry.get("implied_probability"),
        "is_dfs":        entry.get("is_dfs_flat_payout", False),
        "last_update":   entry.get("last_update"),
        "book_tier":     _classify_book(book),
    }


def _filter_props(raw: list[dict], market_keys: set) -> list[dict]:
    results = []
    for entry in raw:
        if entry.get("market_key", "").lower() not in market_keys:
            continue
        if not _is_real_player(entry.get("player", "")):
            continue
        # Block AZ-unavailable books at source
        if entry.get("bookmaker", "").lower() in AZ_BLOCKED_BOOKS:
            continue
        results.append(_normalize(entry))
    return results


# ═════════════════════════════════════════
# PUBLIC PROP GETTERS
# ═════════════════════════════════════════

def get_pitcher_strikeout_props(sport_key: str = SPORT_MLB) -> list[dict]:
    raw = fetch_props_raw(sport_key)
    return _filter_props(raw, PITCHER_STRIKEOUT_KEYS)


def get_pitcher_outs_props(sport_key: str = SPORT_MLB) -> list[dict]:
    raw = fetch_props_raw(sport_key)
    return _filter_props(raw, PITCHER_OUTS_KEYS)


def get_batter_props(sport_key: str = SPORT_MLB, stat: str = None) -> list[dict]:
    raw = fetch_props_raw(sport_key)
    keys = {stat.lower()} if stat else BATTER_PROP_KEYS
    return _filter_props(raw, keys)


def get_all_props(sport_key: str = SPORT_MLB) -> dict:
    """Single 3-credit call returns all prop types."""
    raw = fetch_props_raw(sport_key)
    pitcher_strikeouts = _filter_props(raw, PITCHER_STRIKEOUT_KEYS)
    pitcher_outs       = _filter_props(raw, PITCHER_OUTS_KEYS)
    pitcher_props = [
        p for p in pitcher_strikeouts + pitcher_outs
        if p.get("market") in PITCHER_ONLY_MARKETS
        and p.get("line", 99) <= 15  # hard cap — no pitcher stat exceeds 15
    ]
    return {
        "pitcher_strikeouts": pitcher_strikeouts,
        "pitcher_outs":       pitcher_outs,
        "pitcher_props":      pitcher_props,
        "batter_props":       _filter_props(raw, BATTER_PROP_KEYS),
    }


def get_nba_player_props(sport_key: str = SPORT_NBA) -> list[dict]:
    raw = fetch_props_raw(sport_key)
    return _filter_props(raw, NBA_PROP_KEYS)


def get_all_nba_props(sport_key: str = SPORT_NBA) -> dict:
    """Single 3-credit call returns all NBA player prop types."""
    raw = fetch_props_raw(sport_key)
    return {"all": _filter_props(raw, NBA_PROP_KEYS)}


# ═════════════════════════════════════════
# AGGREGATOR + EV SCORING
# ═════════════════════════════════════════

def _entry_for_book(entries: list[dict], book_key: str) -> dict | None:
    return next((e for e in entries if e.get("book", "").lower() == book_key), None)


def _display_book_entries(entries: list[dict]) -> list[dict]:
    return [
        _entry_for_book(entries, key)
        for key in ACTION_BOOK_KEYS
        if _entry_for_book(entries, key)
    ]


def _best_action_side(entries: list[dict], side: str) -> dict | None:
    """Best price among DK / Fanatics / PrizePicks only (never Pinnacle)."""
    action = _display_book_entries(entries)
    if side == "over":
        candidates = [e for e in action if e.get("over_price") is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda x: x["over_price"])
    candidates = [e for e in action if e.get("under_price") is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda x: x["under_price"])


def build_books_display(entries: list[dict], direction: str) -> dict:
    """Per-book line/price for Discord cards — action books only."""
    out = {}
    for key in ACTION_BOOK_KEYS:
        ent = _entry_for_book(entries, key)
        if not ent:
            continue
        label = ACTION_BOOK_LABELS.get(key, key)
        if direction == "over":
            price = ent.get("over_price")
            if price is None:
                continue
            out[label] = {
                "book_key": key,
                "line":     ent.get("line"),
                "price":    price,
                "side":     "over",
            }
        else:
            price = ent.get("under_price")
            if price is None:
                continue
            out[label] = {
                "book_key": key,
                "line":     ent.get("line"),
                "price":    price,
                "side":     "under",
            }
    return out


def _action_book_count(entries: list[dict]) -> int:
    return len(_display_book_entries(entries))


def _lines_book_count(entries: list[dict]) -> int:
    """Distinct books posting a line (any tier — for thin-market days)."""
    return len({
        e.get("book", "").lower()
        for e in entries
        if e.get("line") is not None and e.get("book")
    })


def _ev_vs_pinnacle(entries: list[dict], pinnacle: dict) -> tuple:
    """Best EV among action books vs Pinnacle (operator math, 0 credits)."""
    ev_over = ev_under = None
    if not pinnacle:
        return ev_over, ev_under
    pin_o = pinnacle.get("over_price")
    pin_u = pinnacle.get("under_price")
    if pin_o and pin_u:
        for ent in _display_book_entries(entries):
            if ent.get("over_price") is not None:
                ev = calculate_ev(ent["over_price"], pin_o)
                ev_over = ev if ev_over is None else max(ev_over, ev)
            if ent.get("under_price") is not None:
                ev = calculate_ev(ent["under_price"], pin_u)
                ev_under = ev if ev_under is None else max(ev_under, ev)
    return ev_over, ev_under


def format_fallback_books_row(entries: list[dict], direction: str) -> str:
    """When DK/Fanatics/PP are absent, show the best available line (not Pinnacle)."""
    for ent in entries:
        book = (ent.get("book_title") or ent.get("book") or "").strip()
        if book.lower() in SHARP_BOOKS:
            continue
        line = ent.get("line")
        if direction == "over":
            price = ent.get("over_price")
            if price is None:
                continue
            sign = "+" if price > 0 else ""
            return f"{book} {sign}{price} O {line}"
        price = ent.get("under_price")
        if price is None:
            continue
        sign = "+" if price > 0 else ""
        return f"{book} {sign}{price} U {line}"
    return ""


def format_books_row(books_display: dict) -> str:
    """DK -108 | Fanatics +105 | PrizePicks o7.5"""
    if not books_display:
        return "No action books posting"
    parts = []
    order = ("DK", "Fanatics", "PrizePicks")
    for label in order:
        bk = books_display.get(label)
        if not bk:
            continue
        price = bk.get("price")
        line = bk.get("line")
        if price is None:
            continue
        sign = "+" if price > 0 else ""
        if label == "PrizePicks" and line is not None:
            parts.append(f"{label} {bk.get('side', '').upper()} {line}")
        else:
            parts.append(f"{label} {sign}{price}")
    return " | ".join(parts) if parts else "No action books posting"


def aggregate_by_player(props: list[dict]) -> dict:
    """
    Group by (player, market_key).
    Pinnacle = sharp reference only. EV vs Pin from DK/Fanatics/PrizePicks.
    """
    grouped = defaultdict(list)
    for p in props:
        grouped[(p["player"], p["market_key"])].append(p)

    result = {}
    for (player, market), entries in grouped.items():
        display_lines = [
            e["line"] for e in entries
            if e.get("line") is not None
            and e.get("book", "").lower() in ACTION_BOOK_KEYS
        ]
        all_lines = [e["line"] for e in entries if e["line"] is not None]

        pinnacle = next((e for e in entries if e.get("book") == "pinnacle"), None)
        sharp    = [e for e in entries if e.get("book_tier") == "sharp"]
        dfs      = [e for e in entries if e.get("book_tier") == "dfs"]
        market_bk = [e for e in entries if e.get("book_tier") == "market"]

        best_over  = _best_action_side(entries, "over")
        best_under = _best_action_side(entries, "under")

        if pinnacle and pinnacle.get("line") is not None:
            sharp_line = pinnacle["line"]
        else:
            try:
                sharp_line = mode(display_lines) if display_lines else None
            except Exception:
                sharp_line = display_lines[0] if display_lines else None
            if sharp_line is None and all_lines:
                try:
                    sharp_line = mode(all_lines)
                except Exception:
                    sharp_line = all_lines[0]

        try:
            consensus_line = mode(all_lines) if all_lines else None
        except Exception:
            consensus_line = all_lines[0] if all_lines else None

        ev_over, ev_under = _ev_vs_pinnacle(entries, pinnacle)
        fair_over_prob = fair_under_prob = None
        if pinnacle and pinnacle.get("over_price") and pinnacle.get("under_price"):
            fair_over_prob, fair_under_prob = calculate_devig(
                pinnacle["over_price"], pinnacle["under_price"]
            )

        result[(player, market)] = {
            "player":            player,
            "market_key":        market,
            "game_date":         entries[0].get("game_date"),
            "home_team":         entries[0].get("home_team"),
            "away_team":         entries[0].get("away_team"),
            "pinnacle":          pinnacle,
            "sharp_lines":       sharp,
            "dfs_lines":         dfs,
            "market_lines":      market_bk,
            "all_lines":         all_lines,
            "line_consensus":    consensus_line,
            "sharp_line":        sharp_line,
            "best_over":         best_over,
            "best_under":        best_under,
            "book_count":        _action_book_count(entries),
            "action_book_count": _action_book_count(entries),
            "lines_book_count":  _lines_book_count(entries),
            "ev_over":           ev_over,
            "ev_under":          ev_under,
            "fair_over_prob":    fair_over_prob,
            "fair_under_prob":   fair_under_prob,
            "_entries":          entries,
        }

    return result


# ═════════════════════════════════════════
# UTILITIES
# ═════════════════════════════════════════

def _classify_book(book: str) -> str:
    b = book.lower()
    if b in SHARP_BOOKS:  return "sharp"
    if b in DFS_BOOKS:    return "dfs"
    if b in MARKET_BOOKS: return "market"
    return "other"


def print_available_markets(sport_key: str = SPORT_MLB):
    raw = fetch_props_raw(sport_key)
    keys = sorted(set(e.get("market_key", "") for e in raw))
    print(f"\nAvailable market_keys for {sport_key}:")
    for k in keys:
        print(f"  {k}")
    return keys


def print_books_by_market(sport_key: str = SPORT_MLB, market_key: str = "player_pitcher_strikeouts"):
    raw = fetch_props_raw(sport_key)
    books = sorted(set(
        e.get("bookmaker_title", "")
        for e in raw
        if e.get("market_key", "").lower() == market_key.lower()
    ))
    print(f"\nBooks posting '{market_key}':")
    for b in books:
        print(f"  {b}")
    return books


def daily_credit_estimate() -> dict:
    """
    Estimate today's credit spend based on what's been called.
    Reads cache timestamps to determine which endpoints fired.
    """
    costs = {
        f"props_{SPORT_MLB}": 3,
        f"consensus_{SPORT_MLB}": 3,
        f"period_{SPORT_MLB}": 2,
        f"historical_{SPORT_MLB}": 5,
    }
    total = 0
    breakdown = {}
    for key, cost in costs.items():
        age = _cache_age_minutes(key)
        if age is not None and age < 1440:  # fired in last 24 hours
            breakdown[key] = cost
            total += cost

    print(f"\n  Estimated credits used today: {total}")
    for k, v in breakdown.items():
        print(f"    {k}: {v} cr")
    return {"total": total, "breakdown": breakdown}


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — ParlayAPI (Lean Credit Build)")
    print("=" * 60)

    # 1. All props — 3 credits, cached
    print("\n[1] Fetching all MLB props (3 cr, cached 4hr)...")
    all_props = get_all_props(SPORT_MLB)
    k = all_props["pitcher_strikeouts"]
    o = all_props["pitcher_outs"]
    b = all_props["batter_props"]
    print(f"    Pitcher Ks   : {len(k)}")
    print(f"    Pitcher outs : {len(o)}")
    print(f"    Batter props : {len(b)}")

    # 2. EV calculation — 0 credits
    if k:
        print("\n[2] EV vs Pinnacle (0 credits — internal calc):")
        agg = aggregate_by_player(k)
        for (player, market), data in list(agg.items())[:5]:
            ev = data.get("ev_over")
            pin = data.get("pinnacle")
            best = data.get("best_over")
            if ev is not None:
                tag = "✅ +EV" if ev > 0.02 else ("⚠️  thin" if ev > 0 else "❌ -EV")
                print(f"    {player:<22} Line: {data['line_consensus']} | EV: {ev:+.1%} {tag}")
                if best:
                    print(f"      Best over: {best['over_price']} @ {best['book_title']}")
            else:
                print(f"    {player:<22} Line: {data['line_consensus']} | No Pinnacle line (off-hours)")

    # 3. Kelly stake example
    print("\n[3] Kelly stake example:")
    ev_example   = 0.04   # +4% edge
    prob_example = 0.54   # 54% fair probability
    stake = kelly_stake(ev_example, prob_example, bankroll=1000)
    print(f"    Edge: +4% | Fair prob: 54% | Quarter Kelly on $1,000: ${stake}")

    # 4. Credit tracker
    print("\n[4] Today's credit estimate:")
    daily_credit_estimate()

    print("\n✓ Done.")

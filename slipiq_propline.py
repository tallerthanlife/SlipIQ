# slipiq_propline.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Prop-Line API Integration
# Role: Dynamic intraday polling + primary Pinnacle line source
# Budget: 1,000 credits/day
# Base URL: https://api.prop-odds.com  (confirm with your key docs)
#
# CREDIT STRATEGY:
#   Dynamic poll (20-min interval)  : ~1-2 cr/call × 36 polls = ~54 cr/day
#   Pinnacle prop pull               : included in props endpoint
#   Daily ceiling with 20% buffer   : 800 cr effective limit
#   Remaining headroom               : 200 cr for emergency re-pulls
#
# OUTPUT CONTRACT:
#   All functions return data in the SAME shape as slipiq_parlayapi.py
#   Downstream modules (ev_engine, confidence_agent) are source-agnostic.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────
PROPLINE_API_KEY = os.getenv("PROPLINE_API_KEY")
# Prop-Line API base — update if their docs specify a different path
BASE_URL   = "https://api.prop-line.com/v1"
HEADERS    = {
    "x-api-key": PROPLINE_API_KEY or "",
    "Accept":    "application/json",
}

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# Sport keys (confirm with Prop-Line documentation)
SPORT_MLB  = "baseball_mlb"
SPORT_NBA  = "basketball_nba"
SPORT_WNBA = "basketball_wnba"

# Book keys used by Prop-Line
BOOK_PINNACLE   = "pinnacle"
BOOK_DRAFTKINGS = "draftkings"
BOOK_FANATICS   = "fanatics"
BOOK_FANDUEL    = "fanduel"

PREFERRED_BOOKS = (BOOK_DRAFTKINGS, BOOK_FANATICS, BOOK_FANDUEL)
SHARP_BOOKS     = (BOOK_PINNACLE,)

# Dynamic polling cache TTL — 20 minutes
POLL_CACHE_TTL_MINUTES = 20

# Daily credit tracker file
CREDIT_LOG = CACHE_DIR / "propline_credits.json"

# Daily budget ceiling (leave 20% buffer)
DAILY_CREDIT_LIMIT = 800


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — CREDIT TRACKER
# ═══════════════════════════════════════════════════════════════

def _load_credit_log() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    if CREDIT_LOG.exists():
        try:
            data = json.loads(CREDIT_LOG.read_text())
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, KeyError):
            pass
    return {"date": today, "used": 0, "calls": []}


def _save_credit_log(log: dict) -> None:
    CREDIT_LOG.write_text(json.dumps(log, indent=2))


def _spend_credits(amount: int, endpoint: str) -> bool:
    """
    Track credit spend. Returns False if budget would be exceeded.
    Blocks the call if over daily limit.
    """
    log = _load_credit_log()
    if log["used"] + amount > DAILY_CREDIT_LIMIT:
        print(f"  [propline] ⚠️  Daily credit limit ({DAILY_CREDIT_LIMIT}) would be exceeded. Skipping {endpoint}.")
        return False
    log["used"] += amount
    log["calls"].append({
        "endpoint": endpoint,
        "credits":  amount,
        "time":     datetime.now().strftime("%H:%M:%S"),
    })
    _save_credit_log(log)
    return True


def get_daily_credit_usage() -> dict:
    """Return today's credit usage summary."""
    log = _load_credit_log()
    return {
        "date":      log["date"],
        "used":      log["used"],
        "limit":     DAILY_CREDIT_LIMIT,
        "remaining": DAILY_CREDIT_LIMIT - log["used"],
        "calls":     len(log["calls"]),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — CACHE HELPERS (same pattern as slipiq_parlayapi.py)
# ═══════════════════════════════════════════════════════════════

def _cache_path(key: str) -> Path:
    safe = key.replace("/", "_").replace(" ", "_")
    return CACHE_DIR / f"propline_{safe}.json"


def _cache_read(key: str, max_age_minutes: int) -> list | dict | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ts   = datetime.fromisoformat(data.get("_ts", "2000-01-01"))
        age  = (datetime.now() - ts).total_seconds() / 60
        if age <= max_age_minutes:
            return data.get("payload")
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return None


def _cache_write(key: str, payload: list | dict) -> None:
    path = _cache_path(key)
    path.write_text(json.dumps({
        "_ts":     datetime.now().isoformat(),
        "payload": payload,
    }, indent=2))


def _cache_age_minutes(key: str) -> float | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        ts   = datetime.fromisoformat(data.get("_ts", "2000-01-01"))
        return (datetime.now() - ts).total_seconds() / 60
    except (json.JSONDecodeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — RAW API FETCHERS
# ═══════════════════════════════════════════════════════════════

def _get(endpoint: str, params: dict = None, credits: int = 1) -> dict | list | None:
    """
    Core HTTP GET with credit tracking and error handling.
    Returns None on failure — callers must handle gracefully.
    """
    if not PROPLINE_API_KEY:
        print("  [propline] ⚠️  PROPLINE_API_KEY not set in .env")
        return None

    if not _spend_credits(credits, endpoint):
        return None

    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    try:
        resp = requests.get(url, headers=HEADERS, params=params or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        print(f"  [propline] HTTP error {resp.status_code} on {endpoint}: {e}")
        return None
    except requests.exceptions.Timeout:
        print(f"  [propline] Timeout on {endpoint}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"  [propline] Request error on {endpoint}: {e}")
        return None


def fetch_propline_props(
    sport:   str  = SPORT_MLB,
    force:   bool = False,
    ttl_min: int  = POLL_CACHE_TTL_MINUTES,
) -> list[dict]:
    """
    Fetch all props for a sport. Cached 20 minutes (dynamic polling TTL).
    Cost: ~1-2 credits per call.

    This is the primary function called by the intraday scanner every 20 min.
    Returns a flat list of prop entries, each with book coverage including Pinnacle.

    Args:
        sport   : sport key (SPORT_MLB, SPORT_NBA, etc.)
        force   : bypass cache and force fresh fetch
        ttl_min : cache TTL in minutes

    Returns:
        List of prop dicts in normalized shape (see _normalize_prop)
    """
    cache_key = f"props_{sport}"

    if not force:
        cached = _cache_read(cache_key, max_age_minutes=ttl_min)
        if cached is not None:
            return cached

    print(f"  [propline] Fetching props {sport} (~2 cr)...")
    raw = _get(
        endpoint=f"props/{sport}",
        params={"apiKey": PROPLINE_API_KEY},
        credits=2,
    )

    if not raw:
        print(f"  [propline] No data returned for {sport} — returning empty list")
        return []

    props = _normalize_props(raw)
    _cache_write(cache_key, props)
    print(f"  [propline] ✓ {len(props)} props fetched for {sport}")
    return props


def fetch_propline_pinnacle(sport: str = SPORT_MLB) -> list[dict]:
    """
    Extract only Pinnacle lines from the props payload.
    No additional credit cost — reads from props cache.

    This is the sharp reference source for EV calculation.
    If Pinnacle is missing for a player, slipiq_odds_supplement.py fires as backup.
    """
    all_props = fetch_propline_props(sport)
    pinnacle  = [p for p in all_props if p.get("book", "").lower() == BOOK_PINNACLE]
    if not pinnacle:
        print(f"  [propline] No Pinnacle lines in {sport} props (off-hours or not posted yet)")
    return pinnacle


def check_line_movement(
    sport:     str   = SPORT_MLB,
    threshold: float = 0.5,
) -> dict:
    """
    Check if any lines moved significantly since last cache.
    Compares fresh fetch vs cached data WITHOUT spending extra credits
    if the cache is still fresh.

    Returns:
        {
            "moved"         : bool,
            "movers"        : list[dict],  # props that moved > threshold
            "total_checked" : int,
        }
    """
    cache_key = f"props_{sport}"
    old       = _cache_read(cache_key, max_age_minutes=240) or []

    # Force fresh fetch only if old cache is >20 min stale
    age = _cache_age_minutes(cache_key)
    if age is None or age > POLL_CACHE_TTL_MINUTES:
        fresh = fetch_propline_props(sport, force=True)
    else:
        fresh = old

    if not old or not fresh:
        return {"moved": False, "movers": [], "total_checked": 0}

    # Index old props by (player, market, book)
    old_index: dict[tuple, float] = {}
    for p in old:
        key = (p.get("player", ""), p.get("market", ""), p.get("book", ""))
        if p.get("line") is not None:
            old_index[key] = float(p["line"])

    movers = []
    for p in fresh:
        key = (p.get("player", ""), p.get("market", ""), p.get("book", ""))
        old_line = old_index.get(key)
        new_line = p.get("line")
        if old_line is not None and new_line is not None:
            if abs(float(new_line) - old_line) >= threshold:
                movers.append({
                    "player":    p.get("player"),
                    "market":    p.get("market"),
                    "book":      p.get("book"),
                    "old_line":  old_line,
                    "new_line":  float(new_line),
                    "delta":     round(float(new_line) - old_line, 2),
                })

    return {
        "moved":         len(movers) > 0,
        "movers":        movers,
        "total_checked": len(fresh),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — NORMALIZATION
# ═══════════════════════════════════════════════════════════════

def _normalize_props(raw: dict | list) -> list[dict]:
    """
    Normalize Prop-Line API response to flat list of prop dicts.
    Each dict represents ONE book's line for ONE player/market.

    Output shape matches slipiq_parlayapi.py aggregate_by_player() input.
    Adapt this function if Prop-Line changes their response structure.
    """
    entries = []

    # Handle both list and dict-wrapped responses
    if isinstance(raw, dict):
        games = raw.get("games") or raw.get("data") or raw.get("props") or []
    elif isinstance(raw, list):
        games = raw
    else:
        return entries

    for game in games:
        if not isinstance(game, dict):
            continue

        home_team     = game.get("home_team") or game.get("homeTeam") or ""
        away_team     = game.get("away_team") or game.get("awayTeam") or ""
        commence_time = game.get("commence_time") or game.get("startTime") or ""
        game_date     = commence_time[:10] if commence_time else ""
        game_id       = game.get("game_id") or game.get("id") or ""

        # Props can be nested under "props" or "markets" or "odds"
        props_list = (
            game.get("props") or
            game.get("markets") or
            game.get("player_props") or
            []
        )

        for prop in props_list:
            if not isinstance(prop, dict):
                continue

            player = (
                prop.get("player_name") or
                prop.get("player") or
                prop.get("name") or
                ""
            ).strip()
            market = (
                prop.get("market_key") or
                prop.get("market") or
                prop.get("prop_type") or
                ""
            ).lower()
            line = prop.get("line") or prop.get("point") or prop.get("handicap")

            # Books
            book_lines = prop.get("books") or prop.get("sportsbooks") or [prop]
            for bl in book_lines:
                if not isinstance(bl, dict):
                    continue

                book     = (bl.get("book_key") or bl.get("sportsbook") or bl.get("book") or "").lower()
                over_p   = bl.get("over_price") or bl.get("over") or bl.get("over_odds")
                under_p  = bl.get("under_price") or bl.get("under") or bl.get("under_odds")
                book_line = bl.get("line") or bl.get("point") or line

                if not player or not market or book_line is None:
                    continue

                entries.append({
                    "player":        player,
                    "market":        market,
                    "line":          float(book_line),
                    "book":          book,
                    "book_title":    bl.get("book_title") or bl.get("name") or book.title(),
                    "over_price":    int(over_p)  if over_p  is not None else None,
                    "under_price":   int(under_p) if under_p is not None else None,
                    "home_team":     home_team,
                    "away_team":     away_team,
                    "game_date":     game_date,
                    "commence_time": commence_time,
                    "game_id":       str(game_id),
                    "_source":       "propline",
                })

    return entries


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — AGGREGATION (matches parlayapi output shape)
# ═══════════════════════════════════════════════════════════════

def aggregate_propline_by_player(
    props: list[dict],
    preferred_books: tuple = PREFERRED_BOOKS,
    sharp_books:     tuple = SHARP_BOOKS,
) -> dict[tuple, dict]:
    """
    Aggregate per-book lines into a per-player/market summary.
    Output shape is identical to slipiq_parlayapi.aggregate_by_player()
    so downstream modules (ev_engine, confidence_agent) are source-agnostic.

    Returns:
        dict keyed by (player_name_lower, market) →
        {
            "player"          : str,
            "market"          : str,
            "line_consensus"  : float,
            "sharp_line"      : float | None,  # Pinnacle line
            "pinnacle"        : {"over": int, "under": int} | None,
            "ev_over"         : float | None,  # computed vs Pinnacle no-vig
            "ev_under"        : float | None,
            "best_over"       : {"book": str, "price": int, "line": float} | None,
            "best_under"      : {"book": str, "price": int, "line": float} | None,
            "book_count"      : int,
            "_entries"        : list[dict],  # raw entries for this player/market
        }
    """
    from slipiq_ev_engine import no_vig_prob

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for p in props:
        key = (p["player"].lower().strip(), p["market"])
        grouped[key].append(p)

    result = {}
    for (player_key, market), entries in grouped.items():
        player_display = entries[0]["player"]

        # Pinnacle data
        pin_entry = next((e for e in entries if e["book"] == BOOK_PINNACLE), None)
        pinnacle  = None
        sharp_line = None
        ev_over    = None
        ev_under   = None

        if pin_entry:
            sharp_line = pin_entry["line"]
            if pin_entry.get("over_price") and pin_entry.get("under_price"):
                pinnacle = {
                    "over":  pin_entry["over_price"],
                    "under": pin_entry["under_price"],
                }
                nv = no_vig_prob(pinnacle["over"], pinnacle["under"])

                # Compute EV vs each preferred book's best line
                for e in entries:
                    if e["book"] in preferred_books:
                        if e.get("over_price"):
                            from slipiq_ev_engine import leg_ev
                            ev_o = leg_ev(nv["true_over"], e["over_price"])
                            if ev_over is None or ev_o > ev_over:
                                ev_over = round(ev_o, 6)
                        if e.get("under_price"):
                            from slipiq_ev_engine import leg_ev
                            ev_u = leg_ev(nv["true_under"], e["under_price"])
                            if ev_under is None or ev_u > ev_under:
                                ev_under = round(ev_u, 6)

        # Consensus line
        lines = [e["line"] for e in entries if e.get("line") is not None]
        line_consensus = round(sum(lines) / len(lines), 1) if lines else None

        # Best over / under across preferred books
        best_over  = None
        best_under = None
        for e in entries:
            if e["book"] not in preferred_books:
                continue
            if e.get("over_price"):
                if best_over is None or e["over_price"] > best_over["price"]:
                    best_over = {"book": e["book"], "book_title": e["book_title"],
                                 "price": e["over_price"], "line": e["line"]}
            if e.get("under_price"):
                if best_under is None or e["under_price"] > best_under["price"]:
                    best_under = {"book": e["book"], "book_title": e["book_title"],
                                  "price": e["under_price"], "line": e["line"]}

        result[(player_key, market)] = {
            "player":         player_display,
            "market":         market,
            "line_consensus": line_consensus,
            "sharp_line":     sharp_line,
            "pinnacle":       pinnacle,
            "ev_over":        ev_over,
            "ev_under":       ev_under,
            "best_over":      best_over,
            "best_under":     best_under,
            "book_count":     len({e["book"] for e in entries if e["book"] in preferred_books}),
            "_entries":       entries,
        }

    return result


def get_propline_for_players(
    player_names: list[str],
    sport:        str = SPORT_MLB,
    market:       str = "player_pitcher_strikeouts",
) -> dict[tuple, dict]:
    """
    Convenience: fetch and aggregate props for a specific list of players.
    Used by the confidence agent to enrich specific pick cards.
    """
    all_props = fetch_propline_props(sport)
    filtered  = [
        p for p in all_props
        if p["player"].lower() in {n.lower() for n in player_names}
        and p["market"] == market
    ]
    return aggregate_propline_by_player(filtered)


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — KELLY STAKE CONVENIENCE
# (mirrors slipiq_parlayapi.kelly_stake signature)
# ═══════════════════════════════════════════════════════════════

def kelly_stake(ev: float, prob: float, bankroll: float, fraction: float = 0.25) -> float:
    """Thin wrapper — delegates to ev_engine."""
    from slipiq_ev_engine import american_to_decimal, kelly_stake as _ks
    if ev <= 0 or prob <= 0:
        return 0.0
    # Reconstruct decimal odds from ev and prob: ev = prob*odds-1 → odds=(1+ev)/prob
    decimal = (1.0 + ev) / prob
    return _ks(ev, prob, decimal, bankroll, fraction)


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Prop-Line API Module Self-Test")
    print("=" * 60)

    # Credit tracker test
    usage = get_daily_credit_usage()
    print(f"\n[1] Credit usage today: {usage['used']}/{usage['limit']} ({usage['remaining']} remaining)")

    # Normalization test with mock data
    mock_raw = {
        "games": [{
            "home_team":     "Chicago Cubs",
            "away_team":     "St. Louis Cardinals",
            "commence_time": "2026-05-28T18:10:00Z",
            "game_id":       "test_001",
            "props": [{
                "player_name": "Jameson Taillon",
                "market_key":  "player_pitcher_strikeouts",
                "line":        5.5,
                "books": [
                    {"book_key": "pinnacle",   "over_price": -115, "under_price": -105},
                    {"book_key": "draftkings", "over_price": -110, "under_price": -110},
                ]
            }]
        }]
    }
    props    = _normalize_props(mock_raw)
    print(f"\n[2] Normalization test: {len(props)} entries extracted")
    for p in props:
        print(f"    {p['player']} | {p['market']} | line={p['line']} | book={p['book']} | over={p['over_price']}")

    # Aggregation test
    agg = aggregate_propline_by_player(props)
    for (pkey, mkt), data in agg.items():
        print(f"\n[3] Aggregated: {data['player']} | line={data['line_consensus']} | books={data['book_count']}")
        print(f"    ev_over={data['ev_over']}  ev_under={data['ev_under']}")
        print(f"    pinnacle={data['pinnacle']}  sharp_line={data['sharp_line']}")

    if not PROPLINE_API_KEY:
        print("\n⚠️  PROPLINE_API_KEY not set — live API tests skipped.")
        print("    Set PROPLINE_API_KEY in .env and re-run to test live fetch.")
    else:
        print(f"\n[4] Live API test — fetching {SPORT_MLB} props...")
        props_live = fetch_propline_props(SPORT_MLB)
        print(f"    {len(props_live)} props returned")
        usage_after = get_daily_credit_usage()
        print(f"    Credits used: {usage_after['used']}")

    print("\n✓ Propline module ready.")

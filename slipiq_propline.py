# slipiq_propline.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Prop-Line API Integration
# Role: Dynamic intraday polling + primary Pinnacle line source
# Budget: 1,000 credits/day
# Base URL: https://api.prop-line.com/v1
#
# CORRECT API FLOW (two steps per sport):
#   Step 1: GET /sports/{sport}/events?apiKey=KEY
#             → list of events (id, home_team, away_team, commence_time)
#   Step 2: GET /sports/{sport}/events/{id}/odds?markets=...&apiKey=KEY
#             → bookmakers array including Pinnacle
#   There is NO bulk /props endpoint.
#
# CREDIT STRATEGY:
#   fetch_events()    : 1 cr/sport
#   fetch_event_odds(): 1 cr/event  (up to ~15 events/day = ~15 cr)
#   fetch_scores()    : 1 cr/call   (30-min cache)
#   fetch_event_stats(): 1 cr/event (permanent cache once final)
#   Daily ceiling: 800 cr effective limit (200 cr headroom)
#
# OUTPUT CONTRACT:
#   All fetch_all_props() / fetch_propline_props() output matches
#   slipiq_parlayapi.py aggregate_by_player() input shape.
#   Downstream modules (ev_engine, confidence_agent) are source-agnostic.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Config ───────────────────────────────────────────────────
from slipiq_env import PROPLINE_API_KEY

BASE_URL  = "https://api.prop-line.com/v1"
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# Sport keys
SPORT_MLB  = "baseball_mlb"
SPORT_NBA  = "basketball_nba"
SPORT_WNBA = "basketball_wnba"

# Book keys
BOOK_PINNACLE   = "pinnacle"
BOOK_DRAFTKINGS = "draftkings"
BOOK_FANATICS   = "fanatics"
BOOK_FANDUEL    = "fanduel"

PREFERRED_BOOKS = (BOOK_DRAFTKINGS, BOOK_FANATICS, BOOK_FANDUEL)
SHARP_BOOKS     = (BOOK_PINNACLE,)

# Prop markets to request from odds endpoint
DEFAULT_MARKETS = (
    "pitcher_strikeouts,"
    "pitcher_earned_runs,"
    "batter_hits,"
    "batter_total_bases"
)

# Cache TTLs
POLL_CACHE_TTL_MINUTES   = 20       # intraday prop polls
SCORES_CACHE_TTL_MINUTES = 30       # scores change frequently
EVENTS_CACHE_TTL_MINUTES = 24 * 60  # event list: once per day
STATS_CACHE_TTL_MINUTES  = 365 * 24 * 60  # box scores: permanent once final

# Daily credit tracker
CREDIT_LOG         = CACHE_DIR / "propline_credits.json"
DAILY_CREDIT_LIMIT = 800

# Rate limiter
_last_request_time   = 0.0
MIN_REQUEST_INTERVAL = 2.0  # seconds between requests


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
    """Returns False and skips the call if daily budget would be exceeded."""
    log = _load_credit_log()
    if log["used"] + amount > DAILY_CREDIT_LIMIT:
        print(f"  [propline] ⚠️  Daily limit ({DAILY_CREDIT_LIMIT} cr) would be exceeded — skipping {endpoint}.")
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
# SECTION 2 — CACHE HELPERS
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
# SECTION 3 — HTTP HELPER
# ═══════════════════════════════════════════════════════════════

def _get(endpoint: str, params: dict = None, credits: int = 1) -> dict | list | None:
    """
    Core GET with credit tracking, apiKey injection, and error handling.
    apiKey is always injected into query params per Prop-Line auth spec.
    Returns None on any failure — callers must handle gracefully.
    """
    global _last_request_time

    if not PROPLINE_API_KEY:
        print("  [propline] ⚠️  PROPLINE_API_KEY not set in .env")
        return None

    if not _spend_credits(credits, endpoint):
        return None

    # Rate limit — wait minimum interval between requests
    elapsed = time.time() - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)

    _last_request_time = time.time()

    url = f"{BASE_URL}/{endpoint.lstrip('/')}"
    merged_params = {"apiKey": PROPLINE_API_KEY}
    if params:
        merged_params.update(params)

    try:
        resp = requests.get(
            url,
            params  = merged_params,
            headers = {"Accept": "application/json"},
            timeout = 15,
        )
        if resp.status_code == 429:
            print(f"  [propline] Rate limited — waiting 60s...")
            time.sleep(60)
            resp = requests.get(url, params=merged_params, headers={"Accept": "application/json"}, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        print(f"  [propline] HTTP {resp.status_code} on {endpoint}: {e}")
        return None
    except requests.exceptions.Timeout:
        print(f"  [propline] Timeout on {endpoint}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"  [propline] Request error on {endpoint}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — API FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def fetch_events(sport: str = SPORT_MLB, force: bool = False) -> list[dict]:
    """
    GET /sports/{sport}/events?apiKey=KEY
    Returns list of today's events with id, home_team, away_team, commence_time.
    Cached for the full day (one call per sport per day).
    Cost: 1 credit.
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"events_{sport}_{today}"

    if not force:
        cached = _cache_read(cache_key, max_age_minutes=EVENTS_CACHE_TTL_MINUTES)
        if cached is not None:
            return cached

    print(f"  [propline] GET /sports/{sport}/events (1 cr)...")
    raw = _get(f"sports/{sport}/events", credits=1)
    if not raw:
        print(f"  [propline] No events returned for {sport}")
        return []

    events = raw if isinstance(raw, list) else (raw.get("data") or raw.get("events") or [])
    _cache_write(cache_key, events)
    print(f"  [propline] ✓ {len(events)} events for {sport}")
    return events


def fetch_event_odds(
    event_id: str,
    sport:    str = SPORT_MLB,
    markets:  str = DEFAULT_MARKETS,
    force:    bool = False,
) -> dict | None:
    """
    GET /sports/{sport}/events/{event_id}/odds?markets=...&apiKey=KEY
    Returns raw odds dict with bookmakers array (includes Pinnacle).
    Cached per event per day.
    Cost: 1 credit per event.
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"event_odds_{event_id}_{today}"

    if not force:
        cached = _cache_read(cache_key, max_age_minutes=POLL_CACHE_TTL_MINUTES)
        if cached is not None:
            return cached

    print(f"  [propline] GET /sports/{sport}/events/{event_id}/odds (1 cr)...")
    raw = _get(
        f"sports/{sport}/events/{event_id}/odds",
        params  = {"markets": markets},
        credits = 1,
    )
    if not raw:
        return None

    _cache_write(cache_key, raw)
    return raw


def fetch_scores(sport: str = SPORT_MLB, days_from: int = 1, force: bool = False) -> list[dict]:
    """
    GET /sports/{sport}/scores?days_from={days_from}&apiKey=KEY
    Returns game scores and completion status — used for sharp review auto-settlement.
    Cached 30 minutes only (scores change during games).
    Cost: 1 credit.
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"scores_{sport}_{days_from}_{today}"

    if not force:
        cached = _cache_read(cache_key, max_age_minutes=SCORES_CACHE_TTL_MINUTES)
        if cached is not None:
            return cached

    print(f"  [propline] GET /sports/{sport}/scores?days_from={days_from} (1 cr)...")
    raw = _get(
        f"sports/{sport}/scores",
        params  = {"days_from": days_from},
        credits = 1,
    )
    if not raw:
        return []

    scores = raw if isinstance(raw, list) else (raw.get("data") or raw.get("scores") or [])
    _cache_write(cache_key, scores)
    print(f"  [propline] ✓ {len(scores)} score records for {sport}")
    return scores


def fetch_event_stats(event_id: str, sport: str = SPORT_MLB, force: bool = False) -> dict | None:
    """
    GET /sports/{sport}/events/{event_id}/stats?apiKey=KEY
    Returns box score stats: pitcher strikeouts, batter hits, total bases, etc.
    Used by sharp review to get actual results for settlement.
    Cached permanently once complete (final box scores don't change).
    Cost: 1 credit per event.
    """
    cache_key = f"event_stats_{event_id}"

    if not force:
        cached = _cache_read(cache_key, max_age_minutes=STATS_CACHE_TTL_MINUTES)
        if cached is not None:
            return cached

    print(f"  [propline] GET /sports/{sport}/events/{event_id}/stats (1 cr)...")
    raw = _get(f"sports/{sport}/events/{event_id}/stats", credits=1)
    if not raw:
        return None

    _cache_write(cache_key, raw)
    return raw


def fetch_all_props(
    sport:   str  = SPORT_MLB,
    markets: str  = DEFAULT_MARKETS,
    force:   bool = False,
) -> list[dict]:
    """
    Full two-step flow:
      1. fetch_events() — get event list (1 cr, cached daily)
      2. fetch_event_odds() for each event — get bookmaker lines (1 cr each)
    Normalizes all results into flat prop list matching parlayapi output shape.
    Total cost: 1 + N events credits.
    """
    today     = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"all_props_{sport}_{today}"

    if not force:
        cached = _cache_read(cache_key, max_age_minutes=POLL_CACHE_TTL_MINUTES)
        if cached is not None:
            return cached

    events = fetch_events(sport, force=force)
    from datetime import date
    today = date.today().isoformat()
    events = [e for e in events
              if (e.get("commence_time") or "")[:10] == today]
    if not events:
        events = fetch_events(sport)[:10]
    print(f"  [propline] {len(events)} today's events")
    if not events:
        print(f"  [propline] No events — cannot fetch props for {sport}")
        return []

    # Filter to only today's games — avoids burning credits on past/future events
    todays_events = [
        e for e in events
        if (e.get("commence_time") or "")[:10] == today
    ]

    # Fallback: if date filter yields nothing, cap at 15
    if not todays_events:
        todays_events = events[:15]

    print(f"  [propline] {len(todays_events)} today's events "
          f"(filtered from {len(events)} total)")

    all_props: list[dict] = []
    for event in todays_events:
        event_id  = event.get("id") or event.get("event_id") or ""
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        commence  = event.get("commence_time", "")
        game_date = commence[:10] if commence else today

        if not event_id:
            continue

        odds = fetch_event_odds(event_id, sport=sport, markets=markets, force=force)
        if not odds:
            continue

        # Skip immediately if no bookmakers — no credits wasted on processing
        bookmakers = odds.get("bookmakers") or odds.get("_entries") or []
        if not bookmakers:
            continue

        props = _normalize_event_odds(
            event_id      = event_id,
            home_team     = home_team,
            away_team     = away_team,
            commence_time = commence,
            game_date     = game_date,
            odds_raw      = odds,
        )
        # Store event_id on each prop for EV engine
        for prop in props:
            prop["_event_id"] = event_id
        all_props.extend(props)

    _cache_write(cache_key, all_props)
    unique_players = len({p["player"] for p in all_props})
    unique_books   = len({p["book"]   for p in all_props})
    print(f"  [propline] ✓ {len(all_props)} prop lines "
          f"({unique_players} players, {unique_books} books) for {sport}")
    return all_props


# ─── Backward-compatible alias (referenced by other modules) ──
def fetch_propline_props(
    sport:   str  = SPORT_MLB,
    force:   bool = False,
    ttl_min: int  = POLL_CACHE_TTL_MINUTES,
) -> list[dict]:
    """Alias for fetch_all_props() — kept for backward compatibility."""
    return fetch_all_props(sport=sport, force=force)


def fetch_propline_pinnacle(sport: str = SPORT_MLB) -> list[dict]:
    """
    Extract only Pinnacle lines from the full props payload.
    No additional credit cost — reads from the fetch_all_props() cache.
    Sharp reference source for EV calculation.
    """
    all_props = fetch_all_props(sport)
    pinnacle  = [p for p in all_props if p.get("book", "").lower() == BOOK_PINNACLE]
    if not pinnacle:
        print(f"  [propline] No Pinnacle lines in {sport} props (off-hours or not posted yet)")
    return pinnacle


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — NORMALIZATION
# ═══════════════════════════════════════════════════════════════

def _normalize_event_odds(
    *,
    event_id:      str,
    home_team:     str,
    away_team:     str,
    commence_time: str,
    game_date:     str,
    odds_raw:      dict,
) -> list[dict]:
    """
    Flatten one event's odds response into prop dicts.
    Handles The Odds API bookmakers/markets/outcomes structure,
    which Prop-Line mirrors.
    """
    entries: list[dict] = []

    bookmakers = (
        odds_raw.get("bookmakers") or
        odds_raw.get("_entries") or
        odds_raw.get("data", {}).get("bookmakers") or
        []
    )

    books_found = [b.get("key") or b.get("name") for b in bookmakers]
    print(f"  [propline] Books in response: {books_found}")
    SHARP_BOOKS_WANTED = ["betonline", "bookmaker", "circa", "pinnacle", "novig"]
    sharp_found = [b for b in books_found if any(s in (b or "").lower() for s in SHARP_BOOKS_WANTED)]
    if sharp_found:
        print(f"  [propline] ✅ Sharp books found: {sharp_found}")
    else:
        print(f"  [propline] ⚠️  No sharp books in this event — books: {books_found}")

    for bm in bookmakers:
        bm_key      = (bm.get("key") or bm.get("book_key") or "").lower()
        is_pinnacle = any(x in (bm_key or "").lower() for x in ("pinnacle",))
        book        = "pinnacle" if is_pinnacle else bm_key
        book_title  = bm.get("title") or bm.get("name") or book.title()

        for market in bm.get("markets") or []:
            market_key = (market.get("key") or market.get("market_key") or "").lower()
            outcomes   = market.get("outcomes") or []

            # Group outcomes by player — each player has over + under outcomes
            players: dict[str, dict] = {}
            for outcome in outcomes:
                player = (
                    outcome.get("description") or
                    outcome.get("player_name") or
                    outcome.get("name") or
                    ""
                ).strip()
                if not player:
                    continue

                label = (outcome.get("name") or "").lower()
                point = outcome.get("point")
                price = outcome.get("price")

                if player not in players:
                    players[player] = {"line": None, "over_price": None, "under_price": None}
                if point is not None:
                    players[player]["line"] = float(point)
                if label == "over" and price is not None:
                    players[player]["over_price"] = int(price)
                elif label == "under" and price is not None:
                    players[player]["under_price"] = int(price)

            for player, data in players.items():
                if data["line"] is None:
                    continue
                entries.append({
                    "player":           player,
                    "market":           market_key,
                    "line":             data["line"],
                    "book":             book,
                    "book_title":       book_title,
                    "over_price":       data["over_price"],
                    "under_price":      data["under_price"],
                    "home_team":        home_team,
                    "away_team":        away_team,
                    "game_date":        game_date,
                    "commence_time":    commence_time,
                    "game_id":          event_id,
                    "_source":          "propline",
                    "bookmakers":       bookmakers,
                    "_event_id":        event_id,
                    "_raw_event_odds":  odds_raw,
                })

    return entries


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — AGGREGATION (matches parlayapi output shape)
# ═══════════════════════════════════════════════════════════════

def aggregate_propline_by_player(
    props:           list[dict],
    preferred_books: tuple = PREFERRED_BOOKS,
    sharp_books:     tuple = SHARP_BOOKS,
) -> dict[tuple, dict]:
    """
    Aggregate per-book lines into a per-player/market summary.
    Output shape is identical to slipiq_parlayapi.aggregate_by_player()
    so downstream modules (ev_engine, confidence_agent) are source-agnostic.
    """
    from slipiq_ev_engine import no_vig_prob

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for p in props:
        key = (p["player"].lower().strip(), p["market"])
        grouped[key].append(p)

    result = {}
    for (player_key, market), entries in grouped.items():
        player_display = entries[0]["player"]

        pin_entry  = next((e for e in entries if e["book"] == BOOK_PINNACLE), None)
        pinnacle   = None
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
                for e in entries:
                    if e["book"] in preferred_books:
                        from slipiq_ev_engine import leg_ev
                        if e.get("over_price"):
                            ev_o = leg_ev(nv["true_over"], e["over_price"])
                            if ev_over is None or ev_o > ev_over:
                                ev_over = round(ev_o, 6)
                        if e.get("under_price"):
                            ev_u = leg_ev(nv["true_under"], e["under_price"])
                            if ev_under is None or ev_u > ev_under:
                                ev_under = round(ev_u, 6)

        lines          = [e["line"] for e in entries if e.get("line") is not None]
        line_consensus = round(sum(lines) / len(lines), 1) if lines else None

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
    market:       str = "pitcher_strikeouts",
) -> dict[tuple, dict]:
    """
    Convenience: fetch and aggregate props for a specific list of players.
    Used by the confidence agent to enrich specific pick cards.
    """
    all_props = fetch_all_props(sport)
    filtered  = [
        p for p in all_props
        if p["player"].lower() in {n.lower() for n in player_names}
        and p["market"] == market
    ]
    return aggregate_propline_by_player(filtered)


def check_line_movement(
    sport:     str   = SPORT_MLB,
    threshold: float = 0.5,
) -> dict:
    """
    Compare fresh props vs cached props to detect significant line movement.
    Forces a fresh fetch only when the cache is >20 minutes stale.
    """
    cache_key = f"all_props_{sport}_{datetime.now().strftime('%Y-%m-%d')}"
    old       = _cache_read(cache_key, max_age_minutes=240) or []

    age = _cache_age_minutes(cache_key)
    if age is None or age > POLL_CACHE_TTL_MINUTES:
        fresh = fetch_all_props(sport, force=True)
    else:
        fresh = old

    if not old or not fresh:
        return {"moved": False, "movers": [], "total_checked": 0}

    old_index: dict[tuple, float] = {}
    for p in old:
        key = (p.get("player", ""), p.get("market", ""), p.get("book", ""))
        if p.get("line") is not None:
            old_index[key] = float(p["line"])

    movers = []
    for p in fresh:
        key      = (p.get("player", ""), p.get("market", ""), p.get("book", ""))
        old_line = old_index.get(key)
        new_line = p.get("line")
        if old_line is not None and new_line is not None:
            if abs(float(new_line) - old_line) >= threshold:
                movers.append({
                    "player":   p.get("player"),
                    "market":   p.get("market"),
                    "book":     p.get("book"),
                    "old_line": old_line,
                    "new_line": float(new_line),
                    "delta":    round(float(new_line) - old_line, 2),
                })

    return {
        "moved":         len(movers) > 0,
        "movers":        movers,
        "total_checked": len(fresh),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 7 — KELLY STAKE (mirrors slipiq_parlayapi signature)
# ═══════════════════════════════════════════════════════════════

def kelly_stake(ev: float, prob: float, bankroll: float, fraction: float = 0.25) -> float:
    """Thin wrapper — delegates to ev_engine."""
    from slipiq_ev_engine import kelly_stake as _ks
    if ev <= 0 or prob <= 0:
        return 0.0
    decimal = (1.0 + ev) / prob
    return _ks(ev, prob, decimal, bankroll, fraction)


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Prop-Line API Module Self-Test")
    print("=" * 60)

    usage = get_daily_credit_usage()
    print(f"\n[1] Credits today: {usage['used']}/{usage['limit']} ({usage['remaining']} remaining)")

    if not PROPLINE_API_KEY:
        print("\n⚠️  PROPLINE_API_KEY not set — live API tests skipped.")
        print("    Set PROPLINE_API_KEY in .env and re-run.")
    else:
        print(f"\n[2] Fetching events for {SPORT_MLB}...")
        events = fetch_events(SPORT_MLB, force=True)
        print(f"    {len(events)} events returned")

        if events:
            eid = events[0].get("id") or events[0].get("event_id", "")
            print(f"\n[3] Fetching odds for event {eid}...")
            odds = fetch_event_odds(eid, SPORT_MLB, force=True)
            print(f"    {'odds returned' if odds else 'no odds'}")

        print(f"\n[4] fetch_all_props({SPORT_MLB})...")
        props = fetch_all_props(SPORT_MLB, force=True)
        print(f"    {len(props)} prop lines returned")

        print(f"\n[5] fetch_scores({SPORT_MLB})...")
        scores = fetch_scores(SPORT_MLB)
        print(f"    {len(scores)} score records")

        usage_after = get_daily_credit_usage()
        print(f"\n[6] Credits used this test: {usage_after['used'] - usage['used']}")

    print("\n✓ Propline module ready.")

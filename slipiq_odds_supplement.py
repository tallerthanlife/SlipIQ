# slipiq_odds_supplement.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Pinnacle Supplement Chain
#
# FAILSAFE ORDER (fires only when ParlayAPI has no Pinnacle lines):
#   1. Prop-Line API (PROPLINE_API_KEY) — 1,000 cr/day
#      Already cached from morning fetch. Zero extra credits.
#      Carries Pinnacle pitcher strikeout props natively.
#
#   2. The Odds API (ODDS_API_KEY / ODDS_API_2 / ODDS_API_3)
#      Only fires if Prop-Line also has no Pinnacle data.
#      Key rotation: tries each key until one returns data.
#
# ENTRY POINT:
#   supplement_pitcher_strikeout_props(props)
#     → called by slipiq_pitcher_model.run_pitcher_model()
#     → merges Pinnacle lines into existing aggregation
#     → returns enriched props list
#
# CREDIT COST:
#   Prop-Line path: 0 extra (reads existing cache from slipiq_propline)
#   Odds API path:  ~1 credit per event (up to ODDS_MAX_EVENTS)
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import requests

from slipiq_cache import get_event_odds_cached, get_events_cached
from slipiq_env import ODDS_API_KEYS, ODDS_MAX_EVENTS, PROPLINE_API_KEY

CACHE_DIR  = Path("cache")
ODDS_BASE  = "https://api.the-odds-api.com/v4"
MARKET_KEY = "player_pitcher_strikeouts"

# Books to pull from Odds API supplement (Pinnacle is the primary target)
SUPPLEMENT_BOOKS = {"pinnacle", "draftkings", "fanatics"}


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — PROP-LINE PINNACLE FETCH (primary failsafe)
# ═══════════════════════════════════════════════════════════════

def fetch_propline_pinnacle_props(
    players_needed: set[str] | None = None,
) -> list[dict]:
    """
    Pull Pinnacle pitcher strikeout lines from Prop-Line API cache.
    Zero extra credits — reads from the morning fetch cache.

    Returns normalized prop dicts in ParlayAPI shape so
    aggregate_by_player() can process them directly.
    """
    if not PROPLINE_API_KEY:
        return []

    try:
        from slipiq_propline import (
            fetch_propline_props,
            SPORT_MLB,
            BOOK_PINNACLE,
        )

        # Read from cache — no new API call
        props = fetch_propline_props(SPORT_MLB, force=False)
        if not props:
            return []

        # Filter to pitcher strikeout markets + Pinnacle book only
        pitcher_markets = {
            "player_pitcher_strikeouts",
            "player_strike_outs",
            "player_pitcher_outs",
        }

        needed_norm = (
            {_norm_name(p) for p in players_needed}
            if players_needed else None
        )

        pinnacle_props = []
        for p in props:
            book   = (p.get("book") or "").lower()
            market = (p.get("market") or "").lower()
            # Normalize market key to match ParlayAPI format
            if market in ("pitcher_strikeouts", "player_strike_outs",
                          "player_pitcher_outs", "player_pitching_outs"):
                market = "player_pitcher_strikeouts"
            player = p.get("player") or ""

            if book != BOOK_PINNACLE:
                continue
            if market not in pitcher_markets:
                continue
            if needed_norm and _norm_name(player) not in needed_norm:
                continue
            if not p.get("over_price") and not p.get("under_price"):
                continue

            # Normalize to ParlayAPI prop shape
            pinnacle_props.append({
                "player":        player,
                "home_team":     p.get("home_team", ""),
                "away_team":     p.get("away_team", ""),
                "game_date":     p.get("game_date", ""),
                "commence_time": p.get("commence_time", ""),
                "event_id":      p.get("game_id", ""),
                "market_key":    market,
                "book":          "pinnacle",
                "book_title":    "Pinnacle",
                "line":          p.get("line"),
                "over_price":    p.get("over_price"),
                "under_price":   p.get("under_price"),
                "implied_prob":  None,
                "is_dfs":        False,
                "last_update":   None,
                "book_tier":     "sharp",
                "_source":       "propline_supplement",
            })

        if pinnacle_props:
            unique_players = len({p["player"] for p in pinnacle_props})
            print(f"  [supplement] Prop-Line Pinnacle: "
                  f"{len(pinnacle_props)} lines for {unique_players} pitchers")
        else:
            print("  [supplement] Prop-Line has no Pinnacle pitcher lines in cache")

        return pinnacle_props

    except Exception as e:
        print(f"  [supplement] Prop-Line Pinnacle fetch failed: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — ODDS API FETCH (secondary failsafe)
# ═══════════════════════════════════════════════════════════════

def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _next_odds_key(start: int = 0) -> tuple[str | None, int]:
    if not ODDS_API_KEYS:
        return None, start
    idx = start % len(ODDS_API_KEYS)
    return ODDS_API_KEYS[idx], idx


def _fetch_event_odds(event_id: str, key_idx: int = 0) -> dict | None:
    """Try each Odds API key until one returns data."""
    attempts = len(ODDS_API_KEYS) or 1
    for offset in range(attempts):
        key, _ = _next_odds_key(key_idx + offset)
        if not key:
            return None
        data = get_event_odds_cached(
            event_id, MARKET_KEY, key, ODDS_BASE,
            bookmakers="pinnacle"
        )
        if data:
            return data
    return None


def _outcome_prices(
    outcomes: list[dict],
    player: str,
) -> tuple[float | None, int | None, int | None]:
    """Return (line, over_price, under_price) for a player from Odds API outcomes."""
    target = _norm_name(player)
    line = over_price = under_price = None

    for out in outcomes:
        desc = _norm_name(out.get("description") or out.get("name") or "")
        if desc != target:
            continue
        point = out.get("point")
        price = out.get("price")
        label = (out.get("name") or "").lower()
        if point is not None:
            line = float(point)
        if label == "over" and price is not None:
            over_price = int(price)
        elif label == "under" and price is not None:
            under_price = int(price)

    return line, over_price, under_price


def _entry_from_odds(
    *,
    player: str,
    book_key: str,
    book_title: str,
    home_team: str,
    away_team: str,
    game_date: str,
    commence_time: str,
    event_id: str,
    line: float,
    over_price: int | None,
    under_price: int | None,
) -> dict:
    """Normalize Odds API outcome to ParlayAPI prop shape."""
    return {
        "player":        player,
        "home_team":     home_team,
        "away_team":     away_team,
        "game_date":     game_date,
        "commence_time": commence_time,
        "event_id":      event_id,
        "market_key":    "player_pitcher_strikeouts",
        "book":          book_key,
        "book_title":    book_title,
        "line":          line,
        "over_price":    over_price,
        "under_price":   under_price,
        "implied_prob":  None,
        "is_dfs":        False,
        "last_update":   None,
        "book_tier":     "sharp" if book_key == "pinnacle" else "market",
        "_source":       "odds_api_supplement",
    }


def fetch_odds_api_strikeout_props(
    players_needed: set[str] | None = None,
) -> list[dict]:
    """
    Pull pitcher_strikeouts from The Odds API (secondary failsafe).
    Only called when Prop-Line also returned no Pinnacle data.
    Uses key rotation across ODDS_API_KEY / ODDS_API_2 / ODDS_API_3.
    """
    if not ODDS_API_KEYS:
        print("  [supplement] No Odds API keys configured — skipping")
        return []

    key, _ = _next_odds_key(0)
    events = get_events_cached(key, ODDS_BASE)
    if not events:
        print("  [supplement] Odds API: no events returned")
        return []

    needed    = {_norm_name(p) for p in (players_needed or set()) if p}
    today     = date.today().isoformat()
    out: list[dict] = []

    for event in events[:ODDS_MAX_EVENTS]:
        event_id = event.get("id")
        if not event_id:
            continue

        home      = event.get("home_team", "")
        away      = event.get("away_team", "")
        commence  = event.get("commence_time", "")
        game_date = commence[:10] if commence else today

        odds = _fetch_event_odds(event_id)
        if not odds:
            continue

        for bm in odds.get("bookmakers") or []:
            book_key = (bm.get("key") or "").lower()
            if book_key not in SUPPLEMENT_BOOKS:
                continue

            for market in bm.get("markets") or []:
                if (market.get("key") or "").lower() != MARKET_KEY:
                    continue

                outcomes = market.get("outcomes") or []
                players_in_market = {
                    _norm_name(o.get("description") or o.get("name") or "")
                    for o in outcomes
                    if o.get("description") or o.get("name")
                }

                for player_norm in players_in_market:
                    if not player_norm or " " not in player_norm:
                        continue
                    if needed and player_norm not in needed:
                        continue

                    display = next(
                        (
                            o.get("description") or o.get("name") or player_norm.title()
                            for o in outcomes
                            if _norm_name(
                                o.get("description") or o.get("name") or ""
                            ) == player_norm
                        ),
                        player_norm.title(),
                    )
                    line, over_p, under_p = _outcome_prices(outcomes, display)
                    if line is None:
                        continue

                    out.append(_entry_from_odds(
                        player        = display,
                        book_key      = book_key,
                        book_title    = bm.get("title") or book_key.title(),
                        home_team     = home,
                        away_team     = away,
                        game_date     = game_date,
                        commence_time = commence,
                        event_id      = event_id,
                        line          = line,
                        over_price    = over_p,
                        under_price   = under_p,
                    ))

    if out:
        unique_books   = len({e["book"] for e in out})
        unique_players = len({e["player"] for e in out})
        print(f"  [supplement] Odds API: +{len(out)} lines "
              f"({unique_books} books, {unique_players} pitchers)")
    else:
        print("  [supplement] Odds API returned no lines "
              "(quota exhausted or books not posted yet)")

    return out


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def supplement_pitcher_strikeout_props(props: list[dict]) -> list[dict]:
    """
    Merge Pinnacle lines into ParlayAPI props when missing.
    Called by slipiq_pitcher_model.run_pitcher_model() after aggregate_by_player().

    FAILSAFE ORDER:
      1. Prop-Line API (reads cache — 0 extra credits)
      2. The Odds API (key rotation — ~1 cr/event)
      3. If both fail, returns props unchanged

    Only fetches for players actually missing Pinnacle.
    Never duplicates (player, book) pairs.
    """
    if not props:
        return props

    # Identify which players are missing Pinnacle
    by_player: dict[str, set[str]] = {}
    for p in props:
        player = p.get("player")
        if not player:
            continue
        by_player.setdefault(player, set()).add((p.get("book") or "").lower())

    missing_pinnacle = [
        player for player, books in by_player.items()
        if "pinnacle" not in books
    ]

    if not missing_pinnacle:
        print(f"  [supplement] All {len(by_player)} pitchers have Pinnacle lines — no supplement needed")
        return props

    print(f"  [supplement] {len(missing_pinnacle)}/{len(by_player)} pitchers missing Pinnacle "
          f"— running failsafe chain")

    targets = set(missing_pinnacle)
    extra: list[dict] = []

    # ── Failsafe 1: Prop-Line API ──────────────────────────────
    extra = fetch_propline_pinnacle_props(players_needed=targets)

    # ── Failsafe 2: Odds API (only if Prop-Line had nothing) ───
    if not extra:
        print("  [supplement] Prop-Line had no Pinnacle data → trying Odds API")
        extra = fetch_odds_api_strikeout_props(players_needed=targets)

    if not extra:
        print("  [supplement] Both failsafe sources returned no Pinnacle lines")
        return props

    # Merge — avoid duplicating existing (player, book) pairs
    existing = {
        (p.get("player"), (p.get("book") or "").lower())
        for p in props
    }
    merged = list(props)
    added  = 0
    for entry in extra:
        key = (entry.get("player"), (entry.get("book") or "").lower())
        if key not in existing:
            merged.append(entry)
            existing.add(key)
            added += 1

    if added:
        print(f"  [supplement] Injected {added} Pinnacle lines into prop data")

    return merged


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Odds Supplement Chain Test")
    print("=" * 60)

    # Simulate props with no Pinnacle
    mock_props = [
        {"player": "Gerrit Cole",   "book": "draftkings",
         "market_key": "player_pitcher_strikeouts", "line": 7.5},
        {"player": "Zack Wheeler",  "book": "fanatics",
         "market_key": "player_pitcher_strikeouts", "line": 6.5},
    ]

    print(f"\nInput: {len(mock_props)} props, 0 Pinnacle lines")
    print("Running supplement chain...")

    result = supplement_pitcher_strikeout_props(mock_props)

    pinnacle_count = sum(1 for p in result if p.get("book") == "pinnacle")
    print(f"\nOutput: {len(result)} props, {pinnacle_count} Pinnacle lines added")

    if pinnacle_count:
        for p in result:
            if p.get("book") == "pinnacle":
                print(f"  ✓ {p['player']} | line={p['line']} | "
                      f"over={p['over_price']} | under={p['under_price']} "
                      f"| source={p.get('_source','?')}")
    else:
        print("  No Pinnacle lines found (expected if off-hours or keys not set)")

    print("\n✓ Supplement chain ready.")

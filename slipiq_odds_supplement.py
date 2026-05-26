# slipiq_odds_supplement.py
# Failsafe: Pinnacle (+ market books) from The Odds API when ParlayAPI is thin.
# 0 extra ParlayAPI credits — uses cached Odds API event/odds pulls.

from __future__ import annotations

import re
from datetime import date

import requests

from slipiq_cache import get_event_odds_cached, get_events_cached
from slipiq_env import ODDS_API_KEYS, ODDS_MAX_EVENTS

ODDS_BASE = "https://api.the-odds-api.com/v4"
MARKET_KEY = "pitcher_strikeouts"
SUPPLEMENT_BOOKS = ("pinnacle", "draftkings", "fanatics")


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
        key, idx = _next_odds_key(key_idx + offset)
        if not key:
            return None
        data = get_event_odds_cached(event_id, MARKET_KEY, key, ODDS_BASE)
        if data:
            return data
    return None


def _outcome_prices(outcomes: list[dict], player: str) -> tuple[float | None, int | None, int | None]:
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
    """Normalize to ParlayAPI prop shape for aggregate_by_player()."""
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
    }


def fetch_odds_api_strikeout_props(
    players_needed: set[str] | None = None,
) -> list[dict]:
    """
    Pull pitcher_strikeouts from The Odds API for up to ODDS_MAX_EVENTS games.
    Returns normalized prop dicts (Pinnacle + DK + Fanatics when posted).
    """
    if not ODDS_API_KEYS:
        return []

    key, _ = _next_odds_key(0)
    events = get_events_cached(key, ODDS_BASE)
    if not events:
        print("  [odds_supplement] no events from Odds API")
        return []

    needed = {_norm_name(p) for p in (players_needed or set()) if p}
    today = date.today().isoformat()
    out: list[dict] = []

    for event in events[:ODDS_MAX_EVENTS]:
        event_id = event.get("id")
        if not event_id:
            continue

        home = event.get("home_team", "")
        away = event.get("away_team", "")
        commence = event.get("commence_time", "")
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

                    # Recover display name from first matching outcome
                    display = next(
                        (o.get("description") or o.get("name") or player_norm.title()
                         for o in outcomes
                         if _norm_name(o.get("description") or o.get("name") or "") == player_norm),
                        player_norm.title(),
                    )
                    line, over_p, under_p = _outcome_prices(outcomes, display)
                    if line is None:
                        continue

                    out.append(_entry_from_odds(
                        player=display,
                        book_key=book_key,
                        book_title=bm.get("title") or book_key.title(),
                        home_team=home,
                        away_team=away,
                        game_date=game_date,
                        commence_time=commence,
                        event_id=event_id,
                        line=line,
                        over_price=over_p,
                        under_price=under_p,
                    ))

    if out:
        print(f"  [odds_supplement] +{len(out)} lines from Odds API "
              f"({len({e['book'] for e in out})} books)")
    else:
        print("  [odds_supplement] Odds API returned no supplemental lines "
              "(quota exhausted or books not posted yet)")

    return out


def supplement_pitcher_strikeout_props(props: list[dict]) -> list[dict]:
    """
    Merge Odds API lines into ParlayAPI props when Pinnacle/action books are missing.
    Skips fetch when every player already has Pinnacle.
    """
    if not props or not ODDS_API_KEYS:
        return props

    by_player: dict[str, set[str]] = {}
    for p in props:
        player = p.get("player")
        if not player:
            continue
        by_player.setdefault(player, set()).add((p.get("book") or "").lower())

    missing_pin = [
        player for player, books in by_player.items()
        if "pinnacle" not in books
    ]
    thin_action = [
        player for player, books in by_player.items()
        if not books.intersection({"draftkings", "fanatics", "prizepicks"})
    ]

    if not missing_pin and not thin_action:
        return props

    targets = set(missing_pin) | set(thin_action)
    extra = fetch_odds_api_strikeout_props(targets)
    if not extra:
        return props

    # Avoid duplicate (player, book) pairs
    existing = {(p.get("player"), (p.get("book") or "").lower()) for p in props}
    merged = list(props)
    for entry in extra:
        key = (entry.get("player"), (entry.get("book") or "").lower())
        if key in existing:
            continue
        merged.append(entry)
        existing.add(key)

    return merged

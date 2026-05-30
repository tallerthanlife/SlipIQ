# DISABLED
# This module is disabled. Import it safely; all functions are no-ops.
import sys as _sys
if False:
    pass

# slipiq_game_lines.py
# F5 ML lines — DraftKings / Fanatics / PrizePicks only (Pinnacle = internal ref later)
# Source: The Odds API h2h_1st_5_innings (cached per event/day, key rotation)

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import statsapi

from slipiq_cache import get_event_odds_cached, get_events_cached
from slipiq_env import ODDS_API_KEYS, ODDS_MAX_EVENTS
from slipiq_grading import calc_grade
from slipiq_parlayapi import DISPLAY_BOOK_KEYS, DISPLAY_BOOK_LABELS

ODDS_BASE = "https://api.the-odds-api.com/v4"
F5_MARKET = "h2h_1st_5_innings"
F5_BOOKS = ",".join(DISPLAY_BOOK_KEYS)

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

ELITE_THRESHOLD = 58
WEAK_THRESHOLD = 45
F5_MIN_CONF = 62


def _norm_team(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _team_match(a: str, b: str) -> bool:
    na, nb = _norm_team(a), _norm_team(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return na.split()[-1] == nb.split()[-1]


def _game_key(home: str, away: str) -> tuple[str, str]:
    return (_norm_team(home), _norm_team(away))


def _find_f5_line(lines: dict, home: str, away: str) -> dict | None:
    gkey = _game_key(home, away)
    if gkey in lines:
        return lines[gkey]
    for data in lines.values():
        if _team_match(data.get("home_team", ""), home) and _team_match(
            data.get("away_team", ""), away
        ):
            return data
    return None


def get_probable_starters(force: bool = False) -> dict[str, dict]:
    """
    Map pitcher name (lower) -> {team, side, home_team, away_team}.
    Cached for the day — statsapi, zero credits.
    """
    today = date.today().isoformat()
    cache_path = CACHE_DIR / f"probable_starters_{today.replace('-', '')}.json"
    if not force and cache_path.exists():
        with open(cache_path) as f:
            return json.load(f)

    d_str = datetime.now().strftime("%m/%d/%Y")
    mapping: dict[str, dict] = {}
    try:
        for g in statsapi.schedule(date=d_str) or []:
            home = g.get("home_name") or ""
            away = g.get("away_name") or ""
            for side, team, prob in (
                ("home", home, g.get("home_probable_pitcher")),
                ("away", away, g.get("away_probable_pitcher")),
            ):
                if not prob or not team:
                    continue
                mapping[prob.strip().lower()] = {
                    "team":      team,
                    "side":      side,
                    "home_team": home,
                    "away_team": away,
                }
    except Exception as e:
        print(f"  [game_lines] probable starters error: {e}")

    with open(cache_path, "w") as f:
        json.dump(mapping, f, indent=2)
    return mapping


def _parse_f5_bookmakers(odds_payload: dict) -> dict[str, dict[str, int]]:
    """team_name -> {book_key: american_price}"""
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for bm in odds_payload.get("bookmakers") or []:
        book = (bm.get("key") or "").lower()
        if book not in DISPLAY_BOOK_KEYS:
            continue
        market = next(
            (m for m in bm.get("markets") or [] if m.get("key") == F5_MARKET),
            None,
        )
        if not market:
            continue
        for outcome in market.get("outcomes") or []:
            team = outcome.get("name")
            price = outcome.get("price")
            if team and price is not None:
                out[team][book] = int(price)
    return dict(out)


def format_f5_books_row(team_prices: dict[str, int]) -> str:
    parts = []
    for key in DISPLAY_BOOK_KEYS:
        price = team_prices.get(key)
        if price is None:
            continue
        label = DISPLAY_BOOK_LABELS.get(key, key)
        sign = "+" if price > 0 else ""
        parts.append(f"{label} {sign}{price}")
    return " | ".join(parts) if parts else "No F5 lines on target books yet"


def fetch_f5_ml_lines(
    games_filter: set[tuple[str, str]] | None = None,
    force: bool = False,
) -> dict[tuple[str, str], dict]:
    """
    Pull F5 ML for today's slate. Cached per event + daily slate file.
    games_filter: optional set of (home_team, away_team) to limit API calls.
    """
    today = date.today().isoformat()
    slate_cache = CACHE_DIR / f"f5_ml_slate_{today.replace('-', '')}.json"

    if not force and slate_cache.exists():
        with open(slate_cache) as f:
            raw = json.load(f)
        result = {}
        for key, v in raw.items():
            if "|" in key:
                home, away = key.split("|", 1)
                result[_game_key(home, away)] = v
            else:
                result[key] = v
        return result

    if not ODDS_API_KEYS:
        print("  [game_lines] no Odds API keys — skip F5 ML")
        return {}

    events = get_events_cached(ODDS_API_KEYS[0], ODDS_BASE)
    if not events:
        print("  [game_lines] no Odds API events for F5 ML")
        return {}

    result: dict[tuple[str, str], dict] = {}
    fetched = 0

    for event in events[:ODDS_MAX_EVENTS]:
        home = event.get("home_team") or ""
        away = event.get("away_team") or ""
        gkey = _game_key(home, away)

        if games_filter:
            matched = any(
                _team_match(home, fh) and _team_match(away, fa)
                for fh, fa in games_filter
            )
            if not matched:
                continue

        event_id = event.get("id")
        if not event_id:
            continue

        odds = get_event_odds_cached(
            event_id,
            F5_MARKET,
            odds_api_key=None,
            base_url=ODDS_BASE,
            bookmakers=F5_BOOKS,
        )
        fetched += 1
        if not odds:
            continue

        team_prices = _parse_f5_bookmakers(odds)
        if not team_prices:
            continue

        home_prices = {}
        away_prices = {}
        for team, prices in team_prices.items():
            if _team_match(team, home):
                home_prices = prices
            elif _team_match(team, away):
                away_prices = prices

        result[gkey] = {
            "home_team":     home,
            "away_team":     away,
            "event_id":      event_id,
            "home_prices":   home_prices,
            "away_prices":   away_prices,
            "home_books_row": format_f5_books_row(home_prices),
            "away_books_row": format_f5_books_row(away_prices),
        }

    serializable = {
        f"{v['home_team']}|{v['away_team']}": v for v in result.values()
    }
    with open(slate_cache, "w") as f:
        json.dump(serializable, f, indent=2)

    print(f"  [game_lines] F5 ML — {len(result)} games "
          f"({fetched} event pulls, cached)")

    # Return as dict keyed by (home_team, away_team) for orchestrator compatibility
    if isinstance(result, list):
        return {
            (item.get("home_team", ""), item.get("away_team", "")): item
            for item in result
            if isinstance(item, dict)
        }
    return result if isinstance(result, dict) else {}


def pitcher_score(card: dict) -> float:
    """0–100 composite from PROJECT_BRAIN game classifier."""
    internal = card.get("_internal") or {}
    k_floor = float(card.get("projection") or internal.get("recent_proj") or 5.0)
    swstr = float(internal.get("season_whiff") or 0.11) * 100
    conf = float(card.get("confidence") or 50)
    recent_bonus = max(0.0, min(15.0, (k_floor - 5.0) * 3.0))
    return (
        min(40.0, k_floor * 4.5)
        + min(35.0, swstr * 2.2)
        + recent_bonus
        + conf / 10.0
    )


def _pick_side(
    home_score: float,
    away_score: float,
    home_card: dict | None,
    away_card: dict | None,
) -> tuple[str | None, float, str]:
    """
    Returns (side: 'home'|'away', confidence, scenario) or (None, 0, reason).
    Primary: correlate F5 ML with pitcher K OVER picks (same starter).
    """
    candidates: list[tuple[str, float, str]] = []

    for side, card in (("home", home_card), ("away", away_card)):
        if not card:
            continue
        conf = float(card.get("confidence") or 0)
        if card.get("direction") == "over" and conf >= F5_MIN_CONF:
            grade = card.get("grade", "C")
            if grade in ("A+", "A", "B+", "B"):
                candidates.append((side, conf - 4, "PITCHER_K_OVER"))

    if candidates:
        side, conf, scen = max(candidates, key=lambda x: x[1])
        return side, conf, scen

    home_elite = home_score >= ELITE_THRESHOLD
    away_elite = away_score >= ELITE_THRESHOLD
    home_weak = home_score <= WEAK_THRESHOLD
    away_weak = away_score <= WEAK_THRESHOLD

    if home_elite and away_weak:
        conf = max(home_card.get("confidence", 0) if home_card else 0, home_score - 5)
        return "home", min(88, conf + 5), "MISMATCH"

    if away_elite and home_weak:
        conf = max(away_card.get("confidence", 0) if away_card else 0, away_score - 5)
        return "away", min(88, conf + 5), "MISMATCH"

    if home_elite and away_elite:
        diff = home_score - away_score
        if abs(diff) >= 10:
            side = "home" if diff > 0 else "away"
            card = home_card if side == "home" else away_card
            conf = (card.get("confidence", 70) if card else 70) - 3
            return side, conf, "DUEL"

    return None, 0, "no edge"


def build_f5_picks(slate: dict, f5_lines: dict | None = None) -> list[dict]:
    """
    Score F5 ML legs from pitcher model cards + cached F5 prices.
    Routed to parlay channel only.
    """
    cards = list(slate.get("all_cards") or [])
    cards += [c for c in slate.get("post_list") or [] if c not in cards]

    if not cards:
        return []

    games_filter = {(c.get("home_team"), c.get("away_team")) for c in cards}
    lines = f5_lines if f5_lines is not None else fetch_f5_ml_lines(games_filter)
    starters = get_probable_starters()

    by_game: dict[tuple, dict] = defaultdict(lambda: {"home": None, "away": None})
    for card in cards:
        if card.get("market") not in (None, "pitcher_strikeouts"):
            continue
        player = (card.get("player") or "").lower()
        info = starters.get(player)
        gkey = _game_key(card.get("home_team", ""), card.get("away_team", ""))
        if info:
            if info["side"] == "home":
                by_game[gkey]["home"] = card
            else:
                by_game[gkey]["away"] = card
        else:
            # Fallback: lone card in game
            slot = by_game[gkey]
            if not slot["home"]:
                slot["home"] = card
            elif not slot["away"]:
                slot["away"] = card

    picks = []
    for gkey, slot in by_game.items():
        sample = slot["home"] or slot["away"]
        if not sample:
            continue
        line_data = _find_f5_line(
            lines, sample.get("home_team", ""), sample.get("away_team", "")
        )
        if not line_data:
            continue

        home_card = slot["home"]
        away_card = slot["away"]
        h_score = pitcher_score(home_card) if home_card else 50.0
        a_score = pitcher_score(away_card) if away_card else 50.0

        side, conf, scenario = _pick_side(h_score, a_score, home_card, away_card)
        if not side or conf < F5_MIN_CONF:
            continue

        home = line_data["home_team"]
        away = line_data["away_team"]
        pick_team = home if side == "home" else away
        books_row = (
            line_data["home_books_row"] if side == "home"
            else line_data["away_books_row"]
        )
        if "No F5 lines" in books_row:
            continue

        grade = calc_grade(conf)
        if grade in ("C", "D"):
            continue

        k_card = home_card if side == "home" else away_card
        k_label = ""
        if k_card:
            d = (k_card.get("direction") or "over").upper()
            k_label = f"{k_card.get('player')} K {d} {k_card.get('line')}"

        picks.append({
            "market":       "f5_ml",
            "player":       pick_team,
            "pick_team":    pick_team,
            "direction":    "ml",
            "line":         None,
            "label":        f"{pick_team} F5 ML",
            "confidence":   round(conf, 1),
            "grade":        grade,
            "books_row":    books_row,
            "home_team":    home,
            "away_team":    away,
            "scenario":     scenario,
            "book_count":   books_row.count("|") + 1 if "|" in books_row else (
                1 if "DK" in books_row or "Fanatics" in books_row else 0
            ),
            "correlated_k": k_label,
            "gate":         "POST",
        })

    picks.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    return picks


def build_f5_correlated_slips(
    pitcher_pool: list[dict],
    f5_picks: list[dict],
) -> list[dict]:
    """2-leg slips: same-game Pitcher K + F5 ML when teams align."""
    slips = []
    for f5 in f5_picks[:6]:
        home, away = f5.get("home_team"), f5.get("away_team")
        pick_team = f5.get("pick_team")
        for pk in pitcher_pool:
            if pk.get("market") == "f5_ml":
                continue
            if not _team_match(pk.get("home_team", ""), home):
                continue
            if not _team_match(pk.get("away_team", ""), away):
                continue
            starter = get_probable_starters().get((pk.get("player") or "").lower())
            if not starter or not _team_match(starter["team"], pick_team):
                continue
            if pk.get("direction") != "over":
                continue

            d = (pk.get("direction") or "over").upper()
            legs = [
                {
                    "n": 1,
                    "label": f"{pk.get('player')} K {d} {pk.get('line')}",
                    "grade": pk.get("grade"),
                    "confidence": pk.get("confidence"),
                    "books_row": pk.get("books_row", ""),
                },
                {
                    "n": 2,
                    "label": f5.get("label"),
                    "grade": f5.get("grade"),
                    "confidence": f5.get("confidence"),
                    "books_row": f5.get("books_row", ""),
                },
            ]
            avg = round(sum(l["confidence"] for l in legs) / 2, 1)
            slips.append({
                "title":       f"SGP — {pk.get('player')} K + F5 ML",
                "legs":        legs,
                "avg_conf":    avg,
                "games":       1,
            })
            break

    return slips[:4]

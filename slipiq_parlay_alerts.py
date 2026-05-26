# slipiq_parlay_alerts.py
# Private #team-parlay channel — pitcher K menu + F5 ML + correlated SGP slips
# Uses cached props + cached Odds API F5 pulls — no extra ParlayAPI credits.

from datetime import datetime

from slipiq_env import (
    SLIPIQ_PARLAY_MAX_MENU,
    SLIPIQ_PARLAY_MAX_SLIP,
    SLIPIQ_PARLAY_MIN_BOOKS,
    SLIPIQ_PARLAY_MIN_CONF,
)
from slipiq_game_lines import (
    build_f5_correlated_slips,
    build_f5_picks,
    fetch_f5_ml_lines,
)

PARLAY_MIN_CONF  = min(SLIPIQ_PARLAY_MIN_CONF, 60)
PARLAY_MIN_BOOKS = min(SLIPIQ_PARLAY_MIN_BOOKS, 1)
PARLAY_MAX_MENU  = SLIPIQ_PARLAY_MAX_MENU
PARLAY_MAX_SLIP  = SLIPIQ_PARLAY_MAX_SLIP


def _curation_score(card: dict) -> float:
    from slipiq_curate import curation_score
    return curation_score(card)


def _format_prop_line(card: dict) -> str:
    """User-facing: O 4.5 K | Proj 5.2 | B+ | 72%"""
    if card.get("market") == "f5_ml":
        return (
            f"**{card.get('pick_team', card.get('player'))} F5 ML** | "
            f"Grade **{card.get('grade')}** | **{card.get('confidence')}%**"
        )
    direction = (card.get("direction") or "over").upper()
    line = card.get("line")
    proj = card.get("projection")
    grade = card.get("grade", "?")
    conf = card.get("confidence", 0)
    proj_str = f" | Proj {proj:.1f}" if proj is not None else ""
    return (
        f"**{card.get('player')}** {direction[0]} {line} K"
        f"{proj_str} | Grade **{grade}** | **{conf}%**"
    )


def filter_parlay_pool(slate: dict) -> list[dict]:
    """
    Pitcher K legs for parlay menu — DK / Fanatics / PrizePicks graded props.
    """
    post  = list(slate.get("post_list") or [])
    hold  = list(slate.get("hold_list") or [])
    all_c = list(slate.get("all_cards") or [])
    pool  = post + hold
    seen_players = {c.get("player") for c in pool}
    for card in all_c:
        if card.get("player") in seen_players:
            continue
        pool.append(card)
        seen_players.add(card.get("player"))

    qualified = []
    for card in pool:
        if card.get("market") not in (None, "pitcher_strikeouts"):
            continue
        conf = card.get("confidence", 0)
        books = card.get("book_count", 0)
        lines_books = card.get("lines_book_count", books)
        grade = card.get("grade", "D")
        if conf < PARLAY_MIN_CONF:
            continue
        if books < PARLAY_MIN_BOOKS and lines_books < 1:
            continue
        if grade in ("C", "D", "N/A"):
            continue
        if not card.get("line"):
            continue
        card = dict(card)
        card["prop_label"] = _format_prop_line(card)
        qualified.append(card)

    qualified.sort(key=_curation_score, reverse=True)

    seen_games = set()
    deduped = []
    for card in qualified:
        key = (card.get("home_team"), card.get("away_team"))
        if key in seen_games and key != (None, None):
            continue
        deduped.append(card)
        seen_games.add(key)
        if len(deduped) >= PARLAY_MAX_MENU:
            break

    return deduped


def build_suggested_slips(pool: list[dict]) -> dict:
    """Cross-game pitcher K core slip."""
    if not pool:
        return {}

    core = pool[:PARLAY_MAX_SLIP]
    legs = []
    for i, card in enumerate(core, 1):
        direction = (card.get("direction") or "over").upper()
        legs.append({
            "n":          i,
            "player":     card.get("player", "?"),
            "label":      card.get("prop_label") or (
                f"{card.get('player')} K {direction} {card.get('line')}"
            ),
            "confidence": card.get("confidence", 0),
            "grade":      card.get("grade", "?"),
            "books_row":  card.get("books_row", ""),
            "game":       f"{card.get('away_team', '')} @ {card.get('home_team', '')}",
        })

    avg_conf = round(sum(l["confidence"] for l in legs) / len(legs), 1) if legs else 0
    games = len({l["game"] for l in legs})

    return {
        "slip_core": {
            "title":       f"Pitcher K Core — {len(legs)} Legs",
            "legs":        legs,
            "avg_conf":    avg_conf,
            "games":       games,
        },
    }


def post_parlay_alerts(slate: dict) -> bool:
    """Post pitcher K menu, F5 ML picks, and correlated slips to CHANNEL_TEAM_PARLAY."""
    from slipiq_discord import post_parlay_channel

    pool = filter_parlay_pool(slate)

    games_filter = {
        (c.get("home_team"), c.get("away_team"))
        for c in (slate.get("all_cards") or []) + (slate.get("post_list") or [])
    }
    f5_lines = fetch_f5_ml_lines(games_filter=games_filter)
    f5_picks = build_f5_picks(slate, f5_lines)

    for f5 in f5_picks:
        f5["prop_label"] = _format_prop_line(f5)

    slips = build_suggested_slips(pool)
    sgp_slips = build_f5_correlated_slips(pool, f5_picks)

    print(
        f"  [parlay] Menu: {len(pool)} K legs | "
        f"{len(f5_picks)} F5 ML | {len(sgp_slips)} SGP slips"
    )

    if not pool and not f5_picks:
        print("  [parlay] No legs qualify for team parlay channel")
        return False

    return post_parlay_channel(pool, slips, f5_picks=f5_picks, sgp_slips=sgp_slips)


if __name__ == "__main__":
    from slipiq_confidence_agent import run_confidence_agent

    print("=== Parlay Alerts Test ===\n")
    slate = run_confidence_agent()
    pool = filter_parlay_pool(slate)
    f5 = build_f5_picks(slate)
    print(f"K pool: {len(pool)} | F5 picks: {len(f5)}")
    for c in pool[:3]:
        print(f"  K: {c.get('prop_label')} | {c.get('books_row')}")
    for c in f5[:3]:
        print(f"  F5: {c.get('prop_label')} | {c.get('books_row')}")

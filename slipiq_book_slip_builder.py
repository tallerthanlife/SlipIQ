# slipiq_book_slip_builder.py
# Sportsbook slip assembly — mixed + correlated slips with per-book output

from slipiq_curate import curation_score
from slipiq_discord import build_parlay_slip_embed
from slipiq_grading import calc_grade, calc_slip_grade
from slipiq_ml_parlay import build_ml_parlays
from slipiq_nba_curate import curation_score as nba_curation_score
from slipiq_nba_parlay import build_nba_parlays
from slipiq_parlayapi import DISPLAY_BOOK_KEYS, DISPLAY_BOOK_LABELS
from slipiq_slip_review import review_pick

from slipiq_chat_pool import card_to_review_pick, filter_elite, match_legs

CHAT_BOOK_KEYS = ("draftkings", "fanatics", "prizepicks")
MAX_SLIP_LEGS = 6


def _score_card(card: dict) -> float:
    if card.get("sport") == "nba":
        return nba_curation_score(card)
    return curation_score(card)


def _prop_short(card: dict) -> str:
    if card.get("prop_label"):
        return card["prop_label"]
    market = card.get("market") or card.get("prop_type") or "prop"
    labels = {
        "player_strikeouts": "K",
        "player_pitcher_strikeouts": "K",
        "player_hits": "H",
        "player_total_bases": "TB",
        "player_points": "PTS",
        "player_rebounds": "REB",
        "player_assists": "AST",
        "player_points_rebounds_assists": "PRA",
        "player_points_+_rebounds_+_assists": "PRA",
        "player_threes": "3PM",
    }
    if market in labels:
        return labels[market]
    return market.replace("player_", "").replace("_", " ").upper()[:8]


def card_to_slip_leg(card: dict, n: int) -> dict:
    direction = (card.get("direction") or "over").upper()
    player = card.get("player", "Unknown")
    line = card.get("line", 0)
    conf = card.get("confidence", 0)
    grade = card.get("grade") or calc_grade(conf)
    review = card.get("slip_review") or review_pick(card_to_review_pick(card))

    return {
        "n": n,
        "label": f"{player} {_prop_short(card)} {direction} {line}",
        "grade": grade,
        "confidence": conf,
        "books_row": card.get("books_row") or per_book_row(card),
        "ev_confirmed": bool(card.get("ev_confirmed")),
        "game": f"{card.get('away_team', '?')} @ {card.get('home_team', '?')}",
        "sport": card.get("sport", "mlb"),
        "slip_review": review,
        "review_score": review.get("score", 0),
        "_card": card,
    }


def per_book_row(card: dict) -> str:
    """DK | Fanatics | PrizePicks side-by-side for one leg."""
    display = card.get("books_display") or {}
    parts = []
    for key in CHAT_BOOK_KEYS:
        label = DISPLAY_BOOK_LABELS.get(key, key[:2].upper())
        ent = display.get(key) or display.get(key.replace("prizepicks", "underdog"))
        if not ent:
            parts.append(f"{label} N/A")
            continue
        direction = (card.get("direction") or "over").lower()
        price = ent.get(f"{direction}_price") or ent.get("price")
        if price is None:
            parts.append(f"{label} N/A")
        else:
            tag = " 💰" if isinstance(price, (int, float)) and price > 0 else ""
            parts.append(f"{label} {price:+d}{tag}" if isinstance(price, int) else f"{label} {price}{tag}")
    return " | ".join(parts) if parts else (card.get("books_row") or "Verify lines")


def per_book_output(slip: dict) -> dict:
    """Attach per-book table rows to each leg."""
    legs = []
    for leg in slip.get("legs") or []:
        updated = dict(leg)
        card = leg.get("_card") or {}
        if card:
            updated["books_row"] = per_book_row(card)
        legs.append(updated)
    out = dict(slip)
    out["legs"] = legs
    return out


def availability_check(slip: dict, book: str) -> dict | None:
    """Drop legs missing on `book`. Never substitute another book."""
    book = book.lower()
    kept = []
    for leg in slip.get("legs") or []:
        card = leg.get("_card") or {}
        display = card.get("books_display") or {}
        if book in display:
            kept.append(leg)
            continue
        row = (leg.get("books_row") or "").lower()
        label = DISPLAY_BOOK_LABELS.get(book, book).lower()
        if label in row and "n/a" not in row.split("|")[0 if book == "draftkings" else -1]:
            kept.append(leg)
            continue
        if f"{label} n/a" in row.lower() or not display:
            continue
        kept.append(leg)
    if not kept:
        return None
    out = dict(slip)
    out["legs"] = [{**l, "n": i + 1} for i, l in enumerate(kept)]
    out["total_legs"] = len(kept)
    if kept:
        out["avg_conf"] = round(sum(l["confidence"] for l in kept) / len(kept), 1)
    grade_info = calc_slip_grade(out)
    out.update(grade_info)
    return out


def _ml_parlay_legs_to_slip(legs: list[dict], title: str) -> dict | None:
    if not legs:
        return None
    slip_legs = []
    for i, leg in enumerate(legs[:MAX_SLIP_LEGS], 1):
        slip_legs.append({
            "n": i,
            "label": leg.get("label", ""),
            "grade": leg.get("grade", "B"),
            "confidence": leg.get("confidence", 0),
            "books_row": leg.get("books_row", ""),
            "ev_confirmed": leg.get("ev_confirmed", False),
            "game": leg.get("game", ""),
            "slip_review": {"score": 100, "passed": True},
            "review_score": 100,
        })
    slip = {
        "title": title,
        "legs": slip_legs,
        "avg_conf": round(sum(l["confidence"] for l in slip_legs) / len(slip_legs), 1),
        "games": len(set(l.get("game") for l in slip_legs)),
    }
    slip.update(calc_slip_grade(slip))
    return slip


def build_mixed_slip(pool: list[dict], max_legs: int = MAX_SLIP_LEGS) -> dict | None:
    if not pool:
        return None
    ranked = sorted(pool, key=_score_card, reverse=True)
    seen_players = set()
    legs = []
    for card in ranked:
        player = card.get("player")
        if player in seen_players:
            continue
        seen_players.add(player)
        legs.append(card_to_slip_leg(card, len(legs) + 1))
        if len(legs) >= max_legs:
            break
    if not legs:
        return None
    slip = {
        "title": "Mixed Slip — Best +EV Legs",
        "legs": legs,
        "avg_conf": round(sum(l["confidence"] for l in legs) / len(legs), 1),
        "games": len(set(l.get("game") for l in legs)),
        "sport": pool[0].get("sport", "both") if len({c.get('sport') for c in pool}) == 1 else "both",
    }
    slip.update(calc_slip_grade(slip))
    return per_book_output(slip)


def build_correlated_slips(
    pool: list[dict],
    game_lines: list[dict] | None = None,
    sport: str = "both",
) -> dict | None:
    game_lines = game_lines or []
    sport = (sport or "both").lower()

    mlb_pool = [c for c in pool if c.get("sport", "mlb") == "mlb" or "strikeout" in (c.get("market") or "")]
    nba_pool = [c for c in pool if c.get("sport") == "nba"]

    best_sgp = None

    if sport in ("mlb", "both") and mlb_pool:
        pitchers = [c for c in mlb_pool if "strikeout" in (c.get("market") or "")]
        batters = [c for c in mlb_pool if c not in pitchers]
        ml = build_ml_parlays(pitchers or mlb_pool, game_lines, batters)
        if ml and ml.get("slip_1"):
            best_sgp = _ml_parlay_legs_to_slip(
                ml["slip_1"]["legs"],
                "Correlated SGP — MLB Combo",
            )

    if sport in ("nba", "both") and nba_pool:
        nba = build_nba_parlays(nba_pool, game_lines)
        if nba and nba.get("slip_1"):
            candidate = _ml_parlay_legs_to_slip(
                nba["slip_1"]["legs"],
                "Correlated SGP — NBA Combo",
            )
            if candidate and (not best_sgp or candidate.get("slip_score", 0) >= best_sgp.get("slip_score", 0)):
                best_sgp = candidate

    return best_sgp


def review_user_slip(matched_cards: list[dict], unmatched: list[dict]) -> dict | None:
    if not matched_cards and not unmatched:
        return None
    legs = [card_to_slip_leg(c, i + 1) for i, c in enumerate(matched_cards)]
    passed = sum(1 for l in legs if l.get("slip_review", {}).get("passed"))
    slip = {
        "title": "Your Slip — Review",
        "legs": legs,
        "avg_conf": round(sum(l["confidence"] for l in legs) / len(legs), 1) if legs else 0,
        "games": len(set(l.get("game") for l in legs)),
        "unmatched": unmatched,
        "passed_legs": passed,
        "total_legs": len(legs),
    }
    slip.update(calc_slip_grade(slip))
    return per_book_output(slip)


def build_full_response(
    pool: list[dict],
    parsed_legs: list[dict] | None = None,
    constraints: dict | None = None,
    game_lines: list[dict] | None = None,
) -> dict:
    constraints = constraints or {}
    ev_only = constraints.get("ev_only", True)
    max_legs = int(constraints.get("max_legs") or MAX_SLIP_LEGS)
    sport = constraints.get("sport", "both")
    prefer_correlated = constraints.get("prefer_correlated", False)

    elite = filter_elite(pool, ev_only=ev_only)
    response = {
        "mixed_slip": None,
        "correlated_slip": None,
        "user_slip": None,
        "pool_note": "",
        "elite_count": len(elite),
        "pool_count": len(pool),
    }

    if not elite:
        hold = sorted(pool, key=_score_card, reverse=True)[:5]
        response["pool_note"] = (
            f"No +EV POST legs pass chat gates ({len(pool)} in pool). "
            f"Showing closest alternatives unavailable for auto-build."
        )
        response["hold_alternatives"] = hold
        return response

    drop_indices = set(constraints.get("drop_leg_indices") or [])
    if constraints.get("tighter"):
        max_legs = min(max_legs, 3)

    matched_cards = []
    unmatched = []
    if parsed_legs:
        matched_cards, unmatched = match_legs(parsed_legs, elite)
        if matched_cards:
            response["user_slip"] = review_user_slip(matched_cards, unmatched)

    mixed = build_mixed_slip(elite, max_legs=max_legs)
    if mixed and drop_indices:
        kept = [l for l in mixed["legs"] if l["n"] not in drop_indices]
        if kept:
            mixed["legs"] = [{**l, "n": i + 1} for i, l in enumerate(kept)]
            mixed.update(calc_slip_grade(mixed))
    response["mixed_slip"] = mixed

    correlated = build_correlated_slips(elite, game_lines=game_lines, sport=sport)
    response["correlated_slip"] = correlated

    if prefer_correlated and correlated and mixed:
        if correlated.get("slip_score", 0) > mixed.get("slip_score", 0):
            response["pool_note"] = "Correlated SGP scores higher than mixed slip today."

    for book in CHAT_BOOK_KEYS:
        if mixed:
            dk_slip = availability_check(mixed, book)
            if dk_slip:
                response[f"mixed_{book}"] = dk_slip

    return response


def build_chat_embeds(response: dict) -> list[dict]:
    embeds = []
    for key in ("user_slip", "mixed_slip", "correlated_slip"):
        slip = response.get(key)
        if slip and slip.get("legs"):
            title = slip.get("title", key)
            grade = slip.get("slip_grade", "?")
            score = slip.get("slip_score", 0)
            payload = {
                "title": f"{title} — Grade {grade} ({score}/100)",
                "legs": slip["legs"],
                "avg_conf": slip.get("avg_conf", 0),
                "games": slip.get("games", 0),
            }
            emb = build_parlay_slip_embed(payload)
            if emb:
                embeds.append(emb)
    return embeds


if __name__ == "__main__":
    from slipiq_chat_pool import load_pool

    pool, meta = load_pool("both", refresh_if_stale=False)
    elite = filter_elite(pool, ev_only=False)
    resp = build_full_response(elite or pool[:10], constraints={"sport": "both", "ev_only": False})
    mixed = resp.get("mixed_slip")
    print(f"Pool {len(pool)} | Elite {len(elite)} | Mixed legs: {len(mixed.get('legs', [])) if mixed else 0}")

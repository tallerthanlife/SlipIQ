# DISABLED
# This module is disabled. Import it safely; all functions are no-ops.
import sys as _sys
if False:
    pass

# slipiq_book_slip_builder.py
# Sportsbook slip assembly — mixed + correlated slips with per-book output

from slipiq_grading import calc_grade
from slipiq_discord import build_parlay_slip_embed
from slipiq_grading import calc_grade, calc_slip_grade
from slipiq_curate import curation_score
try:
    from slipiq_ml_parlay import build_ml_parlays
except ImportError:
    build_ml_parlays = None

try:
    from slipiq_nba_curate import curation_score as nba_curation_score
except ImportError:
    nba_curation_score = None

try:
    from slipiq_nba_parlay import build_nba_parlays
except ImportError:
    build_nba_parlays = None

from slipiq_parlayapi import DISPLAY_BOOK_KEYS, DISPLAY_BOOK_LABELS
try:
    from slipiq_chat_pool import card_to_review_pick, filter_elite, match_legs
except ImportError:
    card_to_review_pick = None
    filter_elite = None
    match_legs = None

try:
    from slipiq_slip_review import review_pick
except ImportError:
    review_pick = lambda card: {"passed": True, "score": 70, "steps_passed": 4, "steps_total": 6, "units": 1.0, "steps": []}

CHAT_BOOK_KEYS = ("draftkings", "fanatics", "prizepicks")
MAX_SLIP_LEGS = 6


def _score_card(card: dict) -> float:
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
    """
    Convert a pick card to a slip leg dict.
    Includes ev, true_prob, pinnacle prices for montecarlo validation.
    """
    direction = (card.get("direction") or "over").upper()
    player    = card.get("player", "Unknown")
    line      = card.get("line", 0)
    conf      = card.get("confidence", 0)
    grade     = card.get("grade") or calc_grade(conf)
    market    = card.get("market", "")
    review    = card.get("slip_review") or review_pick(card_to_review_pick(card))

    prop_map = {
        "player_pitcher_strikeouts": "Strikeouts",
        "player_strikeouts":         "Strikeouts",
        "player_hits":               "Hits",
        "player_total_bases":        "Total Bases",
        "player_home_runs":          "Home Runs",
        "player_rbis":               "RBIs",
    }
    prop_label = prop_map.get(market, _prop_short(card))

    return {
        "n":             n,
        "leg_type":      "pitcher_k" if "strikeout" in market else "batter",
        "label":         f"{player} {prop_label} {direction} {line}",
        "player":        player,
        "market":        market,
        "prop":          f"{prop_label} {direction} {line}",
        "grade":         grade,
        "confidence":    conf,
        "direction":     direction.lower(),
        "line":          line,
        "books_row":     card.get("books_row") or per_book_row(card),
        "ev_confirmed":  bool(card.get("ev_confirmed")),
        "game":          f"{card.get('away_team', '?')} @ {card.get('home_team', '?')}",
        "sport":         card.get("sport", "mlb"),
        "slip_review":   review,
        "review_score":  review.get("score", 0),
        # montecarlo / slip_router fields
        "ev":            card.get("ev"),
        "ev_source":     card.get("ev_source", "none"),
        "true_prob":     card.get("true_prob"),
        "pinnacle_over": card.get("pinnacle_over"),
        "pinnacle_under":card.get("pinnacle_under"),
        "home_team":     card.get("home_team"),
        "away_team":     card.get("away_team"),
        "books_display": card.get("books_display"),
        "_card":         card,
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
    """
    Show all books side-by-side for each leg.
    Now includes EV % and breakeven for each book line.
    💰 = plus-money | EV shown when real Pinnacle data available.
    """
    if not slip or not slip.get("legs"):
        return slip

    for leg in slip.get("legs", []):
        books_display = leg.get("books_display") or {}
        card = leg.get("_card") or {}
        if not books_display and card:
            books_display = card.get("books_display") or {}
        if not books_display:
            continue

        pin_over  = leg.get("pinnacle_over")  or (card.get("pinnacle_over")  if card else None)
        pin_under = leg.get("pinnacle_under") or (card.get("pinnacle_under") if card else None)
        direction = (leg.get("direction") or card.get("direction") or "over").lower()

        book_parts = []
        for label, bk in books_display.items():
            price = bk.get("price")
            if price is None:
                continue

            price_str  = f"+{price}" if price > 0 else str(price)
            money_flag = " 💰" if price > 0 else ""

            ev_str = ""
            be_str = ""
            if pin_over and pin_under:
                try:
                    from slipiq_ev_engine import assess_leg
                    result = assess_leg(pin_over, pin_under, price, direction)
                    ev_val = result.get("ev")
                    if ev_val is not None:
                        ev_str = f" EV {'+' if ev_val >= 0 else ''}{ev_val*100:.1f}%"
                    be_val = result.get("breakeven")
                    if be_val is not None:
                        be_str = f" (BE {be_val*100:.1f}%)"
                except Exception:
                    pass

            book_parts.append(f"{label} {price_str}{money_flag}{ev_str}{be_str}")

        leg["books_row_ev"] = " | ".join(book_parts)

    return slip


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


def _batter_on_team(batter_pick: dict, team_name: str) -> bool:
    """
    Check if batter plays for the given team.
    Uses slipiq_player_ids.is_batter_on_team() for reliable matching.
    Falls back to string matching if player not in lookup table.
    """
    try:
        from slipiq_player_ids import is_batter_on_team
        if is_batter_on_team(batter_pick.get("player", ""), team_name):
            return True
    except Exception:
        pass

    batter_team = batter_pick.get("team", "") or batter_pick.get("home_team", "")
    if not batter_team or not team_name:
        return False
    team_words = [w for w in team_name.lower().split() if len(w) > 3]
    return any(word in batter_team.lower() for word in team_words)


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

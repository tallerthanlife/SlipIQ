# slipiq_novig.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — No-Vig Math Utilities
#
# WHAT THIS DOES:
#   Converts American odds to fair probabilities by removing the
#   sportsbook vig (overround). Used to compute true edge vs any book.
#
# PUBLIC API:
#   american_to_implied(american_odds)   → raw implied probability
#   prob_to_american(prob)               → convert prob back to American
#   remove_vig(over_odds, under_odds)    → fair probs, vig%, fair lines
#   compute_edge(pick_side_prob, book_odds) → edge % vs a book line
#   get_sharpest_line(bookmakers, market)   → best sharp line from response
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations


def american_to_implied(american_odds: int) -> float:
    """Convert American odds to implied probability."""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    else:
        return abs(american_odds) / (abs(american_odds) + 100)


def prob_to_american(prob: float) -> int:
    if prob >= 0.5:
        return round(-prob / (1 - prob) * 100)
    else:
        return round((1 - prob) / prob * 100)


def remove_vig(over_odds: int, under_odds: int) -> dict:
    """
    Given over/under American odds from any book,
    compute the no-vig fair probability and fair line.
    Returns fair_over_prob, fair_under_prob, vig_pct, fair American odds.
    """
    over_implied  = american_to_implied(over_odds)
    under_implied = american_to_implied(under_odds)
    total_implied = over_implied + under_implied
    vig_pct       = (total_implied - 1.0) * 100

    fair_over_prob  = over_implied  / total_implied
    fair_under_prob = under_implied / total_implied

    return {
        "fair_over_prob":      round(fair_over_prob,  4),
        "fair_under_prob":     round(fair_under_prob, 4),
        "vig_pct":             round(vig_pct, 2),
        "fair_over_american":  prob_to_american(fair_over_prob),
        "fair_under_american": prob_to_american(fair_under_prob),
    }


def compute_edge(pick_side_prob: float, book_odds: int) -> float:
    """
    Given our model's probability for a side and the book's odds,
    compute edge percentage.
    Positive = +EV, Negative = -EV.
    """
    book_implied = american_to_implied(book_odds)
    return round((pick_side_prob - book_implied) * 100, 2)


def get_sharpest_line(bookmakers: list, market: str) -> dict | None:
    """
    From a PropLine bookmakers list, find the sharpest available line.
    Priority: pinnacle > novig > circa > bookmaker > draftkings > fanduel.
    Returns over/under odds and point from the highest-priority book found.
    """
    SHARP_PRIORITY = [
        "pinnacle",
        "pinnacle_us",
        "novig",
        "circa",
        "bookmaker",
        "draftkings",
        "betrivers",
        "fanduel",
        "bovada",
    ]

    best          = None
    best_priority = 999

    for bm in bookmakers:
        bm_key = (bm.get("key") or bm.get("name") or "").lower()
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market:
                continue
            priority = next(
                (i for i, s in enumerate(SHARP_PRIORITY) if s in bm_key),
                999,
            )
            if priority < best_priority:
                best_priority = priority
                outcomes = {o["name"]: o["price"] for o in mkt.get("outcomes", [])}
                best = {
                    "book":       bm_key,
                    "over_odds":  outcomes.get("Over"),
                    "under_odds": outcomes.get("Under"),
                    "point":      mkt.get("outcomes", [{}])[0].get("point"),
                    "priority":   priority,
                }

    return best

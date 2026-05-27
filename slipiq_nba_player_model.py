# slipiq_nba_player_model.py
# NBA player prop projection — per-minute model + negative binomial confidence
#
# FIXES v2:
#   - NB parameterization uses prop-type-calibrated overdispersion (from player_obj)
#   - simulate_confidence() uses r from player_obj.nb_overdispersion, not r=mu
#   - run_nba_model() builds roster_lookup + game_context_map ONCE, injects per player
#   - High-variance cap applies a flat reduction across range, not just a clip at 70
#   - Playoff mode: pace/def factors softened, confidence band widened appropriately
#   - Projection uses already-B2B-adjusted minutes — no second penalty here

import json
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.stats import nbinom

from slipiq_grading import calc_grade
from slipiq_nba_data import (
    build_game_context_map,
    build_player_object,
    current_season,
    get_roster_lookup,
    get_todays_games,
    is_playoff_window,
    season_type_string,
)
from slipiq_parlayapi import (
    SPORT_NBA,
    SHARP_BOOKS,
    NBA_HIGH_VARIANCE_PROP_KEYS,
    NBA_PRIMARY_PROP_KEYS,
    NBA_PROP_LABELS,
    aggregate_by_player,
    build_books_display,
    format_books_row,
    format_fallback_books_row,
    get_all_nba_props,
)

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

MIN_LINE        = 0.5
MAX_JUICE       = -300
MIN_BOOKS_TRUST = 2
EV_THRESHOLD    = 0.02
EDGE_STRONG     = 2.0
EDGE_MODERATE   = 1.0
EDGE_WEAK       = 0.5
N_SIMS          = 10_000

# High-variance prop penalty: subtract from raw confidence BEFORE grade
# Applied flat across the range — not just clipping at 70
HIGH_VARIANCE_CONFIDENCE_PENALTY = 8

MARKET_TO_STAT = {
    "player_points":                        "points",
    "player_rebounds":                      "rebounds",
    "player_assists":                       "assists",
    "player_points_rebounds_assists":       "pra",
    "player_points_+_rebounds_+_assists":   "pra",
    "player_threes":                        "threes",
    "player_3_pointers_made":               "threes",
}


# ═══════════════════════════════════════════
# PROJECTION + SIMULATION
# ═══════════════════════════════════════════

def simulate_confidence(
    projection: float,
    line: float,
    direction: str,
    nb_r: float = 4.0,
) -> int:
    """
    Negative binomial Monte Carlo — P(stat > line | over) or P(stat < line | under).

    nb_r (overdispersion): passed from player_obj.nb_overdispersion.
    Lower r = fatter tails = more honest uncertainty.
    This replaces the broken r=max(mu,1) parameterization that made r=mu
    and produced artificially tight distributions.

    NB parameterization:
      mean = mu = projection
      variance = mu + mu² / r
      p = r / (r + mu)   [scipy nbinom convention]

    Higher nb_r → tighter distribution (high-volume scorers).
    Lower nb_r → wider distribution (assists, threes).
    """
    if line is None or projection is None or projection <= 0:
        return 50

    mu  = max(projection, 0.1)
    r   = max(nb_r, 0.5)           # floor at 0.5 to prevent degenerate dist
    p   = r / (r + mu)

    try:
        samples = nbinom.rvs(r, p, size=N_SIMS)
    except Exception:
        samples = np.random.poisson(mu, N_SIMS)

    if direction == "over":
        prob = float((samples > line).mean())
    else:
        prob = float((samples < line).mean())

    return max(0, min(100, round(prob * 100)))


def apply_high_variance_penalty(confidence: int, market_key: str) -> int:
    """
    Flat penalty for high-variance markets (threes, etc.).
    Applied across the full confidence range — not just clipped at top.
    Ensures these props can't accidentally masquerade as Tier 1.
    """
    if market_key in NBA_HIGH_VARIANCE_PROP_KEYS:
        return max(0, confidence - HIGH_VARIANCE_CONFIDENCE_PENALTY)
    return confidence


def score_edge(
    projection: float,
    line: float,
    ev_over=None,
    ev_under=None,
) -> dict:
    if line is None or line == 0:
        return {"signal": "no_line", "grade": "N/A", "direction": "over", "diff": 0}

    diff      = round(projection - line, 2)
    direction = "over" if diff > 0 else "under"
    abs_diff  = abs(diff)

    if abs_diff >= EDGE_STRONG:
        strength = "strong"
    elif abs_diff >= EDGE_MODERATE:
        strength = "moderate"
    elif abs_diff >= EDGE_WEAK:
        strength = "lean"
    else:
        strength = "push"

    ev_value     = ev_over if direction == "over" else ev_under
    ev_confirmed = (ev_value or 0) > EV_THRESHOLD

    return {
        "direction":    direction,
        "diff":         diff,
        "abs_diff":     round(abs_diff, 2),
        "strength":     strength,
        "ev_confirmed": ev_confirmed,
        "ev_value":     round(ev_value, 4) if ev_value else None,
        "signal":       f"{strength}_{direction}" if strength != "push" else "no_play",
    }


def _prop_label(player: str, direction: str, line: float, market_key: str) -> str:
    stat = NBA_PROP_LABELS.get(market_key, market_key.replace("player_", "").upper())
    d    = direction.upper() if direction else "OVER"
    return f"{player} {d[0]}{line} {stat}"


# ═══════════════════════════════════════════
# PICK CARD BUILDER
# ═══════════════════════════════════════════

def build_pick_card(
    player_name: str,
    prop_data: dict,
    market_key: str,
    player_obj: dict,
) -> dict | None:
    line            = prop_data.get("sharp_line") or prop_data.get("line_consensus")
    book_count      = prop_data.get("book_count", 0)
    lines_book_count = prop_data.get("lines_book_count", book_count)
    ev_over         = prop_data.get("ev_over")
    ev_under        = prop_data.get("ev_under")
    pinnacle        = prop_data.get("pinnacle")
    best_over       = prop_data.get("best_over")
    best_under      = prop_data.get("best_under")
    entries         = prop_data.get("_entries") or []

    if not line or line < MIN_LINE:
        return None

    projection = player_obj.get("projected_stat", 0)
    edge       = score_edge(projection, line, ev_over, ev_under)
    direction  = edge.get("direction", "over")

    if edge.get("strength") == "push":
        return None

    # Pull calibrated NB overdispersion from player object
    nb_r       = player_obj.get("nb_overdispersion", 4.0)
    confidence = simulate_confidence(projection, line, direction, nb_r=nb_r)

    # High-variance flat penalty (replaces broken clip-at-70)
    confidence = apply_high_variance_penalty(confidence, market_key)

    grade = calc_grade(confidence)

    # ── Books display ──────────────────────────────────────────
    books_display = build_books_display(entries, direction) if entries else {}
    books_row     = format_books_row(books_display)
    if not books_row and entries:
        books_row = format_fallback_books_row(entries, direction)
    if not books_row:
        books_row = "No lines on target books yet"

    best_book  = None
    pick_side  = best_over if direction == "over" else best_under
    if not pick_side and entries:
        for ent in entries:
            if ent.get("book", "").lower() in SHARP_BOOKS:
                continue
            if direction == "over" and ent.get("over_price") is not None:
                pick_side = ent
                break
            if direction == "under" and ent.get("under_price") is not None:
                pick_side = ent
                break
    if pick_side:
        price = pick_side.get("over_price") if direction == "over" else pick_side.get("under_price")
        if price is not None and price >= MAX_JUICE:
            best_book = {
                "book":  pick_side.get("book_title") or pick_side.get("book"),
                "price": price,
                "side":  direction,
            }

    # ── Flags ──────────────────────────────────────────────────
    flags = []
    if player_obj.get("b2b_flag"):
        flags.append("⚠️  back-to-back — minutes suppressed")
    if player_obj.get("teammates_out"):
        flags.append(f"📈 role expansion: {', '.join(player_obj['teammates_out'])} OUT")
    spread = player_obj.get("spread")
    if spread is not None and abs(float(spread or 0)) > 9:
        flags.append("⚠️  blowout risk — star minutes may sit")
    if book_count < MIN_BOOKS_TRUST:
        flags.append(f"⚠️  thin market: only {book_count} action book(s)")
    if player_obj.get("is_playoff"):
        flags.append("🏆 playoff game — higher defensive intensity expected")
    if not player_obj.get("team_abbr"):
        flags.append("⚠️  team context unresolved — projection uses league averages")

    stat_type = MARKET_TO_STAT.get(market_key, "points")
    trend     = player_obj.get("minutes_trend", "flat")
    tier      = 1 if confidence >= 70 else (2 if confidence >= 55 else 0)

    return {
        "player":        player_name,
        "sport":         "nba",
        "game_date":     prop_data.get("game_date") or player_obj.get("game_date"),
        "home_team":     prop_data.get("home_team") or player_obj.get("home_team"),
        "away_team":     prop_data.get("away_team") or player_obj.get("away_team"),
        "team_abbr":     player_obj.get("team_abbr", ""),
        "opponent_abbr": player_obj.get("opponent_abbr", ""),
        "market":        market_key,
        "market_key":    market_key,
        "prop_type":     stat_type,
        "prop_label":    _prop_label(player_name, direction, line, market_key),

        "line":          line,
        "projection":    projection,
        "direction":     direction,
        "diff":          edge.get("diff"),

        "grade":         grade,
        "signal":        edge.get("signal"),
        "confidence":    confidence,
        "tier":          tier,
        "ev_value":      edge.get("ev_value"),
        "ev_confirmed":  edge.get("ev_confirmed"),

        "best_book":         best_book,
        "books_display":     books_display,
        "books_row":         books_row,
        "book_count":        book_count,
        "lines_book_count":  lines_book_count,

        "trend":              trend,
        "projected_minutes":  player_obj.get("projected_minutes"),
        "pace_factor":        player_obj.get("pace_factor"),
        "b2b_flag":           player_obj.get("b2b_flag"),
        "recent_stat_list":   player_obj.get("recent_stat_list", []),
        "flags":              flags,
        "is_playoff":         player_obj.get("is_playoff", False),
        "season_type":        player_obj.get("season_type", ""),

        "_internal": {
            "pinnacle_line":    pinnacle.get("line") if pinnacle else None,
            "season_avg":       player_obj.get("season_avg_stat"),
            "minutes_avg":      player_obj.get("minutes_season_avg"),
            "opp_def_rating":   player_obj.get("opp_def_rating"),
            "projected_pace":   player_obj.get("projected_pace"),
            "nb_overdispersion": nb_r,
            "team_abbr":        player_obj.get("team_abbr"),
            "opponent_abbr":    player_obj.get("opponent_abbr"),
            "spread":           player_obj.get("spread"),
        },
    }


# ═══════════════════════════════════════════
# MAIN RUNNER
# ═══════════════════════════════════════════

def run_nba_model(sport_key: str = SPORT_NBA) -> list[dict]:
    print("\n" + "=" * 60)
    print("SlipIQ NBA Player Model — Running")
    print(f"Season:      {current_season()} ({season_type_string()})")
    print(f"Playoff:     {is_playoff_window()}")
    print(f"Time:        {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    # ── Step 1: Load props ────────────────────────────────────
    print("\n[1] Loading NBA prop lines...")
    all_props = get_all_nba_props(sport_key)
    raw       = all_props.get("all", [])
    primary   = [p for p in raw if p.get("market_key") in NBA_PRIMARY_PROP_KEYS]
    print(f"    {len(primary)} primary prop lines")

    if not primary:
        print("\n    No NBA player props available yet.")
        return []

    agg = aggregate_by_player(primary)
    print(f"    {len(agg)} player/market combos")

    # ── Step 2: Build shared context lookups ONCE ─────────────
    # This is the core fix: roster and schedule resolved once, not per-player.
    print("\n[2] Building shared context (roster + schedule)...")
    roster_lookup   = get_roster_lookup()
    games           = get_todays_games()
    game_ctx_map    = build_game_context_map(games)

    resolved_count  = 0
    unresolved      = []

    print(f"    {len(roster_lookup)} players in roster lookup")
    print(f"    {len(game_ctx_map)} teams with games today")

    # ── Step 3: Build pick cards ──────────────────────────────
    print("\n[3] Building pick cards...")
    raw_cards = []

    for (player, market), prop_data in agg.items():
        stat = MARKET_TO_STAT.get(market, "points")

        # Resolve team context: roster → schedule → prop data
        home = prop_data.get("home_team", "")
        away = prop_data.get("away_team", "")

        # Try roster lookup
        from slipiq_nba_data import _infer_team_from_prop
        team_abbr, opp_abbr = _infer_team_from_prop(home, away, player, roster_lookup)

        # If roster miss, try schedule map with known team
        if not team_abbr or not opp_abbr:
            # Check both home and away against game_ctx_map
            if home in game_ctx_map or away in game_ctx_map:
                # We know the matchup; resolution will happen inside build_player_object
                pass

        if team_abbr:
            resolved_count += 1
        else:
            unresolved.append(player)

        ctx = {
            "home_team":     home,
            "away_team":     away,
            "game_date":     prop_data.get("game_date", ""),
            "team_abbr":     team_abbr,
            "opponent_abbr": opp_abbr,
            "spread":        prop_data.get("spread"),
            "game_total":    prop_data.get("game_total"),
        }

        player_obj = build_player_object(
            player,
            ctx,
            prop_type=stat,
            roster_lookup=roster_lookup,
        )
        if not player_obj:
            continue

        card = build_pick_card(player, prop_data, market, player_obj)
        if card:
            raw_cards.append(card)

    print(f"    Team resolved: {resolved_count} / {len(agg)} players")
    if unresolved:
        print(f"    Unresolved: {', '.join(unresolved[:10])}"
              f"{'...' if len(unresolved) > 10 else ''}")

    # ── Step 4: Dedup and sort ────────────────────────────────
    seen: dict = {}
    for card in raw_cards:
        key = (card["player"], card.get("prop_type"))
        if key not in seen or card["confidence"] > seen[key]["confidence"]:
            seen[key] = card
    pick_cards = list(seen.values())

    grade_order = {"A+": 0, "A": 1, "B+": 2, "B": 3, "C": 4}
    pick_cards.sort(
        key=lambda x: (
            -x.get("confidence", 0),
            grade_order.get(x.get("grade", "C"), 4),
        )
    )

    print(f"    {len(pick_cards)} picks after dedup + filters")

    # ── Step 5: Cache for downstream use ─────────────────────
    cache_path = CACHE_DIR / "nba_model_cards.json"
    with open(cache_path, "w") as f:
        json.dump(
            {"run_time": datetime.now().isoformat(), "cards": pick_cards},
            f, indent=2, default=str,
        )

    return pick_cards


def print_pick_cards(cards: list[dict]):
    if not cards:
        print("\nNo NBA picks. Re-run when books post lines.")
        return

    print("\n" + "=" * 60)
    print("SLIPIQ — NBA PLAYER PROP PICKS")
    print("=" * 60)
    for card in cards[:15]:
        team  = card.get("team_abbr", "?")
        opp   = card.get("opponent_abbr", "?")
        ptype = card.get("season_type", "")
        print(f"\n  [{card.get('grade')}] {card.get('prop_label')}")
        print(f"  {team} vs {opp} | {ptype}")
        print(f"  Proj: {card.get('projection')} | Conf: {card.get('confidence')}% "
              f"| NB-r: {card.get('_internal', {}).get('nb_overdispersion')}")
        print(f"  {card.get('books_row')}")
        for flag in card.get("flags", []):
            print(f"  {flag}")


if __name__ == "__main__":
    cards = run_nba_model()
    print_pick_cards(cards)

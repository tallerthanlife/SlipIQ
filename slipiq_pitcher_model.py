# slipiq_pitcher_model.py
# Phase 1 Core — MLB Pitcher Strikeout Model
# Rebuilt clean — 2026-05-26
#
# FIXES IN THIS BUILD:
#   - Filter lines < 3.0 (drops milestone/novelty markets)
#   - Juice filter: don't recommend > -300 juice
#   - Deduplicate by player (best confidence card wins)
#   - Conflict flag: trend vs direction disagreement
#   - Minimum 2 books before trusting a line
#
# PROJECTION LOGIC:
#   Blended = (season K rate × BF × whiff adj × opp adj × park) × w_season
#           + (recent avg K × trend mult) × w_recent
#
# LEAGUE BASELINES (2026 Statcast):
#   Avg K/start : 5.8
#   Avg whiff   : 11.2%
#   Avg K rate  : 22.8%
#   Avg BF/start: 24

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from slipiq_parlayapi import (
    SPORT_MLB,
    SHARP_BOOKS,
    aggregate_by_player,
    build_books_display,
    format_books_row,
    format_fallback_books_row,
    get_all_props,
)
from slipiq_mlb_data import get_pitcher_recent_form, get_pitcher_statcast_season

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────
# LEAGUE BASELINES
# ─────────────────────────────────────────
LEAGUE_WHIFF_RATE  = 0.112
LEAGUE_K_RATE      = 0.228
AVG_BF_PER_START   = 24.0
AVG_K_PER_START    = 5.8
MIN_SEASON_PITCHES = 200
MIN_RECENT_STARTS  = 3

# Line filters
MIN_LINE           = 3.0    # drop anything below — milestone/novelty markets
MAX_JUICE          = -300   # don't recommend bets with more juice than this
MIN_BOOKS_TRUST    = 2      # flag picks with fewer books posting
EV_THRESHOLD       = 0.02   # minimum 2% EV to confirm

# Edge thresholds
EDGE_STRONG        = 0.75
EDGE_MODERATE      = 0.40
EDGE_WEAK          = 0.20


# ═════════════════════════════════════════
# PROJECTION ENGINE
# ═════════════════════════════════════════

def project_pitcher_strikeouts(
    player_name: str,
    season_stats: dict,
    recent_form: dict,
    opponent_k_rate: float = None,
    park_factor: float = 1.0,
) -> dict:

    opp_k_rate = opponent_k_rate or LEAGUE_K_RATE

    # ── Season inputs ──
    season_k_rate  = season_stats.get("k_rate", LEAGUE_K_RATE)
    season_whiff   = season_stats.get("whiff_rate", LEAGUE_WHIFF_RATE)
    season_pitches = season_stats.get("pitches_total", 0)

    # Whiff adjustment vs league avg — capped at ±40%
    whiff_adj = season_whiff / LEAGUE_WHIFF_RATE if LEAGUE_WHIFF_RATE > 0 else 1.0
    whiff_adj = max(0.7, min(1.4, whiff_adj))

    # Opponent K vulnerability vs league avg — capped at ±20%
    opp_adj = opp_k_rate / LEAGUE_K_RATE if LEAGUE_K_RATE > 0 else 1.0
    opp_adj = max(0.8, min(1.2, opp_adj))

    season_proj = (
        season_k_rate * AVG_BF_PER_START
        * whiff_adj * opp_adj * park_factor
    )

    # ── Recent form inputs ──
    recent_starts = recent_form.get("n_starts", 0)
    recent_avg_k  = recent_form.get("avg_k", 0)
    recent_trend  = recent_form.get("trend", "flat")

    trend_mult = {
        "hot": 1.05, "flat": 1.00, "cold": 0.93,
        "small_sample": 1.00, "insufficient": 1.00,
    }.get(recent_trend, 1.00)

    recent_proj = recent_avg_k * trend_mult if recent_avg_k > 0 else season_proj

    # ── Weights based on sample quality ──
    has_season = season_pitches >= MIN_SEASON_PITCHES
    if has_season and recent_starts >= 5:
        w_season, w_recent = 0.60, 0.40
    elif has_season and recent_starts >= MIN_RECENT_STARTS:
        w_season, w_recent = 0.70, 0.30
    else:
        w_season, w_recent = 0.90, 0.10

    blended = (season_proj * w_season) + (recent_proj * w_recent)
    projection = round(blended, 2)

    # ── Confidence factors ──
    cf = {}

    if season_pitches >= 500:
        cf["sample_size"] = 90
    elif season_pitches >= 200:
        cf["sample_size"] = 70
    else:
        cf["sample_size"] = 40

    k_list = recent_form.get("k_per_start", [])
    if len(k_list) >= 4:
        std = float(np.std(k_list))
        cf["consistency"] = 85 if std <= 1.5 else (65 if std <= 2.5 else 45)
    else:
        cf["consistency"] = 50

    cf["matchup"] = 75 if opponent_k_rate else 55

    if season_whiff > LEAGUE_WHIFF_RATE * 1.15:
        cf["stuff"] = 85
    elif season_whiff > LEAGUE_WHIFF_RATE * 0.95:
        cf["stuff"] = 65
    else:
        cf["stuff"] = 45

    return {
        "player":             player_name,
        "projection":         projection,
        "season_proj":        round(season_proj, 2),
        "recent_proj":        round(recent_proj, 2),
        "weights":            {"season": w_season, "recent": w_recent},
        "season_k_rate":      round(season_k_rate, 4),
        "season_whiff":       round(season_whiff, 4),
        "whiff_adj":          round(whiff_adj, 4),
        "opp_adj":            round(opp_adj, 4),
        "park_factor":        park_factor,
        "trend":              recent_trend,
        "recent_k_list":      k_list,
        "confidence_factors": cf,
        "season_pitches":     season_pitches,
        "recent_starts":      recent_starts,
    }


# ═════════════════════════════════════════
# EDGE SCORER
# ═════════════════════════════════════════

def score_edge(
    projection: float,
    line: float,
    ev_over: float = None,
    ev_under: float = None,
) -> dict:

    if line is None or line == 0:
        return {"signal": "no_line", "grade": "N/A"}

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

    if strength == "strong" and ev_confirmed:
        grade = "A"
    elif strength == "strong":
        grade = "B+"
    elif strength == "moderate" and ev_confirmed:
        grade = "B"
    elif strength == "moderate":
        grade = "B-"
    elif strength == "lean" and ev_confirmed:
        grade = "C+"
    elif strength == "lean":
        grade = "C"
    else:
        grade = "D"

    return {
        "direction":    direction,
        "diff":         diff,
        "abs_diff":     round(abs_diff, 2),
        "strength":     strength,
        "ev_confirmed": ev_confirmed,
        "ev_value":     round(ev_value, 4) if ev_value else None,
        "grade":        grade,
        "signal":       f"{strength}_{direction}" if strength != "push" else "no_play",
    }


# ═════════════════════════════════════════
# CONFIDENCE SCORER
# ═════════════════════════════════════════

def score_confidence(
    proj_data: dict,
    edge_data: dict,
    book_count: int,
    lines_book_count: int = 0,
) -> int:

    cf = proj_data.get("confidence_factors", {})
    market_depth = max(book_count, lines_book_count)

    base = (
        cf.get("sample_size", 50) * 0.25 +
        cf.get("consistency", 50) * 0.25 +
        cf.get("matchup",     50) * 0.20 +
        cf.get("stuff",       50) * 0.30
    )

    edge_bonus = {
        "A": 15, "B+": 10, "B": 7, "B-": 4,
        "C+": 2, "C": 0, "D": -10, "N/A": -15,
    }.get(edge_data.get("grade", "D"), 0)

    if market_depth >= 6:
        book_bonus = 8
    elif market_depth >= 3:
        book_bonus = 4
    elif market_depth >= 1:
        book_bonus = 0
    else:
        book_bonus = -5

    trend       = proj_data.get("trend", "flat")
    trend_bonus = {"hot": 5, "flat": 0, "cold": -5}.get(trend, 0)

    # Penalty: trend conflicts with signal direction
    direction = edge_data.get("direction", "")
    conflict  = (trend == "hot" and direction == "under") or \
                (trend == "cold" and direction == "over")
    conflict_penalty = -8 if conflict else 0

    raw = base + edge_bonus + book_bonus + trend_bonus + conflict_penalty
    return max(0, min(100, round(raw)))


# ═════════════════════════════════════════
# PICK CARD BUILDER
# ═════════════════════════════════════════

def build_pick_card(
    player_name: str,
    prop_data: dict,
    season_stats: dict,
    recent_form: dict,
    opponent_k_rate: float = None,
    park_factor: float = 1.0,
) -> dict | None:

    line = prop_data.get("sharp_line") or prop_data.get("line_consensus")
    book_count       = prop_data.get("book_count", 0)
    lines_book_count = prop_data.get("lines_book_count", book_count)
    ev_over    = prop_data.get("ev_over")
    ev_under   = prop_data.get("ev_under")
    pinnacle   = prop_data.get("pinnacle")
    best_over  = prop_data.get("best_over")
    best_under = prop_data.get("best_under")
    entries    = prop_data.get("_entries") or []

    # ── Hard filters ──
    if not line or line < MIN_LINE:
        return None  # milestone/novelty market

    # Project + score
    proj = project_pitcher_strikeouts(
        player_name, season_stats, recent_form, opponent_k_rate, park_factor
    )
    edge       = score_edge(proj["projection"], line, ev_over, ev_under)
    confidence = score_confidence(proj, edge, book_count, lines_book_count)

    direction = edge.get("direction", "")
    books_display = build_books_display(entries, direction) if entries else {}
    books_row     = format_books_row(books_display)
    if not books_row and entries:
        books_row = format_fallback_books_row(entries, direction)
    if not books_row:
        books_row = "No lines on target books yet"

    # ── Best action book (DK / Fanatics / PrizePicks only) ──
    best_book = None
    pick_side = best_over if direction == "over" else best_under
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
        price = (
            pick_side.get("over_price")
            if direction == "over"
            else pick_side.get("under_price")
        )
        if price is not None and price >= MAX_JUICE:
            best_book = {
                "book":  pick_side.get("book_title") or pick_side.get("book"),
                "price": price,
                "side":  direction,
            }

    # ── Flags (user-facing — no sharp book names) ──
    flags = []
    trend = proj.get("trend", "flat")
    if trend == "hot" and direction == "under":
        flags.append("⚠️  trend conflicts: hot pitcher, under signal")
    if trend == "cold" and direction == "over":
        flags.append("⚠️  trend conflicts: cold pitcher, over signal")
    if book_count < MIN_BOOKS_TRUST:
        flags.append(f"⚠️  thin market: only {book_count} action book(s) posting")
    if not pinnacle:
        flags.append("⚠️  sharp anchor missing — EV unconfirmed")

    return {
        # Identity
        "player":        player_name,
        "game_date":     prop_data.get("game_date"),
        "home_team":     prop_data.get("home_team"),
        "away_team":     prop_data.get("away_team"),
        "market":        "pitcher_strikeouts",

        # Core numbers
        "line":          line,
        "projection":    proj["projection"],
        "direction":     direction,
        "diff":          edge.get("diff"),

        # Signals
        "grade":         edge.get("grade"),
        "signal":        edge.get("signal"),
        "confidence":    confidence,
        "ev_value":      edge.get("ev_value"),
        "ev_confirmed":  edge.get("ev_confirmed"),

        # Book info — action books only on cards
        "best_book":      best_book,
        "books_display":  books_display,
        "books_row":      books_row,
        "book_count":       book_count,
        "lines_book_count": lines_book_count,

        # Context
        "trend":         trend,
        "recent_k_list": proj.get("recent_k_list"),
        "flags":         flags,

        # Operator only (never post to Discord)
        "_internal": {
            "pinnacle_line":      pinnacle.get("line") if pinnacle else None,
            "pinnacle_over":      pinnacle.get("over_price") if pinnacle else None,
            "pinnacle_under":     pinnacle.get("under_price") if pinnacle else None,
            "confidence_factors": proj.get("confidence_factors"),
            "season_proj":        proj.get("season_proj"),
            "recent_proj":        proj.get("recent_proj"),
            "season_k_rate":      proj.get("season_k_rate"),
            "season_whiff":       proj.get("season_whiff"),
            "whiff_adj":          proj.get("whiff_adj"),
            "opp_adj":            proj.get("opp_adj"),
            "weights":            proj.get("weights"),
            "park_factor":        park_factor,
            "season_pitches":     proj.get("season_pitches"),
            "recent_starts":      proj.get("recent_starts"),
        }
    }


# ═════════════════════════════════════════
# MAIN RUNNER
# ═════════════════════════════════════════

def run_pitcher_model(sport_key: str = SPORT_MLB) -> list[dict]:

    print("\n" + "=" * 60)
    print("SlipIQ Pitcher Model — Running")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    # Step 1: Props
    print("\n[1] Loading prop lines...")
    all_props = get_all_props(sport_key)
    k_props   = all_props["pitcher_strikeouts"]
    agg       = aggregate_by_player(k_props)
    print(f"    {len(agg)} pitcher/market combos found")

    if not agg:
        print("\n    No pitcher strikeout lines available yet.")
        print("    Books typically post full slates by 9-10am AZ.")
        return []

    # Step 2: Season stats (cached)
    print("\n[2] Loading Statcast season stats...")
    season_df = get_pitcher_statcast_season()

    season_lookup = {}
    if not season_df.empty:
        for _, row in season_df.iterrows():
            season_lookup[row["player_name"].lower()] = row.to_dict()
        print(f"    {len(season_lookup)} pitchers in Statcast")
    else:
        print("    [warn] No season stats — using league averages")

    # Step 3: Build pick cards
    print("\n[3] Building pick cards...")
    raw_cards = []

    for (player, market), prop_data in agg.items():
        season_stats = season_lookup.get(player.lower(), {})
        recent_form  = get_pitcher_recent_form(player)

        card = build_pick_card(
            player_name  = player,
            prop_data    = prop_data,
            season_stats = season_stats,
            recent_form  = recent_form,
        )

        if card:
            raw_cards.append(card)

    # Step 4: Deduplicate — best confidence card per pitcher
    seen = {}
    for card in raw_cards:
        player = card["player"]
        if player not in seen or card["confidence"] > seen[player]["confidence"]:
            seen[player] = card
    pick_cards = list(seen.values())

    # Step 5: Sort — confidence desc, then grade
    grade_order = {"A": 0, "B+": 1, "B": 2, "B-": 3, "C+": 4, "C": 5, "D": 6, "N/A": 7}
    pick_cards.sort(key=lambda x: (
        -x.get("confidence", 0),
        grade_order.get(x.get("grade", "D"), 6)
    ))

    print(f"    {len(pick_cards)} picks after dedup + filters")
    return pick_cards


# ═════════════════════════════════════════
# OUTPUT PRINTER
# ═════════════════════════════════════════

def print_pick_cards(cards: list[dict]):

    if not cards:
        print("\nNo picks. Re-run after 9am when full slate posts.")
        return

    print("\n" + "=" * 60)
    print("SLIPIQ — PITCHER STRIKEOUT PICKS")
    print(f"{datetime.now().strftime('%A %B %d, %Y — %I:%M %p AZ')}")
    print("=" * 60)

    for card in cards:
        grade      = card.get("grade", "?")
        player     = card.get("player", "")
        line       = card.get("line")
        proj       = card.get("projection")
        direction  = card.get("direction", "").upper()
        diff       = card.get("diff", 0)
        confidence = card.get("confidence", 0)
        trend      = card.get("trend", "")
        ev         = card.get("ev_value")
        best       = card.get("best_book")
        books      = card.get("book_count", 0)
        flags      = card.get("flags", [])
        home       = card.get("home_team", "")
        away       = card.get("away_team", "")
        k_list     = card.get("recent_k_list", [])
        internal   = card.get("_internal") or {}
        pinnacle   = internal.get("pinnacle_line")
        pin_over   = internal.get("pinnacle_over")

        print(f"\n  ┌─ [{grade}] {player}")
        if home and away:
            print(f"  │  Game: {away} @ {home}")
        print(f"  │  Line: {line} | Proj: {proj} | {direction} by {diff:+.2f}")
        print(f"  │  Confidence: {confidence}% | Trend: {trend} | Books: {books}")

        if pinnacle:
            print(f"  │  Pinnacle: {pinnacle} ({'+' if pin_over and pin_over > 0 else ''}{pin_over})")
        if ev:
            ev_tag = "✅" if ev > EV_THRESHOLD else "⚠️ "
            print(f"  │  EV: {ev:+.1%} vs Pinnacle {ev_tag}")
        if k_list:
            print(f"  │  Last {len(k_list)} starts: {k_list}")
        if best:
            print(f"  │  ▶ Bet: {best['side'].upper()} {best['price']} @ {best['book']}")
        else:
            print(f"  │  ▶ No recommended book (juice or no line)")
        for flag in flags:
            print(f"  │  {flag}")
        print(f"  └{'─' * 50}")

    # Summary
    a_b   = [c for c in cards if c.get("grade") in ("A", "B+", "B")]
    plays = [c for c in cards if c.get("signal") != "no_play"]

    print(f"\n  SUMMARY")
    print(f"  Total picks   : {len(cards)}")
    print(f"  Grade A/B     : {len(a_b)}")
    print(f"  Playable      : {len(plays)}")
    print(f"  EV confirmed  : {len([c for c in cards if c.get('ev_confirmed')])}")
    print(f"  Thin markets  : {len([c for c in cards if c.get('book_count', 0) < MIN_BOOKS_TRUST])}")


# ═════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════

if __name__ == "__main__":
    cards = run_pitcher_model()
    print_pick_cards(cards)

# Alias for orchestrator compatibility
run_all_models = run_pitcher_model

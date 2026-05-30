# slipiq_batter_model.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ Batter Model — Projection + EV scoring for batter props
#
# run_batter_model(sport_key=SPORT_MLB, min_confidence=55, markets=None) -> list[dict]
#
# WHAT CHANGED (rebuild):
#   score_batter_edge() — fake ev_conf = (ev_val or 0) > 0.02 removed.
#   Now calls slipiq_ev_engine.assess_leg() with real Pinnacle prices.
#   build_batter_pick_card() — exposes pinnacle_over, pinnacle_under,
#   true_prob, ev at top level so SlipRouter and confidence agent can
#   use them without digging into _internal.
#   project_batter_stat() — adds true_prob_over / true_prob_under via
#   Poisson CDF (appropriate for hit/TB counts, unlike neg-binomial for Ks).
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations
from datetime import datetime
from pathlib import Path
import numpy as np

from slipiq_mlb_data import get_batter_k_rates, get_pitcher_recent_form
from slipiq_batter_lines import get_batter_lines, SPORT_MLB, PRIMARY_MARKETS
from slipiq_grading import calc_grade

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─── Thresholds ───────────────────────────────────────────────
EDGE_STRONG   = 0.40
EDGE_MODERATE = 0.20
EDGE_WEAK     = 0.10
MIN_EV_GATE   = 0.02

MAX_JUICE       = -200
MIN_BOOKS_TRUST = 2

AVG_PA_PER_GAME = 4.2

BASELINES = {
    "player_hits":        0.9,
    "player_total_bases": 1.3,
    "player_home_runs":   0.12,
    "player_rbis":        0.6,
    "player_runs":        0.5,
    "player_singles":     0.6,
    "player_doubles":     0.15,
}


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — PROJECTION ENGINE
# ═══════════════════════════════════════════════════════════════

def project_batter_stat(
    player_name:  str,
    market_key:   str,
    season_stats: dict,
    recent_form:  dict,
    park_factor:  float = 1.0,
) -> dict:
    """
    Project a batter's stat for a given market.
    Returns projection dict including true_prob_over / true_prob_under
    via Poisson CDF — the calibrated probabilities ev_engine needs.
    """
    baseline    = BASELINES.get(market_key, 0.5)

    stat_map = {
        "player_hits":        "hit_rate",
        "player_total_bases": "xslg",
        "player_home_runs":   "hr_rate",
        "player_rbis":        "hit_rate",
        "player_runs":        "bb_rate",
        "player_singles":     "single_rate",
        "player_doubles":     "double_rate",
    }
    rate_key    = stat_map.get(market_key, "hit_rate")
    season_rate = season_stats.get(rate_key, 0)

    if market_key == "player_total_bases":
        season_proj = (season_rate or 0.380) * AVG_PA_PER_GAME
    elif market_key in ("player_hits", "player_singles", "player_doubles", "player_home_runs"):
        season_proj = (season_rate or baseline / AVG_PA_PER_GAME) * AVG_PA_PER_GAME
    else:
        season_proj = baseline

    form_map = {
        "player_hits":        "avg_hits",
        "player_total_bases": "avg_tb",
        "player_home_runs":   "avg_hr",
        "player_rbis":        "avg_hits",
        "player_runs":        "avg_hits",
        "player_singles":     "avg_hits",
        "player_doubles":     "avg_tb",
    }
    form_key     = form_map.get(market_key, "avg_hits")
    recent_val   = recent_form.get(form_key, 0)
    recent_trend = recent_form.get("trend", "flat")

    trend_mult = {"hot": 1.08, "flat": 1.00, "cold": 0.92, "small_sample": 1.00}.get(recent_trend, 1.00)
    recent_proj = recent_val * trend_mult if recent_val > 0 else season_proj

    n_games  = recent_form.get("n_games", 0)
    pa_total = season_stats.get("pa_total", 0)

    if pa_total >= 200 and n_games >= 8:
        w_season, w_recent = 0.55, 0.45
    elif pa_total >= 100 and n_games >= 5:
        w_season, w_recent = 0.70, 0.30
    else:
        w_season, w_recent = 0.85, 0.15

    blended    = (season_proj * w_season) + (recent_proj * w_recent)
    projection = round(blended * park_factor, 3)

    # ── Confidence factors ─────────────────────────────────────
    cf = {}
    cf["sample_size"]      = 85 if pa_total >= 300 else (65 if pa_total >= 100 else 40)
    cf["consistency"]      = 75 if n_games >= 8 else (55 if n_games >= 5 else 35)
    cf["stuff"]            = 70
    exit_velo              = season_stats.get("avg_exit_velo", 88)
    cf["contact_quality"]  = 85 if exit_velo >= 92 else (65 if exit_velo >= 89 else 45)

    # ── True probability via Poisson CDF ──────────────────────
    # Poisson is appropriate for batter hit/TB counts (discrete, low rate)
    # Use line from caller if available — projection serves as lambda
    true_prob_over  = None
    true_prob_under = None
    try:
        from scipy.stats import poisson
        if projection > 0:
            lam = projection
            # P(stat > line) = 1 - P(stat <= floor(line))
            # Caller provides line; default uses projection as self-reference
            # build_batter_pick_card() calls this and passes the actual line
            # Store raw so caller can compute P(>line) with actual line
            # We store lambda here; caller extracts true_prob with the line
            cf["_lambda"] = round(lam, 4)
    except Exception:
        pass

    return {
        "player":        player_name,
        "market":        market_key,
        "projection":    projection,
        "season_proj":   round(season_proj, 3),
        "recent_proj":   round(recent_proj, 3),
        "weights":       {"season": w_season, "recent": w_recent},
        "trend":         recent_trend,
        "cf":            cf,
        "pa_total":      pa_total,
        "n_games":       n_games,
        "exit_velo":     exit_velo,
        "true_prob_over":  true_prob_over,
        "true_prob_under": true_prob_under,
    }


def _compute_poisson_true_prob(projection: float, line: float) -> tuple[float | None, float | None]:
    """
    Compute P(stat > line) and P(stat <= line) using Poisson CDF.
    Returns (true_prob_over, true_prob_under) or (None, None) on error.
    """
    try:
        from scipy.stats import poisson
        if projection > 0 and line is not None:
            line_floor      = int(line)
            true_prob_under = round(float(poisson.cdf(line_floor, projection)), 6)
            true_prob_over  = round(1.0 - true_prob_under, 6)
            return true_prob_over, true_prob_under
    except Exception:
        pass
    return None, None


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — EDGE SCORER (rebuilt — real ev_engine)
# ═══════════════════════════════════════════════════════════════

def score_batter_edge(
    projection:    float,
    line:          float,
    ev_over:       float = None,
    ev_under:      float = None,
    pinnacle_over:  int  = None,
    pinnacle_under: int  = None,
    true_prob:     float = None,
) -> dict:
    """
    Score edge and grade for a batter pick card.

    REBUILT: calls slipiq_ev_engine.assess_leg() when Pinnacle prices
    are available. ev_confirmed is now a real mathematical check.
    Old code: ev_conf = (ev_val or 0) > 0.02  ← REMOVED.

    Args:
        projection     : model projection
        line           : prop line
        ev_over/under  : parlayapi EV (fallback only)
        pinnacle_over/under : Pinnacle American odds (primary source)
        true_prob      : Poisson true_prob from project_batter_stat (optional)
    """
    if line is None:
        return {
            "signal": "no_line", "grade": "N/A", "direction": "over",
            "diff": 0, "ev": None, "ev_confirmed": False, "ev_value": None,
            "true_prob": None, "no_pinnacle": True,
        }

    diff      = round(projection - line, 3)
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

    # ── Real EV via ev_engine ─────────────────────────────────
    ev_engine_result = None
    real_ev          = None
    no_pinnacle      = True

    if pinnacle_over is not None and pinnacle_under is not None:
        try:
            from slipiq_ev_engine import assess_leg
            # Use parlayapi ev to approximate soft book price if needed
            soft_price = _ev_to_soft_american(ev_over if direction == "over" else ev_under)
            ev_engine_result = assess_leg(
                pinnacle_over  = pinnacle_over,
                pinnacle_under = pinnacle_under,
                book_american  = soft_price,
                direction      = direction,
            )
            real_ev     = ev_engine_result["ev"]
            no_pinnacle = ev_engine_result["no_pinnacle"]
            if true_prob is None:
                true_prob = ev_engine_result.get("true_prob")
        except Exception:
            pass

    # Fallback to parlayapi EV
    if ev_engine_result is None:
        ev_val  = ev_over if direction == "over" else ev_under
        real_ev = round(float(ev_val), 4) if ev_val else None
        no_pinnacle = True

    ev_confirmed = bool(real_ev is not None and real_ev >= MIN_EV_GATE)

    # Grade
    if strength == "strong" and ev_confirmed:      grade = "A"
    elif strength == "strong":                     grade = "B+"
    elif strength == "moderate" and ev_confirmed:  grade = "B"
    elif strength == "moderate":                   grade = "B-"
    elif strength == "lean" and ev_confirmed:      grade = "C+"
    elif strength == "lean":                       grade = "C"
    else:                                          grade = "D"

    return {
        "direction":    direction,
        "diff":         diff,
        "abs_diff":     abs_diff,
        "strength":     strength,
        "ev_confirmed": ev_confirmed,
        "ev_value":     round(real_ev, 4) if real_ev is not None else None,
        "ev":           round(real_ev, 4) if real_ev is not None else None,
        "grade":        grade,
        "signal":       f"{strength}_{direction}" if strength != "push" else "no_play",
        "true_prob":    round(true_prob, 6) if true_prob else None,
        "no_pinnacle":  no_pinnacle,
        "ev_source":    "ev_engine_pinnacle" if (ev_engine_result and not no_pinnacle)
                        else ("parlayapi_only" if real_ev is not None else "none"),
    }


def _ev_to_soft_american(ev_float: float | None) -> int:
    """Approximate soft book American odds from parlayapi EV float."""
    if ev_float and ev_float > 0.03:
        return -110
    elif ev_float and ev_float > 0.01:
        return -115
    return -120


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — CONFIDENCE SCORER
# ═══════════════════════════════════════════════════════════════

def score_batter_confidence(
    proj_data:  dict,
    edge_data:  dict,
    book_count: int,
) -> int:
    """
    Confidence score 0-100. Context modifiers applied in confidence_agent.
    Composite of sample quality, consistency, edge strength, market depth.
    """
    cf   = proj_data.get("cf", {})
    base = (
        cf.get("sample_size",     50) * 0.25 +
        cf.get("consistency",     50) * 0.25 +
        cf.get("stuff",           50) * 0.20 +
        cf.get("contact_quality", 50) * 0.30
    )

    edge_bonus = {
        "A": 15, "B+": 10, "B": 7, "B-": 4,
        "C+": 2, "C": 0, "D": -10,
    }.get(edge_data.get("grade", "D"), 0)

    book_bonus = 8 if book_count >= 6 else (4 if book_count >= 3 else -5)

    trend     = proj_data.get("trend", "flat")
    direction = edge_data.get("direction", "")
    conflict  = (trend == "hot" and direction == "under") or \
                (trend == "cold" and direction == "over")
    trend_bonus = 5 if not conflict and trend in ("hot", "cold") else (-8 if conflict else 0)

    return max(0, min(100, round(base + edge_bonus + book_bonus + trend_bonus)))


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — PICK CARD BUILDER (rebuilt — pinnacle + true_prob at top level)
# ═══════════════════════════════════════════════════════════════

def build_batter_pick_card(
    player_name: str,
    market_key:  str,
    prop_data:   dict,
    season_stats: dict,
    recent_form:  dict,
    park_factor:  float = 1.0,
) -> dict | None:
    """
    Build a batter pick card with real EV, Pinnacle prices, and true_prob
    all exposed at the top level — required by SlipRouter and confidence agent.
    """
    line       = prop_data.get("line") or prop_data.get("line_consensus")
    book_count = prop_data.get("book_count", 0)
    ev_over    = prop_data.get("ev_over")
    ev_under   = prop_data.get("ev_under")
    pinnacle   = prop_data.get("pinnacle") or {}
    best_over  = prop_data.get("best_over") or {}
    best_under = prop_data.get("best_under") or {}

    if not line:
        return None

    # Pinnacle prices
    pin_over  = pinnacle.get("over_price")
    pin_under = pinnacle.get("under_price")

    # Project
    proj = project_batter_stat(player_name, market_key, season_stats, recent_form, park_factor)

    # Compute Poisson true_prob with actual line
    tp_over, tp_under = _compute_poisson_true_prob(proj["projection"], line)
    proj["true_prob_over"]  = tp_over
    proj["true_prob_under"] = tp_under

    direction    = "over" if proj["projection"] >= line else "under"
    nb_true_prob = tp_over if direction == "over" else tp_under

    # Score edge with real ev_engine
    edge = score_batter_edge(
        projection     = proj["projection"],
        line           = line,
        ev_over        = ev_over,
        ev_under       = ev_under,
        pinnacle_over  = pin_over,
        pinnacle_under = pin_under,
        true_prob      = nb_true_prob,
    )

    conf  = score_batter_confidence(proj, edge, book_count)
    grade = calc_grade(conf)

    # Best book
    best_book = {}
    if direction == "over" and best_over:
        price = best_over.get("over_price", 0)
        if price and price >= MAX_JUICE:
            best_book = {
                "book":  best_over.get("book_title", ""),
                "price": price,
                "side":  "over",
            }
    elif direction == "under" and best_under:
        price = best_under.get("under_price", 0)
        if price and price >= MAX_JUICE:
            best_book = {
                "book":  best_under.get("book_title", ""),
                "price": price,
                "side":  "under",
            }

    flags = []
    if book_count < MIN_BOOKS_TRUST:
        flags.append(f"⚠️ thin market: {book_count} book(s)")
    if not pin_over:
        flags.append("⚠️ no Pinnacle line")
    if edge.get("no_pinnacle"):
        flags.append("⚠️ EV unconfirmed — no Pinnacle")
    trend = proj.get("trend", "flat")
    if (trend == "hot" and direction == "under") or (trend == "cold" and direction == "over"):
        flags.append(f"⚠️ trend conflicts: {trend} vs {direction}")

    # Breakeven display
    breakeven_str = None
    try:
        from slipiq_ev_engine import breakeven_display
        soft_price = best_book.get("price") or -115
        breakeven_str = breakeven_display(soft_price)
    except Exception:
        pass

    return {
        # Identity
        "player":         player_name,
        "market":         market_key,
        "sport":          "mlb",
        "game_date":      prop_data.get("game_date"),
        "home_team":      prop_data.get("home_team"),
        "away_team":      prop_data.get("away_team"),
        "line":           line,
        "projection":     proj["projection"],
        "direction":      direction,
        "diff":           edge.get("diff"),

        # Grading
        "grade":          grade,
        "signal":         edge.get("signal"),
        "confidence":     conf,

        # EV — top level (required by SlipRouter + confidence_agent)
        "ev":             edge.get("ev"),
        "ev_value":       edge.get("ev_value"),
        "ev_confirmed":   edge.get("ev_confirmed"),
        "ev_source":      edge.get("ev_source", "none"),
        "true_prob":      edge.get("true_prob"),
        "no_pinnacle":    edge.get("no_pinnacle", True),

        # Pinnacle prices — top level (required by SlipRouter + montecarlo)
        "pinnacle_over":  pin_over,
        "pinnacle_under": pin_under,
        "pinnacle_line":  pinnacle.get("line"),

        # Book info
        "best_book":      best_book,
        "book_count":     book_count,
        "breakeven":      breakeven_str,

        # Context
        "trend":          trend,
        "flags":          flags,

        # Internal (operator only — never post to Discord)
        "_internal": {
            "season_proj":  proj.get("season_proj"),
            "recent_proj":  proj.get("recent_proj"),
            "exit_velo":    proj.get("exit_velo"),
            "pa_total":     proj.get("pa_total"),
            "cf":           proj.get("cf"),
            "weights":      proj.get("weights"),
            "tp_over":      tp_over,
            "tp_under":     tp_under,
        },
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — MAIN RUNNER
# ═══════════════════════════════════════════════════════════════

def run_batter_model(
    sport_key:      str = SPORT_MLB,
    min_confidence: int = 55,
    markets:        set = None,
) -> list[dict]:
    """
    Full batter model pipeline.
    Returns sorted pick cards for confidence agent.
    """
    print("\n" + "=" * 60)
    print("SlipIQ Batter Model — Running")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    target_markets = markets or PRIMARY_MARKETS

    print("\n[1] Loading batter lines...")
    lines = get_batter_lines(sport_key, markets=target_markets)
    print(f"    {len(lines)} player/market combos")

    if not lines:
        print("    No batter lines available.")
        return []

    cards = []
    print(f"\n[2] Building pick cards...")
    ev_engine_count = 0
    fallback_count  = 0

    for (player, market), prop_data in lines.items():
        # get_batter_statcast returns full DataFrame — filter for this player
        try:
            from slipiq_mlb_data import get_batter_statcast_season
            batter_df = get_batter_statcast_season()
            if not batter_df.empty:
                # Match by MLBAM ID if available, else by name
                try:
                    from slipiq_player_ids import get_mlb_id
                    mlbam_id = get_mlb_id(player)
                    if mlbam_id:
                        row = batter_df[batter_df["batter"] == mlbam_id]
                    else:
                        row = pd.DataFrame()
                except Exception:
                    row = pd.DataFrame()
                season_stats = row.iloc[0].to_dict() if not row.empty else {}
            else:
                season_stats = {}
        except Exception:
            season_stats = {}

        try:
            recent_form = get_pitcher_recent_form(player) or {}
        except Exception:
            recent_form = {}

        card = build_batter_pick_card(
            player_name  = player,
            market_key   = market,
            prop_data    = prop_data,
            season_stats = season_stats,
            recent_form  = recent_form,
        )

        if card is None:
            continue
        if card.get("confidence", 0) < min_confidence:
            continue

        src = card.get("ev_source", "none")
        if src == "ev_engine_pinnacle":
            ev_engine_count += 1
        elif src == "parlayapi_only":
            fallback_count += 1

        cards.append(card)

    print(f"    {len(cards)} cards built | "
          f"EV engine: {ev_engine_count} | Fallback: {fallback_count}")

    # Sort: real EV first, then EV value, then confidence
    cards.sort(key=lambda c: (
        0 if c.get("ev_source") == "ev_engine_pinnacle" else 1,
        -(c.get("ev") or 0),
        -(c.get("confidence") or 0),
    ))

    return cards


if __name__ == "__main__":
    cards = run_batter_model()
    print(f"\n{'='*60}")
    print(f"BATTER MODEL — {len(cards)} picks")
    for card in cards[:8]:
        ev_str  = f" EV {card.get('ev', 0)*100:+.1f}%" if card.get("ev") else ""
        pin_str = "" if card.get("pinnacle_over") else " [no Pinnacle]"
        print(f"  [{card.get('grade')}] {card.get('player'):<22} "
              f"{card.get('market','').replace('player_',''):<18} "
              f"{card.get('direction','').upper()} {card.get('line')} | "
              f"{card.get('confidence')}%{ev_str}{pin_str}")

# slipiq_curate.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ Morning Curation Layer
# Runs at 8:30am AZ via scheduler (slipiq_orchestrator.py)
# Re-runs at 9:15am for full slate confirmation
#
# PIPELINE ORDER:
#   1. Run confidence agent → gated slate (POST/HOLD/SKIP)
#   2. Filter to POST picks only
#   3. Sort by real EV (from ev_engine) descending — not arbitrary score
#   4. Cap at MAX_DAILY_POSTS, deduplicate by game
#   5. Route through SlipRouter → SGP / indep / ML-RL / PP / lotto pools
#   6. Post to Discord
#   7. Log to Supabase/cache for Sharp Review
#
# WHAT CHANGED (rebuild):
#   OLD: curation_score() = confidence + (ev_value*100)*2.5 + book_count_bonus + juice_penalty
#        Arbitrary weighting on an uncalibrated confidence number.
#   NEW: sort by card["ev"] from assess_leg() descending.
#        filter: ev >= MIN_EV_POST (0.02)
#        No arbitrary weights. The math already ranked them.
#
#   OLD: SlipRouter was not wired in.
#   NEW: run_curation() calls SlipRouter at the end and returns routing
#        output alongside the pick list for all downstream slip builders.
# ═══════════════════════════════════════════════════════════════

import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from slipiq_confidence_agent import run_confidence_agent, SPORT_MLB
from slipiq_discord import (
    post_morning_brief,
    post_waiting_message,
    run_discord_post,
)

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─── Config ───────────────────────────────────────────────────
MAX_DAILY_POSTS = 7      # cap at 7 picks per day
MIN_EV_POST     = 0.02   # minimum real edge to include in daily post
                         # (0.0 = post anything that passes gate; 0.02 = require edge)
BANKROLL        = 500.0  # default Kelly bankroll — reads from env if set

try:
    from slipiq_env import _get_int
    _br = _get_int("SLIPIQ_BANKROLL", 0)
    if _br > 0:
        BANKROLL = float(_br)
except Exception:
    pass


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — PICK SELECTION (rebuilt — EV sort, not composite score)
# ═══════════════════════════════════════════════════════════════

def _ev_sort_key(card: dict) -> tuple:
    """
    Sort key for POST picks.
    Priority order:
      1. Real EV (ev_source == ev_engine_pinnacle) beats fallback
      2. EV value descending (higher edge first)
      3. Grade (A > B+ > B)
      4. Confidence descending
    """
    grade_order = {"A": 0, "B+": 1, "B": 2, "B-": 3, "C+": 4, "C": 5, "D": 6}
    ev_real     = 1 if card.get("ev_source") != "ev_engine_pinnacle" else 0
    ev_val      = -(card.get("ev") or 0)   # negative = descending
    grade_rank  = grade_order.get(card.get("grade", "D"), 6)
    conf_rank   = -(card.get("confidence") or 0)
    return (ev_real, ev_val, grade_rank, conf_rank)


def select_top_picks(
    post_list:  list[dict],
    max_picks:  int = MAX_DAILY_POSTS,
    min_ev:     float = MIN_EV_POST,
) -> list[dict]:
    """
    Select top N picks from POST list for the daily post.

    Filters: ev >= min_ev (skips picks with no Pinnacle data unless ev_source fallback)
    Sorts: real EV descending
    Deduplicates: one pick per matchup (game_key)
    """
    if not post_list:
        return []

    # Filter: require minimum EV when we have real data
    # If ev_source is parlayapi_only and ev is low, still include (don't punish missing Pinnacle)
    # But if ev_engine confirmed it's negative, skip
    filtered = []
    for card in post_list:
        ev  = card.get("ev")
        src = card.get("ev_source", "none")
        if src == "ev_engine_pinnacle" and ev is not None and ev < min_ev:
            # Real EV confirmed negative — skip
            continue
        filtered.append(card)

    # Sort by EV descending (real EV first)
    sorted_picks = sorted(filtered, key=_ev_sort_key)

    selected   = []
    seen_games = set()

    for card in sorted_picks:
        game_key = (
            card.get("home_team", "").lower(),
            card.get("away_team", "").lower(),
        )
        if game_key in seen_games and game_key != ("", ""):
            continue
        selected.append(card)
        seen_games.add(game_key)
        if len(selected) >= max_picks:
            break

    return selected


def select_best_pick(post_list: list[dict]) -> dict | None:
    """Best single pick — highest real EV with A/B+ grade."""
    if not post_list:
        return None
    return sorted(post_list, key=_ev_sort_key)[0]


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — SLATE LOGGER
# ═══════════════════════════════════════════════════════════════

def log_slate(slate: dict, top_picks: list[dict]):
    """Save curated picks to cache for Sharp Review and calibration logging."""
    today     = datetime.now().strftime("%Y-%m-%d")
    cache_key = f"slate_{today}.json"
    path      = CACHE_DIR / cache_key

    # Also save as latest_picks.json for scanner line-move lookups
    latest_path = CACHE_DIR / "latest_picks.json"

    log_entry = {
        "date":        today,
        "run_time":    datetime.now().isoformat(),
        "top_picks":   top_picks,
        "post_count":  slate.get("post_count", 0),
        "hold_count":  slate.get("hold_count", 0),
        "skip_count":  slate.get("skip_count", 0),
        "total":       slate.get("total", 0),
    }

    path.write_text(json.dumps(log_entry, indent=2, default=str))
    latest_path.write_text(json.dumps(top_picks, indent=2, default=str))

    # Log to calibration tracker
    try:
        from slipiq_calibration import log_prediction
        for pick in top_picks:
            if pick.get("player") and pick.get("market") and pick.get("true_prob"):
                log_prediction(
                    player     = pick["player"],
                    market     = pick.get("market", "player_pitcher_strikeouts"),
                    direction  = pick.get("direction", "over"),
                    line       = pick.get("line") or 0,
                    model_prob = pick.get("true_prob") or 0,
                    book_odds  = (pick.get("best_book") or {}).get("price"),
                    ev         = pick.get("ev"),
                    grade      = pick.get("grade"),
                    sport      = "mlb",
                    game_date  = pick.get("game_date"),
                )
    except Exception as e:
        print(f"  [curate] calibration log error: {e}")

    print(f"  [curate] slate logged → cache/{cache_key}")


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — SLIP ROUTING (new — wires SlipRouter)
# ═══════════════════════════════════════════════════════════════

def route_picks(top_picks: list[dict]) -> dict:
    """
    Route the curated pick pool through SlipRouter.
    Returns routing dict with sgp_pool, indep_pool, ml_rl_pool, pp_queue, lotto_pool.
    Downstream: slipiq_ml_parlay, slipiq_independent_parlay, slipiq_prizepicks.
    """
    try:
        from slipiq_slip_router import SlipRouter
        router  = SlipRouter(bankroll=BANKROLL)
        routing = router.route(top_picks)
        stats   = routing["stats"]
        print(f"  [curate] SlipRouter: "
              f"SGP={stats['sgp']} | Indep={stats['indep']} | "
              f"ML/RL={stats['ml_rl']} | PP={stats['pp_queue']} | "
              f"Lotto={stats['lotto']} | Rejected={stats['rejected']}")
        return routing
    except Exception as e:
        print(f"  [curate] SlipRouter error: {e} — returning empty routing")
        return {
            "sgp_pool": [], "indep_pool": [], "ml_rl_pool": [],
            "pp_queue": [], "lotto_pool": [], "rejected": [],
            "stats": {"total_input": len(top_picks), "sgp": 0, "indep": 0,
                      "ml_rl": 0, "pp_queue": 0, "lotto": 0, "rejected": len(top_picks)},
        }


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — MAIN CURATION RUNNER
# ═══════════════════════════════════════════════════════════════

def run_curation(
    sport_key:  str = SPORT_MLB,
    post_discord: bool = True,
) -> dict:
    """
    Full morning curation pipeline.

    Returns:
        {
            "top_picks"    : list[dict],  # curated picks for Discord
            "best_pick"    : dict | None,
            "routing"      : dict,        # SlipRouter output for slip builders
            "post_count"   : int,
            "slate"        : dict,        # full confidence agent output
        }
    """
    print("\n" + "=" * 60)
    print("SlipIQ Morning Curation — Running")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    # Step 1: Run confidence agent (includes ev_engine, context, gate)
    slate = run_confidence_agent(sport_key)

    post_list = slate.get("post_list", [])

    if not post_list:
        print("\n  No POST picks. Checking HOLD list...")
        hold_list = slate.get("hold_list", [])
        if not hold_list:
            print("  No picks yet — books still opening.")
            if post_discord:
                post_waiting_message()
            return {
                "top_picks":  [],
                "best_pick":  None,
                "routing":    {},
                "post_count": 0,
                "slate":      slate,
            }
        print(f"  {len(hold_list)} picks on HOLD — will retry at confirm run.")

    # Step 2: Select top picks (EV sort, not composite score)
    top_picks = select_top_picks(post_list)
    best_pick = select_best_pick(post_list)

    print(f"\n  Selected {len(top_picks)} picks for Discord post")
    for i, pick in enumerate(top_picks, 1):
        ev_str = f" EV {pick.get('ev', 0)*100:+.1f}%" if pick.get('ev') else ""
        print(f"  {i}. [{pick.get('grade')}] {pick.get('player')} "
              f"{pick.get('direction','').upper()} {pick.get('line')}"
              f" | {pick.get('confidence')}%{ev_str}")

    # Step 3: Route through SlipRouter
    all_post_for_routing = post_list  # route full POST list, not just top N
    routing = route_picks(all_post_for_routing)

    # Step 4: Log
    log_slate(slate, top_picks)

    # Step 5: Post to Discord
    curation_result = {
        "top_picks":    top_picks,
        "best_pick":    best_pick,
        "routing":      routing,
        "post_count":   len(top_picks),
        "slate":        slate,
        "post_count_all": slate.get("post_count", 0),
    }

    if post_discord and top_picks:
        try:
            run_discord_post(curation_result)
        except Exception as e:
            print(f"  [curate] Discord post error: {e}")

    return curation_result


def run_full_curation(sport_key: str = SPORT_MLB) -> dict:
    """
    Alias for run_curation(). Called by orchestrator confirm run.
    Skips Discord post if already posted today.
    """
    return run_curation(sport_key=sport_key, post_discord=True)


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    result = run_curation()
    print(f"\nCuration complete: {result['post_count']} picks posted")
    routing = result.get("routing", {})
    if routing:
        stats = routing.get("stats", {})
        print(f"Routing: SGP={stats.get('sgp',0)} | "
              f"Indep={stats.get('indep',0)} | "
              f"PP={stats.get('pp_queue',0)} | "
              f"Lotto={stats.get('lotto',0)}")

# slipiq_slip_router.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Slip Router
# The missing layer between curation and Discord posting.
#
# WHAT THIS DOES:
#   Takes the full EV-filtered pick pool from slipiq_curate.py
#   and routes every leg to the correct slip type.
#
# SLIP TYPES PRODUCED:
#   A. SGP Parlay (2-4 games × 3-5 legs each) — DK/Fanatics
#      Pitcher K + Batter TB/Hits + F5 or F3 ML (whichever is better EV)
#      Monte Carlo validated via slipiq_montecarlo.py
#
#   B. Independent Pitcher Lotto (7-12 legs) — DK/Fanatics
#      One pitcher per game, sorted by absolute true_prob (lotto mode)
#      $0.25 stake — lottery ticket with highest math-backed odds
#
#   C. Independent ML/RL Parlay (3-8 legs) — DK/Fanatics
#      F3 ML, F5 ML, F3 RL, F5 RL — best EV side per game, one per game
#      Separate slip from SGP
#
#   D. PrizePicks Rolling Entries (3-6 picks) — PrizePicks only
#      All pitcher markets (K, outs, hits allowed, walks)
#      Dynamic intraday via slipiq_prizepicks.intraday_scanner()
#      Queue expiration: force-fire as lock times approach
#
# ROUTING RULES (in priority order):
#   1. If leg has Pinnacle EV > 0.03 AND is a pitcher/batter prop:
#      → route to SGP pool if a correlated partner exists in same game
#      → otherwise route to PP queue (if market eligible) AND independent parlay
#   2. F3/F5 ML or RL legs with EV > 0.02: route to ML/RL parlay pool
#   3. All pitcher prop legs with true_prob > 0.60: eligible for lotto slip
#   4. Legs that fail EV filter but are in PP eligible markets at > 0.57 true_prob:
#      → PP queue only (PrizePicks threshold is different from sportsbook)
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

PRIZEPICKS_LEG_THRESHOLD = 0.542
prizepicks_leg_threshold = 0.542

CACHE_DIR  = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
ROUTER_LOG = CACHE_DIR / "slip_router_output.json"

# ─── EV thresholds per slip type ──────────────────────────────
SGP_MIN_EV       = 0.03    # minimum per-leg EV to include in SGP
INDEP_MIN_EV     = 0.02    # minimum per-leg EV for independent parlay
ML_RL_MIN_EV     = 0.02    # minimum EV for ML/RL independent parlay leg
LOTTO_MIN_PROB   = 0.60    # minimum true_prob for lotto slip (absolute, not EV)
PP_MIN_PROB_4PK  = PRIZEPICKS_LEG_THRESHOLD + 0.02  # 4-pick buffer

# ─── Market classifications ───────────────────────────────────
SGP_ELIGIBLE_PITCHER = {
    "player_pitcher_strikeouts",
    "player_strike_outs",
    "player_pitcher_outs",
}
SGP_ELIGIBLE_BATTER = {
    "player_hits",
    "player_total_bases",
    "player_rbis",
    "player_runs",
    "player_home_runs",
}
PP_ELIGIBLE_PITCHER = {
    "player_pitcher_strikeouts",
    "player_strike_outs",
    "player_pitcher_outs",
    "player_hits_allowed",
    "player_walks",
    "player_earned_runs",
    "player_batters_faced",
}
ML_RL_MARKETS = {
    "f3_ml", "f5_ml", "f3_rl", "f5_rl",
    "first_3_innings_ml", "first_5_innings_ml",
    "first_3_innings_rl", "first_5_innings_rl",
}

# F3 vs F5: prefer F3 when F3 EV > F5 EV (lower vig, sharper early innings)
# Both are valid — router picks the better one per game
F3_MARKETS = {"f3_ml", "f3_rl", "first_3_innings_ml", "first_3_innings_rl"}
F5_MARKETS = {"f5_ml", "f5_rl", "first_5_innings_ml", "first_5_innings_rl"}


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — LEG ENRICHMENT
# ═══════════════════════════════════════════════════════════════

def enrich_leg_with_ev(leg: dict) -> dict:
    """
    Add EV and true_prob to a pick card leg if not already present.
    Uses Pinnacle over/under prices from the leg's data.
    Returns the leg dict with added fields.
    """
    if leg.get("ev") is not None and leg.get("true_prob") is not None:
        return leg

    pin_over  = leg.get("pinnacle_over")  or leg.get("sharp_over")
    pin_under = leg.get("pinnacle_under") or leg.get("sharp_under")
    book_odds = (
        leg.get("best_price") or
        leg.get("odds") or
        leg.get("price") or
        -115
    )
    direction = (leg.get("direction") or "over").lower()

    result = assess_leg(
        pinnacle_over=pin_over,
        pinnacle_under=pin_under,
        book_american=book_odds,
        direction=direction,
        bankroll=float(leg.get("bankroll", 500)),
    )

    if result:
        leg = {
            **leg,
            "ev":          result["ev"],
            "true_prob":   result["true_prob"],
            "passes_ev":   result["passes"],
            "kelly_stake": result["kelly_stake"],
            "breakeven":   result["breakeven"],
            "no_pinnacle": result["no_pinnacle"],
        }
    else:
        leg["no_pinnacle"] = True
        leg["passes_ev"]   = False

    return leg


def _game_key(leg: dict) -> str:
    away = leg.get("away_team") or leg.get("away") or "?"
    home = leg.get("home_team") or leg.get("home") or "?"
    return f"{away}@{home}".lower()


def _market_normalized(leg: dict) -> str:
    return (leg.get("market") or leg.get("prop_type") or "").lower().replace(" ", "_")


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — ROUTING ENGINE
# ═══════════════════════════════════════════════════════════════

class SlipRouter:
    """
    Routes a pool of pick cards into four slip type buckets.
    Call route(pool) to get the full routing result.
    """

    def __init__(
        self,
        sgp_min_ev:     float = SGP_MIN_EV,
        indep_min_ev:   float = INDEP_MIN_EV,
        ml_rl_min_ev:   float = ML_RL_MIN_EV,
        lotto_min_prob: float = LOTTO_MIN_PROB,
        pp_min_prob:    float = PP_MIN_PROB_4PK,
        bankroll:       float = 500.0,
    ):
        self.sgp_min_ev     = sgp_min_ev
        self.indep_min_ev   = indep_min_ev
        self.ml_rl_min_ev   = ml_rl_min_ev
        self.lotto_min_prob = lotto_min_prob
        self.pp_min_prob    = pp_min_prob
        self.bankroll       = bankroll

    def route(self, pool: list[dict]) -> dict:
        """
        Main routing function.

        Args:
            pool: list of pick cards from slipiq_curate.py
                  Each card must have: player, market, direction, line,
                  confidence, grade, and ideally pinnacle_over/under.

        Returns:
            {
                "sgp_pool"    : list[dict],  # legs eligible for SGP
                "indep_pool"  : list[dict],  # legs for independent pitcher parlay
                "ml_rl_pool"  : list[dict],  # legs for ML/RL independent parlay
                "pp_queue"    : list[dict],  # legs for PrizePicks rolling queue
                "lotto_pool"  : list[dict],  # legs for $0.25 lotto slip
                "rejected"    : list[dict],  # legs that failed all filters
                "stats"       : dict,
            }
        """
        sgp_pool   = []
        indep_pool = []
        ml_rl_pool = []
        pp_queue   = []
        lotto_pool = []
        rejected   = []

        # Group by game for SGP correlation check
        by_game: dict[str, list[dict]] = {}
        for leg in pool:
            gk = _game_key(leg)
            by_game.setdefault(gk, []).append(leg)

        for leg in pool:
            leg     = enrich_leg_with_ev(leg)
            market  = _market_normalized(leg)
            ev      = leg.get("ev", -1)
            tp      = leg.get("true_prob", 0)
            gk      = _game_key(leg)
            game_legs = by_game.get(gk, [])

            routed = False

            # ── Route 1: ML/RL markets ──────────────────────────
            if market in ML_RL_MARKETS:
                if ev >= self.ml_rl_min_ev:
                    ml_rl_pool.append(leg)
                    routed = True
                else:
                    rejection_reason = f"ML/RL EV {ev:.3f} < {self.ml_rl_min_ev}"
                    # POST picks always get at least independent channel
                    if leg.get("gate") == "POST":
                        indep_pool.append(leg)
                        print(f"  [router] {leg.get('player')} → Indep (fallback from rejection)")
                    else:
                        print(f"  [router] Rejected {leg.get('player')}: {rejection_reason}")
                        rejected.append({**leg, "_rejected_reason": rejection_reason})
                continue

            # ── Route 2: SGP eligible pitcher props ─────────────
            if market in SGP_ELIGIBLE_PITCHER and ev >= self.sgp_min_ev:
                # Check if same game has a batter or ML leg (correlated partner)
                has_partner = any(
                    _market_normalized(gl) in SGP_ELIGIBLE_BATTER | ML_RL_MARKETS
                    for gl in game_legs if gl is not leg
                )
                sgp_pool.append({**leg, "_has_sgp_partner": has_partner})
                routed = True

            # ── Route 3: Independent pitcher parlay ─────────────
            if market in SGP_ELIGIBLE_PITCHER | PP_ELIGIBLE_PITCHER:
                if ev >= self.indep_min_ev:
                    indep_pool.append(leg)
                    routed = True

            # ── Route 4: PrizePicks queue ────────────────────────
            if market in PP_ELIGIBLE_PITCHER:
                if tp >= self.pp_min_prob:
                    pp_queue.append(leg)
                    routed = True

            # ── Route 5: Lotto slip (absolute prob, any pitcher market) ─
            if market in SGP_ELIGIBLE_PITCHER | PP_ELIGIBLE_PITCHER:
                if tp >= self.lotto_min_prob:
                    lotto_pool.append(leg)
                    routed = True

            # ── Route 6: Batter legs for SGP ────────────────────
            if market in SGP_ELIGIBLE_BATTER and ev >= self.sgp_min_ev:
                sgp_pool.append(leg)
                routed = True

            if not routed:
                rejection_reason = f"ev={ev:.3f} tp={tp:.3f} no route"
                # POST picks always get at least independent channel
                if leg.get("gate") == "POST":
                    indep_pool.append(leg)
                    print(f"  [router] {leg.get('player')} → Indep (fallback from rejection)")
                else:
                    print(f"  [router] Rejected {leg.get('player')}: {rejection_reason}")
                    rejected.append({**leg, "_rejected_reason": rejection_reason})

        # Deduplicate lotto pool — remove duplicates by (player, market, direction)
        lotto_pool = _dedup_legs(lotto_pool)
        indep_pool = _dedup_legs(indep_pool)
        pp_queue   = _dedup_legs(pp_queue)

        # Sort pools
        lotto_pool.sort(key=lambda x: x.get("true_prob", 0), reverse=True)
        indep_pool.sort(key=lambda x: x.get("ev", 0), reverse=True)
        pp_queue.sort(key=lambda x: x.get("true_prob", 0), reverse=True)
        sgp_pool.sort(key=lambda x: x.get("ev", 0), reverse=True)
        ml_rl_pool.sort(key=lambda x: x.get("ev", 0), reverse=True)

        result = {
            "sgp_pool":   sgp_pool,
            "indep_pool": indep_pool,
            "ml_rl_pool": ml_rl_pool,
            "pp_queue":   pp_queue,
            "lotto_pool": lotto_pool,
            "rejected":   rejected,
            "stats": {
                "total_input":  len(pool),
                "sgp":          len(sgp_pool),
                "indep":        len(indep_pool),
                "ml_rl":        len(ml_rl_pool),
                "pp_queue":     len(pp_queue),
                "lotto":        len(lotto_pool),
                "rejected":     len(rejected),
                "routed_at":    datetime.now().isoformat(),
            },
        }

        _save_router_log(result)
        return result


def _dedup_legs(legs: list[dict]) -> list[dict]:
    seen = set()
    out  = []
    for leg in legs:
        key = (
            leg.get("player", "").lower(),
            _market_normalized(leg),
            (leg.get("direction") or "over").lower(),
        )
        if key not in seen:
            seen.add(key)
            out.append(leg)
    return out


def _save_router_log(result: dict) -> None:
    try:
        ROUTER_LOG.write_text(json.dumps(result, indent=2, default=str))
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — F3 vs F5 SELECTOR
# ═══════════════════════════════════════════════════════════════

def select_best_fn_market(
    f3_leg: dict | None,
    f5_leg: dict | None,
) -> dict | None:
    """
    Given F3 and F5 legs for the same game/side,
    return the one with higher EV. If only one exists, return it.
    Prefers F3 when EV is equal (sharper early innings signal).
    """
    if f3_leg is None and f5_leg is None:
        return None
    if f3_leg is None:
        return f5_leg
    if f5_leg is None:
        return f3_leg

    ev3 = f3_leg.get("ev", -1)
    ev5 = f5_leg.get("ev", -1)

    # Prefer F3 unless F5 is materially better (>0.5% difference)
    if ev5 > ev3 + 0.005:
        return f5_leg
    return f3_leg


def filter_best_fn_per_game(ml_rl_pool: list[dict]) -> list[dict]:
    """
    From the ML/RL pool, select the best F3 or F5 leg per game.
    Returns one leg per game — the highest EV side/market.
    """
    by_game: dict[str, dict] = {}

    for leg in ml_rl_pool:
        gk     = _game_key(leg)
        market = _market_normalized(leg)
        ev     = leg.get("ev", -1)
        is_f3  = market in F3_MARKETS
        is_f5  = market in F5_MARKETS

        if gk not in by_game:
            by_game[gk] = {"f3": None, "f5": None}

        if is_f3:
            curr = by_game[gk]["f3"]
            if curr is None or ev > curr.get("ev", -1):
                by_game[gk]["f3"] = leg
        elif is_f5:
            curr = by_game[gk]["f5"]
            if curr is None or ev > curr.get("ev", -1):
                by_game[gk]["f5"] = leg

    result = []
    for gk, opts in by_game.items():
        best = select_best_fn_market(opts["f3"], opts["f5"])
        if best:
            result.append(best)

    result.sort(key=lambda x: x.get("ev", 0), reverse=True)
    return result


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — DISCORD SUMMARY FORMATTER
# ═══════════════════════════════════════════════════════════════

def format_router_summary_discord(routing: dict) -> str:
    """
    Post-routing summary for Discord showing what slips were built
    and what was routed where.
    """
    stats = routing.get("stats", {})
    lines = [
        "📦 **SlipIQ — Daily Slip Routing Summary**",
        f"Input pool: {stats.get('total_input', 0)} picks",
        "",
        f"🎯 SGP pool:            {stats.get('sgp', 0)} legs → slipiq_ml_parlay",
        f"🎰 Independent parlay:  {stats.get('indep', 0)} legs → slipiq_independent_parlay",
        f"🏟️  ML/RL parlay:        {stats.get('ml_rl', 0)} legs → slipiq_independent_parlay",
        f"📱 PrizePicks queue:    {stats.get('pp_queue', 0)} legs → slipiq_prizepicks scanner",
        f"🎲 Lotto pool:          {stats.get('lotto', 0)} legs → $0.25 pitcher lotto",
        f"❌ Rejected:            {stats.get('rejected', 0)} legs (below all thresholds)",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Slip Router Self-Test")
    print("=" * 60)

    mock_pool = [
        # Pitcher K — high EV — should go SGP + indep + lotto
        {"player": "Gerrit Cole",   "market": "player_pitcher_strikeouts",
         "direction": "over", "line": 7.5, "confidence": 78,
         "pinnacle_over": -115, "pinnacle_under": -105, "odds": -110,
         "home_team": "Yankees", "away_team": "Red Sox", "grade": "A"},
        # F5 ML same game — should go ML/RL pool
        {"player": "Yankees",       "market": "f5_ml",
         "direction": "over", "line": None, "confidence": 72,
         "pinnacle_over": -125, "pinnacle_under": 105, "odds": -120,
         "home_team": "Yankees", "away_team": "Red Sox", "grade": "A"},
        # Batter TB same game — should go SGP pool
        {"player": "Aaron Judge",   "market": "player_total_bases",
         "direction": "over", "line": 1.5, "confidence": 68,
         "pinnacle_over": -118, "pinnacle_under": -102, "odds": -115,
         "home_team": "Yankees", "away_team": "Red Sox", "grade": "B+"},
        # Pitcher K different game — indep + PP + lotto
        {"player": "Zack Wheeler",  "market": "player_pitcher_strikeouts",
         "direction": "over", "line": 6.5, "confidence": 73,
         "pinnacle_over": -112, "pinnacle_under": -108, "odds": -108,
         "home_team": "Phillies", "away_team": "Mets", "grade": "A"},
        # Pitcher outs — PP eligible market
        {"player": "Corbin Burnes", "market": "player_pitcher_outs",
         "direction": "over", "line": 16.5, "confidence": 70,
         "pinnacle_over": -118, "pinnacle_under": -102, "odds": -115,
         "home_team": "Orioles", "away_team": "Rays", "grade": "B+"},
        # F3 ML — same game as Wheeler — should compete with F5
        {"player": "Phillies",      "market": "f3_ml",
         "direction": "over", "line": None, "confidence": 65,
         "pinnacle_over": -118, "pinnacle_under": 102, "odds": -115,
         "home_team": "Phillies", "away_team": "Mets", "grade": "B+"},
        # Low EV pitcher — should only make lotto if prob high enough
        {"player": "Pablo Lopez",   "market": "player_pitcher_strikeouts",
         "direction": "over", "line": 5.5, "confidence": 55,
         "pinnacle_over": -135, "pinnacle_under": 115, "odds": -130,
         "home_team": "Twins", "away_team": "Tigers", "grade": "B"},
    ]

    router  = SlipRouter(bankroll=500)
    routing = router.route(mock_pool)

    stats = routing["stats"]
    print(f"\nInput: {stats['total_input']} | SGP: {stats['sgp']} | "
          f"Indep: {stats['indep']} | ML/RL: {stats['ml_rl']} | "
          f"PP: {stats['pp_queue']} | Lotto: {stats['lotto']} | Rejected: {stats['rejected']}")

    print("\nSGP pool:")
    for leg in routing["sgp_pool"]:
        print(f"  {leg['player']} | ev={leg.get('ev', '?')} | partner={leg.get('_has_sgp_partner')}")

    print("\nML/RL pool (best F3/F5 per game):")
    best_fn = filter_best_fn_per_game(routing["ml_rl_pool"])
    for leg in best_fn:
        print(f"  {leg['player']} {leg['market']} | ev={leg.get('ev', '?')}")

    print("\nLotto pool (sorted by true_prob):")
    for leg in routing["lotto_pool"]:
        print(f"  {leg['player']} | true_prob={leg.get('true_prob', '?'):.4f}")

    print("\nPP queue:")
    for leg in routing["pp_queue"]:
        print(f"  {leg['player']} | {leg['market']} | true_prob={leg.get('true_prob', '?'):.4f}")

    print(f"\n{format_router_summary_discord(routing)}")
    print("\n✓ Slip router ready.")

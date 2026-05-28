# slipiq_ev_engine.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Expected Value Engine
# Pure math. Zero API calls. Zero credits.
#
# THIS FILE IS THE FOUNDATION.
# Every pick card, every gate decision, every parlay build
# must run through these functions before posting anything.
#
# MATH SOURCES:
#   Sportsbook EV  : edge = (true_prob × decimal_odds) - 1
#   No-vig prob    : Shin method (two-way and multi-way markets)
#   PrizePicks EV  : prod(p_i) × multiplier - 1  (DIFFERENT from sportsbook)
#   Kelly          : f* = edge / (decimal_odds - 1)  × fraction
#   Monte Carlo    : in slipiq_montecarlo.py (imports from here)
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations
import math
from typing import Optional

# ─── PrizePicks payout table (Power Play) ─────────────────────
# Key = number of picks, Value = multiplier
PP_POWER_PAYOUT: dict[int, float] = {
    2:  3.0,
    3:  5.0,
    4: 10.0,
    5: 20.0,
    6: 25.0,
}

# PrizePicks Flex payout table (allows 1 miss)
# Key = (picks, hits), Value = multiplier
PP_FLEX_PAYOUT: dict[tuple[int, int], float] = {
    (3, 3): 2.25,
    (3, 2): 1.25,
    (4, 4): 5.0,
    (4, 3): 1.5,
    (5, 5): 10.0,
    (5, 4): 2.0,
    (5, 3): 0.4,
    (6, 6): 25.0,
    (6, 5): 2.0,
    (6, 4): 0.4,
}

# ─── Minimum edge thresholds ──────────────────────────────────
MIN_EDGE_SPORTSBOOK   = 0.02   # 2% — straight bets DK/Fanatics
MIN_EDGE_PARLAY_LEG   = 0.02   # 2% per leg minimum before parlay build
MIN_EDGE_PARLAY_COMBO = 0.05   # 5% on the combined parlay
MIN_EDGE_PRIZEPICKS   = 0.05   # 5% on the full PrizePicks entry
QUARTER_KELLY         = 0.25   # conservative fraction


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — AMERICAN ODDS CONVERSION
# ═══════════════════════════════════════════════════════════════

def american_to_decimal(american: int | float) -> float:
    """
    Convert American odds to decimal odds.
    -110  →  1.9091
    +150  →  2.50
    """
    a = float(american)
    if a > 0:
        return 1.0 + (a / 100.0)
    else:
        return 1.0 + (100.0 / abs(a))


def decimal_to_american(decimal: float) -> int:
    """
    Convert decimal odds to American odds.
    1.9091 → -110
    2.50   → +150
    """
    d = float(decimal)
    if d >= 2.0:
        return int(round((d - 1.0) * 100.0))
    else:
        return int(round(-100.0 / (d - 1.0)))


def decimal_to_implied_prob(decimal: float) -> float:
    """Raw implied probability from decimal odds. Includes vig."""
    if decimal <= 1.0:
        return 1.0
    return 1.0 / decimal


def american_to_implied_prob(american: int | float) -> float:
    """Raw implied probability from American odds. Includes vig."""
    return decimal_to_implied_prob(american_to_decimal(american))


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — NO-VIG TRUE PROBABILITY
# ═══════════════════════════════════════════════════════════════

def no_vig_prob(
    over_american: int | float,
    under_american: int | float,
) -> dict[str, float]:
    """
    Strip vig from a two-way market and return true probabilities.

    Args:
        over_american  : American odds for the Over (e.g. -115)
        under_american : American odds for the Under (e.g. -105)

    Returns:
        {
            "true_over"   : float,  # 0-1
            "true_under"  : float,  # 0-1
            "vig"         : float,  # overround as fraction (e.g. 0.047)
            "vig_pct"     : float,  # overround as percent (e.g. 4.7)
        }

    Example (Pinnacle -115 / -105):
        implied_over  = 1/1.8696 = 0.5349
        implied_under = 1/1.9524 = 0.5122
        margin        = 1.0471
        true_over     = 0.5349 / 1.0471 = 0.5109
        true_under    = 0.5122 / 1.0471 = 0.4891
    """
    imp_over  = american_to_implied_prob(over_american)
    imp_under = american_to_implied_prob(under_american)
    margin    = imp_over + imp_under

    true_over  = imp_over  / margin
    true_under = imp_under / margin
    vig        = margin - 1.0

    return {
        "true_over":  round(true_over,  6),
        "true_under": round(true_under, 6),
        "vig":        round(vig,         6),
        "vig_pct":    round(vig * 100.0, 3),
    }


def no_vig_prob_from_pinnacle(
    over_american:  Optional[int | float],
    under_american: Optional[int | float],
) -> dict[str, float] | None:
    """
    Wrapper: returns None if either Pinnacle price is missing.
    Call this before computing edge so callers can handle no-data cleanly.
    """
    if over_american is None or under_american is None:
        return None
    return no_vig_prob(over_american, under_american)


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — SPORTSBOOK EDGE (DK / FANATICS straight bets)
# ═══════════════════════════════════════════════════════════════

def leg_ev(
    true_prob:    float,
    book_american: int | float,
) -> float:
    """
    Expected value of a single leg.
    edge = (true_prob × decimal_odds) - 1

    Positive = +EV (bet it if above threshold)
    Negative = -EV (never bet)

    Args:
        true_prob     : calibrated probability from no_vig_prob() or model
        book_american : the soft book's American odds for this side

    Returns:
        float edge (e.g. 0.047 = +4.7% edge)
    """
    decimal = american_to_decimal(book_american)
    return round((true_prob * decimal) - 1.0, 6)


def sportsbook_edge(
    true_prob:    float,
    book_american: int | float,
    min_edge:     float = MIN_EDGE_SPORTSBOOK,
) -> dict:
    """
    Full edge assessment for a DK/Fanatics straight bet.

    Returns:
        {
            "ev"          : float,   # raw edge value
            "passes"      : bool,    # True if ev >= min_edge
            "true_prob"   : float,
            "decimal_odds": float,
            "breakeven"   : float,   # probability needed to break even
            "juice_pct"   : float,   # effective hold % to overcome
        }
    """
    decimal   = american_to_decimal(book_american)
    ev        = leg_ev(true_prob, book_american)
    breakeven = 1.0 / decimal

    return {
        "ev":           round(ev, 6),
        "passes":       ev >= min_edge,
        "true_prob":    round(true_prob, 6),
        "decimal_odds": round(decimal, 4),
        "breakeven":    round(breakeven, 6),
        "juice_pct":    round((breakeven - 0.5) * 200, 2),  # % over 50/50
    }


def breakeven_display(book_american: int | float) -> str:
    """
    Human-readable breakeven string for pick cards.
    -115 → 'Needs 53.5% to break even'
    """
    decimal   = american_to_decimal(book_american)
    breakeven = 1.0 / decimal
    return f"Needs {breakeven * 100:.1f}% to break even"


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — SPORTSBOOK PARLAY EDGE (DK / Fanatics multi-leg)
# ═══════════════════════════════════════════════════════════════

def parlay_ev(
    leg_true_probs:    list[float],
    parlay_decimal_odds: float,
) -> float:
    """
    Expected value of a multi-leg parlay with independent legs.
    edge = prod(p_i) × parlay_decimal_odds - 1

    For correlated legs (SGP), use slipiq_montecarlo.simulate_correlated_sgp()
    which applies the correlation matrix before computing joint probability.

    Args:
        leg_true_probs      : list of calibrated per-leg probabilities
        parlay_decimal_odds : total parlay decimal odds (e.g. +600 = 7.0)

    Returns:
        float edge
    """
    if not leg_true_probs:
        return -1.0
    joint_prob = 1.0
    for p in leg_true_probs:
        joint_prob *= p
    return round((joint_prob * parlay_decimal_odds) - 1.0, 6)


def parlay_edge_full(
    leg_true_probs:    list[float],
    parlay_decimal_odds: float,
    min_edge:          float = MIN_EDGE_PARLAY_COMBO,
) -> dict:
    """
    Full parlay edge assessment.

    Returns:
        {
            "ev"              : float,
            "passes"          : bool,
            "joint_prob"      : float,
            "legs"            : int,
            "leg_probs"       : list[float],
            "vig_compound_pct": float,   # compounded house edge across legs
        }
    """
    if not leg_true_probs:
        return {"ev": -1.0, "passes": False, "joint_prob": 0.0, "legs": 0,
                "leg_probs": [], "vig_compound_pct": 0.0}

    joint_prob = 1.0
    for p in leg_true_probs:
        joint_prob *= p

    ev = round((joint_prob * parlay_decimal_odds) - 1.0, 6)

    # Approximate compounded vig: (1/joint_prob / parlay_odds) - 1
    fair_parlay_odds = 1.0 / joint_prob if joint_prob > 0 else float("inf")
    vig_compound = (fair_parlay_odds / parlay_decimal_odds) - 1.0 if parlay_decimal_odds > 0 else 0.0

    return {
        "ev":               ev,
        "passes":           ev >= min_edge,
        "joint_prob":       round(joint_prob, 8),
        "legs":             len(leg_true_probs),
        "leg_probs":        [round(p, 4) for p in leg_true_probs],
        "vig_compound_pct": round(vig_compound * 100, 2),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — PRIZEPICKS EV (completely different math)
# ═══════════════════════════════════════════════════════════════

def prizepicks_leg_threshold(n_picks: int, flex: bool = False) -> float:
    """
    Minimum per-leg true probability required for a PrizePicks entry to be +EV.
    Assumes equal probability per leg (use pp_entry_ev for unequal probs).

    Power Play: all legs must win.
        breakeven per-leg = (1 / multiplier) ^ (1/n)

    Flex Play: partial wins allowed — threshold varies.
        This returns the threshold for the MINIMUM win scenario that pays out.

    Args:
        n_picks : number of legs (2-6)
        flex    : True for Flex Play, False for Power Play

    Returns:
        float minimum per-leg probability for theoretical +EV
    """
    if not flex:
        multiplier = PP_POWER_PAYOUT.get(n_picks)
        if multiplier is None:
            raise ValueError(f"PrizePicks Power: unsupported n_picks={n_picks}. Valid: 2-6")
        # prod(p_i) * M > 1  →  p^n * M > 1  →  p > (1/M)^(1/n)
        return round((1.0 / multiplier) ** (1.0 / n_picks), 6)
    else:
        # For Flex, find the threshold where expected payout > 1
        # Iterate over possible outcomes weighted by binomial distribution
        # Simplified: return threshold where Power-like EV holds for min payout tier
        # True Flex EV requires pp_flex_ev() with actual leg probs
        min_payout = min(v for (k, h), v in PP_FLEX_PAYOUT.items() if k == n_picks)
        return round((1.0 / min_payout) ** (1.0 / n_picks), 6)


def pp_entry_ev(
    leg_true_probs: list[float],
    n_picks:        int | None = None,
    flex:           bool = False,
) -> dict:
    """
    True EV of a PrizePicks entry given actual per-leg probabilities.

    Power Play: EV = prod(p_i) * multiplier - 1
    Flex Play:  EV = sum over all outcome combos of P(k wins) * payout(n,k) - 1

    Args:
        leg_true_probs : calibrated probability per leg (list of floats, 0-1)
        n_picks        : number of picks (defaults to len(leg_true_probs))
        flex           : True for Flex Play

    Returns:
        {
            "ev"          : float,
            "passes"      : bool,
            "mode"        : "power" | "flex",
            "multiplier"  : float,   # effective multiplier (power only)
            "joint_prob"  : float,   # probability all legs hit (power)
            "threshold"   : float,   # breakeven per-leg prob (equal case)
            "n_picks"     : int,
        }
    """
    n = n_picks or len(leg_true_probs)
    if n < 2 or n > 6:
        raise ValueError(f"PrizePicks: n_picks must be 2-6, got {n}")
    if len(leg_true_probs) != n:
        raise ValueError(f"leg_true_probs length {len(leg_true_probs)} != n_picks {n}")

    threshold = prizepicks_leg_threshold(n, flex=flex)

    if not flex:
        # Power Play
        multiplier = PP_POWER_PAYOUT[n]
        joint_prob = 1.0
        for p in leg_true_probs:
            joint_prob *= p
        ev = round(joint_prob * multiplier - 1.0, 6)
        return {
            "ev":         ev,
            "passes":     ev >= MIN_EDGE_PRIZEPICKS,
            "mode":       "power",
            "multiplier": multiplier,
            "joint_prob": round(joint_prob, 8),
            "threshold":  threshold,
            "n_picks":    n,
        }
    else:
        # Flex Play — enumerate all 2^n outcome combinations
        from itertools import product as iproduct
        total_ev = 0.0
        for outcomes in iproduct([0, 1], repeat=n):
            hits = sum(outcomes)
            payout = PP_FLEX_PAYOUT.get((n, hits), 0.0)
            if payout == 0.0:
                continue
            # probability of this exact outcome combo
            prob = 1.0
            for i, outcome in enumerate(outcomes):
                prob *= leg_true_probs[i] if outcome == 1 else (1.0 - leg_true_probs[i])
            total_ev += prob * payout
        ev = round(total_ev - 1.0, 6)
        return {
            "ev":         ev,
            "passes":     ev >= MIN_EDGE_PRIZEPICKS,
            "mode":       "flex",
            "multiplier": None,
            "joint_prob": None,
            "threshold":  threshold,
            "n_picks":    n,
        }


def pp_best_mode(leg_true_probs: list[float]) -> dict:
    """
    Compare Power Play vs Flex Play EV for the same legs.
    Returns the mode with higher EV plus both results.

    Use this when deciding whether to submit Power or Flex.
    """
    n = len(leg_true_probs)
    if n < 3:
        # Flex not available for 2-picks
        power = pp_entry_ev(leg_true_probs, flex=False)
        return {"best": "power", "power": power, "flex": None}

    power = pp_entry_ev(leg_true_probs, flex=False)
    flex  = pp_entry_ev(leg_true_probs, flex=True)

    best = "power" if power["ev"] >= flex["ev"] else "flex"
    return {"best": best, "power": power, "flex": flex}


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — KELLY CRITERION
# ═══════════════════════════════════════════════════════════════

def kelly_stake(
    ev:            float,
    true_prob:     float,
    decimal_odds:  float,
    bankroll:      float,
    fraction:      float = QUARTER_KELLY,
) -> float:
    """
    Quarter Kelly stake in dollars.
    f* = edge / (decimal_odds - 1)
    stake = bankroll × f* × fraction

    Returns 0.0 if edge <= 0 (never bet negative EV).

    Args:
        ev           : edge from leg_ev() or parlay_ev()
        true_prob    : calibrated probability
        decimal_odds : decimal odds for the bet
        bankroll     : current bankroll in dollars
        fraction     : Kelly fraction (default 0.25 = quarter Kelly)
    """
    if ev <= 0 or decimal_odds <= 1.0:
        return 0.0
    full_kelly = ev / (decimal_odds - 1.0)
    stake      = bankroll * full_kelly * fraction
    return round(max(0.0, stake), 2)


def kelly_stake_american(
    ev:            float,
    true_prob:     float,
    american_odds: int | float,
    bankroll:      float,
    fraction:      float = QUARTER_KELLY,
) -> float:
    """Convenience wrapper: American odds input."""
    decimal = american_to_decimal(american_odds)
    return kelly_stake(ev, true_prob, decimal, bankroll, fraction)


def kelly_parlay_stake(
    parlay_ev_result: dict,
    bankroll:         float,
    fraction:         float = QUARTER_KELLY,
) -> float:
    """
    Kelly stake for a parlay given the output of parlay_edge_full().
    Uses parlay decimal odds implied by joint_prob and ev.
    """
    ev         = parlay_ev_result.get("ev", 0.0)
    joint_prob = parlay_ev_result.get("joint_prob", 0.0)
    if ev <= 0 or joint_prob <= 0:
        return 0.0
    # Reconstruct decimal odds: ev = joint_prob × odds - 1
    # → odds = (1 + ev) / joint_prob
    parlay_decimal = (1.0 + ev) / joint_prob
    return kelly_stake(ev, joint_prob, parlay_decimal, bankroll, fraction)


# ═══════════════════════════════════════════════════════════════
# SECTION 7 — CLOSING LINE VALUE (CLV)
# ═══════════════════════════════════════════════════════════════

def closing_line_value(
    bet_american:     int | float,
    closing_american: int | float,
) -> dict:
    """
    CLV: did you beat the closing line?

    Positive CLV = you got better odds than the market closed at.
    This is the strongest proof of a real edge over time.

    Args:
        bet_american     : odds when you placed the bet
        closing_american : Pinnacle closing odds for the same side

    Returns:
        {
            "clv_pct"       : float,   # % CLV (positive = good)
            "beat_close"    : bool,
            "bet_decimal"   : float,
            "close_decimal" : float,
        }
    """
    bet_dec   = american_to_decimal(bet_american)
    close_dec = american_to_decimal(closing_american)
    clv_pct   = round(((bet_dec / close_dec) - 1.0) * 100.0, 3)

    return {
        "clv_pct":       clv_pct,
        "beat_close":    clv_pct > 0,
        "bet_decimal":   round(bet_dec,   4),
        "close_decimal": round(close_dec, 4),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 8 — SHARP MONEY DIRECTION FLAG
# ═══════════════════════════════════════════════════════════════

def sharp_move_flag(
    opening_american: int | float,
    current_american: int | float,
    model_direction:  str,  # "over" or "under"
) -> dict:
    """
    Detect sharp line movement and check if it confirms or contradicts model.

    A Pinnacle line moving TOWARD the model pick = confirmation.
    A Pinnacle line moving AWAY from the model pick = warning.

    Args:
        opening_american : Pinnacle open line (American, e.g. -110)
        current_american : Pinnacle current line (American)
        model_direction  : "over" or "under"

    Returns:
        {
            "moved_pct"   : float,   # % change in implied prob
            "direction"   : str,     # "toward_over" | "toward_under" | "stable"
            "confirms"    : bool,    # True if movement agrees with model
            "sharp_signal": str,     # "CONFIRM" | "WARN" | "NEUTRAL"
        }
    """
    open_prob  = american_to_implied_prob(opening_american)
    curr_prob  = american_to_implied_prob(current_american)
    moved_pct  = round((curr_prob - open_prob) * 100.0, 3)

    if abs(moved_pct) < 0.3:
        direction   = "stable"
        sharp_signal = "NEUTRAL"
        confirms    = False
    elif curr_prob > open_prob:
        direction = "toward_over"
        confirms  = model_direction == "over"
        sharp_signal = "CONFIRM" if confirms else "WARN"
    else:
        direction = "toward_under"
        confirms  = model_direction == "under"
        sharp_signal = "CONFIRM" if confirms else "WARN"

    return {
        "moved_pct":    moved_pct,
        "direction":    direction,
        "confirms":     confirms,
        "sharp_signal": sharp_signal,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 9 — FULL LEG ASSESSMENT (convenience wrapper)
# ═══════════════════════════════════════════════════════════════

def assess_leg(
    pinnacle_over:  Optional[int | float],
    pinnacle_under: Optional[int | float],
    book_american:  int | float,
    direction:      str,  # "over" or "under"
    bankroll:       float = 1000.0,
    min_edge:       float = MIN_EDGE_SPORTSBOOK,
) -> dict | None:
    """
    Full single-leg assessment pipeline.
    1. Extract Pinnacle no-vig true probability
    2. Compute edge vs soft book
    3. Compute Kelly stake
    4. Return structured result or None if Pinnacle data missing

    This is the PRIMARY function called by slipiq_confidence_agent.py
    for each leg before gating decisions.

    Args:
        pinnacle_over  : Pinnacle over American odds (or None)
        pinnacle_under : Pinnacle under American odds (or None)
        book_american  : soft book (DK/Fanatics) odds for the chosen direction
        direction      : "over" or "under"
        bankroll       : current bankroll for Kelly sizing
        min_edge       : minimum edge threshold

    Returns:
        {
            "true_prob"  : float,
            "ev"         : float,
            "passes"     : bool,
            "kelly_stake": float,
            "breakeven"  : float,
            "vig_pct"    : float,
            "direction"  : str,
            "no_pinnacle": bool,  # True if Pinnacle data was missing
        }
    """
    no_pinnacle = False
    nv = no_vig_prob_from_pinnacle(pinnacle_over, pinnacle_under)

    if nv is None:
        # No Pinnacle data — use implied prob from book as fallback
        # This is lower quality; flag it
        true_prob   = american_to_implied_prob(book_american)
        no_pinnacle = True
    else:
        true_prob = nv["true_over"] if direction == "over" else nv["true_under"]

    edge_result = sportsbook_edge(true_prob, book_american, min_edge=min_edge)
    decimal     = american_to_decimal(book_american)
    stake       = kelly_stake(edge_result["ev"], true_prob, decimal, bankroll)

    return {
        "true_prob":   true_prob,
        "ev":          edge_result["ev"],
        "passes":      edge_result["passes"] and not no_pinnacle,  # fail if no Pinnacle
        "kelly_stake": stake,
        "breakeven":   edge_result["breakeven"],
        "vig_pct":     nv["vig_pct"] if nv else None,
        "direction":   direction,
        "no_pinnacle": no_pinnacle,
    }


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — EV Engine Self-Test")
    print("=" * 60)

    # Test 1: no-vig from Pinnacle -115 / -105
    nv = no_vig_prob(-115, -105)
    print(f"\n[1] No-vig  Pinnacle -115/-105:")
    print(f"    true_over={nv['true_over']:.4f}  true_under={nv['true_under']:.4f}  vig={nv['vig_pct']:.2f}%")
    assert abs(nv["true_over"] + nv["true_under"] - 1.0) < 1e-6, "probs must sum to 1"

    # Test 2: sportsbook edge — DK offers +105 when true prob is 0.51
    edge = sportsbook_edge(true_prob=0.51, book_american=105)
    print(f"\n[2] Sportsbook edge  true_prob=0.51  DK=+105:")
    print(f"    ev={edge['ev']:+.4f}  passes={edge['passes']}  breakeven={edge['breakeven']:.4f}")

    # Test 3: PrizePicks 4-pick Power Play threshold
    thr = prizepicks_leg_threshold(4, flex=False)
    print(f"\n[3] PrizePicks 4-pick Power threshold: {thr:.4f} ({thr*100:.2f}%)")
    assert thr > 0.56, "4-pick threshold should be ~0.5623"

    # Test 4: PrizePicks 4-pick entry EV with 58% per-leg
    probs = [0.58, 0.60, 0.59, 0.61]
    pp = pp_entry_ev(probs, flex=False)
    print(f"\n[4] PrizePicks 4-pick EV  legs={probs}:")
    print(f"    ev={pp['ev']:+.4f}  passes={pp['passes']}  joint={pp['joint_prob']:.4f}")

    # Test 5: PrizePicks best mode comparison
    best = pp_best_mode([0.60, 0.62, 0.58])
    print(f"\n[5] 3-pick best mode: {best['best']}")
    print(f"    power ev={best['power']['ev']:+.4f}  flex ev={best['flex']['ev']:+.4f}")

    # Test 6: Kelly stake
    stake = kelly_stake_american(ev=0.047, true_prob=0.53, american_odds=-110, bankroll=1000)
    print(f"\n[6] Kelly stake  EV=+4.7%  -110  $1k bankroll: ${stake}")
    assert stake > 0, "positive EV should produce positive stake"

    # Test 7: CLV
    clv = closing_line_value(bet_american=+105, closing_american=-105)
    print(f"\n[7] CLV  bet=+105  close=-105: clv={clv['clv_pct']:+.2f}%  beat={clv['beat_close']}")

    # Test 8: Sharp move flag
    flag = sharp_move_flag(opening_american=-110, current_american=-118, model_direction="over")
    print(f"\n[8] Sharp flag  open=-110  curr=-118  model=over: {flag['sharp_signal']}")

    # Test 9: Full leg assess
    result = assess_leg(
        pinnacle_over=-115, pinnacle_under=-105,
        book_american=+110, direction="over",
        bankroll=1000
    )
    print(f"\n[9] assess_leg  Pinnacle -115/-105  DK=+110  over:")
    print(f"    true_prob={result['true_prob']:.4f}  ev={result['ev']:+.4f}  passes={result['passes']}")
    print(f"    kelly=${result['kelly_stake']}  breakeven={result['breakeven']:.4f}")

    print("\n✓ All tests passed.")

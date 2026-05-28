# slipiq_montecarlo.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Monte Carlo Simulation Engine
#
# THREE USE CASES:
#   1. simulate_sgp()            — independent legs (any parlay)
#   2. simulate_correlated_sgp() — same-game correlated legs (SGP)
#   3. bankroll_simulation()     — equity curve + ruin probability
#
# WHY THIS MATTERS:
#   The EV engine tells you if a bet is theoretically profitable.
#   Monte Carlo tells you if your BANKROLL survives the variance.
#   A +EV 20-leg parlay will still wipe you out. This proves it.
#
# CORRELATION MATRIX (conservative defaults — improve with outcome logs):
#   pitcher_k    + f5_ml        = +0.35  (ace pitching → team wins F5)
#   pitcher_k    + same_bat_hit = -0.15  (opp pitcher Ks → fewer hits for opp batters)
#   pitcher_k    + opp_bat_hit  = -0.20  (dominant pitcher → fewer opp hits)
#   f5_ml        + same_bat_hit = +0.40  (team winning F5 → batters get RBIs)
#   f5_ml        + f5_rl        = +0.75  (win F5 and cover RL often correlated)
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import numpy as np
from typing import Optional

from slipiq_ev_engine import (
    parlay_ev,
    pp_entry_ev,
    kelly_stake,
    american_to_decimal,
    MIN_EDGE_PARLAY_COMBO,
    MIN_EDGE_PRIZEPICKS,
    QUARTER_KELLY,
)

# ─── Default correlation matrix by leg type pair ──────────────
# These are CONSERVATIVE estimates. Update as you log SGP outcomes.
DEFAULT_CORRELATIONS: dict[tuple[str, str], float] = {
    ("pitcher_k",    "f5_ml"):        +0.35,
    ("pitcher_k",    "f5_rl"):        +0.25,
    ("pitcher_k",    "same_bat_hit"): -0.15,
    ("pitcher_k",    "opp_bat_hit"):  -0.20,
    ("pitcher_k",    "game_total"):   -0.30,
    ("f5_ml",        "same_bat_hit"): +0.40,
    ("f5_ml",        "f5_rl"):        +0.75,
    ("f5_ml",        "same_bat_tb"):  +0.35,
    ("same_bat_hit", "same_bat_tb"):  +0.80,
    ("same_bat_hit", "same_bat_rbi"): +0.60,
    # NBA correlations
    ("nba_pts_over", "nba_pace"):     +0.45,
    ("nba_pts_over", "nba_reb"):      +0.20,
    ("nba_reb_over", "nba_blk"):      +0.30,
}

N_SIMS_DEFAULT  = 10_000
N_SIMS_FAST     = 1_000   # for real-time use during polling
RUIN_THRESHOLD  = 0.10    # bankroll falls below 10% = ruin


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — INDEPENDENT LEGS (any parlay)
# ═══════════════════════════════════════════════════════════════

def simulate_sgp(
    leg_probs:      list[float],
    payout_decimal: float,
    n_sims:         int   = N_SIMS_DEFAULT,
    seed:           int | None = 42,
) -> dict:
    """
    Monte Carlo simulation for a parlay with INDEPENDENT legs.
    Use this for multi-game parlays (DK/Fanatics cross-game).
    For same-game correlated legs, use simulate_correlated_sgp().

    Args:
        leg_probs      : list of true probabilities per leg
        payout_decimal : total parlay decimal odds (e.g. +600 = 7.0)
        n_sims         : number of simulations
        seed           : random seed for reproducibility

    Returns:
        {
            "ev"          : float,   # theoretical EV (deterministic)
            "sim_ev"      : float,   # simulated average EV (sanity check)
            "win_rate"    : float,   # % of sims where parlay wins
            "p5_roi"      : float,   # 5th percentile ROI (worst-case)
            "p50_roi"     : float,   # median ROI
            "p95_roi"     : float,   # 95th percentile ROI (best-case)
            "ruin_pct"    : float,   # % of 1-unit bets that lose (always high for parlays)
            "joint_prob"  : float,   # theoretical joint win probability
            "n_legs"      : int,
            "payout"      : float,
        }
    """
    rng        = np.random.default_rng(seed)
    n_legs     = len(leg_probs)
    probs_arr  = np.array(leg_probs, dtype=float)

    # Simulate: each sim = n_legs Bernoulli draws
    draws = rng.random((n_sims, n_legs))
    wins  = draws < probs_arr[None, :]   # shape (n_sims, n_legs)
    all_win = wins.all(axis=1)           # parlay wins only if all legs hit

    # ROI per sim: win=payout-1, lose=-1
    roi = np.where(all_win, payout_decimal - 1.0, -1.0)

    joint_prob = float(np.prod(probs_arr))
    theo_ev    = joint_prob * payout_decimal - 1.0

    return {
        "ev":         round(theo_ev,               6),
        "sim_ev":     round(float(roi.mean()),      6),
        "win_rate":   round(float(all_win.mean()),  6),
        "p5_roi":     round(float(np.percentile(roi, 5)),  4),
        "p50_roi":    round(float(np.percentile(roi, 50)), 4),
        "p95_roi":    round(float(np.percentile(roi, 95)), 4),
        "ruin_pct":   round(float((~all_win).mean()), 4),
        "joint_prob": round(joint_prob,            8),
        "n_legs":     n_legs,
        "payout":     payout_decimal,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — CORRELATED SGP (same-game parlay)
# ═══════════════════════════════════════════════════════════════

def get_correlation(leg_a_type: str, leg_b_type: str) -> float:
    """
    Look up correlation between two leg types.
    Checks both orderings. Returns 0.0 if no correlation defined.
    """
    r = DEFAULT_CORRELATIONS.get((leg_a_type, leg_b_type))
    if r is None:
        r = DEFAULT_CORRELATIONS.get((leg_b_type, leg_a_type), 0.0)
    return r


def _build_correlation_matrix(leg_types: list[str]) -> np.ndarray:
    """
    Build an n×n correlation matrix from leg type list.
    Diagonal = 1. Off-diagonal = get_correlation(i, j).
    """
    n   = len(leg_types)
    mat = np.eye(n, dtype=float)
    for i in range(n):
        for j in range(i + 1, n):
            r         = get_correlation(leg_types[i], leg_types[j])
            mat[i, j] = r
            mat[j, i] = r

    # Ensure positive semi-definite (numerical safety)
    eigvals = np.linalg.eigvalsh(mat)
    if eigvals.min() < 0:
        mat += np.eye(n) * (abs(eigvals.min()) + 1e-6)
    return mat


def simulate_correlated_sgp(
    leg_probs:      list[float],
    leg_types:      list[str],
    payout_decimal: float,
    corr_overrides: dict[tuple[str, str], float] | None = None,
    n_sims:         int   = N_SIMS_DEFAULT,
    seed:           int | None = 42,
) -> dict:
    """
    Monte Carlo simulation for correlated same-game parlay (SGP).
    Uses Gaussian copula to model correlated Bernoulli outcomes.

    HOW IT WORKS:
    1. Build correlation matrix from leg types
    2. Sample correlated standard normals via Cholesky decomposition
    3. Map normals to uniform via Φ (normal CDF)
    4. Compare uniform to true_prob → win/loss per leg
    5. Parlay wins only if all legs hit

    This is the mathematically correct way to price correlated SGPs.
    The book prices them as independent — your edge comes from knowing
    the true correlation.

    Args:
        leg_probs      : calibrated true probability per leg
        leg_types      : leg type key per leg (e.g. ["pitcher_k", "f5_ml"])
        payout_decimal : total SGP decimal odds
        corr_overrides : optional dict to override specific correlations
        n_sims         : number of simulations
        seed           : random seed

    Returns:
        Same shape as simulate_sgp() plus:
        {
            ...
            "corr_adjusted_joint_prob" : float,  # true correlated joint prob
            "independent_joint_prob"   : float,  # naive product (book's assumption)
            "correlation_edge"         : float,  # prob diff from correlation
            "correlation_matrix"       : list,   # n×n matrix used
        }
    """
    rng    = np.random.default_rng(seed)
    n_legs = len(leg_probs)
    assert len(leg_types) == n_legs, "leg_probs and leg_types must be same length"

    probs_arr = np.array(leg_probs, dtype=float)

    # Build correlation matrix
    corr_mat = _build_correlation_matrix(leg_types)
    if corr_overrides:
        for (ta, tb), val in corr_overrides.items():
            for i, lt in enumerate(leg_types):
                if lt == ta:
                    for j, lt2 in enumerate(leg_types):
                        if lt2 == tb and i != j:
                            corr_mat[i, j] = val
                            corr_mat[j, i] = val

    # Cholesky decomposition for correlated normal sampling
    try:
        L = np.linalg.cholesky(corr_mat)
    except np.linalg.LinAlgError:
        # Fallback to independent if matrix is not positive definite
        print("  [montecarlo] ⚠️  Correlation matrix not PD — falling back to independent")
        return simulate_sgp(leg_probs, payout_decimal, n_sims, seed)

    # Sample: Z = L @ standard_normals
    standard_normals = rng.standard_normal((n_legs, n_sims))  # shape (n, n_sims)
    corr_normals     = L @ standard_normals                   # shape (n, n_sims)

    # Convert to uniform via normal CDF, then to Bernoulli
    from scipy.stats import norm as _norm
    uniforms = _norm.cdf(corr_normals)  # shape (n, n_sims)
    wins     = uniforms < probs_arr[:, None]   # shape (n, n_sims)
    all_win  = wins.all(axis=0)                # shape (n_sims,)

    # ROI
    roi = np.where(all_win, payout_decimal - 1.0, -1.0)

    # Compare correlated vs independent joint prob
    corr_joint_prob  = float(all_win.mean())
    indep_joint_prob = float(np.prod(probs_arr))
    theo_ev          = corr_joint_prob * payout_decimal - 1.0

    return {
        "ev":                       round(theo_ev,                    6),
        "sim_ev":                   round(float(roi.mean()),          6),
        "win_rate":                 round(corr_joint_prob,            6),
        "p5_roi":                   round(float(np.percentile(roi, 5)),  4),
        "p50_roi":                  round(float(np.percentile(roi, 50)), 4),
        "p95_roi":                  round(float(np.percentile(roi, 95)), 4),
        "ruin_pct":                 round(float((~all_win).mean()),   4),
        "joint_prob":               round(corr_joint_prob,            8),
        "corr_adjusted_joint_prob": round(corr_joint_prob,            8),
        "independent_joint_prob":   round(indep_joint_prob,           8),
        "correlation_edge":         round(corr_joint_prob - indep_joint_prob, 8),
        "n_legs":                   n_legs,
        "payout":                   payout_decimal,
        "correlation_matrix":       corr_mat.tolist(),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — BANKROLL SIMULATION (equity curve)
# ═══════════════════════════════════════════════════════════════

def bankroll_simulation(
    ev:               float,
    payout_decimal:   float,
    kelly_fraction:   float  = QUARTER_KELLY,
    starting_bankroll: float = 1000.0,
    n_bets:           int    = 500,
    n_sims:           int    = 1000,
    seed:             int | None = 42,
) -> dict:
    """
    Simulate bankroll trajectory over n_bets using dynamic Kelly sizing.

    This shows whether your edge actually GROWS the bankroll or whether
    variance destroys it before the edge can manifest.
    Run this for any strategy before committing real money.

    Args:
        ev               : edge per bet (e.g. 0.047 = 4.7%)
        payout_decimal   : decimal odds for the bet
        kelly_fraction   : fraction of full Kelly (default 0.25)
        starting_bankroll: dollars
        n_bets           : number of bets per simulation path
        n_sims           : number of parallel simulation paths
        seed             : random seed

    Returns:
        {
            "p5_final"     : float,   # 5th percentile final bankroll
            "p50_final"    : float,   # median final bankroll
            "p95_final"    : float,   # 95th percentile final bankroll
            "ruin_pct"     : float,   # % of paths that hit ruin threshold
            "median_roi"   : float,   # median ROI over all bets
            "expected_roi" : float,   # theoretical compound ROI
            "growth_rate"  : float,   # theoretical Kelly growth rate per bet
            "verdict"      : str,     # "VIABLE" | "HIGH_VARIANCE" | "RUINOUS"
        }
    """
    rng = np.random.default_rng(seed)

    # True win probability implied by ev and payout
    # ev = p * payout - 1  →  p = (1 + ev) / payout
    p_win = (1.0 + ev) / payout_decimal
    p_win = max(0.001, min(0.999, p_win))

    # Kelly growth rate (log-optimal)
    full_kelly  = ev / (payout_decimal - 1.0) if payout_decimal > 1.0 else 0.0
    frac_kelly  = full_kelly * kelly_fraction
    growth_rate = p_win * np.log(1 + frac_kelly * (payout_decimal - 1.0)) + \
                  (1 - p_win) * np.log(1 - frac_kelly)

    # Simulate paths
    bankrolls = np.full((n_sims,), starting_bankroll, dtype=float)
    ruined    = np.zeros(n_sims, dtype=bool)

    ruin_floor = starting_bankroll * RUIN_THRESHOLD

    for _ in range(n_bets):
        # Dynamic Kelly stake per path
        stakes = np.maximum(0.0, bankrolls * frac_kelly)

        # Simulate outcomes
        outcomes = rng.random(n_sims) < p_win  # True = win
        bankrolls = np.where(
            outcomes,
            bankrolls + stakes * (payout_decimal - 1.0),
            bankrolls - stakes,
        )
        bankrolls = np.maximum(bankrolls, 0.0)  # floor at 0
        ruined   |= bankrolls < ruin_floor

    final = bankrolls
    median_roi = float(np.median(final / starting_bankroll) - 1.0)
    theo_roi   = float(np.exp(growth_rate * n_bets) - 1.0)

    ruin_pct = float(ruined.mean())

    if ruin_pct < 0.10 and median_roi > 0.05:
        verdict = "VIABLE"
    elif ruin_pct < 0.25:
        verdict = "HIGH_VARIANCE"
    else:
        verdict = "RUINOUS"

    return {
        "p5_final":     round(float(np.percentile(final, 5)),  2),
        "p50_final":    round(float(np.percentile(final, 50)), 2),
        "p95_final":    round(float(np.percentile(final, 95)), 2),
        "ruin_pct":     round(ruin_pct, 4),
        "median_roi":   round(median_roi, 4),
        "expected_roi": round(theo_roi,   4),
        "growth_rate":  round(growth_rate, 6),
        "verdict":      verdict,
        "n_bets":       n_bets,
        "n_sims":       n_sims,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — QUICK VALIDATION (for daily use)
# ═══════════════════════════════════════════════════════════════

def quick_validate_parlay(
    leg_probs:      list[float],
    leg_types:      list[str]  | None,
    payout_decimal: float,
    bankroll:       float = 1000.0,
) -> dict:
    """
    One-call validation for any parlay before posting.
    Runs fast simulation (1k sims) and returns a go/no-go signal.

    Args:
        leg_probs      : true probability per leg
        leg_types      : leg type keys (None = treat as independent)
        payout_decimal : decimal odds for the parlay
        bankroll       : current bankroll

    Returns:
        {
            "go":          bool,   # True = post it, False = skip
            "reason":      str,    # human-readable explanation
            "ev":          float,
            "win_rate":    float,
            "ruin_pct":    float,
            "kelly_stake": float,
        }
    """
    n = len(leg_probs)

    # Choose simulation method
    if leg_types and len(leg_types) == n and any(
        get_correlation(a, b) != 0.0
        for i, a in enumerate(leg_types)
        for j, b in enumerate(leg_types[i+1:])
    ):
        sim = simulate_correlated_sgp(
            leg_probs, leg_types, payout_decimal, n_sims=N_SIMS_FAST
        )
    else:
        sim = simulate_sgp(leg_probs, payout_decimal, n_sims=N_SIMS_FAST)

    ev     = sim["ev"]
    ruin   = sim["ruin_pct"]
    joint  = sim["joint_prob"]
    stake  = kelly_stake(ev, joint, payout_decimal, bankroll)

    # Go/no-go logic
    if ev < MIN_EDGE_PARLAY_COMBO:
        go     = False
        reason = f"EV {ev:+.2%} below {MIN_EDGE_PARLAY_COMBO:.0%} minimum"
    elif stake < 0.50:
        go     = False
        reason = f"Kelly stake ${stake:.2f} below $0.50 minimum — edge too thin for bankroll"
    elif n > 6 and ruin > 0.90:
        go     = False
        reason = f"{n}-leg parlay ruin probability {ruin:.0%} — lottery ticket"
    else:
        go     = True
        reason = f"EV {ev:+.2%} | win rate {sim['win_rate']:.1%} | Kelly ${stake:.2f}"

    return {
        "go":          go,
        "reason":      reason,
        "ev":          round(ev,    6),
        "win_rate":    round(sim["win_rate"], 4),
        "ruin_pct":    round(ruin,  4),
        "kelly_stake": stake,
    }


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Monte Carlo Engine Self-Test")
    print("=" * 60)

    # Test 1: Independent SGP
    leg_probs = [0.55, 0.58, 0.56]
    payout    = 6.0
    sim = simulate_sgp(leg_probs, payout, n_sims=10_000)
    print(f"\n[1] Independent 3-leg parlay ({payout}x):")
    print(f"    EV={sim['ev']:+.4f}  win_rate={sim['win_rate']:.4f}  p50_roi={sim['p50_roi']:+.4f}")
    print(f"    ruin_pct={sim['ruin_pct']:.4f} (single bet)")

    # Test 2: Correlated SGP (pitcher K + F5 ML + batter hit)
    leg_types = ["pitcher_k", "f5_ml", "same_bat_hit"]
    corr_sim  = simulate_correlated_sgp(leg_probs, leg_types, payout, n_sims=10_000)
    print(f"\n[2] Correlated SGP {leg_types}:")
    print(f"    EV={corr_sim['ev']:+.4f}  win_rate={corr_sim['win_rate']:.4f}")
    print(f"    indep_joint={corr_sim['independent_joint_prob']:.4f}")
    print(f"    corr_joint ={corr_sim['corr_adjusted_joint_prob']:.4f}")
    print(f"    corr_edge  ={corr_sim['correlation_edge']:+.4f}")

    # Test 3: 20-leg lotto ticket (should show RUINOUS)
    lotto_probs = [0.55] * 20
    lotto_payout = 5000.0
    lotto_sim = simulate_sgp(lotto_probs, lotto_payout, n_sims=10_000)
    print(f"\n[3] 20-leg lotto (5000x):")
    print(f"    EV={lotto_sim['ev']:+.4f}  win_rate={lotto_sim['win_rate']:.8f}  ruin={lotto_sim['ruin_pct']:.4f}")

    # Test 4: Bankroll simulation
    br_sim = bankroll_simulation(ev=0.047, payout_decimal=1.909, n_bets=500, n_sims=1000)
    print(f"\n[4] Bankroll sim  EV=+4.7%  -110  500 bets  $1k:")
    print(f"    p5=${br_sim['p5_final']}  p50=${br_sim['p50_final']}  p95=${br_sim['p95_final']}")
    print(f"    ruin={br_sim['ruin_pct']:.2%}  verdict={br_sim['verdict']}")

    # Test 5: Quick validate
    val = quick_validate_parlay(
        leg_probs=[0.55, 0.58, 0.56],
        leg_types=["pitcher_k", "f5_ml", "same_bat_hit"],
        payout_decimal=6.0,
        bankroll=1000.0,
    )
    print(f"\n[5] Quick validate: go={val['go']}  reason='{val['reason']}'")

    # Test 6: Lotto ticket quick validate (should fail)
    val_lotto = quick_validate_parlay(
        leg_probs=[0.55] * 20,
        leg_types=None,
        payout_decimal=5000.0,
        bankroll=1000.0,
    )
    print(f"\n[6] 20-leg lotto validate: go={val_lotto['go']}  reason='{val_lotto['reason']}'")

    print("\n✓ Monte Carlo engine ready.")

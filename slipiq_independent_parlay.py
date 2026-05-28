# slipiq_independent_parlay.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Independent Parlay Builder
# Handles two distinct slip types that require independent legs:
#
# SLIP TYPE 1: PITCHER LOTTO ($0.25 stake)
#   7-12 independent pitcher props, one per game
#   DK/Fanatics — sorted by ABSOLUTE TRUE PROBABILITY (not EV)
#   Because: you're buying a lottery ticket. Max hit rate matters.
#   EV is secondary here — you want the legs most likely to hit.
#   Alt line logic: look for lower lines (easier clears) with >= 65% true_prob
#   Markets: K, Outs, Hits Allowed, Walks — anything with a line
#
# SLIP TYPE 2: INDEPENDENT ML/RL PARLAY ($1-5 stake)
#   3-8 independent F3 ML, F5 ML, F3 RL, F5 RL legs
#   One leg per game, best EV side selected by slipiq_slip_router
#   Standard +EV filter applies (edge > 0.02 per leg)
#
# QUEUE EXPIRATION:
#   Both slip types use a 30-minute force-fire trigger.
#   If the earliest leg in the queue is 30 minutes from locking,
#   the bot fires with whatever valid legs are available (auto-downsize).
#
# WHAT THIS IS NOT:
#   SGP correlated slips — that's slipiq_ml_parlay.py
#   PrizePicks entries — that's slipiq_prizepicks.py
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Optional

from slipiq_ev_engine import (
    parlay_edge_full,
    kelly_stake,
    american_to_decimal,
    american_to_implied_prob,
    no_vig_prob,
    MIN_EDGE_PARLAY_COMBO,
    QUARTER_KELLY,
)

CACHE_DIR      = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
LOTTO_LOG      = CACHE_DIR / "lotto_slip_log.json"
ML_RL_LOG      = CACHE_DIR / "mlrl_parlay_log.json"

# ─── Lotto slip config ────────────────────────────────────────
LOTTO_MIN_PROB       = 0.60    # minimum true_prob per lotto leg
LOTTO_ALT_MIN_PROB   = 0.65    # preferred with alt (lower) lines
LOTTO_MIN_LEGS       = 7
LOTTO_MAX_LEGS       = 12
LOTTO_STAKE          = 0.25    # $0.25 — lottery ticket, not investment

# ─── ML/RL parlay config ──────────────────────────────────────
MLRL_MIN_EV          = 0.02
MLRL_MIN_LEGS        = 3
MLRL_MAX_LEGS        = 8
MLRL_STAKE_DEFAULT   = 2.00

# ─── Queue expiration ─────────────────────────────────────────
FORCE_FIRE_MINUTES   = 30    # if any leg locks within this many minutes, fire now
MAX_SAME_GAME_LEGS   = 1     # always enforce — one leg per game

# ─── Pitcher markets eligible for lotto ──────────────────────
LOTTO_ELIGIBLE_MARKETS = {
    "player_pitcher_strikeouts",
    "player_strike_outs",
    "player_pitcher_outs",
    "player_hits_allowed",
    "player_walks",
    "player_earned_runs",
    "player_batters_faced",
}

# ─── ML/RL markets ────────────────────────────────────────────
MLRL_ELIGIBLE_MARKETS = {
    "f3_ml", "f5_ml", "f3_rl", "f5_rl",
    "first_3_innings_ml", "first_5_innings_ml",
    "first_3_innings_rl", "first_5_innings_rl",
}


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — SHARED UTILITIES
# ═══════════════════════════════════════════════════════════════

def _game_key(leg: dict) -> str:
    away = leg.get("away_team") or leg.get("away") or "?"
    home = leg.get("home_team") or leg.get("home") or "?"
    return f"{away}@{home}".lower()


def _minutes_to_lock(lock_time_str: str | None) -> float:
    if not lock_time_str:
        return 9999.0
    try:
        lock = datetime.fromisoformat(lock_time_str.replace("Z", "+00:00"))
        now  = datetime.now(timezone.utc)
        return (lock - now).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 9999.0


def _enforce_one_per_game(legs: list[dict]) -> list[dict]:
    """Keep only the highest-probability leg from each game."""
    by_game: dict[str, dict] = {}
    for leg in legs:
        gk = _game_key(leg)
        tp = leg.get("true_prob", 0)
        if gk not in by_game or tp > by_game[gk].get("true_prob", 0):
            by_game[gk] = leg
    return list(by_game.values())


def _enforce_one_per_game_ev(legs: list[dict]) -> list[dict]:
    """Keep only the highest-EV leg from each game."""
    by_game: dict[str, dict] = {}
    for leg in legs:
        gk = _game_key(leg)
        ev = leg.get("ev", -1)
        if gk not in by_game or ev > by_game[gk].get("ev", -1):
            by_game[gk] = leg
    return list(by_game.values())


def check_queue_expiration(
    queue:            list[dict],
    target_size:      int   = LOTTO_MIN_LEGS,
    force_fire_min:   float = FORCE_FIRE_MINUTES,
    min_legs:         int   = 2,
) -> dict:
    """
    Check if the queue needs a forced early fire due to an imminent lock.

    If the earliest-locking leg is within force_fire_min minutes:
    - Fire immediately with whatever valid legs are available
    - Auto-downsize: use min(available, target_size)
    - Minimum 2 legs required — below that, abandon and wait

    Args:
        queue          : list of eligible legs (already filtered)
        target_size    : ideal number of legs
        force_fire_min : minutes before lock to trigger force-fire
        min_legs       : minimum to fire (abandon if below this)

    Returns:
        {
            "should_fire"  : bool,
            "fire_now"     : bool,  # True = expiration trigger
            "legs_to_use"  : list,
            "reason"       : str,
            "minutes_left" : float,
        }
    """
    if not queue:
        return {"should_fire": False, "fire_now": False,
                "legs_to_use": [], "reason": "empty queue", "minutes_left": 9999}

    # Sort by lock time
    queue_sorted = sorted(
        queue,
        key=lambda x: _minutes_to_lock(x.get("lock_time") or x.get("commence_time")),
    )

    earliest_min = _minutes_to_lock(
        queue_sorted[0].get("lock_time") or queue_sorted[0].get("commence_time")
    )

    available = len(queue_sorted)

    # Ideal: enough legs and not urgent
    if available >= target_size and earliest_min > force_fire_min:
        return {
            "should_fire":  True,
            "fire_now":     False,
            "legs_to_use":  queue_sorted[:target_size],
            "reason":       f"Ideal — {available} legs, {earliest_min:.0f}m to earliest lock",
            "minutes_left": earliest_min,
        }

    # Force fire: time running out
    if earliest_min <= force_fire_min:
        tier = min(available, target_size)
        if tier < min_legs:
            return {
                "should_fire":  False,
                "fire_now":     True,
                "legs_to_use":  [],
                "reason":       f"Force-fire trigger but only {available} legs — below minimum {min_legs}",
                "minutes_left": earliest_min,
            }
        return {
            "should_fire":  True,
            "fire_now":     True,
            "legs_to_use":  queue_sorted[:tier],
            "reason":       f"⚠️ FORCE FIRE — {earliest_min:.0f}m to lock, using {tier}/{target_size} legs",
            "minutes_left": earliest_min,
        }

    # Not enough legs yet — wait
    return {
        "should_fire":  False,
        "fire_now":     False,
        "legs_to_use":  [],
        "reason":       f"Waiting — {available}/{target_size} legs, {earliest_min:.0f}m to earliest lock",
        "minutes_left": earliest_min,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — PITCHER LOTTO SLIP BUILDER
# ═══════════════════════════════════════════════════════════════

def build_pitcher_lotto_slip(
    lotto_pool:    list[dict],
    target_legs:   int   = 10,
    min_legs:      int   = LOTTO_MIN_LEGS,
    min_prob:      float = LOTTO_MIN_PROB,
    stake:         float = LOTTO_STAKE,
) -> dict | None:
    """
    Build a $0.25 high-leg-count pitcher lotto slip for DK/Fanatics.

    SORTING: Absolute true_prob descending — NOT EV.
    Reason: On a lotto ticket you want maximum probability of each leg
    hitting. EV is irrelevant when the stake is $0.25.

    ALT LINE PREFERENCE: If two legs exist for the same pitcher
    (e.g., over 5.5 K at -140 AND over 4.5 K at -230), prefer the
    one where true_prob >= 0.65 and the absolute line is easier to clear.

    INDEPENDENCE: One pitcher per game enforced.

    Args:
        lotto_pool  : legs from SlipRouter.lotto_pool (pre-filtered)
        target_legs : ideal number of legs (default 10)
        min_legs    : fire if this many legs available (default 7)
        min_prob    : minimum true_prob per leg
        stake       : dollar amount to bet (default $0.25)

    Returns:
        Lotto slip dict or None if not enough eligible legs
    """
    # Filter by minimum probability
    eligible = [
        leg for leg in lotto_pool
        if leg.get("true_prob", 0) >= min_prob
        and (leg.get("market") or "").lower().replace(" ", "_") in LOTTO_ELIGIBLE_MARKETS
    ]

    if not eligible:
        return None

    # Prefer alt (lower/easier) lines when available
    eligible = _prefer_alt_lines(eligible)

    # Enforce one pitcher per game (keep highest true_prob)
    eligible = _enforce_one_per_game(eligible)

    # Sort by true_prob descending
    eligible.sort(key=lambda x: x.get("true_prob", 0), reverse=True)

    # Check queue expiration
    expiry = check_queue_expiration(eligible, target_size=target_legs, min_legs=min_legs)

    if not expiry["should_fire"]:
        print(f"  [lotto] {expiry['reason']}")
        return None

    legs_to_use = expiry["legs_to_use"]
    n           = len(legs_to_use)

    # Calculate parlay odds — product of individual decimal odds
    # (approximate; actual parlay odds vary by book)
    approx_decimal_odds = 1.0
    for leg in legs_to_use:
        odds = leg.get("odds") or leg.get("best_price") or -115
        approx_decimal_odds *= american_to_decimal(odds)

    # EV at this stake (informational — lotto mode ignores EV filter)
    joint_prob = 1.0
    for leg in legs_to_use:
        joint_prob *= leg.get("true_prob", 0.5)

    approx_ev = joint_prob * approx_decimal_odds - 1.0
    avg_prob  = sum(l.get("true_prob", 0) for l in legs_to_use) / n

    slip = {
        "slip_type":         "pitcher_lotto",
        "mode":              "lotto",
        "n_legs":            n,
        "stake":             stake,
        "approx_payout":     round(approx_decimal_odds * stake, 2),
        "approx_ev":         round(approx_ev, 6),
        "joint_prob":        round(joint_prob, 8),
        "avg_true_prob":     round(avg_prob, 4),
        "force_fired":       expiry["fire_now"],
        "minutes_to_lock":   round(expiry["minutes_left"], 1),
        "books":             ["DraftKings", "Fanatics"],
        "legs": [
            {
                "n":           i + 1,
                "player":      leg["player"],
                "market":      leg.get("market", ""),
                "direction":   leg.get("direction", "over").upper(),
                "line":        leg.get("line"),
                "true_prob":   round(leg.get("true_prob", 0), 4),
                "odds":        leg.get("odds") or leg.get("best_price") or -115,
                "game":        f"{leg.get('away_team', '?')} @ {leg.get('home_team', '?')}",
                "grade":       leg.get("grade", "?"),
                "lock_time":   leg.get("lock_time") or leg.get("commence_time"),
                "_alt_line":   leg.get("_alt_line", False),
            }
            for i, leg in enumerate(legs_to_use)
        ],
    }

    _log_slip(slip, LOTTO_LOG)
    return slip


def _prefer_alt_lines(legs: list[dict]) -> list[dict]:
    """
    For each unique pitcher, if multiple lines exist (alt lines),
    prefer the one with higher true_prob (easier line to clear).
    This is the lotto slip's alt-line optimization.
    """
    by_pitcher: dict[str, list[dict]] = {}
    for leg in legs:
        key = f"{leg.get('player', '').lower()}_{leg.get('market', '')}"
        by_pitcher.setdefault(key, []).append(leg)

    result = []
    for pitcher_legs in by_pitcher.values():
        # Sort by true_prob descending — take best
        pitcher_legs.sort(key=lambda x: x.get("true_prob", 0), reverse=True)
        best = pitcher_legs[0]
        if len(pitcher_legs) > 1:
            best = {**best, "_alt_line": True}
        result.append(best)
    return result


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — INDEPENDENT ML/RL PARLAY BUILDER
# ═══════════════════════════════════════════════════════════════

def build_mlrl_parlay(
    ml_rl_pool:  list[dict],
    target_legs: int   = 5,
    min_legs:    int   = MLRL_MIN_LEGS,
    min_ev:      float = MLRL_MIN_EV,
    bankroll:    float = 500.0,
    stake:       float = MLRL_STAKE_DEFAULT,
) -> dict | None:
    """
    Build an independent F3/F5 ML or RL parlay for DK/Fanatics.

    SELECTION: Best EV leg per game (one per game, enforced).
    F3 vs F5: Whichever has higher EV wins for that game.
    RL vs ML: RL included when pitcher confidence >= 75 (stronger fade).

    QUEUE EXPIRATION: Same 30-minute force-fire logic.

    Args:
        ml_rl_pool  : from SlipRouter.ml_rl_pool (pre-filtered + best F3/F5)
        target_legs : ideal parlay size (default 5)
        min_legs    : minimum to fire (default 3)
        min_ev      : minimum per-leg EV
        bankroll    : for Kelly sizing
        stake       : dollar amount (if Kelly < stake, use Kelly)

    Returns:
        ML/RL parlay dict or None
    """
    # Filter by min EV and eligible markets
    eligible = [
        leg for leg in ml_rl_pool
        if leg.get("ev", -1) >= min_ev
        and (leg.get("market") or "").lower().replace(" ", "_") in MLRL_ELIGIBLE_MARKETS
    ]

    if not eligible:
        return None

    # Enforce one leg per game (best EV)
    eligible = _enforce_one_per_game_ev(eligible)
    eligible.sort(key=lambda x: x.get("ev", 0), reverse=True)

    # Check expiration
    expiry = check_queue_expiration(eligible, target_size=target_legs, min_legs=min_legs)

    if not expiry["should_fire"]:
        print(f"  [mlrl_parlay] {expiry['reason']}")
        return None

    legs_to_use = expiry["legs_to_use"]
    n           = len(legs_to_use)

    # Parlay EV using ev_engine
    leg_probs = [leg.get("true_prob", 0.5) for leg in legs_to_use]
    dec_odds_list = [american_to_decimal(leg.get("odds") or leg.get("best_price") or -110)
                     for leg in legs_to_use]
    parlay_decimal = 1.0
    for d in dec_odds_list:
        parlay_decimal *= d

    ev_result = parlay_edge_full(leg_probs, parlay_decimal, min_edge=MIN_EDGE_PARLAY_COMBO)
    kelly_amt = kelly_stake(
        ev_result["ev"],
        ev_result["joint_prob"],
        parlay_decimal,
        bankroll,
        QUARTER_KELLY,
    )
    actual_stake = min(stake, kelly_amt) if kelly_amt > 0 else 0.0

    slip = {
        "slip_type":       "independent_mlrl",
        "mode":            "ev_parlay",
        "n_legs":          n,
        "stake":           round(actual_stake, 2),
        "parlay_decimal":  round(parlay_decimal, 2),
        "approx_payout":   round(parlay_decimal * actual_stake, 2),
        "ev":              round(ev_result["ev"], 6),
        "passes_ev":       ev_result["passes"],
        "joint_prob":      round(ev_result["joint_prob"], 8),
        "vig_compound_pct": ev_result["vig_compound_pct"],
        "force_fired":     expiry["fire_now"],
        "minutes_to_lock": round(expiry["minutes_left"], 1),
        "books":           ["DraftKings", "Fanatics"],
        "legs": [
            {
                "n":         i + 1,
                "team":      leg.get("player") or leg.get("team", "?"),
                "market":    leg.get("market", ""),
                "direction": leg.get("direction", ""),
                "true_prob": round(leg.get("true_prob", 0), 4),
                "ev":        round(leg.get("ev", 0), 4),
                "odds":      leg.get("odds") or leg.get("best_price") or -110,
                "game":      f"{leg.get('away_team', '?')} @ {leg.get('home_team', '?')}",
                "grade":     leg.get("grade", "?"),
                "lock_time": leg.get("lock_time") or leg.get("commence_time"),
            }
            for i, leg in enumerate(legs_to_use)
        ],
    }

    _log_slip(slip, ML_RL_LOG)
    return slip


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — DISCORD FORMATTERS
# ═══════════════════════════════════════════════════════════════

def format_lotto_discord(slip: dict) -> str:
    """Format $0.25 pitcher lotto slip for Discord."""
    n        = slip["n_legs"]
    payout   = slip["approx_payout"]
    avg_prob = slip["avg_true_prob"]
    forced   = " ⚠️ FORCE-FIRED" if slip.get("force_fired") else ""
    mins     = slip.get("minutes_to_lock", 0)
    time_str = f"{int(mins)}m" if mins < 9999 else "TBD"

    lines = [
        f"🎲 **Pitcher Lotto Slip — {n} Legs${forced}**",
        f"Stake: $0.25 | Est. payout: ~${payout:.2f} | Avg prob: {avg_prob:.0%} | 🔒 in {time_str}",
        f"📚 DraftKings or Fanatics — verify before submitting",
        "",
    ]

    for leg in slip.get("legs", []):
        prob_pct  = round(leg["true_prob"] * 100, 1)
        dir_emoji = "⬆️" if leg["direction"] == "OVER" else "⬇️"
        alt_tag   = " 🔀alt" if leg.get("_alt_line") else ""
        mkt_short = (leg["market"].replace("player_pitcher_", "").replace("player_", "")
                     .replace("_", " ").title())
        lines.append(
            f"  **{leg['n']}.** [{leg['grade']}] {dir_emoji} {leg['player']} "
            f"{mkt_short} {leg['direction']} {leg['line']} | {prob_pct}%{alt_tag} | {leg['game']}"
        )

    lines.append("")
    lines.append("⚠️ Lotto slip — 1 miss kills the ticket. High variance by design.")
    return "\n".join(lines)


def format_mlrl_discord(slip: dict) -> str:
    """Format independent ML/RL parlay for Discord."""
    n        = slip["n_legs"]
    ev_pct   = slip["ev"] * 100
    payout   = slip["approx_payout"]
    stake    = slip["stake"]
    forced   = " ⚠️ FORCE-FIRED" if slip.get("force_fired") else ""
    mins     = slip.get("minutes_to_lock", 0)
    time_str = f"{int(mins)}m" if mins < 9999 else "TBD"
    passes   = "✅" if slip.get("passes_ev") else "⚠️"

    lines = [
        f"🏟️ **Independent ML/RL Parlay — {n} Legs{forced}**",
        f"{passes} EV: {ev_pct:+.1f}% | Stake: ${stake:.2f} | Est. payout: ~${payout:.2f} | 🔒 in {time_str}",
        f"📚 DraftKings or Fanatics — verify before submitting",
        "",
    ]

    for leg in slip.get("legs", []):
        ev_leg   = round(leg["ev"] * 100, 1)
        prob_pct = round(leg["true_prob"] * 100, 1)
        mkt      = (leg["market"].replace("first_", "F").replace("_innings", "").replace("_", " ").upper()
                    .replace("ML", "ML").replace("RL", "RL"))
        lines.append(
            f"  **{leg['n']}.** [{leg['grade']}] {leg['team']} {mkt} | "
            f"EV {ev_leg:+.1f}% | {prob_pct}% | {leg['game']}"
        )

    lines.append("")
    lines.append("📊 Verify all ML/RL availability on your book before submitting")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — LOGGING
# ═══════════════════════════════════════════════════════════════

def _log_slip(slip: dict, log_file: Path) -> None:
    existing = []
    if log_file.exists():
        try:
            existing = json.loads(log_file.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    existing.append({**slip, "logged_at": datetime.now().isoformat()})
    log_file.write_text(json.dumps(existing[-50:], indent=2, default=str))


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Independent Parlay Builder Self-Test")
    print("=" * 60)

    # Mock lotto pool — 10 pitchers from 10 different games
    mock_lotto = [
        {"player": f"Pitcher {i}", "market": "player_pitcher_strikeouts",
         "direction": "over", "line": 5.5, "true_prob": 0.60 + i*0.01,
         "odds": -115, "grade": "B+",
         "home_team": f"Team{i*2}", "away_team": f"Team{i*2+1}",
         "commence_time": "2099-12-31T18:00:00Z"}
        for i in range(12)
    ]

    print("\n[1] Pitcher lotto slip:")
    lotto = build_pitcher_lotto_slip(mock_lotto, target_legs=10, min_legs=7)
    if lotto:
        print(f"    {lotto['n_legs']} legs | avg_prob={lotto['avg_true_prob']:.2%} | payout≈${lotto['approx_payout']:.2f}")
        print(f"\n{format_lotto_discord(lotto)}")
    else:
        print("    No lotto slip built")

    # Mock ML/RL pool
    mock_mlrl = [
        {"player": f"Team{i*2}", "market": "f5_ml" if i % 2 == 0 else "f3_ml",
         "direction": "ml", "true_prob": 0.55 + i*0.01, "ev": 0.03 + i*0.005,
         "odds": -115, "grade": "B+",
         "home_team": f"Team{i*2}", "away_team": f"Team{i*2+1}",
         "commence_time": "2099-12-31T20:00:00Z"}
        for i in range(6)
    ]

    print("\n[2] ML/RL independent parlay:")
    mlrl = build_mlrl_parlay(mock_mlrl, target_legs=5, min_legs=3, bankroll=500)
    if mlrl:
        print(f"    {mlrl['n_legs']} legs | EV={mlrl['ev']:+.2%} | stake=${mlrl['stake']:.2f} | payout≈${mlrl['approx_payout']:.2f}")
        print(f"\n{format_mlrl_discord(mlrl)}")
    else:
        print("    No ML/RL parlay built")

    # Test force-fire expiration logic
    print("\n[3] Queue expiration — urgent leg:")
    urgent_queue = [
        {"player": "Gerrit Cole", "true_prob": 0.72, "odds": -115,
         "lock_time": "2020-01-01T00:01:00Z"},  # already expired — force fire
        {"player": "Zack Wheeler", "true_prob": 0.68, "odds": -110,
         "lock_time": "2099-01-01T00:00:00Z"},
        {"player": "Corbin Burnes", "true_prob": 0.65, "odds": -112,
         "lock_time": "2099-01-01T00:00:00Z"},
    ]
    expiry = check_queue_expiration(urgent_queue, target_size=7, min_legs=2)
    print(f"    should_fire={expiry['should_fire']} fire_now={expiry['fire_now']}")
    print(f"    reason: {expiry['reason']}")

    print("\n✓ Independent parlay builder ready.")

# slipiq_ml_parlay_patch.py
# ═══════════════════════════════════════════════════════════════
# PATCH — Apply to slipiq_ml_parlay.py in Cursor
#
# TWO CHANGES:
#
# CHANGE 1: Wire quick_validate_parlay() into build_ml_parlays()
#   After build_game_sgp() assembles legs for each game package,
#   call quick_validate_parlay() from slipiq_montecarlo.
#   If go=False, drop the package regardless of score_game_package score.
#   This replaces vibes-based gating with mathematical go/no-go.
#
# CHANGE 2: Add F3 ML/RL support in build_game_sgp()
#   Currently only F5 ML and F5 RL are built.
#   Add: query F3 ML and F3 RL lines from slipiq_game_lines.
#   Use select_best_fn_market() logic — pick higher EV between F3 and F5.
# ═══════════════════════════════════════════════════════════════

# ─── CHANGE 1 ─────────────────────────────────────────────────
# In build_ml_parlays(), REPLACE this block:
#
#   legs = build_game_sgp(pick, batter_picks, game_line)
#   if not legs:
#       continue
#
#   game_packages.append({...})
#
# WITH:

CHANGE_1_REPLACEMENT = """
        legs = build_game_sgp(pick, batter_picks, game_line)
        if not legs:
            continue

        # ── Monte Carlo gate (replaces vibes score gate) ───────
        # Only include package if joint probability is mathematically viable.
        # Parlay decimal odds: approximate from leg count (2-4 legs typical)
        mc_valid = _validate_sgp_package(legs)
        if not mc_valid["go"]:
            print(f"  [ml_parlay] {pick.get('player')} SGP rejected: {mc_valid['reason']}")
            continue

        game_packages.append({
            "game":    game_key,
            "score":   score,
            "legs":    legs,
            "pitcher": pick.get("player"),
            "proj":    pick.get("projection", 0),
            "conf":    pick.get("confidence", 0),
            "mc":      mc_valid,   # attach validation result for Discord display
        })
        used_games.add(game_key)
"""

# ─── CHANGE 1 helper function ──────────────────────────────────
# Add this function at module level (before build_ml_parlays):

CHANGE_1_HELPER = """
def _validate_sgp_package(legs: list[dict]) -> dict:
    \"\"\"
    Validate a same-game SGP package via Monte Carlo before posting.
    Returns quick_validate_parlay() result: {"go": bool, "reason": str, ...}

    Uses leg_type to look up correlation matrix.
    Falls back to independent simulation if montecarlo import fails.
    \"\"\"
    try:
        from slipiq_montecarlo import quick_validate_parlay

        # Extract leg types and true_probs
        leg_probs = []
        leg_types = []
        for leg in legs:
            # Use true_prob if available, fallback to confidence/100
            tp = (
                leg.get("true_prob") or
                (leg.get("confidence", 60) / 100.0)
            )
            leg_probs.append(max(0.35, min(0.95, float(tp))))
            leg_types.append(leg.get("leg_type", "unknown"))

        if not leg_probs:
            return {"go": False, "reason": "no legs"}

        # Approximate parlay decimal odds from leg count
        # Real odds would come from the book — this is conservative estimate
        n = len(leg_probs)
        approx_decimal = 1.0
        for leg in legs:
            odds_american = leg.get("odds") or -115
            if odds_american > 0:
                approx_decimal *= 1 + (odds_american / 100)
            else:
                approx_decimal *= 1 + (100 / abs(odds_american))

        result = quick_validate_parlay(
            leg_probs      = leg_probs,
            leg_types      = leg_types,
            payout_decimal = approx_decimal,
            bankroll       = 500.0,
        )
        return result

    except Exception as e:
        # If montecarlo unavailable, use simple joint probability check
        joint = 1.0
        for leg in legs:
            tp = leg.get("true_prob") or (leg.get("confidence", 60) / 100.0)
            joint *= float(tp)
        # Minimum joint prob: need > 5% to bother posting
        if joint >= 0.05:
            return {"go": True, "reason": f"joint_prob={joint:.3f} (fallback)", "ev": joint - 1}
        return {"go": False, "reason": f"joint_prob={joint:.3f} too low (fallback)"}
"""

# ─── CHANGE 2 ─────────────────────────────────────────────────
# In build_game_sgp(), AFTER the existing F5 ML leg block,
# ADD this section that adds F3 ML as an alternative:

CHANGE_2_F3_BLOCK = """
    # ── Leg: F3 ML (compare against F5 — pick higher EV) ─────
    # Query F3 lines from game_line data (populated by slipiq_game_lines)
    f3_ml_odds  = None
    f3_rl_odds  = None
    f5_ml_odds  = ml_odds   # already set above

    # game_line may contain f3_ml key if slipiq_game_lines fetched it
    f3_data = game_line.get("f3", {}) or {}
    if f3_data:
        f3_ml_odds = f3_data.get("ml_home") if is_home else f3_data.get("ml_away")
        f3_rl_odds = f3_data.get("rl_home") if is_home else f3_data.get("rl_away")

    # Compute EV for F3 vs F5 and pick the better one
    best_fn_market = "f5_ml"
    best_fn_odds   = f5_ml_odds

    if f3_ml_odds and f5_ml_odds:
        try:
            from slipiq_ev_engine import assess_leg, no_vig_prob
            # Get Pinnacle no-vig for this team (approximate from game_line)
            pin_home = game_line.get("pinnacle_home_ml")
            pin_away = game_line.get("pinnacle_away_ml")
            if pin_home and pin_away:
                nv = no_vig_prob(
                    pin_home if is_home else pin_away,
                    pin_away if is_home else pin_home,
                )
                true_prob_ml = nv["true_over"]
                ev_f5 = (true_prob_ml * (1 + abs(f5_ml_odds)/100 if f5_ml_odds < 0
                         else 1 + f5_ml_odds/100)) - 1
                ev_f3 = (true_prob_ml * (1 + abs(f3_ml_odds)/100 if f3_ml_odds < 0
                         else 1 + f3_ml_odds/100)) - 1
                if ev_f3 > ev_f5 + 0.005:
                    best_fn_market = "f3_ml"
                    best_fn_odds   = f3_ml_odds
        except Exception:
            pass
    elif f3_ml_odds and not f5_ml_odds:
        best_fn_market = "f3_ml"
        best_fn_odds   = f3_ml_odds

    # Replace the existing F5 ML leg label with the best FN market
    for leg in legs:
        if leg.get("leg_type") in ("f5_ml", "f3_ml"):
            leg["leg_type"] = best_fn_market
            leg["label"]    = leg["label"].replace("F5 ML", best_fn_market.upper().replace("_", " "))
            leg["prop"]     = leg["prop"].replace("First 5", "First 3" if best_fn_market == "f3_ml" else "First 5")
            leg["odds"]     = best_fn_odds or leg["odds"]
            break
"""

# ─── WHERE TO INSERT CHANGE 2 ─────────────────────────────────
# Insert CHANGE_2_F3_BLOCK immediately AFTER the F5 RL leg block
# (after the "# ── Leg 5/6: F5 RL" section, before "return legs").
# This replaces F5 with F3 in the leg if F3 has higher EV.

# ─── CURSOR PROMPT ────────────────────────────────────────────
CURSOR_INSTRUCTIONS = """
Apply these two changes to slipiq_ml_parlay.py:

CHANGE 1:
1a. Add the _validate_sgp_package() function from CHANGE_1_HELPER
    at module level, before build_ml_parlays().

1b. In build_ml_parlays(), find the block:
        legs = build_game_sgp(pick, batter_picks, game_line)
        if not legs:
            continue
        game_packages.append({...})
    Replace it with CHANGE_1_REPLACEMENT above.

CHANGE 2:
2a. In build_game_sgp(), after the F5 RL leg block (near the end,
    before "return legs"), insert CHANGE_2_F3_BLOCK.
    This checks if F3 ML has better EV than F5 ML and updates
    the ML leg accordingly.

Do not change any other code in the file.
"""

print("ML parlay patch spec loaded.")
print(CURSOR_INSTRUCTIONS)

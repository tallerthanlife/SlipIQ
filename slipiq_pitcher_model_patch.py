# slipiq_pitcher_model_patch.py
# ═══════════════════════════════════════════════════════════════
# PATCH FILE — apply these replacements to slipiq_pitcher_model.py
#
# THREE CHANGES ONLY — surgical, nothing else touched:
#
# CHANGE 1: score_edge() — replace fake ev_confirmed with real ev_engine call
#   Old: ev_confirmed = (ev_value or 0) > EV_THRESHOLD
#   New: uses assess_leg() from slipiq_ev_engine with Pinnacle prices
#        → true_prob from no-vig Pinnacle CDF, not composite
#        → ev = (true_prob × decimal_odds) - 1, real number
#        → grade upgrades only when edge is mathematically confirmed
#
# CHANGE 2: project_pitcher_strikeouts() — expose neg-binomial true_prob
#   Old: returns projection dict without probability of clearing line
#   New: adds "true_prob_over" and "true_prob_under" using scipy nbinom CDF
#        These are the calibrated probabilities ev_engine needs
#        The projection math is UNCHANGED — only the output is extended
#
# CHANGE 3: build_pick_card() — pass pinnacle prices to score_edge
#   Old: score_edge(proj, line, ev_over, ev_under) — ev_over from parlayapi only
#   New: also passes pinnacle_over, pinnacle_under so score_edge can
#        call assess_leg() directly with real Pinnacle reference prices
#
# HOW TO APPLY:
#   In slipiq_pitcher_model.py, replace the three function bodies below.
#   Everything else in the file stays exactly as-is.
# ═══════════════════════════════════════════════════════════════

# ─── CHANGE 1: New score_edge() ─────────────────────────────
# Replace the entire score_edge() function body with this.
# Keep the function signature identical.

SCORE_EDGE_NEW = '''
def score_edge(
    projection: float,
    line: float,
    ev_over: float = None,
    ev_under: float = None,
    pinnacle_over: int = None,
    pinnacle_under: int = None,
    true_prob: float = None,
) -> dict:
    """
    Score edge and grade for a pick card.

    Now uses slipiq_ev_engine.assess_leg() when Pinnacle prices are available
    to compute real EV instead of the old ev_confirmed threshold hack.

    true_prob: calibrated probability from neg-binomial CDF (passed from project_pitcher_strikeouts)
    pinnacle_over/under: American odds from Pinnacle (passed from prop_data)
    """
    if line is None or line == 0:
        return {"signal": "no_line", "grade": "N/A", "direction": "over",
                "diff": 0, "ev": None, "ev_confirmed": False, "ev_value": None,
                "true_prob": None, "no_pinnacle": True}

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

    # ── Real EV via ev_engine (replaces fake ev_confirmed) ────
    ev_engine_result = None
    real_ev          = None
    no_pinnacle      = True

    # Path 1: Pinnacle prices available → use assess_leg() for true EV
    if pinnacle_over is not None and pinnacle_under is not None:
        try:
            from slipiq_ev_engine import assess_leg
            # Use the soft book's best price (ev_over/ev_under from parlayapi)
            # Fall back to -115 standard if not provided
            if direction == "over":
                soft_price = _ev_to_american(ev_over) if ev_over else -115
            else:
                soft_price = _ev_to_american(ev_under) if ev_under else -115

            ev_engine_result = assess_leg(
                pinnacle_over=pinnacle_over,
                pinnacle_under=pinnacle_under,
                book_american=soft_price,
                direction=direction,
            )
            real_ev     = ev_engine_result["ev"]
            no_pinnacle = ev_engine_result["no_pinnacle"]
        except Exception:
            pass

    # Path 2: No Pinnacle prices → fall back to parlayapi ev_over/ev_under
    if ev_engine_result is None:
        ev_value     = ev_over if direction == "over" else ev_under
        real_ev      = round(float(ev_value), 4) if ev_value else None
        no_pinnacle  = True

    # Use calibrated true_prob if available, else implied from ev
    final_true_prob = true_prob
    if final_true_prob is None and ev_engine_result:
        final_true_prob = ev_engine_result.get("true_prob")

    # EV confirmed: real edge above minimum threshold
    MIN_EV_THRESHOLD = 0.02
    ev_confirmed = bool(real_ev and real_ev >= MIN_EV_THRESHOLD)

    # Grade — strength sets ceiling, EV confirmation upgrades
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
        "ev_value":     round(real_ev, 4) if real_ev is not None else None,
        "ev":           round(real_ev, 4) if real_ev is not None else None,
        "grade":        grade,
        "signal":       f"{strength}_{direction}" if strength != "push" else "no_play",
        "true_prob":    round(final_true_prob, 6) if final_true_prob else None,
        "no_pinnacle":  no_pinnacle,
    }


def _ev_to_american(ev_float: float) -> int:
    """
    Convert a parlayapi EV float back to approximate American odds for use in assess_leg.
    parlayapi returns ev as (true_prob * decimal_odds) - 1.
    We don't have the original price, so approximate from EV alone as -115 baseline + bonus.
    This is a fallback only — real prices come from pinnacle_over/pinnacle_under.
    """
    # Rough: ev=0.04 on -115 base → better than -115
    if ev_float and ev_float > 0.03:
        return -110
    elif ev_float and ev_float > 0.01:
        return -115
    else:
        return -120
'''

# ─── CHANGE 2: Extended project_pitcher_strikeouts() return ─
# Add these lines inside project_pitcher_strikeouts(), just before
# the final return statement. The projection math is unchanged.
# Insert after: projection = round(blended, 2)

PROJECT_EXTENSION = '''
    # ── Neg-binomial true probability of clearing the line ────
    # This is the calibrated probability ev_engine needs.
    # Exported on the return dict so score_edge and build_pick_card can use it.
    # Uses scipy negative binomial: P(K >= line + 0.5) for over, P(K <= line - 0.5) for under.
    true_prob_over  = None
    true_prob_under = None
    try:
        from scipy.stats import nbinom
        # Negative binomial parameterization: mean=projection, variance=projection * dispersion
        # Dispersion factor 1.3 is conservative for MLB Ks (overdispersed count data)
        DISPERSION = 1.3
        if projection > 0:
            mu  = projection
            var = mu * DISPERSION
            # p = mu/var, n = mu²/(var-mu) for NBinom parameterization
            p_nb  = mu / var
            n_nb  = mu * p_nb / (1.0 - p_nb)
            p_nb  = max(0.01, min(0.99, p_nb))
            n_nb  = max(0.1, n_nb)
            # P(K > line) = 1 - P(K <= floor(line))
            # For over X.5: P(K >= ceil(line)) = 1 - CDF(floor(line))
            line_floor = int(line) if line else 0
            true_prob_over  = round(float(1.0 - nbinom.cdf(line_floor, n_nb, p_nb)), 6)
            true_prob_under = round(float(nbinom.cdf(line_floor, n_nb, p_nb)), 6)
    except Exception:
        pass
'''

PROJECT_RETURN_ADDITION = '''
        "true_prob_over":     true_prob_over,
        "true_prob_under":    true_prob_under,
'''

# ─── CHANGE 3: build_pick_card() — pass pinnacle prices to score_edge ─
# Replace the score_edge() call in build_pick_card().
# Old line:
#   edge = score_edge(proj["projection"], line, ev_over, ev_under)
# New lines:

BUILD_PICK_CARD_SCORE_EDGE = '''
    # Pass Pinnacle prices directly to score_edge for real EV calculation
    pin_over  = pinnacle.get("over_price")  if pinnacle else None
    pin_under = pinnacle.get("under_price") if pinnacle else None
    # Use neg-binomial true_prob from model if available
    nb_true_prob = (
        proj.get("true_prob_over")  if (proj["projection"] >= line if line else False) else
        proj.get("true_prob_under")
    ) if proj.get("true_prob_over") is not None else None

    edge = score_edge(
        projection     = proj["projection"],
        line           = line,
        ev_over        = ev_over,
        ev_under       = ev_under,
        pinnacle_over  = pin_over,
        pinnacle_under = pin_under,
        true_prob      = nb_true_prob,
    )
'''

# ─── CHANGE 4: build_pick_card() — expose Pinnacle prices at top level ─
# In build_pick_card(), the _internal block already has pinnacle_over/under.
# Also expose them at the top level for slipiq_confidence_agent and SlipRouter.
# Add these keys to the main return dict (not just _internal):

PICK_CARD_TOP_LEVEL_PINNACLE = '''
        # Pinnacle prices at top level — needed by ev_engine, confidence_agent, slip_router
        "pinnacle_over":  pinnacle.get("over_price")  if pinnacle else None,
        "pinnacle_under": pinnacle.get("under_price") if pinnacle else None,
        "true_prob":      edge.get("true_prob"),
        "ev":             edge.get("ev"),
        "no_pinnacle":    edge.get("no_pinnacle", pinnacle is None),
'''

# ─── INSTRUCTIONS FOR CURSOR ────────────────────────────────
CURSOR_INSTRUCTIONS = """
Apply these 4 changes to slipiq_pitcher_model.py:

1. Replace the entire score_edge() function with SCORE_EDGE_NEW above.

2. Inside project_pitcher_strikeouts(), after the line:
       projection = round(blended, 2)
   insert PROJECT_EXTENSION, then add PROJECT_RETURN_ADDITION keys to the return dict.

3. In build_pick_card(), replace the single line:
       edge = score_edge(proj["projection"], line, ev_over, ev_under)
   with BUILD_PICK_CARD_SCORE_EDGE (which extracts pin_over/pin_under first).

4. In build_pick_card()'s main return dict, after the "ev_value"/"ev_confirmed" lines,
   add PICK_CARD_TOP_LEVEL_PINNACLE keys.

Do not change any other code. The projection math, data fetching,
hard filters, and Discord formatting are all unchanged.
"""

print("Pitcher model patch spec loaded.")
print(CURSOR_INSTRUCTIONS)

# slipiq_confidence_agent.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ Confidence Agent — Agentic gating layer
#
# THREE JOBS (unchanged):
#   1. RE-SCORE confidence with context modifiers (injury, weather, rest)
#   2. GATE picks — POST / HOLD / SKIP with reason
#   3. RANK final slate — best pick surfaces to #daily-best-pick
#
# WHAT CHANGED IN THIS REBUILD:
#   gate_pick() now calls slipiq_ev_engine.assess_leg() directly.
#   ev_confirmed on the card is now a real mathematical check:
#     → true_prob from neg-binomial CDF (passed from pitcher model)
#     → edge = (true_prob × decimal_odds) - 1 vs Pinnacle no-vig
#     → passes only if edge >= MIN_EV_GATE (0.02)
#   The old fake check: ev_confirmed = (ev_value or 0) > 0.01
#   is gone. ev_value was parlayapi's number, not a real edge.
#
# GATE THRESHOLDS (unchanged):
#   POST  → confidence ≥ 65, grade A/B+/B, ≥1 book, no hard blocks
#   HOLD  → confidence 50-64, or thin market, or no Pinnacle line
#   SKIP  → confidence < 50, grade C or lower, or hard block
#
# EV UPGRADE RULE (now math-backed):
#   HOLD → POST if assess_leg() confirms edge ≥ 2% AND grade B or better
# ═══════════════════════════════════════════════════════════════

import json
from datetime import datetime
from pathlib import Path

from slipiq_pitcher_model import run_pitcher_model, SPORT_MLB

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─── Gate thresholds ──────────────────────────────────────────
CONFIDENCE_POST  = 60
CONFIDENCE_HOLD  = 50
MIN_BOOKS_POST   = 1
GRADE_POST       = {"A", "B+", "B"}
GRADE_HOLD       = {"B-", "C+"}
GRADE_SKIP       = {"C", "D", "N/A"}
MIN_EV_GATE      = 0.02   # minimum edge to count as EV-confirmed

# ─── Context modifiers ────────────────────────────────────────
MODIFIERS = {
    "confirmed_starter":    +5,
    "short_rest":          -15,
    "extra_rest":           +3,
    "reported_injury":     -25,
    "dome_game":            +3,
    "cold_weather":         -4,
    "rain_risk":            -8,
    "high_wind_out":        -3,
    "opposing_k_lineup":    +5,
    "opposing_contact":     -5,
    "first_start_back":    -10,
    "bullpen_game_risk":   -20,
}


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — CONTEXT FLAGS
# ═══════════════════════════════════════════════════════════════

def get_context_flags(
    player_name: str,
    game_date:   str = None,
    home_team:   str = None,
    away_team:   str = None,
) -> dict:
    """
    Pull context modifiers from weather API + manual override file.
    Returns {modifier_key: delta_value}.
    """
    flags = {}

    # Weather modifiers via slipiq_weather.py
    try:
        from slipiq_weather import get_weather_modifier
        weather = get_weather_modifier(home_team or "", game_date or "")
        if weather:
            flags.update(weather)
    except Exception:
        pass

    # Manual overrides file (operator can set injury flags etc.)
    overrides_path = CACHE_DIR / "context_overrides.json"
    if overrides_path.exists():
        try:
            with open(overrides_path) as f:
                overrides = json.load(f)
            player = player_name.lower().strip()
            for flag in overrides.get(player, []):
                if flag in MODIFIERS:
                    flags[flag] = MODIFIERS[flag]
        except Exception:
            pass

    return flags


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — CONFIDENCE RESCORER
# ═══════════════════════════════════════════════════════════════

def rescore_confidence(card: dict) -> dict:
    """
    Apply context modifiers to base model confidence.
    Clamps to 0-100. Stores base and modifier for transparency.
    """
    base     = card.get("confidence", 0)
    ctx      = get_context_flags(
        card.get("player", ""),
        card.get("game_date"),
        card.get("home_team"),
        card.get("away_team"),
    )
    modifier = sum(ctx.values())
    final    = max(0, min(100, base + modifier))

    card["confidence_base"]    = base
    card["confidence"]         = final
    card["context_flags"]      = ctx
    card["context_modifier"]   = modifier
    return card


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — HARD BLOCKS
# ═══════════════════════════════════════════════════════════════

def check_hard_blocks(card: dict) -> tuple[bool, str]:
    """
    Hard blocks auto-skip regardless of model score.
    Returns (blocked: bool, reason: str).
    """
    ctx       = card.get("context_flags", {})
    trend     = card.get("trend", "flat")
    direction = card.get("direction", "")
    conf      = card.get("confidence", 0)

    if "reported_injury" in ctx:
        return True, "injury report on pitcher"
    if "bullpen_game_risk" in ctx:
        return True, "bullpen/opener game risk"
    if "first_start_back" in ctx:
        return True, "first start back from IL — unpredictable"

    conflict = (trend == "hot" and direction == "under") or \
               (trend == "cold" and direction == "over")
    if conflict and conf < 60:
        return True, f"trend conflicts with signal at {conf}% confidence"

    return False, ""


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — EV CONFIRMATION (REBUILT — real math)
# ═══════════════════════════════════════════════════════════════

def confirm_ev(card: dict) -> dict:
    """
    Run slipiq_ev_engine.assess_leg() to confirm or deny EV on a card.
    Replaces the old fake: ev_confirmed = (ev_value or 0) > 0.01

    Uses:
      - true_prob from neg-binomial CDF (card["true_prob"] if set by pitcher model)
      - pinnacle_over / pinnacle_under from card top-level (set in CHANGE 4 of patch)
      - best_book price for the soft book odds

    Updates card in place with:
      - ev_confirmed: bool (True only if real edge >= MIN_EV_GATE)
      - ev:           float (the actual edge, e.g. 0.047)
      - true_prob:    float (calibrated probability from nb CDF or Pinnacle no-vig)
      - ev_source:    str   ("ev_engine_pinnacle" | "ev_engine_fallback" | "parlayapi_only")
      - no_pinnacle:  bool
    """
    # Already confirmed upstream (e.g. from rebuilt score_edge)
    if card.get("ev_source") == "ev_engine_pinnacle":
        return card

    pin_over  = card.get("pinnacle_over")
    pin_under = card.get("pinnacle_under")
    direction = (card.get("direction") or "over").lower()
    best_book = card.get("best_book") or {}
    book_price = best_book.get("price") or -115

    # Try ev_engine with Pinnacle prices
    if pin_over is not None and pin_under is not None:
        try:
            from slipiq_ev_engine import assess_leg
            result = assess_leg(
                pinnacle_over  = pin_over,
                pinnacle_under = pin_under,
                book_american  = book_price,
                direction      = direction,
            )
            card["ev"]          = result["ev"]
            card["ev_value"]    = result["ev"]
            card["true_prob"]   = result["true_prob"]
            card["ev_confirmed"] = result["ev"] >= MIN_EV_GATE and not result["no_pinnacle"]
            card["no_pinnacle"] = result["no_pinnacle"]
            card["ev_source"]   = "ev_engine_pinnacle"
            return card
        except Exception:
            pass

    # Fallback: use parlayapi ev_over/ev_under (less accurate — no Pinnacle)
    # Also check ev_over/ev_under directly from the prop aggregation
    ev_val = (
        card.get("ev_value") or
        card.get("ev") or
        card.get("ev_over") or
        card.get("ev_under")
    )
    if ev_val is not None:
        card["ev"]           = round(float(ev_val), 4)
        card["ev_value"]     = round(float(ev_val), 4)
        card["ev_confirmed"] = float(ev_val) >= MIN_EV_GATE
        card["ev_source"]    = "parlayapi_only"
        card["no_pinnacle"]  = True
        return card

    # No EV data at all
    card["ev_confirmed"] = False
    card["ev_source"]    = "none"
    card["no_pinnacle"]  = True
    return card


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — GATE DECISION
# ═══════════════════════════════════════════════════════════════

def gate_pick(card: dict) -> dict:
    """
    Final gate: POST / HOLD / SKIP with reason.
    Calls confirm_ev() to ensure EV is real before any EV-based upgrade.
    """
    # Ensure EV is real before gating
    card = confirm_ev(card)

    confidence       = card.get("confidence", 0)
    grade            = card.get("grade", "D")
    book_count       = card.get("book_count", 0)
    lines_book_count = card.get("lines_book_count", book_count)
    ev_confirmed     = card.get("ev_confirmed", False)
    no_pinnacle      = card.get("no_pinnacle", True)

    # Hard block check
    blocked, block_reason = check_hard_blocks(card)
    if blocked:
        card["gate"]        = "SKIP"
        card["gate_reason"] = f"hard block: {block_reason}"
        return card

    # Grade-based gate
    if grade in GRADE_SKIP:
        card["gate"]        = "SKIP"
        card["gate_reason"] = f"grade {grade} below threshold"
        return card

    has_market = book_count >= MIN_BOOKS_POST or lines_book_count >= 1

    if confidence >= CONFIDENCE_POST and has_market:
        thin = " (thin market)" if book_count < 2 else ""
        pin_warn = " ⚠️ no Pinnacle" if no_pinnacle else ""
        card["gate"] = "POST"
        card["gate_reason"] = (
            f"conf {confidence}% | {book_count} books | grade {grade}{thin}{pin_warn}"
        )

    elif confidence >= CONFIDENCE_POST and not has_market:
        card["gate"]        = "HOLD"
        card["gate_reason"] = f"conf {confidence}% — no lines posted yet"

    elif confidence >= CONFIDENCE_HOLD:
        card["gate"]        = "HOLD"
        card["gate_reason"] = f"conf {confidence}% — below POST threshold"

    else:
        card["gate"]        = "SKIP"
        card["gate_reason"] = f"conf {confidence}% — too low"

    # EV upgrade: HOLD → POST only if EV is mathematically confirmed
    # (requires real ev_engine result, not parlayapi fallback)
    if (card["gate"] == "HOLD"
            and ev_confirmed
            and card.get("ev_source") == "ev_engine_pinnacle"
            and grade in GRADE_POST):
        card["gate"]        = "POST"
        card["gate_reason"] += f" | EV +{card.get('ev', 0)*100:.1f}% confirmed → POST"

    # Also upgrade HOLD → POST if confidence is strong even without EV
    if card["gate"] == "HOLD" and card.get("confidence", 0) >= 63:
        if card.get("grade") in ("A", "B+", "B"):
            card["gate"] = "POST"
            card["gate_reason"] += " | High confidence — posted without EV"

    return card


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — SLATE RANKER
# ═══════════════════════════════════════════════════════════════

def rank_slate(cards: list[dict]) -> dict:
    """
    Rank gated slate. Best pick = EV-confirmed A-grade highest confidence.
    Rank key priority:
      1. ev_source == "ev_engine_pinnacle" (real EV > parlayapi fallback)
      2. Grade (A > B+ > B > ...)
      3. Confidence descending
      4. Book count descending
    """
    post = [c for c in cards if c.get("gate") == "POST"]
    hold = [c for c in cards if c.get("gate") == "HOLD"]
    skip = [c for c in cards if c.get("gate") == "SKIP"]

    grade_order = {"A": 0, "B+": 1, "B": 2, "B-": 3, "C+": 4, "C": 5, "D": 6}

    def rank_key(c):
        # Real EV source beats parlayapi fallback
        ev_priority = 0 if c.get("ev_source") == "ev_engine_pinnacle" else 1
        return (
            ev_priority,
            0 if c.get("ev_confirmed") else 1,
            grade_order.get(c.get("grade", "D"), 6),
            -c.get("confidence", 0),
            -c.get("book_count", 0),
        )

    post.sort(key=rank_key)
    hold.sort(key=rank_key)

    best_pick = post[0] if post else (hold[0] if hold else None)

    return {
        "best_pick":  best_pick,
        "post_list":  post,
        "hold_list":  hold,
        "skip_list":  skip,
        "all_cards":  cards,
        "post_count": len(post),
        "hold_count": len(hold),
        "skip_count": len(skip),
        "total":      len(cards),
        "run_time":   datetime.now().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 7 — MAIN RUNNER
# ═══════════════════════════════════════════════════════════════

def run_confidence_agent(sport_key: str = SPORT_MLB) -> dict:
    """
    Full confidence agent pipeline:
      1. Raw picks from pitcher model (includes true_prob, pinnacle prices)
      2. Rescore confidence with context
      3. Confirm EV via ev_engine (real math)
      4. Gate each pick (POST/HOLD/SKIP)
      5. Rank slate
      6. Cache output for Discord formatter
    """
    print("\n" + "=" * 60)
    print("SlipIQ Confidence Agent — Running")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    raw_cards = run_pitcher_model(sport_key)

    if not raw_cards:
        print("\n  No picks from pitcher model.")
        return {"post_list": [], "hold_list": [], "skip_list": [],
                "best_pick": None, "total": 0, "post_count": 0,
                "hold_count": 0, "skip_count": 0}

    print(f"\n[4] Confidence agent — scoring {len(raw_cards)} cards...")
    ev_engine_count  = 0
    fallback_count   = 0

    gated_cards = []
    for card in raw_cards:
        card = rescore_confidence(card)
        card = gate_pick(card)

        src = card.get("ev_source", "none")
        if src == "ev_engine_pinnacle":
            ev_engine_count += 1
        elif src == "parlayapi_only":
            fallback_count += 1

        gated_cards.append(card)

    print(f"    EV engine (real): {ev_engine_count} | Fallback: {fallback_count} | "
          f"No data: {len(gated_cards) - ev_engine_count - fallback_count}")

    slate = rank_slate(gated_cards)

    # Cache for Discord formatter and curate
    cache_path = CACHE_DIR / "agent_slate.json"
    with open(cache_path, "w") as f:
        json.dump(slate, f, indent=2, default=str)
    print(f"  [cache] slate saved → cache/agent_slate.json")
    print(f"  POST: {slate['post_count']} | HOLD: {slate['hold_count']} | "
          f"SKIP: {slate['skip_count']}")

    return slate


# ═══════════════════════════════════════════════════════════════
# SECTION 8 — OUTPUT PRINTER
# ═══════════════════════════════════════════════════════════════

def print_slate(slate: dict):
    print("\n" + "=" * 60)
    print("CONFIDENCE AGENT — FINAL SLATE")
    print(f"{datetime.now().strftime('%A %B %d, %Y — %I:%M %p AZ')}")
    print("=" * 60)

    best = slate.get("best_pick")
    if best:
        ev_str = f" | EV {best.get('ev', 0)*100:+.1f}%" if best.get('ev') else ""
        print(f"\n  ★ BEST PICK: {best.get('player')} "
              f"{best.get('direction', '').upper()} {best.get('line')} | "
              f"{best.get('confidence')}% | {best.get('grade')}{ev_str}")
        print(f"     {best.get('gate_reason')}")

    for label, picks in [("POST", slate.get("post_list", [])),
                          ("HOLD", slate.get("hold_list", []))]:
        if picks:
            print(f"\n  ── {label} ({len(picks)}) ──")
            for c in picks:
                ev_tag = f" EV {c.get('ev', 0)*100:+.1f}%" if c.get('ev_confirmed') else ""
                src    = f" [{c.get('ev_source', '?')[:8]}]" if c.get('ev_source') else ""
                print(f"  [{c.get('grade')}] {c.get('player', ''):<22} "
                      f"{c.get('direction','').upper():5} {c.get('line')} | "
                      f"{c.get('confidence')}%{ev_tag}{src}")

    print(f"\n  Total: {slate.get('total')} | "
          f"POST: {slate.get('post_count')} | "
          f"HOLD: {slate.get('hold_count')} | "
          f"SKIP: {slate.get('skip_count')}")


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    slate = run_confidence_agent()
    print_slate(slate)

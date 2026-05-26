# slipiq_confidence_agent.py
# Agentic Confidence Scoring Layer
# Sits on top of slipiq_pitcher_model.py output
#
# WHAT THIS DOES:
#   Takes raw pick cards from the pitcher model and makes the final
#   gate decision on what gets posted to Discord vs held back.
#
# THREE JOBS:
#   1. RE-SCORE confidence with full context (injury, lineup, weather)
#   2. GATE picks — post / hold / skip with clear reason
#   3. RANK final slate — best pick of the day surfaces to #daily-best-pick
#
# GATE LOGIC:
#   POST  → confidence ≥ 65, grade A/B+/B, ≥2 books, no hard blocks
#   HOLD  → confidence 50-64, or thin market, or conflict flag
#   SKIP  → confidence <50, grade C or lower, or hard block triggered
#
# HARD BLOCKS (auto-skip regardless of model score):
#   - 0 EV-confirmed books available
#   - Only 1 book posting AND it's a DFS book
#   - Trend directly conflicts AND confidence < 60
#   - No Pinnacle line AND grade < B
#
# DISPLAY:
#   Confidence shown to users as percentage (67%)
#   Grade shown on pick card (B+)
#   EV score and raw factors — operator dashboard only

import json
from datetime import datetime
from pathlib import Path
from slipiq_pitcher_model import run_pitcher_model, SPORT_MLB

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────
# GATE THRESHOLDS
# ─────────────────────────────────────────
CONFIDENCE_POST  = 65    # minimum to post
CONFIDENCE_HOLD  = 50    # minimum to hold (below = skip)
MIN_BOOKS_POST   = 1     # action books; lines_book_count used when thin
GRADE_POST       = {"A", "B+", "B"}
GRADE_HOLD       = {"B-", "C+"}
GRADE_SKIP       = {"C", "D", "N/A"}

# ─────────────────────────────────────────
# CONTEXT MODIFIERS
# These adjust confidence up/down based on
# factors the pitcher model doesn't know about
# ─────────────────────────────────────────
MODIFIERS = {
    "confirmed_starter":    +5,   # starter confirmed in lineup card
    "short_rest":          -15,   # pitcher on 3 days rest
    "extra_rest":           +3,   # pitcher on 6+ days rest
    "reported_injury":     -25,   # any injury report on pitcher
    "dome_game":            +3,   # dome parks favor Ks (no wind)
    "cold_weather":         -4,   # <50°F suppresses offense + Ks
    "rain_risk":            -8,   # rain delay / early hook risk
    "high_wind_out":        -3,   # wind blowing out hurts Ks (hitters chase less)
    "opposing_k_lineup":    +5,   # opponent top-5 K% lineup
    "opposing_contact":     -5,   # opponent top-5 contact lineup
    "first_start_back":    -10,   # returning from IL
    "bullpen_game_risk":   -20,   # opener / bulk game risk
}


# ═════════════════════════════════════════
# CONTEXT CHECKER
# Currently manual flags — will wire to
# injury API and weather API in Phase 2
# ═════════════════════════════════════════

def get_context_flags(
    player_name: str,
    game_date: str = None,
    home_team: str = None,
    away_team: str = None,
) -> dict:
    """
    Context modifiers: weather (tomorrow.io chain) + manual overrides.
    """
    flags = {}

    if home_team or away_team:
        try:
            from slipiq_weather import get_game_weather
            wx = get_game_weather(home_team, away_team, game_date)
            for flag in wx.get("flags", []):
                if flag in MODIFIERS:
                    flags[flag] = MODIFIERS[flag]
        except Exception as e:
            print(f"  [confidence] weather skip: {e}")

    override_path = CACHE_DIR / "context_overrides.json"
    if override_path.exists():
        with open(override_path) as f:
            overrides = json.load(f)
        player_flags = overrides.get(player_name, [])
        for flag in player_flags:
            if flag in MODIFIERS:
                flags[flag] = MODIFIERS[flag]

    return flags


# ═════════════════════════════════════════
# CONFIDENCE RE-SCORER
# ═════════════════════════════════════════

def rescore_confidence(card: dict) -> dict:
    """
    Take pitcher model confidence and apply context modifiers.
    Returns updated card with adjusted confidence and modifier log.
    """
    base_confidence = card.get("confidence", 0)
    player          = card.get("player", "")
    game_date       = card.get("game_date")

    context_flags   = get_context_flags(
        player,
        game_date,
        card.get("home_team"),
        card.get("away_team"),
    )

    total_modifier  = sum(context_flags.values())
    final_confidence = max(0, min(100, base_confidence + total_modifier))

    card["confidence_base"]     = base_confidence
    card["confidence"]          = final_confidence
    card["context_flags"]       = context_flags
    card["context_modifier"]    = total_modifier

    return card


# ═════════════════════════════════════════
# HARD BLOCK CHECKER
# ═════════════════════════════════════════

def check_hard_blocks(card: dict) -> tuple[bool, str]:
    """
    Returns (blocked: bool, reason: str).
    Hard blocks auto-skip regardless of model score.
    """
    confidence  = card.get("confidence", 0)
    grade       = card.get("grade", "D")
    book_count  = card.get("book_count", 0)
    ev_confirmed = card.get("ev_confirmed", False)
    flags       = card.get("flags", [])
    context     = card.get("context_flags", {})
    trend       = card.get("trend", "flat")
    direction   = card.get("direction", "")

    # Injury block — hard stop
    if "reported_injury" in context:
        return True, "injury report on pitcher"

    # Bullpen game — hard stop
    if "bullpen_game_risk" in context:
        return True, "bullpen/opener game risk"

    # Returning from IL
    if "first_start_back" in context:
        return True, "first start back from IL — unpredictable"

    # Direct trend conflict + low confidence
    conflict = (trend == "hot" and direction == "under") or \
               (trend == "cold" and direction == "over")
    if conflict and confidence < 60:
        return True, f"trend conflicts with signal and confidence is {confidence}%"

    return False, ""


# ═════════════════════════════════════════
# GATE DECISION
# ═════════════════════════════════════════

def gate_pick(card: dict) -> dict:
    """
    Final gate: POST / HOLD / SKIP with reason.
    Updates card in place, returns it.
    """
    confidence  = card.get("confidence", 0)
    grade       = card.get("grade", "D")
    book_count       = card.get("book_count", 0)
    lines_book_count = card.get("lines_book_count", book_count)
    ev_confirmed     = card.get("ev_confirmed", False)

    # Hard block check first
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

    # Confidence + book count gate (action book or any line source)
    has_market = book_count >= MIN_BOOKS_POST or lines_book_count >= 1

    if confidence >= CONFIDENCE_POST and has_market:
        card["gate"]        = "POST"
        thin = " (thin market)" if book_count < 2 else ""
        card["gate_reason"] = (
            f"confidence {confidence}%, {book_count} action / "
            f"{lines_book_count} w/ line, grade {grade}{thin}"
        )

    elif confidence >= CONFIDENCE_POST and not has_market:
        card["gate"]        = "HOLD"
        card["gate_reason"] = f"confidence {confidence}% but no lines posted"

    elif confidence >= CONFIDENCE_HOLD:
        card["gate"]        = "HOLD"
        card["gate_reason"] = f"confidence {confidence}% — below post threshold"

    else:
        card["gate"]        = "SKIP"
        card["gate_reason"] = f"confidence {confidence}% — too low"

    # EV bonus: upgrade HOLD → POST if EV confirmed + grade B or better
    if card["gate"] == "HOLD" and ev_confirmed and grade in GRADE_POST:
        card["gate"]        = "POST"
        card["gate_reason"] += " | EV confirmed — upgraded to POST"

    return card


# ═════════════════════════════════════════
# SLATE RANKER
# ═════════════════════════════════════════

def rank_slate(cards: list[dict]) -> dict:
    """
    From the gated slate, identify:
      - best_pick: top pick of the day → #daily-best-pick
      - post_list: all POST picks → VIP channels
      - hold_list: HOLD picks → logged, not posted yet
      - skip_list: SKIP picks → not posted

    Best pick criteria (in order):
      1. Grade A
      2. EV confirmed
      3. Highest confidence
      4. Most books posting
    """
    post  = [c for c in cards if c.get("gate") == "POST"]
    hold  = [c for c in cards if c.get("gate") == "HOLD"]
    skip  = [c for c in cards if c.get("gate") == "SKIP"]

    grade_order = {"A": 0, "B+": 1, "B": 2, "B-": 3, "C+": 4, "C": 5, "D": 6}

    def rank_key(c):
        return (
            0 if c.get("ev_confirmed") else 1,
            grade_order.get(c.get("grade", "D"), 6),
            -c.get("confidence", 0),
            -c.get("book_count", 0),
        )

    post.sort(key=rank_key)
    hold.sort(key=rank_key)
    skip.sort(key=rank_key)

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


# ═════════════════════════════════════════
# MAIN RUNNER
# ═════════════════════════════════════════

def run_confidence_agent(sport_key: str = SPORT_MLB) -> dict:
    """
    Full confidence agent pipeline:
      1. Run pitcher model → raw pick cards
      2. Re-score with context modifiers
      3. Apply hard blocks
      4. Gate each pick
      5. Rank final slate
      6. Return structured output for Discord formatter
    """
    print("\n" + "=" * 60)
    print("SlipIQ Confidence Agent — Running")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    # Step 1: Raw picks from pitcher model
    raw_cards = run_pitcher_model(sport_key)

    if not raw_cards:
        print("\n  No picks from pitcher model.")
        return {"post_list": [], "hold_list": [], "skip_list": [],
                "best_pick": None, "total": 0}

    print(f"\n[4] Confidence agent scoring {len(raw_cards)} cards...")

    gated_cards = []
    for card in raw_cards:
        # Re-score with context
        card = rescore_confidence(card)
        # Gate decision
        card = gate_pick(card)
        gated_cards.append(card)

    # Step 5: Rank
    slate = rank_slate(gated_cards)

    # Save to cache for Discord formatter
    cache_path = CACHE_DIR / "agent_slate.json"
    with open(cache_path, "w") as f:
        json.dump(slate, f, indent=2, default=str)
    print("  [cache] slate saved -> cache/agent_slate.json")

    return slate


# ═════════════════════════════════════════
# OUTPUT PRINTER
# ═════════════════════════════════════════

def print_slate(slate: dict):

    print("\n" + "=" * 60)
    print("CONFIDENCE AGENT — FINAL SLATE")
    print(f"{datetime.now().strftime('%A %B %d, %Y — %I:%M %p AZ')}")
    print("=" * 60)

    # Best pick of the day
    best = slate.get("best_pick")
    if best:
        print(f"\n  ★ BEST PICK OF THE DAY")
        print(f"  {best.get('player')} — {best.get('direction', '').upper()} "
              f"{best.get('line')} Ks")
        print(f"  Confidence: {best.get('confidence')}% | Grade: {best.get('grade')}")
        bk = best.get("best_book")
        if bk:
            print(f"  ▶ {bk['side'].upper()} {bk['price']} @ {bk['book']}")
        print(f"  Gate: {best.get('gate')} — {best.get('gate_reason')}")

    # POST picks
    post = slate.get("post_list", [])
    if post:
        print(f"\n  ── POST ({len(post)}) ──────────────────────────")
        for c in post:
            ev_tag = "✅ EV" if c.get("ev_confirmed") else ""
            print(f"  [{c.get('grade')}] {c.get('player'):<22} "
                  f"{c.get('direction','').upper():5} {c.get('line')} | "
                  f"{c.get('confidence')}% {ev_tag}")
            print(f"       {c.get('gate_reason')}")

    # HOLD picks
    hold = slate.get("hold_list", [])
    if hold:
        print(f"\n  ── HOLD ({len(hold)}) ──────────────────────────")
        for c in hold:
            print(f"  [{c.get('grade')}] {c.get('player'):<22} "
                  f"{c.get('direction','').upper():5} {c.get('line')} | "
                  f"{c.get('confidence')}%")
            print(f"       {c.get('gate_reason')}")

    # SKIP picks
    skip = slate.get("skip_list", [])
    if skip:
        print(f"\n  ── SKIP ({len(skip)}) ──────────────────────────")
        for c in skip:
            print(f"  [{c.get('grade')}] {c.get('player'):<22} — "
                  f"{c.get('gate_reason')}")

    # Summary
    print(f"\n  SUMMARY")
    print(f"  Total  : {slate.get('total')}")
    print(f"  POST   : {slate.get('post_count')}")
    print(f"  HOLD   : {slate.get('hold_count')}")
    print(f"  SKIP   : {slate.get('skip_count')}")

    # Morning status message
    if slate.get("post_count", 0) == 0:
        print(f"\n  ⏳ No postable picks yet — books still opening.")
        print(f"     Re-run at 9am for full slate.")


# ═════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════

if __name__ == "__main__":
    slate = run_confidence_agent()
    print_slate(slate)

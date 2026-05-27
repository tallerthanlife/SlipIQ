# slipiq_nba_confidence_agent.py
# NBA confidence scoring + gate layer (mirrors slipiq_confidence_agent.py)

import json
from datetime import datetime
from pathlib import Path

from slipiq_nba_player_model import run_nba_model

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

CONFIDENCE_POST = 65
CONFIDENCE_HOLD = 50
MIN_BOOKS_POST = 1
GRADE_POST = {"A+", "A", "B+", "B"}
GRADE_SKIP = {"C", "D", "N/A"}

MODIFIERS = {
    "b2b_game":              -12,
    "back_from_injury":      -20,
    "star_teammate_out":     +8,
    "blowout_risk":          -15,
    "foul_trouble_prone":    -5,
    "minutes_uncertain":     -10,
    "confirmed_starter":     +4,
    "role_expansion":        +6,
}


def get_nba_context_flags(card: dict) -> dict:
    flags = {}
    if card.get("b2b_flag"):
        flags["b2b_game"] = MODIFIERS["b2b_game"]

    internal = card.get("_internal") or {}
    spread = card.get("spread") or internal.get("spread")
    if spread is not None and abs(float(spread)) > 9:
        flags["blowout_risk"] = MODIFIERS["blowout_risk"]

    for f in card.get("flags", []):
        if "role expansion" in f.lower() or "OUT" in f:
            flags["star_teammate_out"] = MODIFIERS["star_teammate_out"]
            flags["role_expansion"] = MODIFIERS["role_expansion"]

    proj_min = card.get("projected_minutes") or 0
    mins_avg = internal.get("minutes_avg") or 0
    if mins_avg > 0 and proj_min < mins_avg * 0.85:
        flags["minutes_uncertain"] = MODIFIERS["minutes_uncertain"]

    override_path = CACHE_DIR / "context_overrides.json"
    if override_path.exists():
        try:
            with open(override_path) as f:
                overrides = json.load(f)
            player = card.get("player", "")
            for flag in overrides.get(player, []):
                if flag in MODIFIERS:
                    flags[flag] = MODIFIERS[flag]
        except Exception:
            pass

    return flags


def rescore_confidence(card: dict) -> dict:
    base = card.get("confidence", 0)
    ctx = get_nba_context_flags(card)
    modifier = sum(ctx.values())
    final = max(0, min(100, base + modifier))
    card["confidence_base"] = base
    card["confidence"] = final
    card["context_flags"] = ctx
    card["context_modifier"] = modifier
    return card


def check_hard_blocks(card: dict) -> tuple[bool, str]:
    ctx = card.get("context_flags", {})
    if "back_from_injury" in ctx:
        return True, "returning from injury"
    if "minutes_uncertain" in ctx and card.get("confidence", 0) < 58:
        return True, "minutes projection unreliable"
    trend = card.get("trend", "flat")
    direction = card.get("direction", "")
    conflict = (trend == "up" and direction == "under") or (trend == "down" and direction == "over")
    if conflict and card.get("confidence", 0) < 60:
        return True, "minutes trend conflicts with signal"
    return False, ""


def gate_pick(card: dict) -> dict:
    confidence = card.get("confidence", 0)
    grade = card.get("grade", "C")
    book_count = card.get("book_count", 0)
    lines_book_count = card.get("lines_book_count", book_count)
    ev_confirmed = card.get("ev_confirmed", False)

    blocked, reason = check_hard_blocks(card)
    if blocked:
        card["gate"] = "SKIP"
        card["gate_reason"] = f"hard block: {reason}"
        return card

    if grade in GRADE_SKIP:
        card["gate"] = "SKIP"
        card["gate_reason"] = f"grade {grade} below threshold"
        return card

    has_market = book_count >= MIN_BOOKS_POST or lines_book_count >= 1

    if confidence >= CONFIDENCE_POST and has_market:
        thin = " (thin market)" if book_count < 2 else ""
        card["gate"] = "POST"
        card["gate_reason"] = (
            f"confidence {confidence}%, {book_count} action / "
            f"{lines_book_count} w/ line, grade {grade}{thin}"
        )
    elif confidence >= CONFIDENCE_POST:
        card["gate"] = "HOLD"
        card["gate_reason"] = f"confidence {confidence}% but no lines posted"
    elif confidence >= CONFIDENCE_HOLD:
        card["gate"] = "HOLD"
        card["gate_reason"] = f"confidence {confidence}% — below post threshold"
    else:
        card["gate"] = "SKIP"
        card["gate_reason"] = f"confidence {confidence}% — too low"

    if card["gate"] == "HOLD" and ev_confirmed and grade in GRADE_POST:
        card["gate"] = "POST"
        card["gate_reason"] += " | EV confirmed — upgraded to POST"

    return card


def rank_slate(cards: list[dict]) -> dict:
    post = [c for c in cards if c.get("gate") == "POST"]
    hold = [c for c in cards if c.get("gate") == "HOLD"]
    skip = [c for c in cards if c.get("gate") == "SKIP"]

    grade_order = {"A+": 0, "A": 1, "B+": 2, "B": 3, "C": 4, "D": 5}

    def rank_key(c):
        return (
            0 if c.get("ev_confirmed") else 1,
            grade_order.get(c.get("grade", "D"), 5),
            -c.get("confidence", 0),
            -c.get("book_count", 0),
        )

    post.sort(key=rank_key)
    hold.sort(key=rank_key)
    skip.sort(key=rank_key)
    best = post[0] if post else (hold[0] if hold else None)

    return {
        "sport":      "nba",
        "best_pick":  best,
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


def run_nba_confidence_agent() -> dict:
    print("\n" + "=" * 60)
    print("SlipIQ NBA Confidence Agent — Running")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    raw_cards = run_nba_model()
    if not raw_cards:
        return {
            "sport": "nba", "post_list": [], "hold_list": [], "skip_list": [],
            "best_pick": None, "total": 0, "post_count": 0, "hold_count": 0, "skip_count": 0,
        }

    print(f"\n[4] NBA confidence agent scoring {len(raw_cards)} cards...")
    gated = []
    for card in raw_cards:
        card = rescore_confidence(card)
        card = gate_pick(card)
        gated.append(card)

    slate = rank_slate(gated)
    cache_path = CACHE_DIR / "nba_agent_slate.json"
    with open(cache_path, "w") as f:
        json.dump(slate, f, indent=2, default=str)
    print("  [cache] slate saved -> cache/nba_agent_slate.json")
    return slate


if __name__ == "__main__":
    slate = run_nba_confidence_agent()
    print(f"POST: {slate.get('post_count')} | HOLD: {slate.get('hold_count')} | SKIP: {slate.get('skip_count')}")

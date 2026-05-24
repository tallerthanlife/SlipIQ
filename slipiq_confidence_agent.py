"""
SlipIQ Confidence Agent
Agentic confidence scoring + hit-rate-aware grades.
Model computes edge first; Groq adjusts confidence; grade blends track record.
"""

import os
from slipiq_writer import call_groq
from slipiq_results import get_track_record_snapshot

MIN_SETTLED_FOR_HIT_RATE = 5


def _direction(pick):
    return "OVER" if "OVER" in pick.get("recommendation", "") else "UNDER"


def compute_ev_score(pick):
    """Operator-only expected value proxy (not shown on public cards)."""
    edge = abs(pick["projection"] - pick["line"])
    conf = pick.get("display_confidence", pick.get("model_confidence", pick["confidence"]))
    return round(edge * (conf / 100) * 10, 2)


def agentic_confidence(pick):
    """Groq second opinion on model confidence. Returns 1-99."""
    direction = _direction(pick)
    model_conf = pick.get("model_confidence", pick["confidence"])

    prompt = f"""
You are a sharp sports betting analyst reviewing a model's MLB strikeout prop pick.

Pitcher: {pick['pitcher']}
Pick: {direction} {pick['line']} K
Model Projection: {pick['projection']} K
Season Avg: {pick.get('season_avg', 'N/A')} K
Last 3 Starts Avg: {pick.get('last_3_avg', 'N/A')} K
Trend: {pick['trend']}
Model Confidence: {model_conf}%

Respond with ONLY one integer from 0 to 100 — your adjusted confidence. Nothing else.
"""
    result = call_groq(
        prompt,
        system_prompt="You output a single integer. No words.",
        max_tokens=10,
    )

    try:
        score = int("".join(filter(str.isdigit, result)))
        return min(99, max(1, score))
    except (TypeError, ValueError):
        return int(round(model_conf))


def _provisional_grade(confidence, edge):
    if confidence >= 70 and edge >= 1.0:
        return "A"
    if confidence >= 60 and edge >= 0.75:
        return "B"
    return "C"


def compute_grade(pick, track_record=None):
    """
    Grade from display confidence + edge + historical hit rates (when enough data).
    """
    track_record = track_record or get_track_record_snapshot()
    conf = pick.get("display_confidence", pick.get("confidence", 50))
    edge = abs(pick["projection"] - pick["line"])

    settled = track_record.get("settled_count", 0)
    if settled < MIN_SETTLED_FOR_HIT_RATE:
        return _provisional_grade(conf, edge)

    overall = track_record.get("overall_hit_rate", 50)
    trend_stats = track_record.get("by_trend", {}).get(pick.get("trend"), {})
    trend_rate = trend_stats.get("hit_rate", overall)
    direction_stats = track_record.get("by_direction", {}).get(_direction(pick), {})
    direction_rate = direction_stats.get("hit_rate", overall)

    edge_pts = min(edge / 2.0, 1.0) * 100
    composite = (
        conf * 0.45
        + overall * 0.20
        + trend_rate * 0.15
        + direction_rate * 0.10
        + edge_pts * 0.10
    )

    if composite >= 74 and conf >= 68 and edge >= 0.9:
        return "A"
    if composite >= 60 and conf >= 58:
        return "B"
    return "C"


def contextual_hit_rate_label(pick, track_record=None):
    """Short hit-rate string for Discord cards."""
    track_record = track_record or get_track_record_snapshot()
    if track_record.get("settled_count", 0) < MIN_SETTLED_FOR_HIT_RATE:
        return "Track record building"

    grade = pick.get("grade", "B")
    grade_stats = track_record.get("by_grade", {}).get(grade, {})
    if grade_stats.get("picks", 0) >= 2:
        return f"Grade {grade}: {grade_stats['hit_rate']}% ({grade_stats['wins']}/{grade_stats['picks']})"

    overall = track_record.get("overall_hit_rate")
    total = track_record.get("settled_count", 0)
    wins = track_record.get("total_wins", 0)
    return f"Overall: {overall}% ({wins}/{total})"


def format_recommendation(pick):
    direction = _direction(pick)
    conf = pick.get("display_confidence", pick["confidence"])
    grade = pick.get("grade", "C")
    return f"{direction} {pick['line']} | Grade: {grade} | Confidence: {conf}%"


def enrich_pick(pick, track_record=None, use_groq=None):
    """Apply agentic confidence, EV, hit-rate grade, and refresh recommendation string."""
    track_record = track_record or get_track_record_snapshot()

    pick["model_confidence"] = round(float(pick["confidence"]), 1)
    if use_groq is None:
        use_groq = bool(os.getenv("GROQ_API_KEY")) and not os.getenv("SLIPIQ_SKIP_AGENTIC")

    if use_groq:
        pick["display_confidence"] = agentic_confidence(pick)
    else:
        pick["display_confidence"] = int(round(pick["model_confidence"]))

    pick["confidence"] = pick["display_confidence"]
    pick["ev_score"] = compute_ev_score(pick)
    pick["grade"] = compute_grade(pick, track_record)
    pick["hit_rate_label"] = contextual_hit_rate_label(pick, track_record)
    pick["recommendation"] = format_recommendation(pick)
    return pick


def enrich_picks(picks, use_groq=None):
    """Enrich all picks and re-sort by display confidence."""
    if not picks:
        return picks

    track_record = get_track_record_snapshot()
    enriched = [enrich_pick(p, track_record, use_groq=use_groq) for p in picks]
    enriched.sort(key=lambda x: x["display_confidence"], reverse=True)
    return enriched


if __name__ == "__main__":
    sample = {
        "pitcher": "Test Pitcher",
        "line": 5.5,
        "projection": 7.0,
        "confidence": 72.0,
        "trend": "HOT",
        "season_avg": 6.1,
        "last_3_avg": 7.0,
        "recommendation": "OVER 5.5 | Grade: B | Confidence: 72%",
        "bookmaker": "FanDuel",
    }
    out = enrich_pick(sample, use_groq=False)
    print(f"Model: {out['model_confidence']}% → Display: {out['display_confidence']}%")
    print(f"Grade: {out['grade']} | EV: {out['ev_score']} | {out['hit_rate_label']}")
    print(out["recommendation"])

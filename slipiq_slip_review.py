"""
SlipIQ Slip Review — 6-step pre-bet checklist
Validates each pick before it ships to Discord / results.
"""

from slipiq_env import (
    SLIP_MIN_DISPLAY_CONF,
    SLIP_MIN_EDGE,
    SLIP_MIN_MODEL_CONF,
    SLIP_MIN_TRACK_RECORD,
)

MIN_EDGE = SLIP_MIN_EDGE
MIN_MODEL_CONF = SLIP_MIN_MODEL_CONF
MIN_DISPLAY_CONF = SLIP_MIN_DISPLAY_CONF
MIN_TRACK_RECORD_PCT = SLIP_MIN_TRACK_RECORD


def _direction(pick):
    return "OVER" if "OVER" in pick.get("recommendation", "") else "UNDER"


def _edge(pick):
    return abs(pick["projection"] - pick["line"])


def step_edge_check(pick):
    edge = _edge(pick)
    passed = edge >= MIN_EDGE
    return {
        "name": "1. Edge Check",
        "passed": passed,
        "detail": f"Projection {pick['projection']} vs line {pick['line']} (edge {edge:.1f} K)",
    }


def step_model_confidence(pick):
    conf = pick.get("model_confidence", pick.get("confidence", 0))
    passed = conf >= MIN_MODEL_CONF
    return {
        "name": "2. Model Confidence",
        "passed": passed,
        "detail": f"Model confidence {conf}% (min {MIN_MODEL_CONF}%)",
    }


def step_agentic_confidence(pick):
    conf = pick.get("display_confidence", pick.get("confidence", 0))
    passed = conf >= MIN_DISPLAY_CONF
    return {
        "name": "3. Agentic Confidence",
        "passed": passed,
        "detail": f"Display confidence {conf}% (min {MIN_DISPLAY_CONF}%)",
    }


def step_track_record(pick):
    label = pick.get("hit_rate_label", "")
    if "building" in label.lower():
        return {
            "name": "4. Track Record",
            "passed": True,
            "detail": "Track record still building — pass by default",
        }

    rate = None
    sample_size = 0
    if "%" in label:
        try:
            rate = float(label.split("%")[0].split()[-1])
            if "(" in label and "/" in label:
                parts = label.split("(")[-1].split(")")[0].split("/")
                sample_size = int(parts[1]) if len(parts) == 2 else 0
        except (ValueError, IndexError):
            rate = None

    # Need at least 5 samples before enforcing track record
    if rate is None or sample_size < 5:
        return {
            "name": "4. Track Record",
            "passed": True,
            "detail": label or "Insufficient sample — pass by default",
        }

    passed = rate >= MIN_TRACK_RECORD_PCT
    detail = f"{label} (min {MIN_TRACK_RECORD_PCT}%)"
    return {"name": "4. Track Record", "passed": passed, "detail": detail}


def step_trend_alignment(pick):
    direction = _direction(pick)
    trend = pick.get("trend", "NEUTRAL")
    if trend == "NEUTRAL":
        passed = True
        detail = "Neutral trend — no conflict"
    elif trend == "HOT":
        passed = direction == "OVER"
        detail = f"HOT trend vs {direction} pick — {'aligned' if passed else 'advisory only'}"
    else:  # COLD
        passed = direction == "UNDER"
        detail = f"COLD trend vs {direction} pick — {'aligned' if passed else 'advisory only'}"

    # Trend misalignment is advisory — never hard-kill a pick
    return {"name": "5. Trend Alignment", "passed": True, "detail": detail, "advisory": not passed}


def step_bankroll_gate(pick):
    grade = pick.get("grade", "C")
    units = {"A": 1.5, "B": 1.0, "C": 0.5}.get(grade, 0.5)
    passed = units >= 0.5
    return {
        "name": "6. Bankroll Gate",
        "passed": passed,
        "detail": f"Suggested size: {units}u (Grade {grade})",
        "units": units,
    }


def review_pick(pick):
    """Run 6-step checklist on one pick."""
    steps = [
        step_edge_check(pick),
        step_model_confidence(pick),
        step_agentic_confidence(pick),
        step_track_record(pick),
        step_trend_alignment(pick),
        step_bankroll_gate(pick),
    ]

    steps = [{**s, "passed": bool(s["passed"])} for s in steps]
    passed_count = sum(1 for s in steps if s["passed"])
    score = int(round(passed_count / len(steps) * 100))
    all_passed = passed_count == len(steps)
    units = float(next((s.get("units") for s in steps if s["name"] == "6. Bankroll Gate"), 0.5))

    return {
        "passed": bool(all_passed),
        "score": score,
        "steps_passed": int(passed_count),
        "steps_total": len(steps),
        "units": units,
        "steps": steps,
    }


def review_picks(picks, require_all_passed=False):
    """Review all picks; attach slip_review to each."""
    if not picks:
        return [], []

    reviewed = []
    for pick in picks:
        pick = dict(pick)
        pick["slip_review"] = review_pick(pick)
        reviewed.append(pick)

    approved = [p for p in reviewed if p["slip_review"]["passed"]]

    if require_all_passed and not approved:
        reviewed.sort(key=lambda p: p["slip_review"]["score"], reverse=True)
        approved = reviewed[:min(3, len(reviewed))]

    return reviewed, approved


def format_review_text(review):
    """Plain-text checklist for logs."""
    lines = [f"Score: {review['score']}% ({review['steps_passed']}/{review['steps_total']})"]
    for step in review["steps"]:
        mark = "PASS" if step["passed"] else "FAIL"
        lines.append(f"  [{mark}] {step['name']}: {step['detail']}")
    return "\n".join(lines)


def build_slip_review_embed(pick):
    """Discord embed for #slip-builder."""
    import discord

    review = pick.get("slip_review") or review_pick(pick)
    direction = _direction(pick)
    grade = pick.get("grade", "?")
    color = 0x00FF88 if review["passed"] else 0xFF6644

    embed = discord.Embed(
        title=f"Slip Review — {pick['pitcher']} {direction} {pick['line']} K",
        description=f"**{'APPROVED' if review['passed'] else 'CAUTION'}** | Checklist {review['score']}% | {review['units']}u suggested",
        color=color,
    )

    for step in review["steps"]:
        icon = "✅" if step["passed"] else "❌"
        embed.add_field(
            name=f"{icon} {step['name']}",
            value=step["detail"][:1024],
            inline=False,
        )

    conf = pick.get("display_confidence", pick["confidence"])
    embed.add_field(name="Grade", value=grade, inline=True)
    embed.add_field(name="Confidence", value=f"{conf}%", inline=True)
    embed.add_field(name="Projection", value=f"{pick['projection']} K", inline=True)
    embed.set_footer(text="SlipIQ • 6-Step Slip Review")
    return embed


if __name__ == "__main__":
    sample = {
        "pitcher": "Test Ace",
        "line": 5.5,
        "projection": 7.0,
        "confidence": 72,
        "model_confidence": 70,
        "display_confidence": 74,
        "grade": "B",
        "trend": "HOT",
        "recommendation": "OVER 5.5 | Grade: B | Confidence: 74%",
        "hit_rate_label": "Track record building",
    }
    r = review_pick(sample)
    print(format_review_text(r))
    print("Passed:", r["passed"])
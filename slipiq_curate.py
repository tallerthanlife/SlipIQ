"""
SlipIQ Curate — Morning curation
Selects the daily best pick for #daily-best-pick (free tier).
"""

GRADE_BONUS = {"A": 15, "B": 8, "C": 0}


def curation_score(pick):
    """Higher = stronger daily-best candidate."""
    conf = pick.get("display_confidence", pick.get("confidence", 0))
    grade = pick.get("grade", "C")
    edge = abs(pick["projection"] - pick["line"])
    return conf + GRADE_BONUS.get(grade, 0) + (edge * 2)


def select_daily_best(picks):
    """Return the single best pick for the free tier channel, or None."""
    if not picks:
        return None
    return max(picks, key=curation_score)


def daily_best_summary(pick):
    """One-line summary for logs and embeds."""
    direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
    return (
        f"{pick['pitcher']} — {direction} {pick['line']} K "
        f"(Grade {pick.get('grade')}, {pick.get('display_confidence', pick['confidence'])}%)"
    )


if __name__ == "__main__":
    test_picks = [
        {
            "pitcher": "Ace One",
            "line": 6.5,
            "projection": 8.0,
            "confidence": 80,
            "display_confidence": 82,
            "grade": "A",
            "recommendation": "OVER 6.5 | Grade: A | Confidence: 82%",
        },
        {
            "pitcher": "Ace Two",
            "line": 5.5,
            "projection": 6.0,
            "confidence": 75,
            "display_confidence": 76,
            "grade": "B",
            "recommendation": "OVER 5.5 | Grade: B | Confidence: 76%",
        },
    ]
    best = select_daily_best(test_picks)
    print("Daily best:", daily_best_summary(best))

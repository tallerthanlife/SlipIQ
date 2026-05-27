# slipiq_grading.py — shared grade thresholds (MLB + NBA)


def calc_grade(confidence: float, hit_rate: float = None) -> str:
    """
    Letter grade from model confidence (optional hit-rate tie-break later).
    Thresholds per PROJECT_BRAIN §2.
    """
    conf = float(confidence or 0)
    if conf >= 85:
        return "A+"
    if conf >= 72:
        return "A"
    if conf >= 65:
        return "B+"
    if conf >= 58:
        return "B"
    return "C"


if __name__ == "__main__":
    sample = {
        "legs": [
            {"confidence": 79, "ev_confirmed": True, "slip_review": {"score": 100}},
            {"confidence": 72, "ev_confirmed": True, "slip_review": {"score": 83}},
        ]
    }
    print(calc_slip_grade(sample))


def calc_slip_grade(slip: dict) -> dict:
    """
    Slip-level grade from leg confidence, review scores, and +EV ratio.
    Penalizes weak-link legs (min confidence weighs 30%).
    """
    legs = slip.get("legs") or []
    if not legs:
        return {"slip_grade": "C", "slip_score": 0.0}

    confidences = [float(l.get("confidence") or 0) for l in legs]
    avg_conf = sum(confidences) / len(confidences)
    min_conf = min(confidences)

    review_scores = []
    for leg in legs:
        review = leg.get("slip_review") or {}
        if review:
            review_scores.append(float(review.get("score") or 0))
        elif leg.get("review_score") is not None:
            review_scores.append(float(leg["review_score"]))
    review_rate = sum(review_scores) / len(review_scores) if review_scores else 100.0

    ev_count = sum(1 for l in legs if l.get("ev_confirmed"))
    ev_ratio = (ev_count / len(legs)) * 100.0

    composite = (
        avg_conf * 0.40
        + min_conf * 0.30
        + review_rate * 0.20
        + ev_ratio * 0.10
    )
    composite = round(composite, 1)

    return {
        "slip_grade": calc_grade(composite),
        "slip_score": composite,
        "avg_conf": round(avg_conf, 1),
        "min_conf": round(min_conf, 1),
        "ev_leg_ratio": round(ev_count / len(legs), 2),
    }

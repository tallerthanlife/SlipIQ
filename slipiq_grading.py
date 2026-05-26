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

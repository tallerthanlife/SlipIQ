# slipiq_calibration.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Model Calibration Tracker
#
# WHAT THIS DOES:
#   Logs every prediction the model makes, then after settlement
#   computes how well-calibrated it is.
#
# WHY IT MATTERS:
#   The EV engine is only as good as the probabilities fed into it.
#   A model that says "70% probability" must actually win 70% of the time.
#   If it wins 60% of the time when it says 70%, every EV calculation
#   is off by 10 percentage points — your edge is fiction.
#
# BRIER SCORE:
#   Perfect calibration = 0.0
#   Coin flip (random) = 0.25
#   Target: < 0.20 (better than random)
#   Good:   < 0.15
#
# WORKFLOW:
#   1. After pick is generated: log_prediction()
#   2. After game settles: log_result()
#   3. Nightly Sharp Review: calibration_summary() → posts to Discord
#   4. Use adjustment_factors() to correct systematic bias in models
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

CACHE_DIR      = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
CAL_LOG_FILE   = CACHE_DIR / "calibration_log.json"
CAL_BINS       = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.01]
CAL_BIN_LABELS = ["50-55%", "55-60%", "60-65%", "65-70%", "70-75%",
                  "75-80%", "80-85%", "85-90%", "90-95%", "95%+"]

# Supabase integration (optional — logs locally if no Supabase key)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — LOG MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def _load_log() -> list[dict]:
    if CAL_LOG_FILE.exists():
        try:
            return json.loads(CAL_LOG_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _save_log(records: list[dict]) -> None:
    CAL_LOG_FILE.write_text(json.dumps(records, indent=2))


def log_prediction(
    player:      str,
    market:      str,
    direction:   str,        # "over" | "under"
    line:        float,
    model_prob:  float,      # calibrated probability (0-1)
    book_odds:   int | None = None,  # American odds at time of pick
    ev:          float | None = None,
    grade:       str | None = None,
    sport:       str = "mlb",
    game_date:   str | None = None,
) -> str:
    """
    Log a model prediction before the game settles.
    Returns the prediction_id for later result logging.

    Call this immediately after a pick card is generated and posted.
    """
    pred_id  = f"{player.lower().replace(' ', '_')}_{market}_{direction}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    game_date = game_date or datetime.now().strftime("%Y-%m-%d")

    record = {
        "pred_id":    pred_id,
        "player":     player,
        "market":     market,
        "direction":  direction,
        "line":       line,
        "model_prob": round(model_prob, 6),
        "book_odds":  book_odds,
        "ev":         round(ev, 6) if ev is not None else None,
        "grade":      grade,
        "sport":      sport,
        "game_date":  game_date,
        "logged_at":  datetime.now().isoformat(),
        "result":     None,   # filled in by log_result()
        "settled":    False,
        "actual_val": None,   # actual stat value post-game
        "clv":        None,   # closing line value
    }

    records = _load_log()
    records.append(record)
    _save_log(records)

    # Mirror to Supabase if configured
    _supabase_upsert(record)

    return pred_id


def log_result(
    pred_id:    str,
    result:     str,         # "WIN" | "LOSS" | "PUSH" | "NO_ACTION"
    actual_val: float | None = None,
    clv:        float | None = None,   # CLV % from closing_line_value()
) -> bool:
    """
    Update an existing prediction with the game result.
    Returns True if found and updated, False if pred_id not found.

    Call this from slipiq_sharp_review.py after box scores are available.
    """
    records = _load_log()
    updated = False
    for rec in records:
        if rec["pred_id"] == pred_id:
            rec["result"]     = result
            rec["settled"]    = True
            rec["actual_val"] = actual_val
            rec["clv"]        = clv
            rec["settled_at"] = datetime.now().isoformat()
            updated = True
            break

    if updated:
        _save_log(records)
        _supabase_upsert(next(r for r in records if r["pred_id"] == pred_id))

    return updated


def log_result_by_player(
    player:     str,
    market:     str,
    direction:  str,
    game_date:  str,
    result:     str,
    actual_val: float | None = None,
    clv:        float | None = None,
) -> int:
    """
    Bulk update: find all predictions for a player/market/direction/date
    and mark them settled. Returns count of records updated.

    More convenient than tracking pred_ids in Sharp Review.
    """
    records = _load_log()
    count   = 0
    for rec in records:
        if (rec["player"].lower()    == player.lower()
                and rec["market"]    == market
                and rec["direction"] == direction
                and rec["game_date"] == game_date
                and not rec["settled"]):
            rec["result"]     = result
            rec["settled"]    = True
            rec["actual_val"] = actual_val
            rec["clv"]        = clv
            rec["settled_at"] = datetime.now().isoformat()
            count += 1

    if count:
        _save_log(records)

    return count


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — BRIER SCORE
# ═══════════════════════════════════════════════════════════════

def brier_score(
    records: list[dict] | None = None,
    days:    int = 30,
) -> dict:
    """
    Compute Brier Score over settled predictions.

    Brier Score = (1/N) × Σ (p_i - o_i)²
    where p_i = model probability, o_i = 1 if WIN else 0

    Lower = better. Perfect = 0.0. Random = 0.25.

    Args:
        records : list of prediction records (loads from file if None)
        days    : only include last N days

    Returns:
        {
            "brier_score" : float,
            "n"           : int,
            "rating"      : str,  # "EXCELLENT" | "GOOD" | "FAIR" | "POOR"
        }
    """
    if records is None:
        records = _load_log()

    cutoff  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    settled = [
        r for r in records
        if r.get("settled")
        and r.get("result") in ("WIN", "LOSS")
        and r.get("game_date", "") >= cutoff
        and r.get("model_prob") is not None
    ]

    if not settled:
        return {"brier_score": None, "n": 0, "rating": "NO_DATA"}

    bs = sum(
        (r["model_prob"] - (1.0 if r["result"] == "WIN" else 0.0)) ** 2
        for r in settled
    ) / len(settled)
    bs = round(bs, 6)

    if bs < 0.15:
        rating = "EXCELLENT"
    elif bs < 0.20:
        rating = "GOOD"
    elif bs < 0.25:
        rating = "FAIR"
    else:
        rating = "POOR — model overconfident or undercalibrated"

    return {"brier_score": bs, "n": len(settled), "rating": rating}


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — RELIABILITY CURVE
# ═══════════════════════════════════════════════════════════════

def reliability_curve(
    records: list[dict] | None = None,
    days:    int = 30,
) -> list[dict]:
    """
    Reliability (calibration) curve.
    Buckets predictions by probability band and shows actual hit rate.

    Perfect calibration: when model says 70%, actual hit rate = 70%.

    Returns:
        List of dicts, one per probability band:
        {
            "band"       : str,    # "65-70%"
            "predicted"  : float,  # midpoint of band
            "actual"     : float,  # actual win rate in this band
            "n"          : int,    # sample size
            "bias"       : float,  # actual - predicted (+ = underconfident)
        }
    """
    if records is None:
        records = _load_log()

    cutoff  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    settled = [
        r for r in records
        if r.get("settled")
        and r.get("result") in ("WIN", "LOSS")
        and r.get("game_date", "") >= cutoff
        and r.get("model_prob") is not None
    ]

    bins: dict[str, list] = {label: [] for label in CAL_BIN_LABELS}

    for r in settled:
        prob   = r["model_prob"]
        is_win = 1 if r["result"] == "WIN" else 0
        for i in range(len(CAL_BINS) - 1):
            if CAL_BINS[i] <= prob < CAL_BINS[i + 1]:
                bins[CAL_BIN_LABELS[i]].append(is_win)
                break

    curve = []
    for i, label in enumerate(CAL_BIN_LABELS):
        outcomes = bins[label]
        if not outcomes:
            continue
        predicted = (CAL_BINS[i] + CAL_BINS[i + 1]) / 2
        actual    = sum(outcomes) / len(outcomes)
        curve.append({
            "band":      label,
            "predicted": round(predicted, 4),
            "actual":    round(actual,    4),
            "n":         len(outcomes),
            "bias":      round(actual - predicted, 4),
        })

    return curve


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — ADJUSTMENT FACTORS
# ═══════════════════════════════════════════════════════════════

def adjustment_factors(
    records: list[dict] | None = None,
    days:    int = 30,
    min_n:   int = 10,
) -> dict[str, float]:
    """
    Per-band adjustment factors to correct systematic model bias.
    If the model says 70% but actual rate is 60%, adjustment = 60/70 = 0.857.
    Apply: corrected_prob = model_prob * adjustment_factor(band)

    Only return factors for bands with >= min_n samples.
    """
    curve = reliability_curve(records, days=days)
    factors = {}
    for band in curve:
        if band["n"] >= min_n and band["predicted"] > 0:
            factors[band["band"]] = round(band["actual"] / band["predicted"], 4)
    return factors


def apply_adjustment(
    model_prob: float,
    factors:    dict[str, float] | None = None,
) -> float:
    """
    Apply calibration adjustment to a model probability.
    If no factor exists for the band, return unadjusted probability.
    """
    if not factors:
        return model_prob

    for i in range(len(CAL_BINS) - 1):
        if CAL_BINS[i] <= model_prob < CAL_BINS[i + 1]:
            factor = factors.get(CAL_BIN_LABELS[i])
            if factor:
                adjusted = model_prob * factor
                return round(min(0.99, max(0.01, adjusted)), 6)
            break

    return model_prob


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — CALIBRATION SUMMARY (for Sharp Review + Discord)
# ═══════════════════════════════════════════════════════════════

def calibration_summary(days: int = 30) -> dict:
    """
    Full calibration report for nightly Sharp Review.
    Includes Brier score, reliability curve, and CLV summary.
    """
    records = _load_log()
    bs      = brier_score(records, days=days)
    curve   = reliability_curve(records, days=days)
    factors = adjustment_factors(records, days=days)

    # CLV summary
    clv_records = [r for r in records if r.get("clv") is not None and r.get("settled")]
    avg_clv     = round(sum(r["clv"] for r in clv_records) / len(clv_records), 3) if clv_records else None
    clv_positive = sum(1 for r in clv_records if r["clv"] > 0)
    clv_rate     = round(clv_positive / len(clv_records), 4) if clv_records else None

    # Win rate by grade
    grade_stats: dict[str, dict] = defaultdict(lambda: {"wins": 0, "total": 0})
    for r in records:
        if r.get("settled") and r.get("result") in ("WIN", "LOSS") and r.get("grade"):
            grade_stats[r["grade"]]["total"] += 1
            if r["result"] == "WIN":
                grade_stats[r["grade"]]["wins"] += 1

    grade_hit_rates = {
        g: round(v["wins"] / v["total"], 4) if v["total"] > 0 else None
        for g, v in grade_stats.items()
    }

    # Worst-calibrated band (highest absolute bias)
    worst_band = None
    if curve:
        worst = max(curve, key=lambda x: abs(x["bias"]))
        if abs(worst["bias"]) > 0.05:
            worst_band = worst

    return {
        "period_days":      days,
        "brier_score":      bs["brier_score"],
        "brier_rating":     bs["rating"],
        "n_settled":        bs["n"],
        "avg_clv_pct":      avg_clv,
        "clv_beat_rate":    clv_rate,
        "grade_hit_rates":  dict(grade_hit_rates),
        "adjustment_factors": factors,
        "worst_band":       worst_band,
        "reliability_curve": curve,
    }


def format_calibration_discord(summary: dict) -> str:
    """Format calibration summary for Discord posting."""
    bs     = summary.get("brier_score")
    rating = summary.get("brier_rating", "NO_DATA")
    n      = summary.get("n_settled", 0)
    clv    = summary.get("avg_clv_pct")
    clv_rt = summary.get("clv_beat_rate")

    lines = [
        "📊 **Model Calibration Report**",
        f"Period: last {summary.get('period_days', 30)} days | {n} settled picks",
        "",
    ]

    if bs is not None:
        emoji = "🟢" if rating == "EXCELLENT" else ("🟡" if rating in ("GOOD", "FAIR") else "🔴")
        lines.append(f"{emoji} Brier Score: **{bs:.4f}** ({rating})")
    else:
        lines.append("⚪ Brier Score: insufficient data")

    if clv is not None:
        clv_emoji = "✅" if clv > 0 else "❌"
        lines.append(f"{clv_emoji} Avg CLV: **{clv:+.2f}%** | Beat close: {clv_rt:.0%}")

    grades = summary.get("grade_hit_rates", {})
    if grades:
        lines.append("\nHit rates by grade:")
        for g in ("A+", "A", "B+", "B", "C"):
            hr = grades.get(g)
            if hr is not None:
                lines.append(f"  {g}: {hr:.0%}")

    worst = summary.get("worst_band")
    if worst:
        lines.append(
            f"\n⚠️ Worst-calibrated band: {worst['band']} "
            f"(predicted {worst['predicted']:.0%}, actual {worst['actual']:.0%})"
        )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — SUPABASE SYNC (optional)
# ═══════════════════════════════════════════════════════════════

def _supabase_upsert(record: dict) -> bool:
    """
    Mirror calibration record to Supabase.
    Silent fail if Supabase not configured.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False

    try:
        import requests
        base = SUPABASE_URL.rstrip("/")
        if base.endswith("/rest/v1"):
            base = base[:-len("/rest/v1")]
        url = f"{base}/rest/v1/calibration_log"
        headers = {
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "resolution=merge-duplicates",
        }
        resp = requests.post(url, json=record, headers=headers, timeout=5)
        return resp.status_code in (200, 201)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile, shutil

    print("=" * 60)
    print("SlipIQ — Calibration Tracker Self-Test")
    print("=" * 60)

    # Use temp dir to avoid polluting real cache
    orig_cache = CAL_LOG_FILE
    tmp_dir    = Path(tempfile.mkdtemp())
    import slipiq_calibration as _self
    _self.CAL_LOG_FILE = tmp_dir / "test_calibration_log.json"

    # Test 1: log predictions
    pred_ids = []
    test_data = [
        ("Gerrit Cole",   "player_pitcher_strikeouts", "over",  7.5, 0.72, "WIN"),
        ("Zack Wheeler",  "player_pitcher_strikeouts", "over",  6.5, 0.65, "LOSS"),
        ("Logan Webb",    "player_pitcher_strikeouts", "under", 5.5, 0.60, "WIN"),
        ("Pablo Lopez",   "player_pitcher_strikeouts", "over",  5.5, 0.58, "WIN"),
        ("Corbin Burnes", "player_pitcher_strikeouts", "over",  6.5, 0.70, "WIN"),
        ("Dylan Cease",   "player_pitcher_strikeouts", "over",  7.5, 0.75, "LOSS"),
        ("Max Fried",     "player_pitcher_strikeouts", "over",  6.5, 0.68, "WIN"),
        ("Chris Sale",    "player_pitcher_strikeouts", "over",  5.5, 0.62, "WIN"),
    ]

    for player, market, direction, line, prob, _ in test_data:
        pid = log_prediction(player, market, direction, line, prob, book_odds=-110, sport="mlb")
        pred_ids.append(pid)

    print(f"\n[1] Logged {len(pred_ids)} predictions")

    # Test 2: log results
    for i, (player, market, direction, line, prob, result) in enumerate(test_data):
        log_result(pred_ids[i], result, actual_val=line + 1)

    print(f"[2] Settled {len(test_data)} predictions")

    # Test 3: Brier score
    bs = brier_score(days=365)
    print(f"\n[3] Brier Score: {bs['brier_score']}  n={bs['n']}  rating={bs['rating']}")

    # Test 4: Reliability curve
    curve = reliability_curve(days=365)
    print(f"\n[4] Reliability curve ({len(curve)} bins with data):")
    for band in curve:
        print(f"    {band['band']}: predicted={band['predicted']:.0%}  actual={band['actual']:.0%}  n={band['n']}  bias={band['bias']:+.3f}")

    # Test 5: Summary + Discord format
    summary = calibration_summary(days=365)
    discord_text = format_calibration_discord(summary)
    print(f"\n[5] Discord format:\n{discord_text}")

    # Cleanup
    shutil.rmtree(tmp_dir)
    _self.CAL_LOG_FILE = orig_cache

    print("\n✓ Calibration tracker ready.")

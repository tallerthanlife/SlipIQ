# slipiq_lines.py
# Hit rate tracker + alt lines finder + line value tracker
# Feeds: slipiq_sharp_review.py, operator dashboard
#
# THREE JOBS:
#   1. HIT RATE TRACKER
#      Stores every pick result, calculates rolling hit rate
#      Breakdowns by: market, grade, confidence tier, direction
#
#   2. ALT LINES FINDER
#      Detects when a book posts a non-standard line
#      vs consensus — flags as alt line opportunity
#
#   3. LINE VALUE TRACKER
#      Compares line at pick time vs closing line
#      Feeds CLV calculation in slipiq_sharp_review.py

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

CACHE_DIR   = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

LINES_DB    = CACHE_DIR / "lines_db.json"
ALT_LOG     = CACHE_DIR / "alt_lines_log.json"
VALUE_LOG   = CACHE_DIR / "line_value_log.json"

# ─────────────────────────────────────────
# CONFIDENCE TIERS
# ─────────────────────────────────────────
CONFIDENCE_TIERS = {
    "elite":    (80, 100),
    "strong":   (65, 79),
    "moderate": (50, 64),
    "weak":     (0,  49),
}

def get_confidence_tier(confidence: int) -> str:
    for tier, (low, high) in CONFIDENCE_TIERS.items():
        if low <= confidence <= high:
            return tier
    return "weak"


# ═════════════════════════════════════════
# HIT RATE TRACKER
# ═════════════════════════════════════════

def load_lines_db() -> dict:
    """Load hit rate database."""
    if not LINES_DB.exists():
        return {
            "picks":        [],
            "by_market":    {},
            "by_grade":     {},
            "by_conf_tier": {},
            "by_direction": {},
            "last_updated": None,
        }
    with open(LINES_DB) as f:
        return json.load(f)


def save_lines_db(db: dict):
    db["last_updated"] = datetime.now().isoformat()
    with open(LINES_DB, "w") as f:
        json.dump(db, f, indent=2, default=str)


def record_pick_result(
    player: str,
    market: str,
    grade: str,
    confidence: int,
    direction: str,
    line: float,
    projection: float,
    actual: float,
    book: str = None,
    price: int = None,
    game_date: str = None,
) -> dict:
    """
    Record a pick result into the hit rate database.
    Called by slipiq_sharp_review.py after each game.
    """
    db = load_lines_db()

    # Determine outcome
    if direction == "over":
        hit  = actual > line
        push = actual == line
    else:
        hit  = actual < line
        push = actual == line

    outcome    = "HIT" if hit else ("PUSH" if push else "MISS")
    conf_tier  = get_confidence_tier(confidence)
    proj_error = round(abs(actual - projection), 2)

    entry = {
        "date":       game_date or datetime.now().strftime("%Y-%m-%d"),
        "player":     player,
        "market":     market,
        "grade":      grade,
        "confidence": confidence,
        "conf_tier":  conf_tier,
        "direction":  direction,
        "line":       line,
        "projection": projection,
        "actual":     actual,
        "outcome":    outcome,
        "hit":        hit,
        "push":       push,
        "proj_error": proj_error,
        "book":       book,
        "price":      price,
    }

    # Append to full picks list
    db["picks"].append(entry)

    # Update breakdowns
    for key, val in [
        ("by_market",    market),
        ("by_grade",     grade),
        ("by_conf_tier", conf_tier),
        ("by_direction", direction),
    ]:
        if val not in db[key]:
            db[key][val] = {"hits": 0, "misses": 0, "pushes": 0, "total": 0}
        db[key][val]["total"] += 1
        if hit:
            db[key][val]["hits"] += 1
        elif push:
            db[key][val]["pushes"] += 1
        else:
            db[key][val]["misses"] += 1

    save_lines_db(db)
    print(f"  [lines] Recorded: {player} {outcome}")
    return entry


def get_hit_rates(breakdown: str = "by_grade") -> dict:
    """
    Get hit rates for a specific breakdown.
    breakdown: 'by_market', 'by_grade', 'by_conf_tier', 'by_direction'
    Returns dict with hit rate per category.
    """
    db   = load_lines_db()
    data = db.get(breakdown, {})

    rates = {}
    for category, counts in data.items():
        decided = counts["hits"] + counts["misses"]
        if decided == 0:
            rates[category] = {"hit_rate": 0.0, **counts}
        else:
            rates[category] = {
                "hit_rate": round(counts["hits"] / decided, 4),
                **counts
            }

    return rates


def get_rolling_hit_rate(days: int = 30) -> dict:
    """
    Calculate hit rate over the last N days.
    Returns overall + by-grade breakdown.
    """
    db      = load_lines_db()
    cutoff  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent  = [p for p in db["picks"] if p.get("date", "") >= cutoff]

    if not recent:
        return {"total": 0, "hit_rate": 0.0, "period_days": days}

    hits   = sum(1 for p in recent if p["hit"])
    misses = sum(1 for p in recent if not p["hit"] and not p["push"])
    pushes = sum(1 for p in recent if p["push"])
    decided = hits + misses

    by_grade = defaultdict(lambda: {"hits": 0, "misses": 0})
    for p in recent:
        grade = p.get("grade", "?")
        if p["hit"]:
            by_grade[grade]["hits"] += 1
        elif not p["push"]:
            by_grade[grade]["misses"] += 1

    grade_rates = {}
    for grade, counts in by_grade.items():
        d = counts["hits"] + counts["misses"]
        grade_rates[grade] = round(counts["hits"] / d, 4) if d > 0 else 0.0

    return {
        "period_days": days,
        "total":       len(recent),
        "hits":        hits,
        "misses":      misses,
        "pushes":      pushes,
        "hit_rate":    round(hits / decided, 4) if decided > 0 else 0.0,
        "by_grade":    grade_rates,
    }


def get_model_accuracy(market: str = None) -> dict:
    """
    Analyze projection accuracy — avg error, by market.
    Useful for calibrating the model over time.
    """
    db     = load_lines_db()
    picks  = db["picks"]

    if market:
        picks = [p for p in picks if p.get("market") == market]

    if not picks:
        return {"total": 0, "avg_error": 0.0}

    errors = [p["proj_error"] for p in picks if p.get("proj_error") is not None]
    avg_error = round(sum(errors) / len(errors), 3) if errors else 0.0

    # Error buckets
    sharp  = sum(1 for e in errors if e <= 0.5)
    close  = sum(1 for e in errors if 0.5 < e <= 1.5)
    off    = sum(1 for e in errors if 1.5 < e <= 3.0)
    miss   = sum(1 for e in errors if e > 3.0)

    return {
        "total":     len(picks),
        "avg_error": avg_error,
        "buckets": {
            "sharp_≤0.5":  sharp,
            "close_≤1.5":  close,
            "off_≤3.0":    off,
            "miss_>3.0":   miss,
        },
        "market": market or "all",
    }


# ═════════════════════════════════════════
# ALT LINES FINDER
# ═════════════════════════════════════════

def find_alt_lines(aggregated_props: dict, threshold: float = 0.5) -> list[dict]:
    """
    Detect alt line opportunities.
    An alt line exists when one book posts a significantly
    different line than the consensus.

    aggregated_props: output from aggregate_by_player()
    threshold: minimum line difference to flag

    Returns list of alt line opportunities sorted by edge.
    """
    alts = []

    for (player, market), data in aggregated_props.items():
        consensus = data.get("line_consensus")
        all_lines = data.get("all_lines", [])

        if not consensus or len(all_lines) < 2:
            continue

        # Find lines that differ from consensus
        for entry in data.get("market_lines", []) + data.get("dfs_lines", []):
            book_line = entry.get("line")
            if book_line is None:
                continue

            diff = book_line - consensus
            if abs(diff) < threshold:
                continue

            # Alt line type
            if diff < 0:
                alt_type = "lower_line"   # book posting lower line = easier over
                edge_dir = "over"
            else:
                alt_type = "higher_line"  # book posting higher line = easier under
                edge_dir = "under"

            alts.append({
                "player":       player,
                "market":       market,
                "consensus":    consensus,
                "alt_line":     book_line,
                "diff":         round(diff, 2),
                "abs_diff":     round(abs(diff), 2),
                "alt_type":     alt_type,
                "edge_dir":     edge_dir,
                "book":         entry.get("book"),
                "book_title":   entry.get("book_title"),
                "over_price":   entry.get("over_price"),
                "under_price":  entry.get("under_price"),
                "game_date":    data.get("game_date"),
                "home_team":    data.get("home_team"),
                "away_team":    data.get("away_team"),
            })

    # Sort by abs_diff desc
    alts.sort(key=lambda x: -x["abs_diff"])

    # Log to cache
    if alts:
        log = {
            "timestamp": datetime.now().isoformat(),
            "count":     len(alts),
            "alts":      alts,
        }
        with open(ALT_LOG, "w") as f:
            json.dump(log, f, indent=2, default=str)
        print(f"  [lines] {len(alts)} alt lines found → alt_lines_log.json")

    return alts


def get_best_alt_lines(aggregated_props: dict, top_n: int = 5) -> list[dict]:
    """
    Return top N alt line opportunities.
    Filters to lines with actual betting value — needs a price attached.
    """
    alts = find_alt_lines(aggregated_props)
    # Only return alts that have odds attached
    actionable = [
        a for a in alts
        if a.get("over_price") is not None or a.get("under_price") is not None
    ]
    return actionable[:top_n]


# ═════════════════════════════════════════
# LINE VALUE TRACKER (CLV inputs)
# ═════════════════════════════════════════

def record_line_at_pick_time(
    player: str,
    market: str,
    line: float,
    direction: str,
    book: str,
    price: int,
    game_date: str = None,
) -> dict:
    """
    Record the line and price at the time of pick.
    Used later to calculate CLV when closing line is known.
    """
    if VALUE_LOG.exists():
        with open(VALUE_LOG) as f:
            log = json.load(f)
    else:
        log = []

    entry = {
        "timestamp":  datetime.now().isoformat(),
        "game_date":  game_date or datetime.now().strftime("%Y-%m-%d"),
        "player":     player,
        "market":     market,
        "line":       line,
        "direction":  direction,
        "book":       book,
        "price":      price,
        "closing_line": None,   # filled by sharp review
        "clv":          None,   # filled by sharp review
    }

    log.append(entry)

    # Keep last 200 entries
    log = log[-200:]

    with open(VALUE_LOG, "w") as f:
        json.dump(log, f, indent=2, default=str)

    return entry


def update_closing_line(player: str, market: str, game_date: str,
                         closing_line: float) -> bool:
    """
    Update a pick entry with the closing line.
    Called by slipiq_sharp_review.py after game.
    """
    if not VALUE_LOG.exists():
        return False

    with open(VALUE_LOG) as f:
        log = json.load(f)

    updated = False
    for entry in log:
        if (entry.get("player", "").lower() == player.lower() and
                entry.get("market") == market and
                entry.get("game_date") == game_date):

            entry["closing_line"] = closing_line

            # Calculate CLV
            direction = entry.get("direction", "")
            pick_line = entry.get("line")
            if pick_line is not None:
                if direction == "over":
                    clv = pick_line - closing_line   # lower line = better over
                else:
                    clv = closing_line - pick_line   # higher line = better under
                entry["clv"] = round(clv, 2)

            updated = True

    if updated:
        with open(VALUE_LOG, "w") as f:
            json.dump(log, f, indent=2, default=str)
        print(f"  [lines] CLV updated: {player} {game_date}")

    return updated


def get_clv_summary(days: int = 30) -> dict:
    """
    Summarize CLV performance over last N days.
    Positive avg CLV = consistently getting good numbers.
    """
    if not VALUE_LOG.exists():
        return {"total": 0, "avg_clv": 0.0}

    with open(VALUE_LOG) as f:
        log = json.load(f)

    cutoff  = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent  = [
        e for e in log
        if e.get("game_date", "") >= cutoff and e.get("clv") is not None
    ]

    if not recent:
        return {"total": 0, "avg_clv": 0.0, "period_days": days}

    clvs    = [e["clv"] for e in recent]
    avg_clv = round(sum(clvs) / len(clvs), 4)
    positive = sum(1 for c in clvs if c > 0)

    return {
        "period_days":    days,
        "total":          len(recent),
        "avg_clv":        avg_clv,
        "positive_clv":   positive,
        "positive_rate":  round(positive / len(recent), 4),
        "best_clv":       round(max(clvs), 2),
        "worst_clv":      round(min(clvs), 2),
    }


# ═════════════════════════════════════════
# STATS PRINTER
# ═════════════════════════════════════════

def print_performance_report():
    """Print full performance report to console."""
    print("\n" + "=" * 60)
    print("SlipIQ — Performance Report")
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    # Rolling hit rate
    rolling = get_rolling_hit_rate(30)
    print(f"\n  Last 30 days:")
    if rolling.get("total", 0) == 0:
        print(f"  No picks recorded yet")
    else:
        print(f"  {rolling['hits']}W {rolling['misses']}L {rolling['pushes']}P")
        print(f"  Hit rate: {rolling['hit_rate']:.1%}")

    if rolling.get("by_grade"):
        print(f"\n  By grade:")
        for grade, rate in sorted(rolling["by_grade"].items()):
            print(f"    [{grade}] {rate:.1%}")

    # Hit rates by market
    by_market = get_hit_rates("by_market")
    if by_market:
        print(f"\n  By market:")
        for market, data in sorted(by_market.items()):
            rate = data.get("hit_rate", 0)
            total = data.get("total", 0)
            print(f"    {market:<30} {rate:.1%} ({total} picks)")

    # Hit rates by confidence tier
    by_tier = get_hit_rates("by_conf_tier")
    if by_tier:
        print(f"\n  By confidence tier:")
        for tier in ["elite", "strong", "moderate", "weak"]:
            if tier in by_tier:
                data = by_tier[tier]
                rate = data.get("hit_rate", 0)
                total = data.get("total", 0)
                print(f"    {tier:<12} {rate:.1%} ({total} picks)")

    # CLV summary
    clv = get_clv_summary(30)
    if clv.get("total", 0) > 0:
        print(f"\n  CLV (last 30 days):")
        print(f"  Avg CLV: {clv['avg_clv']:+.4f}")
        print(f"  Positive CLV rate: {clv['positive_rate']:.1%}")

    # Model accuracy
    accuracy = get_model_accuracy()
    if accuracy.get("total", 0) > 0:
        print(f"\n  Model accuracy:")
        print(f"  Avg projection error: {accuracy['avg_error']} Ks")
        buckets = accuracy.get("buckets", {})
        for bucket, count in buckets.items():
            print(f"    {bucket}: {count}")

    db = load_lines_db()
    total_picks = len(db.get("picks", []))
    print(f"\n  Total picks recorded: {total_picks}")


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Lines Tracker Test")
    print("=" * 60)

    # 1. Alt lines — load from current props cache
    print("\n[1] Checking for alt lines in current props...")
    try:
        from slipiq_parlayapi import get_pitcher_strikeout_props, aggregate_by_player, SPORT_MLB
        props  = get_pitcher_strikeout_props(SPORT_MLB)
        agg    = aggregate_by_player(props)
        alts   = get_best_alt_lines(agg)
        if alts:
            print(f"  {len(alts)} alt line(s) found:")
            for a in alts[:3]:
                print(f"  {a['player']} — consensus {a['consensus']} | "
                      f"{a['book_title']} posting {a['alt_line']} "
                      f"({a['diff']:+.1f}) → {a['edge_dir'].upper()} edge")
        else:
            print("  No alt lines with current props (expected off-hours)")
    except Exception as e:
        print(f"  [error] {e}")

    # 2. Performance report (empty until picks flow)
    print("\n[2] Performance report:")
    print_performance_report()

    # 3. Simulate a pick result for testing
    print("\n[3] Simulating a pick result...")
    test_result = record_pick_result(
        player     = "Test Pitcher",
        market     = "player_pitcher_strikeouts",
        grade      = "B+",
        confidence = 68,
        direction  = "over",
        line       = 5.5,
        projection = 6.3,
        actual     = 7.0,
        book       = "DraftKings",
        price      = -115,
        game_date  = datetime.now().strftime("%Y-%m-%d"),
    )
    print(f"  Recorded: {test_result['outcome']}")

    # 4. Show updated stats
    print("\n[4] Updated performance after test pick:")
    rolling = get_rolling_hit_rate(30)
    print(f"  {rolling['hits']}W {rolling['misses']}L | "
          f"Hit rate: {rolling['hit_rate']:.1%}")

    print("\n✓ Lines tracker confirmed.")

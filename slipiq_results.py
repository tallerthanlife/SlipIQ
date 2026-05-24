"""
SlipIQ Results Tracker
Logs pick results and calculates hit rates
Stores everything to a local JSON file until Supabase is connected
"""

import json
import os
from datetime import datetime, date

RESULTS_FILE = "slipiq_results.json"

# ─── Load / Save ──────────────────────────────────────────────

def load_results():
    """Load all results from local file"""
    if not os.path.exists(RESULTS_FILE):
        return []
    with open(RESULTS_FILE, "r") as f:
        return json.load(f)

def save_results(results):
    """Save all results to local file"""
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

# ─── Log a Pick ───────────────────────────────────────────────

def log_pick(pick, result=None):
    """
    Log a pick to the results file
    result = 'WIN', 'LOSS', or None (pending)
    """
    results = load_results()
    today = date.today().strftime("%Y-%m-%d")

    direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
    grade = pick.get("grade") or pick["recommendation"].split("Grade: ")[-1].split(" |")[0].strip()

    for entry in results:
        if (
            entry["date"] == today
            and entry["pitcher"] == pick["pitcher"]
            and entry["line"] == pick["line"]
            and entry["direction"] == direction
        ):
            print(f"⏭️  Already logged: {pick['pitcher']} {direction} {pick['line']}")
            return entry

    entry = {
        "date": today,
        "pitcher": pick["pitcher"],
        "direction": direction,
        "line": pick["line"],
        "projection": pick["projection"],
        "grade": grade,
        "confidence": pick["confidence"],
        "trend": pick["trend"],
        "bookmaker": pick["bookmaker"],
        "result": result,
        "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    results.append(entry)
    save_results(results)
    print(f"✅ Logged: {pick['pitcher']} {direction} {pick['line']} — {result or 'PENDING'}")
    return entry

def update_result(pitcher, pick_date, result):
    """
    Update a pending pick with WIN or LOSS
    """
    results = load_results()

    for entry in results:
        if entry["pitcher"] == pitcher and entry["date"] == pick_date:
            entry["result"] = result
            entry["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            save_results(results)
            print(f"✅ Updated: {pitcher} on {pick_date} → {result}")
            return True

    print(f"❌ Pick not found: {pitcher} on {pick_date}")
    return False

# ─── Sharp Review ─────────────────────────────────────────────

def calculate_hit_rates():
    """
    Calculate win rates across all dimensions
    Returns full analytics dict
    """
    results = load_results()
    settled = [r for r in results if r["result"] in ["WIN", "LOSS"]]

    if not settled:
        print("No settled picks yet")
        return None

    total = len(settled)
    wins = len([r for r in settled if r["result"] == "WIN"])
    overall_rate = round(wins / total * 100, 1)

    # By grade
    grade_stats = {}
    for grade in ["A", "B", "C"]:
        grade_picks = [r for r in settled if r["grade"] == grade]
        if grade_picks:
            grade_wins = len([r for r in grade_picks if r["result"] == "WIN"])
            grade_stats[grade] = {
                "picks": len(grade_picks),
                "wins": grade_wins,
                "hit_rate": round(grade_wins / len(grade_picks) * 100, 1)
            }

    # By trend
    trend_stats = {}
    for trend in ["HOT", "COLD", "NEUTRAL"]:
        trend_picks = [r for r in settled if r["trend"] == trend]
        if trend_picks:
            trend_wins = len([r for r in trend_picks if r["result"] == "WIN"])
            trend_stats[trend] = {
                "picks": len(trend_picks),
                "wins": trend_wins,
                "hit_rate": round(trend_wins / len(trend_picks) * 100, 1)
            }

    # By direction
    direction_stats = {}
    for direction in ["OVER", "UNDER"]:
        dir_picks = [r for r in settled if r["direction"] == direction]
        if dir_picks:
            dir_wins = len([r for r in dir_picks if r["result"] == "WIN"])
            direction_stats[direction] = {
                "picks": len(dir_picks),
                "wins": dir_wins,
                "hit_rate": round(dir_wins / len(dir_picks) * 100, 1)
            }

    # By confidence range
    conf_stats = {}
    for label, low, high in [("60-69%", 60, 70), ("70-79%", 70, 80), ("80%+", 80, 100)]:
        conf_picks = [r for r in settled if low <= r["confidence"] < high]
        if conf_picks:
            conf_wins = len([r for r in conf_picks if r["result"] == "WIN"])
            conf_stats[label] = {
                "picks": len(conf_picks),
                "wins": conf_wins,
                "hit_rate": round(conf_wins / len(conf_picks) * 100, 1)
            }

    return {
        "total_picks": total,
        "total_wins": wins,
        "overall_hit_rate": overall_rate,
        "by_grade": grade_stats,
        "by_trend": trend_stats,
        "by_direction": direction_stats,
        "by_confidence": conf_stats,
        "pending": len([r for r in results if r["result"] is None])
    }

def print_sharp_review():
    """Print full sharp review to terminal"""
    stats = calculate_hit_rates()
    if not stats:
        return

    print("\n" + "="*52)
    print("SlipIQ SHARP REVIEW")
    print("="*52)
    print(f"Overall: {stats['total_wins']}/{stats['total_picks']} — {stats['overall_hit_rate']}% hit rate")
    print(f"Pending: {stats['pending']} picks")

    print("\n── By Grade ──")
    for grade, data in stats["by_grade"].items():
        print(f"  Grade {grade}: {data['wins']}/{data['picks']} — {data['hit_rate']}%")

    print("\n── By Trend ──")
    for trend, data in stats["by_trend"].items():
        print(f"  {trend}: {data['wins']}/{data['picks']} — {data['hit_rate']}%")

    print("\n── By Direction ──")
    for direction, data in stats["by_direction"].items():
        print(f"  {direction}: {data['wins']}/{data['picks']} — {data['hit_rate']}%")

    print("\n── By Confidence ──")
    for conf, data in stats["by_confidence"].items():
        print(f"  {conf}: {data['wins']}/{data['picks']} — {data['hit_rate']}%")

# ─── CLI ──────────────────────────────────────────────────────

def show_pending():
    """Show all picks still needing a result"""
    results = load_results()
    pending = [r for r in results if r["result"] is None]

    if not pending:
        print("No pending picks")
        return

    print(f"\n{len(pending)} pending picks:\n")
    for p in pending:
        print(f"  {p['date']} | {p['pitcher']} | {p['direction']} {p['line']} K | Grade {p['grade']}")

# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--pending" in sys.argv:
        show_pending()

    elif "--review" in sys.argv:
        print_sharp_review()

    elif "--update" in sys.argv:
        # Usage: py slipiq_results.py --update "Zac Gallen" "2026-05-23" WIN
        args = sys.argv
        if len(args) >= 5:
            update_result(args[2], args[3], args[4])
        else:
            print("Usage: py slipiq_results.py --update 'Pitcher Name' 'YYYY-MM-DD' WIN/LOSS")

    else:
        # Log today's picks automatically
        print("=== SlipIQ Results Tracker ===\n")
        from slipiq_lines import run_full_analysis
        picks = run_full_analysis()
        if picks:
            for pick in picks:
                log_pick(pick)
            print(f"\n✅ Logged {len(picks)} picks for today")
            show_pending()
        else:
            print("No picks today")
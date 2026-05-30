"""
SlipIQ Results Tracker
Logs picks and hit rates — Supabase when configured, else local JSON.
"""

from datetime import datetime, date

from slipiq_db import (
    load_results,
    save_results,
    save_results_json,
    upsert_pick_entry,
    pick_entry_from_log,
    is_configured,
    sync_json_to_supabase,
)


# ─── Log a Pick ───────────────────────────────────────────────

def log_pick(pick, result=None):
    """Log a pick (JSON + Supabase upsert)."""
    results = load_results()
    today = date.today().strftime("%Y-%m-%d")
    rec = pick.get("recommendation") or ""
    direction = "OVER" if "OVER" in rec else "UNDER"

    for entry in results:
        if (
            entry["date"] == today
            and entry["pitcher"] == pick["pitcher"]
            and entry["line"] == pick["line"]
            and entry["direction"] == direction
        ):
            print(f"Already logged: {pick['pitcher']} {direction} {pick['line']}")
            return entry

    entry = pick_entry_from_log(pick, result=result)
    results.append(entry)
    save_results(results)
    upsert_pick_entry(entry)
    print(f"Logged: {pick['pitcher']} {direction} {pick['line']} — {result or 'PENDING'}")
    return entry


def update_result(pitcher, pick_date, result, extra_fields=None):
    """Update a pending pick with WIN, LOSS, or PUSH."""
    results = load_results()
    extra_fields = extra_fields or {}

    for entry in results:
        if entry["pitcher"] == pitcher and entry["date"] == pick_date:
            entry["result"] = result
            entry["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            entry.update(extra_fields)
            save_results(results)
            upsert_pick_entry(entry)
            print(f"Updated: {pitcher} on {pick_date} -> {result}")
            return True

    print(f"Pick not found: {pitcher} on {pick_date}")
    return False


# ─── Track record (for grading + cards) ───────────────────────

def get_track_record_snapshot():
    results = load_results()
    settled = [r for r in results if r.get("result") in ("WIN", "LOSS")]

    if not settled:
        return {
            "settled_count": 0,
            "total_wins": 0,
            "overall_hit_rate": 50.0,
            "by_grade": {},
            "by_trend": {},
            "by_direction": {},
        }

    total = len(settled)
    wins = len([r for r in settled if r["result"] == "WIN"])
    overall = round(wins / total * 100, 1)

    by_grade = {}
    for grade in ("A", "B", "C"):
        rows = [r for r in settled if r.get("grade") == grade]
        if rows:
            w = len([r for r in rows if r["result"] == "WIN"])
            by_grade[grade] = {
                "picks": len(rows),
                "wins": w,
                "hit_rate": round(w / len(rows) * 100, 1),
            }

    by_trend = {}
    for trend in ("HOT", "COLD", "NEUTRAL"):
        rows = [r for r in settled if r.get("trend") == trend]
        if rows:
            w = len([r for r in rows if r["result"] == "WIN"])
            by_trend[trend] = {
                "picks": len(rows),
                "wins": w,
                "hit_rate": round(w / len(rows) * 100, 1),
            }

    by_direction = {}
    for direction in ("OVER", "UNDER"):
        rows = [r for r in settled if r.get("direction") == direction]
        if rows:
            w = len([r for r in rows if r["result"] == "WIN"])
            by_direction[direction] = {
                "picks": len(rows),
                "wins": w,
                "hit_rate": round(w / len(rows) * 100, 1),
            }

    return {
        "settled_count": total,
        "total_wins": wins,
        "overall_hit_rate": overall,
        "by_grade": by_grade,
        "by_trend": by_trend,
        "by_direction": by_direction,
    }


# ─── Sharp Review stats ───────────────────────────────────────

def calculate_hit_rates(silent=False):
    results = load_results()
    settled = [r for r in results if r.get("result") in ("WIN", "LOSS")]

    if not settled:
        if not silent:
            print("No settled picks yet")
        return None

    total = len(settled)
    wins = len([r for r in settled if r["result"] == "WIN"])
    overall_rate = round(wins / total * 100, 1)

    grade_stats = {}
    for grade in ["A", "B", "C"]:
        grade_picks = [r for r in settled if r.get("grade") == grade]
        if grade_picks:
            grade_wins = len([r for r in grade_picks if r["result"] == "WIN"])
            grade_stats[grade] = {
                "picks": len(grade_picks),
                "wins": grade_wins,
                "hit_rate": round(grade_wins / len(grade_picks) * 100, 1),
            }

    trend_stats = {}
    for trend in ["HOT", "COLD", "NEUTRAL"]:
        trend_picks = [r for r in settled if r.get("trend") == trend]
        if trend_picks:
            trend_wins = len([r for r in trend_picks if r["result"] == "WIN"])
            trend_stats[trend] = {
                "picks": len(trend_picks),
                "wins": trend_wins,
                "hit_rate": round(trend_wins / len(trend_picks) * 100, 1),
            }

    direction_stats = {}
    for direction in ["OVER", "UNDER"]:
        dir_picks = [r for r in settled if r.get("direction") == direction]
        if dir_picks:
            dir_wins = len([r for r in dir_picks if r["result"] == "WIN"])
            direction_stats[direction] = {
                "picks": len(dir_picks),
                "wins": dir_wins,
                "hit_rate": round(dir_wins / len(dir_picks) * 100, 1),
            }

    conf_stats = {}
    for label, low, high in [("60-69%", 60, 70), ("70-79%", 70, 80), ("80%+", 80, 100)]:
        conf_picks = [r for r in settled if low <= r.get("confidence", 0) < high]
        if conf_picks:
            conf_wins = len([r for r in conf_picks if r["result"] == "WIN"])
            conf_stats[label] = {
                "picks": len(conf_picks),
                "wins": conf_wins,
                "hit_rate": round(conf_wins / len(conf_picks) * 100, 1),
            }

    return {
        "total_picks": total,
        "total_wins": wins,
        "overall_hit_rate": overall_rate,
        "by_grade": grade_stats,
        "by_trend": trend_stats,
        "by_direction": direction_stats,
        "by_confidence": conf_stats,
        "pending": len([r for r in results if r.get("result") is None]),
    }


def print_sharp_review():
    stats = calculate_hit_rates()
    if not stats:
        return

    print("\n" + "=" * 52)
    print("SlipIQ SHARP REVIEW")
    print("=" * 52)
    print(f"Overall: {stats['total_wins']}/{stats['total_picks']} — {stats['overall_hit_rate']}% hit rate")
    print(f"Pending: {stats['pending']} picks")
    storage = "Supabase" if is_configured() else "local JSON"
    print(f"Storage: {storage}")

    for label, key in [
        ("By Grade", "by_grade"),
        ("By Trend", "by_trend"),
        ("By Direction", "by_direction"),
        ("By Confidence", "by_confidence"),
    ]:
        print(f"\n-- {label} --")
        for name, data in stats.get(key, {}).items():
            print(f"  {name}: {data['wins']}/{data['picks']} — {data['hit_rate']}%")


def show_pending():
    results = load_results()
    pending = [r for r in results if r.get("result") is None]

    if not pending:
        print("No pending picks")
        return

    print(f"\n{len(pending)} pending picks:\n")
    for p in pending:
        print(f"  {p['date']} | {p['pitcher']} | {p['direction']} {p['line']} K | Grade {p.get('grade')}")


if __name__ == "__main__":
    import sys

    if "--pending" in sys.argv:
        show_pending()
    elif "--review" in sys.argv:
        print_sharp_review()
    elif "--sync" in sys.argv:
        sync_json_to_supabase()
    elif "--update" in sys.argv:
        args = sys.argv
        if len(args) >= 5:
            update_result(args[2], args[3], args[4])
        else:
            print("Usage: py slipiq_results.py --update 'Pitcher' 'YYYY-MM-DD' WIN|LOSS")
    else:
        print("=== SlipIQ Results Tracker ===\n")
        from slipiq_lines import run_full_analysis

        picks = run_full_analysis()
        if picks:
            for pick in picks:
                log_pick(pick)
            print(f"\nLogged {len(picks)} picks for today")
            show_pending()
        else:
            print("No picks today")


def get_track_record_snapshot() -> dict:
    """
    Return a summary dict of the all-time record.
    Called by slipiq_chat_pool.py when building pick pools.
    """
    try:
        stats = calculate_hit_rates(silent=True)
        if not stats:
            return {"total": 0, "hit_rate": 0.0, "label": "No record yet"}
        return {
            "total":      stats.get("settled_count", 0),
            "wins":       stats.get("total_wins", 0),
            "hit_rate":   stats.get("overall_hit_rate", 0.0),
            "by_grade":   stats.get("by_grade", {}),
            "label":      f"{stats.get('total_wins', 0)}W / {stats.get('settled_count', 0)} settled",
        }
    except Exception as e:
        print(f"  [results] track record error: {e}")
        return {"total": 0, "hit_rate": 0.0, "label": "No record yet"}

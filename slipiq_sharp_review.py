# slipiq_sharp_review.py
# Post-game CLV grader + Sharp Review
# Reads cache/latest_picks.json (written by slipiq_curate.py)
# Posts results to #sharp-review on Discord
#
# WHAT THIS DOES:
#   1. Load today's picks from cache
#   2. Fetch actual game results (Statcast / BallDontLie)
#   3. Grade each pick: HIT / MISS / PUSH
#   4. Calculate CLV — did we beat the closing line?
#   5. Score the model — was the projection accurate?
#   6. Post Sharp Review embed to #sharp-review
#   7. Update running record (hit rate, ROI, CLV+/-)
#
# CLV GRADING:
#   CLV = closing line vs line we got
#   Positive CLV = we got a better number than closing → sharp
#   Negative CLV = line moved against us → square
#   CLV matters more than individual hit/miss — it measures process
#
# RECORD TRACKING:
#   Stored in cache/record.json
#   Tracks: total picks, hits, misses, pushes, ROI, avg CLV
#   Resets never — cumulative all-time record

import json
from datetime import datetime, timedelta
from pathlib import Path

import pybaseball as pyb
import pandas as pd

from slipiq_discord import post_sharp_review, post_message
from slipiq_parlayapi import fetch_historical, SPORT_MLB

from slipiq_env import DISCORD_SHARP_REVIEW_CHANNEL

CACHE_DIR  = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

RECORD_PATH  = CACHE_DIR / "record.json"
PICKS_PATH   = CACHE_DIR / "latest_picks.json"


# ═════════════════════════════════════════
# RECORD MANAGER
# ═════════════════════════════════════════

def load_record() -> dict:
    """Load cumulative record from cache."""
    if not RECORD_PATH.exists():
        return {
            "total":       0,
            "hits":        0,
            "misses":      0,
            "pushes":      0,
            "clv_total":   0.0,
            "roi_total":   0.0,
            "streak":      0,
            "streak_type": None,   # "W" or "L"
            "best_streak": 0,
            "last_updated": None,
            "history":     [],
        }
    with open(RECORD_PATH) as f:
        return json.load(f)


def save_record(record: dict):
    """Save cumulative record to cache."""
    record["last_updated"] = datetime.now().isoformat()
    with open(RECORD_PATH, "w") as f:
        json.dump(record, f, indent=2, default=str)


def update_record(record: dict, result: dict) -> dict:
    """
    Add one pick result to the running record.
    result dict: {player, hit, push, clv, roi, grade}
    """
    record["total"] += 1

    if result.get("push"):
        record["pushes"] += 1
        record["streak"] = 0
        record["streak_type"] = None
    elif result.get("hit"):
        record["hits"] += 1
        if record["streak_type"] == "W":
            record["streak"] += 1
        else:
            record["streak"] = 1
            record["streak_type"] = "W"
        record["best_streak"] = max(record["best_streak"], record["streak"])
    else:
        record["misses"] += 1
        if record["streak_type"] == "L":
            record["streak"] += 1
        else:
            record["streak"] = 1
            record["streak_type"] = "L"

    clv = result.get("clv", 0) or 0
    roi = result.get("roi", 0) or 0
    record["clv_total"]  += clv
    record["roi_total"]  += roi

    # Append to history (keep last 100)
    record["history"].append({
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "player":  result.get("player"),
        "line":    result.get("line"),
        "actual":  result.get("actual_ks"),
        "proj":    result.get("proj"),
        "hit":     result.get("hit"),
        "clv":     clv,
        "grade":   result.get("grade"),
    })
    record["history"] = record["history"][-100:]

    return record


def hit_rate(record: dict) -> float:
    """Win rate excluding pushes."""
    decided = record["hits"] + record["misses"]
    if decided == 0:
        return 0.0
    return round(record["hits"] / decided, 4)


def avg_clv(record: dict) -> float:
    """Average CLV per pick."""
    if record["total"] == 0:
        return 0.0
    return round(record["clv_total"] / record["total"], 4)


# ═════════════════════════════════════════
# RESULT FETCHER
# ═════════════════════════════════════════

def fetch_pitcher_actual_ks(player_name: str, game_date: str) -> int | None:
    """
    Fetch actual strikeout total for a pitcher on a given date.
    Source: Statcast via pybaseball.
    Returns K total or None if not found.
    """
    try:
        # Lookup MLBAM ID
        parts  = player_name.strip().split()
        last   = parts[-1]
        first  = parts[0]
        lookup = pyb.playerid_lookup(last, first)

        if lookup.empty:
            print(f"  [sharp] Player not found: {player_name}")
            return None

        mlbam_id = int(lookup.iloc[0]["key_mlbam"])

        # Pull Statcast for that date
        log = pyb.statcast_pitcher(
            start_dt=game_date,
            end_dt=game_date,
            player_id=mlbam_id
        )

        if log is None or log.empty:
            print(f"  [sharp] No Statcast data for {player_name} on {game_date}")
            return None

        # Count strikeout events
        ks = (log["events"] == "strikeout").sum()
        print(f"  [sharp] {player_name} on {game_date}: {ks} Ks")
        return int(ks)

    except Exception as e:
        print(f"  [sharp] Error fetching {player_name}: {e}")
        return None


def fetch_closing_line(player_name: str, market_key: str, game_date: str) -> float | None:
    """
    Fetch closing line from ParlayAPI historical endpoint.
    Used for CLV calculation.
    """
    try:
        historical = fetch_historical(SPORT_MLB, date=game_date)
        if not historical:
            return None

        for entry in historical:
            if (entry.get("player", "").lower() == player_name.lower() and
                    entry.get("market_key", "").lower() == market_key.lower() and
                    entry.get("bookmaker", "").lower() == "pinnacle"):
                return entry.get("line")

        return None

    except Exception as e:
        print(f"  [sharp] Closing line fetch failed: {e}")
        return None


# ═════════════════════════════════════════
# GRADER
# ═════════════════════════════════════════

def grade_pick(card: dict, actual_ks: int, closing_line: float = None) -> dict:
    """
    Grade a single pick.
    Returns result dict with hit/miss/push, CLV, model accuracy.
    """
    player    = card.get("player")
    line      = card.get("line")
    direction = card.get("direction", "")
    proj      = card.get("projection")
    internal = card.get("_internal") or {}
    pick_line = internal.get("pinnacle_over") or (
        card.get("best_book", {}).get("price") if card.get("best_book") else None
    )
    grade     = card.get("grade", "?")

    # Hit/miss/push
    if direction == "over":
        if actual_ks > line:
            outcome = "HIT"
            hit, push = True, False
        elif actual_ks == line:
            outcome = "PUSH"
            hit, push = False, True
        else:
            outcome = "MISS"
            hit, push = False, False
    else:  # under
        if actual_ks < line:
            outcome = "HIT"
            hit, push = True, False
        elif actual_ks == line:
            outcome = "PUSH"
            hit, push = False, True
        else:
            outcome = "MISS"
            hit, push = False, False

    # Model accuracy — how far off was the projection?
    proj_error = round(abs(actual_ks - proj), 2) if proj else None
    proj_tag   = None
    if proj_error is not None:
        if proj_error <= 0.5:
            proj_tag = "🎯 Sharp"
        elif proj_error <= 1.5:
            proj_tag = "✅ Close"
        elif proj_error <= 3.0:
            proj_tag = "⚠️ Off"
        else:
            proj_tag = "❌ Miss"

    # CLV calculation
    clv = None
    if closing_line is not None and line is not None:
        if direction == "over":
            clv = round(line - closing_line, 2)   # positive = got lower line = good
        else:
            clv = round(closing_line - line, 2)   # positive = got higher line = good

    # Sharp Review grade
    if outcome == "HIT" and clv and clv > 0:
        sr_grade = "A"   # hit AND beat closing
    elif outcome == "HIT":
        sr_grade = "B"   # hit but no CLV data
    elif outcome == "PUSH":
        sr_grade = "C"
    elif outcome == "MISS" and clv and clv > 0:
        sr_grade = "B-"  # miss but beat closing — good process, bad result
    else:
        sr_grade = "D"

    return {
        "player":      player,
        "line":        line,
        "direction":   direction,
        "actual_ks":   actual_ks,
        "proj":        proj,
        "outcome":     outcome,
        "hit":         hit,
        "push":        push,
        "clv":         clv,
        "proj_error":  proj_error,
        "proj_tag":    proj_tag,
        "sr_grade":    sr_grade,
        "model_grade": grade,
        "roi":         1.0 if hit else (-1.0 if not push else 0.0),
    }


# ═════════════════════════════════════════
# SHARP REVIEW RUNNER
# ═════════════════════════════════════════

def run_sharp_review(game_date: str = None, post_to_discord: bool = True) -> list[dict]:
    """
    Full Sharp Review pipeline for a given date.
    Defaults to yesterday's picks.
    """
    if not game_date:
        game_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print("\n" + "=" * 60)
    print("SlipIQ Sharp Review")
    print(f"Grading: {game_date}")
    print("=" * 60)

    # Load picks
    date_log = CACHE_DIR / f"slate_{game_date.replace('-', '')}.json"
    if date_log.exists():
        with open(date_log) as f:
            slate_log = json.load(f)
    elif PICKS_PATH.exists():
        with open(PICKS_PATH) as f:
            slate_log = json.load(f)
    else:
        print("  No picks found to grade.")
        return []

    top_picks = slate_log.get("top_picks", [])
    if not top_picks:
        print("  No picks were posted today.")
        return []

    print(f"\n  Grading {len(top_picks)} picks from {game_date}...")

    # Load record
    record  = load_record()
    results = []

    for card in top_picks:
        player    = card.get("player")
        line      = card.get("line")
        direction = card.get("direction")
        proj      = card.get("projection")
        best_book = card.get("best_book", {}) or {}
        book_name = best_book.get("book")
        book_price = best_book.get("price")

        print(f"\n  [{card.get('grade')}] {player} — "
              f"{direction.upper() if direction else '?'} {line}")

        # Fetch actual result
        actual_ks = fetch_pitcher_actual_ks(player, game_date)

        if actual_ks is None:
            print(f"  [sharp] Skipping {player} — no result data yet")
            continue

        # Fetch closing line for CLV
        closing_line = fetch_closing_line(player, "player_pitcher_strikeouts", game_date)

        # Grade it
        result = grade_pick(card, actual_ks, closing_line)
        results.append(result)

        # Update record
        record = update_record(record, result)

        print(f"  Result : {result['outcome']} "
              f"({actual_ks} Ks vs {line} line)")
        print(f"  Proj   : {proj} → error {result['proj_error']} {result['proj_tag']}")
        if result["clv"] is not None:
            print(f"  CLV    : {result['clv']:+.2f}")

        # Post to Discord
        if post_to_discord:
            post_sharp_review(
                player        = player,
                pick_direction = direction,
                line          = line,
                actual_ks     = actual_ks,
                proj          = proj,
                grade         = result["sr_grade"],
                clv           = result["clv"],
                book          = book_name,
                book_price    = book_price,
                closing_price = int(closing_line) if closing_line else None,
            )

    # Save updated record
    save_record(record)

    # Summary
    _print_summary(results, record)

    # Post summary to Discord
    if post_to_discord and results:
        _post_summary_to_discord(results, record, game_date)

    return results


# ═════════════════════════════════════════
# SUMMARY BUILDERS
# ═════════════════════════════════════════

def _print_summary(results: list[dict], record: dict):
    hits   = sum(1 for r in results if r["hit"])
    misses = sum(1 for r in results if not r["hit"] and not r["push"])
    pushes = sum(1 for r in results if r["push"])
    clv_avg = (
        sum(r["clv"] for r in results if r["clv"] is not None) /
        max(len([r for r in results if r["clv"] is not None]), 1)
    )

    print("\n" + "=" * 60)
    print("SHARP REVIEW SUMMARY")
    print("=" * 60)
    print(f"  Today    : {hits}W {misses}L {pushes}P")
    print(f"  Avg CLV  : {clv_avg:+.2f}")
    print(f"\n  All-time record:")
    print(f"  {record['hits']}W {record['misses']}L {record['pushes']}P")
    print(f"  Hit rate : {hit_rate(record):.1%}")
    print(f"  Avg CLV  : {avg_clv(record):+.4f}")
    print(f"  Streak   : {record['streak']} {record.get('streak_type', '')}")


def _post_summary_to_discord(results: list[dict], record: dict, game_date: str):
    """Post daily summary card to #sharp-review."""
    if not DISCORD_SHARP_REVIEW_CHANNEL:
        return

    hits   = sum(1 for r in results if r["hit"])
    misses = sum(1 for r in results if not r["hit"] and not r["push"])
    pushes = sum(1 for r in results if r["push"])
    clvs   = [r["clv"] for r in results if r["clv"] is not None]
    clv_avg = sum(clvs) / len(clvs) if clvs else None

    streak = record.get("streak", 0)
    stype  = record.get("streak_type", "")
    streak_str = f"{streak}{stype}" if streak > 0 else "—"

    clv_str = f"{clv_avg:+.2f}" if clv_avg is not None else "N/A"

    embed = {
        "title":       f"🔍 Sharp Review — {game_date}",
        "description": f"**{hits}W {misses}L {pushes}P** today | CLV avg: {clv_str}",
        "color":       0x00FF88 if hits >= misses else 0xFF2200,
        "fields": [
            {
                "name":   "All-Time Record",
                "value":  f"{record['hits']}W {record['misses']}L {record['pushes']}P",
                "inline": True,
            },
            {
                "name":   "Hit Rate",
                "value":  f"{hit_rate(record):.1%}",
                "inline": True,
            },
            {
                "name":   "Avg CLV",
                "value":  f"{avg_clv(record):+.4f}",
                "inline": True,
            },
            {
                "name":   "Current Streak",
                "value":  streak_str,
                "inline": True,
            },
        ],
        "footer":    {"text": "SlipIQ Sharp Review • Process over results."},
        "timestamp": datetime.utcnow().isoformat(),
    }

    post_message(DISCORD_SHARP_REVIEW_CHANNEL, embed=embed)


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # Pass a date to grade a specific day: python slipiq_sharp_review.py 2026-05-25
    # Default: yesterday
    date_arg    = next((a for a in sys.argv[1:] if a.startswith("20")), None)
    no_discord  = "--no-discord" in sys.argv

    results = run_sharp_review(
        game_date      = date_arg,
        post_to_discord = not no_discord,
    )

    if not results:
        print("\n  No results to display.")
        print("  Either no picks were posted, or game data isn't available yet.")
        print("  Try running after 11pm when all games are final.")

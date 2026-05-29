# slipiq_orchestrator.py
# Morning scheduler + pipeline runner for SlipIQ
# Runs automatically via Windows Task Scheduler or manual trigger
#
# SCHEDULE:
#   6:30am AZ — Early run: post waiting message, first prop check
#   8:30am AZ — Main run: full curation, post picks if market open
#   9:15am AZ — Confirm run: force refresh, post any remaining picks
#   11:00pm AZ — Sharp Review: grade results, update record
#
# MANUAL COMMANDS:
#   python slipiq_orchestrator.py            → run full pipeline now
#   python slipiq_orchestrator.py --morning  → force morning run
#   python slipiq_orchestrator.py --review   → force sharp review
#   python slipiq_orchestrator.py --schedule → start scheduler loop
#   python slipiq_orchestrator.py --status   → show current state
#   python slipiq_orchestrator.py --force    → force prop refresh + curate

import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

STATE_PATH = CACHE_DIR / "orchestrator_state.json"

# ─────────────────────────────────────────
# SCHEDULE (AZ Mountain Standard Time)
# AZ does not observe DST
# ─────────────────────────────────────────
SCHEDULE = {
    "early":        "06:30",   # waiting message + first check
    "main":         "08:30",   # full curation run
    "confirm":      "09:15",   # force refresh + final picks
    "nba_main":     "11:00",   # NBA daily pipeline
    "nba_confirm":  "11:45",   # NBA line refresh
    "nba_breakout": "16:30",   # injury-window breakout scan
    "review":       "23:00",   # sharp review post-game
}

# How long to wait between scheduler loop ticks (seconds)
LOOP_INTERVAL = 60

# ═════════════════════════════════════════
# STATE MANAGER
# ═════════════════════════════════════════

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "date":         None,
            "early_done":   False,
            "main_done":    False,
            "confirm_done": False,
            "nba_main_done":     False,
            "nba_confirm_done":  False,
            "nba_breakout_done": False,
            "review_done":  False,
            "picks_posted": 0,
            "nba_picks_posted": 0,
            "last_run":     None,
        }
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state: dict):
    state["last_run"] = datetime.now().isoformat()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def reset_state_for_new_day(state: dict) -> dict:
    """
    Runs exactly once a day (usually on the first morning run).
    Clears out yesterday's old API responses so we don't bet on stale lines,
    but PROTECTS our historical results and tracking databases.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    last_run = state.get("last_reset_date")

    # 1. If we already reset today, do nothing. Keep using today's cache to save credits!
    if last_run == today_str:
        return state

    print(f"\n  [cache] New day detected ({today_str}). Executing Master Cache Clear...")

    # 2. Files we absolutely DO NOT want to delete (Your historical data)
    protected_files = {
        "slipiq_results.json",
        "orchestrator_state.json",
        "lines_db.json",
        "line_value_log.json",
        "alt_lines_log.json",
        "record.json"
    }

    # 3. Nuke yesterday's slate so we are forced to pull fresh lines for free
    deleted_count = 0
    if CACHE_DIR.exists():
        for file in CACHE_DIR.glob("*.json"):
            if file.name not in protected_files:
                try:
                    file.unlink()  # Deletes the file
                    deleted_count += 1
                except Exception as e:
                    print(f"  [cache] Could not delete {file.name}: {e}")

    print(f"  [cache] Cleared {deleted_count} stale daily files. API Credits protected.")

    # 4. Update the state so the bot knows today is a fresh day
    state["last_reset_date"] = today_str
    state["morning_run_done"] = False
    state["confirm_run_done"] = False
    
    return state

# ═════════════════════════════════════════
# PIPELINE RUNNERS
# ═════════════════════════════════════════

def run_early(state: dict) -> dict:
    """
    6:30am run.
    - Force fresh prop pull
    - Post waiting message to Discord
    - Log that early run fired
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — EARLY RUN (6:30am)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        print("\n  [1] Checking prop cache (no credit burn on early run)...")
        fetch_props_raw(SPORT_MLB, force=False)

        from slipiq_discord import post_waiting_message
        print("  [2] Posting waiting message to Discord...")
        post_waiting_message()

        state["early_done"] = True
        print("\n  ✅ Early run complete")

    except Exception as e:
        print(f"\n  ❌ Early run error: {e}")

    return state


def run_main(state: dict, force_discord: bool = True) -> dict:
    """
    8:30am run — full curation pipeline.
    Now includes Pitcher, Batter, and Correlated SGP engines.
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — MAIN RUN (8:30am)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        # 1. Fresh Props Pull
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        from slipiq_game_lines import fetch_f5_ml_lines
        print("\n  [1] Refreshing prop lines (3 credits)...")
        fetch_props_raw(SPORT_MLB, force=True)
        game_lines = fetch_f5_ml_lines() or {}

        # 2. Run Individual Curation
        from slipiq_curate import run_curation
        print("  [2] Running individual curation pipeline...")
        curation_result = run_curation(post_discord=force_discord)

        # 3. Build & Post Correlated SGP Parlays (New!)
        from slipiq_pitcher_model import run_all_models
        from slipiq_batter_model import run_batter_model
        from slipiq_ml_parlay import build_ml_parlays
        from slipiq_discord import post_message
        from slipiq_env import CHANNEL_TEAM_PARLAY

        print("  [3] Building Correlated SGP Parlays...")
        pitcher_picks = run_all_models(min_confidence=60) if hasattr(__import__('slipiq_pitcher_model'), 'run_all_models') else []
        batter_picks = run_batter_model(min_confidence=60)
        
        ml_parlays = build_ml_parlays(pitcher_picks, game_lines, batter_picks)
        
        if ml_parlays and force_discord:
            from slipiq_discord import post_message
            from slipiq_env import CHANNEL_TEAM_PARLAY
            if ml_parlays:
                slip1 = ml_parlays.get("slip_1")
                slip2 = ml_parlays.get("slip_2")
                for slip in [slip1, slip2]:
                    if slip and slip.get("legs"):
                        legs_text = "\n".join(
                            f"  • {leg.get('label', '')} | {leg.get('confidence', 0)}%"
                            for leg in slip.get("legs", [])[:8]
                        )
                        content = f"🎯 **SlipIQ SGP — {slip.get('total_legs', 0)} Legs**\n{legs_text}"
                        post_message(CHANNEL_TEAM_PARLAY, content=content[:2000])
                        print("  [parlay] Posted correlated slip to Discord")

        # Start PrizePicks intraday scanner as background job
        try:
            from slipiq_propline_scanner import start_scanner
            import threading
            scanner_thread = threading.Thread(
                target=start_scanner,
                kwargs={"block": False},
                daemon=True,
                name="pp_scanner"
            )
            if not any(t.name == "pp_scanner" for t in threading.enumerate()):
                scanner_thread.start()
                print("  [scanner] PrizePicks intraday scanner started")
        except Exception as scan_err:
            print(f"  [scanner] Scanner start skipped: {scan_err}")

        picks_posted = curation_result.get("post_count", 0)
        state["main_done"]    = True
        state["picks_posted"] = picks_posted

        print(f"\n  ✅ Main run complete — {picks_posted} picks + SGP parlays posted")

    except Exception as e:
        print(f"\n  ❌ Main run error: {e}")

    return state

def run_confirm(state: dict) -> dict:
    """
    9:15am run — confirm full slate is posted.
    Force refresh again, post any remaining picks
    that weren't available at 8:30am.
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — CONFIRM RUN (9:15am)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        from slipiq_parlayapi import fetch_props_raw, check_line_movement, SPORT_MLB
        if check_line_movement(SPORT_MLB, threshold=0.5):
            print("\n  [1] Line movement — refreshing props (3 credits)...")
            fetch_props_raw(SPORT_MLB, force=True)
        else:
            print("\n  [1] No line movement — using cached props (0 credits)")
            fetch_props_raw(SPORT_MLB, force=False)

        from slipiq_curate import run_curation
        print("  [2] Running confirm curation...")
        result = run_curation(post_discord=True)

        new_picks = result.get("post_count", 0)
        state["confirm_done"]  = True
        state["picks_posted"] += new_picks

        print(f"\n  ✅ Confirm run complete — {new_picks} additional picks posted")

    except Exception as e:
        print(f"\n  ❌ Confirm run error: {e}")

    return state


def run_nba_main(state: dict, force_discord: bool = True) -> dict:
    """11:00am AZ — full NBA pipeline."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NBA MAIN RUN (11:00am)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        from slipiq_nba_orchestrator import run_nba_pipeline
        result = run_nba_pipeline(post_to_discord=force_discord, include_breakout=True)
        state["nba_main_done"] = True
        state["nba_picks_posted"] = result.get("post_count", 0)
        print(f"\n  ✅ NBA main run complete — {state['nba_picks_posted']} picks posted")
    except Exception as e:
        print(f"\n  ❌ NBA main run error: {e}")

    return state


def run_nba_confirm_run(state: dict) -> dict:
    """11:45am AZ — NBA confirm / line refresh."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NBA CONFIRM RUN (11:45am)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        from slipiq_nba_orchestrator import run_nba_confirm
        result = run_nba_confirm(post_to_discord=True)
        state["nba_confirm_done"] = True
        state["nba_picks_posted"] = state.get("nba_picks_posted", 0) + result.get("post_count", 0)
        print(f"\n  ✅ NBA confirm complete")
    except Exception as e:
        print(f"\n  ❌ NBA confirm error: {e}")

    return state


def run_nba_breakout(state: dict) -> dict:
    """4:30pm AZ — injury-window breakout scan."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NBA BREAKOUT (4:30pm)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        from slipiq_nba_orchestrator import run_breakout_check
        n = len(run_breakout_check(post_alerts=True))
        state["nba_breakout_done"] = True
        print(f"\n  ✅ Breakout scan complete — {n} alert(s)")
    except Exception as e:
        print(f"\n  ❌ Breakout scan error: {e}")

    return state


def run_review(state: dict) -> dict:
    """
    11pm run — Sharp Review.
    Grade today's picks, post results to #sharp-review.
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — SHARP REVIEW (11pm)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        today = datetime.now().strftime("%Y-%m-%d")

        from slipiq_sharp_review import run_all_sharp_reviews
        print(f"\n  Running Sharp Review for {today}...")
        review_out = run_all_sharp_reviews(
            game_date=today,
            post_to_discord=True,
        )
        results = review_out.get("mlb", []) + review_out.get("nba", [])

        state["review_done"] = True
        print(f"\n  ✅ Sharp Review complete — {len(results)} picks graded")

    except Exception as e:
        print(f"\n  ❌ Sharp Review error: {e}")

    return state


# ═════════════════════════════════════════
# SCHEDULER LOOP
# ═════════════════════════════════════════

def should_run(target_time: str, last_done: bool, window_minutes: int = 20) -> bool:
    """
    Check if a scheduled task should run now.
    Fires if current time is within window_minutes of target_time
    and the task hasn't run today.
    """
    if last_done:
        return False

    now    = datetime.now()
    target = datetime.strptime(
        f"{now.strftime('%Y-%m-%d')} {target_time}",
        "%Y-%m-%d %H:%M"
    )
    delta  = (now - target).total_seconds() / 60  # minutes past target

    return 0 <= delta <= window_minutes


def run_scheduler():
    """
    Continuous scheduler loop.
    Checks every 60 seconds if a task should fire.
    Run this in a terminal or as a Windows Task Scheduler entry.
    """
    print("=" * 60)
    print("SlipIQ Orchestrator — Scheduler Mode")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)
    print("\nSchedule:")
    for name, t in SCHEDULE.items():
        print(f"  {t} AZ — {name}")
    print("\nPress Ctrl+C to stop\n")

    while True:
        try:
            state = load_state()
            state = reset_state_for_new_day(state)

            now_str = datetime.now().strftime("%H:%M")

            # Early run — 6:30am
            if should_run(SCHEDULE["early"], state["early_done"]):
                print(f"\n[{now_str}] Firing early run...")
                state = run_early(state)
                save_state(state)

            # Main run — 8:30am
            elif should_run(SCHEDULE["main"], state["main_done"]):
                print(f"\n[{now_str}] Firing main run...")
                state = run_main(state)
                save_state(state)

            # Confirm run — 9:15am
            elif should_run(SCHEDULE["confirm"], state["confirm_done"]):
                print(f"\n[{now_str}] Firing confirm run...")
                state = run_confirm(state)
                save_state(state)

            from slipiq_env import NBA_SEASON_ACTIVE
            if NBA_SEASON_ACTIVE:
                # NBA main — 11:00am
                if should_run(SCHEDULE["nba_main"], state.get("nba_main_done", False)):
                    print(f"\n[{now_str}] Firing NBA main run...")
                    state = run_nba_main(state)
                    save_state(state)

                # NBA confirm — 11:45am
                elif should_run(SCHEDULE["nba_confirm"], state.get("nba_confirm_done", False)):
                    print(f"\n[{now_str}] Firing NBA confirm run...")
                    state = run_nba_confirm_run(state)
                    save_state(state)

                # NBA breakout — 4:30pm
                elif should_run(SCHEDULE["nba_breakout"], state.get("nba_breakout_done", False)):
                    print(f"\n[{now_str}] Firing NBA breakout scan...")
                    state = run_nba_breakout(state)
                    save_state(state)

            # Sharp Review — 11pm
            elif should_run(SCHEDULE["review"], state["review_done"]):
                print(f"\n[{now_str}] Firing sharp review...")
                state = run_review(state)
                save_state(state)

            else:
                # Idle tick
                next_tasks = [
                    (name, t) for name, t in SCHEDULE.items()
                    if not state.get(f"{name}_done", False)
                ]
                if next_tasks:
                    next_name, next_time = next_tasks[0]
                    print(f"  [{now_str}] Waiting... next: {next_name} at {next_time} AZ",
                          end="\r")

            time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            print("\n\nScheduler stopped.")
            break
        except Exception as e:
            print(f"\n  [scheduler] Error: {e}")
            time.sleep(LOOP_INTERVAL)


# ═════════════════════════════════════════
# STATUS DISPLAY
# ═════════════════════════════════════════

def show_status():
    state = load_state()
    record_path = CACHE_DIR / "record.json"

    print("\n" + "=" * 60)
    print("SlipIQ — System Status")
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    print(f"\n  Today's runs:")
    print(f"  Early (6:30am)  : {'✅ Done' if state.get('early_done') else '⏳ Pending'}")
    print(f"  Main (8:30am)   : {'✅ Done' if state.get('main_done') else '⏳ Pending'}")
    print(f"  Confirm (9:15am): {'✅ Done' if state.get('confirm_done') else '⏳ Pending'}")
    print(f"  NBA Main (11:00) : {'✅ Done' if state.get('nba_main_done') else '⏳ Pending'}")
    print(f"  NBA Confirm(11:45): {'✅ Done' if state.get('nba_confirm_done') else '⏳ Pending'}")
    print(f"  NBA Breakout(4:30): {'✅ Done' if state.get('nba_breakout_done') else '⏳ Pending'}")
    print(f"  Review (11pm)   : {'✅ Done' if state.get('review_done') else '⏳ Pending'}")
    print(f"  MLB picks posted: {state.get('picks_posted', 0)}")
    print(f"  NBA picks posted: {state.get('nba_picks_posted', 0)}")
    print(f"  Last run        : {state.get('last_run', 'Never')}")

    try:
        from slipiq_env import discord_channels_status
        ch = discord_channels_status()
        print(f"\n  Discord channels:")
        print(f"  Basketball props: {'✅' if ch.get('basketball') else '❌ CHANNEL_BASKETBALL_PROPS'}")
    except Exception:
        pass

    if record_path.exists():
        with open(record_path) as f:
            record = json.load(f)

        hits   = record.get("hits", 0)
        misses = record.get("misses", 0)
        pushes = record.get("pushes", 0)
        total  = record.get("total", 0)
        rate   = round(hits / max(hits + misses, 1) * 100, 1)
        clv    = round(record.get("clv_total", 0) / max(total, 1), 4)

        print(f"\n  All-time record:")
        print(f"  {hits}W {misses}L {pushes}P — {rate}% hit rate")
        print(f"  Avg CLV : {clv:+.4f}")
        streak = record.get("streak", 0)
        stype  = record.get("streak_type", "")
        if streak > 0:
            print(f"  Streak  : {streak}{stype}")
    else:
        print(f"\n  No record yet — picks start building tonight")

    # Cache status
    print(f"\n  Cache files:")
    for f in sorted(CACHE_DIR.glob("*.json")):
        size = f.stat().st_size
        age  = (datetime.now().timestamp() - f.stat().st_mtime) / 60
        print(f"  {f.name:<35} {size:>7} bytes  {int(age):>4} min ago")


# ═════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    if "--schedule" in args:
        run_scheduler()

    elif "--status" in args:
        show_status()

    elif "--review" in args:
        state = load_state()
        state = run_review(state)
        save_state(state)

    elif "--nba" in args:
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_nba_main(state, force_discord="--no-discord" not in args)
        save_state(state)

    elif "--morning" in args or "--force" in args:
        # Force full morning pipeline regardless of state
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        print("\n  Force refreshing props...")
        fetch_props_raw(SPORT_MLB, force=True)

        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_main(state, force_discord=True)
        save_state(state)

    elif "--no-discord" in args:
        # Full pipeline without Discord
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        fetch_props_raw(SPORT_MLB, force=True)
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_main(state, force_discord=False)
        save_state(state)

    else:
        # Default: run main pipeline now
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_main(state, force_discord=True)
        save_state(state)

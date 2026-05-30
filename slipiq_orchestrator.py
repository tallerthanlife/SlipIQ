"""
slipiq_orchestrator.py
Main scheduler and pipeline runner for SlipIQ.

Schedule (AZ Mountain Standard Time):
  6:30am  — early run
  8:30am  — main run  
  9:15am  — confirm run
  11:00pm — sharp review
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv
load_dotenv()

import pytz

CACHE_DIR  = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
STATE_PATH = CACHE_DIR / "orchestrator_state.json"

SCHEDULE = {
    "early":   "06:30",
    "main":    "08:30",
    "confirm": "09:15",
    "review":  "23:00",
}

LOOP_INTERVAL = 60


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "date":         None,
            "early_done":   False,
            "main_done":    False,
            "confirm_done": False,
            "review_done":  False,
            "picks_posted": 0,
            "last_run":     None,
        }
    with open(STATE_PATH) as f:
        return json.load(f)


def save_state(state: dict):
    state["last_run"] = datetime.now().isoformat()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def reset_state_for_new_day(state: dict) -> dict:
    today_str = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_reset_date") == today_str:
        return state

    protected = {
        "slipiq_results.json",
        "orchestrator_state.json",
        "record.json",
    }
    deleted = 0
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.json"):
            if f.name not in protected:
                try:
                    f.unlink()
                    deleted += 1
                except Exception:
                    pass

    print(f"  [cache] New day ({today_str}) — cleared {deleted} stale files")

    state["last_reset_date"] = today_str
    state["early_done"]   = False
    state["main_done"]    = False
    state["confirm_done"] = False
    state["review_done"]  = False
    state["picks_posted"] = 0
    return state


def run_early(state: dict) -> dict:
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — EARLY RUN (6:30am AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)
    try:
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        fetch_props_raw(SPORT_MLB, force=False)
        from slipiq_discord import post_waiting_message
        post_waiting_message()
        state["early_done"] = True
        print("\n  ✅ Early run complete")
    except Exception as e:
        import traceback
        print(f"\n  ❌ Early run error: {e}")
        print(traceback.format_exc())
    return state


def run_main(state: dict, force_discord: bool = True) -> dict:
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — MAIN RUN (8:30am AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)
    try:
        # Step 1 — PropLine PRIMARY (Pinnacle + PrizePicks + EV)
        print("\n  [1] Fetching props from PropLine (primary)...")
        pl_props = []
        try:
            from slipiq_propline import fetch_all_props
            pl_props = fetch_all_props(sport="baseball_mlb")
            print(f"  [1] PropLine: {len(pl_props)} props "
                  f"(Pinnacle+PP+EV included)")
        except Exception as e:
            print(f"  [1] PropLine failed: {e}")

        # Step 1b — ParlayAPI FALLBACK only if PropLine empty
        if not pl_props:
            print("  [1b] PropLine empty — falling back to ParlayAPI...")
            try:
                from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
                fetch_props_raw(SPORT_MLB, force=True)
                print("  [1b] ParlayAPI fallback loaded")
            except Exception as e:
                print(f"  [1b] ParlayAPI also failed: {e}")
        else:
            # Cache PropLine props so pitcher model can read them
            try:
                from slipiq_cache import cache_set
                cache_set("props_baseball_mlb", pl_props)
                print("  [1] PropLine props cached for pitcher model")
            except Exception as e:
                print(f"  [1] Cache write failed: {e}")

        from slipiq_curate import run_curation
        print("  [2] Running curation pipeline...")
        curation_result = run_curation(post_discord=force_discord)

        picks_posted = curation_result.get("post_count", 0)
        state["main_done"]    = True
        state["picks_posted"] = picks_posted
        print(f"\n  ✅ Main run complete — {picks_posted} picks posted")

    except Exception as e:
        import traceback
        print(f"\n  ❌ Main run error: {e}")
        print(traceback.format_exc())
    return state


def run_confirm(state: dict) -> dict:
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — CONFIRM RUN (9:15am AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)
    try:
        from slipiq_parlayapi import fetch_props_raw, check_line_movement, SPORT_MLB
        if check_line_movement(SPORT_MLB, threshold=0.5):
            print("\n  [1] Line movement — refreshing props...")
            fetch_props_raw(SPORT_MLB, force=True)
        else:
            print("\n  [1] No line movement — using cached props")
            fetch_props_raw(SPORT_MLB, force=False)

        from slipiq_curate import run_curation
        print("  [2] Running confirm curation...")
        result = run_curation(post_discord=True)

        new_picks = result.get("post_count", 0)
        state["confirm_done"]  = True
        state["picks_posted"]  = state.get("picks_posted", 0) + new_picks
        print(f"\n  ✅ Confirm run complete — {new_picks} additional picks posted")

    except Exception as e:
        import traceback
        print(f"\n  ❌ Confirm run error: {e}")
        print(traceback.format_exc())
    return state


def run_review(state: dict) -> dict:
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — SHARP REVIEW (11pm AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)
    try:
        from slipiq_sharp_review import settle_todays_picks
        settled = settle_todays_picks()

        wins    = [p for p in settled if p.get("result") == "WIN"]
        losses  = [p for p in settled if p.get("result") == "LOSS"]
        pending = [p for p in settled if p.get("result") == "PENDING"]
        print(f"\n  Settled: {len(wins)}W {len(losses)}L {len(pending)} pending")

        if wins or losses:
            try:
                record_path = CACHE_DIR / "record.json"
                record = {}
                if record_path.exists():
                    with open(record_path) as f:
                        record = json.load(f)
                from slipiq_discord import post_results
                post_results(settled, record)
                print(f"  [results] Posted to #sharp-review")
            except Exception as e:
                print(f"  [results] Post failed: {e}")

        state["review_done"] = True
        print(f"\n  ✅ Sharp review complete")

    except Exception as e:
        import traceback
        print(f"\n  ❌ Sharp review error: {e}")
        print(traceback.format_exc())
    return state


def should_run(target_time: str, last_done: bool,
               window_minutes: int = 20) -> bool:
    if last_done:
        return False
    now    = datetime.now()
    target = datetime.strptime(
        f"{now.strftime('%Y-%m-%d')} {target_time}", "%Y-%m-%d %H:%M"
    )
    delta = (now - target).total_seconds() / 60
    return 0 <= delta <= window_minutes


def run_scheduler():
    print("=" * 60)
    print("SlipIQ Orchestrator — Scheduler Mode")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)
    print("\nSchedule:")
    for name, t in SCHEDULE.items():
        print(f"  {t} AZ — {name}")
    print()

    # Registered jobs list
    print("Registered jobs:")
    for name, t in SCHEDULE.items():
        print(f"  [scheduler] Job: {name} | Fires at: {t} AZ")
    print("\nPress Ctrl+C to stop\n")

    # Startup catch-up — fire missed runs within window
    AZ = pytz.timezone("US/Arizona")
    now = datetime.now(AZ)
    state = load_state()
    state = reset_state_for_new_day(state)

    for run_name, (hour, minute) in [("main",(8,30)),("confirm",(9,15))]:
        scheduled = now.replace(hour=hour, minute=minute,
                                second=0, microsecond=0)
        minutes_past = (now - scheduled).total_seconds() / 60
        if 0 <= minutes_past <= 45:
            already_ran = state.get(f"{run_name}_done", False)
            if not already_ran:
                print(f"  [startup] Missed {run_name} — firing now")
                if run_name == "main":
                    state = run_main(state, force_discord=True)
                elif run_name == "confirm":
                    state = run_confirm(state)
                save_state(state)

    while True:
        try:
            state = load_state()
            state = reset_state_for_new_day(state)
            now_str = datetime.now().strftime("%H:%M")

            if should_run(SCHEDULE["early"], state["early_done"]):
                print(f"\n[{now_str}] Firing early run...")
                state = run_early(state)
                save_state(state)

            elif should_run(SCHEDULE["main"], state["main_done"]):
                print(f"\n[{now_str}] Firing main run...")
                state = run_main(state)
                save_state(state)

            elif should_run(SCHEDULE["confirm"], state["confirm_done"]):
                print(f"\n[{now_str}] Firing confirm run...")
                state = run_confirm(state)
                save_state(state)

            elif should_run(SCHEDULE["review"], state["review_done"]):
                print(f"\n[{now_str}] Firing sharp review...")
                state = run_review(state)
                save_state(state)

            else:
                next_tasks = [
                    (name, t) for name, t in SCHEDULE.items()
                    if not state.get(f"{name}_done", False)
                ]
                if next_tasks:
                    next_name, next_time = next_tasks[0]
                    print(
                        f"  [{now_str}] Idle — next: {next_name} at {next_time} AZ",
                        end="\r"
                    )
                else:
                    print(
                        f"  [{now_str}] Idle — next: no remaining slates today",
                        end="\r"
                    )

            time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            print("\n\nScheduler stopped.")
            break
        except Exception as e:
            print(f"\n  [scheduler] Error: {e}")
            time.sleep(LOOP_INTERVAL)


def show_status():
    state = load_state()
    print("\n" + "=" * 60)
    print("SlipIQ — System Status")
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)
    print(f"\n  Today's runs:")
    print(f"  Early  (6:30am) : {'✅' if state.get('early_done')   else '⏳'}")
    print(f"  Main   (8:30am) : {'✅' if state.get('main_done')    else '⏳'}")
    print(f"  Confirm(9:15am) : {'✅' if state.get('confirm_done') else '⏳'}")
    print(f"  Review (11pm)   : {'✅' if state.get('review_done')  else '⏳'}")
    print(f"  Picks posted    : {state.get('picks_posted', 0)}")
    print(f"  Last run        : {state.get('last_run', 'Never')}")


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
    elif "--morning" in args or "--force" in args:
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        print("\n  Force refreshing props...")
        fetch_props_raw(SPORT_MLB, force=True)
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_main(state, force_discord=True)
        save_state(state)
    elif "--no-discord" in args:
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        fetch_props_raw(SPORT_MLB, force=True)
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_main(state, force_discord=False)
        save_state(state)
    else:
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_main(state, force_discord=True)
        save_state(state)

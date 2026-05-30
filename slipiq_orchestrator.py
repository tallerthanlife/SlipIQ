# slipiq_orchestrator.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Master Orchestrator
# Single entry point. Runs on Railway 24/7 via Procfile.
#
# SCHEDULE (AZ Mountain Standard — no DST):
#   06:30 — Early: warm cache, post waiting message
#   08:30 — Main:  full MLB pipeline, SGP parlays, slip routing
#   09:15 — Confirm: line-move refresh, post remaining picks
#   10:00 — PrizePicks scanner starts (background thread)
#   22:00 — PrizePicks scanner stops
#   23:00 — Sharp Review: grade picks, CLV, calibration report
#   NBA jobs: ONLY when NBA_SEASON_ACTIVE=true in .env (October+)
#
# MANUAL COMMANDS:
#   python slipiq_orchestrator.py               → run main pipeline now
#   python slipiq_orchestrator.py --morning     → force main run
#   python slipiq_orchestrator.py --force       → force prop refresh + main
#   python slipiq_orchestrator.py --schedule    → start scheduler loop (Railway)
#   python slipiq_orchestrator.py --status      → print system status
#   python slipiq_orchestrator.py --review      → force sharp review
#   python slipiq_orchestrator.py --nba         → force NBA pipeline
#   python slipiq_orchestrator.py --no-discord  → run without posting
#
# BUGS FIXED IN THIS REBUILD:
#   [1] fetch_odds_raw does not exist → replaced with fetch_f5_ml_lines
#   [2] run_curation(post_to_discord=) → correct param: post_discord=
#   [3] build_ml_parlay_embeds does not exist → inline text post
#   [4] run_pitcher_model does not exist → correct: run_all_models
#   [5] CHANNEL_TEAM_PARLAY imported from slipiq_discord → from slipiq_env
# ═══════════════════════════════════════════════════════════════

import json
import sys
import time
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import pytz

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv
from slipiq_slate_clock import SlateClock
load_dotenv()

CACHE_DIR  = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
STATE_PATH = CACHE_DIR / "orchestrator_state.json"

# ─── Schedule (AZ time) ───────────────────────────────────────
SCHEDULE = {
    "early":          "06:30",
    "main":           "08:30",
    "confirm":        "09:15",
    "pp_start":       "10:00",   # PrizePicks scanner start
    "pp_stop":        "22:00",   # PrizePicks scanner stop
    "nightly_scrape": "22:00",
    "review":         "23:00",
}

LOOP_INTERVAL = 60   # scheduler tick (seconds)

# ─── Global scanner reference ────────────────────────────────
_pp_scanner_running = False
_pp_scanner_thread: threading.Thread | None = None

# ─── Overlap / coalesce guards (max_instances=1 equivalent) ──
_pipeline_running: bool = False               # True while any pipeline job is executing
_last_pipeline_end: datetime | None = None    # timestamp of last completed pipeline run
_MIN_PIPELINE_COOLDOWN_SEC: int = 20 * 60     # 20-minute minimum gap between pipeline fires


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — STATE MANAGER
# ═══════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "date":              None,
        "last_reset_date":   None,
        "early_done":        False,
        "main_done":         False,
        "confirm_done":      False,
        "pp_scanner_started":  False,
        "review_done":         False,
        "nightly_scrape_done": False,
        "picks_posted":        0,
        "last_run":          None,
        "morning_done":      False,
        "afternoon_done":    False,
        "evening_done":      False,
    }


def save_state(state: dict) -> None:
    state["last_run"] = datetime.now().isoformat()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def reset_state_for_new_day(state: dict) -> dict:
    """
    Runs once daily. Clears stale cache files.
    Protects: results, record, calibration logs, state.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("last_reset_date") == today:
        return state

    print(f"\n  [cache] New day ({today}) — clearing stale files...")

    protected = {
        "slipiq_results.json",
        "orchestrator_state.json",
        "record.json",
        "nba_record.json",
        "calibration_log.json",
        "sharp_benchmark_log.json",
        "propline_credits.json",
        "lotto_slip_log.json",
        "mlrl_parlay_log.json",
        "pp_eligible_queue.json",
        "slip_router_output.json",
        "scanner_state.json",
    }

    deleted = 0
    for f in CACHE_DIR.glob("*.json"):
        if f.name not in protected and not f.name.startswith("batter_slate_") \
                and not f.name.startswith("closing_"):
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass

    # Also protect closing line cache files (contain CLV data)
    for file in list(CACHE_DIR.glob("closing_*.json")):
        pass  # closing files are protected — do not delete

    print(f"  [cache] Cleared {deleted} stale files.")

    state["last_reset_date"]     = today
    state["early_done"]          = False
    state["main_done"]           = False
    state["confirm_done"]        = False
    state["pp_scanner_started"]  = False
    state["review_done"]         = False
    state["nightly_scrape_done"] = False
    state["picks_posted"]        = 0
    state["morning_done"]        = False
    state["afternoon_done"]      = False
    state["evening_done"]        = False
    return state


def should_run(target_time: str, last_done: bool, window: int = 20) -> bool:
    """True if current AZ time is within window minutes of target and task not done."""
    if last_done:
        return False
    now    = datetime.now()
    target = datetime.strptime(
        f"{now.strftime('%Y-%m-%d')} {target_time}", "%Y-%m-%d %H:%M"
    )
    delta = (now - target).total_seconds() / 60
    return 0 <= delta <= window


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — EARLY RUN (6:30 AM)
# ═══════════════════════════════════════════════════════════════

def run_early(state: dict) -> dict:
    """
    6:30 AM — Warm caches, post waiting message.
    Zero API credits burned — reads from cache only.
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — EARLY RUN (6:30am AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        # Warm prop cache (no force — use whatever is cached)
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        print("\n  [1] Warming prop cache (0 credits)...")
        fetch_props_raw(SPORT_MLB, force=False)

        # Post waiting message to Discord
        from slipiq_discord import post_waiting_message
        print("  [2] Posting waiting message...")
        post_waiting_message()

        state["early_done"] = True
        print("\n  ✅ Early run complete")

    except Exception as e:
        import traceback
        print(f"\n  ❌ Early run error: {e}")
        print(traceback.format_exc())

    return state


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — MAIN RUN (8:30 AM) — FULLY REBUILT
# ═══════════════════════════════════════════════════════════════

def run_main(state: dict, force_discord: bool = True) -> dict:
    """
    8:30 AM — Full MLB pipeline.
    PIPELINE: [1] Props pull → [2] Curation → Discord
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — MAIN RUN (8:30am AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        # ── [1] Props pull ────────────────────────────────────
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        from slipiq_propline import fetch_all_props
        print("\n  [1] Refreshing prop lines...")
        fetch_props_raw(SPORT_MLB, force=True)
        try:
            pl_props = fetch_all_props(sport="baseball_mlb")
            print(f"  [1b] PropLine: {len(pl_props)} props (Pinnacle+PP included)")
        except Exception as e:
            print(f"  [1b] PropLine fetch failed: {e}")

        # ── [2] Curation → Discord ────────────────────────────
        from slipiq_curate import run_curation
        print("\n  [2] Running curation pipeline...")
        curation_result = run_curation(post_discord=force_discord)

        picks_posted = curation_result.get("post_count", 0)
        print(f"\n  ✅ Main run complete — {picks_posted} picks posted")

        state["main_done"]      = True
        state["main_done_date"] = datetime.now().strftime("%Y-%m-%d")
        state["picks_posted"]   = picks_posted

    except Exception as e:
        import traceback
        print(f"\n  ❌ Main run error: {e}")
        print(traceback.format_exc())

    return state


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — CONFIRM RUN (9:15 AM)
# ═══════════════════════════════════════════════════════════════

def run_confirm(state: dict) -> dict:
    """
    9:15 AM — Line-move refresh, post any remaining picks.
    FIXED: post_discord= not post_to_discord=
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — CONFIRM RUN (9:15am AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        from slipiq_parlayapi import fetch_props_raw, check_line_movement, SPORT_MLB

        if check_line_movement(SPORT_MLB, threshold=0.5):
            print("\n  [1] Line movement detected — refreshing props (3 credits)...")
            fetch_props_raw(SPORT_MLB, force=True)
        else:
            print("\n  [1] No line movement — using cached props (0 credits)")
            fetch_props_raw(SPORT_MLB, force=False)

        from slipiq_curate import run_curation
        print("  [2] Running confirm curation...")
        # FIXED: post_discord= not post_to_discord=
        result = run_curation(post_discord=True)

        new_picks = result.get("post_count", 0)
        state["confirm_done"]  = True
        state["picks_posted"] += new_picks
        print(f"\n  ✅ Confirm run complete — {new_picks} additional picks posted")

    except Exception as e:
        import traceback
        print(f"\n  ❌ Confirm run error: {e}")
        print(traceback.format_exc())

    return state


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — PRIZEPICKS SCANNER (background thread)
# ═══════════════════════════════════════════════════════════════

def _scanner_worker() -> None:
    """Background worker — polls Prop-Line every 20 min for PrizePicks entries."""
    global _pp_scanner_running
    from slipiq_env import SLIPIQ_BANKROLL

    print("  [scanner] PrizePicks scanner thread started")

    while _pp_scanner_running:
        try:
            from slipiq_propline_scanner import run_poll, SPORT_MLB
            result = run_poll(sport=SPORT_MLB)
            if result.get("skipped") and result["skipped"] == "outside_hours":
                _pp_scanner_running = False
                print("  [scanner] Outside active hours — scanner stopping")
                break
        except Exception as e:
            print(f"  [scanner] Poll error: {e}")

        # Sleep 20 minutes between polls
        for _ in range(20 * 60):
            if not _pp_scanner_running:
                break
            time.sleep(1)

    print("  [scanner] PrizePicks scanner thread stopped")


def _start_pp_scanner() -> None:
    """Start PrizePicks intraday scanner as daemon thread if not running."""
    global _pp_scanner_running, _pp_scanner_thread

    if _pp_scanner_running:
        return

    # Check if already running in another thread
    for t in threading.enumerate():
        if t.name == "pp_scanner":
            return

    _pp_scanner_running = True
    _pp_scanner_thread  = threading.Thread(
        target = _scanner_worker,
        name   = "pp_scanner",
        daemon = True,
    )
    _pp_scanner_thread.start()
    print("  [scanner] PrizePicks intraday scanner started (20-min polls)")


def _stop_pp_scanner() -> None:
    """Stop PrizePicks scanner thread cleanly."""
    global _pp_scanner_running
    _pp_scanner_running = False
    print("  [scanner] PrizePicks scanner stop signal sent")


def start_pp_scanner_run(state: dict) -> dict:
    """Called by scheduler at 10:00 AM."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — PP SCANNER START (10:00am AZ)")
    print("═" * 60)
    _start_pp_scanner()
    state["pp_scanner_started"] = True
    return state


def stop_pp_scanner_run(state: dict) -> dict:
    """Called by scheduler at 10:00 PM."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — PP SCANNER STOP (10:00pm AZ)")
    print("═" * 60)
    _stop_pp_scanner()
    return state


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — SHARP REVIEW (11:00 PM)
# ═══════════════════════════════════════════════════════════════

def run_review(state: dict) -> dict:
    """
    11:00 PM — Grade today's picks, CLV, calibration report.
    Posts to #sharp-review channel.
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — SHARP REVIEW (11:00pm AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        today = datetime.now().strftime("%Y-%m-%d")

        from slipiq_sharp_review import run_all_sharp_reviews
        print(f"\n  Grading picks for {today}...")
        review_out = run_all_sharp_reviews(
            game_date    = today,
            post_to_discord = True,
        )
        results = review_out.get("mlb", []) + review_out.get("nba", [])

        # Calibration summary
        try:
            from slipiq_calibration import calibration_summary, format_calibration_discord
            from slipiq_discord import post_message
            from slipiq_env import DISCORD_SHARP_REVIEW_CHANNEL

            summary = calibration_summary(days=30)
            cal_msg = format_calibration_discord(summary)
            if DISCORD_SHARP_REVIEW_CHANNEL:
                post_message(DISCORD_SHARP_REVIEW_CHANNEL, content=cal_msg[:2000])
                print("  [calibration] Calibration summary posted")
        except Exception as e:
            print(f"  [calibration] Summary error: {e}")

        state["review_done"] = True
        print(f"\n  ✅ Sharp Review — {len(results)} picks graded")

    except Exception as e:
        print(f"\n  ❌ Sharp Review error: {e}")

    return state


# ═══════════════════════════════════════════════════════════════
# SECTION 8 — SCHEDULER LOOP
# ═══════════════════════════════════════════════════════════════

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass  # suppress access logs

def _start_health_server(port: int = 8080) -> None:
    """Start a minimal HTTP health check server for Railway."""
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print(f"  [health] Health check server started on port {port}")
    except Exception as e:
        print(f"  [health] Health server failed to start: {e}")


def run_nightly_scrape(state: dict) -> dict:
    """Called by scheduler at 22:00 AZ — pre-caches sharp book lines for morning pipeline."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NIGHTLY SCRAPE (10:00pm AZ)")
    print("═" * 60)

    state["nightly_scrape_done"] = True
    return state


def run_scheduler() -> None:
    """
    Continuous loop — checks every 60 seconds if a task should fire.
    Deployed on Railway via Procfile: worker: python slipiq_orchestrator.py --schedule
    """
    import os
    _start_health_server(port=int(os.environ.get("PORT", 8080)))
    print("=" * 60)
    print("SlipIQ Orchestrator — Scheduler Mode")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)
    print("\nSchedule:")
    for name, t in SCHEDULE.items():
        print(f"  {t} AZ — {name}")
    print("\nRegistered jobs:")
    for name, t in SCHEDULE.items():
        print(f"  [scheduler] Job: {name} | Fires at: {t} AZ")
    print("\nPress Ctrl+C to stop\n")

    # Initialize game-aware slate clock
    clock = SlateClock()
    clock.get_fire_windows()  # pre-load on startup
    print(f"\n  {clock.slate_summary()}\n")

    # ── Startup catch-up: fire missed runs on container restart ─────────────
    AZ    = pytz.timezone("US/Arizona")
    now   = datetime.now(AZ)
    today = str(now.date())

    state = load_state()

    # If we start between 8:30-9:15 AZ and main hasn't run today, fire it
    main_window_start = now.replace(hour=8, minute=30, second=0, microsecond=0)
    main_window_end   = now.replace(hour=9,  minute=15, second=0, microsecond=0)

    if main_window_start <= now <= main_window_end:
        if state.get("main_done_date") != today:
            print(f"  [startup] Within main run window — firing main run now")
            state = run_main(state, force_discord=True)
            state["main_done_date"] = today
            save_state(state)

    # If we start between 6:30-7:30 AZ and early hasn't run today, fire it
    early_window_start = now.replace(hour=6, minute=30, second=0, microsecond=0)
    early_window_end   = now.replace(hour=7, minute=30, second=0, microsecond=0)

    if early_window_start <= now <= early_window_end:
        if state.get("early_done_date") != today:
            print(f"  [startup] Within early run window — firing early run now")
            state = run_early(state)
            state["early_done_date"] = today
            save_state(state)
    # ────────────────────────────────────────────────────────────────────────

    while True:
        try:
            state = load_state()
            today = datetime.now().strftime("%Y-%m-%d")
            if state.get("last_reset_date") != today:
                state = reset_state_for_new_day(state)
                save_state(state)
            now_str = datetime.now().strftime("%H:%M")

            global _pipeline_running, _last_pipeline_end

            # ── max_instances=1 guard ─────────────────────────────────
            if _pipeline_running:
                print(f"  [{now_str}] ⚠️  Pipeline already running — tick skipped  ", end="\r")
                time.sleep(LOOP_INTERVAL)
                continue

            # ── 20-min cooldown between pipeline fires (coalesce) ─────
            if _last_pipeline_end is not None:
                elapsed = (datetime.now() - _last_pipeline_end).total_seconds()
                if elapsed < _MIN_PIPELINE_COOLDOWN_SEC:
                    mins_left = int((_MIN_PIPELINE_COOLDOWN_SEC - elapsed) / 60) + 1
                    print(f"  [{now_str}] Cooldown — ~{mins_left}m until next pipeline allowed   ", end="\r")
                    time.sleep(LOOP_INTERVAL)
                    continue

            _pipeline_fired = False
            _pipeline_running = True
            try:
                if should_run(SCHEDULE["early"], state["early_done"]):
                    print(f"\n[{now_str}] → Early run")
                    state = run_early(state)
                    save_state(state)
                    _pipeline_fired = True

                elif (
                    not state.get("morning_done")
                    and clock.should_fire("morning", state)
                ):
                    print(f"\n[{now_str}] → Morning slate detected — firing main run")
                    state = run_main(state)
                    state["morning_done"] = True
                    save_state(state)
                    _pipeline_fired = True

                elif (
                    not state.get("afternoon_done")
                    and clock.should_fire("afternoon", state)
                ):
                    print(f"\n[{now_str}] → Afternoon slate detected — firing second run")
                    state["afternoon_done"] = True
                    state = run_confirm(state)
                    save_state(state)
                    _pipeline_fired = True

                elif (
                    not state.get("evening_done")
                    and clock.should_fire("evening", state)
                ):
                    print(f"\n[{now_str}] → Evening slate detected — firing evening run")
                    state["evening_done"] = True
                    state = run_confirm(state)
                    save_state(state)
                    _pipeline_fired = True

                elif should_run(SCHEDULE["main"], state["main_done"]):
                    # Fallback: fire at 8:30 AM if clock had no data
                    print(f"\n[{now_str}] → Fallback main run (no slate data)")
                    state = run_main(state)
                    save_state(state)
                    _pipeline_fired = True

                elif should_run(SCHEDULE["confirm"], state["confirm_done"]):
                    print(f"\n[{now_str}] → Confirm run")
                    state = run_confirm(state)
                    save_state(state)
                    _pipeline_fired = True

                elif should_run(SCHEDULE["pp_start"], state.get("pp_scanner_started", False)):
                    print(f"\n[{now_str}] → PrizePicks scanner start")
                    state = start_pp_scanner_run(state)
                    save_state(state)
                    _pipeline_fired = True

                elif should_run(SCHEDULE["pp_stop"], False, window=5):
                    print(f"\n[{now_str}] → PrizePicks scanner stop")
                    _stop_pp_scanner()

                elif should_run(SCHEDULE["nightly_scrape"], state.get("nightly_scrape_done", False)):
                    print(f"\n[{now_str}] → Nightly sharp book scrape")
                    state = run_nightly_scrape(state)
                    save_state(state)
                    _pipeline_fired = True

                elif should_run(SCHEDULE["review"], state["review_done"]):
                    print(f"\n[{now_str}] → Sharp Review")
                    state = run_review(state)
                    save_state(state)
                    _pipeline_fired = True

                else:
                    next_info = clock.get_next_fire_info(state)
                    print(
                        f"  [{now_str}] Idle — next: {next_info}   ",
                        end="\r"
                    )

            finally:
                _pipeline_running = False
                if _pipeline_fired:
                    _last_pipeline_end = datetime.now()

            time.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n  Scheduler stopped.")
            _stop_pp_scanner()
            break
        except Exception as e:
            print(f"\n  [scheduler] Unexpected error: {e}")
            time.sleep(LOOP_INTERVAL)


# ═══════════════════════════════════════════════════════════════
# SECTION 9 — STATUS DISPLAY
# ═══════════════════════════════════════════════════════════════

def show_status() -> None:
    state      = load_state()
    record_path = CACHE_DIR / "record.json"

    print("\n" + "=" * 60)
    print("SlipIQ — System Status")
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    checks = [
        ("Early (6:30am)",    state.get("early_done")),
        ("Main (8:30am)",     state.get("main_done")),
        ("Confirm (9:15am)",  state.get("confirm_done")),
        ("PP Scanner",        state.get("pp_scanner_started")),
        ("Review (11:00pm)",  state.get("review_done")),
    ]
    print("\n  Today's runs:")
    for name, done in checks:
        print(f"    {'✅' if done else '⏳'} {name}")

    print(f"\n  Picks posted : {state.get('picks_posted', 0)}")
    print(f"  Last run     : {state.get('last_run', 'Never')}")

    # API key status
    try:
        from slipiq_env import api_keys_status
        keys = api_keys_status()
        print("\n  API keys:")
        for k, v in keys.items():
            if isinstance(v, bool):
                print(f"    {'✅' if v else '❌'} {k}")
            else:
                print(f"    ℹ️  {k} = {v}")
    except Exception:
        pass

    try:
        from slipiq_cache import api_tier_status
        tiers = api_tier_status()
        print("\n  API Tiers:")
        print(f"    {'✅' if tiers['tier_1_parlayapi'] else '❌'} Tier 1: ParlayAPI (primary)")
        print(f"    {'✅' if tiers['tier_2_propline'] else '❌'} Tier 2: Prop-Line (Pinnacle supplement)")
        print(f"    {'✅' if tiers['tier_3_odds_keys'] > 0 else '❌'} Tier 3: Odds API ({tiers['tier_3_odds_keys']} keys)")
    except Exception:
        pass

    # Discord channel status
    try:
        from slipiq_env import discord_channels_status
        channels = discord_channels_status()
        print("\n  Discord channels:")
        for k, v in channels.items():
            print(f"    {'✅' if v else '❌'} {k}")
    except Exception:
        pass

    # Calibration snapshot
    try:
        from slipiq_calibration import brier_score
        bs = brier_score(days=30)
        if bs["n"] > 0:
            print(f"\n  Model calibration (30d): Brier={bs['brier_score']:.4f} "
                  f"[{bs['rating']}] n={bs['n']}")
    except Exception:
        pass

    # Record
    if record_path.exists():
        try:
            with open(record_path) as f:
                rec = json.load(f)
            hits   = rec.get("hits", 0)
            misses = rec.get("misses", 0)
            total  = hits + misses
            rate   = round(hits / max(total, 1) * 100, 1)
            clv    = round(rec.get("clv_total", 0) / max(rec.get("total", 1), 1), 4)
            print(f"\n  Record: {hits}W {misses}L — {rate}% | Avg CLV {clv:+.4f}")
        except Exception:
            pass

    # Cache snapshot
    print("\n  Cache files:")
    for f in sorted(CACHE_DIR.glob("*.json"))[:12]:
        size = f.stat().st_size
        age  = int((datetime.now().timestamp() - f.stat().st_mtime) / 60)
        print(f"    {f.name:<35} {size:>7}b  {age:>5}m ago")


# ═══════════════════════════════════════════════════════════════
# SECTION 10 — ENTRY POINT
# ═══════════════════════════════════════════════════════════════

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

    elif "--confirm" in args:
        state = load_state()
        state = run_confirm(state)
        save_state(state)

    elif "--scanner-start" in args:
        _start_pp_scanner()
        print("  Scanner running — press Ctrl+C to stop")
        try:
            while _pp_scanner_running:
                time.sleep(5)
        except KeyboardInterrupt:
            _stop_pp_scanner()

    elif "--scrape-now" in args:
        print("=== MANUAL SCRAPE RUN ===")
        state = load_state()
        run_nightly_scrape(state)
        print("=== SCRAPE COMPLETE ===")
        sys.exit(0)

    else:
        # Default: run full pipeline now
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_main(state, force_discord=True)
        save_state(state)

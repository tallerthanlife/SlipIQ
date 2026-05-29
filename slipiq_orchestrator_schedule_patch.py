# slipiq_orchestrator_schedule_patch.py
# ═══════════════════════════════════════════════════════════════
# PATCH — Apply these changes to slipiq_orchestrator.py
#
# CHANGE 1: Replace the scheduler job definitions with the correct
#   schedule matching the full pipeline.
#
# CHANGE 2: Wire slipiq_propline_scanner as a background job
#   starting at 10 AM AZ, stopping at 10 PM AZ.
#
# CHANGE 3: Gate all NBA jobs behind NBA_SEASON_ACTIVE flag.
#   Currently they fire daily even in off-season, burning Groq credits.
#
# HOW TO APPLY:
#   In slipiq_orchestrator.py, find the start_nightly_scheduler()
#   function (or whatever starts APScheduler) and replace the
#   scheduler.add_job() calls with the ones below.
# ═══════════════════════════════════════════════════════════════

SCHEDULER_JOBS = '''
# ─── CORRECT SCHEDULE (AZ = UTC-7, no DST) ────────────────────

# MLB early warmup — props cache, no Discord post yet
scheduler.add_job(
    run_early,
    "cron", hour=6, minute=30, timezone="America/Phoenix",
    id="mlb_early", replace_existing=True,
)

# MLB main run — full pipeline, Discord post
scheduler.add_job(
    run_main,
    "cron", hour=8, minute=30, timezone="America/Phoenix",
    id="mlb_main", replace_existing=True,
)

# MLB confirm — catch missing props, re-evaluate HOLDs
scheduler.add_job(
    run_confirm,
    "cron", hour=9, minute=15, timezone="America/Phoenix",
    id="mlb_confirm", replace_existing=True,
)

# PrizePicks scanner — START at 10 AM AZ
scheduler.add_job(
    _start_pp_scanner,
    "cron", hour=10, minute=0, timezone="America/Phoenix",
    id="pp_scanner_start", replace_existing=True,
)

# PrizePicks scanner — STOP at 10 PM AZ
scheduler.add_job(
    _stop_pp_scanner,
    "cron", hour=22, minute=0, timezone="America/Phoenix",
    id="pp_scanner_stop", replace_existing=True,
)

# Nightly sharp review + calibration summary — 11 PM AZ
scheduler.add_job(
    run_all_sharp_reviews,
    "cron", hour=23, minute=0, timezone="America/Phoenix",
    id="sharp_review", replace_existing=True,
)

# ─── NBA (only when season active) ───────────────────────────
from slipiq_env import NBA_SEASON_ACTIVE
if NBA_SEASON_ACTIVE:
    scheduler.add_job(
        run_nba_pipeline,
        "cron", hour=11, minute=0, timezone="America/Phoenix",
        id="nba_main", replace_existing=True,
    )
    scheduler.add_job(
        run_nba_confirm,
        "cron", hour=11, minute=45, timezone="America/Phoenix",
        id="nba_confirm", replace_existing=True,
    )
    scheduler.add_job(
        run_breakout_check,
        "cron", hour=16, minute=30, timezone="America/Phoenix",
        id="nba_breakout", replace_existing=True,
    )
    print("  NBA jobs scheduled (season active)")
else:
    print("  NBA jobs SKIPPED — NBA_SEASON_ACTIVE=false in .env")
'''

PP_SCANNER_FUNCTIONS = '''
# ─── PrizePicks Scanner Control ───────────────────────────────
_pp_scheduler = None

def _start_pp_scanner():
    """Start the Prop-Line intraday scanner as a background scheduler."""
    global _pp_scheduler
    if _pp_scheduler is not None:
        return  # Already running
    try:
        from slipiq_propline_scanner import start_scanner
        from apscheduler.schedulers.background import BackgroundScheduler
        _pp_scheduler = BackgroundScheduler(timezone="America/Phoenix")
        _pp_scheduler.add_job(
            _run_pp_poll,
            "interval",
            minutes=20,
            id="pp_intraday_poll",
        )
        _pp_scheduler.start()
        print(f"  [orchestrator] PrizePicks scanner started — polling every 20 min")
    except Exception as e:
        print(f"  [orchestrator] PrizePicks scanner start error: {e}")


def _run_pp_poll():
    """Single poll execution — called by background scheduler."""
    try:
        from slipiq_propline_scanner import run_poll, SPORT_MLB
        run_poll(sport=SPORT_MLB)
    except Exception as e:
        print(f"  [scanner poll] Error: {e}")


def _stop_pp_scanner():
    """Shut down the background scanner at end of active hours."""
    global _pp_scheduler
    if _pp_scanner is not None:
        try:
            _pp_scheduler.shutdown(wait=False)
            _pp_scheduler = None
            print("  [orchestrator] PrizePicks scanner stopped (end of active hours)")
        except Exception:
            pass
'''

SHARP_REVIEW_CLV_PATCH = '''
# ─── Sharp Review CLV patch ───────────────────────────────────
# In run_sharp_review() or run_all_sharp_reviews(), after grading each pick,
# add CLV logging via calibration module:

def _log_settled_pick_with_clv(pick: dict, actual_value: float, result: str):
    """
    Log settled pick result to calibration tracker including CLV.
    Call this after determining WIN/LOSS for each pick in sharp review.
    """
    try:
        from slipiq_ev_engine import closing_line_value
        from slipiq_calibration import log_result_by_player

        # Get closing Pinnacle line if available
        closing_over  = pick.get("closing_pinnacle_over")
        closing_under = pick.get("closing_pinnacle_under")
        direction     = pick.get("direction", "over")
        bet_price     = (pick.get("best_book") or {}).get("price")

        clv_pct = None
        if closing_over and closing_under and bet_price:
            closing_price = closing_over if direction == "over" else closing_under
            clv_result    = closing_line_value(bet_price, closing_price)
            clv_pct       = clv_result["clv_pct"]

        count = log_result_by_player(
            player     = pick.get("player", ""),
            market     = pick.get("market", "player_pitcher_strikeouts"),
            direction  = direction,
            game_date  = pick.get("game_date", ""),
            result     = result,
            actual_val = actual_value,
            clv        = clv_pct,
        )
        if count:
            print(f"  [calibration] {pick.get('player')} logged: {result} | CLV {clv_pct:+.2f}%")
    except Exception as e:
        print(f"  [calibration] log error: {e}")
'''

CURSOR_INSTRUCTIONS = """
Apply these changes to slipiq_orchestrator.py:

1. Replace all scheduler.add_job() calls in start_nightly_scheduler()
   (or equivalent function) with SCHEDULER_JOBS above.

2. Add PP_SCANNER_FUNCTIONS as module-level functions before the scheduler function.

3. In run_sharp_review() or run_all_sharp_reviews(), after the WIN/LOSS determination
   for each pick, call _log_settled_pick_with_clv(pick, actual_value, result)
   from SHARP_REVIEW_CLV_PATCH above.

4. Add this to .env and Railway environment:
   NBA_SEASON_ACTIVE=false   (disables NBA jobs until October)
   SLIPIQ_BANKROLL=500       (or your actual bankroll)
   DISCORD_PRIZEPICKS_CHANNEL=<your channel ID>
"""

print("Orchestrator patch spec loaded.")
print(CURSOR_INSTRUCTIONS)

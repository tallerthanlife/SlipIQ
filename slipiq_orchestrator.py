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
    "early":        "06:30",
    "main":         "08:30",
    "confirm":      "09:15",
    "pp_start":     "10:00",   # PrizePicks scanner start
    "nba_main":     "11:00",
    "nba_confirm":  "11:45",
    "nba_breakout": "16:30",
    "pp_stop":      "22:00",   # PrizePicks scanner stop
    "review":       "23:00",
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
        "pp_scanner_started":False,
        "nba_main_done":     False,
        "nba_confirm_done":  False,
        "nba_breakout_done": False,
        "review_done":       False,
        "picks_posted":      0,
        "nba_picks_posted":  0,
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

    state["last_reset_date"]    = today
    state["early_done"]         = False
    state["main_done"]          = False
    state["confirm_done"]       = False
    state["pp_scanner_started"] = False
    state["nba_main_done"]      = False
    state["nba_confirm_done"]   = False
    state["nba_breakout_done"]  = False
    state["review_done"]        = False
    state["picks_posted"]       = 0
    state["nba_picks_posted"]   = 0
    state["morning_done"]       = False
    state["afternoon_done"]     = False
    state["evening_done"]       = False
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

    PIPELINE:
      [1] Props pull (3 cr) + F5/F3 ML lines (0 cr — cached)
      [2] Individual pick curation → confidence agent → ev_engine gate
          → SlipRouter → sgp_pool / indep_pool / ml_rl_pool / pp_queue / lotto_pool
      [3] Correlated SGP parlays (ml_parlay) → post to #team-parlay
      [4] Pitcher lotto slip ($0.25) → post to #team-parlay
      [5] Independent ML/RL parlay → post to #team-parlay
      [6] PrizePicks rolling scanner start (background thread)
      [7] State update

    FIXES APPLIED:
      • fetch_odds_raw removed — not a real function
      • run_curation called with correct param: post_discord=
      • build_ml_parlay_embeds removed — not a real function
      • run_all_models used instead of run_pitcher_model
      • CHANNEL_TEAM_PARLAY imported from slipiq_env not slipiq_discord
    """
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — MAIN RUN (8:30am AZ)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        # Pre-populate game schedule so SlateClock cache is not empty
        try:
            from slipiq_game_lines import get_probable_starters
            games = get_probable_starters(force=True)
            print(f"  [slate] Loaded {len(games)} games for today")
        except Exception as e:
            print(f"  [slate] Game fetch failed: {e} — using fallback")

        # Load today's game schedule from PropLine (free endpoint)
        try:
            from slipiq_propline import fetch_events, fetch_scores
            # Try scores first (includes in-progress and recent games)
            todays_scores = fetch_scores(sport="baseball_mlb", days_from=1)
            # Also get upcoming events
            todays_events = fetch_events(sport="baseball_mlb")
            # Merge both lists, deduplicate by id
            all_games = {g["id"]: g for g in (todays_scores + todays_events)}.values()
            all_games = list(all_games)
            if all_games:
                from slipiq_cache import cache_set
                cache_set("mlb_todays_games", all_games)
                print(f"  [slate] Loaded {len(all_games)} MLB games (scores+events)")
            else:
                print(f"  [slate] PropLine returned 0 games — fallback schedule active")
        except Exception as e:
            print(f"  [slate] PropLine games failed: {e} — fallback active")

        # Alert Discord that a new slate window was detected
        try:
            from slipiq_discord import post_message
            from slipiq_env import DISCORD_DAILY_PICKS_CHANNEL
            from slipiq_slate_clock import SlateClock
            clock   = SlateClock()
            windows = clock.get_fire_windows()
            total   = windows.get("total_games", 0)
            summary = clock.slate_summary()
            if total > 0 and DISCORD_DAILY_PICKS_CHANNEL:
                post_message(
                    DISCORD_DAILY_PICKS_CHANNEL,
                    content=(
                        f"⚾ **SlipIQ pipeline firing** — "
                        f"{total} games detected today\n"
                        f"{summary}\n"
                        f"Picks incoming shortly..."
                    )
                )
        except Exception:
            pass

        # ── [1] Props pull ────────────────────────────────────
        from slipiq_parlayapi import fetch_props_raw, SPORT_MLB
        print("\n  [1] Refreshing prop lines (3 credits)...")
        fetch_props_raw(SPORT_MLB, force=True)

        # F5/F3 ML lines — zero credits (cached from game_lines module)
        f5_lines = {}
        try:
            from slipiq_game_lines import fetch_f5_ml_lines
            f5_lines = fetch_f5_ml_lines() or {}
            print(f"      F5/F3 lines: {len(f5_lines)} games")
        except Exception as e:
            print(f"      F5/F3 lines unavailable: {e}")

        # ── [2] Individual curation (EV-gated, SlipRouter wired) ─
        from slipiq_curate import run_curation
        print("\n  [2] Running individual curation pipeline...")
        # FIXED: post_discord= not post_to_discord=
        curation_result = run_curation(post_discord=force_discord)

        picks_posted = curation_result.get("post_count", 0)
        routing      = curation_result.get("routing", {})

        print(f"      {picks_posted} picks posted")
        if routing:
            stats = routing.get("stats", {})
            print(f"      Routing → SGP:{stats.get('sgp',0)} | "
                  f"Indep:{stats.get('indep',0)} | "
                  f"ML/RL:{stats.get('ml_rl',0)} | "
                  f"PP:{stats.get('pp_queue',0)} | "
                  f"Lotto:{stats.get('lotto',0)}")

        # Run batter model and post batter picks
        try:
            from slipiq_batter_model import run_batter_model
            from slipiq_batter_lines import get_batter_lines
            print("  [2b] Running batter model...")
            batter_lines = get_batter_lines()
            batter_picks = run_batter_model(
                batter_lines=batter_lines,
                min_confidence=60,
                post_to_discord=force_discord
            )
            print(f"  [2b] Batter model: {len(batter_picks)} picks")
        except Exception as e:
            import traceback
            print(f"  [2b] Batter model error: {e}")
            print(traceback.format_exc())
            batter_picks = []

        # ── [3] Correlated SGP parlays ────────────────────────
        print("\n  [3] Building correlated SGP parlays...")
        try:
            from slipiq_env import CHANNEL_TEAM_PARLAY
            from slipiq_discord import post_message

            # Pull picks from curation result or run models directly
            sgp_pool = routing.get("sgp_pool", []) if routing else []

            if sgp_pool:
                from slipiq_ml_parlay import build_ml_parlays
                from slipiq_batter_lines import run_batter_analysis

                # FIXED: run_all_models not run_pitcher_model
                try:
                    from slipiq_pitcher_model import run_all_models
                    pitcher_picks = run_all_models(min_confidence=60)
                except (ImportError, TypeError):
                    pitcher_picks = sgp_pool  # use curated pool as fallback

                batter_picks  = run_batter_analysis(min_confidence=55)
                game_lines_list = list(f5_lines.values()) if f5_lines else []

                ml_parlays = build_ml_parlays(pitcher_picks, game_lines_list, batter_picks)

                if ml_parlays and force_discord and CHANNEL_TEAM_PARLAY:
                    _post_sgp_slips(ml_parlays, CHANNEL_TEAM_PARLAY, post_message)
                else:
                    print("      SGP parlays built — Discord skip (no_discord mode or no channel)")
            else:
                print("      SGP pool empty — no correlated slips built")

        except Exception as e:
            print(f"      SGP parlay build error: {e}")

        # ── [4] Pitcher lotto slip ($0.25) ────────────────────
        print("\n  [4] Building pitcher lotto slip...")
        try:
            lotto_pool = routing.get("lotto_pool", []) if routing else []
            if lotto_pool:
                from slipiq_independent_parlay import build_pitcher_lotto_slip
                from slipiq_env import CHANNEL_TEAM_PARLAY
                from slipiq_discord import post_message

                lotto = build_pitcher_lotto_slip(lotto_pool, target_legs=10, min_legs=7)
                if lotto and lotto.get("n_legs", 0) >= 7 and force_discord and CHANNEL_TEAM_PARLAY:
                    from slipiq_independent_parlay import format_lotto_discord
                    content = format_lotto_discord(lotto)
                    post_message(CHANNEL_TEAM_PARLAY, content=content[:2000])
                    print(f"      Lotto slip posted: {lotto['n_legs']} legs, "
                          f"avg_prob={lotto.get('avg_true_prob', 0):.1%}")
                elif lotto:
                    print(f"      Lotto slip built ({lotto.get('n_legs',0)} legs) — Discord skip")
                else:
                    print("      Lotto: waiting for more legs (queue not full yet)")
            else:
                print("      Lotto pool empty — no high-prob pitchers cleared threshold")

        except Exception as e:
            print(f"      Lotto slip error: {e}")

        # ── [5] Independent ML/RL parlay ──────────────────────
        print("\n  [5] Building independent ML/RL parlay...")
        try:
            ml_rl_pool = routing.get("ml_rl_pool", []) if routing else []
            if ml_rl_pool:
                from slipiq_slip_router import filter_best_fn_per_game
                from slipiq_independent_parlay import build_mlrl_parlay, format_mlrl_discord
                from slipiq_env import CHANNEL_TEAM_PARLAY
                from slipiq_discord import post_message
                from slipiq_env import SLIPIQ_BANKROLL

                best_fn_legs = filter_best_fn_per_game(ml_rl_pool)
                mlrl = build_mlrl_parlay(
                    best_fn_legs,
                    target_legs = 5,
                    min_legs    = 3,
                    bankroll    = SLIPIQ_BANKROLL,
                )
                if mlrl and mlrl.get("passes_ev") and force_discord and CHANNEL_TEAM_PARLAY:
                    content = format_mlrl_discord(mlrl)
                    post_message(CHANNEL_TEAM_PARLAY, content=content[:2000])
                    print(f"      ML/RL parlay posted: {mlrl['n_legs']} legs, "
                          f"EV={mlrl.get('ev', 0)*100:+.1f}%")
                elif mlrl:
                    print(f"      ML/RL parlay built ({mlrl['n_legs']} legs) — "
                          f"{'EV not confirmed' if not mlrl.get('passes_ev') else 'Discord skip'}")
                else:
                    print("      ML/RL parlay: insufficient +EV legs")
            else:
                print("      ML/RL pool empty — no F3/F5 lines cleared EV threshold")

        except Exception as e:
            print(f"      ML/RL parlay error: {e}")

        # ── [6] PrizePicks scanner (background) ───────────────
        # Note: scheduler also fires pp_start at 10:00 AM.
        # This starts it early if main run happens to fire after 10 AM.
        now_hour = datetime.now().hour
        if now_hour >= 10:
            _start_pp_scanner()

        # ── [7] State update ──────────────────────────────────
        state["main_done"]    = True
        state["picks_posted"] = picks_posted
        print(f"\n  ✅ Main run complete — {picks_posted} picks + slips posted")

    except Exception as e:
        import traceback
        print(f"\n  ❌ Main run error: {e}")
        print(traceback.format_exc())

    return state


def _post_sgp_slips(ml_parlays: dict, channel: str, post_fn) -> None:
    """
    Format and post SGP slip_1 and slip_2 to Discord.
    FIXED: no longer calls build_ml_parlay_embeds (does not exist).
    Uses plain text formatting instead.
    """
    for key, label in [("slip_1", "SGP Combo"), ("slip_2", "Best Legs")]:
        slip = ml_parlays.get(key)
        if not slip or not slip.get("legs"):
            continue

        total_legs = slip.get("total_legs", len(slip.get("legs", [])))
        avg_conf   = slip.get("avg_conf", 0)
        games      = slip.get("games_covered", slip.get("games", "?"))

        lines = [
            f"🎯 **SlipIQ SGP — {label} | {total_legs} Legs | {avg_conf}% avg confidence**",
            f"Games: {games}",
            "",
        ]

        leg_emoji = {
            "pitcher_k":      "⚾",
            "f5_ml":          "🏟️",
            "f3_ml":          "🏟️",
            "f5_rl":          "📊",
            "f3_rl":          "📊",
            "batter_hits":    "🏏",
            "batter":         "🏏",
            "opp_total_under": "⬇️",
        }

        for leg in slip.get("legs", [])[:10]:
            emoji   = leg_emoji.get(leg.get("leg_type", ""), "📊")
            label_  = leg.get("label", leg.get("prop", ""))
            odds_v  = leg.get("odds")
            note    = leg.get("note", "")
            conf    = leg.get("confidence", 0)
            odds_s  = f" | {'+' if odds_v and odds_v > 0 else ''}{int(odds_v)}" if odds_v else ""
            ev_tag  = ""
            if leg.get("ev") is not None:
                ev_tag = f" | EV {leg['ev']*100:+.1f}%"
            lines.append(f"  {emoji} {label_}{odds_s} | {conf}%{ev_tag}")
            if note:
                lines.append(f"       ↳ {note}")

        lines.append("")
        lines.append("📚 DraftKings · Fanatics — verify lines before submitting")

        try:
            from slipiq_writer import write_sgp_narrative
            mock_slip = {"legs": slip.get("legs", [])}
            sgp_note = write_sgp_narrative(mock_slip)
            if sgp_note:
                lines.append("")
                lines.append(f"💡 {sgp_note}")
        except Exception:
            pass

        content = "\n".join(lines)
        post_fn(channel, content=content[:2000])
        print(f"      Posted SGP {label} ({total_legs} legs) to Discord")


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
# SECTION 6 — NBA RUNNERS (gated by NBA_SEASON_ACTIVE)
# ═══════════════════════════════════════════════════════════════

def run_nba_main(state: dict, force_discord: bool = True) -> dict:
    """11:00 AM — NBA pipeline. Only runs if NBA_SEASON_ACTIVE=true."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NBA MAIN (11:00am AZ)")
    print("═" * 60)

    try:
        from slipiq_nba_orchestrator import run_nba_pipeline
        result = run_nba_pipeline(post_to_discord=force_discord, include_breakout=True)
        state["nba_main_done"]    = True
        state["nba_picks_posted"] = result.get("post_count", 0)
        print(f"\n  ✅ NBA main complete — {state['nba_picks_posted']} picks")
    except Exception as e:
        print(f"\n  ❌ NBA main error: {e}")

    return state


def run_nba_confirm_run(state: dict) -> dict:
    """11:45 AM — NBA confirm / line refresh."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NBA CONFIRM (11:45am AZ)")
    print("═" * 60)

    try:
        from slipiq_nba_orchestrator import run_nba_confirm
        result = run_nba_confirm(post_to_discord=True)
        state["nba_confirm_done"]  = True
        state["nba_picks_posted"] += result.get("post_count", 0)
        print(f"\n  ✅ NBA confirm complete")
    except Exception as e:
        print(f"\n  ❌ NBA confirm error: {e}")

    return state


def run_nba_breakout(state: dict) -> dict:
    """4:30 PM — Injury-window breakout scan."""
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NBA BREAKOUT (4:30pm AZ)")
    print("═" * 60)

    try:
        from slipiq_nba_orchestrator import run_breakout_check
        n = len(run_breakout_check(post_alerts=True))
        state["nba_breakout_done"] = True
        print(f"\n  ✅ Breakout scan — {n} alert(s)")
    except Exception as e:
        print(f"\n  ❌ Breakout scan error: {e}")

    return state


# ═══════════════════════════════════════════════════════════════
# SECTION 7 — SHARP REVIEW (11:00 PM)
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
    print("\nPress Ctrl+C to stop\n")

    # Initialize game-aware slate clock
    clock = SlateClock()
    clock.get_fire_windows()  # pre-load on startup
    print(f"\n  {clock.slate_summary()}\n")

    # Import NBA flag once
    try:
        from slipiq_env import NBA_SEASON_ACTIVE
    except Exception:
        NBA_SEASON_ACTIVE = False

    if not NBA_SEASON_ACTIVE:
        print("  ⚠️  NBA jobs disabled — NBA_SEASON_ACTIVE=false in .env\n")

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

                elif should_run(SCHEDULE["nba_main"], state.get("nba_main_done", False)):
                    if NBA_SEASON_ACTIVE:
                        print(f"\n[{now_str}] → NBA main run")
                        state = run_nba_main(state)
                        save_state(state)
                    else:
                        state["nba_main_done"] = True  # skip silently
                        save_state(state)
                    _pipeline_fired = True

                elif should_run(SCHEDULE["nba_confirm"], state.get("nba_confirm_done", False)):
                    if NBA_SEASON_ACTIVE:
                        print(f"\n[{now_str}] → NBA confirm")
                        state = run_nba_confirm_run(state)
                        save_state(state)
                    else:
                        state["nba_confirm_done"] = True
                        save_state(state)
                    _pipeline_fired = True

                elif should_run(SCHEDULE["nba_breakout"], state.get("nba_breakout_done", False)):
                    if NBA_SEASON_ACTIVE:
                        print(f"\n[{now_str}] → NBA breakout scan")
                        state = run_nba_breakout(state)
                        save_state(state)
                    else:
                        state["nba_breakout_done"] = True
                        save_state(state)
                    _pipeline_fired = True

                elif should_run(SCHEDULE["pp_stop"], False, window=5):
                    print(f"\n[{now_str}] → PrizePicks scanner stop")
                    _stop_pp_scanner()

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
        ("NBA Main (11:00am)",state.get("nba_main_done")),
        ("NBA Confirm",       state.get("nba_confirm_done")),
        ("NBA Breakout",      state.get("nba_breakout_done")),
        ("Review (11:00pm)",  state.get("review_done")),
    ]
    print("\n  Today's runs:")
    for name, done in checks:
        print(f"    {'✅' if done else '⏳'} {name}")

    print(f"\n  MLB picks posted : {state.get('picks_posted', 0)}")
    print(f"  NBA picks posted : {state.get('nba_picks_posted', 0)}")
    print(f"  Last run         : {state.get('last_run', 'Never')}")

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

    elif "--nba" in args:
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_nba_main(state, force_discord="--no-discord" not in args)
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

    else:
        # Default: run full pipeline now
        state = load_state()
        state = reset_state_for_new_day(state)
        state = run_main(state, force_discord=True)
        save_state(state)

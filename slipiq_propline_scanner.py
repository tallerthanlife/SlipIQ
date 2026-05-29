# slipiq_propline_scanner.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Prop-Line Intraday Scanner
# Role: 20-minute interval polling loop for dynamic PrizePicks
#
# THIS IS A SEPARATE PROCESS from the main orchestrator.
# Deploy as a second Railway service or run alongside via
# APScheduler's BackgroundScheduler in the orchestrator.
#
# WHAT IT DOES EVERY 20 MINUTES:
#   1. Fetch latest props from Prop-Line API (cached 20-min TTL)
#   2. Check for line movement > 0.5 pts on tracked picks
#   3. Re-evaluate EV for any movers → post alerts
#   4. Run PrizePicks intraday scanner → build new entries
#   5. Post new +EV entries to Discord before lock time
#   6. Track credit budget — stop polling if limit approached
#
# ACTIVE HOURS: 10 AM – 10 PM AZ (MLB day + night games)
# POLL INTERVAL: 20 minutes = 36 polls/day = ~72 credits
# BUDGET REMAINING: ~928 credits/day for emergency re-pulls
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

from slipiq_propline import (
    fetch_propline_props,
    aggregate_propline_by_player,
    check_line_movement,
    get_daily_credit_usage,
    SPORT_MLB,
    SPORT_NBA,
)
from slipiq_prizepicks import (
    intraday_scanner,
    format_pp_entry_discord,
    scan_eligible_legs,
    PP_ELIGIBLE_MARKETS_MLB,
)
from slipiq_ev_engine import (
    sportsbook_edge,
    assess_leg,
    no_vig_prob,
    sharp_move_flag,
)

# ─── Config from env ──────────────────────────────────────────
DISCORD_LIVE_ALERTS_CHANNEL   = os.getenv("DISCORD_LIVE_ALERTS_CHANNEL")
DISCORD_PRIZEPICKS_CHANNEL    = os.getenv("DISCORD_PRIZEPICKS_CHANNEL") or \
                                 os.getenv("DISCORD_DAILY_PICKS_CHANNEL")

# Scanner active hours (AZ time = UTC-7)
SCAN_START_HOUR_AZ = 10   # 10 AM AZ
SCAN_END_HOUR_AZ   = 22   # 10 PM AZ
POLL_INTERVAL_MIN  = 20   # minutes between polls
CREDIT_SAFETY_STOP = 900  # stop polling if daily usage hits this

# Cache dir
CACHE_DIR     = Path("cache")
SCANNER_STATE = CACHE_DIR / "scanner_state.json"

# Line movement threshold to trigger re-evaluation
LINE_MOVE_THRESHOLD = 0.5   # points
SHARP_MOVE_THRESHOLD = 1.0  # points — sharper signal

BANKROLL = float(os.getenv("SLIPIQ_BANKROLL", "500"))


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def _load_state() -> dict:
    if SCANNER_STATE.exists():
        try:
            return json.loads(SCANNER_STATE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {
        "last_poll":       None,
        "polls_today":     0,
        "entries_posted":  0,
        "alerts_posted":   0,
        "date":            datetime.now().strftime("%Y-%m-%d"),
    }


def _save_state(state: dict) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        # Reset daily counters
        state = {
            "last_poll":      state.get("last_poll"),
            "polls_today":    0,
            "entries_posted": 0,
            "alerts_posted":  0,
            "date":           today,
        }
    SCANNER_STATE.write_text(json.dumps(state, indent=2))


def _is_active_hours() -> bool:
    """Check if current AZ time is within scan window."""
    # AZ = UTC-7 (no DST)
    now_utc = datetime.now(timezone.utc)
    az_hour = (now_utc.hour - 7) % 24
    return SCAN_START_HOUR_AZ <= az_hour < SCAN_END_HOUR_AZ


def _budget_ok() -> bool:
    """Return True if daily credit usage is below safety stop."""
    usage = get_daily_credit_usage()
    return usage["used"] < CREDIT_SAFETY_STOP


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — DISCORD POSTING
# ═══════════════════════════════════════════════════════════════

def _post_discord(channel_id: str | None, content: str) -> bool:
    """Post a message to Discord via the bot token."""
    if not channel_id:
        print(f"  [scanner] No channel configured — printing instead:")
        print(f"  {content[:200]}")
        return False

    try:
        import discord
        import asyncio

        token = os.getenv("DISCORD_BOT_TOKEN")
        if not token:
            print("  [scanner] DISCORD_BOT_TOKEN not set")
            return False

        async def _send():
            client = discord.Client(intents=discord.Intents.default())
            async with client:
                await client.login(token)
                ch = await client.fetch_channel(int(channel_id))
                await ch.send(content[:2000])

        asyncio.run(_send())
        return True
    except Exception as e:
        print(f"  [scanner] Discord post error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — LINE MOVEMENT ALERTS
# ═══════════════════════════════════════════════════════════════

def _post_line_move_alert(mover: dict, sharp_signal: str = "NEUTRAL") -> None:
    """Format and post a line movement alert to Discord."""
    try:
        from slipiq_parlay_alerts import post_line_move_alert as _alert
        _alert(
            player       = mover.get("player", ""),
            market       = mover.get("market", ""),
            old_line     = mover.get("old_line", 0),
            new_line     = mover.get("new_line", 0),
            book         = mover.get("book", ""),
            delta        = mover.get("delta", 0),
            sharp_signal = sharp_signal,
        )
    except Exception as e:
        print(f"  [scanner] Line move alert error: {e}")


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — MAIN POLL FUNCTION
# ═══════════════════════════════════════════════════════════════

def run_poll(sport: str = SPORT_MLB, model_probs: dict | None = None) -> dict:
    """
    Single poll execution. Called every 20 minutes by scheduler.

    Args:
        sport       : sport to scan (MLB default)
        model_probs : {(player_key, market, direction): true_prob}
                      If None, falls back to Pinnacle no-vig probabilities.

    Returns:
        {
            "polls_run"        : int,
            "movers"           : list,
            "pp_entries_built" : int,
            "credits_used"     : int,
            "skipped"          : str | None,  # reason if skipped
        }
    """
    from datetime import datetime
    now_hour = datetime.now().hour
    # Only scan during active hours: 10 AM to 10 PM AZ
    if now_hour < 10 or now_hour >= 22:
        return {"skipped": "outside_hours", "hour": now_hour}

    state = _load_state()

    if not _is_active_hours():
        print(f"  [scanner] Outside active hours ({SCAN_START_HOUR_AZ}–{SCAN_END_HOUR_AZ} AZ) — skipping")
        return {"polls_run": 0, "movers": [], "pp_entries_built": 0,
                "credits_used": 0, "skipped": "outside_hours"}

    if not _budget_ok():
        print(f"  [scanner] Daily credit limit approached — stopping polls")
        return {"polls_run": 0, "movers": [], "pp_entries_built": 0,
                "credits_used": 0, "skipped": "budget_limit"}

    print(f"\n{'─'*50}")
    print(f"[scanner] Poll @ {datetime.now().strftime('%H:%M:%S AZ')} | Sport: {sport}")

    # ── Step 1: Fetch fresh props ──────────────────────────────
    props = fetch_propline_props(sport)
    if not props:
        print("  [scanner] No props returned — skipping this poll")
        return {"polls_run": 1, "movers": [], "pp_entries_built": 0,
                "credits_used": 0, "skipped": "no_props"}

    # ── Step 2: Check line movement ────────────────────────────
    move_result = check_line_movement(sport, threshold=LINE_MOVE_THRESHOLD)
    movers      = move_result.get("movers", [])

    alerts_posted = 0
    if movers:
        print(f"  [scanner] {len(movers)} line(s) moved >{LINE_MOVE_THRESHOLD} pts")
        # Aggregate to get Pinnacle data for sharp move detection
        agg = aggregate_propline_by_player(props)

        for mover in movers:
            # Check if this is a sharp move (Pinnacle specifically)
            sharp_signal = "NEUTRAL"
            if mover["book"] == "pinnacle":
                # Find model direction for this player from pick cache
                model_dir = _get_model_direction(mover["player"], mover["market"])
                if model_dir:
                    sharp_signal = sharp_move_flag(
                        opening_american=_line_to_odds(mover["old_line"]),
                        current_american=_line_to_odds(mover["new_line"]),
                        model_direction=model_dir,
                    )["sharp_signal"]

            # Only alert on significant moves or sharp signals
            if abs(mover["delta"]) >= SHARP_MOVE_THRESHOLD or sharp_signal in ("CONFIRM", "WARN"):
                _post_line_move_alert(mover, sharp_signal)
                alerts_posted += 1
    else:
        print(f"  [scanner] No significant line movement")

    # ── Step 3: PrizePicks intraday scan ──────────────────────
    agg = aggregate_propline_by_player(props)

    # If no model_probs provided, use Pinnacle no-vig as fallback
    if model_probs is None:
        model_probs = _build_pinnacle_probs(agg)

    pp_entries = intraday_scanner(agg, model_probs, bankroll=BANKROLL, sport=sport.split("_")[0])
    entries_posted = 0

    if pp_entries:
        print(f"  [scanner] {len(pp_entries)} new PrizePicks entries ready")
        for entry in pp_entries:
            msg = format_pp_entry_discord(entry)
            posted = _post_discord(DISCORD_PRIZEPICKS_CHANNEL, msg)
            if posted:
                entries_posted += 1
                print(f"  [scanner] ✓ Posted {entry['n_picks']}-pick {entry['mode']} entry (EV {entry['ev']:+.1%})")
    else:
        print(f"  [scanner] No new +EV PrizePicks entries this poll")

    eligible_legs = []
    try:
        from slipiq_ev_engine import prizepicks_leg_threshold
        min_prob = prizepicks_leg_threshold(4) + 0.02
        for prop in (props or []):
            tp = prop.get("true_prob") or prop.get("fair_over_prob")
            if tp and float(tp) >= min_prob:
                eligible_legs.append(prop)
    except Exception:
        eligible_legs = []

    # Post PrizePicks entries if any were built
    try:
        from slipiq_prizepicks import build_pp_entry_with_expiry
        from slipiq_discord import post_prizepicks_entry
        from slipiq_env import DISCORD_PRIZEPICKS_CHANNEL

        if eligible_legs and DISCORD_PRIZEPICKS_CHANNEL:
            entry = build_pp_entry_with_expiry(
                eligible_legs = eligible_legs,
                target_picks  = 4,
                flex          = False,
            )
            if entry and entry.get("passes"):
                posted = post_prizepicks_entry(entry)
                if posted:
                    print(f"  [scanner] PrizePicks {entry['n_picks']}-pick entry posted")
    except Exception as e:
        print(f"  [scanner] PrizePicks post error: {e}")

    # ── Step 4: Update state ───────────────────────────────────
    state["last_poll"]      = datetime.now().isoformat()
    state["polls_today"]    = state.get("polls_today", 0) + 1
    state["entries_posted"] = state.get("entries_posted", 0) + entries_posted
    state["alerts_posted"]  = state.get("alerts_posted", 0) + alerts_posted
    _save_state(state)

    usage = get_daily_credit_usage()

    return {
        "polls_run":         1,
        "movers":            movers,
        "pp_entries_built":  entries_posted,
        "credits_used":      usage["used"],
        "skipped":           None,
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — HELPERS
# ═══════════════════════════════════════════════════════════════

def _build_pinnacle_probs(agg: dict) -> dict:
    """
    Build model_probs dict from Pinnacle no-vig data.
    Used as fallback when no external model probs are provided.
    Returns {(player_key, market, direction): true_prob}
    """
    probs = {}
    for (player_key, market), data in agg.items():
        pin = data.get("pinnacle")
        if pin and pin.get("over") and pin.get("under"):
            nv = no_vig_prob(pin["over"], pin["under"])
            probs[(player_key, market, "over")]  = nv["true_over"]
            probs[(player_key, market, "under")] = nv["true_under"]
    return probs


def _get_model_direction(player: str, market: str) -> str | None:
    """
    Look up the model's pick direction for a player from today's pick cache.
    Returns "over", "under", or None if not in today's picks.
    """
    pick_cache = CACHE_DIR / "latest_picks.json"
    if not pick_cache.exists():
        return None
    try:
        picks = json.loads(pick_cache.read_text())
        for pick in picks:
            if (pick.get("player", "").lower() == player.lower()
                    and pick.get("market", "") == market):
                return pick.get("direction")
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return None


def _line_to_odds(line: float) -> int:
    """Rough conversion of a prop line change to implied odds change (for sharp_move_flag)."""
    # This is an approximation — actual prices needed for precise CLV
    return -110  # default, placeholder


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — SCHEDULER ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def start_scanner(block: bool = True) -> None:
    """
    Start the APScheduler polling loop.

    Args:
        block : True = blocking (standalone process),
                False = background (alongside orchestrator)
    """
    SchedulerClass = BlockingScheduler if block else BackgroundScheduler
    scheduler = SchedulerClass(timezone="America/Phoenix")

    scheduler.add_job(
        run_poll,
        trigger="interval",
        minutes=POLL_INTERVAL_MIN,
        id="propline_poll_mlb",
        name="Prop-Line MLB Intraday Poll",
        kwargs={"sport": SPORT_MLB},
        replace_existing=True,
    )

    print("=" * 60)
    print("SlipIQ — Prop-Line Scanner Starting")
    print(f"Poll interval: every {POLL_INTERVAL_MIN} minutes")
    print(f"Active hours:  {SCAN_START_HOUR_AZ}:00 – {SCAN_END_HOUR_AZ}:00 AZ")
    print(f"Credit budget: {CREDIT_SAFETY_STOP}/day hard stop")
    print("=" * 60)

    # Run once immediately on start
    print("\n[scanner] Running initial poll...")
    run_poll(sport=SPORT_MLB)

    if block:
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()
            print("\n[scanner] Scanner stopped.")
    else:
        scheduler.start()
        return scheduler


def get_scanner_status() -> dict:
    """Return current scanner state for health checks / Discord commands."""
    state  = _load_state()
    usage  = get_daily_credit_usage()
    return {
        "last_poll":      state.get("last_poll"),
        "polls_today":    state.get("polls_today", 0),
        "entries_posted": state.get("entries_posted", 0),
        "alerts_posted":  state.get("alerts_posted", 0),
        "credits_used":   usage["used"],
        "credits_left":   usage["remaining"],
        "active_now":     _is_active_hours(),
        "budget_ok":      _budget_ok(),
    }


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT (standalone process)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if "--status" in sys.argv:
        status = get_scanner_status()
        print(json.dumps(status, indent=2))
    elif "--poll-once" in sys.argv:
        result = run_poll(sport=SPORT_MLB)
        print(json.dumps(result, indent=2, default=str))
    else:
        start_scanner(block=True)

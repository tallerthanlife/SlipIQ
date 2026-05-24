"""
SlipIQ Orchestrator
Runs the full pipeline automatically on a schedule

Schedule (ET):
  12:00 PM ET — Morning run (9am AZ) — lines live, starters confirmed
  4:00 PM ET  — Pre-game run (1pm AZ) — catches late line movement

Run modes:
  py slipiq_orchestrator.py           — run once immediately
  py slipiq_orchestrator.py --schedule — run on schedule (keeps running)
"""

import os
import discord
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ET = ZoneInfo("America/New_York")


# ─── Pipeline ─────────────────────────────────────────────────

def run_pipeline():
    """
    Full SlipIQ pipeline:
    1. Pull today's MLB games
    2. Fetch live lines + run strikeout model
    3. Generate Groq AI writeups
    4. Post ranked picks to Discord
    """
    print(f"\n{'='*52}")
    print(f"SlipIQ Pipeline — {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"{'='*52}")

    try:
        # ── Step 1: MLB Data ──────────────────────────────────
        print("\n[1/4] Pulling today's MLB games...")
        from slipiq_mlb_data import get_todays_games
        games = get_todays_games()

        if not games:
            print("      No games found today. Pipeline stopped.")
            return

        print(f"      ✅ {len(games)} games found")

        # ── Step 2: Lines + Model ─────────────────────────────
        print("\n[2/4] Fetching live lines + running strikeout model...")
        from slipiq_lines import run_full_analysis
        picks = run_full_analysis()

        if not picks:
            print("      No high confidence picks today. Pipeline stopped.")
            return

        print(f"      ✅ {len(picks)} picks generated")

        # ── Step 3: Groq Writeups ─────────────────────────────
        print("\n[3/4] Generating AI analysis via Groq...")
        from slipiq_writer import generate_daily_brief
        brief = generate_daily_brief(picks)
        print(f"      ✅ Brief generated")

        # ── Step 4: Log picks + post to Discord ───────────────
        print("\n[4/4] Logging picks and posting to Discord...")
        from slipiq_results import log_pick

        for pick in picks:
            log_pick(pick)

        if not DISCORD_BOT_TOKEN:
            print("      ❌ DISCORD_BOT_TOKEN not set in .env — picks logged only")
            return

        from slipiq_discord import SlipIQBot

        intents = discord.Intents.default()
        bot = SlipIQBot(intents=intents, picks=picks, brief=brief)

        try:
            bot.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            print(f"      Discord session ended: {e}")

        print(f"\n✅ Pipeline complete — {datetime.now(ET).strftime('%H:%M:%S %Z')}")

    except Exception as e:
        print(f"\n❌ Pipeline error: {e}")
        import traceback
        traceback.print_exc()


# ─── Scheduler ────────────────────────────────────────────────

def start_scheduler():
    """
    Runs pipeline on schedule — keeps running until Ctrl+C

    12:00 PM ET = 9:00 AM AZ  — Morning run
    4:00 PM ET  = 1:00 PM AZ  — Pre-game run
    """
    now_et = datetime.now(ET)
    print("\n" + "="*52)
    print("SlipIQ Orchestrator — Scheduled Mode")
    print("="*52)
    print(f"Started:    {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("Run 1:      12:00 PM ET (9:00 AM AZ) — Morning")
    print("Run 2:       4:00 PM ET (1:00 PM AZ) — Pre-game")
    print("Press Ctrl+C to stop")
    print("="*52)

    scheduler = BlockingScheduler(timezone=ET)
    scheduler.add_job(run_pipeline, "cron", hour=12, minute=0, id="morning_run")
    scheduler.add_job(run_pipeline, "cron", hour=16, minute=0, id="pregame_run")

    if now_et.hour >= 12:
        print("\nPast 12pm ET — running pipeline now...")
        run_pipeline()

    scheduler.start()


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--schedule" in sys.argv:
        start_scheduler()
    else:
        run_pipeline()
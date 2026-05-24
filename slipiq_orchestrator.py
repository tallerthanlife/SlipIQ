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
import time
import schedule
import discord
import asyncio
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")


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
    print(f"SlipIQ Pipeline — {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}")
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

        # ── Step 4: Post to Discord ───────────────────────────
        print("\n[4/4] Posting picks to Discord...")

        if not DISCORD_BOT_TOKEN:
            print("      ❌ DISCORD_BOT_TOKEN not set in .env")
            return

        from slipiq_discord import SlipIQBot

        intents = discord.Intents.default()
        bot = SlipIQBot(intents=intents)

        try:
            bot.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            print(f"      Discord session ended: {e}")

        print(f"\n✅ Pipeline complete — {datetime.now().strftime('%H:%M:%S')}")

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
    print("\n" + "="*52)
    print("SlipIQ Orchestrator — Scheduled Mode")
    print("="*52)
    print(f"Started:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Run 1:      12:00 PM ET (9:00 AM AZ) — Morning")
    print("Run 2:       4:00 PM ET (1:00 PM AZ) — Pre-game")
    print("Press Ctrl+C to stop")
    print("="*52)

    # Schedule both runs
    schedule.every().day.at("12:00").do(run_pipeline)
    schedule.every().day.at("16:00").do(run_pipeline)

    # Run immediately if already past 12pm ET
    now = datetime.now()
    if now.hour >= 12:
        print("\nPast 12pm ET — running pipeline now...")
        run_pipeline()

    # Keep alive — check every 60 seconds
    while True:
        schedule.run_pending()
        time.sleep(60)


# ─── Entry Point ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--schedule" in sys.argv:
        start_scheduler()
    else:
        run_pipeline()
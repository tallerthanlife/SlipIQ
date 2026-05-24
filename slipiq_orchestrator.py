"""
SlipIQ Orchestrator
Runs the full pipeline automatically on a schedule

Schedule (ET):
  12:00 PM ET — Morning run (9am AZ) — lines live, starters confirmed
  4:00 PM ET  — Pre-game run (1pm AZ) — catches late line movement

Run modes:
  py slipiq_orchestrator.py           — run once immediately
  py slipiq_orchestrator.py --schedule — run on schedule (keeps running)
  py slipiq_orchestrator.py --sharp-review — settle + post Sharp Review only
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


def run_pipeline():
    """
    Full SlipIQ pipeline:
    0. Auto-settle yesterday's picks (Sharp Review agent)
    1. Pull today's MLB games
    2. Fetch live lines + model + confidence agent
    3. Six-step slip review
    4. Curate daily best pick
    5. Generate Groq brief
    6. Log picks (JSON + Supabase) + post to Discord
    """
    print(f"\n{'='*52}")
    print(f"SlipIQ Pipeline — {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"{'='*52}")

    try:
        # ── Step 0: Settle pending picks ──────────────────────
        print("\n[0/7] Sharp Review — settling pending picks...")
        from slipiq_sharp_review_agent import auto_settle_pending
        from slipiq_results import calculate_hit_rates

        settled_today = auto_settle_pending()
        sharp_stats = calculate_hit_rates(silent=True)
        print(f"      ✅ {len(settled_today)} settled | stats: {sharp_stats is not None}")

        # ── Step 1: MLB Data ──────────────────────────────────
        print("\n[1/7] Pulling today's MLB games...")
        from slipiq_mlb_data import get_todays_games

        games = get_todays_games()
        if not games:
            print("      No games found today. Pipeline stopped.")
            return
        print(f"      ✅ {len(games)} games found")

        # ── Step 2: Lines + Model + Confidence ────────────────
        print("\n[2/7] Fetching lines + model + confidence agent...")
        from slipiq_lines import run_full_analysis

        picks = run_full_analysis()
        if not picks:
            print("      No high confidence picks today. Pipeline stopped.")
            return
        print(f"      ✅ {len(picks)} picks generated")

        # ── Step 3: Slip review checklist ────────────────────
        print("\n[3/7] Running 6-step slip review...")
        from slipiq_slip_review import review_picks, format_review_text

        picks, approved = review_picks(picks, require_all_passed=False)
        print(f"      {len(approved)}/{len(picks)} picks passed full checklist")
        if approved:
            picks = approved
        for pick in picks[:3]:
            review = pick.get("slip_review", {})
            status = "APPROVED" if review.get("passed") else "CAUTION"
            print(f"      [{status}] {pick['pitcher']} ({review.get('score', 0)}%)")

        # ── Step 4: Curate daily best ─────────────────────────
        print("\n[4/7] Curating daily best pick...")
        from slipiq_curate import select_daily_best, daily_best_summary

        daily_best = select_daily_best(picks)
        if daily_best:
            print(f"      {daily_best_summary(daily_best)}")
            if daily_best.get("slip_review"):
                print(format_review_text(daily_best["slip_review"]))

        # ── Step 5: Groq brief ────────────────────────────────
        print("\n[5/7] Generating AI daily brief...")
        from slipiq_writer import generate_daily_brief

        brief = generate_daily_brief(picks)
        print("      Brief generated")

        # ── Step 6: Log picks ─────────────────────────────────
        print("\n[6/7] Logging picks...")
        from slipiq_results import log_pick
        from slipiq_db import is_configured

        for pick in picks:
            log_pick(pick)
        if is_configured():
            print("      Synced to Supabase")

        # ── Step 7: Discord ───────────────────────────────────
        print("\n[7/7] Posting to Discord...")
        if not DISCORD_BOT_TOKEN:
            print("      ❌ DISCORD_BOT_TOKEN not set — picks logged only")
            return

        from slipiq_discord import SlipIQBot

        intents = discord.Intents.default()
        bot = SlipIQBot(
            intents=intents,
            picks=picks,
            brief=brief,
            daily_best=daily_best,
            sharp_review_stats=sharp_stats,
            settled_today=settled_today,
        )
        try:
            bot.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            print(f"      Discord session ended: {e}")

        print(f"\n✅ Pipeline complete — {datetime.now(ET).strftime('%H:%M:%S %Z')}")

    except Exception as e:
        print(f"\n❌ Pipeline error: {e}")
        import traceback
        traceback.print_exc()


def run_sharp_review_only():
    """Settle pending picks and optionally post Sharp Review to Discord."""
    from slipiq_sharp_review_agent import run_sharp_review

    post = bool(os.getenv("CHANNEL_SHARP_REVIEW"))
    run_sharp_review(post_discord=post)


def start_scheduler():
    """12:00 PM ET morning run + 4:00 PM ET pre-game run."""
    now_et = datetime.now(ET)
    print("\n" + "=" * 52)
    print("SlipIQ Orchestrator — Scheduled Mode")
    print("=" * 52)
    print(f"Started:    {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("Run 1:      12:00 PM ET — Morning pipeline")
    print("Run 2:       4:00 PM ET — Pre-game pipeline")
    print("Press Ctrl+C to stop")
    print("=" * 52)

    scheduler = BlockingScheduler(timezone=ET)
    scheduler.add_job(run_pipeline, "cron", hour=12, minute=0, id="morning_run")
    scheduler.add_job(run_pipeline, "cron", hour=16, minute=0, id="pregame_run")

    if now_et.hour >= 12:
        print("\nPast 12pm ET — running pipeline now...")
        run_pipeline()

    scheduler.start()


if __name__ == "__main__":
    import sys

    if "--sharp-review" in sys.argv:
        run_sharp_review_only()
    elif "--schedule" in sys.argv:
        start_scheduler()
    else:
        run_pipeline()

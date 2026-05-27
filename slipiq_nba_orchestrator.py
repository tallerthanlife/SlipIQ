# slipiq_nba_orchestrator.py
# NBA pipeline runner — props → breakout → curation → Discord

import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from datetime import datetime

from slipiq_nba_curate import run_nba_curation
from slipiq_nba_data import detect_breakout_candidates
from slipiq_nba_discord import post_breakout_alert
from slipiq_parlayapi import (
    SPORT_NBA,
    check_line_movement,
    fetch_period_markets,
    fetch_props_raw,
)


def run_breakout_check(post_alerts: bool = True) -> list[dict]:
    """Injury-window breakout scan — posts to live-alerts + basketball channel."""
    print("\n  [nba] Breakout candidate scan...")
    candidates = detect_breakout_candidates()
    print(f"  [nba] {len(candidates)} breakout candidate(s)")
    if post_alerts:
        for c in candidates:
            try:
                post_breakout_alert(c)
            except Exception as e:
                print(f"  [nba] breakout post error: {e}")
    return candidates


def run_nba_pipeline(post_to_discord: bool = True, include_breakout: bool = True) -> dict:
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NBA PIPELINE")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        print("\n  [1] Refreshing NBA props (3 credits)...")
        fetch_props_raw(SPORT_NBA, force=True)

        print("  [2] Fetching Q1 period markets (2 credits)...")
        try:
            fetch_period_markets(SPORT_NBA)
        except Exception as e:
            print(f"  [nba] period markets skip: {e}")

        if include_breakout:
            run_breakout_check(post_alerts=post_to_discord)

        print("  [3] Running NBA curation...")
        result = run_nba_curation(post_to_discord=post_to_discord)
        print(f"\n  ✅ NBA pipeline complete — {result.get('post_count', 0)} picks posted")
        return result

    except Exception as e:
        print(f"\n  ❌ NBA pipeline error: {e}")
        return {"post_count": 0, "error": str(e)}


def run_nba_confirm(post_to_discord: bool = True) -> dict:
    print("\n" + "═" * 60)
    print("ORCHESTRATOR — NBA CONFIRM RUN")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("═" * 60)

    try:
        if check_line_movement(SPORT_NBA, threshold=0.5):
            print("\n  [1] Line movement — refreshing NBA props...")
            fetch_props_raw(SPORT_NBA, force=True)
        else:
            print("\n  [1] No line movement — using cached props")
            fetch_props_raw(SPORT_NBA, force=False)

        result = run_nba_curation(post_to_discord=post_to_discord)
        print(f"\n  ✅ NBA confirm complete — {result.get('post_count', 0)} picks")
        return result

    except Exception as e:
        print(f"\n  ❌ NBA confirm error: {e}")
        return {"post_count": 0, "error": str(e)}


if __name__ == "__main__":
    args = sys.argv[1:]
    no_discord = "--no-discord" in args
    breakout_only = "--breakout" in args

    if breakout_only:
        run_breakout_check(post_alerts=not no_discord)
    elif "--confirm" in args:
        run_nba_confirm(post_to_discord=not no_discord)
    else:
        run_nba_pipeline(
            post_to_discord=not no_discord,
            include_breakout="--no-breakout" not in args,
        )

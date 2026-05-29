# slipiq_parlay_alerts.py
# Parlay channel alerts — posts SGP and multi-leg picks to Discord
# Phase 3 feature — stub in place so slipiq_curate.py doesn't error
#
# WHEN BUILT (Phase 3):
#   Reads f5_picks and correlated slips from slipiq_game_lines.py
#   Posts to CHANNEL_TEAM_PARLAY channel on Discord

from slipiq_env import CHANNEL_TEAM_PARLAY


def post_parlay_alerts(slate: dict) -> bool:
    """
    Post parlay/SGP alerts to Discord parlay channel.
    Phase 3 stub — logs intent, does not post yet.
    """
    if not CHANNEL_TEAM_PARLAY:
        print("  [parlay] CHANNEL_TEAM_PARLAY not set — skipping")
        return False

    post_list = slate.get("post_list", [])
    hold_list = slate.get("hold_list", [])
    all_picks = post_list + hold_list

    if not all_picks:
        print("  [parlay] No picks to build parlay from")
        return False

    print(f"  [parlay] Phase 3 stub — {len(all_picks)} picks available for parlay builder")
    print(f"  [parlay] Channel: {CHANNEL_TEAM_PARLAY}")
    print(f"  [parlay] Parlay alerts will post here in Phase 3")
    return True

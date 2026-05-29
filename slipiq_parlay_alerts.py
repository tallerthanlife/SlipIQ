# slipiq_parlay_alerts.py
# High-Confidence Parlay Builder

from datetime import datetime
from slipiq_env import CHANNEL_TEAM_PARLAY
from slipiq_discord import post_message

def post_parlay_alerts(slate: dict) -> bool:
    """
    Builds a high-confidence "Best Legs" Parlay from today's top picks.
    Posts to the designated Discord channel.
    """
    # Skip if routing has already handled parlay posting via orchestrator
    if slate.get("routing") and slate["routing"].get("stats", {}).get("indep", 0) > 0:
        print("  [parlay_alerts] Routing handled — skipping duplicate post")
        return

    post_list = slate.get("post_list", [])
    
    # 1. Filter for the absolute safest picks (Confidence 65+, Grade A or B+)
    elite_legs = [
        p for p in post_list 
        if p.get("confidence", 0) >= 65 and p.get("grade") in ["A+", "A", "B+"]
    ]

    # 2. Need at least 2 strong legs to justify a parlay
    if len(elite_legs) < 2:
        print("  [parlay] Not enough elite picks today to force a parlay.")
        return False

    # 3. Take the top 3 safest legs
    parlay_legs = elite_legs[:3]
    
    # 4. Print the parlay to the local terminal for testing
    print("\n  " + "═" * 45)
    print("  🔥 SLIPIQ HIGH-CONFIDENCE PARLAY")
    print("  " + "═" * 45)
    for leg in parlay_legs:
        direction = (leg.get("direction") or "over").upper()
        print(f"  ▶ {leg.get('player'):<20} {direction} {leg.get('line')} Ks | {leg.get('confidence')}%")
    print("  " + "═" * 45 + "\n")

    # 5. Build the Discord embed 
    if not CHANNEL_TEAM_PARLAY:
        print("  [parlay] CHANNEL_TEAM_PARLAY missing in .env — skipping Discord post")
        return False

    description = "Here is the highest-confidence combined slip for today's MLB slate, strictly using model-approved edges.\n\n"
    
    for leg in parlay_legs:
        direction = (leg.get("direction") or "over").upper()
        
        # THE FIX: Safely evaluate missing sportsbooks to an empty dictionary
        best_book = leg.get("best_book") or {}
        book = best_book.get("book", "Any Book")
        price = best_book.get("price", "-110")
        
        description += f"⚾ **{leg.get('player')}** — {direction} {leg.get('line')} Strikeouts\n"
        description += f"└ *Confidence: {leg.get('confidence')}% | Grade: {leg.get('grade')} | {book} ({price})*\n\n"

    embed = {
        "title": "⚡ SlipIQ Top-Tier Parlay",
        "color": 0xFFD700,  # Electric Gold 
        "description": description,
        "footer": {"text": "SlipIQ AI • Play responsibly"}
    }

    return post_message(CHANNEL_TEAM_PARLAY, embed=embed)
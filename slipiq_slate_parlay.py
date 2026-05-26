"""
SlipIQ Slate Parlay Builder
Builds a cross-game slate parlay from the best props of the day
Combines: best pitcher K prop + best batter TB prop per game + F5 ML where justified
Posts to Discord as one combined slip
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Game Classification ──────────────────────────────────────

def classify_game(pitcher_pick, opposing_pitcher_pick=None):
    """
    Classify each game as DUEL, MISMATCH, or WEAK
    Drives what legs get added to the parlay
    """
    if not pitcher_pick:
        return "WEAK"

    proj = pitcher_pick["projection"]
    line = pitcher_pick["line"]
    confidence = pitcher_pick["confidence"]

    # Elite pitcher — projects well above line
    if proj >= 7.0 and confidence >= 70:
        if opposing_pitcher_pick and opposing_pitcher_pick.get("projection", 0) >= 7.0:
            return "DUEL"  # Both aces — skip ML/RL, take Ks only
        return "MISMATCH"  # Our pitcher dominates

    # Solid pitcher
    if proj >= 5.5 and confidence >= 65:
        return "MISMATCH"

    return "WEAK"


def get_f5_ml_justification(pitcher_pick):
    """
    Check if F5 ML is justified based on pitcher projection
    Returns True if the pitcher is projected strongly enough
    """
    if not pitcher_pick:
        return False

    proj = pitcher_pick["projection"]
    confidence = pitcher_pick["confidence"]
    trend = pitcher_pick.get("trend", "NEUTRAL")

    # Need strong projection and confidence for F5 ML
    if proj >= 6.5 and confidence >= 70:
        return True
    if proj >= 5.5 and confidence >= 72 and trend == "HOT":
        return True

    return False


# ─── Parlay Builder ───────────────────────────────────────────

def build_slate_parlay(pitcher_picks, batter_picks):
    """
    Build the cross-game slate parlay
    Takes best legs from each qualifying game
    Returns structured parlay dict
    """
    if not pitcher_picks:
        return None

    parlay_legs = []
    games_used = set()
    skipped_duels = []

    # Group pitcher picks by game
    game_pitchers = {}
    for pick in pitcher_picks:
        home = pick.get("home_team", "")
        away = pick.get("away_team", "")
        game_key = f"{away}@{home}" if away and home else pick["pitcher"]
        if game_key not in game_pitchers:
            game_pitchers[game_key] = []
        game_pitchers[game_key].append(pick)

    # Group batter picks by game
    game_batters = {}
    for pick in batter_picks:
        home = pick.get("home_team", "")
        away = pick.get("away_team", "")
        game_key = f"{away}@{home}" if away and home else pick["batter"]
        if game_key not in game_batters:
            game_batters[game_key] = []
        game_batters[game_key].append(pick)

    # Process each game
    all_game_keys = set(list(game_pitchers.keys()) + list(game_batters.keys()))

    for game_key in all_game_keys:
        game_pitcher_picks = game_pitchers.get(game_key, [])
        game_batter_picks = game_batters.get(game_key, [])

        if not game_pitcher_picks and not game_batter_picks:
            continue

        # Get best pitcher for this game
        best_pitcher = None
        if game_pitcher_picks:
            best_pitcher = max(game_pitcher_picks, key=lambda x: x["confidence"])

        # Get best batter TB pick for this game
        best_batter_tb = None
        tb_picks = [p for p in game_batter_picks if p["prop_type"] == "total_bases"]
        if tb_picks:
            best_batter_tb = max(tb_picks, key=lambda x: x["confidence"])

        # Classify game
        scenario = classify_game(best_pitcher)

        if scenario == "WEAK" and not best_batter_tb:
            continue

        # Add pitcher K leg
        if best_pitcher and scenario != "WEAK":
            direction = "OVER" if "OVER" in best_pitcher["recommendation"] else "UNDER"
            parlay_legs.append({
                "type": "pitcher",
                "game": game_key,
                "player": best_pitcher["pitcher"],
                "prop": f"Strikeouts {direction} {best_pitcher['line']}",
                "direction": direction,
                "line": best_pitcher["line"],
                "projection": best_pitcher["projection"],
                "confidence": best_pitcher["confidence"],
                "grade": best_pitcher.get("grade", "B"),
                "bookmaker": best_pitcher["bookmaker"],
                "scenario": scenario,
            })
            games_used.add(game_key)

            # Add F5 ML if justified (MISMATCH only)
            if scenario == "MISMATCH" and get_f5_ml_justification(best_pitcher):
                team = best_pitcher.get("home_team", "Home Team")
                parlay_legs.append({
                    "type": "f5_ml",
                    "game": game_key,
                    "player": f"{team} F5 ML",
                    "prop": "First 5 Innings ML",
                    "direction": "OVER",
                    "line": None,
                    "projection": None,
                    "confidence": best_pitcher["confidence"] - 5,
                    "grade": best_pitcher.get("grade", "B"),
                    "bookmaker": best_pitcher["bookmaker"],
                    "scenario": scenario,
                })

        # Add best batter TB leg
        if best_batter_tb:
            direction = "OVER" if "OVER" in best_batter_tb["recommendation"] else "UNDER"
            parlay_legs.append({
                "type": "batter",
                "game": game_key,
                "player": best_batter_tb["batter"],
                "prop": f"Total Bases {direction} {best_batter_tb['line']}",
                "direction": direction,
                "line": best_batter_tb["line"],
                "projection": best_batter_tb["projection"],
                "confidence": best_batter_tb["confidence"],
                "grade": best_batter_tb.get("grade", "B"),
                "bookmaker": best_batter_tb["bookmaker"],
                "scenario": scenario if scenario != "WEAK" else "BATTER_ONLY",
            })
            games_used.add(game_key)

    # Sort by confidence
    parlay_legs.sort(key=lambda x: x["confidence"], reverse=True)

    if not parlay_legs:
        return None

    # Calculate overall parlay confidence
    avg_confidence = round(
        sum(leg["confidence"] for leg in parlay_legs) / len(parlay_legs), 1
    )

    # Count by type
    pitcher_legs = [l for l in parlay_legs if l["type"] == "pitcher"]
    batter_legs = [l for l in parlay_legs if l["type"] == "batter"]
    f5_legs = [l for l in parlay_legs if l["type"] == "f5_ml"]

    return {
        "legs": parlay_legs,
        "total_legs": len(parlay_legs),
        "games_covered": len(games_used),
        "pitcher_legs": len(pitcher_legs),
        "batter_legs": len(batter_legs),
        "f5_legs": len(f5_legs),
        "avg_confidence": avg_confidence,
        "top_legs": parlay_legs[:10],  # Top 10 for the focused parlay
    }


# ─── Format for Discord ───────────────────────────────────────

def format_parlay_text(parlay):
    """Format parlay as clean text for Discord"""
    if not parlay:
        return "No slate parlay today"

    lines = [
        f"🎯 **SlipIQ Slate Parlay — {parlay['total_legs']} Legs**",
        f"Games covered: {parlay['games_covered']} | Avg confidence: {parlay['avg_confidence']}%",
        f"Breakdown: {parlay['pitcher_legs']} pitcher | {parlay['batter_legs']} batter | {parlay['f5_legs']} F5 ML",
        "",
        "**Top 10 Legs (by confidence):**",
    ]

    type_emoji = {"pitcher": "⚾", "batter": "🏏", "f5_ml": "🎰"}
    grade_emoji = {"A": "🔥", "B": "✅", "C": "⚠️"}

    for i, leg in enumerate(parlay["top_legs"], 1):
        emoji = type_emoji.get(leg["type"], "📊")
        grade = grade_emoji.get(leg["grade"], "📊")
        proj_str = f" (proj: {leg['projection']})" if leg["projection"] else ""
        lines.append(
            f"{grade} {i}. {emoji} **{leg['player']}** — {leg['prop']}{proj_str} | {leg['confidence']}% conf"
        )

    lines.append("")
    lines.append(f"*Bet on DraftKings · Fanatics · PrizePicks. Verify lines before submitting.*")

    return "\n".join(lines)


def build_parlay_embed(parlay):
    """Build Discord embed for slate parlay"""
    import discord

    if not parlay:
        return None

    color = 0x00FF88 if parlay["avg_confidence"] >= 72 else 0x3399FF

    embed = discord.Embed(
        title=f"🎯 SlipIQ Slate Parlay — {parlay['total_legs']} Legs",
        description=f"Cross-game slate | {parlay['games_covered']} games | Avg {parlay['avg_confidence']}% confidence",
        color=color,
    )

    embed.add_field(
        name="📊 Breakdown",
        value=f"⚾ {parlay['pitcher_legs']} pitcher | 🏏 {parlay['batter_legs']} batter | 🎰 {parlay['f5_legs']} F5 ML",
        inline=False,
    )

    type_emoji = {"pitcher": "⚾", "batter": "🏏", "f5_ml": "🎰"}
    grade_emoji = {"A": "🔥", "B": "✅", "C": "⚠️"}

    legs_text = ""
    for i, leg in enumerate(parlay["top_legs"], 1):
        emoji = type_emoji.get(leg["type"], "📊")
        grade = grade_emoji.get(leg["grade"], "📊")
        proj_str = f" → proj {leg['projection']}" if leg["projection"] else ""
        legs_text += f"{grade}{emoji} **{leg['player']}** {leg['prop']}{proj_str}\n"

    embed.add_field(
        name=f"🏆 Top {len(parlay['top_legs'])} Legs",
        value=legs_text[:1024] if legs_text else "No legs",
        inline=False,
    )

    embed.add_field(
        name="📡 Books",
        value="DraftKings · Fanatics · PrizePicks — verify lines before submitting",
        inline=False,
    )

    embed.set_footer(text="SlipIQ • Cross-Game Slate Parlay")
    return embed


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ Slate Parlay Test ===\n")

    from slipiq_lines import run_full_analysis
    from slipiq_batter_lines import run_batter_analysis

    print("Pulling pitcher picks...")
    pitcher_picks = run_full_analysis()

    print("\nPulling batter picks...")
    batter_picks = run_batter_analysis()

    print("\nBuilding slate parlay...")
    parlay = build_slate_parlay(pitcher_picks, batter_picks)

    if parlay:
        print(format_parlay_text(parlay))
        print(f"\nTotal legs: {parlay['total_legs']}")
        print(f"Games covered: {parlay['games_covered']}")
    else:
        print("No parlay built today")
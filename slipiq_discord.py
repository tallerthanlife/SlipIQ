"""
SlipIQ Discord Bot — Personal Use
Posts MLB pitcher strikeout picks with AI writeups to your private Discord
Powered by Groq (free) — no Anthropic credits needed
"""

import discord
import os
import asyncio
from dotenv import load_dotenv
from slipiq_lines import run_full_analysis
from slipiq_writer import generate_pick_writeup, generate_daily_brief

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MLB_CHANNEL_ID = int(os.getenv("CHANNEL_MLB_PITCHER_PROPS", 0))

# ─── Helpers ──────────────────────────────────────────────────

def grade_color(grade):
    return {"A": 0x00ff88, "B": 0x3399ff, "C": 0xffaa00}.get(grade, 0x888888)

def trend_emoji(trend):
    return {"HOT": "🔥", "COLD": "❄️", "NEUTRAL": "➡️"}.get(trend, "➡️")

def direction_emoji(rec):
    return "⬆️" if "OVER" in rec else "⬇️"

# ─── Formatter ────────────────────────────────────────────────

def build_pick_embed(pick, rank, writeup=None):
    direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
    grade = pick["recommendation"].split("Grade: ")[-1].split(" |")[0].strip()

    embed = discord.Embed(
        title=f"{direction_emoji(pick['recommendation'])} #{rank} {pick['pitcher']} — {direction} {pick['line']} K",
        color=grade_color(grade)
    )

    # AI writeup as description
    if writeup and "unavailable" not in writeup.lower():
        embed.description = f"*{writeup}*"

    embed.add_field(name="🎯 Projection", value=f"**{pick['projection']} K**", inline=True)
    embed.add_field(name="📊 Season Avg", value=f"{pick.get('season_avg', 'N/A')} K", inline=True)
    embed.add_field(name="📉 Last 3", value=f"{pick.get('last_3_avg', 'N/A')} K", inline=True)
    embed.add_field(name="📈 Trend", value=f"{trend_emoji(pick['trend'])} {pick['trend']}", inline=True)
    embed.add_field(name="🏆 Grade", value=f"**{grade}**", inline=True)
    embed.add_field(name="💯 Confidence", value=f"**{pick['confidence']}%**", inline=True)
    embed.add_field(name="📡 Source", value=pick["bookmaker"], inline=True)
    embed.set_footer(text="SlipIQ • MLB Strikeout Model • Powered by Groq")
    return embed

def build_header_embed(picks, brief=None):
    embed = discord.Embed(
        title="⚾ SlipIQ — MLB Pitcher Props",
        description=brief or f"**{len(picks)} picks** generated from live FanDuel lines",
        color=0x1a1a2e
    )
    embed.set_footer(text="SlipIQ • Personal Analytics")
    return embed

# ─── Bot ──────────────────────────────────────────────────────

class SlipIQBot(discord.Client):

    async def on_ready(self):
        print(f"✅ SlipIQ Bot online as {self.user}")

        if not MLB_CHANNEL_ID:
            print("❌ CHANNEL_MLB_PITCHER_PROPS not set in .env")
            await self.close()
            return

        print("🔄 Running analysis...")
        picks = run_full_analysis()

        if not picks:
            print("No picks generated today")
            await self.close()
            return

        # Generate daily brief
        print("✍️ Generating daily brief...")
        brief = generate_daily_brief(picks)

        try:
            channel = await self.fetch_channel(MLB_CHANNEL_ID)

            # Post header with brief
            await channel.send(embed=build_header_embed(picks, brief))
            await asyncio.sleep(1)

            # Post each pick with writeup
            for i, pick in enumerate(picks, 1):
                print(f"✍️ Writing up pick {i}/{len(picks)}...")
                writeup = generate_pick_writeup(pick)
                await channel.send(embed=build_pick_embed(pick, i, writeup))
                await asyncio.sleep(1.5)

            print(f"✅ Posted {len(picks)} picks to Discord")

        except Exception as e:
            print(f"❌ Discord error: {e}")

        await self.close()

# ─── Test Mode (no Discord needed) ───────────────────────────

def test_output():
    """Test full output without posting to Discord"""
    print("=== SlipIQ Discord Test ===\n")
    picks = run_full_analysis()

    if not picks:
        print("No picks today")
        return

    brief = generate_daily_brief(picks)
    print(f"Daily Brief:\n{brief}\n")

    for i, pick in enumerate(picks, 1):
        writeup = generate_pick_writeup(pick)
        direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
        grade = pick["recommendation"].split("Grade: ")[-1].split(" |")[0].strip()
        print(f"#{i} {pick['pitcher']} — {direction} {pick['line']} K")
        print(f"Projection: {pick['projection']} K | Grade: {grade} | Confidence: {pick['confidence']}%")
        print(f"Analysis: {writeup}")
        print()

# ─── Runner ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        test_output()
    else:
        if not DISCORD_BOT_TOKEN:
            print("❌ DISCORD_BOT_TOKEN not set in .env")
            print("Run with --test flag to test without Discord")
        else:
            intents = discord.Intents.default()
            bot = SlipIQBot(intents=intents)
            bot.run(DISCORD_BOT_TOKEN)
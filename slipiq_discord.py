"""
SlipIQ Discord Bot
Posts MLB pitcher props, batter props, slate parlay, daily best pick, and Sharp Review.
Personal use — all picks go to your private server.
"""

import discord
import os
import asyncio
from dotenv import load_dotenv
from slipiq_lines import run_full_analysis
from slipiq_writer import generate_pick_writeup, generate_daily_brief
from slipiq_curate import select_daily_best, daily_best_summary

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")


def _env_int(name, default=0):
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


MLB_CHANNEL_ID = _env_int("CHANNEL_MLB_PITCHER_PROPS")
DAILY_BEST_CHANNEL_ID = _env_int("CHANNEL_DAILY_BEST_PICK")
SHARP_REVIEW_CHANNEL_ID = _env_int("CHANNEL_SHARP_REVIEW")
SLIP_BUILDER_CHANNEL_ID = _env_int("CHANNEL_SLIP_BUILDER")
RESULTS_PUBLIC_CHANNEL_ID = _env_int("CHANNEL_RESULTS_PUBLIC")


# ─── Helpers ──────────────────────────────────────────────────

def grade_color(grade):
    return {"A": 0x00ff88, "B": 0x3399ff, "C": 0xffaa00}.get(grade, 0x888888)


def trend_emoji(trend):
    return {"HOT": "🔥", "COLD": "❄️", "NEUTRAL": "➡️"}.get(trend, "➡️")


def direction_emoji(rec):
    return "⬆️" if "OVER" in rec else "⬇️"


# ─── Pitcher Embeds ───────────────────────────────────────────

def build_pick_embed(pick, rank, writeup=None, daily_best=False):
    direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
    grade = pick.get("grade") or pick["recommendation"].split("Grade: ")[-1].split(" |")[0].strip()
    conf = pick.get("display_confidence", pick["confidence"])
    hit_rate = pick.get("hit_rate_label", "—")

    prefix = "⭐ DAILY BEST — " if daily_best else f"#{rank} "
    embed = discord.Embed(
        title=f"{direction_emoji(pick['recommendation'])} {prefix}{pick['pitcher']} — {direction} {pick['line']} K",
        color=grade_color(grade),
    )

    if writeup and "unavailable" not in writeup.lower():
        embed.description = f"*{writeup}*"

    embed.add_field(name="🎯 Projection", value=f"**{pick['projection']} K**", inline=True)
    embed.add_field(name="📊 Season Avg", value=f"{pick.get('season_avg', 'N/A')} K", inline=True)
    embed.add_field(name="📉 Last 3", value=f"{pick.get('last_3_avg', 'N/A')} K", inline=True)
    embed.add_field(name="📈 Trend", value=f"{trend_emoji(pick['trend'])} {pick['trend']}", inline=True)
    embed.add_field(name="🏆 Grade", value=f"**{grade}**", inline=True)
    embed.add_field(name="💯 Confidence", value=f"**{conf}%**", inline=True)
    embed.add_field(name="📈 Track Record", value=hit_rate, inline=False)
    embed.add_field(name="📡 Source", value=pick["bookmaker"], inline=True)

    review = pick.get("slip_review")
    if review:
        status = "Approved" if review.get("passed") else "Caution"
        embed.add_field(
            name="Slip Review",
            value=f"{status} — {review.get('score', 0)}% | {review.get('units', 1)}u",
            inline=True,
        )

    embed.set_footer(text="SlipIQ • MLB Strikeout Model • Powered by Groq")
    return embed


def build_header_embed(picks, brief=None):
    books = {p["bookmaker"] for p in picks}
    source = ", ".join(sorted(books)) if books else "live books"
    embed = discord.Embed(
        title="⚾ SlipIQ — MLB Pitcher Props",
        description=brief or f"**{len(picks)} picks** from {source}",
        color=0x1A1A2E,
    )
    embed.set_footer(text="SlipIQ • Pitcher Props")
    return embed


# ─── Batter Embeds ────────────────────────────────────────────

def build_batter_embed(pick, rank):
    prop_labels = {"hits": "Hits", "total_bases": "Total Bases", "rbi": "RBI"}
    label = prop_labels.get(pick["prop_type"], pick["prop_type"])
    direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
    grade = pick.get("grade", "B")

    embed = discord.Embed(
        title=f"{'⬆️' if direction == 'OVER' else '⬇️'} #{rank} {pick['batter']} — {label} {direction} {pick['line']}",
        color=grade_color(grade),
    )
    embed.add_field(name="🎯 Projection", value=f"**{pick['projection']}**", inline=True)
    embed.add_field(name="📊 Season Avg", value=f"{pick['season_avg']}", inline=True)
    embed.add_field(name="📉 Last 3", value=f"{pick['last_3_avg']}", inline=True)
    embed.add_field(name="🏆 Grade", value=f"**{grade}**", inline=True)
    embed.add_field(name="💯 Confidence", value=f"**{pick['confidence']}%**", inline=True)
    embed.add_field(name="📡 Source", value=pick["bookmaker"], inline=True)
    embed.set_footer(text="SlipIQ • Batter Props Model")
    return embed


def build_batter_header_embed(picks):
    embed = discord.Embed(
        title="🏏 SlipIQ — Batter Props",
        description=f"**{len(picks)} curated picks** from live DraftKings lines",
        color=0x2B2D42,
    )
    embed.set_footer(text="SlipIQ • Batter Props")
    return embed


# ─── Bot ──────────────────────────────────────────────────────

class SlipIQBot(discord.Client):

    def __init__(
        self,
        *args,
        picks=None,
        brief=None,
        daily_best=None,
        sharp_review_stats=None,
        settled_today=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._picks = picks
        self._brief = brief
        self._daily_best = daily_best
        self._sharp_review_stats = sharp_review_stats
        self._settled_today = settled_today or []

    async def on_ready(self):
        print(f"✅ SlipIQ Bot online as {self.user}")

        if not MLB_CHANNEL_ID and not DAILY_BEST_CHANNEL_ID:
            print("❌ Set CHANNEL_MLB_PITCHER_PROPS in .env")
            await self.close()
            return

        picks = self._picks
        brief = self._brief
        daily_best = self._daily_best
        batter_picks = []

        if picks is None:
            print("🔄 Running pitcher analysis...")
            picks = run_full_analysis()

        if not picks:
            print("No pitcher picks generated today")
            await self.close()
            return

        if brief is None:
            print("✍️ Generating daily brief...")
            brief = generate_daily_brief(picks)

        if daily_best is None:
            daily_best = select_daily_best(picks)

        try:
            # ── Daily best ────────────────────────────────────
            if DAILY_BEST_CHANNEL_ID and daily_best:
                ch = await self.fetch_channel(DAILY_BEST_CHANNEL_ID)
                print(f"📌 Daily best: {daily_best_summary(daily_best)}")
                writeup = generate_pick_writeup(daily_best)
                await ch.send(embed=build_pick_embed(daily_best, 1, writeup, daily_best=True))
                await asyncio.sleep(1)

            # ── Full pitcher slate ────────────────────────────
            if MLB_CHANNEL_ID:
                channel = await self.fetch_channel(MLB_CHANNEL_ID)
                await channel.send(embed=build_header_embed(picks, brief))
                await asyncio.sleep(1)

                for i, pick in enumerate(picks, 1):
                    print(f"✍️ Writing up pick {i}/{len(picks)}...")
                    writeup = generate_pick_writeup(pick)
                    await channel.send(embed=build_pick_embed(pick, i, writeup))
                    await asyncio.sleep(1.5)

                print(f"✅ Posted {len(picks)} pitcher picks to Discord")

            # ── Batter props ──────────────────────────────────
            if MLB_CHANNEL_ID:
                try:
                    print("🔄 Running batter analysis...")
                    from slipiq_batter_lines import run_batter_analysis
                    batter_picks = run_batter_analysis()

                    if batter_picks:
                        channel = await self.fetch_channel(MLB_CHANNEL_ID)
                        await channel.send(embed=build_batter_header_embed(batter_picks))
                        await asyncio.sleep(1)

                        for i, pick in enumerate(batter_picks[:15], 1):
                            await channel.send(embed=build_batter_embed(pick, i))
                            await asyncio.sleep(1.2)

                        print(f"✅ Posted {min(len(batter_picks), 15)} batter picks to Discord")
                    else:
                        print("No batter picks today")
                except Exception as e:
                    print(f"Batter picks error: {e}")

            # ── Slate Parlay ──────────────────────────────────
            if MLB_CHANNEL_ID:
                try:
                    print("🔄 Building slate parlay...")
                    from slipiq_slate_parlay import build_slate_parlay, build_parlay_embed

                    parlay = build_slate_parlay(picks, batter_picks)

                    if parlay:
                        channel = await self.fetch_channel(MLB_CHANNEL_ID)
                        embed = build_parlay_embed(parlay)
                        if embed:
                            await channel.send(embed=embed)
                            print(f"✅ Posted slate parlay — {parlay['total_legs']} legs")
                    else:
                        print("No slate parlay built today")
                except Exception as e:
                    print(f"Slate parlay error: {e}")

            # ── Slip builder ──────────────────────────────────
            if SLIP_BUILDER_CHANNEL_ID and daily_best and daily_best.get("slip_review"):
                from slipiq_slip_review import build_slip_review_embed
                ch = await self.fetch_channel(SLIP_BUILDER_CHANNEL_ID)
                await ch.send(embed=build_slip_review_embed(daily_best))
                print("Posted slip review to #slip-builder")

            # ── Public results ────────────────────────────────
            if RESULTS_PUBLIC_CHANNEL_ID and self._settled_today:
                ch = await self.fetch_channel(RESULTS_PUBLIC_CHANNEL_ID)
                for e in self._settled_today[:8]:
                    color = 0x00FF88 if e.get("result") == "WIN" else 0xFF4444
                    embed = discord.Embed(
                        title="SlipIQ Result",
                        description=(
                            f"**{e['pitcher']}** — {e['direction']} {e['line']} K\n"
                            f"Final: {e.get('actual_strikeouts', '?')} K -> **{e['result']}**"
                        ),
                        color=color,
                    )
                    await ch.send(embed=embed)
                    await asyncio.sleep(0.5)
                print(f"Posted {len(self._settled_today)} results to #results-public")

            # ── Sharp Review ──────────────────────────────────
            if SHARP_REVIEW_CHANNEL_ID and self._sharp_review_stats:
                from slipiq_sharp_review_agent import build_sharp_review_embed
                ch = await self.fetch_channel(SHARP_REVIEW_CHANNEL_ID)
                embed = build_sharp_review_embed(self._sharp_review_stats)
                if self._settled_today:
                    summary = "\n".join(
                        f"- {e['pitcher']}: {e['direction']} {e['line']} -> "
                        f"{e.get('actual_strikeouts', '?')} K **{e['result']}**"
                        for e in self._settled_today[:10]
                    )
                    embed.add_field(name="Just settled", value=summary, inline=False)
                await ch.send(embed=embed)
                print("✅ Posted Sharp Review debrief")

        except Exception as e:
            print(f"❌ Discord error: {e}")

        await self.close()


# ─── Connection Test ──────────────────────────────────────────

CHANNEL_MAP = [
    ("CHANNEL_MLB_PITCHER_PROPS", MLB_CHANNEL_ID, "MLB pitcher + batter props + parlay"),
    ("CHANNEL_DAILY_BEST_PICK", DAILY_BEST_CHANNEL_ID, "Daily best pick"),
    ("CHANNEL_SHARP_REVIEW", SHARP_REVIEW_CHANNEL_ID, "Sharp Review"),
    ("CHANNEL_SLIP_BUILDER", SLIP_BUILDER_CHANNEL_ID, "Slip builder"),
    ("CHANNEL_RESULTS_PUBLIC", RESULTS_PUBLIC_CHANNEL_ID, "Results public"),
]


class DiscordConnectionTest(discord.Client):
    async def on_ready(self):
        print(f"Bot online as {self.user}\n")
        posted = 0
        for env_name, channel_id, label in CHANNEL_MAP:
            if not channel_id:
                print(f"  SKIP {env_name} (not set)")
                continue
            try:
                ch = await self.fetch_channel(channel_id)
                embed = discord.Embed(
                    title="SlipIQ connection test",
                    description=f"**#{ch.name}** is wired correctly.",
                    color=0x5865F2,
                )
                embed.add_field(name="Channel", value=label, inline=True)
                embed.set_footer(text="SlipIQ • --discord-test")
                await ch.send(embed=embed)
                print(f"  OK  {env_name} -> #{ch.name}")
                posted += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                print(f"  FAIL {env_name} ({channel_id}): {e}")
        if posted == 0:
            print("\nNo channels posted. Set at least one CHANNEL_* ID in .env")
        else:
            print(f"\nPosted to {posted} channel(s). Check Discord.")
        await self.close()


def run_discord_connection_test():
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN not set in .env")
        return
    intents = discord.Intents.default()
    DiscordConnectionTest(intents=intents).run(DISCORD_BOT_TOKEN)


# ─── Test Output ──────────────────────────────────────────────

def test_output():
    """Test full output without posting to Discord"""
    print("=== SlipIQ Discord Test ===\n")
    picks = run_full_analysis()

    if not picks:
        print("No pitcher picks today")
        return

    brief = generate_daily_brief(picks)
    best = select_daily_best(picks)
    print(f"Daily Brief:\n{brief}\n")
    print(f"Daily Best: {daily_best_summary(best)}\n")

    for i, pick in enumerate(picks, 1):
        writeup = generate_pick_writeup(pick)
        direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
        grade = pick.get("grade", "?")
        conf = pick.get("display_confidence", pick["confidence"])
        print(f"#{i} {pick['pitcher']} — {direction} {pick['line']} K")
        print(f"  Grade: {grade} | Confidence: {conf}% | {pick.get('hit_rate_label')}")
        print(f"  Analysis: {writeup}\n")

    print("\n--- Batter Picks ---\n")
    from slipiq_batter_lines import run_batter_analysis
    batter_picks = run_batter_analysis()

    prop_labels = {"hits": "Hits", "total_bases": "Total Bases", "rbi": "RBI"}
    for i, pick in enumerate(batter_picks[:15], 1):
        label = prop_labels.get(pick["prop_type"], pick["prop_type"])
        direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
        print(f"#{i} {pick['batter']} — {label} {direction} {pick['line']}")
        print(f"  Grade: {pick['grade']} | Confidence: {pick['confidence']}%\n")

    print("\n--- Slate Parlay ---\n")
    from slipiq_slate_parlay import build_slate_parlay, format_parlay_text
    parlay = build_slate_parlay(picks, batter_picks)
    if parlay:
        print(format_parlay_text(parlay))
    else:
        print("No parlay today")


# ─── Runner ───────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        test_output()
    elif "--discord-test" in sys.argv:
        run_discord_connection_test()
    elif not DISCORD_BOT_TOKEN:
        print("❌ DISCORD_BOT_TOKEN not set in .env")
        print("Run with --test to test without Discord")
    else:
        intents = discord.Intents.default()
        bot = SlipIQBot(intents=intents)
        bot.run(DISCORD_BOT_TOKEN)
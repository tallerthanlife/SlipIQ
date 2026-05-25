"""
SlipIQ Discord Bot
5 messages total — no more message flood
1. Daily Brief Header
2. Pitcher Props Card (all picks in one embed)
3. Batter Props Card (top 15-20 in one embed)
4. Slate Parlay Card
5. Sharp Review (if settled picks exist)
"""

import discord
import os
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from slipiq_writer import generate_daily_brief
from slipiq_curate import select_daily_best, daily_best_summary

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")


def _env_int(name, default=0):
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


MLB_CHANNEL_ID          = _env_int("CHANNEL_MLB_PITCHER_PROPS")
DAILY_BEST_CHANNEL_ID   = _env_int("CHANNEL_DAILY_BEST_PICK")
SHARP_REVIEW_CHANNEL_ID = _env_int("CHANNEL_SHARP_REVIEW")
RESULTS_PUBLIC_CHANNEL_ID = _env_int("CHANNEL_RESULTS_PUBLIC")


# ─── Helpers ──────────────────────────────────────────────────

def grade_emoji(grade):
    return {"A": "🔥", "B": "✅", "C": "⚠️"}.get(grade, "📊")

def trend_emoji(trend):
    return {"HOT": "📈", "COLD": "📉", "NEUTRAL": "➡️"}.get(trend, "➡️")

def dir_arrow(rec):
    return "⬆️" if "OVER" in str(rec) else "⬇️"

def grade_color(grade):
    return {"A": 0x00ff88, "B": 0x3399ff, "C": 0xffaa00}.get(grade, 0x888888)


# ─── MESSAGE 1: Daily Brief Header ────────────────────────────

def build_daily_header(picks, batter_picks, parlay, brief, daily_best, settled_today):
    """Single header embed summarizing the full day"""
    today = datetime.now().strftime("%A, %B %d")

    k_picks   = [p for p in picks if p.get("prop_type") == "Strikeouts"]
    out_picks  = [p for p in picks if p.get("prop_type") == "Outs Recorded"]
    hit_picks  = [p for p in picks if p.get("prop_type") == "Hits Allowed"]
    run_picks  = [p for p in picks if p.get("prop_type") == "Runs Allowed"]

    a_picks = [p for p in picks if p.get("grade") == "A"]
    b_picks = [p for p in picks if p.get("grade") == "B"]

    desc = brief or "SlipIQ daily analysis complete."

    embed = discord.Embed(
        title=f"⚾ SlipIQ — {today}",
        description=f"*{desc}*",
        color=0x1A1A2E,
    )

    # Pick counts
    pitcher_summary = f"⚾ {len(k_picks)} K | 🎯 {len(out_picks)} Outs"
    if hit_picks:
        pitcher_summary += f" | 🎳 {len(hit_picks)} Hits"
    if run_picks:
        pitcher_summary += f" | 🏃 {len(run_picks)} Runs"

    embed.add_field(name="Pitcher Props", value=pitcher_summary, inline=False)
    embed.add_field(
        name="Grades",
        value=f"🔥 Grade A: {len(a_picks)} | ✅ Grade B: {len(b_picks)}",
        inline=True,
    )
    embed.add_field(
        name="Batters",
        value=f"🏏 {len(batter_picks)} curated picks",
        inline=True,
    )

    if parlay:
        embed.add_field(
            name="Slate Parlay",
            value=f"🎯 {parlay['total_legs']} legs | {parlay['avg_confidence']}% avg conf",
            inline=True,
        )

    if daily_best:
        db = daily_best
        direction = "OVER" if "OVER" in db.get("recommendation", "") else "UNDER"
        prop = db.get("prop_type", "Strikeouts")
        conf = db.get("display_confidence", db.get("confidence", 0))
        embed.add_field(
            name="⭐ Best Pick of the Day",
            value=f"**{db['pitcher']}** — {prop} {direction} {db['line']} | {conf}% confidence",
            inline=False,
        )

    if settled_today:
        wins = sum(1 for e in settled_today if e.get("result") == "WIN")
        embed.add_field(
            name="📋 Results Settled",
            value=f"{wins}/{len(settled_today)} WIN today",
            inline=True,
        )

    embed.set_footer(text="SlipIQ • Powered by SportsData + Groq • Personal Analytics")
    return embed


# ─── MESSAGE 2: Pitcher Props Card ────────────────────────────

def build_pitcher_card(picks, max_picks=15):
    """All pitcher picks in ONE embed using compact text rows"""
    if not picks:
        return None

    # Sort by confidence, take top max_picks
    sorted_picks = sorted(picks, key=lambda x: x.get("display_confidence", x["confidence"]), reverse=True)
    top_picks = sorted_picks[:max_picks]

    prop_short = {
        "Strikeouts":   "K",
        "Outs Recorded": "Outs",
        "Hits Allowed":  "HA",
        "Runs Allowed":  "RA",
    }

    # Build compact text — one line per pick
    lines = []
    for pick in top_picks:
        direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
        prop = prop_short.get(pick.get("prop_type", "Strikeouts"), "K")
        conf = pick.get("display_confidence", pick["confidence"])
        grade = pick.get("grade", "B")
        trend = pick.get("trend", "NEUTRAL")
        proj = pick.get("projection", "?")
        line = pick["line"]
        ge = grade_emoji(grade)
        te = trend_emoji(trend)
        arrow = "⬆" if direction == "OVER" else "⬇"

        lines.append(
            f"{ge}{arrow} **{pick['pitcher']}** {prop} {direction} {line} "
            f"| Proj {proj} | {conf}% {te}"
        )

    # Split into chunks if needed (Discord 4096 char description limit)
    description = "\n".join(lines)

    # Grade breakdown
    a_count = sum(1 for p in top_picks if p.get("grade") == "A")
    b_count = sum(1 for p in top_picks if p.get("grade") == "B")
    books = {p["bookmaker"] for p in top_picks}

    embed = discord.Embed(
        title=f"⚾ Pitcher Props — Top {len(top_picks)} Picks",
        description=description,
        color=0x1A1A2E,
    )
    embed.add_field(name="🔥 Grade A", value=str(a_count), inline=True)
    embed.add_field(name="✅ Grade B", value=str(b_count), inline=True)
    embed.add_field(name="📡 Sources", value=" | ".join(sorted(books)), inline=True)
    embed.set_footer(text="SlipIQ • Pitcher Props | ge=grade te=trend")
    return embed


# ─── MESSAGE 3: Batter Props Card ─────────────────────────────

def build_batter_card(batter_picks, max_picks=15):
    """Top batter picks in ONE embed using compact text rows"""
    if not batter_picks:
        return None

    prop_labels = {
        "hits":         "H",
        "total_bases":  "TB",
        "rbi":          "RBI",
        "runs":         "R",
        "home_runs":    "HR",
    }

    sorted_picks = sorted(batter_picks, key=lambda x: x["confidence"], reverse=True)
    top_picks = sorted_picks[:max_picks]

    lines = []
    for pick in top_picks:
        direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
        prop = prop_labels.get(pick["prop_type"], pick["prop_type"])
        conf = pick["confidence"]
        grade = pick.get("grade", "B")
        proj = pick.get("projection", "?")
        line = pick["line"]
        ge = grade_emoji(grade)
        arrow = "⬆" if direction == "OVER" else "⬇"

        lines.append(
            f"{ge}{arrow} **{pick['batter']}** {prop} {direction} {line} "
            f"| Proj {proj} | {conf}%"
        )

    description = "\n".join(lines)

    a_count = sum(1 for p in top_picks if p.get("grade") == "A")
    b_count = sum(1 for p in top_picks if p.get("grade") == "B")

    embed = discord.Embed(
        title=f"🏏 Batter Props — Top {len(top_picks)} Picks",
        description=description,
        color=0x2B2D42,
    )
    embed.add_field(name="🔥 Grade A", value=str(a_count), inline=True)
    embed.add_field(name="✅ Grade B", value=str(b_count), inline=True)
    embed.add_field(name="📡 Source", value="SportsData / DraftKings", inline=True)
    embed.set_footer(text="SlipIQ • Batter Props | H=Hits TB=TotalBases RBI HR=HomeRun")
    return embed


# ─── MESSAGE 4: Slate Parlay Card ─────────────────────────────

def build_parlay_card(parlay):
    """Full slate parlay in ONE embed"""
    if not parlay:
        return None

    from slipiq_slate_parlay import build_parlay_embed
    return build_parlay_embed(parlay)


# ─── MESSAGE 5: Sharp Review Card ─────────────────────────────

def build_sharp_review_card(stats, settled_today):
    """Results and hit rate in ONE embed"""
    if not stats:
        return None

    color = 0x00FF88 if stats["overall_hit_rate"] >= 55 else 0xFF6644

    embed = discord.Embed(
        title="📋 The Sharp Review",
        description=(
            f"Overall: **{stats['overall_hit_rate']}%** "
            f"({stats['total_wins']}/{stats['total_picks']}) | "
            f"Pending: {stats['pending']}"
        ),
        color=color,
    )

    for grade in ("A", "B", "C"):
        data = stats.get("by_grade", {}).get(grade)
        if data and data["picks"] >= 2:
            embed.add_field(
                name=f"{grade_emoji(grade)} Grade {grade}",
                value=f"{data['hit_rate']}% ({data['wins']}/{data['picks']})",
                inline=True,
            )

    if settled_today:
        results_text = "\n".join(
            f"{'✅' if e.get('result') == 'WIN' else '❌'} "
            f"{e['pitcher']} {e['direction']} {e['line']} → "
            f"{e.get('actual_strikeouts', '?')} K **{e['result']}**"
            for e in settled_today[:10]
        )
        embed.add_field(name="Today's Results", value=results_text or "—", inline=False)

    embed.set_footer(text="SlipIQ • Post-Game Sharp Review")
    return embed


# ─── Daily Best Card ──────────────────────────────────────────

def build_daily_best_card(daily_best):
    """Single best pick card for free tier channel"""
    if not daily_best:
        return None

    direction = "OVER" if "OVER" in daily_best.get("recommendation", "") else "UNDER"
    grade = daily_best.get("grade", "B")
    conf = daily_best.get("display_confidence", daily_best["confidence"])
    prop = daily_best.get("prop_type", "Strikeouts")
    unit = "K" if prop == "Strikeouts" else ""

    embed = discord.Embed(
        title=f"⭐ Best Pick — {daily_best['pitcher']}",
        description=f"**{prop} {direction} {daily_best['line']} {unit}**".strip(),
        color=grade_color(grade),
    )
    embed.add_field(name="🎯 Projection", value=f"{daily_best.get('projection')} {unit}".strip(), inline=True)
    embed.add_field(name="📊 Season Avg", value=f"{daily_best.get('season_avg', 'N/A')} {unit}".strip(), inline=True)
    embed.add_field(name="📉 Last 3", value=f"{daily_best.get('last_3_avg', 'N/A')} {unit}".strip(), inline=True)
    embed.add_field(name="🏆 Grade", value=f"**{grade}**", inline=True)
    embed.add_field(name="💯 Confidence", value=f"**{conf}%**", inline=True)
    embed.add_field(name="📈 Trend", value=f"{trend_emoji(daily_best.get('trend', 'NEUTRAL'))} {daily_best.get('trend', 'NEUTRAL')}", inline=True)
    embed.add_field(name="📡 Source", value=daily_best.get("bookmaker", "—"), inline=True)
    hit_rate = daily_best.get("hit_rate_label", "")
    if hit_rate and "building" not in hit_rate.lower():
        embed.add_field(name="📈 Track Record", value=hit_rate, inline=False)
    embed.set_footer(text="SlipIQ • Best Pick of the Day • Powered by Groq")
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
        batter_picks=None,
        parlay=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._picks           = picks or []
        self._brief           = brief
        self._daily_best      = daily_best
        self._sharp_review_stats = sharp_review_stats
        self._settled_today   = settled_today or []
        self._batter_picks    = batter_picks or []
        self._parlay          = parlay

    async def on_ready(self):
        print(f"✅ SlipIQ Bot online as {self.user}")

        if not MLB_CHANNEL_ID:
            print("❌ Set CHANNEL_MLB_PITCHER_PROPS in .env")
            await self.close()
            return

        picks        = self._picks
        brief        = self._brief
        daily_best   = self._daily_best
        batter_picks = self._batter_picks
        parlay       = self._parlay

        if not picks:
            print("No pitcher picks to post")
            await self.close()
            return

        if brief is None:
            brief = generate_daily_brief(picks)
        if daily_best is None:
            daily_best = select_daily_best(picks)

        try:
            channel = await self.fetch_channel(MLB_CHANNEL_ID)

            # ── MSG 1: Daily Brief Header ─────────────────────
            header = build_daily_header(
                picks, batter_picks, parlay, brief,
                daily_best, self._settled_today
            )
            await channel.send(embed=header)
            await asyncio.sleep(1)

            # ── MSG 2: Pitcher Props Card ─────────────────────
            pitcher_card = build_pitcher_card(picks, max_picks=15)
            if pitcher_card:
                await channel.send(embed=pitcher_card)
                await asyncio.sleep(1)
                print(f"✅ Posted pitcher card — {min(len(picks), 15)} picks")

            # ── MSG 3: Batter Props Card ──────────────────────
            batter_card = build_batter_card(batter_picks, max_picks=15)
            if batter_card:
                await channel.send(embed=batter_card)
                await asyncio.sleep(1)
                print(f"✅ Posted batter card — {min(len(batter_picks), 15)} picks")
            else:
                print("No batter picks today")

            # ── MSG 4: Slate Parlay ───────────────────────────
            parlay_embed = build_parlay_card(parlay)
            if parlay_embed:
                await channel.send(embed=parlay_embed)
                await asyncio.sleep(1)
                print(f"✅ Posted slate parlay — {parlay['total_legs']} legs")
            else:
                print("No slate parlay today")

            # ── Daily Best (separate channel) ─────────────────
            if DAILY_BEST_CHANNEL_ID and daily_best:
                ch = await self.fetch_channel(DAILY_BEST_CHANNEL_ID)
                db_card = build_daily_best_card(daily_best)
                if db_card:
                    await ch.send(embed=db_card)
                    print(f"📌 Posted daily best: {daily_best_summary(daily_best)}")
                await asyncio.sleep(1)

            # ── Results (separate channel) ────────────────────
            if RESULTS_PUBLIC_CHANNEL_ID and self._settled_today:
                ch = await self.fetch_channel(RESULTS_PUBLIC_CHANNEL_ID)
                wins = sum(1 for e in self._settled_today if e.get("result") == "WIN")
                results_embed = discord.Embed(
                    title="📊 SlipIQ Results",
                    description=f"**{wins}/{len(self._settled_today)}** today",
                    color=0x00FF88 if wins > len(self._settled_today) / 2 else 0xFF4444,
                )
                for e in self._settled_today[:10]:
                    icon = "✅" if e.get("result") == "WIN" else "❌"
                    results_embed.add_field(
                        name=f"{icon} {e['pitcher']}",
                        value=f"{e['direction']} {e['line']} → {e.get('actual_strikeouts', '?')} K **{e['result']}**",
                        inline=False,
                    )
                await ch.send(embed=results_embed)
                print(f"✅ Posted {len(self._settled_today)} results")

            # ── MSG 5: Sharp Review ───────────────────────────
            if SHARP_REVIEW_CHANNEL_ID and self._sharp_review_stats:
                ch = await self.fetch_channel(SHARP_REVIEW_CHANNEL_ID)
                sr_card = build_sharp_review_card(
                    self._sharp_review_stats, self._settled_today
                )
                if sr_card:
                    await ch.send(embed=sr_card)
                    print("✅ Posted Sharp Review")

            print("\n✅ All Discord messages sent — 4 cards total")

        except Exception as e:
            print(f"❌ Discord error: {e}")
            import traceback
            traceback.print_exc()

        await self.close()


# ─── Connection Test ──────────────────────────────────────────

CHANNEL_MAP = [
    ("CHANNEL_MLB_PITCHER_PROPS", MLB_CHANNEL_ID, "Main picks channel"),
    ("CHANNEL_DAILY_BEST_PICK", DAILY_BEST_CHANNEL_ID, "Daily best pick"),
    ("CHANNEL_SHARP_REVIEW", SHARP_REVIEW_CHANNEL_ID, "Sharp Review"),
    ("CHANNEL_RESULTS_PUBLIC", RESULTS_PUBLIC_CHANNEL_ID, "Results"),
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
        print(f"\nPosted to {posted} channel(s).")
        await self.close()


def run_discord_connection_test():
    if not DISCORD_BOT_TOKEN:
        print("DISCORD_BOT_TOKEN not set in .env")
        return
    intents = discord.Intents.default()
    DiscordConnectionTest(intents=intents).run(DISCORD_BOT_TOKEN)


# ─── Test Output ──────────────────────────────────────────────

def test_output():
    """Test card formatting without posting to Discord"""
    print("=== SlipIQ Discord Test — Card Format ===\n")

    from slipiq_pitcher_props import run_full_pitcher_props_analysis
    picks = run_full_pitcher_props_analysis()

    if not picks:
        print("No pitcher picks today")
        return

    brief = generate_daily_brief(picks)
    best = select_daily_best(picks)

    print(f"Daily Best: {daily_best_summary(best)}\n")
    print(f"Brief: {brief}\n")

    print(f"--- Pitcher Card ({min(len(picks), 15)} picks) ---")
    prop_short = {"Strikeouts": "K", "Outs Recorded": "Outs", "Hits Allowed": "HA", "Runs Allowed": "RA"}
    for pick in sorted(picks, key=lambda x: x.get("display_confidence", x["confidence"]), reverse=True)[:15]:
        direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
        prop = prop_short.get(pick.get("prop_type", "Strikeouts"), "K")
        conf = pick.get("display_confidence", pick["confidence"])
        print(f"  {grade_emoji(pick.get('grade','B'))} {pick['pitcher']} {prop} {direction} {pick['line']} | {pick.get('projection')} | {conf}%")

    print(f"\n--- Batter Card ---")
    from slipiq_batter_lines import run_batter_analysis
    batter_picks = run_batter_analysis()
    prop_labels = {"hits": "H", "total_bases": "TB", "rbi": "RBI", "runs": "R", "home_runs": "HR"}
    for pick in batter_picks[:15]:
        direction = "OVER" if "OVER" in pick["recommendation"] else "UNDER"
        prop = prop_labels.get(pick["prop_type"], pick["prop_type"])
        print(f"  {grade_emoji(pick.get('grade','B'))} {pick['batter']} {prop} {direction} {pick['line']} | {pick['projection']} | {pick['confidence']}%")

    print(f"\n--- Slate Parlay ---")
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
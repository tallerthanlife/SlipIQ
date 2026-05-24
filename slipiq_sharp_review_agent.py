"""
SlipIQ Sharp Review Agent
Auto-settles pending picks from MLB box scores and posts debrief to Discord.
"""

import os
from datetime import date, timedelta
from dotenv import load_dotenv

from slipiq_mlb_data import get_pitcher_id, get_pitcher_game_log
from slipiq_db import load_results, save_results
from slipiq_results import calculate_hit_rates, update_result

load_dotenv()


def _env_int(name, default=0):
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


CHANNEL_SHARP_REVIEW = _env_int("CHANNEL_SHARP_REVIEW")


def get_actual_strikeouts(pitcher_name, game_date, search_days=2):
    """
    Strikeouts for a pitcher's start on YYYY-MM-DD, or None if not found.
    Searches ±search_days when pick was logged the evening before first pitch.
    Returns (strikeouts, actual_game_date) or (None, None).
    """
    pitcher_id, _ = get_pitcher_id(pitcher_name)
    if not pitcher_id:
        return None, None

    season = int(str(game_date)[:4])
    splits = get_pitcher_game_log(pitcher_id, season=season)
    if not splits:
        return None, None

    target = date.fromisoformat(game_date)
    candidates = [target]
    for n in range(1, search_days + 1):
        candidates.append(target - timedelta(days=n))
        candidates.append(target + timedelta(days=n))

    by_date = {s.get("date"): s for s in splits if s.get("date")}

    for day in candidates:
        key = day.isoformat()
        split = by_date.get(key)
        if not split:
            continue
        stat = split.get("stat", {})
        if stat:
            return int(stat.get("strikeOuts", 0) or 0), key

    return None, None


def grade_result(entry, strikeouts):
    """WIN / LOSS / PUSH from actual K total and pick direction."""
    line = float(entry["line"])
    direction = entry["direction"]

    if direction == "OVER":
        if strikeouts > line:
            return "WIN"
        if strikeouts < line:
            return "LOSS"
    else:
        if strikeouts < line:
            return "WIN"
        if strikeouts > line:
            return "LOSS"

    # Push only on whole-number lines (e.g. 5.0)
    if strikeouts == line and line == int(line):
        return "PUSH"
    return "LOSS"


def auto_settle_pending(max_days_back=7):
    """
    Settle pending picks when box score K totals are available.
    Returns list of updated entries.
    """
    results = load_results()
    today = date.today()
    updated = []

    for entry in results:
        if entry.get("result") not in (None, "PENDING"):
            continue

        pick_date = entry.get("date")
        if not pick_date:
            continue

        try:
            pick_day = date.fromisoformat(pick_date)
        except ValueError:
            continue

        if (today - pick_day).days > max_days_back:
            continue

        strikeouts, actual_date = get_actual_strikeouts(entry["pitcher"], pick_date)
        if strikeouts is None:
            continue

        result = grade_result(entry, strikeouts)
        extra = {
            "actual_strikeouts": strikeouts,
            "settled_by": "sharp_review_agent",
        }
        if actual_date and actual_date != pick_date:
            extra["actual_game_date"] = actual_date

        update_result(entry["pitcher"], pick_date, result, extra_fields=extra)
        entry["result"] = result
        entry.update(extra)
        updated.append(entry)
        print(
            f"  Settled: {entry['pitcher']} {entry['direction']} {entry['line']} "
            f"-> {strikeouts} K = {result}"
        )

    return updated


def build_sharp_review_message(stats=None):
    """Plain-text + stats dict for terminal or Discord."""
    stats = stats or calculate_hit_rates()
    if not stats:
        return "No settled picks yet — track record is still building.", None

    lines = [
        "**The Sharp Review**",
        f"Overall: **{stats['overall_hit_rate']}%** ({stats['total_wins']}/{stats['total_picks']})",
        f"Pending: {stats['pending']} picks",
    ]

    if stats.get("by_grade"):
        lines.append("\n**By grade**")
        for grade, data in stats["by_grade"].items():
            lines.append(f"  Grade {grade}: {data['hit_rate']}% ({data['wins']}/{data['picks']})")

    if stats.get("by_trend"):
        lines.append("\n**By trend**")
        for trend, data in stats["by_trend"].items():
            lines.append(f"  {trend}: {data['hit_rate']}% ({data['wins']}/{data['picks']})")

    return "\n".join(lines), stats


def build_sharp_review_embed(stats):
    """Discord embed for #the-sharp-review."""
    import discord

    color = 0x00ff88 if stats["overall_hit_rate"] >= 55 else 0xffaa00
    embed = discord.Embed(
        title="📋 The Sharp Review",
        description=f"Overall hit rate: **{stats['overall_hit_rate']}%** ({stats['total_wins']}/{stats['total_picks']})",
        color=color,
    )
    embed.add_field(name="Pending", value=str(stats["pending"]), inline=True)

    for grade in ("A", "B", "C"):
        data = stats.get("by_grade", {}).get(grade)
        if data:
            embed.add_field(
                name=f"Grade {grade}",
                value=f"{data['hit_rate']}% ({data['wins']}/{data['picks']})",
                inline=True,
            )

    embed.set_footer(text="SlipIQ • Post-game debrief")
    return embed


async def post_sharp_review_to_discord(client, channel_id, settled_today=None):
    """Post Sharp Review embed to Discord channel."""
    stats = calculate_hit_rates()
    if not stats:
        return False

    channel = await client.fetch_channel(channel_id)
    embed = build_sharp_review_embed(stats)

    if settled_today:
        summary = ", ".join(
            f"{e['pitcher']} ({e['result']})" for e in settled_today[:8]
        )
        embed.add_field(
            name="Today's results",
            value=summary or "—",
            inline=False,
        )

    await channel.send(embed=embed)
    return True


def run_sharp_review(post_discord=False):
    """
    Settle pending picks, print summary, optionally post to Discord.
    """
    print("\n=== Sharp Review Agent ===\n")
    print("Settling pending picks...")
    settled = auto_settle_pending()
    print(f"  {len(settled)} pick(s) settled\n")

    message, stats = build_sharp_review_message()
    print(message)

    if not post_discord or not CHANNEL_SHARP_REVIEW:
        return settled, stats

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("  Discord post skipped — no DISCORD_BOT_TOKEN")
        return settled, stats

    import discord
    import asyncio

    class ReviewBot(discord.Client):
        async def on_ready(self):
            try:
                await post_sharp_review_to_discord(self, CHANNEL_SHARP_REVIEW, settled)
                print("  ✅ Posted to #the-sharp-review")
            except Exception as e:
                print(f"  ❌ Discord post failed: {e}")
            await self.close()

    intents = discord.Intents.default()
    bot = ReviewBot(intents=intents)
    bot.run(token)
    return settled, stats


if __name__ == "__main__":
    import sys

    post = "--discord" in sys.argv
    run_sharp_review(post_discord=post)

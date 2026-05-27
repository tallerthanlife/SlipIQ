# slipiq_nba_discord.py
# NBA Discord output — posts to CHANNEL_BASKETBALL_PROPS (+ live alerts for breakouts)

from datetime import datetime

from slipiq_discord import post_message
from slipiq_env import (
    CHANNEL_BASKETBALL_PROPS,
    DISCORD_LIVE_ALERTS_CHANNEL,
)


def _tier_label(confidence: int) -> str:
    if confidence >= 70:
        return "✅ TIER 1 — STRONG EDGE"
    if confidence >= 55:
        return "⚠️ TIER 2 — LEAN"
    return "❌ SUPPRESSED"


def build_nba_pick_embed(card: dict) -> dict:
    player = card.get("player", "Unknown")
    grade = card.get("grade", "?")
    line = card.get("line")
    proj = card.get("projection")
    direction = (card.get("direction") or "over").upper()
    diff = card.get("diff", 0)
    confidence = card.get("confidence", 0)
    trend = card.get("trend", "flat")
    books_row = card.get("books_row", "No DK / Fanatics / PrizePicks lines yet")
    home = card.get("home_team", "")
    away = card.get("away_team", "")
    ev_conf = card.get("ev_confirmed", False)
    proj_min = card.get("projected_minutes")
    pace = card.get("pace_factor")
    b2b = card.get("b2b_flag", False)
    recent = card.get("recent_stat_list", [])
    prop_label = card.get("prop_label") or f"{player} {direction} {line}"

    trend_emoji = {"up": "📈", "down": "📉", "flat": "➡️"}.get(trend, "➡️")
    dir_emoji = "⬆️" if direction == "OVER" else "⬇️"
    ev_tag = " ✅ +EV confirmed" if ev_conf else ""
    tier = _tier_label(confidence)

    form_str = " → ".join(str(x) for x in recent[:5]) if recent else "N/A"
    b2b_str = "Yes ⚠️" if b2b else "No"

    fields = [
        {
            "name": "📋 Matchup",
            "value": f"{away} @ {home}" if home and away else card.get("game_date", ""),
            "inline": True,
        },
        {
            "name": "📊 Line / Projection",
            "value": f"Line: **{line}** | Proj: **{proj}** | {dir_emoji} {direction} by **{diff:+.1f}**",
            "inline": False,
        },
        {
            "name": f"{trend_emoji} Recent Form",
            "value": form_str,
            "inline": False,
        },
        {
            "name": "⏱️ Minutes / Pace",
            "value": f"Proj min: **{proj_min}** | Pace factor: **{pace}** | B2B: {b2b_str}",
            "inline": False,
        },
        {
            "name": "🎯 Confidence",
            "value": f"**{confidence}%** | Grade: **{grade}**{ev_tag}\n{tier}",
            "inline": True,
        },
        {
            "name": "💰 DK · Fanatics · PrizePicks",
            "value": f"**{books_row}**",
            "inline": False,
        },
    ]

    return {
        "title":       f"🏀 SlipIQ NBA — {prop_label}",
        "description": f"*{datetime.now().strftime('%A, %B %d')}*",
        "color":       _nba_grade_color(grade),
        "fields":      fields,
        "footer":      {"text": "SlipIQ NBA • Model-driven. Negative binomial confidence."},
        "timestamp":   datetime.utcnow().isoformat(),
    }


def _nba_grade_color(grade: str) -> int:
    colors = {
        "A+": 0x00FF88,
        "A":  0x00DD77,
        "B+": 0x44DD66,
        "B":  0x88CC44,
        "C":  0xFF8800,
    }
    return colors.get(grade, 0x888888)


def build_nba_brief_embed(slate: dict) -> dict:
    post_count = slate.get("post_count", 0)
    hold_count = slate.get("hold_count", 0)
    total = slate.get("total", 0)
    post_list = slate.get("post_list", [])
    lean_mode = slate.get("lean_mode", False)

    pick_lines = []
    for card in post_list[:5]:
        grade = card.get("grade", "?")
        if lean_mode and card.get("gate") == "LEAN":
            grade = f"{grade} LEAN"
        conf = card.get("confidence", 0)
        ev_tag = " ✅" if card.get("ev_confirmed") else ""
        prop = card.get("prop_label") or card.get("player")
        bk = card.get("books_row", "")
        pick_lines.append(f"`[{grade}]` {prop} | {conf}%{ev_tag}\n   {bk}")

    picks_str = "\n".join(pick_lines) if pick_lines else "No postable picks yet."

    if lean_mode and post_count > 0:
        status = "⚠️ Lean slate — verify lines"
    elif post_count > 0:
        status = "✅ Picks ready"
    else:
        status = "⏳ Waiting for full market"

    return {
        "title":       f"🏀 SLIPIQ NBA BRIEF — {datetime.now().strftime('%A, %B %d')}",
        "description": status,
        "color":       0xFF6B00,
        "fields": [
            {
                "name": "📋 Today's Slate",
                "value": f"**{post_count}** posting | **{hold_count}** on hold | **{total}** analyzed",
                "inline": False,
            },
            {
                "name": "🎯 Pick Summary",
                "value": picks_str,
                "inline": False,
            },
        ],
        "footer": {"text": "SlipIQ NBA • DK · Fanatics · PrizePicks only"},
        "timestamp": datetime.utcnow().isoformat(),
    }


def build_breakout_embed(candidate: dict) -> dict:
    return {
        "title":       f"🚨 BREAKOUT ALERT — {candidate.get('player')}",
        "description": f"**{candidate.get('star_out')}** OUT tonight",
        "color":       0xFF0000,
        "fields": [
            {
                "name": "Season / Projected",
                "value": (
                    f"Avg: **{candidate.get('season_avg_stat')}** pts | "
                    f"**{candidate.get('season_avg_min')}** min\n"
                    f"Tonight: **{candidate.get('projected_stat'):.1f}+** pts | "
                    f"**{candidate.get('projected_min'):.1f}+** min"
                ),
                "inline": False,
            },
            {
                "name": "Line",
                "value": f"**{candidate.get('prop_label')}** — {candidate.get('confidence')}%",
                "inline": False,
            },
            {
                "name": "⚡ Action",
                "value": "Line set before injury confirmed. Act before books adjust.",
                "inline": False,
            },
        ],
        "footer": {"text": "SlipIQ Breakout Alert • Injury-driven edge"},
        "timestamp": datetime.utcnow().isoformat(),
    }


def post_nba_morning_brief(slate: dict) -> bool:
    if not CHANNEL_BASKETBALL_PROPS:
        print("  [nba_discord] CHANNEL_BASKETBALL_PROPS not set — skip")
        return False

    if not slate.get("post_list") and not slate.get("best_pick"):
        return post_nba_waiting_message()

    post_message(CHANNEL_BASKETBALL_PROPS, embed=build_nba_brief_embed(slate))

    best = slate.get("best_pick")
    if best:
        post_message(CHANNEL_BASKETBALL_PROPS, embed=build_nba_pick_embed(best))

    for card in slate.get("post_list", [])[1:]:
        post_message(CHANNEL_BASKETBALL_PROPS, embed=build_nba_pick_embed(card))

    return True


def post_nba_waiting_message() -> bool:
    if not CHANNEL_BASKETBALL_PROPS:
        print("  [nba_discord] CHANNEL_BASKETBALL_PROPS not set — skip")
        return False
    content = (
        f"🏀 **SlipIQ NBA — {datetime.now().strftime('%A, %B %d')}**\n\n"
        f"⏳ Analyzing today's NBA slate. Full picks post once "
        f"DraftKings / Fanatics / PrizePicks open.\n\n"
        f"*Model running. Lines loading.*"
    )
    return post_message(CHANNEL_BASKETBALL_PROPS, content=content)


def post_breakout_alert(candidate: dict) -> bool:
    embed = build_breakout_embed(candidate)
    ok = True
    if DISCORD_LIVE_ALERTS_CHANNEL:
        ok = post_message(DISCORD_LIVE_ALERTS_CHANNEL, embed=embed)
    if CHANNEL_BASKETBALL_PROPS:
        summary = f"🚨 **Breakout:** {candidate.get('prop_label')} — see live alert"
        post_message(CHANNEL_BASKETBALL_PROPS, content=summary)
    return ok


if __name__ == "__main__":
    print("SlipIQ NBA Discord — use slipiq_nba_orchestrator.py to post")

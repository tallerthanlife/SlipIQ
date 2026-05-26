# slipiq_discord.py
# Discord formatter + poster for SlipIQ
# Reads cache/agent_slate.json from confidence agent
# Posts to correct channels based on output type
#
# CHANNELS:
#   #daily-picks   → best pick of the day (morning post)
#   #live-alerts   → pre-game line moves + lineup updates
#   #sharp-review  → post-game CLV results + grade
#
# MESSAGE TYPES:
#   morning_brief  → full slate summary + best pick
#   pick_card      → individual pick post
#   line_move      → alert when a line moves significantly
#   sharp_review   → post-game result + CLV grade

import json
import requests
from datetime import datetime
from pathlib import Path

from slipiq_env import (
    CHANNEL_TEAM_PARLAY,
    DISCORD_BOT_TOKEN,
    DISCORD_DAILY_PICKS_CHANNEL,
    DISCORD_LIVE_ALERTS_CHANNEL,
    DISCORD_SHARP_REVIEW_CHANNEL,
)

DISCORD_TEAM_PARLAY_CHANNEL = CHANNEL_TEAM_PARLAY

CACHE_DIR  = Path("cache")
DISCORD_API = "https://discord.com/api/v10"

HEADERS = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type": "application/json",
}


# ═════════════════════════════════════════
# CORE POSTER
# ═════════════════════════════════════════

def post_message(channel_id: str, content: str = None, embed: dict = None) -> bool:
    """
    Post a message to a Discord channel.
    Supports plain text and rich embeds.
    Returns True on success.
    """
    if not DISCORD_BOT_TOKEN:
        print("  [discord] ERROR: DISCORD_BOT_TOKEN not set in .env")
        return False

    if not channel_id:
        print("  [discord] ERROR: channel_id is None — check .env channel IDs")
        return False

    payload = {}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]

    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    r   = requests.post(url, headers=HEADERS, json=payload, timeout=10)

    if r.status_code == 200:
        print(f"  [discord] OK posted to channel {channel_id}")
        return True
    else:
        print(f"  [discord] FAIL {r.status_code}: {r.text[:200]}")
        return False


# ═════════════════════════════════════════
# EMBED BUILDERS
# ═════════════════════════════════════════

def _grade_color(grade: str) -> int:
    """Discord embed color as integer by grade."""
    return {
        "A":   0x00FF88,   # bright green
        "B+":  0x44DD66,   # green
        "B":   0x88CC44,   # yellow-green
        "B-":  0xCCAA00,   # yellow
        "C+":  0xFF8800,   # orange
        "C":   0xFF5500,   # orange-red
        "D":   0xFF2200,   # red
    }.get(grade, 0x888888)


def build_best_pick_embed(card: dict) -> dict:
    """
    Rich embed for #daily-picks — best pick of the day.
    Clean, readable, no internal data exposed.
    """
    player     = card.get("player", "Unknown")
    grade      = card.get("grade", "?")
    line       = card.get("line")
    proj       = card.get("projection")
    direction  = card.get("direction", "").upper()
    diff       = card.get("diff", 0)
    confidence = card.get("confidence", 0)
    trend      = card.get("trend", "flat")
    k_list     = card.get("recent_k_list", [])
    best_book  = card.get("best_book")
    ev         = card.get("ev_value")
    home       = card.get("home_team", "")
    away       = card.get("away_team", "")
    game_date  = card.get("game_date", "")
    ev_conf    = card.get("ev_confirmed", False)

    # Trend emoji
    trend_emoji = {"hot": "🔥", "flat": "➡️", "cold": "🧊"}.get(trend, "➡️")

    # Direction emoji
    dir_emoji = "⬆️" if direction == "OVER" else "⬇️"

    # EV tag
    ev_tag = f" ✅ +EV confirmed" if ev_conf else ""

    # Recent form string
    form_str = " → ".join(str(k) for k in k_list) if k_list else "N/A"

    # Action books only — DK | Fanatics | PrizePicks (sharp line is internal)
    books_row = card.get("books_row")
    if books_row:
        book_line = f"**{books_row}**"
    elif best_book:
        book_line = (
            f"**{best_book['side'].upper()} "
            f"{best_book['price']} "
            f"@ {best_book['book']}**"
        )
    else:
        book_line = "No DK / Fanatics / PrizePicks lines yet"

    fields = [
        {
            "name": "📋 Matchup",
            "value": f"{away} @ {home}" if home and away else game_date,
            "inline": True,
        },
        {
            "name": "📊 Line / Projection",
            "value": f"Line: **{line}** | Proj: **{proj}** | {dir_emoji} {direction} by **{diff:+.2f}**",
            "inline": False,
        },
        {
            "name": f"{trend_emoji} Recent Form (last {len(k_list)} starts)",
            "value": form_str,
            "inline": False,
        },
        {
            "name": "🎯 Confidence",
            "value": f"**{confidence}%** | Grade: **{grade}**{ev_tag}",
            "inline": True,
        },
        {
            "name": "💰 DK · Fanatics · PrizePicks",
            "value": book_line,
            "inline": False,
        },
    ]

    return {
        "title":       f"⚾ SlipIQ Pick — {player} Strikeouts",
        "description": f"*Best pick of the day — {datetime.now().strftime('%A, %B %d')}*",
        "color":       _grade_color(grade),
        "fields":      fields,
        "footer": {
            "text": "SlipIQ • Model-driven. Sharp-anchored. Always improving."
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


def build_morning_brief_embed(slate: dict) -> dict:
    """
    Morning brief embed — slate summary for #daily-picks.
    Posted before the best pick card.
    """
    post_count = slate.get("post_count", 0)
    hold_count = slate.get("hold_count", 0)
    total      = slate.get("total", 0)
    post_list  = slate.get("post_list", [])
    lean_mode  = slate.get("lean_mode", False)

    # Build pick summary lines
    pick_lines = []
    for card in post_list[:5]:  # cap at 5 in brief
        grade     = card.get("grade", "?")
        if lean_mode and card.get("gate") == "LEAN":
            grade = f"{grade} LEAN"
        player    = card.get("player", "")
        direction = card.get("direction", "").upper()
        line      = card.get("line")
        conf      = card.get("confidence", 0)
        ev_conf   = card.get("ev_confirmed", False)
        ev_tag    = " ✅" if ev_conf else ""
        bk_row = card.get("books_row", "")
        bk_snip = f"\n   {bk_row}" if bk_row else ""
        pick_lines.append(
            f"`[{grade}]` {player} — {direction} {line} | {conf}%{ev_tag}{bk_snip}"
        )

    picks_str = "\n".join(pick_lines) if pick_lines else "No postable picks yet."

    fields = [
        {
            "name":   "📋 Today's Slate",
            "value":  f"**{post_count}** picks posting | **{hold_count}** on hold | **{total}** analyzed",
            "inline": False,
        },
        {
            "name":   "🎯 Pick Summary",
            "value":  picks_str,
            "inline": False,
        },
    ]

    if lean_mode and post_count > 0:
        status = "⚠️ Lean slate — thin market (verify lines)"
    elif post_count > 0:
        status = "✅ Picks ready"
    else:
        status = "⏳ Waiting for full market"

    return {
        "title":       f"☀️ SlipIQ Morning Brief — {datetime.now().strftime('%A, %B %d')}",
        "description": status,
        "color":       0x1DA1F2,  # SlipIQ blue
        "fields":      fields,
        "footer": {
            "text": "SlipIQ • Picks update as books open. Full slate by 9am AZ."
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


def build_line_move_embed(player: str, old_line: float, new_line: float,
                           direction: str, book: str, game: str = "") -> dict:
    """
    Line movement alert embed for #live-alerts.
    Fires when a line moves ≥0.5 pts.
    """
    move    = new_line - old_line
    move_dir = "⬆️ UP" if move > 0 else "⬇️ DOWN"
    color   = 0xFF8800 if abs(move) >= 1.0 else 0xFFCC00

    return {
        "title":       f"📡 Line Move — {player}",
        "description": f"{game}" if game else "MLB Pitcher Strikeouts",
        "color":       color,
        "fields": [
            {
                "name":   "Movement",
                "value":  f"{old_line} → **{new_line}** ({move_dir} {abs(move):.1f})",
                "inline": True,
            },
            {
                "name":   "Book",
                "value":  book,
                "inline": True,
            },
        ],
        "footer":    {"text": "SlipIQ Live Alerts"},
        "timestamp": datetime.utcnow().isoformat(),
    }


def build_sharp_review_embed(player: str, pick_direction: str, line: float,
                              actual_ks: int, proj: float, grade: str,
                              clv: float = None, book: str = None,
                              book_price: int = None,
                              closing_price: int = None) -> dict:
    """
    Post-game Sharp Review embed for #sharp-review.
    Shows result + CLV grade.
    """
    hit      = (pick_direction == "over" and actual_ks > line) or \
               (pick_direction == "under" and actual_ks < line)
    result   = "✅ HIT" if hit else "❌ MISS"
    color    = 0x00FF88 if hit else 0xFF2200

    fields = [
        {
            "name":   "Pick",
            "value":  f"{pick_direction.upper()} {line} Ks",
            "inline": True,
        },
        {
            "name":   "Result",
            "value":  f"**{actual_ks} Ks** — {result}",
            "inline": True,
        },
        {
            "name":   "Model Projection",
            "value":  f"{proj} Ks",
            "inline": True,
        },
    ]

    if clv is not None:
        clv_tag = "✅ Beat closing" if clv > 0 else "❌ Closing moved against"
        fields.append({
            "name":   "CLV",
            "value":  f"{clv:+.2f} units — {clv_tag}",
            "inline": True,
        })

    if book and book_price and closing_price:
        fields.append({
            "name":   "Line Shopping",
            "value":  f"Got {book_price} @ {book} | Closed {closing_price}",
            "inline": False,
        })

    return {
        "title":       f"🔍 Sharp Review — {player}",
        "description": f"Grade: **{grade}** | {datetime.now().strftime('%B %d, %Y')}",
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "SlipIQ Sharp Review • Track everything."},
        "timestamp":   datetime.utcnow().isoformat(),
    }


# ═════════════════════════════════════════
# HIGH LEVEL POSTING FUNCTIONS
# ═════════════════════════════════════════

def post_morning_brief(slate: dict) -> bool:
    """
    Post morning brief + best pick to #daily-picks.
    Called by slipiq_curate.py at 6:30am.
    """
    if not slate.get("post_list") and not slate.get("best_pick"):
        # Post a waiting message instead
        content = (
            f"☀️ **SlipIQ Morning Brief — "
            f"{datetime.now().strftime('%A, %B %d')}**\n"
            f"⏳ Markets still opening. Full slate incoming by 9am AZ."
        )
        return post_message(DISCORD_DAILY_PICKS_CHANNEL, content=content)

    # Brief summary first
    brief_embed = build_morning_brief_embed(slate)
    post_message(DISCORD_DAILY_PICKS_CHANNEL, embed=brief_embed)

    # Best pick card
    best = slate.get("best_pick")
    if best:
        pick_embed = build_best_pick_embed(best)
        return post_message(DISCORD_DAILY_PICKS_CHANNEL, embed=pick_embed)

    return True


def post_pick_update(card: dict) -> bool:
    """
    Post a single pick update to #daily-picks.
    Called when a HOLD pick upgrades to POST after line confirmation.
    """
    embed = build_best_pick_embed(card)
    header = f"📬 **Pick Update** — {card.get('player')}"
    post_message(DISCORD_DAILY_PICKS_CHANNEL, content=header)
    return post_message(DISCORD_DAILY_PICKS_CHANNEL, embed=embed)


def post_line_move_alert(player: str, old_line: float, new_line: float,
                          direction: str, book: str, game: str = "") -> bool:
    """
    Post line movement alert to #live-alerts.
    Called by slipiq_parlayapi.check_line_movement().
    """
    embed = build_line_move_embed(player, old_line, new_line, direction, book, game)
    return post_message(DISCORD_LIVE_ALERTS_CHANNEL, embed=embed)


def post_sharp_review(player: str, pick_direction: str, line: float,
                       actual_ks: int, proj: float, grade: str,
                       clv: float = None, book: str = None,
                       book_price: int = None,
                       closing_price: int = None) -> bool:
    """
    Post Sharp Review result to #sharp-review.
    Called by slipiq_sharp_review.py post-game.
    """
    embed = build_sharp_review_embed(
        player, pick_direction, line, actual_ks,
        proj, grade, clv, book, book_price, closing_price
    )
    return post_message(DISCORD_SHARP_REVIEW_CHANNEL, embed=embed)


def post_waiting_message() -> bool:
    """
    Post early morning waiting message to #daily-picks.
    Books not open yet — sets expectations.
    """
    content = (
        f"☀️ **SlipIQ — {datetime.now().strftime('%A, %B %d')}**\n\n"
        f"⏳ Analyzing today's slate. Full picks post by 9am AZ "
        f"once DraftKings / Fanatics / PrizePicks open.\n\n"
        f"*Model running. Lines loading.*"
    )
    return post_message(DISCORD_DAILY_PICKS_CHANNEL, content=content)


# ═════════════════════════════════════════
# SLATE RUNNER — main entry point
# ═════════════════════════════════════════

def build_parlay_menu_embed(pool: list[dict], title_suffix: str = "") -> dict:
    """Ranked leg menu for private parlay channel."""
    lines = []
    for i, card in enumerate(pool, 1):
        prop = card.get("prop_label") or _parlay_card_line(card)
        away = card.get("away_team", "")
        home = card.get("home_team", "")
        matchup = f"{away} @ {home}" if home and away else ""
        bk = card.get("books_row", "")
        ev_tag = " ✅" if card.get("ev_confirmed") else ""
        lines.append(
            f"**{i}.** {prop}{ev_tag}\n"
            f"   {matchup}\n"
            f"   {bk}"
        )

    body = "\n\n".join(lines[:15])
    if len(body) > 3900:
        body = body[:3900] + "\n…"

    suffix = f" — {title_suffix}" if title_suffix else ""
    return {
        "title":       f"📋 Parlay Menu{suffix} — {len(pool)} Legs",
        "description": (
            f"*{datetime.now().strftime('%A, %B %d')} — "
            f"DK · Fanatics · PrizePicks only*"
        ),
        "color":       0x9B59B6,
        "fields":      [{
            "name":  "Ranked picks (verify lines before betting)",
            "value": body or "No legs today",
            "inline": False,
        }],
        "footer":      {"text": "SlipIQ Parlay Alerts • Model vs line probability"},
        "timestamp":   datetime.utcnow().isoformat(),
    }


def _parlay_card_line(card: dict) -> str:
    """Fallback formatter when prop_label missing."""
    if card.get("market") == "f5_ml":
        return (
            f"[{card.get('grade')}] **{card.get('pick_team')}** F5 ML — "
            f"{card.get('confidence')}%"
        )
    direction = (card.get("direction") or "over").upper()
    return (
        f"[{card.get('grade')}] **{card.get('player')}** "
        f"{direction} {card.get('line')} K — {card.get('confidence')}%"
    )


def build_parlay_slip_embed(slip: dict) -> dict | None:
    """(B) One suggested slip embed."""
    if not slip or not slip.get("legs"):
        return None

    leg_lines = []
    for leg in slip["legs"]:
        leg_lines.append(
            f"**{leg['n']}.** [{leg['grade']}] {leg['label']} — {leg['confidence']}%\n"
            f"   {leg.get('books_row', '')}"
        )
    text = "\n".join(leg_lines)
    if len(text) > 1024:
        text = text[:1020] + "…"

    return {
        "title":       f"🎯 {slip.get('title', 'Suggested Slip')}",
        "description": f"Avg confidence **{slip.get('avg_conf', 0)}%** · {slip.get('games', 0)} games",
        "color":       0x00FF88,
        "fields":      [{
            "name":   "Legs",
            "value":  text,
            "inline": False,
        }, {
            "name":   "Books",
            "value":  "DraftKings · Fanatics · PrizePicks — verify before submit",
            "inline": False,
        }],
        "footer":      {"text": "SlipIQ • Suggested slip — not auto-placed"},
        "timestamp":   datetime.utcnow().isoformat(),
    }


def post_parlay_channel(
    pool: list[dict],
    slips: dict,
    f5_picks: list[dict] | None = None,
    sgp_slips: list[dict] | None = None,
) -> bool:
    """Post pitcher K menu, F5 ML picks, core slip, and SGP builds to CHANNEL_TEAM_PARLAY."""
    channel = DISCORD_TEAM_PARLAY_CHANNEL
    if not channel:
        print("  [discord] CHANNEL_TEAM_PARLAY not set — skip parlay alerts")
        return False

    f5_picks = f5_picks or []
    sgp_slips = sgp_slips or []

    header = (
        f"🎰 **SlipIQ Parlay Alerts — "
        f"{datetime.now().strftime('%A, %B %d')}**\n"
        f"Pitcher K props + F5 ML · DK · Fanatics · PrizePicks · verify before submit"
    )
    post_message(channel, content=header)

    ok = True
    if pool:
        menu_embed = build_parlay_menu_embed(pool, title_suffix="Pitcher K")
        ok = post_message(channel, embed=menu_embed) and ok

    if f5_picks:
        f5_embed = build_parlay_menu_embed(f5_picks, title_suffix="F5 Moneyline")
        post_message(channel, content="**First 5 Innings ML — model edge picks**")
        ok = post_message(channel, embed=f5_embed) and ok

    core = slips.get("slip_core")
    if core:
        emb = build_parlay_slip_embed(core)
        if emb:
            post_message(channel, content="**Suggested pitcher K core**")
            ok = post_message(channel, embed=emb) and ok

    for sgp in sgp_slips[:4]:
        emb = build_parlay_slip_embed(sgp)
        if emb:
            post_message(channel, content="**Correlated SGP (K + F5 ML)**")
            ok = post_message(channel, embed=emb) and ok

    return ok


def run_discord_post(slate: dict = None) -> bool:
    """
    Main entry point. Called by slipiq_curate.py.
    Loads slate from cache if not passed directly.
    """
    if not slate:
        slate_path = CACHE_DIR / "agent_slate.json"
        if not slate_path.exists():
            print("  [discord] No slate cache found — run confidence agent first")
            return False
        with open(slate_path) as f:
            slate = json.load(f)

    post_count = slate.get("post_count", 0)
    print(f"\n  [discord] Posting slate: {post_count} picks to #daily-picks")
    return post_morning_brief(slate)


# ═════════════════════════════════════════
# ENV VALIDATOR
# ═════════════════════════════════════════

def validate_discord_env() -> bool:
    """Check all required Discord env vars are set."""
    required = {
        "DISCORD_BOT_TOKEN":            DISCORD_BOT_TOKEN,
        "DISCORD_DAILY_PICKS_CHANNEL":  DISCORD_DAILY_PICKS_CHANNEL,
        "CHANNEL_TEAM_PARLAY":          CHANNEL_TEAM_PARLAY,
        "DISCORD_LIVE_ALERTS_CHANNEL":  DISCORD_LIVE_ALERTS_CHANNEL,
        "DISCORD_SHARP_REVIEW_CHANNEL": DISCORD_SHARP_REVIEW_CHANNEL,
    }
    all_good = True
    for key, val in required.items():
        if not val:
            print(f"  [discord] MISSING: {key}")
            all_good = False
        else:
            print(f"  [discord] OK {key} set")
    return all_good


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Discord Integration Test")
    print("=" * 60)

    print("\n[1] Validating Discord environment...")
    valid = validate_discord_env()

    if not valid:
        print("\n  ❌ Discord env not configured.")
        print("  Add these to your .env file:")
        print("    DISCORD_BOT_TOKEN=your_bot_token")
        print("    DISCORD_DAILY_PICKS_CHANNEL=channel_id")
        print("    CHANNEL_TEAM_PARLAY=channel_id")
        print("    DISCORD_LIVE_ALERTS_CHANNEL=channel_id")
        print("    DISCORD_SHARP_REVIEW_CHANNEL=channel_id")
        print("\n  Then re-run this file.")
    else:
        print("\n[2] Loading agent slate from cache...")
        slate_path = CACHE_DIR / "agent_slate.json"

        if not slate_path.exists():
            print("  No slate found — run slipiq_confidence_agent.py first")
        else:
            with open(slate_path) as f:
                slate = json.load(f)

            post_count = slate.get("post_count", 0)
            print(f"  Slate loaded: {post_count} picks ready to post")

            print("\n[3] Posting to Discord...")
            success = run_discord_post(slate)

            if success:
                print("\n  ✅ Discord post complete")
            else:
                print("\n  ❌ Discord post failed — check token and channel IDs")

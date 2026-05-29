# slipiq_writer.py
# AI write-up generator for SlipIQ pick cards
# Model: Groq llama-3.3-70b-versatile
# Used for: Discord narrative posts, pick card summaries
#
# WRITE-UP TYPES:
#   pitcher_pick   → full narrative for pitcher strikeout pick
#   batter_pick    → full narrative for batter prop pick
#   morning_brief  → slate summary write-up
#   sharp_review   → post-game result narrative
#
# TONE:
#   Sharp, data-driven, confident but not arrogant
#   No filler phrases ("great pick", "love this one")
#   Lead with the edge, support with the data
#   Short — Discord readers scroll fast

import os
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_URL     = "https://api.groq.com/openai/v1/chat/completions"

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────
# SYSTEM PROMPT — SlipIQ voice
# ─────────────────────────────────────────
SYSTEM_PROMPT = """You are the write-up engine for SlipIQ, an AI-powered sports prop analytics bot.

Your job is to write sharp, concise pick narratives for Discord posts.

TONE RULES:
- Data-driven and direct. Lead with the edge, back it with stats.
- No hype. No "love this pick." No "this is a must-bet."
- Confident but honest. Acknowledge risk when it exists.
- Short. Discord readers scroll fast. 3-5 sentences max per pick.
- Use numbers. Whiff rate, K rate, recent form, line movement — make it concrete.
- End with one clear action line: what to bet, where, at what price.

FORMAT:
- No headers or bullet points unless writing a morning brief
- Plain text — Discord renders markdown but keep it minimal
- Bold (**) the key stat or edge signal only

Never invent statistics. Only use what's provided in the pick card data."""


# ═════════════════════════════════════════
# GROQ API CALL
# ═════════════════════════════════════════

def call_groq(prompt: str, max_tokens: int = 300) -> str:
    """
    Call Groq API with llama-3.3-70b-versatile.
    Returns generated text or error string.
    """
    try:
        import requests
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type":  "application/json",
        }
        payload = {
            "model":       GROQ_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            "max_tokens":  max_tokens,
            "temperature": 0.7,
        }
        r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()

    except Exception as e:
        print(f"  [writer] Groq error: {e}")
        return ""


# ═════════════════════════════════════════
# PROMPT BUILDERS
# ═════════════════════════════════════════

def build_pitcher_prompt(card: dict) -> str:
    player     = card.get("player", "")
    line       = card.get("line")
    proj       = card.get("projection")
    direction  = card.get("direction", "").upper()
    diff       = card.get("diff", 0)
    confidence = card.get("confidence", 0)
    grade      = card.get("grade", "")
    trend      = card.get("trend", "flat")
    k_list     = card.get("recent_k_list", [])
    ev         = card.get("ev_value")
    best_book  = card.get("best_book")
    home       = card.get("home_team", "")
    away       = card.get("away_team", "")
    pinnacle   = card.get("pinnacle_line")
    whiff      = card.get("season_whiff")
    k_rate     = card.get("season_k_rate")

    # Internal data for context
    internal   = card.get("_internal", {})
    season_proj = internal.get("season_proj")
    recent_proj = internal.get("recent_proj")

    book_line = ""
    if best_book:
        book_line = (f"Best bet: {best_book['side'].upper()} "
                     f"{best_book['price']} @ {best_book['book']}")

    prompt = f"""Write a sharp 3-4 sentence Discord pick narrative for this pitcher strikeout prop.

PICK DATA:
Player: {player}
Game: {away} @ {home}
Line: {line} Ks | Direction: {direction} | Grade: {grade}
Projection: {proj} Ks (Season model: {season_proj}, Recent form: {recent_proj})
Edge: {diff:+.2f} Ks vs line
Confidence: {confidence}%
Trend: {trend}
Last {len(k_list)} starts: {k_list}
Season K rate: {k_rate} | Whiff rate: {whiff}
Pinnacle line: {pinnacle if pinnacle else 'not posted yet'}
EV vs Pinnacle: {f'{ev:+.1%}' if ev else 'unconfirmed'}
{book_line}

Write the Discord post. Lead with the sharpest data point. End with the bet."""

    return prompt


def build_batter_prompt(card: dict) -> str:
    player    = card.get("player", "")
    market    = card.get("market", "").replace("player_", "")
    line      = card.get("line")
    proj      = card.get("projection")
    direction = card.get("direction", "").upper()
    diff      = card.get("diff", 0)
    conf      = card.get("confidence", 0)
    grade     = card.get("grade", "")
    trend     = card.get("trend", "flat")
    ev        = card.get("ev_value")
    best_book = card.get("best_book")
    home      = card.get("home_team", "")
    away      = card.get("away_team", "")

    internal    = card.get("_internal", {})
    exit_velo   = internal.get("exit_velo")
    season_proj = internal.get("season_proj")

    book_line = ""
    if best_book:
        book_line = (f"Best bet: {best_book['side'].upper()} "
                     f"{best_book['price']} @ {best_book['book']}")

    prompt = f"""Write a sharp 3-4 sentence Discord pick narrative for this batter prop.

PICK DATA:
Player: {player}
Game: {away} @ {home}
Market: {market} | Line: {line} | Direction: {direction} | Grade: {grade}
Projection: {proj} (Season baseline: {season_proj})
Edge: {diff:+.3f} vs line
Confidence: {conf}%
Trend (last 10 games): {trend}
Avg exit velocity: {exit_velo} mph
EV vs Pinnacle: {f'{ev:+.1%}' if ev else 'unconfirmed'}
{book_line}

Write the Discord post. Lead with the sharpest data point. End with the bet."""

    return prompt


def build_morning_brief_prompt(slate: dict, top_picks: list) -> str:
    date       = datetime.now().strftime("%A, %B %d")
    post_count = len(top_picks)
    total      = slate.get("total", 0)

    pick_lines = []
    for card in top_picks[:3]:
        player    = card.get("player", "")
        direction = card.get("direction", "").upper()
        line      = card.get("line")
        grade     = card.get("grade", "")
        conf      = card.get("confidence", 0)
        pick_lines.append(
            f"- {player}: {direction} {line} [{grade}] {conf}%"
        )

    picks_str = "\n".join(pick_lines)

    prompt = f"""Write a sharp 2-3 sentence morning brief for SlipIQ's Discord.

DATE: {date}
PICKS POSTING TODAY: {post_count} of {total} analyzed
TOP PICKS:
{picks_str}

Keep it tight. Set the tone for the day. No hype."""

    return prompt


def build_sharp_review_prompt(result: dict) -> str:
    player    = result.get("player", "")
    outcome   = result.get("outcome", "")
    line      = result.get("line")
    actual    = result.get("actual_ks")
    proj      = result.get("proj")
    direction = result.get("direction", "")
    clv       = result.get("clv")
    sr_grade  = result.get("sr_grade", "")
    proj_tag  = result.get("proj_tag", "")

    prompt = f"""Write a sharp 2-3 sentence Sharp Review post for SlipIQ's Discord.

RESULT DATA:
Player: {player}
Pick: {direction.upper()} {line} Ks
Result: {actual} Ks — {outcome}
Model projection: {proj} Ks ({proj_tag})
CLV: {f'{clv:+.2f}' if clv is not None else 'N/A'}
Sharp Review grade: {sr_grade}

Be honest. If it was a miss, own it and explain what the model saw.
If CLV was positive despite a miss, note the process was sound.
Keep it under 3 sentences."""

    return prompt


# ═════════════════════════════════════════
# PUBLIC WRITE-UP FUNCTIONS
# ═════════════════════════════════════════

def write_pitcher_pick(card: dict) -> str:
    """Generate Discord narrative for a pitcher strikeout pick."""
    if not GROQ_API_KEY:
        return _fallback_pitcher(card)

    prompt = build_pitcher_prompt(card)
    text   = call_groq(prompt, max_tokens=250)
    return text if text else _fallback_pitcher(card)


def write_batter_pick(card: dict) -> str:
    """Generate Discord narrative for a batter prop pick."""
    if not GROQ_API_KEY:
        return _fallback_batter(card)

    prompt = build_batter_prompt(card)
    text   = call_groq(prompt, max_tokens=250)
    return text if text else _fallback_batter(card)


def write_morning_brief(slate: dict, top_picks: list) -> str:
    """Generate morning brief narrative."""
    if not GROQ_API_KEY:
        return _fallback_brief(top_picks)

    prompt = build_morning_brief_prompt(slate, top_picks)
    text   = call_groq(prompt, max_tokens=150)
    return text if text else _fallback_brief(top_picks)


def write_sharp_review(result: dict) -> str:
    """Generate Sharp Review result narrative."""
    if not GROQ_API_KEY:
        return _fallback_review(result)

    prompt = build_sharp_review_prompt(result)
    text   = call_groq(prompt, max_tokens=200)
    return text if text else _fallback_review(result)


# ═════════════════════════════════════════
# FALLBACKS — no API key or Groq down
# ═════════════════════════════════════════

def _fallback_pitcher(card: dict) -> str:
    player    = card.get("player", "")
    direction = card.get("direction", "").upper()
    line      = card.get("line")
    proj      = card.get("projection")
    diff      = card.get("diff", 0)
    conf      = card.get("confidence", 0)
    grade     = card.get("grade", "")
    best      = card.get("best_book")
    k_list    = card.get("recent_k_list", [])

    bet_str = ""
    if best:
        bet_str = f"▶ {best['side'].upper()} {best['price']} @ {best['book']}"

    return (
        f"**{player} — {direction} {line} Ks** [{grade}]\n"
        f"Model projects {proj} Ks ({diff:+.2f} edge). "
        f"Recent: {k_list}. Confidence: {conf}%.\n"
        f"{bet_str}"
    ).strip()


def _fallback_batter(card: dict) -> str:
    player    = card.get("player", "")
    market    = card.get("market", "").replace("player_", "")
    direction = card.get("direction", "").upper()
    line      = card.get("line")
    proj      = card.get("projection")
    conf      = card.get("confidence", 0)
    grade     = card.get("grade", "")
    best      = card.get("best_book")

    bet_str = ""
    if best:
        bet_str = f"▶ {best['side'].upper()} {best['price']} @ {best['book']}"

    return (
        f"**{player} — {direction} {line} {market}** [{grade}]\n"
        f"Model projects {proj}. Confidence: {conf}%.\n"
        f"{bet_str}"
    ).strip()


def _fallback_brief(top_picks: list) -> str:
    date = datetime.now().strftime("%A, %B %d")
    if not top_picks:
        return f"☀️ SlipIQ — {date}\nAnalyzing today's slate. Picks incoming."
    lines = [f"**{c['player']}** {c['direction'].upper()} {c['line']} [{c['grade']}]"
             for c in top_picks[:3]]
    return f"☀️ SlipIQ — {date}\n" + " | ".join(lines)


def _fallback_review(result: dict) -> str:
    outcome = result.get("outcome", "")
    player  = result.get("player", "")
    actual  = result.get("actual_ks")
    line    = result.get("line")
    clv     = result.get("clv")
    emoji   = "✅" if outcome == "HIT" else ("➡️" if outcome == "PUSH" else "❌")
    clv_str = f" | CLV: {clv:+.2f}" if clv is not None else ""
    return f"{emoji} {player} — {outcome} ({actual} Ks vs {line} line){clv_str}"


# ═════════════════════════════════════════
# BATCH WRITER
# ═════════════════════════════════════════

def write_slate(cards: list[dict], card_type: str = "pitcher") -> list[dict]:
    """
    Generate write-ups for a full slate of pick cards.
    Adds 'writeup' field to each card.
    card_type: 'pitcher' or 'batter'
    """
    writer = write_pitcher_pick if card_type == "pitcher" else write_batter_pick

    for card in cards:
        player = card.get("player", "")
        print(f"  [writer] Generating: {player}...")
        card["writeup"] = writer(card)

    return cards


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Writer Test (Groq)")
    print("=" * 60)

    if not GROQ_API_KEY:
        print("\n  ❌ GROQ_API_KEY not set in .env")
        print("  Add: GROQ_API_KEY=your_key_here")
        print("\n  Testing fallback writer instead...")
    else:
        print(f"\n  ✅ Groq key loaded | Model: {GROQ_MODEL}")

    # Load latest slate from cache
    slate_path = CACHE_DIR / "agent_slate.json"
    if not slate_path.exists():
        print("\n  No slate cache — run slipiq_confidence_agent.py first")
    else:
        with open(slate_path) as f:
            slate = json.load(f)

        # Try best pick
        best = slate.get("best_pick")
        hold = slate.get("hold_list", [])
        skip = slate.get("skip_list", [])

        # Use first available card for test
        test_card = best or (hold[0] if hold else None) or (skip[0] if skip else None)

        if test_card:
            print(f"\n  Testing with: {test_card.get('player')}")
            print(f"  Grade: {test_card.get('grade')} | "
                  f"Confidence: {test_card.get('confidence')}%")

            print("\n  [pitcher write-up]")
            print("  " + "-" * 50)
            writeup = write_pitcher_pick(test_card)
            print(writeup)
            print("  " + "-" * 50)

        # Morning brief test
        post_list = slate.get("post_list", [])
        hold_list = slate.get("hold_list", [])
        all_cards = post_list + hold_list

        if all_cards:
            print("\n  [morning brief]")
            print("  " + "-" * 50)
            brief = write_morning_brief(slate, all_cards[:3])
            print(brief)
            print("  " + "-" * 50)

    print("\n✓ Writer test complete.")

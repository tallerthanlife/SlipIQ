# slipiq_discord_patch.py
# ═══════════════════════════════════════════════════════════════
# PATCH — Apply to slipiq_discord.py in Cursor
#
# CHANGE 1: build_best_pick_embed() — show real EV % and breakeven
#   Currently shows ev_confirmed as ✅ emoji only.
#   Now shows: "EV +4.7% | needs 53.5% to break even"
#   Also shows ev_source so operator knows if it's real or fallback.
#
# CHANGE 2: build_morning_brief_embed() — add EV % to pick summary lines
#   Currently shows: [A] Cole — OVER 7.5 | 78% ✅
#   Now shows:       [A] Cole — OVER 7.5 | 78% | EV +4.7%
#
# CHANGE 3: Add DISCORD_PRIZEPICKS_CHANNEL to imports from slipiq_env
# ═══════════════════════════════════════════════════════════════

# ─── CHANGE 1: Replace build_best_pick_embed() fields section ─
# Find the "fields" section in build_best_pick_embed() and replace
# the EV field with this expanded version:

CHANGE_1_EV_FIELD = '''
    # ── EV field (real math now) ───────────────────────────────
    ev_val    = card.get("ev")
    ev_conf   = card.get("ev_confirmed", False)
    ev_src    = card.get("ev_source", "none")
    breakeven = card.get("breakeven")   # e.g. "Needs 53.5% to break even"
    no_pin    = card.get("no_pinnacle", True)

    if ev_val is not None and ev_src == "ev_engine_pinnacle":
        ev_sign   = "+" if ev_val >= 0 else ""
        ev_str    = f"**{ev_sign}{ev_val*100:.1f}%** edge vs Pinnacle"
        if breakeven:
            ev_str += f"\\n{breakeven}"
        ev_emoji  = "✅" if ev_val >= 0.03 else ("⚠️" if ev_val >= 0 else "❌")
    elif ev_val is not None and ev_src == "parlayapi_only":
        ev_str   = f"{'+' if ev_val >= 0 else ''}{ev_val*100:.1f}% (unverified — no Pinnacle)"
        ev_emoji = "⚠️"
        if breakeven:
            ev_str += f"\\n{breakeven}"
    else:
        ev_str   = "Unconfirmed — Pinnacle line not posted yet"
        ev_emoji = "⏳"

    fields.append({
        "name":   f"{ev_emoji} Expected Value",
        "value":  ev_str,
        "inline": False,
    })
'''

# Replace the existing EV field in build_best_pick_embed().
# The existing field likely looks like:
#   {"name": "EV", "value": "✅ Confirmed" if ev_confirmed else "Unconfirmed", ...}
# Replace that entire dict with CHANGE_1_EV_FIELD inserted into the fields list.

# ─── CHANGE 2: Morning brief pick summary line ─────────────────
# In build_morning_brief_embed(), find where pick_lines are built:
#   pick_lines.append(f"`[{grade}]` {player} — {direction} {line} | {conf}%{ev_tag}{bk_snip}")
# Replace with:

CHANGE_2_PICK_LINE = '''
        ev_val  = card.get("ev")
        ev_src  = card.get("ev_source", "none")
        if ev_val is not None and ev_src == "ev_engine_pinnacle":
            ev_tag = f" | EV {'+' if ev_val >= 0 else ''}{ev_val*100:.1f}%"
        elif ev_conf:
            ev_tag = " ✅"
        else:
            ev_tag = ""

        bk_row  = card.get("books_row", "")
        bk_snip = f"\\n   {bk_row}" if bk_row else ""
        pick_lines.append(
            f"`[{grade}]` {player} — {direction} {line} | {conf}%{ev_tag}{bk_snip}"
        )
'''

# ─── CHANGE 3: Add DISCORD_PRIZEPICKS_CHANNEL import ──────────
# In the imports block at the top of slipiq_discord.py,
# add DISCORD_PRIZEPICKS_CHANNEL to the from slipiq_env import:

CHANGE_3_IMPORT_ADDITION = """
# Add to existing slipiq_env import:
from slipiq_env import (
    CHANNEL_TEAM_PARLAY,
    DISCORD_BOT_TOKEN,
    DISCORD_DAILY_PICKS_CHANNEL,
    DISCORD_LIVE_ALERTS_CHANNEL,
    DISCORD_SHARP_REVIEW_CHANNEL,
    DISCORD_PRIZEPICKS_CHANNEL,   # NEW — add this line
)
"""

# ─── CHANGE 4: Add PrizePicks entry poster function ───────────
# Add this function to slipiq_discord.py for the intraday scanner:

CHANGE_4_PP_POSTER = '''
def post_prizepicks_entry(entry: dict) -> bool:
    """
    Post a PrizePicks intraday entry to DISCORD_PRIZEPICKS_CHANNEL.
    Called by slipiq_propline_scanner after intraday_scanner() returns entries.
    """
    try:
        from slipiq_prizepicks import format_pp_entry_discord
        content = format_pp_entry_discord(entry)
        return post_message(DISCORD_PRIZEPICKS_CHANNEL, content=content[:2000])
    except Exception as e:
        print(f"  [discord] PrizePicks post error: {e}")
        return False


def post_lotto_slip(slip: dict) -> bool:
    """
    Post a pitcher lotto slip to CHANNEL_TEAM_PARLAY.
    Called by slipiq_independent_parlay after build_pitcher_lotto_slip().
    """
    try:
        from slipiq_independent_parlay import format_lotto_discord
        content = format_lotto_discord(slip)
        return post_message(CHANNEL_TEAM_PARLAY, content=content[:2000])
    except Exception as e:
        print(f"  [discord] Lotto slip post error: {e}")
        return False


def post_mlrl_parlay(slip: dict) -> bool:
    """
    Post an independent ML/RL parlay to CHANNEL_TEAM_PARLAY.
    Called by slipiq_independent_parlay after build_mlrl_parlay().
    """
    try:
        from slipiq_independent_parlay import format_mlrl_discord
        content = format_mlrl_discord(slip)
        return post_message(CHANNEL_TEAM_PARLAY, content=content[:2000])
    except Exception as e:
        print(f"  [discord] ML/RL parlay post error: {e}")
        return False
'''

CURSOR_INSTRUCTIONS = """
Apply these 4 changes to slipiq_discord.py:

CHANGE 1: In build_best_pick_embed(), find the EV-related field
in the fields list and replace it with the CHANGE_1_EV_FIELD block.
This shows real EV % and breakeven string on every pick card.

CHANGE 2: In build_morning_brief_embed(), replace the pick_lines.append()
call with CHANGE_2_PICK_LINE which shows EV % in the brief summary.

CHANGE 3: Add DISCORD_PRIZEPICKS_CHANNEL to the from slipiq_env import block.

CHANGE 4: Add the three new poster functions (post_prizepicks_entry,
post_lotto_slip, post_mlrl_parlay) at module level before the
validate_discord_env() function.

Do not change any other code.
"""

print("Discord patch spec loaded.")
print(CURSOR_INSTRUCTIONS)

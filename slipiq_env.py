# slipiq_env.py
# ═══════════════════════════════════════════════════════════════
# Single source of truth for all env config.
# Import from here — never scatter os.getenv() calls.
# Match .env.example exactly. Add new keys here first.
# ═══════════════════════════════════════════════════════════════

import os
from dotenv import load_dotenv

load_dotenv()


def _get(*names: str, default: str = "") -> str:
    """First non-empty env var among names (values stripped)."""
    for name in names:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return default


def _get_int(name: str, default: int = 0) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float = 0.0) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _normalize_supabase_url(url: str) -> str:
    """Accept base URL or .../rest/v1/ without requiring edits."""
    if not url:
        return ""
    u = url.rstrip("/")
    if u.endswith("/rest/v1"):
        u = u[: -len("/rest/v1")]
    return u


# ─── AI ───────────────────────────────────────────────────────
GROQ_API_KEY      = _get("GROQ_API_KEY")
GROQ_API_CHAT_KEY = _get("GROQ_API_CHAT_KEY") or GROQ_API_KEY
GROQ_CHAT_MODEL   = _get("GROQ_CHAT_MODEL")   or "llama-3.3-70b-versatile"
GROQ_VISION_MODEL = _get("GROQ_VISION_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct"
SLIPIQ_SKIP_AGENTIC = _get_bool("SLIPIQ_SKIP_AGENTIC", default=False)

# ─── Props API keys (role-tagged — see .env.example) ──────────
# PRIMARY — Parlay-API — 1,000 cr/month — morning foundation pull
PARLAY_API_KEY = _get("PARLAY_API_KEY")

# DYNAMIC POLLING — Prop-Line API — 1,000 cr/day — intraday + Pinnacle source
PROPLINE_API_KEY = _get("PROPLINE_API_KEY")

# TEST BENCH — Sharp API (sharpapi.io) — EV benchmarking only
SHARP_API_KEY = _get("SHARP_API_KEY")

# FAILSAFE CHAIN — The Odds API — Pinnacle backup when Prop-Line has no Pinnacle data
ODDS_API_KEY   = _get("ODDS_API_KEY")
ODDS_API_KEY_2 = _get("ODDS_API_2")
ODDS_API_KEY_3 = _get("ODDS_API_3")
ODDS_API_KEYS  = [k for k in (ODDS_API_KEY, ODDS_API_KEY_2, ODDS_API_KEY_3) if k]

# UNASSIGNED — identify before using
ODDS_PAPI_KEY = _get("ODDS_PAPI")

ODDS_MAX_EVENTS  = _get_int("ODDS_MAX_EVENTS", 15)
ODDS_CACHE_HOURS = _get_int("ODDS_CACHE_HOURS", 6)

# ─── Stats / weather ──────────────────────────────────────────
BDL_API_KEY         = _get("BDL_API_KEY")
API_FOOTBALL_KEY    = _get("API_FOOTBALL", "API_FOOTBALL_KEY")
TOMORROW_IO_API_KEY = _get("TOMORROW_IO_API_KEY", "TOMORROW_API_KEY")
OPENWEATHER_API_KEY = _get("OPENWEATHER_API_KEY")

# ─── Database ─────────────────────────────────────────────────
SUPABASE_URL = _normalize_supabase_url(_get("SUPABASE_URL"))
SUPABASE_KEY = _get("SUPABASE_KEY")

# ─── Discord ──────────────────────────────────────────────────
DISCORD_BOT_TOKEN = _get("DISCORD_BOT_TOKEN")

# MLB daily picks
DISCORD_DAILY_PICKS_CHANNEL = _get(
    "DISCORD_DAILY_PICKS_CHANNEL",
    "CHANNEL_MLB_PITCHER_PROPS",
    "CHANNEL_DAILY_BEST_PICK",
)

# Parlay / SGP channel
CHANNEL_TEAM_PARLAY = _get("CHANNEL_TEAM_PARLAY")

# NBA daily picks
CHANNEL_BASKETBALL_PROPS = _get("CHANNEL_BASKETBALL_PROPS")

# Live alerts (line moves, scanner alerts)
DISCORD_LIVE_ALERTS_CHANNEL = _get("DISCORD_LIVE_ALERTS_CHANNEL")

# Nightly sharp review
DISCORD_SHARP_REVIEW_CHANNEL = _get(
    "DISCORD_SHARP_REVIEW_CHANNEL",
    "CHANNEL_SHARP_REVIEW",
)

# Daily results post (falls back to sharp review channel if not set separately)
DISCORD_RESULTS_CHANNEL = _get(
    "DISCORD_RESULTS_CHANNEL",
    "CHANNEL_RESULTS",
) or DISCORD_SHARP_REVIEW_CHANNEL

# AI slip builder chat
CHANNEL_SLIPIQ_CHAT = _get("CHANNEL_SLIPIQ_CHAT")

# PrizePicks intraday entries — falls back to daily picks channel if not set
DISCORD_PRIZEPICKS_CHANNEL = _get(
    "DISCORD_PRIZEPICKS_CHANNEL",
    "DISCORD_DAILY_PICKS_CHANNEL",
    "CHANNEL_MLB_PITCHER_PROPS",
)

# ─── Bankroll + Kelly ─────────────────────────────────────────
# Current betting bankroll in dollars — used by Kelly sizing throughout
SLIPIQ_BANKROLL = _get_float("SLIPIQ_BANKROLL", 500.0)

# ─── NBA off-season flag ──────────────────────────────────────
# Set NBA_SEASON_ACTIVE=false in .env to disable NBA scheduler jobs
# until October. Prevents burning Groq credits on dead season.
NBA_SEASON_ACTIVE = _get_bool("NBA_SEASON_ACTIVE", default=False)

# ─── Pipeline tuning ──────────────────────────────────────────
SLIPIQ_TOP_PICKS        = _get_int("SLIPIQ_TOP_PICKS", 0)
SLIPIQ_PARLAY_MIN_CONF  = _get_int("SLIPIQ_PARLAY_MIN_CONF", 68)
SLIPIQ_PARLAY_MIN_BOOKS = _get_int("SLIPIQ_PARLAY_MIN_BOOKS", 2)
SLIPIQ_PARLAY_MAX_MENU  = _get_int("SLIPIQ_PARLAY_MAX_MENU", 12)
SLIPIQ_PARLAY_MAX_SLIP  = _get_int("SLIPIQ_PARLAY_MAX_SLIP", 6)

SLIP_MIN_EDGE          = _get_float("SLIP_MIN_EDGE", 0.02)
SLIP_MIN_MODEL_CONF    = _get_float("SLIP_MIN_MODEL_CONF", 60.0)
SLIP_MIN_DISPLAY_CONF  = _get_float("SLIP_MIN_DISPLAY_CONF", 58.0)
SLIP_MIN_TRACK_RECORD  = _get_float("SLIP_MIN_TRACK_RECORD", 50.0)

SLIP_CHAT_MIN_CONF        = _get_int("SLIP_CHAT_MIN_CONF", 65)
SLIP_CHAT_MIN_EV          = _get_float("SLIP_CHAT_MIN_EV", 0.02)
SLIP_CHAT_SESSION_TTL_MIN = _get_int("SLIP_CHAT_SESSION_TTL_MIN", 30)
SLIP_CHAT_USER_COOLDOWN_SEC = _get_int("SLIP_CHAT_USER_COOLDOWN_SEC", 10)


# ─── Status checker ───────────────────────────────────────────
def discord_channels_status() -> dict:
    """Quick check for orchestrator / --status."""
    return {
        "daily_picks":    bool(DISCORD_DAILY_PICKS_CHANNEL),
        "team_parlay":    bool(CHANNEL_TEAM_PARLAY),
        "live_alerts":    bool(DISCORD_LIVE_ALERTS_CHANNEL),
        "sharp_review":   bool(DISCORD_SHARP_REVIEW_CHANNEL),
        "basketball":     bool(CHANNEL_BASKETBALL_PROPS),
        "slipiq_chat":    bool(CHANNEL_SLIPIQ_CHAT),
        "prizepicks":     bool(DISCORD_PRIZEPICKS_CHANNEL),
    }


def api_keys_status() -> dict:
    """Check which API keys are configured."""
    return {
        "parlay_api":    bool(PARLAY_API_KEY),
        "propline":      bool(PROPLINE_API_KEY),
        "sharp_api":     bool(SHARP_API_KEY),
        "odds_api_1":    bool(ODDS_API_KEY),
        "odds_api_2":    bool(ODDS_API_KEY_2),
        "odds_api_3":    bool(ODDS_API_KEY_3),
        "groq":          bool(GROQ_API_KEY),
        "supabase":      bool(SUPABASE_URL and SUPABASE_KEY),
        "bankroll":      SLIPIQ_BANKROLL,
        "nba_active":    NBA_SEASON_ACTIVE,
    }


if __name__ == "__main__":
    print("Discord channels:", discord_channels_status())
    print("API keys:", api_keys_status())

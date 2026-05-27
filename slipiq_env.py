# slipiq_env.py
# Single source of truth — matches the project's .env layout.
# Import from here instead of scattering os.getenv() calls.

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


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _normalize_supabase_url(url: str) -> str:
    """Accept base URL or .../rest/v1/ from .env without requiring edits."""
    if not url:
        return ""
    u = url.rstrip("/")
    if u.endswith("/rest/v1"):
        u = u[: -len("/rest/v1")]
    return u


# ─── AI ───────────────────────────────────────────────────────
GROQ_API_KEY = _get("GROQ_API_KEY")
# Dedicated Groq key for slipiq_chat (falls back to GROQ_API_KEY if unset)
GROQ_API_CHAT_KEY = _get("GROQ_API_CHAT_KEY", "GROQ_API_KEY")
GROQ_CHAT_MODEL = _get("GROQ_CHAT_MODEL") or "llama-3.3-70b-versatile"
GROQ_VISION_MODEL = _get("GROQ_VISION_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct"
SLIPIQ_SKIP_AGENTIC = _get_bool("SLIPIQ_SKIP_AGENTIC", default=False)

# ─── Odds / lines (failsafe chain per bot architecture) ───────
PARLAY_API_KEY = _get("PARLAY_API_KEY")
ODDS_PAPI_KEY  = _get("ODDS_PAPI")
SHARP_API_KEY  = _get("SHARP_API_KEY")

ODDS_API_KEY  = _get("ODDS_API_KEY")
ODDS_API_KEY_2 = _get("ODDS_API_2")
ODDS_API_KEY_3 = _get("ODDS_API_3")
ODDS_API_KEYS = [k for k in (ODDS_API_KEY, ODDS_API_KEY_2, ODDS_API_KEY_3) if k]

ODDS_MAX_EVENTS = _get_int("ODDS_MAX_EVENTS", 15)
ODDS_CACHE_HOURS = _get_int("ODDS_CACHE_HOURS", 6)

# ─── Stats / context APIs ─────────────────────────────────────
BDL_API_KEY       = _get("BDL_API_KEY")
API_FOOTBALL_KEY  = _get("API_FOOTBALL", "API_FOOTBALL_KEY")
TOMORROW_IO_API_KEY = _get("TOMORROW_IO_API_KEY", "TOMORROW_API_KEY")
OPENWEATHER_API_KEY = _get("OPENWEATHER_API_KEY")

# ─── Database ─────────────────────────────────────────────────
SUPABASE_URL = _normalize_supabase_url(_get("SUPABASE_URL"))
SUPABASE_KEY = _get("SUPABASE_KEY")

# ─── Discord (names match .env) ───────────────────────────────
DISCORD_BOT_TOKEN = _get("DISCORD_BOT_TOKEN")

# MLB daily picks — .env uses DISCORD_DAILY_PICKS_CHANNEL
DISCORD_DAILY_PICKS_CHANNEL = _get(
    "DISCORD_DAILY_PICKS_CHANNEL",
    "CHANNEL_MLB_PITCHER_PROPS",
    "CHANNEL_DAILY_BEST_PICK",
)

CHANNEL_TEAM_PARLAY = _get("CHANNEL_TEAM_PARLAY")
CHANNEL_BASKETBALL_PROPS = _get("CHANNEL_BASKETBALL_PROPS")

DISCORD_LIVE_ALERTS_CHANNEL = _get("DISCORD_LIVE_ALERTS_CHANNEL")
DISCORD_SHARP_REVIEW_CHANNEL = _get(
    "DISCORD_SHARP_REVIEW_CHANNEL",
    "CHANNEL_SHARP_REVIEW",
)

CHANNEL_SLIPIQ_CHAT = _get("CHANNEL_SLIPIQ_CHAT")

# ─── Pipeline tuning (optional — safe defaults if omitted) ────
SLIPIQ_TOP_PICKS = _get_int("SLIPIQ_TOP_PICKS", 0)
SLIPIQ_PARLAY_MIN_CONF = _get_int("SLIPIQ_PARLAY_MIN_CONF", 68)
SLIPIQ_PARLAY_MIN_BOOKS = _get_int("SLIPIQ_PARLAY_MIN_BOOKS", 2)
SLIPIQ_PARLAY_MAX_MENU = _get_int("SLIPIQ_PARLAY_MAX_MENU", 12)
SLIPIQ_PARLAY_MAX_SLIP = _get_int("SLIPIQ_PARLAY_MAX_SLIP", 6)

SLIP_MIN_EDGE = float(_get("SLIP_MIN_EDGE") or "0.75")
SLIP_MIN_MODEL_CONF = float(_get("SLIP_MIN_MODEL_CONF") or "60")
SLIP_MIN_DISPLAY_CONF = float(_get("SLIP_MIN_DISPLAY_CONF") or "58")
SLIP_MIN_TRACK_RECORD = float(_get("SLIP_MIN_TRACK_RECORD") or "50")

SLIP_CHAT_MIN_CONF = _get_int("SLIP_CHAT_MIN_CONF", 65)
SLIP_CHAT_MIN_EV = float(_get("SLIP_CHAT_MIN_EV") or "0.02")
SLIP_CHAT_SESSION_TTL_MIN = _get_int("SLIP_CHAT_SESSION_TTL_MIN", 30)
SLIP_CHAT_USER_COOLDOWN_SEC = _get_int("SLIP_CHAT_USER_COOLDOWN_SEC", 10)


def discord_channels_status() -> dict:
    """Quick check for orchestrator / --status."""
    return {
        "daily_picks":    bool(DISCORD_DAILY_PICKS_CHANNEL),
        "team_parlay":    bool(CHANNEL_TEAM_PARLAY),
        "live_alerts":    bool(DISCORD_LIVE_ALERTS_CHANNEL),
        "sharp_review":   bool(DISCORD_SHARP_REVIEW_CHANNEL),
        "basketball":     bool(CHANNEL_BASKETBALL_PROPS),
        "slipiq_chat":    bool(CHANNEL_SLIPIQ_CHAT),
    }

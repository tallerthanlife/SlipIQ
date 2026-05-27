# slipiq_chat.py
# Long-running Discord bot for #slipiq-chat — Groq-powered slip builder

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import discord

from slipiq_book_slip_builder import build_chat_embeds, build_full_response
from slipiq_chat_groq import parse_screenshot, parse_slip_intent, refine_constraints, summarize_slips
from slipiq_chat_pool import load_pool
from slipiq_env import (
    CHANNEL_SLIPIQ_CHAT,
    DISCORD_BOT_TOKEN,
    SLIP_CHAT_SESSION_TTL_MIN,
    SLIP_CHAT_USER_COOLDOWN_SEC,
)

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
SESSION_PATH = CACHE_DIR / "chat_sessions.json"

TRIGGER_PREFIX = "!slip"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


class ChatSessionStore:
    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._cooldowns: dict[str, float] = {}
        self._load()

    def _load(self):
        if SESSION_PATH.exists():
            try:
                with open(SESSION_PATH, encoding="utf-8") as f:
                    self._sessions = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._sessions = {}

    def _save(self):
        try:
            with open(SESSION_PATH, "w", encoding="utf-8") as f:
                json.dump(self._sessions, f, indent=2)
        except OSError as e:
            print(f"  [chat] session save failed: {e}")

    def get(self, user_id: str) -> dict:
        sess = self._sessions.get(user_id)
        if not sess:
            return self._new_session(user_id)
        updated = sess.get("updated")
        if updated:
            try:
                ts = datetime.fromisoformat(updated)
                if datetime.now() - ts > timedelta(minutes=SLIP_CHAT_SESSION_TTL_MIN):
                    return self._new_session(user_id)
            except ValueError:
                pass
        return sess

    def _new_session(self, user_id: str) -> dict:
        sess = {
            "messages": [],
            "constraints": {
                "sport": "both",
                "max_legs": 6,
                "prefer_correlated": False,
                "ev_only": True,
                "drop_leg_indices": [],
                "tighter": False,
            },
            "updated": datetime.now().isoformat(),
        }
        self._sessions[user_id] = sess
        return sess

    def update(self, user_id: str, messages: list, constraints: dict):
        self._sessions[user_id] = {
            "messages": messages[-6:],
            "constraints": constraints,
            "updated": datetime.now().isoformat(),
        }
        self._save()

    def reset(self, user_id: str):
        self._sessions.pop(user_id, None)
        self._save()

    def on_cooldown(self, user_id: str) -> float:
        last = self._cooldowns.get(user_id, 0)
        elapsed = time.time() - last
        if elapsed < SLIP_CHAT_USER_COOLDOWN_SEC:
            return SLIP_CHAT_USER_COOLDOWN_SEC - elapsed
        return 0

    def mark_request(self, user_id: str):
        self._cooldowns[user_id] = time.time()


def _parse_command(text: str) -> tuple[str, str]:
    """Return (subcommand, remainder)."""
    text = (text or "").strip()
    lower = text.lower()
    if not lower.startswith(TRIGGER_PREFIX):
        return "", text
    rest = text[len(TRIGGER_PREFIX):].strip()
    if not rest:
        return "build", ""
    parts = rest.split(maxsplit=1)
    return parts[0].lower(), parts[1] if len(parts) > 1 else ""


def _fetch_game_lines(sport: str) -> list:
    try:
        from slipiq_confidence_agent import SPORT_MLB
        from slipiq_parlayapi import fetch_odds_raw

        if sport in ("mlb", "both"):
            return fetch_odds_raw(SPORT_MLB) or []
    except Exception as e:
        print(f"  [chat] game lines fetch failed: {e}")
    return []


async def _download_attachment(attachment: discord.Attachment) -> tuple[bytes, str] | None:
    ext = Path(attachment.filename or "").suffix.lower()
    if ext not in IMAGE_EXT:
        return None
    mime = attachment.content_type or "image/png"
    if "jpeg" in mime or ext in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif "webp" in mime:
        mime = "image/webp"
    else:
        mime = "image/png"
    try:
        data = await attachment.read()
        return data, mime
    except Exception as e:
        print(f"  [chat] attachment download failed: {e}")
        return None


def _run_builder(
    sport: str,
    constraints: dict,
    parsed_legs: list | None,
    user_context: str,
) -> tuple[str, list[dict]]:
    pool, meta = load_pool(sport, refresh_if_stale=True)
    if meta.get("stale") and not pool:
        return (
            "No slate cached yet — run the morning pipeline first (`slipiq_orchestrator.py`).",
            [],
        )

    game_lines = _fetch_game_lines(sport)
    response = build_full_response(
        pool,
        parsed_legs=parsed_legs,
        constraints=constraints,
        game_lines=game_lines,
    )

    summary = summarize_slips(response, user_context=user_context)
    embeds = build_chat_embeds(response)

    if not embeds and response.get("hold_alternatives"):
        summary += "\n\nNo legs pass +EV POST gates today. Check `#daily-picks` for the morning brief."

    return summary, embeds


def _embeds_from_payloads(payloads: list[dict]) -> list[discord.Embed]:
    embeds = []
    for payload in payloads:
        if not payload:
            continue
        data = dict(payload)
        data.pop("timestamp", None)
        try:
            embeds.append(discord.Embed.from_dict(data))
        except Exception as e:
            print(f"  [chat] embed build failed: {e}")
    return embeds


def create_bot() -> discord.Client:
    store = ChatSessionStore()

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True

    class SlipChatBot(discord.Client):
        async def on_ready(self):
            print(f"  [chat] SlipIQ chat bot online as {self.user}")
            print(f"  [chat] Listening in channel ID: {CHANNEL_SLIPIQ_CHAT or '(not set)'}")

        async def on_message(self, message: discord.Message):
            if message.author.bot:
                return
            if not CHANNEL_SLIPIQ_CHAT or str(message.channel.id) != str(CHANNEL_SLIPIQ_CHAT):
                return

            content = message.content or ""
            has_image = any(
                (a.filename or "").lower().endswith(tuple(IMAGE_EXT))
                for a in message.attachments
            )
            mentioned = self.user and self.user in message.mentions
            is_command = content.lower().startswith(TRIGGER_PREFIX)

            if not (is_command or has_image or mentioned):
                return

            user_id = str(message.author.id)
            wait = store.on_cooldown(user_id)
            if wait > 0:
                await message.reply(f"Slow down — try again in {wait:.0f}s.", mention_author=False)
                return

            store.mark_request(user_id)
            sess = store.get(user_id)
            constraints = dict(sess.get("constraints") or {})
            messages = list(sess.get("messages") or [])

            subcmd, remainder = _parse_command(content)
            parsed_legs = None
            user_context = content[:200]

            if subcmd == "reset":
                store.reset(user_id)
                await message.reply("Session reset. Send `!slip` or upload a screenshot to start fresh.")
                return

            if subcmd == "help":
                await message.reply(
                    "**SlipIQ Chat Builder**\n"
                    "`!slip` — build mixed + correlated slips (MLB + NBA)\n"
                    "`!slip mlb` / `!slip nba` — sport filter\n"
                    "`!slip reset` — clear conversation\n"
                    "Upload a **screenshot** of your slip or lines board\n"
                    "Follow up: *\"make it 3 legs\"*, *\"more correlated\"*, *\"drop leg 2\"*",
                    mention_author=False,
                )
                return

            async with message.channel.typing():
                if subcmd in ("mlb", "nba"):
                    constraints["sport"] = subcmd
                    sport = subcmd
                elif subcmd == "mixed":
                    constraints["prefer_correlated"] = False
                    sport = constraints.get("sport", "both")
                else:
                    sport = constraints.get("sport", "both")

                if has_image:
                    for att in message.attachments:
                        downloaded = await _download_attachment(att)
                        if not downloaded:
                            continue
                        data, mime = downloaded
                        vision = parse_screenshot(data, mime)
                        parsed_legs = vision.get("legs") or []
                        if vision.get("sport") in ("mlb", "nba"):
                            constraints["sport"] = vision["sport"]
                            sport = vision["sport"]
                        user_context = f"screenshot upload, source={vision.get('source')}"
                        break

                elif is_command and remainder:
                    intent = parse_slip_intent(remainder)
                    if intent.get("sport") in ("mlb", "nba", "both"):
                        constraints["sport"] = intent["sport"]
                        sport = intent["sport"]
                    if intent.get("max_legs"):
                        constraints["max_legs"] = intent["max_legs"]
                    if intent.get("prefer_correlated") is not None:
                        constraints["prefer_correlated"] = intent["prefer_correlated"]
                    if intent.get("ev_only") is not None:
                        constraints["ev_only"] = intent["ev_only"]

                elif mentioned or (not is_command and content.strip()):
                    intent = parse_slip_intent(content)
                    if intent.get("sport") in ("mlb", "nba", "both"):
                        constraints["sport"] = intent["sport"]
                        sport = intent["sport"]
                    if messages:
                        constraints = refine_constraints(messages, content, constraints)
                    else:
                        if intent.get("max_legs"):
                            constraints["max_legs"] = intent["max_legs"]
                        if intent.get("prefer_correlated"):
                            constraints["prefer_correlated"] = True

                elif not is_command and not has_image:
                    return

                if sport == "unknown":
                    sport = "both"

                try:
                    summary, embeds = await asyncio.to_thread(
                        _run_builder,
                        sport,
                        constraints,
                        parsed_legs,
                        user_context,
                    )
                except Exception as e:
                    print(f"  [chat] builder error: {e}")
                    await message.reply(f"Slip build failed: {e}", mention_author=False)
                    return

                messages.append({"role": "user", "content": user_context})
                messages.append({"role": "assistant", "content": summary[:500]})
                store.update(user_id, messages, constraints)

                if not embeds:
                    await message.reply(summary[:1900], mention_author=False)
                    return

                await message.reply(
                    summary[:500],
                    embeds=_embeds_from_payloads(embeds),
                    mention_author=False,
                )

    return SlipChatBot(intents=intents)


def run_chat_bot():
    if not DISCORD_BOT_TOKEN:
        print("  [chat] DISCORD_BOT_TOKEN not set")
        sys.exit(1)
    if not CHANNEL_SLIPIQ_CHAT:
        print("  [chat] CHANNEL_SLIPIQ_CHAT not set")
        sys.exit(1)

    bot = create_bot()
    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    run_chat_bot()

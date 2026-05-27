# slipiq_chat_groq.py
# Groq layer for slipiq_chat — multi-turn chat, intent parse, vision OCR, summaries

import base64
import json
import re
from pathlib import Path

import requests

from slipiq_env import (
    GROQ_API_KEY,
    GROQ_CHAT_MODEL,
    GROQ_VISION_MODEL,
)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

CHAT_SYSTEM_PROMPT = """You are SlipIQ's interactive slip builder assistant in Discord.

RULES:
- Data-driven and direct. No hype or "must-bet" language.
- Never invent confidence, EV, grades, or projections — those come from the model pipeline.
- Help users build, refine, and understand parlay slips for MLB and NBA props.
- Keep replies short (2-4 sentences) unless summarizing a built slip.
- Books shown to users: DraftKings, Fanatics, PrizePicks only."""

INTENT_PROMPT = """Extract slip-builder intent from the user message.
Return ONLY valid JSON with these keys:
{
  "sport": "mlb" | "nba" | "both" | "unknown",
  "action": "build" | "review" | "refine" | "help" | "reset",
  "max_legs": integer 2-8 or null,
  "prefer_correlated": boolean,
  "ev_only": boolean,
  "notes": "brief paraphrase of user request"
}"""

VISION_PROMPT = """Extract sports betting legs from this screenshot.
Return ONLY valid JSON:
{
  "sport": "mlb" | "nba" | "unknown",
  "source": "user_slip" | "lines_board" | "stats" | "unknown",
  "legs": [
    {
      "player": "name or team for game lines",
      "prop": "e.g. strikeouts, points, F5 ML, total",
      "line": number or null,
      "direction": "over" | "under" | null,
      "book": "draftkings" | "fanatics" | "prizepicks" | null
    }
  ]
}
Use null for missing fields. Do not guess lines you cannot read."""


def _groq_headers() -> dict:
    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return None


def call_groq_chat(
    messages: list[dict],
    max_tokens: int = 512,
    json_mode: bool = False,
    model: str | None = None,
) -> str:
    """Multi-turn Groq chat. messages: [{role, content}, ...]."""
    if not GROQ_API_KEY:
        return ""

    payload = {
        "model": model or GROQ_CHAT_MODEL,
        "messages": [{"role": "system", "content": CHAT_SYSTEM_PROMPT}, *messages],
        "max_tokens": max_tokens,
        "temperature": 0.5,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        r = requests.post(GROQ_URL, headers=_groq_headers(), json=payload, timeout=20)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  [chat_groq] error: {e}")
        return ""


def parse_slip_intent(text: str) -> dict:
    """Natural language → structured intent."""
    default = {
        "sport": "both",
        "action": "build",
        "max_legs": None,
        "prefer_correlated": False,
        "ev_only": True,
        "notes": text[:120],
    }
    if not GROQ_API_KEY or not text.strip():
        return default

    raw = call_groq_chat(
        [{"role": "user", "content": f"{INTENT_PROMPT}\n\nUser: {text}"}],
        max_tokens=200,
        json_mode=True,
    )
    parsed = _extract_json(raw)
    if not parsed:
        return default
    return {**default, **{k: v for k, v in parsed.items() if v is not None}}


def parse_screenshot(image_bytes: bytes, mime_type: str = "image/png") -> dict:
    """Vision OCR → ParsedLegs JSON via Groq Llama 4 Scout."""
    empty = {"sport": "unknown", "source": "unknown", "legs": []}
    if not GROQ_API_KEY or not image_bytes:
        return empty

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"

    payload = {
        "model": GROQ_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    try:
        r = requests.post(GROQ_URL, headers=_groq_headers(), json=payload, timeout=30)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        parsed = _extract_json(raw)
        return parsed if parsed else empty
    except Exception as e:
        print(f"  [chat_groq] vision error: {e}")
        return empty


def summarize_slips(response: dict, user_context: str = "") -> str:
    """Short plain-text lead-in for Discord reply."""
    if not GROQ_API_KEY:
        mixed = response.get("mixed_slip") or {}
        grade = mixed.get("slip_grade", "?")
        score = mixed.get("slip_score", 0)
        return f"Slip builder result — Grade {grade} ({score}/100)."

    mixed = response.get("mixed_slip") or {}
    correlated = response.get("correlated_slip") or {}
    user_slip = response.get("user_slip") or {}
    pool_note = response.get("pool_note", "")

    prompt = f"""Write 2-3 sharp sentences summarizing these slips for Discord.
User context: {user_context or 'build slip'}

Mixed slip: grade {mixed.get('slip_grade')} score {mixed.get('slip_score')} \
{len(mixed.get('legs') or [])} legs avg conf {mixed.get('avg_conf')}%
Correlated: {correlated.get('title', 'none')} grade {correlated.get('slip_grade', 'n/a')}
User slip review: {user_slip.get('slip_grade', 'n/a')} \
{user_slip.get('passed_legs', 0)}/{user_slip.get('total_legs', 0)} legs pass gates
Pool note: {pool_note}

Lead with the grade. Flag weak links. No invented stats."""

    text = call_groq_chat([{"role": "user", "content": prompt}], max_tokens=180)
    return text or f"Mixed slip grades **{mixed.get('slip_grade', '?')}** ({mixed.get('slip_score', 0)}/100)."


def refine_constraints(
    session_messages: list[dict],
    new_message: str,
    current_constraints: dict,
) -> dict:
    """Use chat history to adjust builder constraints on follow-up."""
    default = dict(current_constraints)
    if not GROQ_API_KEY:
        return default

    prompt = f"""Given the conversation and new message, return JSON adjusting slip constraints:
{{
  "sport": "mlb"|"nba"|"both",
  "max_legs": int or null,
  "prefer_correlated": bool,
  "ev_only": bool,
  "drop_leg_indices": [int] or [],
  "tighter": bool
}}
Current constraints: {json.dumps(current_constraints)}
New message: {new_message}"""

    msgs = list(session_messages[-4:]) + [{"role": "user", "content": prompt}]
    raw = call_groq_chat(msgs, max_tokens=200, json_mode=True)
    parsed = _extract_json(raw)
    if not parsed:
        return default
    merged = {**default, **parsed}
    return merged


if __name__ == "__main__":
    import sys

    if "--intent" in sys.argv:
        idx = sys.argv.index("--intent")
        text = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "build me a 4 leg nba slip"
        print(json.dumps(parse_slip_intent(text), indent=2))
    elif "--image" in sys.argv:
        idx = sys.argv.index("--image")
        path = Path(sys.argv[idx + 1])
        if not path.exists():
            print(f"File not found: {path}")
        else:
            data = path.read_bytes()
            mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
            print(json.dumps(parse_screenshot(data, mime), indent=2))
    else:
        print("Usage: python slipiq_chat_groq.py --intent \"build nba slip\"")
        print("       python slipiq_chat_groq.py --image screenshot.png")

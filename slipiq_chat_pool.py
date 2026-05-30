# DISABLED
# This module is disabled. Import it safely; all functions are no-ops.
import sys as _sys
if False:
    pass

# slipiq_chat_pool.py
# Unified MLB/NBA pick pool for slipiq_chat

import json
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

try:
    from fuzzywuzzy import fuzz
except ImportError:
    class _FuzzFallback:
        @staticmethod
        def token_sort_ratio(a: str, b: str) -> int:
            return int(SequenceMatcher(None, a, b).ratio() * 100)

        @staticmethod
        def partial_ratio(a: str, b: str) -> int:
            return int(SequenceMatcher(None, a, b).ratio() * 100)

    fuzz = _FuzzFallback()

from slipiq_env import SLIP_CHAT_MIN_CONF, SLIP_CHAT_MIN_EV
from slipiq_grading import calc_grade
from slipiq_results import get_track_record_snapshot
from slipiq_slip_review import review_pick

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

ELITE_GRADES = {"A+", "A", "B+", "B"}
SLATE_MAX_AGE_HOURS = 6


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [chat_pool] cache read failed {path.name}: {e}")
        return None


def _slate_stale(slate: dict) -> bool:
    run_time = slate.get("run_time")
    if not run_time:
        return True
    try:
        ts = datetime.fromisoformat(str(run_time).replace("Z", "+00:00"))
        if ts.tzinfo:
            ts = ts.replace(tzinfo=None)
        age = datetime.now() - ts
        return age > timedelta(hours=SLATE_MAX_AGE_HOURS)
    except (ValueError, TypeError):
        return True


def _refresh_mlb_slate() -> dict | None:
    try:
        from slipiq_confidence_agent import run_confidence_agent

        return run_confidence_agent()
    except Exception as e:
        print(f"  [chat_pool] MLB refresh failed: {e}")
        return None


def _refresh_nba_slate() -> dict | None:
    try:
        from slipiq_nba_confidence_agent import run_nba_confidence_agent

        return run_nba_confidence_agent()
    except Exception as e:
        print(f"  [chat_pool] NBA refresh failed: {e}")
        return None


def load_mlb_pool(refresh_if_stale: bool = True) -> tuple[list[dict], dict]:
    """Return (post_list, slate_meta)."""
    slate = _load_json(CACHE_DIR / "agent_slate.json")
    if refresh_if_stale and (not slate or _slate_stale(slate)):
        slate = _refresh_mlb_slate() or slate
    if not slate:
        return [], {"sport": "mlb", "post_count": 0, "stale": True}
    pool = list(slate.get("post_list") or [])
    for card in pool:
        card.setdefault("sport", "mlb")
    return pool, slate


def load_nba_pool(refresh_if_stale: bool = True) -> tuple[list[dict], dict]:
    slate = _load_json(CACHE_DIR / "nba_agent_slate.json")
    if refresh_if_stale and (not slate or _slate_stale(slate)):
        slate = _refresh_nba_slate() or slate
    if not slate:
        return [], {"sport": "nba", "post_count": 0, "stale": True}
    pool = list(slate.get("post_list") or [])
    for card in pool:
        card.setdefault("sport", "nba")
    return pool, slate


def load_pool(sport: str = "both", refresh_if_stale: bool = True) -> tuple[list[dict], dict]:
    sport = (sport or "both").lower()
    if sport == "mlb":
        pool, meta = load_mlb_pool(refresh_if_stale)
        return pool, {"sport": "mlb", **meta}
    if sport == "nba":
        pool, meta = load_nba_pool(refresh_if_stale)
        return pool, {"sport": "nba", **meta}

    mlb_pool, mlb_meta = load_mlb_pool(refresh_if_stale)
    nba_pool, nba_meta = load_nba_pool(refresh_if_stale)
    combined = mlb_pool + nba_pool
    return combined, {
        "sport": "both",
        "mlb_post_count": len(mlb_pool),
        "nba_post_count": len(nba_pool),
        "post_count": len(combined),
        "stale": _slate_stale(mlb_meta) and _slate_stale(nba_meta),
    }


def card_to_review_pick(card: dict) -> dict:
    """Adapt modern pick card → slipiq_slip_review schema."""
    player = card.get("player") or card.get("pitcher") or "Unknown"
    direction = (card.get("direction") or "over").upper()
    line = card.get("line", 0)
    grade = card.get("grade") or calc_grade(card.get("confidence", 0))
    conf = card.get("confidence", 0)
    trend = (card.get("trend") or "NEUTRAL").upper()
    if trend in ("UP", "HOT"):
        trend = "HOT"
    elif trend in ("DOWN", "COLD"):
        trend = "COLD"
    elif trend == "FLAT":
        trend = "NEUTRAL"

    market = card.get("market") or card.get("prop_type") or "prop"
    return {
        "pitcher": player,
        "player": player,
        "line": line,
        "projection": card.get("projection", line),
        "confidence": conf,
        "model_confidence": card.get("model_confidence", conf),
        "display_confidence": card.get("display_confidence", conf),
        "grade": grade,
        "trend": trend,
        "recommendation": (
            f"{direction} {line} | Grade: {grade} | Confidence: {conf}% | {market}"
        ),
        "hit_rate_label": card.get("hit_rate_label", "Track record building"),
        "_source_card": card,
    }


def enrich_hit_rates(cards: list[dict]) -> list[dict]:
    snapshot = get_track_record_snapshot()
    by_grade = snapshot.get("by_grade") or {}
    out = []
    for card in cards:
        c = dict(card)
        grade = c.get("grade") or calc_grade(c.get("confidence", 0))
        g = by_grade.get(grade[0] if grade else "C") or by_grade.get(grade)
        if g and g.get("picks", 0) >= 5:
            c["hit_rate_label"] = (
                f"{g['hit_rate']}% ({g['wins']}/{g['picks']}) grade {grade}"
            )
        else:
            c.setdefault("hit_rate_label", "Track record building")
        out.append(c)
    return out


def _ev_ok(card: dict) -> bool:
    if card.get("ev_confirmed"):
        return True
    ev = card.get("ev_value")
    if ev is not None and float(ev) >= SLIP_CHAT_MIN_EV:
        return True
    return False


def filter_elite(cards: list[dict], ev_only: bool = True) -> list[dict]:
    out = []
    for card in cards:
        conf = float(card.get("confidence") or 0)
        grade = card.get("grade") or calc_grade(conf)
        gate = (card.get("gate") or "POST").upper()
        if conf < SLIP_CHAT_MIN_CONF:
            continue
        if grade not in ELITE_GRADES:
            continue
        if gate not in ("POST", ""):
            continue
        if ev_only and not _ev_ok(card):
            continue
        review = review_pick(card_to_review_pick(card))
        if review.get("score", 0) < 83:
            continue
        enriched = dict(card)
        enriched["slip_review"] = review
        enriched["review_score"] = review["score"]
        out.append(enriched)
    return out


def _normalize_prop(prop: str) -> str:
    p = (prop or "").lower().strip()
    aliases = {
        "k": "strikeouts",
        "ks": "strikeouts",
        "strikeout": "strikeouts",
        "strikeouts": "strikeouts",
        "pts": "points",
        "points": "points",
        "reb": "rebounds",
        "rebounds": "rebounds",
        "ast": "assists",
        "assists": "assists",
        "pra": "points_rebounds_assists",
        "threes": "threes",
        "3pm": "threes",
    }
    for key, val in aliases.items():
        if key in p:
            return val
    return p


def _prop_matches(card: dict, parsed_prop: str) -> bool:
    if not parsed_prop:
        return True
    norm = _normalize_prop(parsed_prop)
    market = (card.get("market") or card.get("prop_type") or "").lower()
    label = (card.get("prop_label") or "").lower()
    if norm in market or norm in label:
        return True
    if norm == "strikeouts" and "strikeout" in market:
        return True
    if norm == "points" and "point" in market:
        return True
    return fuzz.partial_ratio(norm, market) >= 70


def match_legs(parsed_legs: list[dict], pool: list[dict]) -> tuple[list[dict], list[dict]]:
    """Match OCR/intent legs to pool cards. Returns (matched_cards, unmatched_parsed)."""
    matched = []
    unmatched = []
    used_keys = set()

    for leg in parsed_legs or []:
        player = (leg.get("player") or "").strip()
        if not player:
            unmatched.append(leg)
            continue

        best = None
        best_score = 0
        for card in pool:
            key = (card.get("player"), card.get("market"), card.get("line"))
            if key in used_keys:
                continue
            name_score = fuzz.token_sort_ratio(player.lower(), (card.get("player") or "").lower())
            if name_score < 72:
                continue
            if not _prop_matches(card, leg.get("prop", "")):
                continue
            leg_line = leg.get("line")
            card_line = card.get("line")
            if leg_line is not None and card_line is not None:
                try:
                    if abs(float(leg_line) - float(card_line)) > 1.5:
                        continue
                except (TypeError, ValueError):
                    pass
            if name_score > best_score:
                best_score = name_score
                best = card

        if best:
            used_keys.add((best.get("player"), best.get("market"), best.get("line")))
            matched.append(dict(best))
        else:
            unmatched.append(leg)

    return matched, unmatched


if __name__ == "__main__":
    pool, meta = load_pool("both", refresh_if_stale=False)
    elite = filter_elite(pool)
    print(f"Pool: {len(pool)} POST | Elite: {len(elite)} | meta: {meta.get('sport')}")

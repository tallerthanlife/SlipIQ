# slipiq_nba_curate.py
# NBA morning curation — mirrors slipiq_curate.py

import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from slipiq_nba_confidence_agent import run_nba_confidence_agent
from slipiq_nba_discord import post_nba_morning_brief, post_nba_waiting_message

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

GRADE_BONUS = {
    "A+": 22,
    "A":   20,
    "B+":  14,
    "B":   10,
    "C":    0,
}

MAX_DAILY_POSTS = 3


def curation_score(card: dict) -> float:
    confidence = card.get("confidence", 0)
    grade = card.get("grade", "C")
    diff = abs(card.get("diff", 0))
    ev_conf = card.get("ev_confirmed", False)
    book_count = card.get("book_count", 0)
    trend = card.get("trend", "flat")
    direction = card.get("direction", "")

    grade_pts = GRADE_BONUS.get(grade, 0)
    edge_pts = diff * 2
    ev_pts = 15 if ev_conf else 0
    book_pts = min(book_count * 2, 12)
    trend_aligned = (trend == "up" and direction == "over") or (trend == "down" and direction == "under")
    trend_pts = 5 if trend_aligned else 0

    return confidence + grade_pts + edge_pts + ev_pts + book_pts + trend_pts


def select_best_pick(post_list: list[dict]) -> dict | None:
    if not post_list:
        return None
    return max(post_list, key=curation_score)


def select_top_picks(post_list: list[dict], max_picks: int = MAX_DAILY_POSTS) -> list[dict]:
    if not post_list:
        return []
    scored = sorted(post_list, key=curation_score, reverse=True)
    selected = []
    seen = set()
    for card in scored:
        key = (card.get("player"), card.get("prop_type"))
        if key in seen:
            continue
        selected.append(card)
        seen.add(key)
        if len(selected) >= max_picks:
            break
    return selected


def log_slate(slate: dict, top_picks: list[dict]):
    log = {
        "sport":      "nba",
        "date":       datetime.now().strftime("%Y-%m-%d"),
        "run_time":   datetime.now().isoformat(),
        "top_picks":  top_picks,
        "post_count": slate.get("post_count", 0),
        "hold_count": slate.get("hold_count", 0),
        "skip_count": slate.get("skip_count", 0),
        "total":      slate.get("total", 0),
    }
    log_path = CACHE_DIR / f"nba_slate_{datetime.now().strftime('%Y%m%d')}.json"
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2, default=str)
    latest_path = CACHE_DIR / "nba_latest_picks.json"
    with open(latest_path, "w") as f:
        json.dump(log, f, indent=2, default=str)
    print(f"  [nba_curate] Slate logged -> {log_path.name}")
    return log_path


def is_market_open(slate: dict) -> bool:
    if slate.get("post_list") or slate.get("hold_list"):
        return True
    for card in slate.get("all_cards") or []:
        if card.get("line") and card.get("confidence", 0) >= 50:
            return True
    return False


def select_lean_picks(slate: dict, max_picks: int = MAX_DAILY_POSTS) -> list[dict]:
    candidates = []
    for card in slate.get("hold_list", []) + slate.get("skip_list", []):
        if card.get("grade") == "C" and card.get("confidence", 0) < 58:
            continue
        if card.get("confidence", 0) < 58:
            continue
        if not card.get("line"):
            continue
        candidates.append(card)
    candidates.sort(key=curation_score, reverse=True)
    out = []
    seen = set()
    for card in candidates:
        key = (card.get("player"), card.get("prop_type"))
        if key in seen:
            continue
        seen.add(key)
        card = dict(card)
        card["gate"] = "LEAN"
        out.append(card)
        if len(out) >= max_picks:
            break
    return out


def run_nba_curation(post_to_discord: bool = True) -> dict:
    print("\n" + "=" * 60)
    print("SlipIQ NBA Morning Curation")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    slate = run_nba_confidence_agent()
    market_open = is_market_open(slate)
    post_list = slate.get("post_list", [])

    print(f"\n  Market open : {'YES' if market_open else 'NO'}")
    print(f"  POST picks  : {len(post_list)}")

    best_pick = select_best_pick(post_list)
    top_picks = select_top_picks(post_list)
    lean_picks = []

    if not top_picks:
        lean_picks = select_lean_picks(slate)
        if lean_picks:
            print(f"\n  Thin market — posting {len(lean_picks)} LEAN picks")
            best_pick = lean_picks[0]
            top_picks = lean_picks

    if best_pick:
        print(f"\n  Best pick   : {best_pick.get('prop_label')} "
              f"[{best_pick.get('grade')}] {best_pick.get('confidence')}%")
    else:
        print("\n  No best pick — market not open yet")

    log_path = log_slate(slate, top_picks)

    if post_to_discord:
        if top_picks:
            slate["best_pick"] = best_pick
            slate["post_list"] = top_picks
            slate["post_count"] = len(top_picks)
            slate["lean_mode"] = bool(lean_picks)
            try:
                post_nba_morning_brief(slate)
            except Exception as e:
                print(f"\n  [nba_discord] Error: {e}")
        elif market_open:
            print("\n  [nba_discord] Market thin — no picks cleared POST gate")
            post_nba_waiting_message()
        else:
            print("\n  [nba_discord] Posting waiting message...")
            post_nba_waiting_message()
    else:
        print("\n  [nba_discord] Skipped (post_to_discord=False)")

    return {
        "best_pick":   best_pick,
        "top_picks":   top_picks,
        "market_open": market_open,
        "post_count":  len(top_picks),
        "slate":       slate,
        "log_path":    str(log_path),
    }


if __name__ == "__main__":
    no_discord = "--no-discord" in sys.argv
    result = run_nba_curation(post_to_discord=not no_discord)
    print(f"\nPosted: {result.get('post_count')} picks")

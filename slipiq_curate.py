# slipiq_curate.py
# Morning Curation Layer — ties the full pipeline together
# Runs at 6:30am AZ via scheduler (slipiq_orchestrator.py)
# Re-runs at 9am when full slate is posted
#
# PIPELINE ORDER:
#   1. Run confidence agent → get gated slate
#   2. Select best pick of the day
#   3. Select top picks for full post
#   4. Post to Discord
#   5. Log slate to DB / cache for Sharp Review
#
# CURATION LOGIC:
#   Best pick = highest curation score
#   Curation score = confidence + grade bonus + edge bonus
#                  + ev bonus + book count bonus
#   Tiebreaker = most books posting (market consensus)

import json
import sys
from datetime import datetime
from pathlib import Path

# Windows console: avoid UnicodeEncodeError on log lines
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from slipiq_confidence_agent import run_confidence_agent, SPORT_MLB
from slipiq_discord import (
    post_morning_brief,
    post_waiting_message,
    run_discord_post,
)

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────
# GRADE BONUSES
# ─────────────────────────────────────────
GRADE_BONUS = {
    "A":   20,
    "B+":  14,
    "B":   10,
    "B-":   6,
    "C+":   3,
    "C":    0,
    "D":  -10,
}

# ─────────────────────────────────────────
# HOW MANY PICKS TO POST
# ─────────────────────────────────────────
MAX_DAILY_POSTS = 3   # cap at 3 picks per day — quality over quantity


# ═════════════════════════════════════════
# CURATION SCORER
# ═════════════════════════════════════════

def curation_score(card: dict) -> float:
    """
    Score a pick card for curation ranking.
    Higher = stronger candidate for daily best pick.

    Factors:
      - Confidence (0-100)
      - Grade bonus
      - Edge magnitude (projection vs line diff)
      - EV confirmation bonus
      - Book count bonus (market consensus)
      - Trend alignment bonus
    """
    confidence  = card.get("confidence", 0)
    grade       = card.get("grade", "D")
    diff        = abs(card.get("diff", 0))
    ev_conf     = card.get("ev_confirmed", False)
    book_count  = card.get("book_count", 0)
    trend       = card.get("trend", "flat")
    direction   = card.get("direction", "")

    grade_pts   = GRADE_BONUS.get(grade, 0)
    edge_pts    = diff * 3                        # 3 pts per K of edge
    ev_pts      = 15 if ev_conf else 0            # big bonus for confirmed EV
    book_pts    = min(book_count * 2, 12)         # 2 pts per book, max 12

    # Trend alignment bonus — trend agrees with signal
    trend_aligned = (trend == "hot" and direction == "over") or \
                    (trend == "cold" and direction == "under")
    trend_pts   = 5 if trend_aligned else 0

    return confidence + grade_pts + edge_pts + ev_pts + book_pts + trend_pts


# ═════════════════════════════════════════
# PICK SELECTOR
# ═════════════════════════════════════════

def select_best_pick(post_list: list[dict]) -> dict | None:
    """
    From POST-gated picks, select the single best pick of the day.
    Used for #daily-picks headline pick.
    """
    if not post_list:
        return None
    return max(post_list, key=curation_score)


def select_top_picks(post_list: list[dict], max_picks: int = MAX_DAILY_POSTS) -> list[dict]:
    """
    Select top N picks from POST list for full daily post.
    Sorted by curation score, capped at max_picks.
    Filters out picks with the same game (avoids double-posting same matchup).
    """
    if not post_list:
        return []

    scored   = sorted(post_list, key=curation_score, reverse=True)
    selected = []
    seen_games = set()

    for card in scored:
        # Deduplicate by game — one pick per matchup
        game_key = (card.get("home_team"), card.get("away_team"))
        if game_key in seen_games:
            continue

        selected.append(card)
        seen_games.add(game_key)

        if len(selected) >= max_picks:
            break

    return selected


# ═════════════════════════════════════════
# SLATE LOGGER
# ═════════════════════════════════════════

def log_slate(slate: dict, top_picks: list[dict]):
    """
    Save today's curated picks to cache for Sharp Review.
    Sharp Review reads this post-game to grade results.
    """
    log = {
        "date":       datetime.now().strftime("%Y-%m-%d"),
        "run_time":   datetime.now().isoformat(),
        "top_picks":  top_picks,
        "post_count": slate.get("post_count", 0),
        "hold_count": slate.get("hold_count", 0),
        "skip_count": slate.get("skip_count", 0),
        "total":      slate.get("total", 0),
    }

    # Daily log
    log_path = CACHE_DIR / f"slate_{datetime.now().strftime('%Y%m%d')}.json"
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2, default=str)

    # Latest picks — Sharp Review always reads this
    latest_path = CACHE_DIR / "latest_picks.json"
    with open(latest_path, "w") as f:
        json.dump(log, f, indent=2, default=str)

    print(f"  [curate] Slate logged -> {log_path.name}")
    return log_path


# ═════════════════════════════════════════
# STATUS CHECKER
# ═════════════════════════════════════════

def is_market_open(slate: dict) -> bool:
    """
    True when we have postable or lean picks with lines (thin market OK).
    """
    post_list = slate.get("post_list", [])
    hold_list = slate.get("hold_list", [])
    if post_list or hold_list:
        return True

    all_cards = slate.get("all_cards") or []
    for card in all_cards:
        if card.get("line") and card.get("confidence", 0) >= 50:
            return True
    return False


def select_lean_picks(slate: dict, max_picks: int = MAX_DAILY_POSTS) -> list[dict]:
    """When nothing POSTs, surface best HOLD/SKIP cards for private channel."""
    candidates = []
    for card in slate.get("hold_list", []) + slate.get("skip_list", []):
        if card.get("grade") in ("C", "D", "N/A"):
            continue
        if card.get("confidence", 0) < 58:
            continue
        if not card.get("line"):
            continue
        candidates.append(card)

    candidates.sort(key=curation_score, reverse=True)
    seen = set()
    out = []
    for card in candidates:
        key = (card.get("home_team"), card.get("away_team"))
        if key in seen:
            continue
        seen.add(key)
        card = dict(card)
        card["gate"] = "LEAN"
        out.append(card)
        if len(out) >= max_picks:
            break
    return out


# ═════════════════════════════════════════
# MAIN CURATION RUNNER
# ═════════════════════════════════════════

def run_curation(post_to_discord: bool = True, sport_key: str = SPORT_MLB) -> dict:
    """
    Full morning curation pipeline.

    1. Run confidence agent
    2. Check if market is open
    3. Select best + top picks
    4. Log slate
    5. Post to Discord (if post_to_discord=True)

    Returns summary dict.
    """
    print("\n" + "=" * 60)
    print("SlipIQ Morning Curation")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    # Step 1: Run full pipeline
    slate = run_confidence_agent(sport_key)

    # Step 2: Market open check
    market_open = is_market_open(slate)
    post_list   = slate.get("post_list", [])

    print(f"\n  Market open : {'YES' if market_open else 'NO — DFS books only'}")
    print(f"  POST picks  : {len(post_list)}")

    # Step 3: Select picks (POST first, else lean slate for thin markets)
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
        print(f"\n  Best pick   : {best_pick.get('player')} "
              f"{best_pick.get('direction', '').upper()} "
              f"{best_pick.get('line')} "
              f"[{best_pick.get('grade')}] "
              f"{best_pick.get('confidence')}%")
        print(f"  Score       : {curation_score(best_pick):.1f}")
    else:
        print("\n  No best pick — market not open yet")

    # Step 4: Log
    log_path = log_slate(slate, top_picks)

    # Step 5: Discord + parlay channel (parlay always attempted when picks exist)
    if post_to_discord:
        if top_picks:
            print(f"\n  [discord] Posting {len(top_picks)} picks...")
            slate["best_pick"]  = best_pick
            slate["post_list"]  = top_picks
            slate["post_count"] = len(top_picks)
            slate["lean_mode"]  = bool(lean_picks)
            try:
                run_discord_post(slate)
            except Exception as e:
                print(f"\n  [discord] Error: {e}")
        elif market_open:
            print("\n  [discord] Market thin — no picks cleared POST gate")
            post_waiting_message()
        else:
            print("\n  [discord] Posting waiting message...")
            post_waiting_message()

        try:
            from slipiq_parlay_alerts import post_parlay_alerts
            post_parlay_alerts(slate)
        except Exception as e:
            print(f"\n  [parlay] Error posting parlay channel: {e}")
    else:
        print("\n  [discord] Skipped (post_to_discord=False)")

    return {
        "best_pick":    best_pick,
        "top_picks":    top_picks,
        "market_open":  market_open,
        "post_count":   len(top_picks),
        "slate":        slate,
        "log_path":     str(log_path),
    }


# ═════════════════════════════════════════
# TEST / MANUAL RUN
# ═════════════════════════════════════════

if __name__ == "__main__":
    import sys

    # Pass --no-discord to test without posting
    no_discord = "--no-discord" in sys.argv

    result = run_curation(post_to_discord=not no_discord)

    print("\n" + "=" * 60)
    print("CURATION SUMMARY")
    print("=" * 60)

    best = result.get("best_pick")
    if best:
        print(f"\n  ★ Best Pick: {best.get('player')}")
        print(f"    {best.get('direction','').upper()} {best.get('line')} Ks")
        print(f"    Grade: {best.get('grade')} | Confidence: {best.get('confidence')}%")
        print(f"    Curation score: {curation_score(best):.1f}")
        bk = best.get("best_book")
        if bk:
            print(f"    ▶ {bk['side'].upper()} {bk['price']} @ {bk['book']}")

    top = result.get("top_picks", [])
    if top:
        print(f"\n  Top {len(top)} picks for today:")
        for i, card in enumerate(top, 1):
            print(f"  {i}. [{card.get('grade')}] {card.get('player'):<22} "
                  f"{card.get('direction','').upper():5} {card.get('line')} | "
                  f"{card.get('confidence')}% | "
                  f"Score: {curation_score(card):.1f}")

    print(f"\n  Market open : {result.get('market_open')}")
    print(f"  Log saved   : {result.get('log_path')}")
    print(f"\n  Run with --no-discord to skip Discord posting")

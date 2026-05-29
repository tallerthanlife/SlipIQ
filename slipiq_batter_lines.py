# slipiq_batter_lines.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ Batter Lines — fetch + aggregate batter prop lines
#
# WHAT CHANGED:
#   filter_batter_candidates() — ev_confirmed used fake check:
#     ev_confirmed = (ev_over and ev_over > 0.02) or (ev_under and ev_under > 0.02)
#   This was using parlayapi's raw EV float, not real edge from ev_engine.
#   Fixed: ev_confirmed now calls assess_leg() with Pinnacle prices.
#   If no Pinnacle: falls back to parlayapi flag (marked as unconfirmed).
#
#   run_batter_analysis() — no longer imports from slipiq_lines.
#   Delegates directly to slipiq_batter_model.run_batter_model().
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from slipiq_parlayapi import (
    fetch_props_raw,
    aggregate_by_player,
    SPORT_MLB,
)
from slipiq_grading import calc_grade

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

PRIMARY_MARKETS = {
    "player_hits",
    "player_total_bases",
    "player_home_runs",
    "player_rbis",
    "player_runs",
}

SECONDARY_MARKETS = {
    "player_singles",
    "player_doubles",
    "player_stolen_bases",
}

ALL_BATTER_MARKETS = PRIMARY_MARKETS | SECONDARY_MARKETS


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — LINE FETCH
# ═══════════════════════════════════════════════════════════════

def get_all_batter_lines(
    sport_key: str = SPORT_MLB,
    markets:   set = None,
    force:     bool = False,
) -> dict:
    """
    Fetch and aggregate batter prop lines from parlayapi.
    Returns dict keyed by (player_name, market).
    """
    target = markets or PRIMARY_MARKETS
    raw    = fetch_props_raw(sport_key, force=force)

    if not raw:
        return {}

    # Filter to batter markets only
    batter_raw = [
        p for p in raw
        if p.get("market_key") in target
        or (p.get("market", "") in target)
    ]

    return aggregate_by_player(batter_raw)


def get_batter_lines(
    sport_key: str = SPORT_MLB,
    markets:   set = None,
    force:     bool = False,
) -> dict:
    """Alias — primary entry point for batter model."""
    return get_all_batter_lines(sport_key, markets, force)


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — CANDIDATE FILTER (fixed — real EV check)
# ═══════════════════════════════════════════════════════════════

def filter_batter_candidates(
    sport_key:  str   = SPORT_MLB,
    min_books:  int   = 1,
    ev_only:    bool  = False,
    markets:    set   = None,
) -> list[dict]:
    """
    Filter batter props to candidates worth running through the model.

    FIXED: ev_confirmed now uses assess_leg() with Pinnacle prices
    instead of the old fake threshold check.

    Args:
        min_books : minimum books posting
        ev_only   : if True, only return legs where real EV confirmed
        markets   : set of market keys to consider
    """
    all_lines = get_all_batter_lines(sport_key, markets=markets)
    candidates = []

    for (player, market), data in all_lines.items():
        book_count = data.get("book_count", 0)
        if book_count < min_books:
            continue

        pinnacle   = data.get("pinnacle") or {}
        pin_over   = pinnacle.get("over_price")
        pin_under  = pinnacle.get("under_price")
        ev_over    = data.get("ev_over")
        ev_under   = data.get("ev_under")
        best_over  = data.get("best_over") or {}
        best_under = data.get("best_under") or {}

        # ── Real EV check (fixed) ──────────────────────────────
        ev_confirmed = False
        ev_source    = "none"

        if pin_over and pin_under:
            try:
                from slipiq_ev_engine import assess_leg
                # Check over side
                soft_over_price  = best_over.get("over_price",   -115)
                soft_under_price = best_under.get("under_price", -115)

                res_over  = assess_leg(pin_over, pin_under, soft_over_price,  "over")
                res_under = assess_leg(pin_over, pin_under, soft_under_price, "under")

                if res_over["ev"] >= 0.02 or res_under["ev"] >= 0.02:
                    ev_confirmed = True
                    ev_source    = "ev_engine_pinnacle"
            except Exception:
                # Fallback to parlayapi
                if (ev_over and ev_over > 0.02) or (ev_under and ev_under > 0.02):
                    ev_confirmed = True
                    ev_source    = "parlayapi_fallback"
        else:
            # No Pinnacle — use parlayapi flag as soft indicator only
            if (ev_over and ev_over > 0.02) or (ev_under and ev_under > 0.02):
                ev_confirmed = True
                ev_source    = "parlayapi_only"

        if ev_only and not ev_confirmed:
            continue

        candidates.append({
            "player":       player,
            "market":       market,
            "line":         data.get("line_consensus"),
            "book_count":   book_count,
            "ev_over":      ev_over,
            "ev_under":     ev_under,
            "ev_confirmed": ev_confirmed,
            "ev_source":    ev_source,
            "best_over":    best_over,
            "best_under":   best_under,
            "pinnacle":     pinnacle,
            "home_team":    data.get("home_team"),
            "away_team":    data.get("away_team"),
            "game_date":    data.get("game_date"),
        })

    # Sort: ev_engine confirmed first, then book count, then market priority
    market_priority = {m: i for i, m in enumerate(sorted(PRIMARY_MARKETS))}
    candidates.sort(key=lambda x: (
        0 if x["ev_source"] == "ev_engine_pinnacle" else
        1 if x["ev_source"] == "parlayapi_fallback" else 2,
        -x["book_count"],
        market_priority.get(x["market"], 99),
    ))

    return candidates


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — FULL ANALYSIS (fixed — no longer imports slipiq_lines)
# ═══════════════════════════════════════════════════════════════

def run_batter_analysis(
    sport_key:      str = SPORT_MLB,
    min_confidence: int = 55,
) -> list[dict]:
    """
    Full batter analysis pipeline.
    FIXED: no longer imports from slipiq_lines (which was circular/broken).
    Delegates directly to slipiq_batter_model.run_batter_model().
    """
    try:
        from slipiq_batter_model import run_batter_model
        return run_batter_model(sport_key=sport_key, min_confidence=min_confidence)
    except Exception as e:
        print(f"  [batter_lines] run_batter_analysis error: {e}")
        return []


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — CACHE
# ═══════════════════════════════════════════════════════════════

def save_batter_slate_cache(slate: dict) -> Path:
    path = CACHE_DIR / f"batter_slate_{datetime.now().strftime('%Y%m%d')}.json"
    with open(path, "w") as f:
        json.dump(slate, f, indent=2, default=str)
    print(f"  [cache] batter slate → {path.name}")
    return path


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Batter Lines Test")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    lines = get_batter_lines()
    print(f"\n[1] Lines: {len(lines)} player/market combos")

    market_counts: dict[str, int] = defaultdict(int)
    for (player, market) in lines.keys():
        market_counts[market] += 1
    for mkt, cnt in sorted(market_counts.items()):
        print(f"    {mkt:<35} {cnt} players")

    candidates = filter_batter_candidates(min_books=2)
    ev_real    = [c for c in candidates if c["ev_source"] == "ev_engine_pinnacle"]
    print(f"\n[2] Candidates: {len(candidates)} | Real EV (Pinnacle): {len(ev_real)}")

    if candidates:
        print("\n  Top 5 candidates:")
        for c in candidates[:5]:
            print(f"    {c['player']:<22} {c['market'].replace('player_',''):<18} "
                  f"EV: {c['ev_source']:<20} conf: {c['ev_confirmed']}")

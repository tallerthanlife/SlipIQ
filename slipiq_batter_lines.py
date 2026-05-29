# slipiq_batter_lines.py
# Batter prop lines from ParlayAPI
# Phase 2 — runs after pitcher model is confirmed
#
# MARKETS COVERED:
#   player_hits, player_total_bases, player_home_runs,
#   player_rbis, player_runs, player_singles, player_doubles,
#   player_stolen_bases, player_hitter_strikeouts,
#   player_walks, player_hits_runs_rbis
#
# OUTPUT:
#   Normalized batter prop lines aggregated by player + market
#   Ready for slipiq_batter_model.py projection engine
#
# CREDIT COST:
#   0 additional credits — reuses cached /props from pitcher run
#   get_all_props() pulls everything in one 3-credit call

import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

from slipiq_parlayapi import (
    get_batter_props,
    aggregate_by_player,
    SPORT_MLB,
    BATTER_PROP_KEYS,
    AZ_BLOCKED_BOOKS,
)

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────
# TARGET MARKETS — what we actually model
# ─────────────────────────────────────────
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
    "player_hitter_strikeouts",
    "player_walks",
    "player_hits_runs_rbis",
}

# Minimum line values — filter noise
MIN_LINES = {
    "player_hits":             0.5,
    "player_total_bases":      0.5,
    "player_home_runs":        0.5,
    "player_rbis":             0.5,
    "player_runs":             0.5,
    "player_singles":          0.5,
    "player_doubles":          0.5,
    "player_stolen_bases":     0.5,
    "player_hitter_strikeouts": 0.5,
    "player_walks":            0.5,
    "player_hits_runs_rbis":   1.5,
}


# ═════════════════════════════════════════
# FETCHER
# ═════════════════════════════════════════

def get_batter_lines(
    sport_key: str = SPORT_MLB,
    markets: set = None,
) -> dict:
    """
    Pull batter prop lines from ParlayAPI.
    Uses cached /props — no additional credits if pitcher model already ran.

    Returns: aggregated dict keyed by (player, market_key)
    Same structure as pitcher model aggregate_by_player() output.
    """
    target_markets = markets or PRIMARY_MARKETS

    raw_props = get_batter_props(sport_key)

    # Filter to target markets only
    filtered = [
        p for p in raw_props
        if p.get("market_key", "").lower() in target_markets
    ]

    # Filter minimum lines
    filtered = [
        p for p in filtered
        if p.get("line") is not None and
        p.get("line") >= MIN_LINES.get(p.get("market_key", ""), 0.5)
    ]

    if not filtered:
        return {}

    return aggregate_by_player(filtered)


def get_all_batter_lines(sport_key: str = SPORT_MLB) -> dict:
    """
    Pull ALL batter markets — primary + secondary.
    Returns: aggregated dict keyed by (player, market_key)
    """
    all_markets = PRIMARY_MARKETS | SECONDARY_MARKETS
    return get_batter_lines(sport_key, markets=all_markets)


# ═════════════════════════════════════════
# PLAYER SUMMARY
# ═════════════════════════════════════════

def get_player_prop_summary(player_name: str, sport_key: str = SPORT_MLB) -> dict:
    """
    Get all available prop lines for a single player.
    Useful for slip builder and pre-game alerts.
    Returns dict of {market_key: aggregated_data}
    """
    all_lines = get_all_batter_lines(sport_key)

    player_props = {
        market: data
        for (player, market), data in all_lines.items()
        if player.lower() == player_name.lower()
    }

    return player_props


def get_todays_batters(sport_key: str = SPORT_MLB) -> list[str]:
    """
    Return list of unique batter names with lines today.
    Used by batter model to know who to project.
    """
    lines = get_all_batter_lines(sport_key)
    players = sorted(set(player for player, market in lines.keys()))
    return players


# ═════════════════════════════════════════
# SLATE BUILDER
# ═════════════════════════════════════════

def build_batter_slate(sport_key: str = SPORT_MLB) -> dict:
    """
    Build full batter prop slate for today.
    Groups by player, then by market.
    Ready for batter model input.

    Returns:
    {
        "player_name": {
            "player_hits": {line, best_over, best_under, book_count, ...},
            "player_total_bases": {...},
            ...
        }
    }
    """
    all_lines = get_all_batter_lines(sport_key)

    slate = defaultdict(dict)
    for (player, market), data in all_lines.items():
        slate[player][market] = {
            "line":          data.get("line_consensus"),
            "best_over":     data.get("best_over"),
            "best_under":    data.get("best_under"),
            "pinnacle":      data.get("pinnacle"),
            "book_count":    data.get("book_count", 0),
            "ev_over":       data.get("ev_over"),
            "ev_under":      data.get("ev_under"),
            "home_team":     data.get("home_team"),
            "away_team":     data.get("away_team"),
            "game_date":     data.get("game_date"),
        }

    return dict(slate)


def get_high_value_batter_props(
    min_books: int = 3,
    ev_only: bool = False,
    sport_key: str = SPORT_MLB,
) -> list[dict]:
    """
    Filter batter props to high-value candidates only.

    min_books: minimum books posting (market consensus)
    ev_only: only return props with confirmed EV vs Pinnacle

    Returns sorted list of prop dicts ready for model input.
    """
    all_lines = get_all_batter_lines(sport_key)

    candidates = []
    for (player, market), data in all_lines.items():
        book_count  = data.get("book_count", 0)
        ev_over     = data.get("ev_over")
        ev_under    = data.get("ev_under")
        ev_confirmed = (ev_over and ev_over > 0.02) or \
                       (ev_under and ev_under > 0.02)

        if book_count < min_books:
            continue
        if ev_only and not ev_confirmed:
            continue

        best_over  = data.get("best_over")
        best_under = data.get("best_under")
        pinnacle   = data.get("pinnacle")

        candidates.append({
            "player":      player,
            "market":      market,
            "line":        data.get("line_consensus"),
            "book_count":  book_count,
            "ev_over":     ev_over,
            "ev_under":    ev_under,
            "ev_confirmed": ev_confirmed,
            "best_over":   best_over,
            "best_under":  best_under,
            "pinnacle":    pinnacle,
            "home_team":   data.get("home_team"),
            "away_team":   data.get("away_team"),
            "game_date":   data.get("game_date"),
        })

    # Sort: EV confirmed first, then book count, then market priority
    market_priority = {m: i for i, m in enumerate(PRIMARY_MARKETS)}
    candidates.sort(key=lambda x: (
        0 if x["ev_confirmed"] else 1,
        -x["book_count"],
        market_priority.get(x["market"], 99),
    ))

    return candidates


# ═════════════════════════════════════════
# CACHE SAVE
# ═════════════════════════════════════════

def save_batter_slate_cache(slate: dict):
    """Save today's batter slate to cache for batter model."""
    path = CACHE_DIR / f"batter_slate_{datetime.now().strftime('%Y%m%d')}.json"
    with open(path, "w") as f:
        json.dump(slate, f, indent=2, default=str)
    print(f"  [cache] batter slate saved → {path.name}")
    return path


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — Batter Lines Test")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    # 1. Primary markets
    print("\n[1] Primary batter prop lines...")
    lines = get_batter_lines()
    print(f"    {len(lines)} player/market combos found")

    # Market breakdown
    market_counts = defaultdict(int)
    for (player, market) in lines.keys():
        market_counts[market] += 1

    print("\n    Markets posting:")
    for market, count in sorted(market_counts.items()):
        print(f"    {market:<35} {count} players")

    # 2. Sample lines
    if lines:
        print("\n[2] Sample batter lines (first 5):")
        for (player, market), data in list(lines.items())[:5]:
            line       = data.get("line_consensus")
            books      = data.get("book_count", 0)
            pinnacle   = data.get("pinnacle")
            best_over  = data.get("best_over")
            ev         = data.get("ev_over")

            print(f"\n  {player} — {market}")
            print(f"    Line: {line} | Books: {books}")
            if pinnacle:
                print(f"    Pinnacle: {pinnacle.get('line')} "
                      f"({pinnacle.get('over_price')} / {pinnacle.get('under_price')})")
            if best_over:
                print(f"    Best over: {best_over.get('over_price')} "
                      f"@ {best_over.get('book_title')}")
            if ev:
                print(f"    EV: {ev:+.1%}")

    # 3. High value props
    print("\n[3] High value props (3+ books)...")
    hv = get_high_value_batter_props(min_books=3)
    print(f"    {len(hv)} props with 3+ books posting")

    if hv:
        print("\n    Top 5 by book count:")
        for prop in hv[:5]:
            ev_tag = "✅ +EV" if prop.get("ev_confirmed") else ""
            print(f"    {prop['player']:<22} {prop['market']:<25} "
                  f"Line: {prop['line']} | Books: {prop['book_count']} {ev_tag}")

    # 4. Full slate
    print("\n[4] Building full batter slate...")
    slate = build_batter_slate()
    print(f"    {len(slate)} players in today's slate")

    save_batter_slate_cache(slate)

    print("\n✓ Batter lines confirmed.")

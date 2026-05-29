# slipiq_lines.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ Lines — compatibility shim
#
# WHAT THIS WAS:
#   A mini-pipeline that ran props → model → curate → picks list.
#   It duplicated slipiq_orchestrator + slipiq_confidence_agent entirely.
#   Anything importing run_full_analysis() from here bypassed
#   the rebuilt ev_engine, real EV gate, and SlipRouter.
#
# WHAT THIS IS NOW:
#   A thin delegation layer. All real logic lives in:
#     slipiq_confidence_agent.run_confidence_agent()
#     slipiq_game_lines (F5/F3 ML lines)
#     slipiq_parlayapi (props fetch)
#
# IMPORTERS:
#   slipiq_slate_parlay.py  — import updated below
#   slipiq_book_slip_builder.py — import updated below
#   Any other file importing run_full_analysis() will now get
#   the real confidence agent output automatically.
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

from slipiq_parlayapi import (
    fetch_props_raw,
    aggregate_by_player,
    get_pitcher_strikeout_props,
    SPORT_MLB,
)

from slipiq_game_lines import fetch_f5_ml_lines, build_f5_picks


# ─── Primary delegation ───────────────────────────────────────

def run_full_analysis(
    sport_key:   str  = SPORT_MLB,
    min_confidence: int = 55,
) -> list[dict]:
    """
    REBUILT: Delegates to run_confidence_agent() instead of running
    a duplicate pipeline. Returns the POST list from the confidence agent.

    All callers (slipiq_slate_parlay, slipiq_book_slip_builder) now
    automatically go through ev_engine, real EV gate, and SlipRouter.
    """
    try:
        from slipiq_confidence_agent import run_confidence_agent
        slate = run_confidence_agent(sport_key)
        post_list = slate.get("post_list", [])
        # Filter by min_confidence for callers that set a threshold
        if min_confidence > 0:
            post_list = [c for c in post_list if c.get("confidence", 0) >= min_confidence]
        return post_list
    except Exception as e:
        print(f"  [lines] run_full_analysis delegation error: {e}")
        # Hard fallback — raw props only, no model scoring
        try:
            raw   = get_pitcher_strikeout_props(sport_key)
            agg   = aggregate_by_player(raw)
            # Return minimal dicts so callers don't crash
            return [
                {"player": k[0], "market": k[1], "line": v.get("sharp_line"),
                 "confidence": 0, "grade": "?", "_raw": True}
                for k, v in agg.items() if v.get("sharp_line")
            ]
        except Exception:
            return []


def get_mlb_pitcher_props(sport_key: str = SPORT_MLB) -> list[dict]:
    """Raw pitcher prop fetch — no model. Delegates to parlayapi."""
    return get_pitcher_strikeout_props(sport_key)


def get_f5_lines(
    games_filter: set | None = None,
    force: bool = False,
) -> dict:
    """F5 ML lines — delegates to slipiq_game_lines."""
    return fetch_f5_ml_lines(games_filter=games_filter, force=force)


def get_f5_picks(slate: dict) -> list[dict]:
    """F5 ML pick selection — delegates to slipiq_game_lines."""
    return build_f5_picks(slate)


# ─── Legacy aliases (keep so old imports don't crash) ─────────

def pull_all_lines(sport_key: str = SPORT_MLB) -> dict:
    """Legacy alias — returns props dict from parlayapi."""
    raw = get_pitcher_strikeout_props(sport_key)
    return {"pitcher_strikeouts": raw}


def run_pitcher_analysis(sport_key: str = SPORT_MLB, **kwargs) -> list[dict]:
    """Legacy alias for run_full_analysis."""
    return run_full_analysis(sport_key, **kwargs)


MIN_CONFIDENCE = 55  # legacy constant some files may import

# slipiq_pitcher_props.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Pitcher Props Compatibility Shim
#
# PURPOSE: Resolves broken import:
#   from slipiq_pitcher_props import ...
#
# These functions were referenced in legacy code paths but never
# had their own module. This shim delegates to slipiq_parlayapi.py
# which is where the real logic lives.
#
# DO NOT add business logic here. This is a redirect only.
# ═══════════════════════════════════════════════════════════════

from slipiq_parlayapi import (
    get_pitcher_strikeout_props,
    aggregate_by_player,
    fetch_props_raw,
    SPORT_MLB,
)

# Legacy alias used in old import paths
get_all_pitcher_props  = get_pitcher_strikeout_props
get_strikeout_props    = get_pitcher_strikeout_props
aggregate_props        = aggregate_by_player


def enrich_picks(picks: list[dict]) -> list[dict]:
    """
    Legacy compatibility stub.
    Old code called enrich_picks() to add Groq confidence scoring.
    That logic now lives in slipiq_confidence_agent.rescore_confidence()
    and slipiq_confidence_agent.confirm_ev().

    This stub passes picks through unchanged so existing call sites
    don't crash. The real enrichment happens in the confidence agent.
    """
    return picks


def get_props_for_players(
    player_names: list[str],
    sport: str = SPORT_MLB,
) -> dict:
    """
    Fetch and aggregate props for a specific list of players.
    Delegates to parlayapi.
    """
    raw = get_pitcher_strikeout_props(sport)
    agg = aggregate_by_player(raw)

    if not player_names:
        return agg

    names_lower = {n.lower().strip() for n in player_names}
    return {
        k: v for k, v in agg.items()
        if k[0].lower() in names_lower
    }

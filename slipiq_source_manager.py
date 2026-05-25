"""
SlipIQ Source Manager
Orchestrates all prop line sources with priority order and failsafe fallback
Priority: Cache → SportsData.io → ActionNetwork → Odds API

Never fails silently — always reports which source provided data
"""

import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# Source health tracking
_source_health = {
    "sportsdata": {"status": "unknown", "last_count": 0},
    "action_network": {"status": "unknown", "last_count": 0},
    "odds_api": {"status": "unknown", "last_count": 0},
    "cache": {"status": "unknown", "last_count": 0},
}


# ─── Pitcher Props ────────────────────────────────────────────

def get_pitcher_props():
    """
    Get pitcher props from best available source
    Returns dict keyed by pitcher name
    Priority: Cache → SportsData.io → Odds API
    """
    print("\n[Source Manager] Fetching pitcher props...")

    # 1. Try cache first
    try:
        from slipiq_cache import cache_get, cache_set
        cache_key = f"pitcher_props_{date.today().isoformat()}"
        cached = cache_get(cache_key)
        if cached:
            _source_health["cache"]["status"] = "ok"
            _source_health["cache"]["last_count"] = len(cached)
            print(f"  ✅ Cache: {len(cached)} pitchers")
            return cached, "Cache"
    except Exception as e:
        print(f"  Cache error: {e}")

    # 2. Try SportsData.io (primary free source)
    try:
        from slipiq_sportsdata import get_pitcher_props as sd_pitchers
        props = sd_pitchers()
        if props:
            _source_health["sportsdata"]["status"] = "ok"
            _source_health["sportsdata"]["last_count"] = len(props)
            print(f"  ✅ SportsData.io: {len(props)} pitchers")
            # Cache result
            try:
                from slipiq_cache import cache_set
                cache_set(f"pitcher_props_{date.today().isoformat()}", props)
            except Exception:
                pass
            return props, "SportsData"
    except Exception as e:
        _source_health["sportsdata"]["status"] = "error"
        print(f"  ❌ SportsData.io failed: {e}")

    # 3. Fallback to Odds API (preserve quota)
    try:
        from slipiq_pitcher_props import get_all_pitcher_props
        props = get_all_pitcher_props()
        if props:
            _source_health["odds_api"]["status"] = "ok"
            _source_health["odds_api"]["last_count"] = len(props)
            print(f"  ✅ Odds API fallback: {len(props)} pitchers")
            return props, "Odds API"
        else:
            _source_health["odds_api"]["status"] = "empty"
    except Exception as e:
        _source_health["odds_api"]["status"] = "error"
        print(f"  ❌ Odds API failed: {e}")

    print("  ❌ All pitcher prop sources failed")
    return {}, "None"


# ─── Batter Props ─────────────────────────────────────────────

def get_batter_props():
    """
    Get batter props from best available source
    Returns list of prop dicts
    Priority: Cache → SportsData.io → Odds API
    """
    print("\n[Source Manager] Fetching batter props...")

    # 1. Try cache
    try:
        from slipiq_cache import cache_get
        cache_key = f"batter_props_{date.today().isoformat()}"
        cached = cache_get(cache_key)
        if cached:
            print(f"  ✅ Cache: {len(cached)} batter props")
            return cached, "Cache"
    except Exception as e:
        print(f"  Cache error: {e}")

    # 2. Try SportsData.io
    try:
        from slipiq_sportsdata import get_batter_props as sd_batters
        props = sd_batters()
        if props:
            print(f"  ✅ SportsData.io: {len(set(p['batter'] for p in props))} batters")
            try:
                from slipiq_cache import cache_set
                cache_set(f"batter_props_{date.today().isoformat()}", props)
            except Exception:
                pass
            return props, "SportsData"
    except Exception as e:
        print(f"  ❌ SportsData.io failed: {e}")

    # 3. Fallback to Odds API
    try:
        from slipiq_batter_lines import get_mlb_batter_props
        props = get_mlb_batter_props()
        if props:
            print(f"  ✅ Odds API fallback: {len(props)} batter props")
            return props, "Odds API"
    except Exception as e:
        print(f"  ❌ Odds API batter fallback failed: {e}")

    print("  ❌ All batter prop sources failed")
    return [], "None"


# ─── Game Lines ───────────────────────────────────────────────

def get_game_lines():
    """
    Get game ML, totals, team totals from ActionNetwork
    Used for F5 ML logic in slate parlay
    """
    print("\n[Source Manager] Fetching game lines...")

    try:
        from slipiq_action_network import get_all_game_lines
        lines = get_all_game_lines()
        if lines:
            _source_health["action_network"]["status"] = "ok"
            _source_health["action_network"]["last_count"] = len(lines)
            print(f"  ✅ ActionNetwork: {len(lines)} games")
            return lines, "ActionNetwork"
        else:
            _source_health["action_network"]["status"] = "empty"
    except Exception as e:
        _source_health["action_network"]["status"] = "error"
        print(f"  ❌ ActionNetwork failed: {e}")

    print("  ❌ Game lines unavailable")
    return [], "None"


# ─── Full Morning Pull ────────────────────────────────────────

def pull_all_sources():
    """
    Pull everything needed for the morning pipeline
    Returns complete data package with source attribution
    """
    print("\n" + "="*52)
    print("SlipIQ Source Manager — Morning Pull")
    print("="*52)

    pitcher_props, pitcher_source = get_pitcher_props()
    batter_props, batter_source = get_batter_props()
    game_lines, lines_source = get_game_lines()

    summary = {
        "pitcher_props": pitcher_props,
        "pitcher_source": pitcher_source,
        "batter_props": batter_props,
        "batter_source": batter_source,
        "game_lines": game_lines,
        "lines_source": lines_source,
        "pitcher_count": len(pitcher_props),
        "batter_count": len(set(
            p.get("batter", p.get("Name", ""))
            for p in (batter_props if isinstance(batter_props, list) else [])
        )),
        "game_count": len(game_lines),
    }

    print(f"\n{'='*52}")
    print("Source Summary:")
    print(f"  Pitchers:  {summary['pitcher_count']} from {pitcher_source}")
    print(f"  Batters:   {summary['batter_count']} from {batter_source}")
    print(f"  Games:     {summary['game_count']} from {lines_source}")
    print("="*52)

    return summary


# ─── Source Health Report ─────────────────────────────────────

def get_source_health_embed():
    """Build Discord embed showing source health"""
    try:
        import discord
        status_emoji = {"ok": "✅", "error": "❌", "empty": "⚠️", "unknown": "❓"}

        embed = discord.Embed(
            title="📡 SlipIQ Source Health",
            color=0x1A1A2E,
        )

        for source, health in _source_health.items():
            emoji = status_emoji.get(health["status"], "❓")
            embed.add_field(
                name=f"{emoji} {source.replace('_', ' ').title()}",
                value=f"Status: {health['status']} | Last: {health['last_count']} items",
                inline=True,
            )

        embed.set_footer(text="SlipIQ • Source Manager")
        return embed
    except Exception:
        return None


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    summary = pull_all_sources()

    print(f"\nPitcher props sample:")
    for name, data in list(summary["pitcher_props"].items())[:3]:
        props = data.get("props", {})
        prop_list = ", ".join(
            f"{p}: {sides.get('Over', {}).get('line', '?')}"
            for p, sides in props.items()
        )
        print(f"  {name}: {prop_list}")

    print(f"\nBatter props sample:")
    seen = set()
    count = 0
    for prop in summary["batter_props"]:
        batter = prop.get("batter", prop.get("Name", ""))
        if batter not in seen and prop.get("direction") == "Over":
            seen.add(batter)
            print(f"  {batter} — {prop.get('prop_type', prop.get('Description', '?'))} {prop.get('line', prop.get('OverUnder', '?'))}")
            count += 1
            if count >= 5:
                break

    print(f"\nGame lines sample:")
    for game in summary["game_lines"][:3]:
        fg = game.get("full_game", {})
        print(f"  {game['away_team']} @ {game['home_team']} | ML: {fg.get('ml_away')} / {fg.get('ml_home')}")
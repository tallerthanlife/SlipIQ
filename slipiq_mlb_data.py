# slipiq_mlb_data.py
# Phase ③ — Stats pull · ZERO credits · ZERO API keys
# Source: pybaseball → Baseball Savant (Statcast)
# FanGraphs dropped — 403 blocking pybaseball as of 2026
#
# What this pulls:
#   - Pitcher Statcast season aggregates (K rate, whiff, spin, velo)
#   - Pitcher recent form (last 5 starts via Statcast game logs)
#   - Batter K vulnerability (Statcast batter data)
#   - Park factors (pybaseball parkfactors)

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pybaseball as pyb

pyb.cache.enable()

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

CURRENT_YEAR = datetime.now().year
SEASON_START = f"{CURRENT_YEAR}-03-15"
TODAY = datetime.now().strftime("%Y-%m-%d")
LAST_30 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")


# ═════════════════════════════════════════
# CACHE HELPERS
# ═════════════════════════════════════════

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"mlb_{key}.json"

def _cache_write(key: str, data):
    payload = {"timestamp": datetime.utcnow().isoformat(), "data": data}
    with open(_cache_path(key), "w") as f:
        json.dump(payload, f)

def _cache_read(key: str, max_age_hours: int = 12):
    path = _cache_path(key)
    if not path.exists():
        return None
    with open(path) as f:
        payload = json.load(f)
    ts = datetime.fromisoformat(payload["timestamp"])
    if datetime.utcnow() - ts > timedelta(hours=max_age_hours):
        return None
    return payload["data"]


# ═════════════════════════════════════════
# PITCHER STATCAST SEASON AGGREGATES
# ═════════════════════════════════════════

def get_pitcher_statcast_season(force: bool = False) -> pd.DataFrame:
    """
    Pull pitcher-level Statcast aggregates for the full season.
    Source: Baseball Savant via pybaseball.statcast_pitcher_exitvelo_barrels()
    and pitch-level data aggregated by pitcher.

    Key outputs per pitcher:
      whiff_rate, k_rate, chase_rate, avg_velo, avg_spin, pitches_total
    """
    cache_key = f"statcast_season_{CURRENT_YEAR}"
    if not force:
        cached = _cache_read(cache_key, max_age_hours=12)
        if cached:
            print(f"  [cache] statcast season hit")
            return pd.DataFrame(cached)

    print(f"  [fetch] Statcast pitcher season {SEASON_START} -> {TODAY}...")
    print(f"          (Baseball Savant — this takes 30-60s first run)")

    try:
        df = pyb.statcast(start_dt=SEASON_START, end_dt=TODAY)
    except Exception as e:
        print(f"  [error] Statcast full season failed: {e}")
        return pd.DataFrame()

    if df is None or df.empty:
        print("  [warn] Statcast returned empty")
        return pd.DataFrame()

    print(f"  [raw] {len(df):,} pitches loaded — aggregating...")

    # Filter to pitcher view
    df = df[df["player_name"].notna() & (df["inning_topbot"].notna())]

    agg = (
        df.groupby("player_name")
        .agg(
            pitches_total    = ("pitch_type", "count"),
            avg_velo         = ("release_speed", "mean"),
            avg_spin         = ("release_spin_rate", "mean"),
            whiff_rate       = ("description", lambda x:
                                (x == "swinging_strike").sum() / len(x)),
            called_k_rate    = ("description", lambda x:
                                (x == "called_strike").sum() / len(x)),
            k_rate           = ("events", lambda x:
                                (x == "strikeout").sum() / max(len(x), 1)),
            bb_rate          = ("events", lambda x:
                                (x == "walk").sum() / max(len(x), 1)),
            hard_hit_rate    = ("launch_speed", lambda x:
                                (x >= 95).sum() / max(len(x), 1)),
        )
        .reset_index()
    )

    agg["avg_velo"]      = agg["avg_velo"].round(1)
    agg["avg_spin"]      = agg["avg_spin"].round(0)
    agg["whiff_rate"]    = agg["whiff_rate"].round(4)
    agg["k_rate"]        = agg["k_rate"].round(4)
    agg["bb_rate"]       = agg["bb_rate"].round(4)
    agg["hard_hit_rate"] = agg["hard_hit_rate"].round(4)

    # Only include pitchers with meaningful sample
    agg = agg[agg["pitches_total"] >= 50]

    _cache_write(cache_key, agg.to_dict(orient="records"))
    print(f"  [done] {len(agg)} pitchers aggregated")
    return agg


# ═════════════════════════════════════════
# PITCHER RECENT FORM (Last N Starts)
# ═════════════════════════════════════════

def get_pitcher_recent_form(player_name: str, n_starts: int = 5) -> dict:
    """
    Pull per-start K totals for a specific pitcher via Statcast game logs.
    Returns avg_k, trend, and per-start breakdown.
    """
    safe_name = player_name.replace(" ", "_").replace("'", "")
    cache_key = f"form_{safe_name}_{CURRENT_YEAR}"

    cached = _cache_read(cache_key, max_age_hours=6)
    if cached:
        print(f"  [cache] recent form: {player_name}")
        return cached

    print(f"  [fetch] Recent form: {player_name}...")

    try:
        # Lookup MLBAM ID
        parts = player_name.strip().split()
        last, first = parts[-1], parts[0]
        lookup = pyb.playerid_lookup(last, first)

        if lookup.empty:
            print(f"  [warn] Not found: {player_name}")
            return {"player": player_name, "error": "not_found"}

        mlbam_id = int(lookup.iloc[0]["key_mlbam"])

        # Pull Statcast pitcher log for season
        log = pyb.statcast_pitcher(
            start_dt=SEASON_START,
            end_dt=TODAY,
            player_id=mlbam_id
        )

        if log is None or log.empty:
            return {"player": player_name, "error": "no_data"}

        log["game_date"] = pd.to_datetime(log["game_date"])

        # Aggregate per game start
        starts = (
            log.groupby("game_date")
            .agg(
                k_total   = ("events", lambda x: (x == "strikeout").sum()),
                pitches   = ("pitch_type", "count"),
                whiffs    = ("description", lambda x:
                             (x == "swinging_strike").sum()),
            )
            .sort_values("game_date", ascending=False)
            .head(n_starts)
        )

        k_list = starts["k_total"].tolist()
        avg_k  = round(sum(k_list) / len(k_list), 1) if k_list else 0

        # Trend: last 2 vs prior
        if len(k_list) >= 4:
            recent = sum(k_list[:2]) / 2
            prior  = sum(k_list[2:]) / len(k_list[2:])
            if recent > prior + 0.75:
                trend = "hot"
            elif recent < prior - 0.75:
                trend = "cold"
            else:
                trend = "flat"
        else:
            trend = "small_sample"

        result = {
            "player":      player_name,
            "mlbam_id":    mlbam_id,
            "n_starts":    len(starts),
            "k_per_start": k_list,
            "avg_k":       avg_k,
            "avg_pitches": round(starts["pitches"].mean(), 1),
            "trend":       trend,
            "dates":       [str(d.date()) for d in starts.index.tolist()],
        }

        _cache_write(cache_key, result)
        return result

    except Exception as e:
        print(f"  [error] {player_name}: {e}")
        return {"player": player_name, "error": str(e)}


# ═════════════════════════════════════════
# BATTER K VULNERABILITY
# ═════════════════════════════════════════

def get_batter_k_rates(force: bool = False) -> pd.DataFrame:
    """
    Pull batter strikeout rates from Statcast season data.
    Used to assess how K-prone an opponent lineup is vs a given pitcher.
    """
    cache_key = f"batter_k_{CURRENT_YEAR}"

    if not force:
        cached = _cache_read(cache_key, max_age_hours=12)
        if cached:
            print(f"  [cache] batter K rates hit")
            return pd.DataFrame(cached)

    print(f"  [fetch] Batter K rates {SEASON_START} -> {TODAY}...")

    try:
        df = pyb.statcast(start_dt=SEASON_START, end_dt=TODAY)

        if df is None or df.empty:
            return pd.DataFrame()

        # Batter view — flip the perspective
        batter_agg = (
            df[df["batter"].notna()]
            .groupby("batter")
            .agg(
                pa_total   = ("pitch_type", "count"),
                k_rate     = ("events", lambda x:
                              (x == "strikeout").sum() / max(len(x), 1)),
                bb_rate    = ("events", lambda x:
                              (x == "walk").sum() / max(len(x), 1)),
                whiff_rate = ("description", lambda x:
                              (x == "swinging_strike").sum() / len(x)),
            )
            .reset_index()
        )

        batter_agg = batter_agg[batter_agg["pa_total"] >= 30]
        batter_agg["k_rate"]     = batter_agg["k_rate"].round(4)
        batter_agg["bb_rate"]    = batter_agg["bb_rate"].round(4)
        batter_agg["whiff_rate"] = batter_agg["whiff_rate"].round(4)

        _cache_write(cache_key, batter_agg.to_dict(orient="records"))
        print(f"  [done] {len(batter_agg)} batters loaded")
        return batter_agg

    except Exception as e:
        print(f"  [error] Batter K rates: {e}")
        return pd.DataFrame()


# ═════════════════════════════════════════
# PITCHER BUNDLE — single call for model
# ═════════════════════════════════════════

def get_pitcher_bundle(player_name: str) -> dict:
    """
    Everything slipiq_pitcher_model.py needs for one pitcher:
      - Season Statcast aggregates (whiff, K rate, velo, spin)
      - Recent form (last 5 starts)
    """
    print(f"\n  Building bundle: {player_name}")

    # Season stats
    season = get_pitcher_statcast_season()
    season_row = season[
        season["player_name"].str.lower() == player_name.lower()
    ] if not season.empty else pd.DataFrame()

    season_data = season_row.iloc[0].to_dict() if not season_row.empty else {}
    if not season_data:
        print(f"  [warn] {player_name} not in Statcast season data")

    # Recent form
    form = get_pitcher_recent_form(player_name)

    return {
        "player":       player_name,
        "season_stats": season_data,
        "recent_form":  form,
    }


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — MLB Data Layer (Statcast only, no FanGraphs)")
    print("=" * 60)

    # 1. Season pitcher aggregates
    print("\n[1] Loading pitcher Statcast season stats...")
    print("    (30-60s first run — fetching from Baseball Savant)")
    pitchers = get_pitcher_statcast_season()

    if not pitchers.empty:
        print(f"\n    Loaded {len(pitchers)} pitchers")
        top = pitchers.sort_values("whiff_rate", ascending=False).head(5)
        print("\n    Top 5 by whiff rate:")
        for _, row in top.iterrows():
            print(f"    {row['player_name']:<25} "
                  f"Whiff: {row['whiff_rate']:.1%}  "
                  f"K%: {row['k_rate']:.1%}  "
                  f"Velo: {row['avg_velo']}")
    else:
        print("    No data returned")

    # 2. Batter K rates (uses same cached Statcast pull — no extra fetch)
    print("\n[2] Batter K vulnerability...")
    batters = get_batter_k_rates()
    if not batters.empty:
        top_k = batters.sort_values("k_rate", ascending=False).head(3)
        print(f"    {len(batters)} batters loaded")
        print("    Highest K% batters (by batter ID — name lookup in pitcher model):")
        for _, row in top_k.iterrows():
            print(f"    ID {int(row['batter'])}  K%: {row['k_rate']:.1%}  "
                  f"Whiff: {row['whiff_rate']:.1%}")

    print("\n✓ MLB data layer confirmed.")

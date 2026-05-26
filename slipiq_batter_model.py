# slipiq_batter_model.py
# Batter prop projection engine
# Phase 2 — mirrors pitcher model structure
#
# PROJECTION LOGIC:
#   Base = season Statcast rate × estimated PA
#   Adjusted by:
#     - Recent form (last 10 games)
#     - Pitcher matchup (K rate, whiff rate, handedness)
#     - Park factor
#     - Lineup spot (leadoff vs cleanup)
#     - Home/away split
#
# MARKETS:
#   player_hits, player_total_bases, player_home_runs,
#   player_rbis, player_runs, player_singles, player_doubles
#
# LEAGUE BASELINES (2026 Statcast):
#   Avg hits/game:        0.89
#   Avg total bases/game: 1.42
#   Avg HR rate/game:     0.045
#   Avg RBI/game:         0.52
#   Avg runs/game:        0.51
#   Avg PA/game:          3.9

import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
import pybaseball as pyb

from slipiq_batter_lines import (
    get_batter_lines,
    get_all_batter_lines,
    build_batter_slate,
    PRIMARY_MARKETS,
)
from slipiq_parlayapi import SPORT_MLB

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

CURRENT_YEAR  = datetime.now().year
SEASON_START  = f"{CURRENT_YEAR}-03-15"
TODAY         = datetime.now().strftime("%Y-%m-%d")
LAST_14       = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

# ─────────────────────────────────────────
# LEAGUE BASELINES
# ─────────────────────────────────────────
BASELINES = {
    "player_hits":         0.89,
    "player_total_bases":  1.42,
    "player_home_runs":    0.045,
    "player_rbis":         0.52,
    "player_runs":         0.51,
    "player_singles":      0.55,
    "player_doubles":      0.18,
}

AVG_PA_PER_GAME = 3.9

# Edge thresholds (same as pitcher model)
EDGE_STRONG   = 0.25
EDGE_MODERATE = 0.12
EDGE_WEAK     = 0.06

# Minimum books to trust a line
MIN_BOOKS_TRUST = 2

# Max juice filter
MAX_JUICE = -300


# ═════════════════════════════════════════
# STATCAST BATTER DATA
# ═════════════════════════════════════════

def get_batter_statcast_season(force: bool = False) -> pd.DataFrame:
    """
    Pull batter Statcast season aggregates.
    Cached 12 hours — reuses mlb_data cache if available.
    """
    cache_key = f"batter_statcast_{CURRENT_YEAR}"
    cache_path = CACHE_DIR / f"mlb_{cache_key}.json"

    if not force and cache_path.exists():
        age = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
        if age < 12:
            print(f"  [cache] batter statcast hit ({int(age*60)} min old)")
            with open(cache_path) as f:
                return pd.DataFrame(json.load(f)["data"])

    print(f"  [fetch] Batter Statcast season {SEASON_START} → {TODAY}...")
    print(f"          (uses cached Statcast pull if available)")

    try:
        df = pyb.statcast(start_dt=SEASON_START, end_dt=TODAY)
        if df is None or df.empty:
            return pd.DataFrame()

        # Batter aggregates
        agg = (
            df[df["batter"].notna()]
            .groupby("batter")
            .agg(
                pa_total      = ("pitch_type", "count"),
                avg_exit_velo = ("launch_speed", "mean"),
                barrel_rate   = ("launch_speed_angle", lambda x:
                                 ((x >= 26) & (x <= 30)).sum() / max(len(x), 1)
                                 if hasattr(x, '__len__') else 0),
                hard_hit_rate = ("launch_speed", lambda x:
                                 (x >= 95).sum() / max(len(x), 1)),
                k_rate        = ("events", lambda x:
                                 (x == "strikeout").sum() / max(len(x), 1)),
                bb_rate       = ("events", lambda x:
                                 (x == "walk").sum() / max(len(x), 1)),
                hit_rate      = ("events", lambda x:
                                 x.isin(["single","double","triple","home_run"]).sum()
                                 / max(len(x), 1)),
                hr_rate       = ("events", lambda x:
                                 (x == "home_run").sum() / max(len(x), 1)),
                single_rate   = ("events", lambda x:
                                 (x == "single").sum() / max(len(x), 1)),
                double_rate   = ("events", lambda x:
                                 (x == "double").sum() / max(len(x), 1)),
                xba           = ("estimated_ba_using_speedangle", "mean"),
                xslg          = ("estimated_slg_using_speedangle", "mean"),
            )
            .reset_index()
        )

        agg = agg[agg["pa_total"] >= 30]
        agg = agg.round(4)

        payload = {"timestamp": datetime.utcnow().isoformat(),
                   "data": agg.to_dict(orient="records")}
        with open(cache_path, "w") as f:
            json.dump(payload, f)

        print(f"  [done] {len(agg)} batters aggregated")
        return agg

    except Exception as e:
        print(f"  [error] Batter Statcast: {e}")
        return pd.DataFrame()


def get_batter_recent_form(player_name: str, n_games: int = 10) -> dict:
    """
    Pull recent game log for a batter.
    Returns per-game averages for last N games.
    """
    safe = player_name.replace(" ", "_").replace("'", "")
    cache_path = CACHE_DIR / f"batter_form_{safe}_{CURRENT_YEAR}.json"

    if cache_path.exists():
        age = (datetime.now().timestamp() - cache_path.stat().st_mtime) / 3600
        if age < 6:
            with open(cache_path) as f:
                data = json.load(f)
            print(f"  [cache] batter form: {player_name}")
            return data

    print(f"  [fetch] Batter form: {player_name}...")

    try:
        parts  = player_name.strip().split()
        last, first = parts[-1], parts[0]
        lookup = pyb.playerid_lookup(last, first)

        if lookup.empty:
            return {"player": player_name, "error": "not_found"}

        mlbam_id = int(lookup.iloc[0]["key_mlbam"])

        log = pyb.statcast_batter(
            start_dt=LAST_14,
            end_dt=TODAY,
            player_id=mlbam_id
        )

        if log is None or log.empty:
            return {"player": player_name, "error": "no_data"}

        log["game_date"] = pd.to_datetime(log["game_date"])

        games = (
            log.groupby("game_date")
            .agg(
                hits    = ("events", lambda x:
                           x.isin(["single","double","triple","home_run"]).sum()),
                tb      = ("events", lambda x:
                           (x == "single").sum() * 1 +
                           (x == "double").sum() * 2 +
                           (x == "triple").sum() * 3 +
                           (x == "home_run").sum() * 4),
                hr      = ("events", lambda x: (x == "home_run").sum()),
                pa      = ("pitch_type", "count"),
                hard_hit = ("launch_speed", lambda x: (x >= 95).sum()),
            )
            .sort_values("game_date", ascending=False)
            .head(n_games)
        )

        avg_hits = round(games["hits"].mean(), 2)
        avg_tb   = round(games["tb"].mean(), 2)
        avg_hr   = round(games["hr"].mean(), 3)

        # Trend
        if len(games) >= 6:
            recent = games.head(3)["hits"].mean()
            prior  = games.tail(3)["hits"].mean()
            if recent > prior + 0.3:
                trend = "hot"
            elif recent < prior - 0.3:
                trend = "cold"
            else:
                trend = "flat"
        else:
            trend = "small_sample"

        result = {
            "player":    player_name,
            "mlbam_id":  mlbam_id,
            "n_games":   len(games),
            "avg_hits":  avg_hits,
            "avg_tb":    avg_tb,
            "avg_hr":    avg_hr,
            "trend":     trend,
            "hit_list":  games["hits"].tolist(),
            "tb_list":   games["tb"].tolist(),
        }

        with open(cache_path, "w") as f:
            json.dump(result, f)

        return result

    except Exception as e:
        print(f"  [error] {player_name}: {e}")
        return {"player": player_name, "error": str(e)}


# ═════════════════════════════════════════
# PROJECTION ENGINE
# ═════════════════════════════════════════

def project_batter_stat(
    player_name: str,
    market_key: str,
    season_stats: dict,
    recent_form: dict,
    park_factor: float = 1.0,
) -> dict:
    """
    Project a batter stat for today's game.

    Returns projection dict with confidence factors.
    """
    baseline = BASELINES.get(market_key, 0.5)

    # ── Season rate ──
    stat_map = {
        "player_hits":        "hit_rate",
        "player_total_bases": "xslg",
        "player_home_runs":   "hr_rate",
        "player_rbis":        "hit_rate",
        "player_runs":        "bb_rate",
        "player_singles":     "single_rate",
        "player_doubles":     "double_rate",
    }

    rate_key    = stat_map.get(market_key, "hit_rate")
    season_rate = season_stats.get(rate_key, 0)

    # Convert rate to per-game projection
    if market_key == "player_total_bases":
        # xSLG × PA gives total bases estimate
        season_proj = (season_rate or 0.380) * AVG_PA_PER_GAME
    elif market_key in ("player_hits", "player_singles",
                        "player_doubles", "player_home_runs"):
        season_proj = (season_rate or baseline / AVG_PA_PER_GAME) * AVG_PA_PER_GAME
    else:
        season_proj = baseline

    # ── Recent form ──
    form_map = {
        "player_hits":        "avg_hits",
        "player_total_bases": "avg_tb",
        "player_home_runs":   "avg_hr",
        "player_rbis":        "avg_hits",   # proxy
        "player_runs":        "avg_hits",   # proxy
        "player_singles":     "avg_hits",
        "player_doubles":     "avg_tb",
    }

    form_key    = form_map.get(market_key, "avg_hits")
    recent_val  = recent_form.get(form_key, 0)
    recent_trend = recent_form.get("trend", "flat")

    trend_mult = {
        "hot": 1.08, "flat": 1.00, "cold": 0.92,
        "small_sample": 1.00,
    }.get(recent_trend, 1.00)

    recent_proj = recent_val * trend_mult if recent_val > 0 else season_proj

    # ── Weights ──
    n_games     = recent_form.get("n_games", 0)
    pa_total    = season_stats.get("pa_total", 0)

    if pa_total >= 200 and n_games >= 8:
        w_season, w_recent = 0.55, 0.45
    elif pa_total >= 100 and n_games >= 5:
        w_season, w_recent = 0.70, 0.30
    else:
        w_season, w_recent = 0.85, 0.15

    blended    = (season_proj * w_season) + (recent_proj * w_recent)
    projection = round(blended * park_factor, 3)

    # ── Confidence factors ──
    cf = {}
    cf["sample_size"] = 85 if pa_total >= 300 else (65 if pa_total >= 100 else 40)
    cf["consistency"] = 75 if n_games >= 8 else (55 if n_games >= 5 else 35)
    cf["stuff"]       = 70  # placeholder — pitcher matchup added in Phase 3

    exit_velo = season_stats.get("avg_exit_velo", 88)
    if exit_velo >= 92:
        cf["contact_quality"] = 85
    elif exit_velo >= 89:
        cf["contact_quality"] = 65
    else:
        cf["contact_quality"] = 45

    return {
        "player":        player_name,
        "market":        market_key,
        "projection":    projection,
        "season_proj":   round(season_proj, 3),
        "recent_proj":   round(recent_proj, 3),
        "weights":       {"season": w_season, "recent": w_recent},
        "trend":         recent_trend,
        "cf":            cf,
        "pa_total":      pa_total,
        "n_games":       n_games,
        "exit_velo":     exit_velo,
    }


# ═════════════════════════════════════════
# EDGE + CONFIDENCE
# ═════════════════════════════════════════

def score_batter_edge(projection: float, line: float,
                       ev_over: float = None, ev_under: float = None) -> dict:
    if line is None:
        return {"signal": "no_line", "grade": "N/A"}

    diff      = round(projection - line, 3)
    direction = "over" if diff > 0 else "under"
    abs_diff  = abs(diff)

    if abs_diff >= EDGE_STRONG:
        strength = "strong"
    elif abs_diff >= EDGE_MODERATE:
        strength = "moderate"
    elif abs_diff >= EDGE_WEAK:
        strength = "lean"
    else:
        strength = "push"

    ev_val   = ev_over if direction == "over" else ev_under
    ev_conf  = (ev_val or 0) > 0.02

    if strength == "strong" and ev_conf:    grade = "A"
    elif strength == "strong":              grade = "B+"
    elif strength == "moderate" and ev_conf: grade = "B"
    elif strength == "moderate":            grade = "B-"
    elif strength == "lean" and ev_conf:    grade = "C+"
    elif strength == "lean":                grade = "C"
    else:                                   grade = "D"

    return {
        "direction":    direction,
        "diff":         diff,
        "abs_diff":     abs_diff,
        "strength":     strength,
        "ev_confirmed": ev_conf,
        "ev_value":     round(ev_val, 4) if ev_val else None,
        "grade":        grade,
        "signal":       f"{strength}_{direction}" if strength != "push" else "no_play",
    }


def score_batter_confidence(proj_data: dict, edge_data: dict, book_count: int) -> int:
    cf = proj_data.get("cf", {})
    base = (
        cf.get("sample_size",      50) * 0.25 +
        cf.get("consistency",      50) * 0.25 +
        cf.get("stuff",            50) * 0.20 +
        cf.get("contact_quality",  50) * 0.30
    )

    edge_bonus = {
        "A": 15, "B+": 10, "B": 7, "B-": 4,
        "C+": 2, "C": 0, "D": -10,
    }.get(edge_data.get("grade", "D"), 0)

    book_bonus  = 8 if book_count >= 6 else (4 if book_count >= 3 else -5)
    trend       = proj_data.get("trend", "flat")
    direction   = edge_data.get("direction", "")
    conflict    = (trend == "hot" and direction == "under") or \
                  (trend == "cold" and direction == "over")
    trend_bonus = 5 if not conflict and trend in ("hot","cold") else (-8 if conflict else 0)

    return max(0, min(100, round(base + edge_bonus + book_bonus + trend_bonus)))


# ═════════════════════════════════════════
# PICK CARD BUILDER
# ═════════════════════════════════════════

def build_batter_pick_card(
    player_name: str,
    market_key: str,
    prop_data: dict,
    season_stats: dict,
    recent_form: dict,
    park_factor: float = 1.0,
) -> dict | None:

    line       = prop_data.get("line")
    book_count = prop_data.get("book_count", 0)
    ev_over    = prop_data.get("ev_over")
    ev_under   = prop_data.get("ev_under")
    pinnacle   = prop_data.get("pinnacle")
    best_over  = prop_data.get("best_over")
    best_under = prop_data.get("best_under")

    if not line:
        return None

    proj  = project_batter_stat(player_name, market_key,
                                 season_stats, recent_form, park_factor)
    edge  = score_batter_edge(proj["projection"], line, ev_over, ev_under)
    conf  = score_batter_confidence(proj, edge, book_count)

    direction = edge.get("direction", "")
    best_book = None

    if direction == "over" and best_over:
        price = best_over.get("over_price", 0)
        if price >= MAX_JUICE:
            best_book = {"book": best_over.get("book_title"),
                         "price": price, "side": "over"}
    elif direction == "under" and best_under:
        price = best_under.get("under_price", 0)
        if price >= MAX_JUICE:
            best_book = {"book": best_under.get("book_title"),
                         "price": price, "side": "under"}

    flags = []
    if book_count < MIN_BOOKS_TRUST:
        flags.append(f"⚠️  thin market: {book_count} book(s)")
    if not pinnacle:
        flags.append("⚠️  no Pinnacle line")
    trend = proj.get("trend", "flat")
    if (trend == "hot" and direction == "under") or \
       (trend == "cold" and direction == "over"):
        flags.append(f"⚠️  trend conflicts: {trend}, signal {direction}")

    return {
        "player":        player_name,
        "market":        market_key,
        "game_date":     prop_data.get("game_date"),
        "home_team":     prop_data.get("home_team"),
        "away_team":     prop_data.get("away_team"),
        "line":          line,
        "projection":    proj["projection"],
        "direction":     direction,
        "diff":          edge.get("diff"),
        "grade":         edge.get("grade"),
        "signal":        edge.get("signal"),
        "confidence":    conf,
        "ev_value":      edge.get("ev_value"),
        "ev_confirmed":  edge.get("ev_confirmed"),
        "best_book":     best_book,
        "book_count":    book_count,
        "pinnacle_line": pinnacle.get("line") if pinnacle else None,
        "trend":         trend,
        "flags":         flags,
        "_internal": {
            "season_proj":  proj.get("season_proj"),
            "recent_proj":  proj.get("recent_proj"),
            "exit_velo":    proj.get("exit_velo"),
            "pa_total":     proj.get("pa_total"),
            "cf":           proj.get("cf"),
            "weights":      proj.get("weights"),
        }
    }


# ═════════════════════════════════════════
# MAIN RUNNER
# ═════════════════════════════════════════

def run_batter_model(
    sport_key: str = SPORT_MLB,
    min_confidence: int = 55,
    markets: set = None,
) -> list[dict]:
    """
    Full batter model pipeline.
    Returns sorted pick cards for confidence agent.
    """
    print("\n" + "=" * 60)
    print("SlipIQ Batter Model — Running")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M AZ')}")
    print("=" * 60)

    target_markets = markets or PRIMARY_MARKETS

    # Lines
    print("\n[1] Loading batter lines...")
    lines = get_batter_lines(sport_key, markets=target_markets)
    print(f"    {len(lines)} player/market combos")

    if not lines:
        print("    No batter lines available.")
        return []

    # Season stats
    print("\n[2] Loading Statcast season stats...")
    season_df = get_batter_statcast_season()
    season_lookup = {}
    if not season_df.empty:
        for _, row in season_df.iterrows():
            season_lookup[int(row["batter"])] = row.to_dict()
        print(f"    {len(season_lookup)} batters in Statcast")

    # Build cards
    print("\n[3] Building pick cards...")
    raw_cards = []

    for (player, market), prop_data in lines.items():
        recent_form  = get_batter_recent_form(player)
        season_stats = {}

        mlbam_id = recent_form.get("mlbam_id")
        if mlbam_id and mlbam_id in season_lookup:
            season_stats = season_lookup[mlbam_id]

        card = build_batter_pick_card(
            player_name  = player,
            market_key   = market,
            prop_data    = prop_data,
            season_stats = season_stats,
            recent_form  = recent_form,
        )

        if card and card["confidence"] >= min_confidence:
            raw_cards.append(card)

    # Deduplicate — best card per player+market
    seen = {}
    for card in raw_cards:
        key = (card["player"], card["market"])
        if key not in seen or card["confidence"] > seen[key]["confidence"]:
            seen[key] = card
    pick_cards = list(seen.values())

    # Sort
    grade_order = {"A":0,"B+":1,"B":2,"B-":3,"C+":4,"C":5,"D":6,"N/A":7}
    pick_cards.sort(key=lambda x: (
        -x.get("confidence", 0),
        grade_order.get(x.get("grade","D"), 6)
    ))

    print(f"    {len(pick_cards)} picks at confidence ≥{min_confidence}%")
    return pick_cards


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    cards = run_batter_model(min_confidence=50)

    if not cards:
        print("\nNo batter picks above threshold right now.")
    else:
        print("\n" + "=" * 60)
        print("BATTER PICK CARDS")
        print("=" * 60)

        for card in cards[:10]:
            grade   = card.get("grade","?")
            player  = card.get("player","")
            market  = card.get("market","").replace("player_","")
            line    = card.get("line")
            proj    = card.get("projection")
            dirn    = card.get("direction","").upper()
            diff    = card.get("diff",0)
            conf    = card.get("confidence",0)
            trend   = card.get("trend","")
            ev      = card.get("ev_value")
            best    = card.get("best_book")
            books   = card.get("book_count",0)

            print(f"\n  [{grade}] {player} — {market}")
            print(f"    Line: {line} | Proj: {proj} | {dirn} {diff:+.3f}")
            print(f"    Conf: {conf}% | Trend: {trend} | Books: {books}")
            if ev:
                print(f"    EV: {ev:+.1%}")
            if best:
                print(f"    ▶ {best['side'].upper()} {best['price']} @ {best['book']}")

        print(f"\n  Total: {len(cards)} picks")
        grade_ab = [c for c in cards if c.get("grade") in ("A","B+","B")]
        print(f"  Grade A/B: {len(grade_ab)}")

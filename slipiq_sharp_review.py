# slipiq_sharp_review.py
# Post-game CLV grader + Sharp Review
# Reads cache/latest_picks.json (written by slipiq_curate.py)
# Posts results to #sharp-review on Discord
#
# WHAT THIS DOES:
#   1. Load today's picks from cache
#   2. Fetch actual game results (Statcast / BallDontLie)
#   3. Grade each pick: HIT / MISS / PUSH
#   4. Calculate CLV — did we beat the closing line?
#   5. Score the model — was the projection accurate?
#   6. Post Sharp Review embed to #sharp-review
#   7. Update running record (hit rate, ROI, CLV+/-)
#
# CLV GRADING:
#   CLV = closing line vs line we got
#   Positive CLV = we got a better number than closing → sharp
#   Negative CLV = line moved against us → square
#   CLV matters more than individual hit/miss — it measures process
#
# RECORD TRACKING:
#   Stored in cache/record.json
#   Tracks: total picks, hits, misses, pushes, ROI, avg CLV
#   Resets never — cumulative all-time record

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pybaseball as pyb
import pandas as pd

from slipiq_discord import post_sharp_review, post_message
from slipiq_parlayapi import fetch_historical, SPORT_MLB, SPORT_NBA

from slipiq_env import DISCORD_SHARP_REVIEW_CHANNEL

CACHE_DIR  = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

RECORD_PATH     = CACHE_DIR / "record.json"
NBA_RECORD_PATH = CACHE_DIR / "nba_record.json"
PICKS_PATH      = CACHE_DIR / "latest_picks.json"
NBA_PICKS_PATH  = CACHE_DIR / "nba_latest_picks.json"

NBA_STAT_KEYS = {
    "points":   "pts",
    "rebounds": "reb",
    "assists":  "ast",
    "pra":      "pra",
    "threes":   "fg3m",
}


# ═════════════════════════════════════════
# RECORD MANAGER
# ═════════════════════════════════════════

def load_record(path: Path = RECORD_PATH) -> dict:
    """Load cumulative record from cache."""
    if not path.exists():
        return {
            "total":       0,
            "hits":        0,
            "misses":      0,
            "pushes":      0,
            "clv_total":   0.0,
            "roi_total":   0.0,
            "streak":      0,
            "streak_type": None,   # "W" or "L"
            "best_streak": 0,
            "last_updated": None,
            "history":     [],
        }
    with open(path) as f:
        return json.load(f)


def save_record(record: dict, path: Path = RECORD_PATH):
    """Save cumulative record to cache."""
    record["last_updated"] = datetime.now().isoformat()
    with open(path, "w") as f:
        json.dump(record, f, indent=2, default=str)


def update_record(record: dict, result: dict) -> dict:
    """
    Add one pick result to the running record.
    result dict: {player, hit, push, clv, roi, grade}
    """
    record["total"] += 1

    if result.get("push"):
        record["pushes"] += 1
        record["streak"] = 0
        record["streak_type"] = None
    elif result.get("hit"):
        record["hits"] += 1
        if record["streak_type"] == "W":
            record["streak"] += 1
        else:
            record["streak"] = 1
            record["streak_type"] = "W"
        record["best_streak"] = max(record["best_streak"], record["streak"])
    else:
        record["misses"] += 1
        if record["streak_type"] == "L":
            record["streak"] += 1
        else:
            record["streak"] = 1
            record["streak_type"] = "L"

    clv = result.get("clv", 0) or 0
    roi = result.get("roi", 0) or 0
    record["clv_total"]  += clv
    record["roi_total"]  += roi

    # Append to history (keep last 100)
    record["history"].append({
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "player":  result.get("player"),
        "line":    result.get("line"),
        "actual":  result.get("actual_ks"),
        "proj":    result.get("proj"),
        "hit":     result.get("hit"),
        "clv":     clv,
        "grade":   result.get("grade"),
    })
    record["history"] = record["history"][-100:]

    return record


def hit_rate(record: dict) -> float:
    """Win rate excluding pushes."""
    decided = record["hits"] + record["misses"]
    if decided == 0:
        return 0.0
    return round(record["hits"] / decided, 4)


def avg_clv(record: dict) -> float:
    """Average CLV per pick."""
    if record["total"] == 0:
        return 0.0
    return round(record["clv_total"] / record["total"], 4)


# ═════════════════════════════════════════
# RESULT FETCHER
# ═════════════════════════════════════════

def _extract_stat_from_propline(stats: dict, pick: dict) -> float | None:
    """Extract the relevant stat from PropLine box score for this pick."""
    player = pick.get("player", "").lower()
    market = pick.get("market", "").lower()

    players = stats.get("players", []) or stats.get("player_stats", [])
    for p in players:
        name = (p.get("name") or p.get("player", "")).lower()
        if player in name or name in player:
            if "strikeout" in market or "pitcher_k" in market:
                return p.get("strikeouts") or p.get("pitcher_strikeouts")
            elif "hit" in market:
                return p.get("hits") or p.get("batter_hits")
            elif "total_base" in market:
                return p.get("total_bases")
    return None


def _fetch_with_timeout(player_name: str, game_date: str, timeout: int = 15) -> int | None:
    import concurrent.futures

    def _do_fetch():
        # Try PropLine stats first (free, faster than pybaseball)
        try:
            from slipiq_propline import fetch_event_stats, fetch_scores
            scores = fetch_scores(sport="baseball_mlb", days_from=2)
            for game in scores:
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                pick = {"player": player_name, "market": "pitcher_strikeouts",
                        "home_team": home, "away_team": away}
                pick_team = pick.get("home_team", "") or pick.get("away_team", "")
                if pick_team and (pick_team in home or pick_team in away):
                    event_id = game.get("id")
                    stats = fetch_event_stats(event_id, sport="baseball_mlb")
                    if stats:
                        result = _extract_stat_from_propline(stats, pick)
                        if result is not None:
                            print(f"  [sharp] PropLine: {player_name} on {game_date}: {int(result)} Ks")
                            return int(result)
        except Exception as e:
            print(f"  [sharp] PropLine stats failed: {e} — falling back to pybaseball")

        try:
            # Lookup MLBAM ID
            parts  = player_name.strip().split()
            last   = parts[-1]
            first  = parts[0]
            lookup = pyb.playerid_lookup(last, first)

            if lookup.empty:
                print(f"  [sharp] Player not found: {player_name}")
                return None

            mlbam_id = int(lookup.iloc[0]["key_mlbam"])

            # Pull Statcast for that date
            log = pyb.statcast_pitcher(
                start_dt=game_date,
                end_dt=game_date,
                player_id=mlbam_id
            )

            if log is None or log.empty:
                print(f"  [sharp] No Statcast data for {player_name} on {game_date}")
                return None

            # Count strikeout events
            ks = (log["events"] == "strikeout").sum()
            print(f"  [sharp] {player_name} on {game_date}: {ks} Ks")
            return int(ks)

        except Exception as e:
            print(f"  [sharp] Error fetching {player_name}: {e}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_do_fetch)
        try:
            return future.result(timeout=timeout)
        except Exception:
            return None


def fetch_pitcher_actual_ks(player_name: str, game_date: str) -> int | None:
    """
    Fetch actual strikeout total for a pitcher on a given date.
    Source: PropLine box score (primary), Statcast via pybaseball (fallback).
    Returns K total or None if not found.
    """
    import signal
    # If pybaseball hangs, return None after 15 seconds
    try:
        result = _fetch_with_timeout(player_name, game_date, timeout=15)
        return result
    except Exception:
        return None


def fetch_nba_actual_stat(
    player_name: str,
    game_date: str,
    prop_type: str = "points",
) -> float | None:
    """Fetch actual stat total from nba_api game log for grading."""
    try:
        from slipiq_nba_data import find_player_id, get_player_game_log, current_season

        player_id = find_player_id(player_name)
        if not player_id:
            print(f"  [sharp-nba] Player not found: {player_name}")
            return None

        games = get_player_game_log(player_id, n=15, season=current_season())
        target = game_date[:10]
        for g in games:
            gd = str(g.get("game_date", ""))[:10]
            if gd != target:
                continue
            if prop_type == "pra":
                return float(g["pts"] + g["reb"] + g["ast"])
            key = NBA_STAT_KEYS.get(prop_type, "pts")
            return float(g.get(key, 0))

        print(f"  [sharp-nba] No box score for {player_name} on {game_date}")
        return None

    except Exception as e:
        print(f"  [sharp-nba] Error fetching {player_name}: {e}")
        return None


def fetch_closing_line(
    player_name: str,
    market:      str,
    game_date:   str,
    sport_key:   str = SPORT_MLB,
) -> dict | None:
    """
    Fetch Pinnacle closing line for CLV calculation.
    Uses parlayapi historical endpoint — cached per date.
    Returns American odds float or None.
    """
    market_key = market

    cache_dir  = Path("cache")
    cache_path = cache_dir / f"closing_{game_date.replace('-','')}.json"

    closing_cache = {}
    if cache_path.exists():
        try:
            closing_cache = json.loads(cache_path.read_text())
        except Exception:
            pass

    cache_key = f"{player_name.lower().strip()}_{market_key}"
    if cache_key in closing_cache:
        return closing_cache[cache_key]

    try:
        from slipiq_parlayapi import fetch_historical_props as fetch_historical
        historical = fetch_historical(sport_key, date=game_date)
        if not historical:
            closing_cache[cache_key] = None
            cache_path.write_text(json.dumps(closing_cache))
            return None

        player_norm = player_name.lower().strip()
        result_val  = None

        for prop in historical:
            prop_player = (prop.get("player") or "").lower().strip()
            prop_market = (prop.get("market_key") or "").lower()

            if prop_player != player_norm:
                continue
            if market_key and market_key not in prop_market and prop_market not in market_key:
                continue

            pinnacle = prop.get("pinnacle")
            if pinnacle and pinnacle.get("over_price"):
                result_val = float(pinnacle["over_price"])
                break

            for book_entry in (prop.get("bookmakers") or []):
                if "pinnacle" not in (book_entry.get("key") or "").lower():
                    continue
                for mkt in (book_entry.get("markets") or []):
                    for outcome in (mkt.get("outcomes") or []):
                        if "over" in (outcome.get("name") or "").lower():
                            result_val = float(outcome.get("price", 0)) or None
                            break

        closing_cache[cache_key] = result_val
        cache_path.write_text(json.dumps(closing_cache, indent=2))
        return result_val

    except Exception as e:
        print(f"  [sharp] fetch_closing_line error for {player_name}: {e}")
        return None


# ═════════════════════════════════════════
# GRADER
# ═════════════════════════════════════════

def grade_pick(card: dict, actual_ks: int, closing_line: float = None) -> dict:
    """
    Grade a single pick.
    Returns result dict with hit/miss/push, CLV, model accuracy.
    """
    player    = card.get("player")
    line      = card.get("line")
    direction = card.get("direction", "")
    proj      = card.get("projection")
    internal = card.get("_internal") or {}
    pick_line = internal.get("pinnacle_over") or (
        card.get("best_book", {}).get("price") if card.get("best_book") else None
    )
    grade     = card.get("grade", "?")

    # Hit/miss/push
    if direction == "over":
        if actual_ks > line:
            outcome = "HIT"
            hit, push = True, False
        elif actual_ks == line:
            outcome = "PUSH"
            hit, push = False, True
        else:
            outcome = "MISS"
            hit, push = False, False
    else:  # under
        if actual_ks < line:
            outcome = "HIT"
            hit, push = True, False
        elif actual_ks == line:
            outcome = "PUSH"
            hit, push = False, True
        else:
            outcome = "MISS"
            hit, push = False, False

    # Model accuracy — how far off was the projection?
    proj_error = round(abs(actual_ks - proj), 2) if proj else None
    proj_tag   = None
    if proj_error is not None:
        if proj_error <= 0.5:
            proj_tag = "🎯 Sharp"
        elif proj_error <= 1.5:
            proj_tag = "✅ Close"
        elif proj_error <= 3.0:
            proj_tag = "⚠️ Off"
        else:
            proj_tag = "❌ Miss"

    # CLV calculation
    clv = None
    if closing_line is not None and line is not None:
        if direction == "over":
            clv = round(line - closing_line, 2)   # positive = got lower line = good
        else:
            clv = round(closing_line - line, 2)   # positive = got higher line = good

    # Sharp Review grade
    if outcome == "HIT" and clv and clv > 0:
        sr_grade = "A"   # hit AND beat closing
    elif outcome == "HIT":
        sr_grade = "B"   # hit but no CLV data
    elif outcome == "PUSH":
        sr_grade = "C"
    elif outcome == "MISS" and clv and clv > 0:
        sr_grade = "B-"  # miss but beat closing — good process, bad result
    else:
        sr_grade = "D"

    return {
        "player":      player,
        "line":        line,
        "direction":   direction,
        "actual_ks":   actual_ks,
        "proj":        proj,
        "outcome":     outcome,
        "hit":         hit,
        "push":        push,
        "clv":         clv,
        "proj_error":  proj_error,
        "proj_tag":    proj_tag,
        "sr_grade":    sr_grade,
        "model_grade": grade,
        "roi":         1.0 if hit else (-1.0 if not push else 0.0),
    }


def grade_nba_pick(card: dict, actual: float, closing_line: float = None) -> dict:
    """Grade NBA prop pick (points/reb/ast/PRA)."""
    player = card.get("player")
    line = card.get("line")
    direction = card.get("direction", "")
    proj = card.get("projection")
    prop_type = card.get("prop_type", "points")
    grade = card.get("grade", "?")

    if direction == "over":
        if actual > line:
            outcome, hit, push = "HIT", True, False
        elif actual == line:
            outcome, hit, push = "PUSH", False, True
        else:
            outcome, hit, push = "MISS", False, False
    else:
        if actual < line:
            outcome, hit, push = "HIT", True, False
        elif actual == line:
            outcome, hit, push = "PUSH", False, True
        else:
            outcome, hit, push = "MISS", False, False

    proj_error = round(abs(actual - proj), 2) if proj else None
    clv = None
    if closing_line is not None and line is not None:
        if direction == "over":
            clv = round(line - closing_line, 2)
        else:
            clv = round(closing_line - line, 2)

    if outcome == "HIT" and clv and clv > 0:
        sr_grade = "A"
    elif outcome == "HIT":
        sr_grade = "B"
    elif outcome == "PUSH":
        sr_grade = "C"
    elif outcome == "MISS" and clv and clv > 0:
        sr_grade = "B-"
    else:
        sr_grade = "D"

    stat_label = prop_type.upper()
    return {
        "sport":       "nba",
        "player":      player,
        "line":        line,
        "direction":   direction,
        "prop_type":   prop_type,
        "actual_stat": actual,
        "actual_ks":   actual,  # compat with post_sharp_review embed
        "proj":        proj,
        "outcome":     outcome,
        "hit":         hit,
        "push":        push,
        "clv":         clv,
        "proj_error":  proj_error,
        "sr_grade":    sr_grade,
        "model_grade": grade,
        "roi":         1.0 if hit else (-1.0 if not push else 0.0),
        "stat_label":  stat_label,
    }


# ═════════════════════════════════════════
# SHARP REVIEW RUNNER
# ═════════════════════════════════════════

def run_nba_sharp_review(game_date: str = None, post_to_discord: bool = True) -> list[dict]:
    """Grade NBA picks from cache/nba_latest_picks.json."""
    if not game_date:
        game_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print("\n" + "=" * 60)
    print("SlipIQ NBA Sharp Review")
    print(f"Grading: {game_date}")
    print("=" * 60)

    date_log = CACHE_DIR / f"nba_slate_{game_date.replace('-', '')}.json"
    if date_log.exists():
        with open(date_log) as f:
            slate_log = json.load(f)
    elif NBA_PICKS_PATH.exists():
        with open(NBA_PICKS_PATH) as f:
            slate_log = json.load(f)
    else:
        print("  No NBA picks found to grade.")
        return []

    top_picks = slate_log.get("top_picks", [])
    if not top_picks:
        print("  No NBA picks were posted.")
        return []

    record = load_record(NBA_RECORD_PATH)
    results = []

    for card in top_picks:
        player = card.get("player")
        prop_type = card.get("prop_type", "points")
        market_key = card.get("market_key", f"player_{prop_type}")
        print(f"\n  🏀 [{card.get('grade')}] {player} — {card.get('prop_label', prop_type)}")

        actual = fetch_nba_actual_stat(player, game_date, prop_type)
        if actual is None:
            continue

        closing_line = fetch_closing_line(player, market_key, game_date, sport_key=SPORT_NBA)
        closing_line_val = closing_line.get("line") if closing_line else None
        result = grade_nba_pick(card, actual, closing_line_val)
        results.append(result)
        record = update_record(record, result)
        print(f"  Result : {result['outcome']} ({actual} vs {card.get('line')} line)")

        if post_to_discord and DISCORD_SHARP_REVIEW_CHANNEL:
            nba_direction = card.get("direction", "over")
            nba_closing_price = None
            if closing_line:
                nba_closing_price = (
                    closing_line.get("over_price") if nba_direction == "over"
                    else closing_line.get("under_price")
                )
            post_message(
                DISCORD_SHARP_REVIEW_CHANNEL,
                content=f"🏀 **NBA Sharp Review** — {player}",
            )
            post_sharp_review(
                player=player,
                pick_direction=nba_direction,
                line=card.get("line"),
                actual_ks=int(actual) if actual == int(actual) else actual,
                proj=card.get("projection"),
                grade=result["sr_grade"],
                clv=result.get("clv"),
                book=(card.get("best_book") or {}).get("book"),
                book_price=(card.get("best_book") or {}).get("price"),
                closing_price=nba_closing_price,
            )

    save_record(record, NBA_RECORD_PATH)
    if results:
        _print_summary(results, record)
    return results


def _find_game_for_pick(pick: dict, scores: list) -> dict | None:
    home = (pick.get("home_team") or "").lower()
    away = (pick.get("away_team") or "").lower()
    for game in scores:
        gh = game.get("home_team", "").lower()
        ga = game.get("away_team", "").lower()
        if home in gh or away in ga or gh in home or ga in away:
            return game
    return None


def _extract_stat(stats: dict, player: str, market: str):
    player_lower = player.lower()
    players = stats.get("players") or stats.get("player_stats") or []
    for p in players:
        name = (p.get("name") or p.get("player") or "").lower()
        if player_lower in name or name in player_lower:
            if "strikeout" in market:
                return p.get("strikeouts") or p.get("pitcher_strikeouts")
            elif "hit" in market:
                return p.get("hits") or p.get("batter_hits")
            elif "total_base" in market:
                return p.get("total_bases")
            elif "earned_run" in market:
                return p.get("earned_runs")
            elif "out" in market:
                return p.get("outs_recorded")
    return None


def settle_todays_picks() -> list[dict]:
    """
    Auto-settle today's picks using PropLine box scores.
    Called at 23:00 AZ nightly.
    """
    from pathlib import Path
    import json
    from datetime import date

    settled = []

    # Load today's picks from cache
    picks_path = Path("cache/latest_picks.json")
    if not picks_path.exists():
        print("  [review] No picks to settle — cache/latest_picks.json missing")
        return []

    with open(picks_path) as f:
        data = json.load(f)

    picks = data.get("top_picks", [])
    if not picks:
        print("  [review] No picks in latest_picks.json")
        return []

    print(f"  [review] Settling {len(picks)} picks...")

    # Get today's scores from PropLine
    try:
        from slipiq_propline import fetch_scores
        scores = fetch_scores(sport="baseball_mlb", days_from=1)
        print(f"  [review] Loaded {len(scores)} game scores")
    except Exception as e:
        print(f"  [review] PropLine scores failed: {e}")
        scores = []

    for pick in picks:
        player = pick.get("player", "")
        market = pick.get("market", "pitcher_strikeouts")
        line = pick.get("line") or pick.get("pp_line")
        direction = pick.get("direction", "over").lower()

        if not player or not line:
            continue

        # Find the game for this pick
        game = _find_game(pick, scores)
        if not game:
            print(f"  [review] Game not found for {player}")
            pick["result"] = "PENDING"
            settled.append(pick)
            continue

        # Get box score
        event_id = game.get("id")
        actual = _get_actual_stat(event_id, player, market)

        if actual is None:
            print(f"  [review] No stat found for {player}")
            pick["result"] = "PENDING"
            settled.append(pick)
            continue

        # Grade it
        if direction == "over":
            result = "WIN" if actual > line else "LOSS"
        else:
            result = "WIN" if actual < line else "LOSS"

        pick["actual"] = actual
        pick["result"] = result
        pick["settled_at"] = str(date.today())

        emoji = "✅" if result == "WIN" else "❌"
        print(f"  [review] {emoji} {player}: actual={actual} "
              f"{'>' if direction=='over' else '<'} {line} → {result}")
        settled.append(pick)

    # Save results
    results_path = Path("cache/record.json")
    _update_record(settled, results_path)

    return settled


def _find_game(pick: dict, scores: list) -> dict | None:
    home = (pick.get("home_team") or "").lower()
    away = (pick.get("away_team") or "").lower()
    for game in scores:
        gh = game.get("home_team", "").lower()
        ga = game.get("away_team", "").lower()
        if (home and (home in gh or gh in home)) or \
           (away and (away in ga or ga in away)):
            return game
    return None


def _get_actual_stat(event_id: str, player: str,
                      market: str) -> float | None:
    if not event_id:
        return None
    try:
        from slipiq_propline import fetch_event_stats
        stats = fetch_event_stats(event_id, sport="baseball_mlb")
        if not stats:
            return None
        player_lower = player.lower()
        players = stats.get("players") or stats.get("player_stats") or []
        for p in players:
            name = (p.get("name") or p.get("player") or "").lower()
            if player_lower in name or name in player_lower:
                if "strikeout" in market:
                    return p.get("strikeouts") or p.get("pitcher_strikeouts")
                elif "hit" in market:
                    return p.get("hits")
                elif "total_base" in market:
                    return p.get("total_bases")
                elif "earned_run" in market:
                    return p.get("earned_runs")
    except Exception as e:
        print(f"  [review] Stats fetch failed: {e}")
    return None


def _update_record(settled: list, record_path) -> dict:
    import json
    from pathlib import Path

    record = {}
    if record_path.exists():
        with open(record_path) as f:
            record = json.load(f)

    wins = record.get("hits", 0)
    losses = record.get("misses", 0)

    for pick in settled:
        result = pick.get("result")
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1

    total = wins + losses
    hit_rate = round(wins / total * 100, 1) if total > 0 else 0.0

    record["hits"] = wins
    record["misses"] = losses
    record["total"] = total
    record["hit_rate"] = hit_rate
    record["last_updated"] = str(__import__("datetime").date.today())

    with open(record_path, "w") as f:
        json.dump(record, f, indent=2)

    print(f"  [record] All-time: {wins}W {losses}L ({hit_rate}%)")
    return record


def settle_picks_from_propline() -> int | None:
    """
    Auto-grade today's picks using PropLine box scores.
    Called at 11pm AZ after games end.
    """
    from slipiq_propline import fetch_scores, fetch_event_stats
    from slipiq_db import load_todays_picks, update_pick_result

    picks = load_todays_picks()
    if not picks:
        print("  [review] No picks to settle")
        return

    scores = fetch_scores(sport="baseball_mlb", days_from=1)
    settled = 0

    for pick in picks:
        player    = pick.get("player", "")
        market    = pick.get("market", "").lower()
        line      = pick.get("line") or pick.get("pp_line")
        direction = pick.get("direction", "over")

        game = _find_game_for_pick(pick, scores)
        if not game:
            continue

        event_id = game.get("id")
        stats = fetch_event_stats(event_id, sport="baseball_mlb")
        if not stats:
            continue

        actual = _extract_stat(stats, player, market)
        if actual is None:
            continue

        if direction == "over":
            result = "WIN" if actual > line else "LOSS"
        else:
            result = "WIN" if actual < line else "LOSS"

        pick["actual"]  = actual
        pick["result"]  = result
        pick["settled"] = True

        update_pick_result(pick)
        settled += 1
        print(f"  [review] {player}: actual={actual} line={line} → {result}")

    print(f"  [review] Settled {settled}/{len(picks)} picks")
    return [p for p in picks if p.get("settled")]


def post_results_to_discord(settled_picks: list) -> None:
    """Post yesterday's results to Discord after settlement."""
    if not settled_picks:
        return

    wins   = [p for p in settled_picks if p.get("result") == "WIN"]
    losses = [p for p in settled_picks if p.get("result") == "LOSS"]
    record = f"{len(wins)}-{len(losses)}"

    lines = []
    for p in settled_picks:
        emoji     = "✅" if p.get("result") == "WIN" else "❌"
        player    = p.get("player", "")
        direction = p.get("direction", "over").upper()
        line      = p.get("pp_line") or p.get("line")
        actual    = p.get("actual", "?")
        market    = (
            p.get("market", "")
            .replace("pitcher_", "")
            .replace("_", " ")
            .title()
        )
        lines.append(
            f"{emoji} {player} {direction} {line} {market} "
            f"| Actual: {actual}"
        )

    win_rate = round(len(wins) / len(settled_picks) * 100) if settled_picks else 0

    message = (
        f"📊 **SlipIQ Results — {date.today().strftime('%B %d')}**\n\n"
        f"**Record: {record} ({win_rate}%)**\n\n"
        + "\n".join(lines)
        + "\n\n_SlipIQ · Model-driven. Sharp-anchored. Always improving._"
    )

    from slipiq_env import DISCORD_RESULTS_CHANNEL
    post_message(DISCORD_RESULTS_CHANNEL, message)
    print(f"  [results] Posted {record} to Discord")


def run_sharp_review(game_date: str = None, post_to_discord: bool = True, sport: str = "mlb") -> list[dict]:
    """
    Full Sharp Review pipeline for a given date.
    Defaults to yesterday's picks. sport='nba' grades basketball only.
    """
    if sport == "nba":
        return run_nba_sharp_review(game_date=game_date, post_to_discord=post_to_discord)

    # Auto-settle today's picks via PropLine box scores before grading
    try:
        settled_picks = settle_picks_from_propline() or []
        post_results_to_discord(settled_picks)
    except Exception as e:
        print(f"  [review] PropLine settle skipped: {e}")

    if not game_date:
        game_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print("\n" + "=" * 60)
    print("SlipIQ Sharp Review")
    print(f"Grading: {game_date}")
    print("=" * 60)

    # Load picks
    date_log = CACHE_DIR / f"slate_{game_date}.json"
    if date_log.exists():
        with open(date_log) as f:
            slate_log = json.load(f)
    elif PICKS_PATH.exists():
        with open(PICKS_PATH) as f:
            slate_log = json.load(f)
    else:
        print("  No picks found to grade.")
        return []

    top_picks = slate_log.get("top_picks", [])
    if not top_picks:
        print("  No picks were posted today.")
        return []

    print(f"\n  Grading {len(top_picks)} picks from {game_date}...")

    # Load record
    record  = load_record()
    results = []

    for card in top_picks:
        player    = card.get("player")
        line      = card.get("line")
        direction = card.get("direction")
        proj      = card.get("projection")
        best_book = card.get("best_book", {}) or {}
        book_name = best_book.get("book")
        book_price = best_book.get("price")

        print(f"\n  [{card.get('grade')}] {player} — "
              f"{direction.upper() if direction else '?'} {line}")

        # Fetch actual result
        actual_ks = fetch_pitcher_actual_ks(player, game_date)

        if actual_ks is None:
            # Statcast has 24-hour delay — results from today won't be
            # available until tomorrow morning
            from datetime import datetime, date
            today = date.today().isoformat()
            if game_date >= today:
                print(f"  [sharp] {player} — game result not yet in Statcast "
                      f"(24h delay). Will grade tomorrow.")
            else:
                print(f"  [sharp] {player} — no result found for {game_date}")
            continue

        # Fetch closing line for CLV
        closing_line = fetch_closing_line(player, "player_pitcher_strikeouts", game_date)

        # Grade it — pass the prop line value for CLV diff calculation
        closing_line_val = closing_line.get("line") if closing_line else None
        result = grade_pick(card, actual_ks, closing_line_val)
        results.append(result)

        # Log to calibration tracker with CLV
        try:
            from slipiq_calibration import log_result_by_player
            from slipiq_ev_engine import closing_line_value

            clv_pct = None
            if closing_line and card.get("best_book"):
                bet_price     = (card.get("best_book") or {}).get("price")
                closing_price_log = (
                    closing_line.get("over_price")  if direction == "over"
                    else closing_line.get("under_price")
                ) if closing_line else None

                if bet_price and closing_price_log:
                    clv_result = closing_line_value(bet_price, closing_price_log)
                    clv_pct    = clv_result["clv_pct"]

            log_result_by_player(
                player     = player,
                market     = card.get("market", "player_pitcher_strikeouts"),
                direction  = direction,
                game_date  = game_date,
                result     = result["outcome"],
                actual_val = actual_ks,
                clv        = clv_pct,
            )
        except Exception as e:
            print(f"  [calibration] log error: {e}")

        # Update record
        record = update_record(record, result)

        # Persist result to slipiq_results (Supabase + JSON)
        try:
            from slipiq_results import update_result as persist_result
            persist_result(
                pitcher   = player,
                pick_date = game_date,
                result    = result["outcome"],
                extra_fields = {
                    "actual_ks":  actual_ks,
                    "proj":       proj,
                    "clv":        result.get("clv"),
                    "sr_grade":   result.get("sr_grade"),
                    "proj_error": result.get("proj_error"),
                },
            )
        except Exception as e:
            print(f"  [sharp] persist error: {e}")

        print(f"  Result : {result['outcome']} "
              f"({actual_ks} Ks vs {line} line)")
        print(f"  Proj   : {proj} → error {result['proj_error']} {result['proj_tag']}")
        if result["clv"] is not None:
            print(f"  CLV    : {result['clv']:+.2f}")

        # Log CLV to calibration tracker
        try:
            from slipiq_calibration import log_result_by_player
            from slipiq_ev_engine import closing_line_value
            clv_pct = None
            if closing_line and card.get("best_book"):
                bet_price = (card.get("best_book") or {}).get("price")
                if bet_price and closing_line:
                    clv_result = closing_line_value(int(bet_price), int(closing_line))
                    clv_pct    = clv_result.get("clv_pct")
            log_result_by_player(
                player     = player,
                market     = "player_pitcher_strikeouts",
                direction  = direction or "over",
                game_date  = game_date,
                result     = result["outcome"],
                actual_val = float(actual_ks) if actual_ks is not None else None,
                clv        = clv_pct,
            )
        except Exception as e:
            print(f"  [calibration] log error: {e}")

        # Post to Discord
        if post_to_discord:
            closing_price_discord = None
            if closing_line:
                closing_price_discord = (
                    closing_line.get("over_price") if direction == "over"
                    else closing_line.get("under_price")
                )
            post_sharp_review(
                player        = player,
                pick_direction = direction,
                line          = line,
                actual_ks     = actual_ks,
                proj          = proj,
                grade         = result["sr_grade"],
                clv           = result["clv"],
                book          = book_name,
                book_price    = book_price,
                closing_price = closing_price_discord,
            )

    # Save updated record
    save_record(record)

    # Summary
    _print_summary(results, record)

    # Post summary to Discord
    if post_to_discord and results:
        _post_summary_to_discord(results, record, game_date)

    return results


def run_all_sharp_reviews(game_date: str = None, post_to_discord: bool = True) -> dict:
    """Grade both MLB and NBA picks for the given date."""
    mlb = run_sharp_review(game_date=game_date, post_to_discord=post_to_discord, sport="mlb")
    try:
        nba_results = run_nba_sharp_review(game_date=game_date, post_to_discord=post_to_discord)
    except Exception as e:
        print(f"  [review] NBA review skipped: {e}")
        nba_results = []
    return {"mlb": mlb, "nba": nba_results}


# ═════════════════════════════════════════
# SUMMARY BUILDERS
# ═════════════════════════════════════════

def _print_summary(results: list[dict], record: dict):
    hits   = sum(1 for r in results if r["hit"])
    misses = sum(1 for r in results if not r["hit"] and not r["push"])
    pushes = sum(1 for r in results if r["push"])
    clv_avg = (
        sum(r["clv"] for r in results if r["clv"] is not None) /
        max(len([r for r in results if r["clv"] is not None]), 1)
    )

    print("\n" + "=" * 60)
    print("SHARP REVIEW SUMMARY")
    print("=" * 60)
    print(f"  Today    : {hits}W {misses}L {pushes}P")
    print(f"  Avg CLV  : {clv_avg:+.2f}")
    print(f"\n  All-time record:")
    print(f"  {record['hits']}W {record['misses']}L {record['pushes']}P")
    print(f"  Hit rate : {hit_rate(record):.1%}")
    print(f"  Avg CLV  : {avg_clv(record):+.4f}")
    print(f"  Streak   : {record['streak']} {record.get('streak_type', '')}")


def _post_summary_to_discord(results: list[dict], record: dict, game_date: str):
    """Post daily summary card to #sharp-review."""
    if not DISCORD_SHARP_REVIEW_CHANNEL:
        return

    hits   = sum(1 for r in results if r["hit"])
    misses = sum(1 for r in results if not r["hit"] and not r["push"])
    pushes = sum(1 for r in results if r["push"])
    clvs   = [r["clv"] for r in results if r["clv"] is not None]
    clv_avg = sum(clvs) / len(clvs) if clvs else None

    streak = record.get("streak", 0)
    stype  = record.get("streak_type", "")
    streak_str = f"{streak}{stype}" if streak > 0 else "—"

    clv_str = f"{clv_avg:+.2f}" if clv_avg is not None else "N/A"

    embed = {
        "title":       f"🔍 Sharp Review — {game_date}",
        "description": f"**{hits}W {misses}L {pushes}P** today | CLV avg: {clv_str}",
        "color":       0x00FF88 if hits >= misses else 0xFF2200,
        "fields": [
            {
                "name":   "All-Time Record",
                "value":  f"{record['hits']}W {record['misses']}L {record['pushes']}P",
                "inline": True,
            },
            {
                "name":   "Hit Rate",
                "value":  f"{hit_rate(record):.1%}",
                "inline": True,
            },
            {
                "name":   "Avg CLV",
                "value":  f"{avg_clv(record):+.4f}",
                "inline": True,
            },
            {
                "name":   "Current Streak",
                "value":  streak_str,
                "inline": True,
            },
        ],
        "footer":    {"text": "SlipIQ Sharp Review • Process over results."},
        "timestamp": datetime.utcnow().isoformat(),
    }

    post_message(DISCORD_SHARP_REVIEW_CHANNEL, embed=embed)

    # Post to team-parlay as day-complete operator summary
    try:
        from slipiq_env import CHANNEL_TEAM_PARLAY
        from slipiq_discord import post_message
        from slipiq_slate_clock import SlateClock

        tomorrow_clock = SlateClock()
        tomorrow_clock.get_fire_windows(force_refresh=True)
        tomorrow_summary = tomorrow_clock.slate_summary()

        wins   = sum(1 for r in results if r.get("hit"))
        losses = sum(1 for r in results if not r.get("hit") and not r.get("push"))
        hr     = round(wins / max(wins + losses, 1) * 100, 1)
        clv_vals = [r.get("clv") for r in results if r.get("clv") is not None]
        avg_clv  = round(sum(clv_vals) / len(clv_vals), 2) if clv_vals else None
        clv_str  = f" | Avg CLV {avg_clv:+.2f}" if avg_clv is not None else ""

        content = (
            f"📊 **Day Complete — {game_date}**\n"
            f"Results: **{wins}W {losses}L** ({hr}%){clv_str}\n"
            f"Tomorrow: {tomorrow_summary}"
        )
        if CHANNEL_TEAM_PARLAY:
            post_message(CHANNEL_TEAM_PARLAY, content=content)
    except Exception as e:
        print(f"  [sharp] day summary post error: {e}")


# ═════════════════════════════════════════
# TEST
# ═════════════════════════════════════════

if __name__ == "__main__":
    import sys

    date_arg = next((a for a in sys.argv[1:] if a.startswith("20")), None)
    no_discord = "--no-discord" in sys.argv
    sport = "nba" if "--sport" in sys.argv and "nba" in sys.argv else (
        "nba" if sys.argv[-1] == "nba" else "mlb"
    )
    if "--sport" in sys.argv:
        idx = sys.argv.index("--sport")
        if idx + 1 < len(sys.argv):
            sport = sys.argv[idx + 1].lower()

    if sport == "all":
        results = run_all_sharp_reviews(game_date=date_arg, post_to_discord=not no_discord)
    else:
        results = run_sharp_review(
            game_date=date_arg,
            post_to_discord=not no_discord,
            sport=sport,
        )

    if not results:
        print("\n  No results to display.")
        print("  Either no picks were posted, or game data isn't available yet.")
        print("  Try running after 11pm when all games are final.")

# slipiq_prizepicks.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — PrizePicks EV Engine + Intraday Scanner
#
# PrizePicks math is FUNDAMENTALLY DIFFERENT from sportsbooks:
#   - No American odds — fixed multiplier per entry size
#   - Power Play: ALL legs must win
#   - Flex Play:  partial wins allowed (see PP_FLEX_PAYOUT)
#   - Legs are INDEPENDENT — never build same-game PrizePicks entries
#   - Breakeven per-leg threshold varies by entry size
#
# INTRADAY STRATEGY (MLB):
#   - Baseball has rolling lock times (1pm, 4pm, 7pm, 10pm ET waves)
#   - Bot scans for +EV legs every 20 min via slipiq_propline.py
#   - Assembles entries just-in-time before earliest lock in the combo
#   - Targets 4-pick Power Play (10x) as primary — highest ROI tier
#   - Posts to DISCORD_PRIZEPICKS_CHANNEL when entry is ready
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import os

load_dotenv()

from slipiq_ev_engine import (
    pp_entry_ev,
    pp_best_mode,
    prizepicks_leg_threshold,
    PP_POWER_PAYOUT,
    PP_FLEX_PAYOUT,
    MIN_EDGE_PRIZEPICKS,
    no_vig_prob,
    kelly_stake,
    american_to_decimal,
)

CACHE_DIR  = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
PP_QUEUE_FILE = CACHE_DIR / "pp_eligible_queue.json"

# ─── Config ───────────────────────────────────────────────────
# Minutes before lock time to stop adding a leg to new entries
LOCK_BUFFER_MINUTES = 10

# Preferred entry sizes (priority order)
TARGET_PICKS_ORDER = [4, 3, 5, 2]

# Min per-leg edge buffer above theoretical breakeven
# e.g. threshold 0.5623 + 0.02 buffer = 0.5823 required
PER_LEG_EDGE_BUFFER = 0.02

# Maximum legs per game in any single PP entry (enforce independence)
MAX_SAME_GAME_LEGS = 1

# Markets eligible for PrizePicks (MLB)
PP_ELIGIBLE_MARKETS_MLB = {
    "player_pitcher_strikeouts",
    "player_strike_outs",
    "player_hits",
    "player_total_bases",
    "player_home_runs",
    "player_rbis",
    "player_runs",
    "player_hitter_strikeouts",
}

# Markets eligible for PrizePicks (NBA)
PP_ELIGIBLE_MARKETS_NBA = {
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_points_rebounds_assists",
    "player_threes",
    "player_steals",
    "player_blocks",
}


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — LEG ELIGIBILITY
# ═══════════════════════════════════════════════════════════════

def leg_is_eligible(
    true_prob:    float,
    n_picks:      int,
    flex:         bool = False,
    buffer:       float = PER_LEG_EDGE_BUFFER,
) -> bool:
    """
    Is a single leg eligible for a PrizePicks entry of n_picks?
    True if true_prob > (breakeven threshold + buffer).

    Args:
        true_prob : calibrated model probability for the leg's direction
        n_picks   : size of the entry this leg would go into
        flex      : True = Flex Play math
        buffer    : extra margin above theoretical breakeven
    """
    threshold = prizepicks_leg_threshold(n_picks, flex=flex)
    return true_prob >= threshold + buffer


def scan_eligible_legs(
    aggregated_props: dict,   # output of aggregate_propline_by_player()
    model_probs:      dict,   # {(player_lower, market, direction): true_prob}
    target_n:         int     = 4,
    flex:             bool    = False,
    eligible_markets: set     = None,
) -> list[dict]:
    """
    Scan all props and return legs that meet PrizePicks +EV threshold
    for a target_n-pick entry.

    Args:
        aggregated_props : from slipiq_propline.aggregate_propline_by_player()
        model_probs      : model-derived true probabilities per (player, market, dir)
        target_n         : target entry size to filter for (default 4)
        flex             : Flex Play mode
        eligible_markets : set of market keys to consider (default MLB)

    Returns:
        List of eligible leg dicts sorted by true_prob descending.
    """
    if eligible_markets is None:
        eligible_markets = PP_ELIGIBLE_MARKETS_MLB

    eligible = []
    threshold = prizepicks_leg_threshold(target_n, flex=flex) + PER_LEG_EDGE_BUFFER

    for (player_key, market), data in aggregated_props.items():
        if market not in eligible_markets:
            continue

        line = data.get("line_consensus") or data.get("sharp_line")
        if line is None:
            continue

        for direction in ("over", "under"):
            prob_key = (player_key, market, direction)
            true_prob = model_probs.get(prob_key)

            if true_prob is None:
                # Fall back to Pinnacle no-vig if no model prob
                pin = data.get("pinnacle")
                if pin and pin.get("over") and pin.get("under"):
                    nv = no_vig_prob(pin["over"], pin["under"])
                    true_prob = nv["true_over"] if direction == "over" else nv["true_under"]

            if true_prob is None or true_prob < threshold:
                continue

            # Find lock time from first entry
            entries  = data.get("_entries") or []
            lock_str = entries[0].get("commence_time") if entries else None
            game_id  = entries[0].get("game_id", "") if entries else ""

            eligible.append({
                "player":        data["player"],
                "player_key":    player_key,
                "market":        market,
                "direction":     direction,
                "line":          line,
                "true_prob":     round(true_prob, 6),
                "lock_time":     lock_str,
                "game_id":       game_id,
                "home_team":     entries[0].get("home_team", "") if entries else "",
                "away_team":     entries[0].get("away_team", "") if entries else "",
                "ev_over":       data.get("ev_over"),
                "ev_under":      data.get("ev_under"),
                "book_count":    data.get("book_count", 0),
                "_threshold":    round(threshold, 6),
            })

    return sorted(eligible, key=lambda x: x["true_prob"], reverse=True)


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — ENTRY BUILDER
# ═══════════════════════════════════════════════════════════════

def _no_same_game_conflict(combo: tuple[dict, ...]) -> bool:
    """Ensure no more than MAX_SAME_GAME_LEGS from the same game."""
    game_counts: dict[str, int] = {}
    for leg in combo:
        gid = leg.get("game_id") or f"{leg.get('home_team')}_{leg.get('away_team')}"
        game_counts[gid] = game_counts.get(gid, 0) + 1
        if game_counts[gid] > MAX_SAME_GAME_LEGS:
            return False
    return True


def _minutes_to_lock(lock_time_str: str | None) -> float:
    """Minutes until game lock. Returns 9999 if unknown."""
    if not lock_time_str:
        return 9999.0
    try:
        lock = datetime.fromisoformat(lock_time_str.replace("Z", "+00:00"))
        now  = datetime.now(timezone.utc)
        return (lock - now).total_seconds() / 60.0
    except (ValueError, TypeError):
        return 9999.0


def build_pp_entry(
    eligible_legs: list[dict],
    target_picks:  int  = 4,
    flex:          bool = False,
    min_ev:        float = MIN_EDGE_PRIZEPICKS,
) -> dict | None:
    """
    Build the highest-EV PrizePicks entry from eligible legs.

    Strategy:
    1. Filter legs where lock time > LOCK_BUFFER_MINUTES away
    2. Try all combinations of target_picks legs
    3. Enforce MAX_SAME_GAME_LEGS per entry
    4. Calculate full entry EV via pp_entry_ev()
    5. Return the combination with highest EV that passes min_ev

    Args:
        eligible_legs : output of scan_eligible_legs()
        target_picks  : entry size to build
        flex          : Flex Play mode
        min_ev        : minimum entry EV to accept

    Returns:
        Entry dict or None if no +EV entry found
    """
    # Filter out legs too close to lock
    open_legs = [
        leg for leg in eligible_legs
        if _minutes_to_lock(leg.get("lock_time")) > LOCK_BUFFER_MINUTES
    ]

    if len(open_legs) < target_picks:
        return None

    best_ev    = min_ev - 1e-9  # start below threshold
    best_combo = None

    for combo in combinations(open_legs, target_picks):
        if not _no_same_game_conflict(combo):
            continue

        probs = [leg["true_prob"] for leg in combo]
        result = pp_entry_ev(probs, n_picks=target_picks, flex=flex)

        if result["ev"] > best_ev:
            best_ev    = result["ev"]
            best_combo = (combo, result)

    if best_combo is None:
        return None

    combo, ev_result = best_combo
    earliest_lock = min(
        (leg.get("lock_time") for leg in combo if leg.get("lock_time")),
        default=None,
    )

    return {
        "mode":          "flex" if flex else "power",
        "n_picks":       target_picks,
        "multiplier":    ev_result.get("multiplier"),
        "ev":            round(ev_result["ev"], 6),
        "passes":        ev_result["ev"] >= min_ev,
        "joint_prob":    ev_result.get("joint_prob"),
        "earliest_lock": earliest_lock,
        "minutes_to_lock": round(_minutes_to_lock(earliest_lock), 1),
        "legs": [
            {
                "player":    leg["player"],
                "market":    leg["market"],
                "direction": leg["direction"],
                "line":      leg["line"],
                "true_prob": leg["true_prob"],
                "game":      f"{leg.get('away_team', '?')} @ {leg.get('home_team', '?')}",
                "lock_time": leg.get("lock_time"),
            }
            for leg in combo
        ],
    }


def build_best_pp_entry(
    eligible_legs: list[dict],
    flex:          bool = False,
) -> dict | None:
    """
    Try each target pick size in priority order and return the
    first +EV entry found. Tries 4-pick first, then 3, 5, 2.

    Also compares Power vs Flex and returns the higher-EV mode
    if flex=True is passed.
    """
    for n in TARGET_PICKS_ORDER:
        if len(eligible_legs) < n:
            continue
        entry = build_pp_entry(eligible_legs, target_picks=n, flex=False)
        if entry and entry["passes"]:
            if flex and n >= 3:
                flex_entry = build_pp_entry(eligible_legs, target_picks=n, flex=True)
                if flex_entry and flex_entry.get("ev", -1) > entry["ev"]:
                    return flex_entry
            return entry
    return None


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — INTRADAY SCANNER (called by scheduler every 20 min)
# ═══════════════════════════════════════════════════════════════

def intraday_scanner(
    aggregated_props: dict,
    model_probs:      dict,
    bankroll:         float = 100.0,
    sport:            str   = "mlb",
) -> list[dict]:
    """
    Main intraday scan function.
    Called every 20 minutes by slipiq_propline_scanner.py.

    1. Scans all prop sizes (4, 3, 5, 2)
    2. Builds best +EV entry for each size that clears threshold
    3. Returns list of ready entries with Kelly stake
    4. Deduplicates against previously posted entries (via queue file)

    Args:
        aggregated_props : from slipiq_propline.aggregate_propline_by_player()
        model_probs      : {(player_key, market, direction): true_prob}
        bankroll         : current bankroll for Kelly sizing
        sport            : "mlb" or "nba"

    Returns:
        List of entry dicts ready to post to Discord
    """
    markets = PP_ELIGIBLE_MARKETS_MLB if sport == "mlb" else PP_ELIGIBLE_MARKETS_NBA
    ready_entries = []

    for n in TARGET_PICKS_ORDER:
        eligible = scan_eligible_legs(
            aggregated_props, model_probs,
            target_n=n, eligible_markets=markets,
        )
        if len(eligible) < n:
            continue

        # Try Power Play
        entry = build_pp_entry(eligible, target_picks=n, flex=False)
        if entry and entry["passes"]:
            # Add Kelly stake
            if entry.get("joint_prob") and entry.get("multiplier"):
                dec_odds = float(entry["multiplier"])
                entry["kelly_stake"] = kelly_stake(
                    ev=entry["ev"],
                    true_prob=entry["joint_prob"],
                    decimal_odds=dec_odds,
                    bankroll=bankroll,
                )
            else:
                entry["kelly_stake"] = 0.0

            # Compare with Flex if 3+ picks
            if n >= 3:
                flex_entry = build_pp_entry(eligible, target_picks=n, flex=True)
                if flex_entry and flex_entry.get("ev", -1) > entry["ev"]:
                    flex_entry["kelly_stake"] = entry["kelly_stake"]
                    entry = flex_entry

            entry["scan_time"] = datetime.now().isoformat()
            entry["sport"]     = sport
            ready_entries.append(entry)

    # Deduplicate against previously posted entries
    ready_entries = _filter_new_entries(ready_entries)

    if ready_entries:
        _save_queue(ready_entries)

    return ready_entries


def _load_queue() -> list[dict]:
    if PP_QUEUE_FILE.exists():
        try:
            return json.loads(PP_QUEUE_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _save_queue(entries: list[dict]) -> None:
    existing = _load_queue()
    # Keep only today's entries
    today = datetime.now().strftime("%Y-%m-%d")
    existing = [e for e in existing if e.get("scan_time", "")[:10] == today]
    existing.extend(entries)
    PP_QUEUE_FILE.write_text(json.dumps(existing, indent=2))


def _entry_fingerprint(entry: dict) -> frozenset:
    """Unique fingerprint for deduplication — based on sorted player+direction."""
    return frozenset(
        f"{leg['player']}_{leg['direction']}_{leg['line']}"
        for leg in entry.get("legs", [])
    )


def _filter_new_entries(entries: list[dict]) -> list[dict]:
    """Remove entries that have already been posted today."""
    posted = _load_queue()
    posted_fps = {_entry_fingerprint(e) for e in posted}
    return [e for e in entries if _entry_fingerprint(e) not in posted_fps]


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — DISCORD FORMATTER
# ═══════════════════════════════════════════════════════════════

def format_pp_entry_discord(entry: dict) -> str:
    """
    Format a PrizePicks entry for Discord posting.
    Plain text — caller wraps in embed if needed.
    """
    mode       = entry.get("mode", "power").upper()
    n          = entry.get("n_picks", 0)
    multiplier = entry.get("multiplier")
    ev         = entry.get("ev", 0)
    stake      = entry.get("kelly_stake", 0)
    mins_left  = entry.get("minutes_to_lock", 0)

    mult_str = f"{multiplier}x" if multiplier else "Flex"
    ev_str   = f"{ev * 100:+.1f}%"
    time_str = f"{int(mins_left)}m" if mins_left < 9999 else "TBD"

    lines = [
        f"🎯 **PrizePicks {mode} — {n}-Pick {mult_str}** | EV: {ev_str} | 🔒 in {time_str}",
        "",
    ]

    for i, leg in enumerate(entry.get("legs", []), 1):
        prob_pct  = round(leg["true_prob"] * 100, 1)
        dir_emoji = "⬆️" if leg["direction"] == "over" else "⬇️"
        lines.append(
            f"  **{i}.** {dir_emoji} {leg['player']} — "
            f"{leg['market'].replace('player_', '').replace('_', ' ').title()} "
            f"{leg['direction'].upper()} {leg['line']} | {prob_pct}% | {leg['game']}"
        )

    lines.append("")
    if stake > 0:
        lines.append(f"💰 Kelly Stake: ${stake:.2f} (¼ Kelly)")
    lines.append("📊 Verify lines on PrizePicks before submitting")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# SELF-TEST
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("SlipIQ — PrizePicks Engine Self-Test")
    print("=" * 60)

    # Test 1: Thresholds
    print("\n[1] Per-leg breakeven thresholds (Power Play):")
    for n in range(2, 7):
        thr = prizepicks_leg_threshold(n, flex=False)
        print(f"    {n}-pick: {thr:.4f} ({thr*100:.2f}%)")

    # Test 2: leg_is_eligible
    from slipiq_ev_engine import prizepicks_leg_threshold
    assert leg_is_eligible(0.60, 4) is True,  "0.60 should pass 4-pick"
    assert leg_is_eligible(0.55, 4) is False, "0.55 should fail 4-pick"
    print("\n[2] leg_is_eligible: ✓")

    # Test 3: scan_eligible_legs with mock data
    mock_agg = {
        ("gerrit cole", "player_pitcher_strikeouts"): {
            "player":         "Gerrit Cole",
            "market":         "player_pitcher_strikeouts",
            "line_consensus": 7.5,
            "sharp_line":     7.5,
            "pinnacle":       {"over": -115, "under": -105},
            "ev_over":        0.04,
            "ev_under":       -0.02,
            "book_count":     3,
            "_entries":       [{"commence_time": "2099-12-31T20:00:00Z", "game_id": "g1",
                                "home_team": "Yankees", "away_team": "Red Sox"}],
        },
        ("zack wheeler", "player_pitcher_strikeouts"): {
            "player":         "Zack Wheeler",
            "market":         "player_pitcher_strikeouts",
            "line_consensus": 6.5,
            "sharp_line":     6.5,
            "pinnacle":       {"over": -118, "under": -102},
            "ev_over":        0.03,
            "ev_under":       -0.01,
            "book_count":     3,
            "_entries":       [{"commence_time": "2099-12-31T22:00:00Z", "game_id": "g2",
                                "home_team": "Phillies", "away_team": "Mets"}],
        },
        ("corbin burnes", "player_pitcher_strikeouts"): {
            "player":         "Corbin Burnes",
            "market":         "player_pitcher_strikeouts",
            "line_consensus": 5.5,
            "sharp_line":     5.5,
            "pinnacle":       {"over": -112, "under": -108},
            "ev_over":        0.06,
            "ev_under":       -0.02,
            "book_count":     3,
            "_entries":       [{"commence_time": "2099-12-31T19:00:00Z", "game_id": "g3",
                                "home_team": "Orioles", "away_team": "Rays"}],
        },
        ("pablo lopez", "player_pitcher_strikeouts"): {
            "player":         "Pablo Lopez",
            "market":         "player_pitcher_strikeouts",
            "line_consensus": 5.5,
            "sharp_line":     5.5,
            "pinnacle":       {"over": -110, "under": -110},
            "ev_over":        0.05,
            "ev_under":       -0.01,
            "book_count":     2,
            "_entries":       [{"commence_time": "2099-12-31T23:00:00Z", "game_id": "g4",
                                "home_team": "Twins", "away_team": "Tigers"}],
        },
    }
    model_probs = {
        ("gerrit cole",   "player_pitcher_strikeouts", "over"): 0.62,
        ("zack wheeler",  "player_pitcher_strikeouts", "over"): 0.61,
        ("corbin burnes", "player_pitcher_strikeouts", "over"): 0.60,
        ("pablo lopez",   "player_pitcher_strikeouts", "over"): 0.59,
    }

    eligible = scan_eligible_legs(mock_agg, model_probs, target_n=4)
    print(f"\n[3] scan_eligible_legs: {len(eligible)} legs found")
    for leg in eligible:
        print(f"    {leg['player']} {leg['direction']} {leg['line']} | prob={leg['true_prob']:.4f}")

    # Test 4: build entry
    entry = build_pp_entry(eligible, target_picks=4, flex=False)
    if entry:
        print(f"\n[4] 4-pick Power entry: ev={entry['ev']:+.4f}  passes={entry['passes']}")
        print(f"    joint_prob={entry['joint_prob']:.6f}")
        for leg in entry["legs"]:
            print(f"      {leg['player']} {leg['direction']} {leg['line']}")
    else:
        print("\n[4] No 4-pick entry built (check mock probs or lock time)")

    # Test 5: Discord format
    if entry:
        entry["kelly_stake"] = 5.0
        print(f"\n[5] Discord format:\n{format_pp_entry_discord(entry)}")

    print("\n✓ PrizePicks engine ready.")

# ═══════════════════════════════════════════════════════════════
# PATCH: Mixed pitcher markets + queue expiration downsize
# ═══════════════════════════════════════════════════════════════

# All pitcher markets eligible for PrizePicks (not just strikeouts)
PP_MIXED_PITCHER_MARKETS = {
    "player_pitcher_strikeouts",
    "player_strike_outs",
    "player_pitcher_outs",
    "player_hits_allowed",
    "player_walks",
    "player_earned_runs",
    "player_batters_faced",
}

def build_pp_entry_with_expiry(
    eligible_legs:  list[dict],
    target_picks:   int   = 4,
    flex:           bool  = False,
    min_ev:         float = MIN_EDGE_PRIZEPICKS,
    force_min:      float = 30.0,  # minutes before lock to force-fire
) -> dict | None:
    """
    Build PrizePicks entry with queue expiration auto-downsize.
    If earliest leg is within force_min minutes of locking,
    auto-downsize: 6→5→4→3→2 until we have enough or abandon.

    Wraps build_pp_entry() with the time-pressure escalation logic.
    """
    from slipiq_independent_parlay import check_queue_expiration

    # Add lock_time field if missing (use commence_time)
    for leg in eligible_legs:
        if not leg.get("lock_time") and leg.get("lock_time") is not leg.get("commence_time"):
            leg["lock_time"] = leg.get("commence_time")

    # Try ideal size first, then downsize under time pressure
    for try_n in range(target_picks, 1, -1):
        if len(eligible_legs) < try_n:
            continue

        expiry = check_queue_expiration(
            eligible_legs, target_size=try_n, force_fire_min=force_min, min_legs=2
        )

        if expiry["should_fire"]:
            legs_subset = expiry["legs_to_use"]
            entry = build_pp_entry(legs_subset, target_picks=try_n, flex=flex, min_ev=min_ev)
            if entry and entry.get("passes"):
                entry["force_fired"]    = expiry["fire_now"]
                entry["minutes_to_lock"] = expiry["minutes_left"]
                if expiry["fire_now"] and try_n < target_picks:
                    entry["downsize_note"] = f"Auto-downsized {target_picks}→{try_n} due to lock pressure"
                return entry

        # If not urgent yet, only try the target size
        if not expiry["fire_now"]:
            break

    return None

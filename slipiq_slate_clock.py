# slipiq_slate_clock.py
# ═══════════════════════════════════════════════════════════════
# SlipIQ — Game-Aware Slate Clock
#
# WHAT THIS DOES:
#   Reads today's game schedule from ParlayAPI cached props
#   (zero extra credits — reads commence_time from existing cache).
#   Computes fire windows based on actual first pitch times.
#   Replaces hardcoded 8:30 AM trigger with dynamic slate detection.
#
# THREE SLATE WINDOWS:
#   MORNING   → first pitch before 4:00 PM ET  → fire 2.5h before
#   AFTERNOON → first pitch 4:00–7:00 PM ET    → fire 2.5h before
#   EVENING   → first pitch after 7:00 PM ET   → fire 2.5h before
#
# FIRE LOGIC:
#   - Each window fires once per day (state tracks per window)
#   - Fire window is 30 minutes wide (don't miss if scheduler was late)
#   - Falls back to 8:30 AM AZ if no game data available
#   - PrizePicks scanner start tied to morning/afternoon fire
#
# USAGE (from orchestrator):
#   from slipiq_slate_clock import SlateClock
#   clock = SlateClock()
#   windows = clock.get_fire_windows()
#   if clock.should_fire("morning", state):
#       run_main(state)
# ═══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CACHE_DIR = Path("cache")
AZ_TZ     = ZoneInfo("America/Phoenix")   # UTC-7, no DST
ET_TZ     = ZoneInfo("America/New_York")

# How far before first pitch to fire each window
LEAD_TIME_HOURS = 2.5

# Fire window width — if scheduler was delayed, still catch it
FIRE_WINDOW_MINUTES = 35

# Fallback fire times (AZ) if no game data available
FALLBACK_TIMES = {
    "morning":   "08:30",
    "afternoon": "13:00",
    "evening":   "17:00",
}

# ET boundaries between slate windows
MORNING_CUTOFF_ET   = 16   # before 4 PM ET = morning slate
AFTERNOON_CUTOFF_ET = 19   # 4-7 PM ET = afternoon slate
                            # after 7 PM ET = evening slate

# Minimum games in a window to fire that window
MIN_GAMES_TO_FIRE = 1


class SlateClock:
    """
    Game-aware schedule detector.
    Reads today's game schedule from cached ParlayAPI data.
    Computes optimal fire windows per slate.
    """

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self._windows: dict | None = None
        self._games:   list[dict] | None = None

    # ─── Public API ───────────────────────────────────────────

    def get_fire_windows(self, force_refresh: bool = False) -> dict:
        """
        Compute fire windows for today's slate.

        Returns:
        {
            "morning":   {"fire_time": "10:35", "games": 3, "first_pitch_et": "13:05"},
            "afternoon": {"fire_time": "14:05", "games": 5, "first_pitch_et": "16:35"},
            "evening":   {"fire_time": "17:10", "games": 4, "first_pitch_et": "19:40"},
            "source":    "parlayapi_cache" | "fallback",
            "total_games": 12,
        }
        """
        if self._windows and not force_refresh:
            return self._windows

        games = self._load_today_games()
        self._games   = games
        self._windows = self._compute_windows(games)
        return self._windows

    def should_fire(self, window: str, state: dict) -> bool:
        """
        Check if a slate window should fire right now.

        Args:
            window: "morning" | "afternoon" | "evening"
            state:  orchestrator state dict

        Returns True if:
          - Window has games
          - Current AZ time is within FIRE_WINDOW_MINUTES of fire_time
          - Window hasn't already fired today (checked via state)
        """
        done_key = f"{window}_done"
        if state.get(done_key):
            return False

        windows = self.get_fire_windows()
        info    = windows.get(window, {})

        if info.get("games", 0) < MIN_GAMES_TO_FIRE:
            return False

        fire_time_str = info.get("fire_time")
        if not fire_time_str:
            return False

        return self._is_in_fire_window(fire_time_str)

    def get_next_fire_info(self, state: dict) -> str:
        """Human-readable string of next fire event for idle log."""
        windows = self.get_fire_windows()
        now_str = datetime.now(AZ_TZ).strftime("%H:%M")

        for window in ("morning", "afternoon", "evening"):
            done_key = f"{window}_done"
            if state.get(done_key):
                continue
            info = windows.get(window, {})
            if info.get("games", 0) < MIN_GAMES_TO_FIRE:
                continue
            fire_time = info.get("fire_time", "?")
            n_games   = info.get("games", 0)
            first_et  = info.get("first_pitch_et", "?")
            return (f"{window} slate @ {fire_time} AZ "
                    f"({n_games} games, first pitch {first_et} ET)")

        return "no remaining slates today"

    def slate_summary(self) -> str:
        """One-line summary for Discord/logs showing today's slate windows."""
        windows = self.get_fire_windows()
        total   = windows.get("total_games", 0)
        source  = windows.get("source", "?")
        parts   = []

        for w in ("morning", "afternoon", "evening"):
            info = windows.get(w, {})
            n    = info.get("games", 0)
            t    = info.get("fire_time", "")
            if n > 0:
                parts.append(f"{w.capitalize()}: {n}G → fire {t} AZ")

        body = " | ".join(parts) if parts else "No games detected"
        return f"[slate] {total} total games ({source}) — {body}"

    # ─── Game Loading ─────────────────────────────────────────

    def _load_today_games(self) -> list[dict]:
        """
        Load today's games from ParlayAPI prop cache.
        Zero extra credits — reads commence_time from existing cache.
        Falls back to Odds API events cache if ParlayAPI cache missing.
        """
        games = self._from_parlayapi_cache()
        if games:
            return games

        games = self._from_odds_events_cache()
        if games:
            return games

        print("  [slate_clock] No game data in cache — using fallback schedule")
        return []

    def _from_parlayapi_cache(self) -> list[dict]:
        """Read commence_time from ParlayAPI props cache."""
        # ParlayAPI cache is written as parlayapi_props_baseball_mlb.json
        cache_patterns = [
            "parlayapi_props_baseball_mlb.json",
            "props_baseball_mlb.json",
            "parlayapi_props_mlb.json",
        ]

        for pattern in cache_patterns:
            path = self.cache_dir / pattern
            if not path.exists():
                continue
            try:
                with open(path) as f:
                    payload = json.load(f)

                props = payload.get("data") or payload.get("value") or []
                if not isinstance(props, list):
                    continue

                return self._extract_games(props, source="parlayapi_cache")
            except Exception as e:
                print(f"  [slate_clock] ParlayAPI cache read error: {e}")
                continue

        return []

    def _from_odds_events_cache(self) -> list[dict]:
        """Read from Odds API events cache as fallback."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        path      = self.cache_dir / f"mlb_events_{today_str}.json"

        if not path.exists():
            return []

        try:
            with open(path) as f:
                payload = json.load(f)
            events = payload.get("value") or payload.get("data") or []
            if not isinstance(events, list):
                return []

            games = []
            seen  = set()
            for event in events:
                commence = event.get("commence_time") or event.get("cached_at", "")
                home     = event.get("home_team", "")
                away     = event.get("away_team", "")
                game_key = f"{away}@{home}"
                if game_key in seen:
                    continue
                seen.add(game_key)
                games.append({
                    "home_team":     home,
                    "away_team":     away,
                    "commence_time": commence,
                    "source":        "odds_events_cache",
                })
            return games
        except Exception as e:
            print(f"  [slate_clock] Odds events cache error: {e}")
            return []

    def _extract_games(self, props: list[dict], source: str) -> list[dict]:
        """Extract unique games with commence_time from props list."""
        seen  = set()
        games = []

        for prop in props:
            home     = prop.get("home_team", "")
            away     = prop.get("away_team", "")
            commence = prop.get("commence_time")
            if not commence:
                continue
            game_key = f"{away}@{home}"
            if game_key in seen:
                continue
            seen.add(game_key)
            games.append({
                "home_team":     home,
                "away_team":     away,
                "commence_time": commence,
                "source":        source,
            })

        return games

    # ─── Window Computation ───────────────────────────────────

    def _compute_windows(self, games: list[dict]) -> dict:
        """Sort games into morning/afternoon/evening windows."""
        today = datetime.now(AZ_TZ).date()

        morning_games   = []
        afternoon_games = []
        evening_games   = []

        for g in games:
            et_time = self._parse_commence_et(g.get("commence_time"))
            if et_time is None:
                continue
            if et_time.date() != today:
                continue

            hour_et = et_time.hour
            if hour_et < MORNING_CUTOFF_ET:
                morning_games.append((et_time, g))
            elif hour_et < AFTERNOON_CUTOFF_ET:
                afternoon_games.append((et_time, g))
            else:
                evening_games.append((et_time, g))

        source = "parlayapi_cache" if games else "fallback"

        return {
            "morning":     self._window_info(morning_games,   "morning"),
            "afternoon":   self._window_info(afternoon_games, "afternoon"),
            "evening":     self._window_info(evening_games,   "evening"),
            "source":      source,
            "total_games": len(games),
        }

    def _window_info(
        self,
        game_times: list[tuple],
        window:     str,
    ) -> dict:
        """
        Compute fire time for a slate window.
        Fire = first pitch - LEAD_TIME_HOURS, converted to AZ.
        Falls back to FALLBACK_TIMES if no games.
        """
        if not game_times:
            return {
                "fire_time":     FALLBACK_TIMES.get(window),
                "games":         0,
                "first_pitch_et": None,
                "fallback":      True,
            }

        # First pitch in this window
        first_et = min(et_time for et_time, _ in game_times)

        # Fire time = first pitch minus lead time
        fire_et  = first_et - timedelta(hours=LEAD_TIME_HOURS)
        fire_az  = fire_et.astimezone(AZ_TZ)

        # If fire time is in the past (bot deployed late), fire 5 min from now
        now_az = datetime.now(AZ_TZ)
        if fire_az < now_az - timedelta(minutes=5):
            fire_az = now_az + timedelta(minutes=5)

        return {
            "fire_time":      fire_az.strftime("%H:%M"),
            "fire_dt":        fire_az.isoformat(),
            "games":          len(game_times),
            "first_pitch_et": first_et.strftime("%H:%M"),
            "first_pitch_dt": first_et.isoformat(),
            "fallback":       False,
        }

    # ─── Time Utilities ───────────────────────────────────────

    def _parse_commence_et(self, commence_time: str | None) -> datetime | None:
        """Parse commence_time ISO string to ET datetime."""
        if not commence_time:
            return None
        try:
            dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
            return dt.astimezone(ET_TZ)
        except (ValueError, TypeError):
            return None

    def _is_in_fire_window(self, fire_time_str: str) -> bool:
        """
        Check if current AZ time is within FIRE_WINDOW_MINUTES of fire_time.
        Fire window: [fire_time, fire_time + FIRE_WINDOW_MINUTES].
        """
        now   = datetime.now(AZ_TZ)
        today = now.strftime("%Y-%m-%d")

        try:
            fire_dt = datetime.strptime(
                f"{today} {fire_time_str}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=AZ_TZ)
        except ValueError:
            return False

        delta_minutes = (now - fire_dt).total_seconds() / 60
        return 0 <= delta_minutes <= FIRE_WINDOW_MINUTES


# ═══════════════════════════════════════════════════════════════
# STANDALONE SCHEDULE PRINTER
# ═══════════════════════════════════════════════════════════════

def print_today_schedule() -> None:
    """Print today's detected slate windows. Run standalone to verify."""
    clock   = SlateClock()
    windows = clock.get_fire_windows()

    print("\n" + "=" * 60)
    print("SlipIQ Slate Clock — Today's Schedule")
    print(f"{datetime.now(AZ_TZ).strftime('%A, %B %d %Y — %I:%M %p AZ')}")
    print("=" * 60)
    print(f"\n  Source    : {windows['source']}")
    print(f"  Total games: {windows['total_games']}")
    print()

    for window in ("morning", "afternoon", "evening"):
        info     = windows.get(window, {})
        n_games  = info.get("games", 0)
        fire_str = info.get("fire_time", "N/A")
        fp_et    = info.get("first_pitch_et", "N/A")
        fallback = " (fallback)" if info.get("fallback") else ""
        status   = f"{n_games} games → fire {fire_str} AZ (first pitch {fp_et} ET){fallback}"

        if n_games == 0:
            status = "No games — window skipped"

        print(f"  {window.upper():<10}: {status}")

    print()
    print(f"  Summary: {clock.slate_summary()}")


if __name__ == "__main__":
    print_today_schedule()

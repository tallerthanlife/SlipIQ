# slipiq_weather.py
# Game weather for confidence modifiers — tomorrow.io primary, fallbacks chained.
# 0 ParlayAPI credits. Cache per venue + game date.

import json
from datetime import datetime, timedelta
from pathlib import Path

import requests

from slipiq_env import OPENWEATHER_API_KEY, TOMORROW_IO_API_KEY

TOMORROW_API_KEY = TOMORROW_IO_API_KEY

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# MLB park approx coords (lat, lon) — extend as needed
MLB_PARK_COORDS = {
    "arizona diamondbacks": (33.4453, -112.0667),
    "atlanta braves": (33.8907, -84.4678),
    "baltimore orioles": (39.2839, -76.6217),
    "boston red sox": (42.3467, -71.0972),
    "chicago cubs": (41.9484, -87.6553),
    "chicago white sox": (41.8299, -87.6338),
    "cincinnati reds": (39.0979, -84.5082),
    "cleveland guardians": (41.4962, -81.6852),
    "colorado rockies": (39.7559, -104.9942),
    "detroit tigers": (42.3390, -83.0485),
    "houston astros": (29.7573, -95.3555),
    "kansas city royals": (39.0517, -94.4803),
    "los angeles angels": (33.8003, -117.8827),
    "los angeles dodgers": (34.0739, -118.2400),
    "miami marlins": (25.7781, -80.2197),
    "milwaukee brewers": (43.0280, -87.9712),
    "minnesota twins": (44.9817, -93.2776),
    "new york mets": (40.7571, -73.8458),
    "new york yankees": (40.8296, -73.9262),
    "oakland athletics": (37.7516, -122.2005),
    "philadelphia phillies": (39.9061, -75.1665),
    "pittsburgh pirates": (40.4469, -80.0058),
    "san diego padres": (32.7073, -117.1566),
    "san francisco giants": (37.7786, -122.3893),
    "seattle mariners": (47.5914, -122.3325),
    "st. louis cardinals": (38.6226, -90.1928),
    "tampa bay rays": (27.7682, -82.6534),
    "texas rangers": (32.7473, -97.0847),
    "toronto blue jays": (43.6414, -79.3894),
    "washington nationals": (38.8730, -77.0074),
}

DOME_TEAMS = {
    "tampa bay rays", "milwaukee brewers", "houston astros",
    "miami marlins", "arizona diamondbacks", "texas rangers",
}


def _cache_path(game_date: str, venue_key: str) -> Path:
    safe = venue_key.replace(" ", "_")[:40]
    return CACHE_DIR / f"weather_{game_date}_{safe}.json"


def _cache_read(path: Path, max_hours: int = 12) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path) as f:
            payload = json.load(f)
        ts = datetime.fromisoformat(payload["timestamp"])
        if datetime.utcnow() - ts > timedelta(hours=max_hours):
            return None
        return payload.get("data")
    except Exception:
        return None


def _cache_write(path: Path, data: dict):
    with open(path, "w") as f:
        json.dump({"timestamp": datetime.utcnow().isoformat(), "data": data}, f)


def resolve_venue_key(home_team: str, away_team: str) -> str:
    """Outdoor games use home park; default to home team name."""
    home = (home_team or "").lower().strip()
    if home in DOME_TEAMS:
        return home
    return home or (away_team or "").lower().strip()


def _coords_for_venue(venue_key: str) -> tuple[float, float] | None:
    return MLB_PARK_COORDS.get(venue_key.lower())


def fetch_tomorrow_io(lat: float, lon: float) -> dict | None:
    if not TOMORROW_API_KEY:
        return None
    try:
        r = requests.get(
            "https://api.tomorrow.io/v4/weather/realtime",
            params={
                "location": f"{lat},{lon}",
                "apikey": TOMORROW_API_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        values = (data.get("data") or {}).get("values") or {}
        return {
            "source":      "tomorrow.io",
            "temp_f":      values.get("temperature"),
            "wind_mph":    values.get("windSpeed"),
            "precip_prob": values.get("precipitationProbability"),
            "humidity":    values.get("humidity"),
        }
    except Exception as e:
        print(f"  [weather] tomorrow.io: {e}")
        return None


def fetch_openweather(lat: float, lon: float) -> dict | None:
    if not OPENWEATHER_API_KEY:
        return None
    try:
        r = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY, "units": "imperial"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        wind = (data.get("wind") or {}).get("speed", 0)
        main = data.get("main") or {}
        return {
            "source":      "openweather",
            "temp_f":      main.get("temp"),
            "wind_mph":    wind,
            "precip_prob": None,
            "humidity":    main.get("humidity"),
        }
    except Exception as e:
        print(f"  [weather] openweather: {e}")
        return None


def weather_to_modifier_flags(wx: dict, venue_key: str) -> list[str]:
    """Map conditions to confidence_agent MODIFIERS keys."""
    flags = []
    if venue_key.lower() in DOME_TEAMS:
        flags.append("dome_game")
        return flags

    if not wx:
        return flags

    temp = wx.get("temp_f")
    wind = wx.get("wind_mph")
    precip = wx.get("precip_prob")

    if temp is not None and temp < 50:
        flags.append("cold_weather")
    if precip is not None and precip >= 40:
        flags.append("rain_risk")
    elif precip is None and wx.get("source") == "openweather":
        weather_main = wx.get("weather_main", "")
        if weather_main in ("Rain", "Drizzle", "Thunderstorm"):
            flags.append("rain_risk")
    if wind is not None and wind >= 12:
        flags.append("high_wind_out")

    return flags


def get_game_weather(
    home_team: str,
    away_team: str,
    game_date: str = None,
) -> dict:
    """
    Pull weather with fallback chain:
      tomorrow.io → OpenWeather → dome/neutral
    Returns {flags: [...], raw: {...}, source: str}
    """
    game_date = game_date or datetime.now().strftime("%Y-%m-%d")
    venue_key = resolve_venue_key(home_team, away_team)
    path = _cache_path(game_date, venue_key)

    cached = _cache_read(path)
    if cached:
        return cached

    if venue_key.lower() in DOME_TEAMS:
        result = {"flags": ["dome_game"], "raw": {}, "source": "dome_table", "venue": venue_key}
        _cache_write(path, result)
        return result

    coords = _coords_for_venue(venue_key)
    raw = None
    source = "neutral"

    if coords:
        lat, lon = coords
        raw = fetch_tomorrow_io(lat, lon)
        if raw:
            source = raw["source"]
        else:
            raw = fetch_openweather(lat, lon)
            if raw:
                source = raw["source"]

    flags = weather_to_modifier_flags(raw or {}, venue_key)
    result = {
        "flags":  flags,
        "raw":    raw or {},
        "source": source,
        "venue":  venue_key,
    }
    _cache_write(path, result)
    return result

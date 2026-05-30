"""
Bookmaker.eu MLB main line scraper using Playwright.
Used as the truest fair line source — line origin benchmark.
Bookmaker.eu is where sharp syndicates hit first.
Runs nightly, caches lines for morning EV calculation.
"""
import json
from datetime import date
from pathlib import Path

CACHE_PATH = Path("cache/bookmaker_lines.json")


def get_cached_lines() -> list[dict]:
    if not CACHE_PATH.exists():
        return []
    try:
        data = json.loads(CACHE_PATH.read_text())
        if data.get("date") == str(date.today()):
            lines = data.get("lines", [])
            print(f"  [bookmaker] Cache hit — {len(lines)} lines")
            return lines
    except Exception:
        pass
    return []


def scrape_bookmaker_mlb() -> list[dict]:
    """
    Scrape Bookmaker.eu MLB lines using Playwright.
    Focus: game lines (ML, spread, total) + any pitcher props available.
    These are the truest prices in the market.
    """
    cached = get_cached_lines()
    if cached:
        return cached

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [bookmaker] Playwright not installed")
        return []

    lines = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )
        page = context.new_page()

        # Intercept API responses — Bookmaker loads odds via XHR
        captured = []

        def handle_response(response):
            if "bookmaker" in response.url and response.status == 200:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        body = response.json()
                        captured.append({
                            "url": response.url,
                            "body": body
                        })
                    except Exception:
                        pass

        page.on("response", handle_response)

        try:
            page.goto(
                "https://www.bookmaker.eu/sports-betting/baseball/mlb",
                wait_until="networkidle",
                timeout=30000,
            )
            page.wait_for_timeout(4000)

            # Also try their props page if it exists
            try:
                page.goto(
                    "https://www.bookmaker.eu/sports-betting/baseball/mlb/player-props",
                    wait_until="networkidle",
                    timeout=15000,
                )
                page.wait_for_timeout(2000)
            except Exception:
                pass

            # Parse captured XHR responses
            for capture in captured:
                parsed = _parse_bookmaker_response(capture["body"])
                lines.extend(parsed)

            # Fallback: parse DOM if XHR capture empty
            if not lines:
                lines = _parse_bookmaker_dom(page)

        except Exception as e:
            print(f"  [bookmaker] Scrape error: {e}")
        finally:
            browser.close()

    if lines:
        CACHE_PATH.parent.mkdir(exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "date": str(date.today()),
            "lines": lines,
        }))
        print(f"  [bookmaker] Scraped and cached {len(lines)} lines")
    else:
        print("  [bookmaker] No lines captured")

    return lines


def _parse_bookmaker_response(body: dict | list) -> list[dict]:
    """Parse Bookmaker.eu XHR JSON response into normalized lines."""
    lines = []
    try:
        events = body if isinstance(body, list) else body.get("events", [])
        for event in events:
            home = event.get("home") or event.get("homeTeam", "")
            away = event.get("away") or event.get("awayTeam", "")
            start = event.get("startTime") or event.get("start", "")
            for market in event.get("markets", []):
                market_name = market.get("name", "").lower()
                for outcome in market.get("outcomes", []):
                    lines.append({
                        "source": "bookmaker",
                        "home_team": home,
                        "away_team": away,
                        "start_time": start,
                        "market": market_name,
                        "outcome": outcome.get("name", ""),
                        "price": outcome.get("price"),
                        "point": outcome.get("point"),
                    })
    except Exception:
        pass
    return lines


def _parse_bookmaker_dom(page) -> list[dict]:
    """Fallback DOM parser for Bookmaker.eu."""
    lines = []
    try:
        rows = page.query_selector_all(
            "[class*='event'], [class*='game-row'], [class*='matchup']"
        )
        for row in rows:
            try:
                text = row.inner_text().strip()
                if text:
                    lines.append({"raw": text, "source": "bookmaker_dom"})
            except Exception:
                pass
    except Exception:
        pass
    return lines


def get_fair_line_bookmaker(home_team: str, away_team: str) -> dict | None:
    """
    Get Bookmaker.eu fair line for a specific game.
    Use this as the true probability anchor for EV calculation.
    """
    lines = get_cached_lines() or scrape_bookmaker_mlb()
    home_lower = home_team.lower()
    away_lower = away_team.lower()

    game_lines = [
        l for l in lines
        if (home_lower in l.get("home_team", "").lower() or
            away_lower in l.get("away_team", "").lower())
        and l.get("market") in ("moneyline", "h2h", "ml", "run line")
    ]

    if not game_lines:
        return None

    # Find over/under pair
    over = next((l for l in game_lines if "over" in l.get("outcome", "").lower()), None)
    under = next((l for l in game_lines if "under" in l.get("outcome", "").lower()), None)

    if over and under:
        try:
            from slipiq_novig import remove_vig
            novig = remove_vig(int(over["price"]), int(under["price"]))
            return {
                "source": "bookmaker",
                "home_team": home_team,
                "away_team": away_team,
                "over_odds": over["price"],
                "under_odds": under["price"],
                "fair_over_prob": novig["fair_over_prob"],
                "fair_under_prob": novig["fair_under_prob"],
            }
        except Exception:
            pass

    return None

"""
BetOnline MLB prop scraper using Playwright.
Runs nightly at 10pm AZ, caches lines for morning pipeline.
BetOnline posts MLB props 12-14 hours before game time.
"""
import json
import os
from datetime import date
from pathlib import Path

CACHE_PATH = Path("cache/betonline_lines.json")


def get_cached_lines() -> list[dict]:
    """Return cached BetOnline lines if from today."""
    if not CACHE_PATH.exists():
        return []
    try:
        data = json.loads(CACHE_PATH.read_text())
        if data.get("date") == str(date.today()):
            lines = data.get("lines", [])
            print(f"  [betonline] Cache hit — {len(lines)} lines")
            return lines
    except Exception:
        pass
    return []


def scrape_betonline_mlb_props() -> list[dict]:
    """
    Scrape BetOnline MLB pitcher props using Playwright.
    Returns list of prop lines.
    """
    cached = get_cached_lines()
    if cached:
        return cached

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [betonline] Playwright not installed — pip install playwright")
        return []

    lines = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        try:
            # BetOnline MLB player props URL
            page.goto(
                "https://www.betonline.ag/sportsbook/baseball/mlb",
                wait_until="networkidle",
                timeout=30000,
            )

            # Wait for props to load
            page.wait_for_timeout(3000)

            # Intercept the API calls BetOnline makes internally
            # Their props load from their internal odds API
            # We extract from the rendered DOM

            prop_sections = page.query_selector_all(
                "[class*='prop'], [class*='player-prop'], [data-market*='strikeout']"
            )

            for section in prop_sections:
                try:
                    text = section.inner_text()
                    lines.append({"raw": text, "source": "betonline"})
                except Exception:
                    pass

        except Exception as e:
            print(f"  [betonline] Scrape error: {e}")
        finally:
            browser.close()

    # Cache results
    if lines:
        CACHE_PATH.parent.mkdir(exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "date": str(date.today()),
            "lines": lines,
        }))
        print(f"  [betonline] Scraped and cached {len(lines)} lines")
    else:
        print("  [betonline] No lines scraped — DOM structure may have changed")

    return lines


def get_pitcher_k_line_betonline(player_name: str) -> dict | None:
    """Get BetOnline strikeout line for a pitcher."""
    lines = get_cached_lines() or scrape_betonline_mlb_props()
    player_lower = player_name.lower()
    for line in lines:
        if player_lower in line.get("player", "").lower():
            if "strikeout" in line.get("market", "").lower():
                return line
    return None

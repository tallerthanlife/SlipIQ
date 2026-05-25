"""
SlipIQ DraftKings Direct Props
Pulls pitcher strikeout lines directly from DraftKings public API
No API key needed — supplements Odds API coverage
"""

import requests
import json

DK_BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups"
DK_MLB_GROUP = "84240"  # MLB event group ID

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sportsbook.draftkings.com/",
}


def get_dk_mlb_events():
    """Get today's MLB event IDs from DraftKings"""
    url = f"{DK_BASE}/{DK_MLB_GROUP}"
    params = {
        "includeSubcategories": "false",
        "format": "json",
    }

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        events = []
        event_group = data.get("eventGroup", {})
        for event in event_group.get("events", []):
            events.append({
                "event_id": event.get("eventId"),
                "name": event.get("name", ""),
                "home_team": event.get("teamName1", ""),
                "away_team": event.get("teamName2", ""),
            })

        return events

    except Exception as e:
        print(f"DK events error: {e}")
        return []


def get_dk_pitcher_props(event_id):
    """Get pitcher strikeout props for a specific DK event"""
    url = f"https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/{DK_MLB_GROUP}/categories/1000/subcategories/6993"
    params = {
        "eventId": event_id,
        "format": "json",
    }

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if response.status_code != 200:
            return []

        data = response.json()
        props = []

        for category in data.get("eventGroup", {}).get("offerCategories", []):
            for subcategory in category.get("offerSubcategoryDescriptors", []):
                for offer_category in subcategory.get("offerSubcategory", {}).get("offers", []):
                    for offer in offer_category:
                        label = offer.get("label", "").lower()
                        if "strikeout" not in label:
                            continue

                        outcomes = offer.get("outcomes", [])
                        for outcome in outcomes:
                            props.append({
                                "pitcher": outcome.get("participant", ""),
                                "line": outcome.get("line", 0),
                                "direction": outcome.get("label", ""),
                                "odds": outcome.get("oddsAmerican", ""),
                                "bookmaker": "DraftKings",
                            })

        return props

    except Exception as e:
        print(f"DK props error for event {event_id}: {e}")
        return []


def get_all_dk_pitcher_props():
    """
    Pull all pitcher strikeout props from DraftKings directly
    Returns same format as Odds API props
    """
    print("Pulling DraftKings direct props...")

    # Use the DK offer catalog endpoint — more reliable
    url = "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/84240/categories/1000"
    params = {"format": "json"}

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        props = []
        seen = set()

        # Walk through offer categories
        for offer_cat in data.get("eventGroup", {}).get("offerCategories", []):
            for sub in offer_cat.get("offerSubcategoryDescriptors", []):
                name = sub.get("name", "").lower()
                if "strikeout" not in name and "pitcher" not in name:
                    continue

                sub_data = sub.get("offerSubcategory", {})
                for offer_list in sub_data.get("offers", []):
                    for offer in offer_list:
                        for outcome in offer.get("outcomes", []):
                            pitcher = outcome.get("participant", "").strip()
                            line = outcome.get("line")
                            direction = outcome.get("label", "")

                            if not pitcher or line is None:
                                continue

                            key = f"{pitcher}_{line}_{direction}"
                            if key in seen:
                                continue
                            seen.add(key)

                            props.append({
                                "pitcher": pitcher,
                                "line": float(line),
                                "direction": direction,
                                "odds": outcome.get("oddsAmerican", ""),
                                "home_team": "",
                                "away_team": "",
                                "bookmaker": "DraftKings",
                            })

        print(f"DraftKings direct: {len(set(p['pitcher'] for p in props))} pitchers found")
        return props

    except Exception as e:
        print(f"DraftKings direct pull failed: {e}")
        return []


if __name__ == "__main__":
    props = get_all_dk_pitcher_props()
    pitchers = set(p["pitcher"] for p in props if p["direction"] == "Over")
    print(f"\nTotal pitchers with Over lines: {len(pitchers)}")
    for p in sorted(pitchers):
        line = next(x["line"] for x in props if x["pitcher"] == p and x["direction"] == "Over")
        print(f"  {p}: {line} K")
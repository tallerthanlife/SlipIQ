# slipiq_sharp_review_patch.py
# ═══════════════════════════════════════════════════════════════
# PATCH — Apply to slipiq_sharp_review.py in Cursor
#
# ONE CHANGE: Replace fetch_closing_line() stub with real implementation
# that calls slipiq_parlayapi.fetch_historical_props() to get the
# Pinnacle closing line for a player/market/date.
# Without this, CLV is always None and the calibration tracker is useless.
# ═══════════════════════════════════════════════════════════════

FETCH_CLOSING_LINE_NEW = '''
def fetch_closing_line(
    player_name: str,
    market:      str,
    game_date:   str,
) -> dict | None:
    """
    Fetch Pinnacle closing line for a player/market/date.
    Used for CLV calculation in Sharp Review.

    Returns:
        {
            "over_price":  int | None,   # Pinnacle American odds
            "under_price": int | None,
            "line":        float | None, # closing prop line
        }
        or None if no data found.

    Source: slipiq_parlayapi.fetch_historical_props() — 5 credits/call.
    Result cached per day so Sharp Review only costs 5 credits once.
    """
    from pathlib import Path
    import json
    from datetime import datetime

    cache_dir  = Path("cache")
    cache_path = cache_dir / f"closing_lines_{game_date.replace('-', '')}.json"

    # Load from cache if available
    closing_cache = {}
    if cache_path.exists():
        try:
            closing_cache = json.loads(cache_path.read_text())
        except (json.JSONDecodeError, ValueError):
            pass

    cache_key = f"{player_name.lower().strip()}_{market}"
    if cache_key in closing_cache:
        return closing_cache[cache_key] or None

    # Fetch from parlayapi historical endpoint
    try:
        from slipiq_parlayapi import fetch_historical_props, SPORT_MLB
        historical = fetch_historical_props(SPORT_MLB, game_date=game_date)

        if not historical:
            closing_cache[cache_key] = None
            cache_path.write_text(json.dumps(closing_cache, indent=2))
            return None

        # Search for matching player/market in historical data
        player_norm = player_name.lower().strip()
        result      = None

        for prop in historical:
            prop_player = (prop.get("player_name") or prop.get("player") or "").lower().strip()
            prop_market = (prop.get("market_key")  or prop.get("market")  or "").lower()

            if prop_player != player_norm:
                continue
            if market and prop_market and market not in prop_market and prop_market not in market:
                continue

            # Find Pinnacle line in this prop's books
            for book_entry in (prop.get("books") or prop.get("bookmakers") or []):
                book_key = (book_entry.get("key") or book_entry.get("book") or "").lower()
                if "pinnacle" not in book_key:
                    continue

                markets_list = book_entry.get("markets") or [book_entry]
                for mkt in markets_list:
                    outcomes = mkt.get("outcomes") or []
                    if not outcomes and mkt.get("over_price"):
                        outcomes = [mkt]

                    over_price  = None
                    under_price = None
                    line_val    = mkt.get("line") or mkt.get("point")

                    for outcome in outcomes:
                        name  = (outcome.get("name") or "").lower()
                        price = outcome.get("price")
                        pt    = outcome.get("point")
                        if pt is not None:
                            line_val = pt
                        if "over" in name and price:
                            over_price = int(price)
                        elif "under" in name and price:
                            under_price = int(price)

                    if over_price or under_price:
                        result = {
                            "over_price":  over_price,
                            "under_price": under_price,
                            "line":        float(line_val) if line_val else None,
                        }
                        break
                if result:
                    break

            if result:
                break

        # Cache result (None if not found)
        closing_cache[cache_key] = result
        cache_path.write_text(json.dumps(closing_cache, indent=2))
        return result

    except Exception as e:
        print(f"  [sharp_review] fetch_closing_line error for {player_name}: {e}")
        return None
'''

# ─── WHERE TO INSERT ─────────────────────────────────────────
# In slipiq_sharp_review.py, find the existing fetch_closing_line()
# function (which returns None or is stubbed) and replace its
# entire body with the implementation above.
# Keep the function signature identical.

# ─── ALSO ADD: CLV logging call after grade_pick() ─────────
# In run_mlb_sharp_review(), after:
#     result = grade_pick(card, actual_ks, closing_line)
#     results.append(result)
# ADD:
CLV_LOGGING_CALL = '''
        # Log to calibration tracker with CLV
        try:
            from slipiq_calibration import log_result_by_player
            from slipiq_ev_engine import closing_line_value

            clv_pct = None
            if closing_line and card.get("best_book"):
                bet_price     = (card.get("best_book") or {}).get("price")
                closing_price = (
                    closing_line.get("over_price")  if direction == "over"
                    else closing_line.get("under_price")
                ) if closing_line else None

                if bet_price and closing_price:
                    clv_result = closing_line_value(bet_price, closing_price)
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
'''

CURSOR_INSTRUCTIONS = """
Apply these changes to slipiq_sharp_review.py:

CHANGE 1: Replace the body of fetch_closing_line() with
FETCH_CLOSING_LINE_NEW above.
Keep the function signature: def fetch_closing_line(player_name, market, game_date)

CHANGE 2: In run_mlb_sharp_review(), after:
    result = grade_pick(card, actual_ks, closing_line)
    results.append(result)
Insert CLV_LOGGING_CALL above.

These two changes complete the CLV feedback loop:
  fetch_closing_line() gets real Pinnacle closing prices
  CLV_LOGGING_CALL writes them to calibration_log.json + Supabase
"""

print("Sharp review patch spec loaded.")
print(CURSOR_INSTRUCTIONS)

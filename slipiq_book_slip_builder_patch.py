# slipiq_book_slip_builder_patch.py
# ═══════════════════════════════════════════════════════════════
# PATCH — Apply to slipiq_book_slip_builder.py in Cursor
#
# THREE CHANGES:
#
# CHANGE 1: per_book_output() — add EV % and breakeven to each book's line
#   Currently shows: DK -108 | Fanatics +105 💰 | FanDuel -112
#   Now shows:       DK -108 (BE 51.9%) | Fanatics +105 💰 EV +3.2% | FanDuel -112
#
# CHANGE 2: _batter_on_team() — replace fuzzy string match with player_ids lookup
#   The current function does string matching on team names.
#   Replace with is_batter_on_team() from slipiq_player_ids.
#
# CHANGE 3: card_to_slip_leg() — include ev and true_prob in leg dict
#   Downstream montecarlo validation needs these fields.
# ═══════════════════════════════════════════════════════════════

CHANGE_1_PER_BOOK_OUTPUT = '''
def per_book_output(slip: dict) -> dict:
    """
    Show all books side-by-side for each leg.
    Now includes EV % and breakeven for each book line.
    💰 = plus-money | EV shown when real Pinnacle data available.
    """
    if not slip or not slip.get("legs"):
        return slip

    for leg in slip.get("legs", []):
        books_display = leg.get("books_display") or {}
        if not books_display:
            continue

        # Get Pinnacle prices from the leg for EV calculation
        pin_over  = leg.get("pinnacle_over")
        pin_under = leg.get("pinnacle_under")
        direction = (leg.get("direction") or "over").lower()

        book_parts = []
        for label, bk in books_display.items():
            price = bk.get("price")
            if price is None:
                continue

            price_str = f"+{price}" if price > 0 else str(price)
            money_flag = " 💰" if price > 0 else ""

            # EV per book (if Pinnacle available)
            ev_str = ""
            be_str = ""
            if pin_over and pin_under:
                try:
                    from slipiq_ev_engine import assess_leg, breakeven_display
                    result = assess_leg(pin_over, pin_under, price, direction)
                    ev_val = result["ev"]
                    if ev_val is not None:
                        ev_str = f" EV {'+' if ev_val >= 0 else ''}{ev_val*100:.1f}%"
                    be_str = f" (BE {result['breakeven']*100:.1f}%)"
                except Exception:
                    pass

            book_parts.append(f"{label} {price_str}{money_flag}{ev_str}{be_str}")

        leg["books_row_ev"] = " | ".join(book_parts)

    return slip
'''

CHANGE_2_BATTER_ON_TEAM = '''
def _batter_on_team(batter_pick: dict, team_name: str) -> bool:
    """
    REBUILT: Uses slipiq_player_ids.is_batter_on_team() for reliable matching.
    Falls back to original string matching if player not in lookup table.
    """
    if not team_name:
        return False

    player_name = batter_pick.get("player", "")

    # Primary: ID lookup
    try:
        from slipiq_player_ids import is_batter_on_team, get_team
        result = is_batter_on_team(player_name, team_name)
        if result:
            return True
        # If lookup returns False AND player isn't in the table at all,
        # fall through to string matching
        player_team = get_team(player_name)
        if player_team is not None:
            return False  # In table but on different team
    except Exception:
        pass

    # Fallback: original string matching (for players not yet in lookup)
    batter_team = batter_pick.get("team", "") or batter_pick.get("home_team", "")
    team_lower  = team_name.lower()
    if not batter_team:
        return False
    team_words = [w for w in team_lower.split() if len(w) > 3]
    for word in team_words:
        if word in batter_team.lower():
            return True
    return False
'''

CHANGE_3_CARD_TO_SLIP_LEG = '''
def card_to_slip_leg(card: dict, leg_num: int = 1) -> dict:
    """
    Convert a pick card to a slip leg dict.
    UPDATED: includes ev, true_prob, pinnacle prices for montecarlo validation.
    """
    market   = card.get("market", "")
    prop_map = {
        "player_pitcher_strikeouts": "Strikeouts",
        "player_hits":               "Hits",
        "player_total_bases":        "Total Bases",
        "player_home_runs":          "Home Runs",
        "player_rbis":               "RBIs",
    }
    prop_label = prop_map.get(market, market.replace("player_", "").replace("_", " ").title())
    direction  = (card.get("direction") or "over").upper()
    line       = card.get("line")
    player     = card.get("player", "Unknown")

    return {
        "leg_num":       leg_num,
        "leg_type":      "pitcher_k" if "strikeout" in market else "batter",
        "player":        player,
        "market":        market,
        "prop":          f"{prop_label} {direction} {line}",
        "label":         f"{player} {prop_label} {direction} {line}",
        "game":          f"{card.get('away_team','?')} @ {card.get('home_team','?')}",
        "odds":          (card.get("best_book") or {}).get("price") or -115,
        "confidence":    card.get("confidence", 0),
        "grade":         card.get("grade", "?"),
        "trend":         card.get("trend", "flat"),
        "projection":    card.get("projection"),
        "direction":     direction.lower(),
        "line":          line,
        # NEW — needed by montecarlo and slip_router
        "ev":            card.get("ev"),
        "ev_confirmed":  card.get("ev_confirmed", False),
        "ev_source":     card.get("ev_source", "none"),
        "true_prob":     card.get("true_prob"),
        "pinnacle_over": card.get("pinnacle_over"),
        "pinnacle_under":card.get("pinnacle_under"),
        "home_team":     card.get("home_team"),
        "away_team":     card.get("away_team"),
        "books_display": card.get("books_display"),
    }
'''

CURSOR_INSTRUCTIONS = """
Apply these 3 changes to slipiq_book_slip_builder.py:

CHANGE 1: Replace the entire per_book_output() function with
CHANGE_1_PER_BOOK_OUTPUT above. This adds EV % and breakeven
to each book's line in the slip display.

CHANGE 2: Replace the entire _batter_on_team() function with
CHANGE_2_BATTER_ON_TEAM above. This uses slipiq_player_ids
for reliable matching instead of fuzzy string matching.

CHANGE 3: Replace the entire card_to_slip_leg() function with
CHANGE_3_CARD_TO_SLIP_LEG above. This adds ev, true_prob,
and pinnacle prices to the slip leg dict so montecarlo can
validate the slip before posting.

Do not change any other code in the file.
"""

print("Book slip builder patch spec loaded.")
print(CURSOR_INSTRUCTIONS)

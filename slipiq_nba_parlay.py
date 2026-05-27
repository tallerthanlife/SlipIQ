# slipiq_nba_parlay.py
# NBA correlated SGP engine — mirrors slipiq_ml_parlay patterns

PROP_SHORT = {
    "player_points": "PTS",
    "player_rebounds": "REB",
    "player_assists": "AST",
    "player_points_rebounds_assists": "PRA",
    "player_points_+_rebounds_+_assists": "PRA",
    "player_threes": "3PM",
    "player_steals": "STL",
    "player_blocks": "BLK",
}


def _game_key(card: dict) -> str:
    away = card.get("away_team") or "?"
    home = card.get("home_team") or "?"
    return f"{away}@{home}"


def _same_team(card_a: dict, card_b: dict) -> bool:
    ta = (card_a.get("team_abbr") or "").upper()
    tb = (card_b.get("team_abbr") or "").upper()
    if ta and tb:
        return ta == tb
    return _game_key(card_a) == _game_key(card_b)


def score_nba_game_package(star_pick: dict, teammate_picks: list[dict], game_context: dict | None = None) -> int:
    if not star_pick:
        return 0

    score = 0
    conf = float(star_pick.get("confidence") or 0)
    grade = star_pick.get("grade") or "C"
    ctx = game_context or {}

    if conf >= 82:
        score += 25
    elif conf >= 75:
        score += 18
    elif conf >= 68:
        score += 10

    if grade in ("A+", "A"):
        score += 15
    elif grade in ("B+", "B"):
        score += 8

    total = ctx.get("game_total")
    if total is not None:
        try:
            if float(total) >= 225:
                score += 12
            elif float(total) >= 218:
                score += 6
        except (TypeError, ValueError):
            pass

    spread = ctx.get("spread")
    if spread is not None:
        try:
            if abs(float(spread)) > 9:
                score -= 20
        except (TypeError, ValueError):
            pass

    if star_pick.get("b2b_flag"):
        score -= 12

    mins = star_pick.get("projected_minutes")
    if mins is not None:
        try:
            if float(mins) < 28:
                score -= 10
        except (TypeError, ValueError):
            pass

    hot_teammates = [t for t in teammate_picks if t.get("grade") in ("A+", "A", "B+")]
    score += min(12, len(hot_teammates) * 4)

    prop = (star_pick.get("market") or "").lower()
    if "point" in prop and (star_pick.get("direction") or "") == "over":
        score += 8

    return max(0, score)


def _leg_from_card(card: dict, leg_type: str = "player") -> dict:
    market = card.get("market") or card.get("prop_type") or "prop"
    short = PROP_SHORT.get(market, market.replace("player_", "").upper()[:6])
    direction = (card.get("direction") or "over").upper()
    player = card.get("player", "Unknown")
    line = card.get("line", 0)
    conf = card.get("confidence", 0)
    grade = card.get("grade", "B")

    return {
        "leg_type": leg_type,
        "game": f"{card.get('away_team', '?')} @ {card.get('home_team', '?')}",
        "team": card.get("team_abbr") or card.get("home_team", ""),
        "player": player,
        "label": f"{player} {short} {direction} {line}",
        "prop": f"{short} {direction} {line}",
        "odds": (card.get("best_book") or {}).get("price", -115),
        "note": f"Proj {card.get('projection')} | {conf}% | Grade {grade}",
        "confidence": conf,
        "grade": grade,
        "trend": card.get("trend", "flat"),
        "projection": card.get("projection", 0),
        "bookmaker": (card.get("best_book") or {}).get("book", "DraftKings"),
        "books_row": card.get("books_row", ""),
        "ev_confirmed": card.get("ev_confirmed", False),
        "score": conf,
        "_card": card,
    }


def build_nba_game_sgp(star_pick: dict, teammate_picks: list[dict], game_context: dict | None = None) -> list[dict]:
    legs = [_leg_from_card(star_pick, "star_prop")]
    team_mates = [
        t for t in teammate_picks
        if _same_team(t, star_pick) and t.get("player") != star_pick.get("player")
    ]
    team_mates.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    for mate in team_mates[:2]:
        legs.append(_leg_from_card(mate, "teammate_corr"))

    total = (game_context or {}).get("game_total")
    if total is not None and star_pick.get("direction") == "over":
        try:
            if float(total) >= 218:
                legs.append({
                    "leg_type": "game_total",
                    "game": legs[0]["game"],
                    "team": "",
                    "player": "Game Total",
                    "label": f"Game Total OVER {total}",
                    "prop": f"Total OVER {total}",
                    "odds": -110,
                    "note": "Pace/total correlation",
                    "confidence": max(60, star_pick.get("confidence", 0) - 5),
                    "grade": star_pick.get("grade", "B"),
                    "trend": "flat",
                    "projection": total,
                    "bookmaker": "DraftKings",
                    "books_row": "",
                    "ev_confirmed": False,
                    "score": star_pick.get("confidence", 0) - 5,
                })
        except (TypeError, ValueError):
            pass

    return legs


def _game_context_from_cards(cards: list[dict]) -> dict:
    ctx = {}
    for c in cards:
        internal = c.get("_internal") or {}
        if internal.get("spread") is not None:
            ctx.setdefault("spread", internal["spread"])
        pace = internal.get("projected_pace")
        if pace:
            ctx.setdefault("projected_pace", pace)
    return ctx


def build_nba_parlays(pool: list[dict], game_lines: list[dict] | None = None) -> dict | None:
    if not pool:
        return None

    game_lines = game_lines or []
    line_index = {}
    for gl in game_lines:
        key = f"{gl.get('away_team', '')}@{gl.get('home_team', '')}"
        line_index[key.lower()] = gl

    by_game: dict[str, list[dict]] = {}
    for card in pool:
        if card.get("sport") != "nba" and card.get("market", "").startswith("player_") is False:
            sport = card.get("sport")
            if sport and sport != "nba":
                continue
        gk = _game_key(card)
        by_game.setdefault(gk, []).append(card)

    game_packages = []
    for gk, cards in by_game.items():
        stars = sorted(cards, key=lambda x: x.get("confidence", 0), reverse=True)
        if not stars:
            continue
        star = stars[0]
        ctx = _game_context_from_cards(cards)
        gl = line_index.get(gk.lower())
        if gl:
            fg = gl.get("full_game") or {}
            if fg.get("total"):
                ctx["game_total"] = fg["total"]
            if fg.get("spread_home") is not None:
                ctx["spread"] = fg["spread_home"]

        score = score_nba_game_package(star, cards[1:], ctx)
        if score < 20:
            continue
        legs = build_nba_game_sgp(star, cards[1:], ctx)
        if len(legs) < 2:
            continue
        game_packages.append({"game": gk, "score": score, "legs": legs, "star": star.get("player")})

    if not game_packages:
        return None

    game_packages.sort(key=lambda x: x["score"], reverse=True)
    slip_1_legs = []
    seen_games = set()
    for pkg in game_packages[:3]:
        if pkg["game"] in seen_games:
            continue
        seen_games.add(pkg["game"])
        slip_1_legs.extend(pkg["legs"])

    grade_a = [c for c in pool if c.get("grade") in ("A+", "A")]
    grade_a.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    slip_2_legs = []
    seen_players = set()
    for card in grade_a[:8]:
        p = card.get("player")
        if p in seen_players:
            continue
        seen_players.add(p)
        slip_2_legs.append(_leg_from_card(card, "individual"))

    def _summary(legs):
        if not legs:
            return None
        return {
            "legs": legs,
            "total_legs": len(legs),
            "avg_conf": round(sum(l["confidence"] for l in legs) / len(legs), 1),
            "games": len(set(l["game"] for l in legs)),
            "leg_types": list({l["leg_type"] for l in legs}),
        }

    slip_1 = _summary(slip_1_legs)
    slip_2 = _summary(slip_2_legs)
    if not slip_1 and not slip_2:
        return None

    return {
        "slip_1": slip_1,
        "slip_2": slip_2,
        "game_packages": game_packages,
    }


if __name__ == "__main__":
    sample = [{
        "sport": "nba",
        "player": "Test Star",
        "away_team": "BOS",
        "home_team": "MIA",
        "team_abbr": "BOS",
        "market": "player_points",
        "line": 27.5,
        "direction": "over",
        "projection": 30.1,
        "confidence": 76,
        "grade": "A",
        "books_row": "DK -110 | Fan -108",
        "ev_confirmed": True,
    }]
    result = build_nba_parlays(sample)
    print("packages:", len(result["game_packages"]) if result else 0)

"""
SlipIQ ML Parlay Builder — SGP Correlation Engine
Builds two slips per day:

SLIP 1 — SGP Combo Slip
Per qualifying game: Pitcher K + correlated batters + F5 ML + F5 RL
Cross-game combination of best 2-3 game packages
No leg limit — more correlated games = bigger slip

SLIP 2 — Best Legs Slip
Individual high confidence Grade A picks
Standalone legs from any game
Sorted by confidence, top 6-8 legs

Correlation rules:
STACK:  Pitcher K OVER + F5 ML same team + F5 RL same team
STACK:  Pitcher K OVER + same team batter TB/Hits OVER
STACK:  F5 ML + F5 RL same team (same direction)
AVOID:  Same team pitcher K OVER + team total OVER (contradicts)
AVOID:  Opposing team batter TB OVER + pitcher K OVER (slight conflict)
NEVER:  Both sides of same game in same slip
"""

# ─── Correlation Scorer ───────────────────────────────────────

def score_game_package(pitcher_pick, same_team_batters, game_line):
    """
    Score a full game SGP package 0-100
    Higher = stronger correlated slip candidate
    """
    if not pitcher_pick:
        return 0

    score      = 0
    proj       = pitcher_pick.get("projection", 0)
    confidence = pitcher_pick.get("confidence", 0)
    trend      = pitcher_pick.get("trend", "NEUTRAL")
    grade      = pitcher_pick.get("grade", "C")
    fg         = game_line.get("full_game", {}) if game_line else {}
    tt         = game_line.get("team_totals", {}) if game_line else {}

    # Pitcher quality — core signal
    if proj >= 7.0:   score += 35
    elif proj >= 6.0: score += 25
    elif proj >= 5.0: score += 15
    elif proj >= 4.0: score += 8

    # Confidence
    if confidence >= 82:   score += 20
    elif confidence >= 75: score += 14
    elif confidence >= 68: score += 8

    # Trend
    if trend == "HOT":     score += 15
    elif trend == "NEUTRAL": score += 5
    elif trend == "COLD":  score -= 15

    # Grade
    if grade == "A":   score += 12
    elif grade == "B": score += 6

    # Batter correlation bonus
    hot_batters = [b for b in same_team_batters if b.get("grade") in ("A", "B")]
    score += min(15, len(hot_batters) * 5)

    # ML value
    home_ml = fg.get("ml_home")
    away_ml = fg.get("ml_away")
    if home_ml and away_ml:
        pitcher_team  = pitcher_pick.get("home_team", "").lower()
        game_home     = game_line.get("home_team", "").lower() if game_line else ""
        is_home       = pitcher_team and (
            pitcher_team[:5] in game_home or game_home[:5] in pitcher_team
        )
        pitcher_ml    = home_ml if is_home else away_ml

        if pitcher_ml:
            if -160 <= pitcher_ml <= -110:   score += 15
            elif -110 < pitcher_ml <= 120:   score += 8
            elif pitcher_ml < -160:          score += 3
            else:                            score -= 8

    # Opponent team total — lower = better
    opp_total = tt.get("away_total") if True else tt.get("home_total")
    if opp_total:
        if opp_total <= 3.5:   score += 15
        elif opp_total <= 4.0: score += 10
        elif opp_total <= 4.5: score += 5
        else:                  score -= 5

    return max(0, score)


# ─── Per-Game SGP Builder ─────────────────────────────────────

def build_game_sgp(pitcher_pick, batter_picks, game_line):
    """
    Build a full SGP package for one game
    Returns list of correlated legs
    """
    legs = []
    if not pitcher_pick or not game_line:
        return legs

    fg         = game_line.get("full_game", {})
    tt         = game_line.get("team_totals", {})
    home       = game_line.get("home_team", "Home")
    away       = game_line.get("away_team", "Away")
    book       = game_line.get("bookmaker", "DraftKings")
    proj       = pitcher_pick.get("projection", 0)
    confidence = pitcher_pick.get("confidence", 0)
    trend      = pitcher_pick.get("trend", "NEUTRAL")
    grade      = pitcher_pick.get("grade", "B")

    # Determine pitcher's team
    pitcher_home = pitcher_pick.get("home_team", "").lower()
    game_home    = home.lower()
    is_home      = pitcher_home and (
        pitcher_home[:5] in game_home or game_home[:5] in pitcher_home
    )
    team_name    = home if is_home else away
    opp_name     = away if is_home else home
    ml_odds      = fg.get("ml_home") if is_home else fg.get("ml_away")
    opp_total    = fg.get("away_total") if is_home else fg.get("home_total")

    # ── Leg 1: Pitcher K ──────────────────────────────────────
    direction = "OVER" if "OVER" in pitcher_pick.get("recommendation", "") else "UNDER"
    legs.append({
        "leg_type":   "pitcher_k",
        "game":       f"{away} @ {home}",
        "team":       team_name,
        "player":     pitcher_pick["pitcher"],
        "label":      f"{pitcher_pick['pitcher']} K {direction} {pitcher_pick['line']}",
        "prop":       f"Strikeouts {direction} {pitcher_pick['line']}",
        "odds":       -115,
        "note":       f"Proj {proj}K | {trend} | {confidence}%",
        "confidence": confidence,
        "grade":      grade,
        "trend":      trend,
        "projection": proj,
        "bookmaker":  book,
        "score":      confidence,
    })

    # ── Leg 2 + 3: Same-team batters (correlated with team winning) ──
    # Take top 2 same-team batters sorted by confidence
    team_batters = [
        b for b in batter_picks
        if b.get("grade") in ("A", "B")
        and _batter_on_team(b, team_name)
    ]
    team_batters.sort(key=lambda x: x["confidence"], reverse=True)

    for batter in team_batters[:2]:
        prop_labels = {
            "hits":         "Hits",
            "total_bases":  "Total Bases",
            "rbi":          "RBI",
            "runs":         "Runs",
            "home_runs":    "Home Runs",
        }
        b_direction = "OVER" if "OVER" in batter.get("recommendation", "") else "UNDER"
        prop_label  = prop_labels.get(batter["prop_type"], batter["prop_type"])
        legs.append({
            "leg_type":   "batter_corr",
            "game":       f"{away} @ {home}",
            "team":       team_name,
            "player":     batter["batter"],
            "label":      f"{batter['batter']} {prop_label} {b_direction} {batter['line']}",
            "prop":       f"{prop_label} {b_direction} {batter['line']}",
            "odds":       -115,
            "note":       f"Proj {batter['projection']} | {batter['confidence']}% | Correlated",
            "confidence": batter["confidence"],
            "grade":      batter.get("grade", "B"),
            "trend":      batter.get("trend", "NEUTRAL"),
            "projection": batter["projection"],
            "bookmaker":  batter.get("bookmaker", "DraftKings"),
            "score":      batter["confidence"],
        })

    # ── Leg 4: F5 ML ──────────────────────────────────────────
    if ml_odds:
        legs.append({
            "leg_type":   "f5_ml",
            "game":       f"{away} @ {home}",
            "team":       team_name,
            "player":     team_name,
            "label":      f"{team_name} F5 ML",
            "prop":       "First 5 Innings ML",
            "odds":       ml_odds,
            "note":       f"Correlated with {pitcher_pick['pitcher']} K OVER",
            "confidence": confidence - 3,
            "grade":      grade,
            "trend":      trend,
            "projection": proj,
            "bookmaker":  book,
            "score":      confidence - 3,
        })

    # ── Leg 5: F5 RL (only if pitcher confidence >= 75%) ──────
    if confidence >= 75 and ml_odds and ml_odds < 0:
        rl_odds = ml_odds + 40 if ml_odds else -150
        legs.append({
            "leg_type":   "f5_rl",
            "game":       f"{away} @ {home}",
            "team":       team_name,
            "player":     team_name,
            "label":      f"{team_name} F5 RL -0.5",
            "prop":       "First 5 Innings Run Line -0.5",
            "odds":       rl_odds,
            "note":       f"Strong pitcher correlation | {proj}K proj",
            "confidence": confidence - 8,
            "grade":      grade,
            "trend":      trend,
            "projection": proj,
            "bookmaker":  book,
            "score":      confidence - 8,
        })

    # ── Leg 6: Opponent team total UNDER ──────────────────────
    # Only add if pitcher is HOT or high confidence
    if opp_total and (trend == "HOT" or confidence >= 78):
        opp_total_odds = tt.get("away_under") if is_home else tt.get("home_under")
        legs.append({
            "leg_type":   "opp_total_under",
            "game":       f"{away} @ {home}",
            "team":       opp_name,
            "player":     opp_name,
            "label":      f"{opp_name} Team Total UNDER {opp_total}",
            "prop":       f"Team Total Under {opp_total}",
            "odds":       opp_total_odds or -115,
            "note":       f"Correlated with {pitcher_pick['pitcher']} dominance",
            "confidence": confidence - 5,
            "grade":      grade,
            "trend":      trend,
            "projection": proj,
            "bookmaker":  book,
            "score":      confidence - 5,
        })

    return legs


def _batter_on_team(batter_pick, team_name):
    """Check if batter plays for the given team"""
    if not team_name:
        return False
    batter_team = batter_pick.get("team", "").lower()
    team_lower  = team_name.lower()
    if not batter_team:
        return False
    # Try partial match on team name
    team_words = [w for w in team_lower.split() if len(w) > 3]
    for word in team_words:
        if word in batter_team:
            return True
    return False


# ─── Cross-Game Combination ───────────────────────────────────

def combine_game_packages(game_packages):
    """
    Combine per-game SGP packages into cross-game slips
    Slip 1: Top 2-3 game SGPs combined
    Returns list of all legs for the combined slip
    """
    if not game_packages:
        return []

    # Sort packages by score
    sorted_packages = sorted(game_packages, key=lambda x: x["score"], reverse=True)

    # Take top 3 game packages for Slip 1
    top_packages = sorted_packages[:3]
    combined_legs = []
    seen_games    = set()

    for pkg in top_packages:
        game = pkg["game"]
        if game in seen_games:
            continue
        seen_games.add(game)
        combined_legs.extend(pkg["legs"])

    return combined_legs


# ─── Main Builder ─────────────────────────────────────────────

def build_ml_parlays(pitcher_picks, game_lines, batter_picks=None):
    """
    Build two slips:
    Slip 1 — SGP Combo: per-game correlated packages combined cross-game
    Slip 2 — Best Legs: top individual Grade A picks by confidence

    Returns dict with slip_1, slip_2
    """
    if not pitcher_picks or not game_lines:
        return None

    batter_picks = batter_picks or []

    # Index game lines by team name
    game_index = {}
    for gl in game_lines:
        home = gl.get("home_team", "").lower()
        away = gl.get("away_team", "").lower()
        game_index[home] = gl
        game_index[away] = gl
        # Also index by first 5 chars for fuzzy match
        if home:
            game_index[home[:5]] = gl
        if away:
            game_index[away[:5]] = gl

    # Only use strikeout picks for SGP
    k_picks = [
        p for p in pitcher_picks
        if p.get("prop_type") == "Strikeouts"
        and p.get("grade") in ("A", "B")
    ]

    # ── Build per-game packages ───────────────────────────────
    game_packages = []
    used_games    = set()

    for pick in k_picks:
        home_team = pick.get("home_team", "").lower()
        away_team = pick.get("away_team", "").lower()

        # Find matching game line
        game_line = (
            game_index.get(home_team) or
            game_index.get(away_team) or
            game_index.get(home_team[:5] if home_team else "") or
            game_index.get(away_team[:5] if away_team else "")
        )

        if not game_line:
            continue

        game_key = f"{game_line.get('away_team', '')}@{game_line.get('home_team', '')}"
        if game_key in used_games:
            continue

        score = score_game_package(pick, batter_picks, game_line)
        if score < 25:
            continue

        legs = build_game_sgp(pick, batter_picks, game_line)
        if not legs:
            continue

        game_packages.append({
            "game":     game_key,
            "score":    score,
            "legs":     legs,
            "pitcher":  pick["pitcher"],
            "proj":     pick.get("projection", 0),
            "conf":     pick.get("confidence", 0),
        })
        used_games.add(game_key)

    if not game_packages:
        return None

    # ── Slip 1: SGP Combo ─────────────────────────────────────
    slip_1_legs = combine_game_packages(game_packages)

    # ── Slip 2: Best Individual Legs ─────────────────────────
    # All Grade A pitcher picks + top batter picks sorted by confidence
    grade_a_pitchers = [
        p for p in pitcher_picks
        if p.get("grade") == "A"
    ]
    grade_a_batters = [
        b for b in batter_picks
        if b.get("grade") == "A"
    ]

    # Build individual legs
    slip_2_legs = []
    seen_players = set()

    all_individuals = sorted(
        grade_a_pitchers + grade_a_batters,
        key=lambda x: x.get("confidence", 0),
        reverse=True,
    )

    for item in all_individuals[:10]:
        player = item.get("pitcher") or item.get("batter", "")
        if player in seen_players:
            continue
        seen_players.add(player)

        direction = "OVER" if "OVER" in item.get("recommendation", "") else "UNDER"
        prop_type = item.get("prop_type", "Strikeouts")
        line      = item.get("line", 0)
        conf      = item.get("confidence", 0)
        grade     = item.get("grade", "A")
        trend     = item.get("trend", "NEUTRAL")
        proj      = item.get("projection", 0)

        prop_labels = {
            "Strikeouts":    "K",
            "Outs Recorded": "Outs",
            "Hits Allowed":  "HA",
            "Runs Allowed":  "RA",
            "hits":          "H",
            "total_bases":   "TB",
            "rbi":           "RBI",
            "runs":          "R",
            "home_runs":     "HR",
        }
        prop_short = prop_labels.get(prop_type, prop_type)

        slip_2_legs.append({
            "leg_type":   "individual",
            "game":       item.get("home_team", "") + "@" + item.get("away_team", ""),
            "team":       item.get("home_team", ""),
            "player":     player,
            "label":      f"{player} {prop_short} {direction} {line}",
            "prop":       f"{prop_type} {direction} {line}",
            "odds":       -115,
            "note":       f"Proj {proj} | {conf}% | Grade A",
            "confidence": conf,
            "grade":      grade,
            "trend":      trend,
            "projection": proj,
            "bookmaker":  item.get("bookmaker", "SportsData"),
            "score":      conf,
        })

    def parlay_summary(legs):
        if not legs:
            return None
        avg_conf = round(sum(l["confidence"] for l in legs) / len(legs), 1)
        games    = len(set(l["game"] for l in legs))
        types    = list(set(l["leg_type"] for l in legs))
        return {
            "legs":       legs,
            "total_legs": len(legs),
            "avg_conf":   avg_conf,
            "games":      games,
            "leg_types":  types,
        }

    slip_1 = parlay_summary(slip_1_legs)
    slip_2 = parlay_summary(slip_2_legs)

    if not slip_1 and not slip_2:
        return None

    return {
        "slip_1":     slip_1,
        "slip_2":     slip_2,
        "total_legs": (slip_1["total_legs"] if slip_1 else 0) + (slip_2["total_legs"] if slip_2 else 0),
        "game_packages": game_packages,
    }


# ─── Discord Embeds ───────────────────────────────────────────

def build_ml_parlay_embeds(ml_parlays):
    """Build Discord embeds — Slip 1 SGP Combo + Slip 2 Best Legs"""
    import discord

    if not ml_parlays:
        return []

    embeds = []

    slip_configs = [
        ("slip_1", "🎯 Slip 1 — SGP Combo", 0x00FF88,
         "Correlated legs per game — pitcher + batters + F5 ML + F5 RL"),
        ("slip_2", "⭐ Slip 2 — Best Individual Legs", 0x3399FF,
         "Top Grade A picks by confidence — standalone high hit rate legs"),
    ]

    for key, title, color, subtitle in slip_configs:
        slip = ml_parlays.get(key)
        if not slip:
            continue

        legs = slip["legs"]

        # Group legs by game for display
        games_seen = {}
        for leg in legs:
            game = leg["game"]
            if game not in games_seen:
                games_seen[game] = []
            games_seen[game].append(leg)

        leg_text  = ""
        type_map  = {
            "pitcher_k":      "⚾",
            "batter_corr":    "🏏",
            "f5_ml":          "🎰",
            "f5_rl":          "📊",
            "opp_total_under":"📉",
            "individual":     "🔥",
        }
        grade_e = {"A": "🔥", "B": "✅"}.get
        trend_e = {"HOT": "📈", "COLD": "📉", "NEUTRAL": "➡️"}.get

        for game, game_legs in games_seen.items():
            if key == "slip_1":
                leg_text += f"\n**{game}**\n"
            for leg in game_legs:
                type_icon = type_map.get(leg["leg_type"], "📊")
                ge        = grade_e(leg.get("grade", "B"), "✅")
                te        = trend_e(leg.get("trend", "NEUTRAL"), "➡️")
                odds_str  = (
                    f"{'+' if leg['odds'] > 0 else ''}{int(leg['odds'])}"
                    if leg.get("odds") else "—"
                )
                leg_text += (
                    f"{type_icon}{ge} **{leg['label']}** {odds_str} {te}\n"
                    f"  ↳ {leg['note']}\n"
                )

        # Trim to Discord limit
        if len(leg_text) > 3800:
            leg_text = leg_text[:3800] + "\n*...truncated*"

        embed = discord.Embed(
            title=f"{title} — {slip['total_legs']} Legs",
            description=f"*{subtitle}*\nAvg confidence: **{slip['avg_conf']}%** | {slip['games']} games",
            color=color,
        )
        embed.add_field(
            name="Legs",
            value=leg_text.strip() or "No legs",
            inline=False,
        )

        # Type breakdown
        type_counts = {}
        for leg in legs:
            lt = leg["leg_type"]
            type_counts[lt] = type_counts.get(lt, 0) + 1

        breakdown = " | ".join(
            f"{type_map.get(lt, '📊')} {count}"
            for lt, count in type_counts.items()
        )
        embed.add_field(name="Breakdown", value=breakdown or "—", inline=True)
        embed.add_field(
            name="📡 Books",
            value="DraftKings · Fanatics · PrizePicks — verify lines before submitting",
            inline=False,
        )
        embed.set_footer(text="SlipIQ • SGP Correlation Engine")
        embeds.append(embed)

    return embeds


# ─── Format Text ──────────────────────────────────────────────

def format_ml_parlays_text(ml_parlays):
    """Format both slips as plain text for testing"""
    if not ml_parlays:
        return "No ML parlays built today"

    lines = []

    for key, label in [("slip_1", "SGP Combo"), ("slip_2", "Best Legs")]:
        slip = ml_parlays.get(key)
        if not slip:
            continue

        lines.append(
            f"\n{'='*50}\n"
            f"Slip {label} — {slip['total_legs']} legs | {slip['avg_conf']}% avg conf"
        )

        current_game = None
        for leg in slip["legs"]:
            if leg["game"] != current_game and key == "slip_1":
                current_game = leg["game"]
                lines.append(f"\n  [{leg['game']}]")
            odds_str = (
                f"{'+' if leg['odds'] > 0 else ''}{int(leg['odds'])}"
                if leg.get("odds") else "—"
            )
            lines.append(f"  {leg['label']} {odds_str} | {leg['note']}")

    return "\n".join(lines) if lines else "No ML parlays built today"


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ ML Parlay Test ===\n")

    from slipiq_batter_lines import run_batter_analysis
    from slipiq_parlayapi import fetch_odds_raw, SPORT_MLB
    from slipiq_pitcher_model import run_pitcher_model

    print("Pulling pitcher picks...")
    pitcher_picks = run_pitcher_model()

    print("\nPulling batter picks...")
    batter_picks = run_batter_analysis()

    print("\nPulling game lines (ParlayAPI /odds)...")
    game_lines = fetch_odds_raw(SPORT_MLB) or []

    print("\nBuilding ML parlays...")
    ml_parlays = build_ml_parlays(pitcher_picks, game_lines, batter_picks)

    if ml_parlays:
        print(format_ml_parlays_text(ml_parlays))
        print(f"\nGame packages scored: {len(ml_parlays.get('game_packages', []))}")
    else:
        print("No ML parlays built — need fresh lines")
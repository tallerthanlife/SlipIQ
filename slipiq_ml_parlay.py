"""
SlipIQ ML Parlay Builder — SGP Correlation Engine
Builds two slips per day:

SLIP 1 — SGP Combo Slip
Per qualifying game: Pitcher K + correlated batters + F5 ML + F5 RL
Cross-game combination of best 2-3 game packages

SLIP 2 — Best Legs Slip
Individual high confidence Grade A picks
Standalone legs from any game
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
    trend      = pitcher_pick.get("trend", "flat")
    grade      = pitcher_pick.get("grade", "C")
    fg         = game_line.get("full_game", {}) if game_line else {}
    tt         = game_line.get("team_totals", {}) if game_line else {}

    # Determine home/away logic upfront to fix the "if True" bug
    pitcher_team  = pitcher_pick.get("home_team", "").lower()
    game_home     = game_line.get("home_team", "").lower() if game_line else ""
    is_home       = pitcher_team and (
        pitcher_team[:5] in game_home or game_home[:5] in pitcher_team
    )

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
    if trend == "hot":     score += 15
    elif trend == "flat":  score += 5
    elif trend == "cold":  score -= 15

    # Grade
    if grade == "A":   score += 12
    elif grade == "B": score += 6

    # Batter correlation bonus
    hot_batters = [b for b in same_team_batters if b.get("grade") in ("A", "B", "B+")]
    score += min(15, len(hot_batters) * 5)

    # ML value
    home_ml = fg.get("ml_home")
    away_ml = fg.get("ml_away")
    if home_ml and away_ml:
        pitcher_ml = home_ml if is_home else away_ml

        if pitcher_ml:
            if -160 <= pitcher_ml <= -110:   score += 15
            elif -110 < pitcher_ml <= 120:   score += 8
            elif pitcher_ml < -160:          score += 3
            else:                            score -= 8

    # Opponent team total — lower = better
    opp_total = tt.get("away_total") if is_home else tt.get("home_total")
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
    trend      = pitcher_pick.get("trend", "flat")
    grade      = pitcher_pick.get("grade", "B")
    player     = pitcher_pick.get("player", "Unknown Pitcher")

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
    direction = pitcher_pick.get("direction", "over").upper()
    legs.append({
        "leg_type":   "pitcher_k",
        "game":       f"{away} @ {home}",
        "team":       team_name,
        "player":     player,
        "label":      f"{player} K {direction} {pitcher_pick.get('line')}",
        "prop":       f"Strikeouts {direction} {pitcher_pick.get('line')}",
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
    team_batters = [
        b for b in batter_picks
        if b.get("grade") in ("A", "A+", "B+", "B")
        and _batter_on_team(b, team_name)
    ]
    team_batters.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    for batter in team_batters[:2]:
        prop_labels = {
            "player_hits":        "Hits",
            "player_total_bases": "Total Bases",
            "player_rbis":        "RBI",
            "player_runs":        "Runs",
            "player_home_runs":   "Home Runs",
        }
        market_key  = batter.get("market", "")
        b_direction = batter.get("direction", "over").upper()
        prop_label  = prop_labels.get(market_key, market_key)
        b_player    = batter.get("player", "Unknown Batter")
        
        legs.append({
            "leg_type":   "batter_corr",
            "game":       f"{away} @ {home}",
            "team":       team_name,
            "player":     b_player,
            "label":      f"{b_player} {prop_label} {b_direction} {batter.get('line')}",
            "prop":       f"{prop_label} {b_direction} {batter.get('line')}",
            "odds":       -115,
            "note":       f"Proj {batter.get('projection')} | {batter.get('confidence')}% | Correlated",
            "confidence": batter.get("confidence", 0),
            "grade":      batter.get("grade", "B"),
            "trend":      batter.get("trend", "flat"),
            "projection": batter.get("projection", 0),
            "bookmaker":  batter.get("best_book", {}).get("book", book),
            "score":      batter.get("confidence", 0),
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
            "note":       f"Correlated with {player} K OVER",
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
    if opp_total and (trend == "hot" or confidence >= 78):
        opp_total_odds = tt.get("away_under") if is_home else tt.get("home_under")
        legs.append({
            "leg_type":   "opp_total_under",
            "game":       f"{away} @ {home}",
            "team":       opp_name,
            "player":     opp_name,
            "label":      f"{opp_name} Team Total UNDER {opp_total}",
            "prop":       f"Team Total Under {opp_total}",
            "odds":       opp_total_odds or -115,
            "note":       f"Correlated with {player} dominance",
            "confidence": confidence - 5,
            "grade":      grade,
            "trend":      trend,
            "projection": proj,
            "bookmaker":  book,
            "score":      confidence - 5,
        })

    # ── Leg: F3 ML (compare against F5 — pick higher EV) ─────
    f3_ml_odds  = None
    f5_ml_odds  = ml_odds

    f3_data = game_line.get("f3", {}) or {}
    if f3_data:
        f3_ml_odds = f3_data.get("ml_home") if is_home else f3_data.get("ml_away")

    best_fn_market = "f5_ml"
    best_fn_odds   = f5_ml_odds

    if f3_ml_odds and f5_ml_odds:
        try:
            from slipiq_ev_engine import no_vig_prob
            pin_home = game_line.get("pinnacle_home_ml")
            pin_away = game_line.get("pinnacle_away_ml")
            if pin_home and pin_away:
                nv = no_vig_prob(
                    pin_home if is_home else pin_away,
                    pin_away if is_home else pin_home,
                )
                true_prob_ml = nv["true_over"]
                ev_f5 = (true_prob_ml * (1 + abs(f5_ml_odds)/100 if f5_ml_odds < 0
                         else 1 + f5_ml_odds/100)) - 1
                ev_f3 = (true_prob_ml * (1 + abs(f3_ml_odds)/100 if f3_ml_odds < 0
                         else 1 + f3_ml_odds/100)) - 1
                if ev_f3 > ev_f5 + 0.005:
                    best_fn_market = "f3_ml"
                    best_fn_odds   = f3_ml_odds
        except Exception:
            pass
    elif f3_ml_odds and not f5_ml_odds:
        best_fn_market = "f3_ml"
        best_fn_odds   = f3_ml_odds

    for leg in legs:
        if leg.get("leg_type") in ("f5_ml", "f3_ml"):
            leg["leg_type"] = best_fn_market
            leg["label"]    = leg["label"].replace("F5 ML", best_fn_market.upper().replace("_", " "))
            leg["prop"]     = leg["prop"].replace("First 5", "First 3" if best_fn_market == "f3_ml" else "First 5")
            leg["odds"]     = best_fn_odds or leg["odds"]
            break

    return legs


def _batter_on_team(batter_pick, team_name):
    """Check if batter plays for the given team"""
    if not team_name:
        return False
    # API data usually provides team in 'team' or 'home_team' if playing home
    batter_team = batter_pick.get("team", "") or batter_pick.get("home_team", "")
    team_lower  = team_name.lower()
    if not batter_team:
        return False
        
    team_words = [w for w in team_lower.split() if len(w) > 3]
    for word in team_words:
        if word in batter_team.lower():
            return True
    return False


# ─── Cross-Game Combination ───────────────────────────────────

def _validate_sgp_package(legs: list[dict]) -> dict:
    """
    Validate a same-game SGP package via Monte Carlo before posting.
    Returns quick_validate_parlay() result: {"go": bool, "reason": str, ...}

    Uses leg_type to look up correlation matrix.
    Falls back to independent simulation if montecarlo import fails.
    """
    try:
        from slipiq_montecarlo import quick_validate_parlay

        leg_probs = []
        leg_types = []
        for leg in legs:
            tp = (
                leg.get("true_prob") or
                (leg.get("confidence", 60) / 100.0)
            )
            leg_probs.append(max(0.35, min(0.95, float(tp))))
            leg_types.append(leg.get("leg_type", "unknown"))

        if not leg_probs:
            return {"go": False, "reason": "no legs"}

        approx_decimal = 1.0
        for leg in legs:
            odds_american = leg.get("odds") or -115
            if odds_american > 0:
                approx_decimal *= 1 + (odds_american / 100)
            else:
                approx_decimal *= 1 + (100 / abs(odds_american))

        result = quick_validate_parlay(
            leg_probs      = leg_probs,
            leg_types      = leg_types,
            payout_decimal = approx_decimal,
            bankroll       = 500.0,
        )
        return result

    except Exception:
        joint = 1.0
        for leg in legs:
            tp = leg.get("true_prob") or (leg.get("confidence", 60) / 100.0)
            joint *= float(tp)
        if joint >= 0.05:
            return {"go": True, "reason": f"joint_prob={joint:.3f} (fallback)", "ev": joint - 1}
        return {"go": False, "reason": f"joint_prob={joint:.3f} too low (fallback)"}


def combine_game_packages(game_packages):
    if not game_packages:
        return []
    sorted_packages = sorted(game_packages, key=lambda x: x["score"], reverse=True)
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
    if not pitcher_picks or not game_lines:
        return None

    batter_picks = batter_picks or []
    game_index = {}
    for gl in game_lines:
        home = gl.get("home_team", "").lower()
        away = gl.get("away_team", "").lower()
        game_index[home] = gl
        game_index[away] = gl
        if home: game_index[home[:5]] = gl
        if away: game_index[away[:5]] = gl

    k_picks = [
        p for p in pitcher_picks
        if p.get("market") == "player_strikeouts"
        and p.get("grade") in ("A+", "A", "B+", "B")
    ]

    game_packages = []
    used_games    = set()

    for pick in k_picks:
        home_team = pick.get("home_team", "").lower()
        away_team = pick.get("away_team", "").lower()

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

        # ── Monte Carlo gate (replaces vibes score gate) ───────
        mc_valid = _validate_sgp_package(legs)
        if not mc_valid["go"]:
            print(f"  [ml_parlay] {pick.get('player')} SGP rejected: {mc_valid['reason']}")
            continue

        game_packages.append({
            "game":    game_key,
            "score":   score,
            "legs":    legs,
            "pitcher": pick.get("player"),
            "proj":    pick.get("projection", 0),
            "conf":    pick.get("confidence", 0),
            "mc":      mc_valid,
        })
        used_games.add(game_key)

    if not game_packages:
        return None

    slip_1_legs = combine_game_packages(game_packages)

    grade_a_pitchers = [p for p in pitcher_picks if p.get("grade") in ("A+", "A")]
    grade_a_batters = [b for b in batter_picks if b.get("grade") in ("A+", "A")]

    slip_2_legs = []
    seen_players = set()

    all_individuals = sorted(
        grade_a_pitchers + grade_a_batters,
        key=lambda x: x.get("confidence", 0),
        reverse=True,
    )

    for item in all_individuals[:10]:
        player = item.get("player", "")
        if player in seen_players:
            continue
        seen_players.add(player)

        direction = item.get("direction", "over").upper()
        prop_type = item.get("market", "player_strikeouts")
        line      = item.get("line", 0)
        conf      = item.get("confidence", 0)
        grade     = item.get("grade", "A")
        trend     = item.get("trend", "flat")
        proj      = item.get("projection", 0)

        prop_labels = {
            "player_strikeouts":  "K",
            "player_outs":        "Outs",
            "player_hits":        "H",
            "player_total_bases": "TB",
            "player_rbis":        "RBI",
            "player_runs":        "R",
            "player_home_runs":   "HR",
        }
        prop_short = prop_labels.get(prop_type, prop_type.replace("player_", ""))

        slip_2_legs.append({
            "leg_type":   "individual",
            "game":       item.get("home_team", "") + "@" + item.get("away_team", ""),
            "team":       item.get("home_team", ""),
            "player":     player,
            "label":      f"{player} {prop_short} {direction} {line}",
            "prop":       f"{prop_type} {direction} {line}",
            "odds":       -115,
            "note":       f"Proj {proj} | {conf}% | Grade {grade}",
            "confidence": conf,
            "grade":      grade,
            "trend":      trend,
            "projection": proj,
            "bookmaker":  item.get("best_book", {}).get("book", "SportsData"),
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
    import discord

    if not ml_parlays:
        return []

    embeds = []

    # Updated colors to Electric Gold and Royal Purple
    slip_configs = [
        ("slip_1", "🎯 Slip 1 — SGP Combo", 0xFFD700,
         "Correlated legs per game — pitcher + batters + F5 ML + F5 RL"),
        ("slip_2", "⭐ Slip 2 — Best Individual Legs", 0x6A0DAD,
         "Top Grade A picks by confidence — standalone high hit rate legs"),
    ]

    for key, title, color, subtitle in slip_configs:
        slip = ml_parlays.get(key)
        if not slip:
            continue

        legs = slip["legs"]
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
        grade_e = {"A+": "🔥", "A": "🔥", "B+": "✅", "B": "✅"}.get
        trend_e = {"hot": "📈", "cold": "📉", "flat": "➡️"}.get

        for game, game_legs in games_seen.items():
            if key == "slip_1":
                leg_text += f"\n**{game}**\n"
            for leg in game_legs:
                type_icon = type_map.get(leg["leg_type"], "📊")
                ge        = grade_e(leg.get("grade", "B"), "✅")
                te        = trend_e(leg.get("trend", "flat"), "➡️")
                odds_str  = (
                    f"{'+' if leg['odds'] > 0 else ''}{int(leg['odds'])}"
                    if leg.get("odds") else "—"
                )
                leg_text += (
                    f"{type_icon}{ge} **{leg['label']}** {odds_str} {te}\n"
                    f"  ↳ {leg['note']}\n"
                )

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
    
    # Text output remains exactly the same
    lines = []
    for key, label in [("slip_1", "SGP Combo"), ("slip_2", "Best Legs")]:
        slip = ml_parlays.get(key)
        if not slip: continue

        lines.append(f"\n{'='*50}\nSlip {label} — {slip['total_legs']} legs | {slip['avg_conf']}% avg conf")
        current_game = None
        for leg in slip["legs"]:
            if leg["game"] != current_game and key == "slip_1":
                current_game = leg["game"]
                lines.append(f"\n  [{leg['game']}]")
            odds_str = f"{'+' if leg['odds'] > 0 else ''}{int(leg['odds'])}" if leg.get("odds") else "—"
            lines.append(f"  {leg['label']} {odds_str} | {leg['note']}")

    return "\n".join(lines) if lines else "No ML parlays built today"


# ─── Test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== SlipIQ ML Parlay Test ===\n")

    # FIX: Import from the batter model file we just rebuilt
    from slipiq_batter_model import run_batter_model as run_batter_analysis
    from slipiq_parlayapi import fetch_odds_raw, SPORT_MLB
    from slipiq_pitcher_model import run_pitcher_model

    print("Pulling pitcher picks...")
    pitcher_picks = run_pitcher_model()

    print("\nPulling batter picks...")
    batter_picks = run_batter_analysis(min_confidence=50) # Added threshold
    
    # ... rest of the test block remains the same
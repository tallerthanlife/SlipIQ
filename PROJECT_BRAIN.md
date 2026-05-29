# PROJECT_BRAIN.md — SlipIQ Master Context Document
> Generated: May 25, 2026 | For use in Cursor / Claude Code / any new AI session
> Read this entire file before touching a single line of code.

---

## 1. SYSTEM ARCHITECTURE AND DATA FLOW

### MLB Pipeline — End-to-End Data Flow

```
[MLB Stats API / pybaseball / Baseball Savant]
        ↓
slipiq_mlb_data.py
  pull_todays_data()          ← confirmed starters, game_pk, opponent
  get_pitcher_savant()        ← SwStr%, K/9, velo, xFIP, pitch mix
  get_pitcher_game_logs()     ← last 5-10 starts (K count, outs, pitches)
  get_team_k_rate()           ← opponent K rate vs RHP/LHP
  get_batter_last_14()        ← hits/TB/RBI rate last 14 days
  get_opposing_starter_data() ← FIP, ERA last 3, SwStr% of opp pitcher
        ↓
slipiq_pitcher_model.py
  run_pitcher_model(name, line)
    → Negative binomial distribution over last-10-start K counts
    → adj_floor, adj_ceiling, projection (weighted mean)
    → confidence = P(outcome > line) expressed as percentage
    → trend = HOT / COLD / NEUTRAL based on recent 3 vs season SwStr%
  run_all_models(pitchers_list) ← slate-level loop
        ↓
slipiq_batter_model.py
  model_batter_hits()
  model_batter_total_bases()
  model_batter_rbi()
  classify_game(home_pitcher_model, away_pitcher_model)
    → DUEL / MISMATCH / BOTH_WEAK
    → pitcher_score() = 0-100 composite: K floor (40pts) + SwStr% (35pts) + recent bonus (15pts) + conf (10pts)
    → ELITE_THRESHOLD = 65 | WEAK_THRESHOLD = 45
  select_game_legs(classification, game_lines)
    → Returns F5 ML, F3 RL (with per-book availability flag)
        ↓
slipiq_odds_api.py
  pull_all_lines()
    → The Odds API: baseball_mlb/events → event IDs
    → Per-event: pitcher_strikeouts market
    → Also pulls: f5_ml, f3_rl (with available flag), team totals
    → Preferred books: DraftKings → FanDuel → Fanatics → Pinnacle
    → MAX_EVENTS = env var ODDS_MAX_EVENTS (default 15)
  get_pinnacle_no_vig_line()   ← sharp reference price
        ↓
slipiq_curate.py
  run_curation(models, pickem_lines)
    → Filters: MIN_CONFIDENCE (env SLIPIQ_MIN_CONF, floor=55)
    →          MIN_EDGE (env SLIPIQ_MIN_EDGE, floor=0.4)
    → Enriches each pick with: best_platform, best_line, best_book, best_price
    → Classifies into: pickem (PrizePicks/Underdog) vs sportsbook legs
  curate_batters(batter_game_data) ← separate pass for batter props
        ↓
slipiq_grading.py  [SHARED — used by MLB + NBA]
  calc_grade(confidence, hit_rate)
    → A+ : conf ≥ 85 | A: conf ≥ 72 | B+: conf ≥ 65 | B: conf ≥ 58 | C: else
        ↓
slipiq_confidence_agent.py
  enrich_picks(picks)
    → Calls Groq API (not Claude API — zero cost for this step)
    → Scores each pick 0-100 using: SwStr%, edge magnitude, trend, opp K rate
    → Merges Groq score with model confidence → final enriched confidence %
    → NEVER caps at fewer than 5 picks (guard: top_n >= 5 check)
        ↓
slipiq_book_slip_builder.py
  build_pitcher_slip(pitcher_model, game_lines)
    → 3-4 leg correlated pitcher core: K over + outs over + F5 ML + F3 RL
  build_full_slip(pitcher_slip, batter_model)
    → Adds qualified batter hits/TB/RBI legs
  per_book_output(slip, books=["draftkings","fanatics","fanduel"])
    → Shows ALL THREE books side-by-side per leg
    → format: DK | Fanatics | FanDuel with prices
    → 💰 flag on any plus-money line
  availability_check(slip)
    → Drops F3 RL leg if not on that book — NEVER substitutes
        ↓
slipiq_discord.py
  post_daily_brief(slip_review, channel_id)
    → Bot Token + channel ID pattern (NOT webhooks — consistent across all files)
    → Posts: header card + Tier 1 picks + Tier 2 picks + sportsbook slips
    → BOOK_LABEL dict: maps API key → display label (DK / Fanatics / FanDuel)
    → Tier 1 (≥70% conf): ✅ | Tier 2 (55-69%): ⚠️ | Tier 0 (<55%): suppressed
    → pick card format: ✅✅ = plus-money | Grade=A+ | Edge% | ¼Kelly shown
  post_sharp_review_card(card) ← nightly to #sharp-review channel
        ↓
slipiq_sharp_review.py
  log_pick(pick)               ← writes to picks_log.json (local file)
  grade_pick(pick, actual_stats) ← HIT/MISS based on prop vs actual result
  calculate_running_stats(picks) ← win rate by grade, trend, confidence band
  format_sharp_review_card(graded, stats, date)
  run_sharp_review()           ← nightly 11pm ET, reads log, grades, posts
        ↓
slipiq_orchestrator.py
  run_morning_pipeline()
    → Step 1: pull_todays_data()
    → Step 2: run_all_models(pitchers)
    → Step 3: run_curation(models, pickem_lines)
    → Step 4: run_slip_review(curate_out)
    → Step 5: post_daily_brief(slip_review, DAILY_PICKS_CHANNEL)
  log_todays_picks(curate_output, batter_plays)
  run_nightly_sharp_review()   ← 11pm ET
  start_nightly_scheduler()    ← APScheduler: 8am morning + 11pm sharp review
```

### Discord Output Format — Per Pick Card
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚾ SLIPIQ DAILY BRIEF — [Date]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIER 1 — STRONG EDGE
Strider (ATL vs CHC)
  Line: Over 7.5 Ks
  Model: 9.1 K projection | Edge: +1.6 Ks
  SwStr%: 16.4 | K/9: 11.8 | Rest: 5 days
  Confidence: 79% | Grade=A+
  ✅ PLAY

SPORTSBOOK SLIP — Per-book display:
               DK      Fanatics  FanDuel
Strider O7.5K  -108    +105 💰   -112
Strider O17.5  +100 💰  +108 💰   -105
ATL F5 ML      -135    -128      -140
ATL F3 RL -0.5 +115 💰  N/A       N/A
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 2. CORE LOGIC AND BUSINESS RULES

### Pitcher Strikeout Model (Primary)
- **Distribution**: Negative binomial (NOT normal) — K counts are right-skewed
- **Data window**: Last 10 starts (weighted, recency bias)
- **Projection**: Weighted mean K count adjusted for:
  - Opponent team K rate vs RHP/LHP (relative to MLB avg)
  - Rest days (5+ = slight boost, 0-1 = flag)
  - Weather: wind speed, dome flag via OpenWeather API
  - SwStr% trend: recent 3 vs season (HOT if recent > season by threshold)
- **Confidence formula**: `P(K_actual > line)` from Monte Carlo simulations (10,000 runs default)
- **adj_floor**: 25th percentile of distribution — used as safety floor
- **adj_ceiling**: 75th percentile — defines upside range
- **Edge**: `projection - line` — minimum 0.4 required to pass curation

### Batter Model
- **Hits**: `hits_per_game_last_14 × vs_handedness_multiplier`
- **Total Bases**: `tb_per_game_last_14 × opp_pitcher_fip_factor`
- **RBI**: `rbi_rate × run_support_index × lineup_slot_weight`
- **Qualifier**: Any prop clearing threshold passes to slip builder

### Pick Tiering
```
Tier 1 (STRONG EDGE):  confidence ≥ 70%
  → Auto-include in daily brief + sportsbook slip
Tier 2 (LEAN):         confidence 55-69%
  → Show with ⚠️ warning, label as LEAN
  → Only include in correlated slips if it strengthens a Tier 1 core
Tier 0 (AVOID):        confidence < 55%
  → Never post — logged but suppressed from Discord
```

### EV and Grade Logic
```python
# Grade (visible to users):
A+ : confidence ≥ 85
A  : confidence ≥ 72
B+ : confidence ≥ 65
B  : confidence ≥ 58
C  : below 58 (Tier 2 floor)

# EV Score (operator-only, never shown on Discord):
ev_score = (confidence/100) * decimal_odds - 1
# Positive = positive expected value

# ¼ Kelly (operator-only):
quarter_kelly = 0.25 * ((confidence/100 * decimal_odds - 1) / (decimal_odds - 1))
```

### Game Classifier (Batter Leg Gating)
```python
# pitcher_score() — 0 to 100
score = min(40, k_floor * 4.5)          # K floor (0-40 pts)
      + min(35, swstr * 2.2)            # SwStr% (0-35 pts)
      + min(15, (recent-season) * 5)    # Recent bonus (0-15 pts)
      + conf / 10                        # Confidence (0-10 pts)

ELITE_THRESHOLD = 65  # Elite pitcher — K props emphasized
WEAK_THRESHOLD  = 45  # Weak pitcher — batter legs emphasized

Scenario → DUEL        (both ≥ 65): K props only, no batter adds
         → MISMATCH    (one ≥ 65):  K props for elite side + batters vs weak side
         → BOTH_WEAK   (both ≤ 45): Batter legs only, game total, F5 ML
```

### Correlated Slip Logic
```
Pitcher Slip (sportsbook):
  K over + Outs over + F5 ML (same pitcher's team) + F3 RL (if available)
  — All 4 legs correlated: pitcher dominates → team wins early

Full Slip (pick'em + sportsbook mix):
  Pitcher core + batter hit/TB/RBI adds from pitcher's lineup
  — Max 6 legs total

Fanatics Card (pick'em):
  Auto-formats PrizePicks / Underdog pick'em legs
  Shows combined probability and payout multiplier
  💰 flags plus-money legs for manual sportsbook action
```

### Sharp Review Grading
```python
# HIT conditions per prop type:
pitcher_strikeouts: actual_K >= line  (if over pick)
pitcher_outs:       actual_outs >= line
pitcher_pitches:    actual_pitches >= line
pitcher_bb:         actual_bb <= line  (under pick)
batter_hits:        actual_hits >= 0.5 (binary)
batter_total_bases: actual_tb >= line
batter_rbi:         actual_rbi >= 0.5

# Running stats tracked:
win_rate_by_grade     (A+/A/B+/B/C)
win_rate_by_trend     (HOT/COLD/NEUTRAL)
win_rate_by_conf_band (70-80 / 80-90 / 90+)
win_rate_by_scenario  (DUEL/MISMATCH/BOTH_WEAK)
```

---

## 3. DATABASE AND STATE MANAGEMENT

### Current State — Local File System (No Supabase Yet)
- **picks_log.json**: Local JSON file written by `slipiq_sharp_review.log_pick()`
  - Schema per entry: `{player_name, prop, direction, line, adj_floor, confidence, platform, scenario, best_edge, type, date, graded, hit}`
  - Duplicate prevention: check `date + player_name + prop` before inserting
  - File lives in `/slipiq/picks_log.json`

### Supabase — Designed but NOT integrated yet
- 17 tables designed, credentials in `.env` as `SUPABASE_URL` + `SUPABASE_KEY`
- Integration is Phase 2 — the current system uses local file persistence
- When Supabase is added: replace `picks_log.json` read/write with supabase-py client
- Priority tables: `picks`, `results`, `pitcher_stats`, `batter_stats`, `sharp_review_runs`

### Duplicate Alert Prevention
```python
# In slipiq_sharp_review.log_pick():
existing = [p for p in load_log()
            if p['date'] == today
            and p['player_name'] == pick['player_name']
            and p['prop'] == pick['prop']]
if existing:
    return  # Skip — already logged today
```

### Groq API (Confidence Agent State)
- Groq is used INSTEAD of Claude API for `slipiq_confidence_agent.py` — zero cost
- No persistent state — fresh call per morning pipeline run
- Key: `GROQ_API_KEY` in `.env`

### Rate Limit Guard (Odds API)
```python
MAX_EVENTS = int(os.getenv("ODDS_MAX_EVENTS", "15"))
# Each event = 1 API call for lines. Free tier = 500/month.
# 15 events/day × 30 days = 450 calls/month — fits free tier.
# Supplement with Pinnacle props for pitchers not covered.
```

---

## 4. CODEBASE DIRECTORY LAYOUT

```
slipiq/
│
├── .env                         ← secrets — NEVER commit
├── .env.example                 ← template with all required keys
├── .gitignore                   ← includes .env, __pycache__, venv
├── requirements.txt             ← all pip dependencies
├── railway.toml                 ← Railway deployment config
├── picks_log.json               ← local pick/result tracking (auto-created)
│
├── slipiq_orchestrator.py       ← MASTER ENTRY POINT
│     run_morning_pipeline()     ← 8am ET daily
│     run_nightly_sharp_review() ← 11pm ET daily
│     log_todays_picks()
│     start_nightly_scheduler()  ← APScheduler
│     parse_lines_command()      ← /lines Discord command parser
│
├── slipiq_mlb_data.py           ← MLB data layer
│     pull_todays_data()         ← confirmed starters from MLB Stats API
│     get_pitcher_savant()       ← pybaseball Baseball Savant scrape
│     get_pitcher_game_logs()    ← last 10 starts K/outs/pitches
│     get_team_k_rate()          ← opponent K% vs handedness
│     get_batter_last_14()       ← hitting stats last 14 days
│     get_opposing_starter_data() ← FIP/ERA for opposing pitcher
│
├── slipiq_pitcher_model.py      ← Strikeout projection engine
│     run_pitcher_model(name, line) → full projection dict
│     run_all_models(pitchers)   ← slate loop
│     get_recommendation(proj, line) → "OVER X.X Ks | Grade=A | ..."
│     american_to_prob(price)    ← utility: odds → implied probability
│
├── slipiq_batter_model.py       ← Batter prop projection engine
│     model_batter_hits()
│     model_batter_total_bases()
│     model_batter_rbi()
│     classify_game(home_model, away_model) → DUEL/MISMATCH/BOTH_WEAK
│     select_game_legs(classification, lines) → F5 ML, F3 RL dicts
│     pitcher_score(model)       ← 0-100 composite pitcher quality
│
├── slipiq_odds_api.py           ← The Odds API integration
│     get_mlb_pitcher_props()    ← event → prop per-book
│     pull_all_lines()           ← full slate: props + F5/F3/totals
│     get_pinnacle_pitcher_props() ← Pinnacle supplement
│
├── slipiq_curate.py             ← Filtering and pick selection
│     run_curation(models, pickem_lines) → curate_out dict
│     curate_pickem(picks)       ← PrizePicks/Underdog legs
│     curate_batters(batter_data) ← batter prop passes
│     run_full_curation()        ← combined pitcher + batter
│
├── slipiq_grading.py            ← SHARED grading module (MLB + NBA)
│     calc_grade(confidence)     → "A+" / "A" / "B+" / "B" / "C"
│     format_grade_emoji(grade)  → 🔥 / ✅ / ⚠️
│
├── slipiq_confidence_agent.py   ← Groq-powered confidence enrichment
│     enrich_picks(picks)        → picks list with enriched confidence
│     call_groq(prompt)          ← Groq API call (NOT Anthropic)
│
├── slipiq_lines.py              ← Lines fetch + model comparison pipeline
│     get_mlb_pitcher_props()    ← Odds API props
│     run_full_analysis()        ← props → model → curate → picks list
│     MIN_CONFIDENCE = env SLIPIQ_MIN_CONF (floor 55)
│     MIN_EDGE       = env SLIPIQ_MIN_EDGE (floor 0.4)
│
├── slipiq_book_slip_builder.py  ← Sportsbook slip assembly
│     build_pitcher_slip()       ← 3-4 leg correlated core
│     build_full_slip()          ← pitcher + batter combined
│     per_book_output()          ← DK / Fanatics / FanDuel side-by-side
│     availability_check()       ← drop F3 RL if not offered
│
├── slipiq_discord.py            ← Discord output and posting (MLB)
│     post_daily_brief()         ← morning picks post
│     post_sharp_review_card()   ← nightly results post
│     format_pick_card()         ← single pick embed
│     format_daily_header()      ← header embed
│     BOOK_LABEL dict            ← API key → display label mapping
│
├── slipiq_sharp_review.py       ← Nightly grading + results tracking
│     log_pick(pick)             ← write to picks_log.json
│     grade_pick(pick, actual)   → graded pick dict with hit/miss
│     calculate_running_stats()  → stats by grade/trend/conf
│     format_sharp_review_card() ← Discord-ready text block
│     run_sharp_review()         ← full nightly pipeline
│     get_final_games()          ← MLB Stats API results
│     get_pitcher_final_stats()  ← actual K/outs/pitches from box score
│     get_batter_final_stats()   ← actual hits/TB/RBI
│
├── slipiq_normalization.py      ← Player name matching across APIs
│     normalize_name(name)       ← fuzzywuzzy match
│     build_player_id_table()    ← ESPN + MLB ID static lookup
│
├── slipiq_pinnacle_props.py     ← Pinnacle props supplement
│     get_pinnacle_pitcher_props() ← pitchers not covered by Odds API
│
├── test_pitchers.py             ← Dev test file (not production)
│
├── slipiq_results.py            ← Manual result logging (early version)
│
│── NBA FILES (built, needs fixes before October use):
├── slipiq_nba_data.py           ← NBA data layer (5 known bugs)
├── slipiq_nba_player_model.py   ← NBA prop projection (5 known bugs)
├── slipiq_nba_discord.py        ← NBA Discord output (needs rebuild)
└── slipiq_nba_orchestrator.py   ← NBA pipeline (needs curation step)
```

### .env Key Reference
```
ANTHROPIC_API_KEY=          ← Claude API (writer, narrative — NOT confidence agent)
GROQ_API_KEY=               ← Groq (confidence agent — zero cost)
ODDS_API_KEY=               ← The Odds API (lines, props)
BDL_API_KEY=                ← BallDontLie (NBA/general)
OPENWEATHER_API_KEY=        ← Weather (dome/wind for pitcher model)
DISCORD_BOT_TOKEN=          ← Bot token (Bot Token pattern, not webhook)
DISCORD_DAILY_PICKS_CHANNEL=← Channel ID (int, not webhook URL)
DISCORD_SHARP_REVIEW_CHANNEL=
DISCORD_NBA_PICKS_CHANNEL=  ← NBA channel (future)
SUPABASE_URL=               ← Future — not integrated yet
SUPABASE_KEY=               ← Future — not integrated yet
ODDS_MAX_EVENTS=15          ← Rate limit guard
SLIPIQ_MIN_CONF=55          ← Curation confidence floor
SLIPIQ_MIN_EDGE=0.4         ← Curation edge floor
SLIPIQ_TOP_PICKS=0          ← 0 = no cap; if set, never fewer than 5
```

---

## 5. NBA / WNBA BLUEPRINT

### Why NBA Requires Different Architecture Than MLB
```
MLB                          NBA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pitcher controls outcome     Minutes = master variable
K variance per start ~1.5    Points variance per game ~6-8
Starter known by morning     Rotation can change at halftime
Weather matters              Foul trouble = random death
Savant = 200+ data points    Back-to-back = major suppressant
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Data Sources for NBA
```python
# PRIMARY — NBA Stats API (free, no key)
pip install nba_api
from nba_api.stats.endpoints import (
    playergamelog,          # last 10/20/season game logs
    leaguedashteamstats,    # PACE, DEF_RATING — NOT teaminfocommon
    leaguedashplayerstats,  # usage rate, per-36
    teamgamelog,            # team results
)

# CRITICAL BUG TO KNOW: MIN column returns "32:14" as a STRING
# Always convert before using:
df['MIN'] = df['MIN'].apply(
    lambda x: int(x.split(':')[0]) + int(x.split(':')[1])/60
              if isinstance(x, str) else float(x)
)

# SECONDARY — BallDontLie NBA API (key already in .env)
# Schedules, scores, team stats, live data — backup to nba_api

# Season string must be dynamic:
from datetime import datetime
year = datetime.now().year
season = f"{year-1}-{str(year)[2:]}" if datetime.now().month < 10 else f"{year}-{str(year+1)[2:]}"
```

### NBA Model Architecture
```python
# Projection formula — same logic as MLB, different variables:
stat_per_min = recent_stat / recent_minutes
projected_stat = stat_per_min × projected_minutes

# Adjustments applied:
projected_stat *= opp_def_rating_vs_position_factor  # vs PG/SG/SF/PF/C
projected_stat *= pace_factor                         # (combined_pace / 100)
projected_stat *= b2b_penalty if b2b else 1.0         # B2B = 0.88 multiplier
projected_stat *= role_factor                         # injury-driven expansion

# Distribution: Negative binomial (NOT normal) — same as MLB K model
# Grade thresholds: USE slipiq_grading.py — do NOT re-implement in NBA files
# Tier gating: same 55/70 split as MLB
```

### Player Features Required (build_player_object())
```
PLAYER FEATURES:
  minutes_season_avg, minutes_last_3, minutes_trend
  pts/reb/ast/stl/blk per_game and per_36
  usage_rate_season, usage_rate_last_5
  fouls_per_game, dnp_last_3

OPPONENT FEATURES:
  opp_def_rating_vs_position  ← leaguedashteamstats endpoint
  opp_pace_season, opp_pace_last_5

GAME CONTEXT:
  projected_pace = (home_pace + away_pace) / 2
  game_total, spread           ← from Odds API
  b2b_flag, days_rest, home_away
  teammate_out, lineup_changes, key_defender_out
```

### Breakout Detection Algorithm (High Priority — Must Run First)
```python
# Signal fires when ALL conditions true:
# 1. Player avg minutes < 25 (bench/secondary) OR recent role change
# 2. Star teammate confirmed OUT for tonight (injury report)
# 3. Player min projection > season avg × 1.25
# 4. Pick'em line NOT yet adjusted for injury (books lag 20-60 min)
#
# OUTPUT: Fires to Discord BEFORE daily brief — fastest-posted card
# Window: Injury reports drop ~4:30pm ET — post within 30 min or edge gone

# Breakout card format:
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 🚨 BREAKOUT ALERT — [Player Name]
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# [Star] OUT tonight
# Season avg: X.X pts | XX.X min
# Projected tonight: X+ pts | X+ min
# Points O[line]  PrizePicks  XX%  Grade=A
# PRA    O[line]  Underdog    XX%  Grade=A
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Line set before injury confirmed. Act before books adjust.
```

### NBA Props to Model and Game Lines to Track
```
PLAYER PROPS (curated):
  Points O/U, Rebounds O/U, Assists O/U
  PRA (Pts+Reb+Ast) combined
  3-Pointers Made (shooters only — high variance flag)
  Steals+Blocks O/U

TEAM/GAME LINES (tracked alongside):
  Game total O/U         ← primary correlation signal
  Team spread (ATS)
  1H total               ← less garbage time variance
  1H spread
  1Q total               ← tightest game, best pace signal

NBA CORRELATION RULES (for slip builder):
  High total game → star points over (positive)
  Blowout expected (spread >9) → reduce star minutes → skip prop
  B2B game → apply 0.88 multiplier to all stat projections
  Star injured → bench player spike (breakout alert)
  Pace mismatch → faster team's players more possessions
```

### NBA Build Order (start October for 2025-26 season)
```
Phase 1: slipiq_nba_data.py       ← fix 5 bugs, build detect_breakout_candidates()
Phase 2: slipiq_nba_player_model.py ← fix 5 bugs, fix lambda scope, neg binomial
Phase 3: slipiq_nba_curate.py     ← new file, mirrors curate_pickem() structure
Phase 4: slipiq_nba_discord.py    ← rebuild to match MLB card format exactly
Phase 5: slipiq_nba_orchestrator.py ← add curation step, fix webhook/token mismatch
Phase 6: Add NBA markets to slipiq_odds_api.py
Phase 7: Add NBA pipeline to slipiq_orchestrator.py (11am ET scheduler)
Phase 8: Add NBA to slipiq_sharp_review.py (same grading, separate log)
```

### Known Bugs in Existing NBA Files
```
slipiq_nba_data.py:
  ❌ Season hardcoded "2023-24" — needs dynamic computation
  ❌ detect_breakout_candidates() is just `pass` — body not written
  ❌ teaminfocommon doesn't return PACE/DEF_RATING — use leaguedashteamstats
  ❌ MIN column not converted from "32:14" string → float
  ❌ build_player_object() missing: b2b_flag, rest_days, pace, opp_def, spread, total

slipiq_nba_player_model.py:
  ❌ Uses normal distribution — should be negative binomial
  ❌ simulate_stat() returns lambda with captured local var — compute float directly
  ❌ MIN column crashes (same string issue)
  ❌ Grade thresholds 65%/58% — must use slipiq_grading.calc_grade() instead
  ❌ No game context (pace, b2b, spread) used in model

slipiq_nba_discord.py:
  ❌ No actual Discord posting — only formatters, nothing calls post_to()
  ❌ Uses webhook URL pattern — rest of system uses Bot Token + channel ID
  ❌ No Tier 1/2/0 filter — will post everything regardless of confidence
  ❌ Card format doesn't match MLB output (no edge%, no ¼Kelly, no Grade=A+)

slipiq_nba_orchestrator.py:
  ❌ Webhook vs Bot Token mismatch
  ❌ active_lines is manual input — should call slipiq_odds_api.pull_all_lines()
  ❌ detect_breakout_candidates() never called in pipeline
  ❌ No curation step — model → Discord directly (no edge filter)
```

---

## 6. CODING STYLE AND DIRECTIVES

### Language and Runtime
- **Python 3.x** — Windows local dev (`py` command), Railway for prod (`python`)
- **Virtual environment**: `venv` in project root — always activate before running
- **Entry point**: `python slipiq_orchestrator.py` — everything chains from here

### Libraries — Exact Stack
```python
# Core
import requests           # All HTTP API calls — use this, not httpx
import os
from dotenv import load_dotenv   # Always call load_dotenv() at module top

# Data
import pandas as pd       # DataFrames for game logs, stats tables
import numpy as np        # Distributions, simulations
from scipy.stats import nbinom  # Negative binomial (pitcher K + NBA stats)
import pybaseball         # Baseball Savant, FanGraphs — no key needed
import statsapi           # MLB Stats API (pip install MLB-StatsAPI)
from nba_api.stats.endpoints import playergamelog, leaguedashteamstats

# AI
import anthropic          # Claude API — for narrative/writeups ONLY
from groq import Groq     # Groq API — for confidence agent (zero cost)

# Discord
import discord            # discord.py — Bot Token pattern, not webhooks
import asyncio

# Scheduling
from apscheduler.schedulers.blocking import BlockingScheduler

# Matching
from fuzzywuzzy import fuzz   # Player name normalization across APIs

# Database (future)
from supabase import create_client  # Not yet integrated
```

### Module Pattern — Every File Follows This
```python
"""
SlipIQ [Module Name]
[One sentence purpose]
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Constants from env ───────────────────────────────────────
SOME_KEY = os.getenv("SOME_KEY")
SOME_PARAM = int(os.getenv("SOME_PARAM", "default_value"))

# ─── Section Header Style (em-dash, 60 char width) ───────────

def function_name(param):
    """
    One-line docstring — what it does, what it returns.
    """
    ...

# ─── Main (test only, not for import) ────────────────────────
if __name__ == "__main__":
    result = function_name(test_param)
    print(result)
```

### Error Handling Rules
```python
# All external API calls: try/except with fallback return
try:
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
except Exception as e:
    print(f"[module_name] ERROR: {e}")
    return []   # Never crash the pipeline — return empty, log the error

# Never use bare `except:` — always `except Exception as e:`
# Always include timeout= on requests calls
```

### Data Flow Contracts — Key Dict Schemas
```python
# Pitcher model output (from slipiq_pitcher_model.run_pitcher_model()):
{
    "pitcher_name": str,
    "season": {
        "season_swstr_pct": float,
        "recent_3_swstr_pct": float,
        "k_per_9": float,
    },
    "props": {
        "strikeouts": {
            "prop": "strikeouts_over_X.X",
            "line": float,
            "adj_floor": float,
            "adj_ceiling": float,
            "edge": float,          # projection - line
            "confidence": float,    # 0-100
            "direction": "over",
        },
        "outs": {...},
        "pitches": {...},
        "walks": {...},
    },
    "projection": float,            # primary K projection
    "confidence": float,            # primary K confidence
    "trend": "HOT" | "COLD" | "NEUTRAL",
}

# Curate output leg (pickem or sportsbook):
{
    "player_name": str,  # or "pitcher_name" for pitcher legs
    "prop": str,         # "strikeouts_over_7.5"
    "direction": "over" | "under",
    "line": float,
    "best_line": float,
    "adj_floor": float,
    "confidence": float,
    "platform": "prizepicks" | "underdog" | "draftkings" | "fanatics" | "fanduel",
    "best_book": str,
    "best_price": int,   # American odds
    "plus_money": bool,
    "all_books": {       # per-book price dict
        "draftkings": {"price": int},
        "fanatics": {"price": int},
        "fanduel": {"price": int},
        "pinnacle": {"price": int},
    },
    "type": "pitcher" | "batter" | "game",
    "scenario": "DUEL" | "MISMATCH" | "BOTH_WEAK",
    "best_edge": float,
    "grade": str,        # "A+" / "A" / "B+" / "B" / "C"
}
```

### Immutable Rules — Never Break These
1. **Bot Token pattern only** — never use Discord webhook URLs; rest of system uses `discord.py` Bot Token + channel ID integers
2. **Never substitute missing book lines** — if F3 RL not on Fanatics, drop the leg; never swap in a different book or different bet
3. **Groq for confidence agent** — never route confidence scoring through Anthropic API (cost)
4. **Claude API only for narrative** — writer, sharp review prose, Discord writeup text
5. **Negative binomial distribution** — never use normal/Gaussian for count data (Ks, points, rebounds)
6. **Never cap picks below 5** — if SLIPIQ_TOP_PICKS > 0, enforce `and top_n >= 5` guard
7. **Shared slipiq_grading.py** — NBA must import this, never redefine thresholds locally
8. **All three books side-by-side** — slip builder always shows DK | Fanatics | FanDuel columns, never "best book only"
9. **Section headers use em-dash style**: `# ─── Section Name ─────────`
10. **Every module is independently runnable** — `if __name__ == "__main__":` block with test case

### Deployment — Railway
```toml
# railway.toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "python slipiq_orchestrator.py"
```
- Railway runs 24/7 — APScheduler inside the process handles timing
- No Railway cron configured — use APScheduler inside `start_nightly_scheduler()`
- GitHub repo: `tallerthanlife/SlipIQ` — Railway auto-deploys on push to main

### Discord Channel Architecture (Private Server — No Selling)
```
#daily-picks          ← DAILY_PICKS_CHANNEL — MLB morning brief
#sharp-review         ← SHARP_REVIEW_CHANNEL — nightly grading
#live-alerts          ← real-time line moves, breakout alerts
#the-vault            ← general chat
#nba-picks            ← NBA_PICKS_CHANNEL (future, October)
```

---

## 7. PHASE ROADMAP

```
Phase 1 (ACTIVE — MLB Summer 2026):
  ✅ slipiq_mlb_data.py
  ✅ slipiq_pitcher_model.py
  ✅ slipiq_batter_model.py
  ✅ slipiq_curate.py
  ✅ slipiq_confidence_agent.py (Groq — ev_engine wired)
  ✅ slipiq_lines.py (delegation shim)
  ✅ slipiq_book_slip_builder.py
  ✅ slipiq_discord.py
  ✅ slipiq_sharp_review.py
  ✅ slipiq_orchestrator.py (all crash bugs fixed)
  ✅ slipiq_grading.py
  ✅ slipiq_parlayapi.py
  ✅ slipiq_game_lines.py
  ✅ slipiq_odds_supplement.py
  ✅ slipiq_ev_engine.py (EV math foundation)
  ✅ slipiq_propline.py (Prop-Line API — 1000 cr/day)
  ✅ slipiq_prizepicks.py (PrizePicks fixed-multiplier engine)
  ✅ slipiq_montecarlo.py (correlated SGP + bankroll simulation)
  ✅ slipiq_calibration.py (Brier score, CLV logging)
  ✅ slipiq_sharp_api.py (EV benchmarking — test bench only)
  ✅ slipiq_propline_scanner.py (intraday scanner — 20-min polls)
  ✅ slipiq_slip_router.py (routes picks to SGP/lotto/PP/ML-RL)
  ✅ slipiq_independent_parlay.py (lotto $0.25 + ML/RL parlay)
  ✅ slipiq_player_ids.py (MLB ID lookup — 11/11 tests passing)
  ✅ slipiq_pitcher_props.py (broken import shim)
  ✅ slipiq_batter_lines.py (fixed ev_confirmed, clean imports)
  ✅ slipiq_parlay_alerts.py (routing reader, no more ad-hoc builder)
  ✅ Supabase: calibration_log + closing_lines tables added

  ARCHIVED (header comment added, not deleted):
    slipiq_sharp_review_agent.py → superseded by slipiq_sharp_review.py
    slipiq_odds_api.py → superseded by slipiq_odds_supplement.py

Phase 2 (MLB Hitter Props — Ongoing):
  🔲 Expand slipiq_curate.py batter coverage
  🔲 Improve opposing starter FIP data pull

Phase 3 (NBA — October 2025-26 season):
  🔲 Fix 5 bugs in slipiq_nba_data.py
  🔲 Fix 5 bugs in slipiq_nba_player_model.py
  🔲 Rebuild slipiq_nba_discord.py to match MLB format
  🔲 slipiq_nba_curate.py (new file)
  🔲 Fix slipiq_nba_orchestrator.py
  🔲 Add NBA markets to slipiq_odds_api.py
  🔲 Integrate NBA pipeline into slipiq_orchestrator.py

Phase 4 (SGP Correlation + Advanced):
  🔲 SGP Correlation Agent
  🔲 CLV Tracking Agent
  🔲 SGP Pricing Model
  🔲 Arb detection engine
```

---

## 8. API ENDPOINT REFERENCE

```
# The Odds API
Base: https://api.the-odds-api.com/v4
Events:  /sports/baseball_mlb/events?apiKey=&regions=us
Props:   /sports/baseball_mlb/events/{event_id}/odds?markets=pitcher_strikeouts&oddsFormat=american
NBA:     /sports/basketball_nba/events (future)

# MLB Stats API (no key)
Base: https://statsapi.mlb.com/api/v1
Schedule: /schedule?sportId=1&date=YYYY-MM-DD
Boxscore: /game/{game_pk}/boxscore

# Baseball Savant (pybaseball)
pybaseball.statcast_pitcher(start_dt, end_dt, player_id)
pybaseball.pitching_stats(year, qual=1)

# NBA Stats API (nba_api library)
nba_api.stats.endpoints.playergamelog(player_id, season)
nba_api.stats.endpoints.leaguedashteamstats(season, per_mode='Per100Possessions')

# Groq API
Base: via groq library — model: "mixtral-8x7b-32768" or "llama3-8b-8192"

# OpenWeather API
Base: https://api.openweathermap.org/data/2.5/weather?q={city}&appid=KEY
```

---

*End of PROJECT_BRAIN.md — This document is the authoritative source of truth for all SlipIQ architectural decisions, coding patterns, and build state. Any new AI must read this fully before modifying any file.*

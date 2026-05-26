# ARCHITECTURE_MAP.md — SlipIQ Full System Architecture
> Last Updated: May 26, 2026 | Read alongside PROJECT_BRAIN.md
> This is the visual/structural reference. PROJECT_BRAIN.md is the technical spec.

---

## SYSTEM OVERVIEW — Two Bots, One Server, One Mission

```
┌─────────────────────────────────────────────────────────────────┐
│                    DISCORD SERVER: SlipIQ                       │
│                                                                 │
│   ┌──────────────────────┐    ┌──────────────────────────────┐  │
│   │     SLIPIQ BOT       │    │        TRADEIQ BOT           │  │
│   │  Sports Props Engine │    │   Financial Markets Engine   │  │
│   │  (MLB active now)    │    │   (Architecture finalized,   │  │
│   │  (NBA → October)     │    │    build pending)            │  │
│   └──────────────────────┘    └──────────────────────────────┘  │
│                                                                 │
│   Shared: slipiq_grading.py · Supabase DB · Railway hosting    │
│   Bridge: SlipIQ confidence ≥75% → TradeIQ #event-contracts   │
└─────────────────────────────────────────────────────────────────┘
```

---

## LAYER 1 — TRIGGER LAYER (When the Bot Wakes Up)

```
┌──────────────────────────────────────────────────────────────────┐
│                        SCHEDULER                                 │
│                  APScheduler (inside Railway process)            │
│                                                                  │
│   6:30 AM AZ ──→ MLB Morning Pipeline (slipiq_orchestrator.py)  │
│   11:00 AM AZ ──→ NBA Pipeline (future — October 2026)          │
│    4:30 PM AZ ──→ Breakout Alert Check (injury reports drop)    │
│   11:00 PM AZ ──→ Nightly Sharp Review                         │
│                                                                  │
│   Deployed on: Railway (24/7 process, auto-deploys from GitHub) │
│   Repo: tallerthanlife/SlipIQ                                   │
└──────────────────────────────────────────────────────────────────┘
```

---

## LAYER 2 — DATA SOURCES (What Feeds the Models)

```
┌─────────────────────── FREE / NO KEY ────────────────────────────┐
│                                                                   │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐  │
│  │   pybaseball /   │  │  MLB Stats API   │  │   nba_api     │  │
│  │ Baseball Savant  │  │statsapi.mlb.com  │  │stats.nba.com  │  │
│  │                  │  │                  │  │               │  │
│  │ SwStr%  K/9      │  │ Confirmed        │  │ Player game   │  │
│  │ xFIP    velo     │  │ starters         │  │ logs          │  │
│  │ pitch mix        │  │ Lineups          │  │ Team pace     │  │
│  │ Statcast data    │  │ Injuries         │  │ Def rating    │  │
│  │ Batter splits    │  │ Game schedule    │  │ B2B flags     │  │
│  │ 0 credits ever   │  │ Box scores       │  │ 0 credits     │  │
│  └────────┬─────────┘  └────────┬─────────┘  └──────┬────────┘  │
│           │                     │                    │           │
│           └─────────────────────┴────────────────────┘           │
│                                 │                                 │
│                        slipiq_mlb_data.py                        │
│                        slipiq_nba_data.py                        │
└──────────────────────────────────┬───────────────────────────────┘

┌─────────────────── PAID API (ODDS / LINES) ──────────────────────┐
│                                                                   │
│  ┌──────────────────────────┐  ┌──────────────────────────────┐  │
│  │     The Odds API         │  │     Pinnacle (supplement)    │  │
│  │  api.the-odds-api.com    │  │   slipiq_pinnacle_props.py   │  │
│  │                          │  │                              │  │
│  │  Pitcher strikeout props │  │  Sharp reference price       │  │
│  │  F5 ML / F3 RL           │  │  No-vig implied probability  │  │
│  │  Game totals/spreads     │  │  Covers pitchers Odds API    │  │
│  │  Per-book pricing:       │  │  misses on free tier         │  │
│  │   DraftKings             │  │                              │  │
│  │   FanDuel                │  │                              │  │
│  │   Fanatics               │  │                              │  │
│  │  Free tier: 500 req/mo   │  │                              │  │
│  │  MAX_EVENTS=15 (guard)   │  │                              │  │
│  └──────────────┬───────────┘  └──────────────┬───────────────┘  │
│                 └────────────────┬─────────────┘                  │
│                          slipiq_odds_api.py                      │
└──────────────────────────────────┬───────────────────────────────┘

┌─────────────── CONTEXTUAL DATA ──────────────────────────────────┐
│                                                                   │
│  ┌──────────────────────┐  ┌────────────────────────────────┐   │
│  │   OpenWeather API    │  │      BallDontLie API           │   │
│  │  OPENWEATHER_API_KEY │  │      BDL_API_KEY               │   │
│  │                      │  │                                │   │
│  │  Wind speed          │  │  Game outcomes / H2H           │   │
│  │  Temperature         │  │  Team trends                   │   │
│  │  Dome flag           │  │  NBA live data (future)        │   │
│  │  Affects K% model    │  │  General sports context        │   │
│  │  Free tier = 1K/day  │  │  Free tier available           │   │
│  └──────────────────────┘  └────────────────────────────────┘   │
│           Both feed into slipiq_mlb_data.py / slipiq_nba_data.py│
└──────────────────────────────────────────────────────────────────┘
```

---

## LAYER 3 — MODEL LAYER (Computing the Edge)

```
┌──────────────────────────────────────────────────────────────────┐
│                        MLB MODEL LAYER                           │
│                                                                  │
│  slipiq_pitcher_model.py                                         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  INPUT: pitcher name, market line, game context          │   │
│  │                                                          │   │
│  │  Distribution: Negative Binomial (right-skewed K data)   │   │
│  │  Simulations: 10,000 Monte Carlo runs                    │   │
│  │  Adjustments:                                            │   │
│  │    × opponent team K rate vs RHP/LHP                     │   │
│  │    × rest days multiplier                                │   │
│  │    × weather/dome flag                                   │   │
│  │    × SwStr% trend (recent 3 vs season)                   │   │
│  │                                                          │   │
│  │  OUTPUT: {                                               │   │
│  │    projection: float,    ← weighted mean K projection    │   │
│  │    adj_floor: float,     ← 25th percentile               │   │
│  │    adj_ceiling: float,   ← 75th percentile               │   │
│  │    confidence: float,    ← P(actual > line) × 100        │   │
│  │    edge: float,          ← projection - line             │   │
│  │    trend: HOT/COLD/NEUTRAL                               │   │
│  │  }                                                       │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  slipiq_batter_model.py                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  classify_game(home_model, away_model)                   │   │
│  │    pitcher_score = K_floor(40) + SwStr%(35)              │   │
│  │                  + recent_bonus(15) + conf(10)           │   │
│  │    ELITE ≥ 65 | WEAK ≤ 45                                │   │
│  │    → DUEL / MISMATCH / BOTH_WEAK                         │   │
│  │                                                          │   │
│  │  model_batter_hits()     ← last 14 days rate × handedness│   │
│  │  model_batter_tb()       ← TB rate × opp FIP factor      │   │
│  │  model_batter_rbi()      ← RBI rate × lineup slot weight │   │
│  │  select_game_legs()      ← F5 ML, F3 RL with avail flag  │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                       NBA MODEL LAYER (October 2026)             │
│                                                                  │
│  slipiq_nba_player_model.py [EXISTS — 5 bugs to fix]            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Master variable: MINUTES (not stats directly)           │   │
│  │  Formula: stat_per_min × projected_minutes               │   │
│  │  Adjustments:                                            │   │
│  │    × opp_def_rating_vs_position                          │   │
│  │    × pace_factor = combined_pace / 100                   │   │
│  │    × b2b_penalty = 0.88 if back-to-back                  │   │
│  │    × role_factor (injury-driven expansion)               │   │
│  │  Distribution: Negative Binomial (same as MLB)           │   │
│  │  Props: Points / Rebounds / Assists / PRA / 3PM          │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│              SHARED GRADING MODULE (MLB + NBA)                   │
│                    slipiq_grading.py                             │
│                                                                  │
│  calc_grade(confidence):                                         │
│    A+ → conf ≥ 85%    🔥                                        │
│    A  → conf ≥ 72%    ✅                                        │
│    B+ → conf ≥ 65%    ✅                                        │
│    B  → conf ≥ 58%    ⚠️                                       │
│    C  → conf < 58%    ⚠️  (Tier 2 floor — correlated slips)    │
└──────────────────────────────────────────────────────────────────┘
```

---

## LAYER 4 — INTELLIGENCE LAYER (AI Enrichment)

```
┌──────────────────────────────────────────────────────────────────┐
│                    AI INTELLIGENCE LAYER                         │
│                                                                  │
│  ┌────────────────────────────┐  ┌────────────────────────────┐ │
│  │   GROQ API                 │  │   ANTHROPIC CLAUDE API     │ │
│  │   slipiq_confidence_agent  │  │   slipiq_writer.py         │ │
│  │                            │  │                            │ │
│  │  Model: mixtral-8x7b or    │  │  Model: claude-sonnet-4-6  │ │
│  │         llama3-8b          │  │                            │ │
│  │  Cost: $0                  │  │  Cost: ~$3/MTok input      │ │
│  │  Use: confidence scoring   │  │  Use: narrative writeups   │ │
│  │  enrich_picks(picks)       │  │       sharp review prose   │ │
│  │  → Groq scores 0-100 each  │  │       Discord card text    │ │
│  │  → Merges with model conf  │  │                            │ │
│  │  → Final enriched %        │  │  NEVER use for confidence  │ │
│  │  NEVER cap < 5 picks       │  │  scoring (cost issue)      │ │
│  └────────────────────────────┘  └────────────────────────────┘ │
│                                                                  │
│  RULE: Groq = cost layer | Claude = voice/reasoning layer       │
└──────────────────────────────────────────────────────────────────┘
```

---

## LAYER 5 — CURATION LAYER (Filtering the Signal)

```
┌──────────────────────────────────────────────────────────────────┐
│                      CURATION PIPELINE                           │
│                                                                  │
│  slipiq_curate.py                                                │
│                                                                  │
│  run_curation(models, pickem_lines)                              │
│     │                                                            │
│     ├── Confidence filter: MIN_CONF = env var (floor 55%)       │
│     ├── Edge filter: MIN_EDGE = env var (floor 0.4 Ks)          │
│     ├── Enrichment: best_platform, best_line, best_book, price  │
│     └── Classification:                                          │
│            pickem → PrizePicks / Underdog legs                   │
│            sportsbook → DK / Fanatics / FanDuel legs            │
│                                                                  │
│  curate_batters(batter_game_data)                                │
│     └── Separate pass for batter prop legs                       │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                  TIER CLASSIFICATION                    │    │
│  │                                                         │    │
│  │  Tier 1 (conf ≥ 70%): STRONG EDGE                       │    │
│  │    → Auto-include in daily brief                        │    │
│  │    → Auto-include in sportsbook slip core               │    │
│  │    → ✅ emoji on Discord card                           │    │
│  │                                                         │    │
│  │  Tier 2 (conf 55-69%): LEAN                             │    │
│  │    → Show with ⚠️ warning                              │    │
│  │    → Only in correlated slips if strengthens Tier 1     │    │
│  │                                                         │    │
│  │  Tier 0 (conf < 55%): AVOID                             │    │
│  │    → Logged internally, never posted to Discord         │    │
│  └─────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

---

## LAYER 6 — SLIP CONSTRUCTION LAYER (Building the Cards)

```
┌──────────────────────────────────────────────────────────────────┐
│                    SLIP CONSTRUCTION                             │
│                                                                  │
│  slipiq_book_slip_builder.py                                     │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  build_pitcher_slip(pitcher_model, game_lines)           │   │
│  │  Correlated 3-4 leg core per game:                       │   │
│  │    Leg 1: Pitcher K over ← primary                       │   │
│  │    Leg 2: Pitcher Outs over ← correlated                 │   │
│  │    Leg 3: F5 ML (pitcher's team) ← correlated            │   │
│  │    Leg 4: F3 RL -0.5 ← correlated (if available)        │   │
│  │  Correlation logic: pitcher dominates → team wins early  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  build_full_slip(pitcher_slip, batter_model)             │   │
│  │  Adds batter legs on top of pitcher core:                │   │
│  │    + Batter Hits O0.5                                    │   │
│  │    + Batter Total Bases O1.5                             │   │
│  │    + Batter RBI O0.5                                     │   │
│  │  Max 6 legs total                                        │   │
│  │  Scenario gating:                                        │   │
│  │    DUEL → K props only, no batter adds                   │   │
│  │    MISMATCH → K props + batters vs weak pitcher side     │   │
│  │    BOTH_WEAK → batter legs only + game total + F5 ML     │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  per_book_output(slip, books=["dk","fanatics","fanduel"])│   │
│  │  Shows ALL THREE books side-by-side (NEVER best-only):   │   │
│  │                                                          │   │
│  │               DK        Fanatics    FanDuel              │   │
│  │  Strider O7.5K  -108    +105 💰     -112                 │   │
│  │  Strider O17.5  +100 💰  +108 💰    -105                 │   │
│  │  ATL F5 ML      -135    -128        -140                 │   │
│  │  ATL F3 RL      +115 💰  N/A        N/A                  │   │
│  │                                                          │   │
│  │  💰 flag = plus-money line                               │   │
│  │  N/A = not available on that book (NEVER substituted)    │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  slipiq_lines.py — Lines analysis pipeline               │   │
│  │  get_mlb_pitcher_props() → run_full_analysis()           │   │
│  │  → Props + model → curate → picks list                   │   │
│  │  Also integrates Pinnacle via slipiq_pinnacle_props.py   │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

---

## LAYER 7 — OUTPUT LAYER (Discord Delivery)

```
┌──────────────────────────────────────────────────────────────────┐
│                       DISCORD OUTPUT                             │
│              slipiq_discord.py (Bot Token pattern)               │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              MORNING BRIEF STRUCTURE                     │   │
│  │           Posts to: #daily-picks channel                 │   │
│  │                                                          │   │
│  │  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                   │   │
│  │  ⚾ SLIPIQ DAILY BRIEF — [Date]                          │   │
│  │  [N] games today | [N] pitchers cleared curation         │   │
│  │  Weather flags: [None / flags]                           │   │
│  │  Injury flags: [player — note]                           │   │
│  │  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                   │   │
│  │                                                          │   │
│  │  SECTION 1 — PITCHER STRIKEOUTS                          │   │
│  │  🟢 TIER 1 — STRONG EDGE                                 │   │
│  │    [Pitcher] (TEAM vs OPP)                               │   │
│  │    Line: Over X.X Ks at -XXX Book                        │   │
│  │    Model: X.X K projection | Edge: +X.X Ks              │   │
│  │    SwStr%: XX.X | K/9: XX.X | Rest: X days              │   │
│  │    Confidence: XX% | Grade=A+                            │   │
│  │    ✅ PLAY — Over X.X Ks                                 │   │
│  │                                                          │   │
│  │  🟡 TIER 2 — LEAN                                        │   │
│  │    ⚠️ LEAN — Only in correlated slip                    │   │
│  │                                                          │   │
│  │  🔴 TIER 3 — AVOID TODAY                                 │   │
│  │    (suppressed from Discord — logged internally)         │   │
│  │                                                          │   │
│  │  SECTION 2 — SPORTSBOOK SLIPS (per game)                 │   │
│  │  [3-book side-by-side table per game]                    │   │
│  │                                                          │   │
│  │  SECTION 3 — PICK'EM CARDS                               │   │
│  │  [PrizePicks / Underdog formatted legs]                  │   │
│  │                                                          │   │
│  │  SECTION 4 — SLATE PARLAY                                │   │
│  │  [Best legs cross-game, organized by game]               │   │
│  │  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━                   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  BOOK_LABEL dict (API key → display name):                      │
│    "draftkings" → "DK"                                          │
│    "fanatics" → "Fanatics"                                      │
│    "fanduel" → "FanDuel"                                        │
│    "pinnacle" → "Pin"                                           │
└──────────────────────────────────────────────────────────────────┘
```

---

## LAYER 8 — RESULTS TRACKING LAYER (The Sharp Review)

```
┌──────────────────────────────────────────────────────────────────┐
│                    SHARP REVIEW SYSTEM                           │
│                  slipiq_sharp_review.py                          │
│                                                                  │
│  11:00 PM AZ every night:                                        │
│                                                                  │
│  log_pick(pick)                                                  │
│    → Writes to picks_log.json (local)                            │
│    → Duplicate check: date + player_name + prop                  │
│    → Schema: {player_name, prop, direction, line, adj_floor,    │
│               confidence, platform, scenario, best_edge, type,  │
│               date, graded, hit}                                 │
│                                                                  │
│  get_pitcher_final_stats(pitcher_name)                           │
│    → MLB Stats API box score pull                                │
│    → Returns actual: K, outs, pitches, walks, hits              │
│                                                                  │
│  grade_pick(pick, actual_stats)                                  │
│    → HIT: actual_K ≥ line (if over pick)                        │
│    → MISS: actual_K < line                                       │
│    → Same logic for outs, pitches, BBs, batter props            │
│                                                                  │
│  calculate_running_stats(picks)                                  │
│    → win_rate_by_grade (A+/A/B+/B/C)                            │
│    → win_rate_by_trend (HOT/COLD/NEUTRAL)                        │
│    → win_rate_by_conf_band (70-80 / 80-90 / 90+)               │
│    → win_rate_by_scenario (DUEL/MISMATCH/BOTH_WEAK)             │
│                                                                  │
│  format_sharp_review_card() → post to #sharp-review             │
└──────────────────────────────────────────────────────────────────┘
```

---

## LAYER 9 — PERSISTENCE LAYER (Data Storage)

```
┌──────────────────────────────────────────────────────────────────┐
│                      PERSISTENCE LAYER                           │
│                                                                  │
│  CURRENT STATE:                                                  │
│  ┌────────────────────────────────────────────────────────┐     │
│  │  picks_log.json  ← Local file, auto-created            │     │
│  │  Lives at: /slipiq/picks_log.json                      │     │
│  │  Written by: slipiq_sharp_review.log_pick()            │     │
│  │  Read by: slipiq_sharp_review.run_sharp_review()       │     │
│  └────────────────────────────────────────────────────────┘     │
│                                                                  │
│  FUTURE STATE (Phase 2):                                         │
│  ┌────────────────────────────────────────────────────────┐     │
│  │  Supabase (Postgres)                                   │     │
│  │  SUPABASE_URL + SUPABASE_KEY in .env                   │     │
│  │  17 tables designed:                                   │     │
│  │    picks, results, pitcher_stats, batter_stats,        │     │
│  │    sharp_review_runs, team_k_rates, game_context,      │     │
│  │    confidence_history, line_movement, etc.             │     │
│  │  NOT YET INTEGRATED — pending Phase 2                  │     │
│  └────────────────────────────────────────────────────────┘     │
│                                                                  │
│  Rate limit state: MAX_EVENTS=15 (env var, Odds API guard)      │
│  Groq state: stateless — fresh call each morning pipeline       │
└──────────────────────────────────────────────────────────────────┘
```

---

## FULL DAILY FLOW — SEQUENCE DIAGRAM

```
6:30 AM AZ
    │
    ▼
slipiq_orchestrator.py: run_morning_pipeline()
    │
    ├──[Step 1]── slipiq_mlb_data.pull_todays_data()
    │               ↓
    │             MLB Stats API → confirmed starters list
    │             pybaseball → SwStr%, K/9, xFIP, velo per pitcher
    │             OpenWeather → dome flag, wind speed
    │             BallDontLie → team K rates, bullpen context
    │
    ├──[Step 2]── slipiq_pitcher_model.run_all_models(pitchers)
    │               ↓
    │             Negative binomial × 10K Monte Carlo per pitcher
    │             adj_floor, adj_ceiling, projection, confidence, trend
    │
    │             slipiq_batter_model.classify_game() per matchup
    │               ↓
    │             DUEL / MISMATCH / BOTH_WEAK classification
    │             Batter hit/TB/RBI projections
    │
    ├──[Step 3]── slipiq_odds_api.pull_all_lines()
    │               ↓
    │             The Odds API: pitcher_strikeouts per event
    │             Pinnacle supplement for missed events
    │             F5 ML, F3 RL, game totals per game
    │
    ├──[Step 4]── slipiq_lines.run_full_analysis()
    │               ↓
    │             Model projection vs live market line
    │             Edge calculation per pitcher
    │             MIN_CONF (55%) + MIN_EDGE (0.4) filtering
    │
    ├──[Step 5]── slipiq_curate.run_full_curation()
    │               ↓
    │             Tier 1 / Tier 2 / Tier 0 classification
    │             Pick'em vs sportsbook routing
    │             Best platform, best book, best price selection
    │
    ├──[Step 6]── slipiq_confidence_agent.enrich_picks()
    │               ↓
    │             Groq API: 0-100 score per pick
    │             Merged with model confidence → final %
    │             Guard: never cap below 5 picks
    │
    ├──[Step 7]── slipiq_book_slip_builder.build_pitcher_slip()
    │               ↓
    │             3-4 leg correlated pitcher core per game
    │             Full slip = pitcher core + batter legs (max 6)
    │             3-book side-by-side: DK | Fanatics | FanDuel
    │             Availability check: drop F3 RL if N/A, never sub
    │
    ├──[Step 8]── slipiq_discord.post_daily_brief()
    │               ↓
    │             Bot Token + channel ID (NEVER webhooks)
    │             Tier 1 picks → #daily-picks
    │             Sportsbook slips with per-book table
    │             Pick'em Fanatics card
    │             Slate parlay
    │
    └──[Step 9]── slipiq_orchestrator.log_todays_picks()
                    ↓
                  Writes all picks to picks_log.json
                  Ready for tonight's sharp review grading

11:00 PM AZ
    │
    ▼
slipiq_orchestrator.run_nightly_sharp_review()
    │
    ├── MLB Stats API: get final box scores
    ├── Grade each pick: HIT or MISS
    ├── Calculate running stats by grade/trend/conf
    ├── Format sharp review card
    └── Post to #sharp-review channel
```

---

## DISCORD SERVER CHANNEL MAP

```
SERVER: SlipIQ (Private — Jonathan only, no public selling)
│
├── 📢 SLIPIQ (Sports Props)
│   ├── #daily-picks          ← MLB morning brief (Tier 1 + slips)
│   ├── #live-alerts          ← Line moves, breakout alerts
│   └── #sharp-review         ← Nightly grading + running stats
│
├── 🏀 NBA (October 2026)
│   └── #nba-picks            ← NBA player props + breakout alerts
│                                Breakout fires BEFORE daily brief
│
├── 📈 TRADEIQ (Financial Signals — Future Build)
│   ├── #equity-signals       ← Stocks/ETFs → execute Webull
│   ├── #options-signals      ← Options setups → execute TastyTrade
│   ├── #crypto-signals       ← Crypto → execute Webull
│   ├── #forex-signals        ← Auto-executed via tastyfx (confirms here)
│   ├── #futures-signals      ← Auto-executed via NinjaTrader (confirms here)
│   └── #event-contracts      ← Kalshi + Crypto.com (SlipIQ bridge triggers)
│
└── 💬 GENERAL
    ├── #slipiq-chat          ← Sports props discussion
    ├── #tradeiq-chat         ← Trading discussion (all 5 platforms visible)
    ├── #trade-log            ← Every executed trade logged automatically
    └── #the-vault            ← General channel
```

---

## SLIPIQ → TRADEIQ INTELLIGENCE BRIDGE

```
┌─────────────────────────────────────────────────────────────────┐
│              CROSS-SYSTEM INTELLIGENCE BRIDGE                   │
│                                                                 │
│  When SlipIQ confidence ≥ 75% on a pick:                       │
│    ↓                                                            │
│  TradeIQ checks Kalshi for corresponding event contract         │
│    ↓                                                            │
│  If contract exists AND line hasn't adjusted:                   │
│    ↓                                                            │
│  Post to #event-contracts (TradeIQ channel)                     │
│                                                                 │
│  Example:                                                       │
│  SlipIQ: "Strider O7.5K — 79% confidence"                      │
│    →  TradeIQ: "Strider 8+ Ks tonight — Kalshi YES contract"   │
│    →  Books: sports prop market → Kalshi event contract         │
│    →  Edge: same underlying, different market, lagging line     │
│                                                                 │
│  This is the feature no competitor has.                         │
│  REQUIREMENT: SlipIQ confidence agent must be running first.   │
└─────────────────────────────────────────────────────────────────┘
```

---

## CODEBASE FILE DEPENDENCY GRAPH

```
slipiq_orchestrator.py  [MASTER ENTRY POINT]
    │
    ├── slipiq_mlb_data.py
    │       ├── pybaseball (external)
    │       ├── statsapi (external)
    │       └── requests (OpenWeather, BallDontLie)
    │
    ├── slipiq_pitcher_model.py
    │       ├── numpy, scipy.stats.nbinom
    │       └── requests (MLB Stats API)
    │
    ├── slipiq_batter_model.py
    │       └── slipiq_pitcher_model.py (imports pitcher_score)
    │
    ├── slipiq_odds_api.py
    │       ├── requests (The Odds API)
    │       └── slipiq_pinnacle_props.py
    │
    ├── slipiq_lines.py
    │       ├── slipiq_odds_api.py
    │       ├── slipiq_pitcher_model.py
    │       └── slipiq_confidence_agent.py
    │
    ├── slipiq_curate.py
    │       ├── slipiq_grading.py  [SHARED]
    │       └── slipiq_normalization.py
    │
    ├── slipiq_confidence_agent.py
    │       └── groq (external — NOT anthropic)
    │
    ├── slipiq_book_slip_builder.py
    │       ├── slipiq_batter_model.py
    │       └── slipiq_odds_api.py
    │
    ├── slipiq_discord.py
    │       └── discord.py (Bot Token, NOT webhooks)
    │
    └── slipiq_sharp_review.py
            ├── statsapi (box scores)
            └── slipiq_grading.py  [SHARED]

slipiq_nba_data.py          [NBA — future]
    └── nba_api (external — free, no key)
slipiq_nba_player_model.py  [NBA — future]
    └── slipiq_grading.py   [SHARED — import this, never redefine]
slipiq_nba_discord.py       [NBA — future — needs rebuild]
slipiq_nba_orchestrator.py  [NBA — future — needs fixes]
```

---

## DEPLOYMENT ARCHITECTURE

```
┌──────────────────────────────────────────────────────────────────┐
│                      RAILWAY DEPLOYMENT                          │
│                                                                  │
│  GitHub: tallerthanlife/SlipIQ                                  │
│  Deploy trigger: push to main branch                            │
│  Command: python slipiq_orchestrator.py                         │
│  Process: runs 24/7, APScheduler handles all timing internally  │
│  .env: secrets injected via Railway environment variables       │
│  railway.toml: [build] nixpacks / [deploy] startCommand        │
│                                                                  │
│  NO Railway cron — all scheduling inside APScheduler process    │
│  NO webhooks — Discord.py Bot Token throughout                  │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    DEV ENVIRONMENT                               │
│                                                                  │
│  Local: Windows, VS Code, venv, PowerShell                      │
│  Run command: py slipiq_orchestrator.py                         │
│  Note: Windows uses `py`, Railway/Linux uses `python`           │
│  IDE backup: Cursor (full codebase context via Composer)        │
│  AI backup: Claude Code CLI (terminal-based, separate from UI)  │
└──────────────────────────────────────────────────────────────────┘
```

---

## PHASE STATUS TRACKER

```
PHASE 1 — MLB (ACTIVE — Summer 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ slipiq_mlb_data.py
✅ slipiq_pitcher_model.py
✅ slipiq_batter_model.py
✅ slipiq_odds_api.py
✅ slipiq_curate.py
✅ slipiq_confidence_agent.py
✅ slipiq_lines.py
✅ slipiq_book_slip_builder.py
✅ slipiq_discord.py
✅ slipiq_sharp_review.py
✅ slipiq_orchestrator.py
✅ slipiq_grading.py
✅ slipiq_normalization.py
✅ slipiq_pinnacle_props.py
🔲 slipiq_player_ids.py        ← 30 min build — fixes batter ESPN IDs
🔲 Supabase integration        ← replaces picks_log.json

PHASE 2 — MLB HITTER PROPS (Ongoing)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔲 Expand curate_batters() coverage
🔲 Improve opposing starter FIP data pull

PHASE 3 — NBA (October 2026)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔲 Fix 5 bugs in slipiq_nba_data.py
🔲 Fix 5 bugs in slipiq_nba_player_model.py
🔲 Build slipiq_nba_curate.py (new)
🔲 Rebuild slipiq_nba_discord.py to match MLB format
🔲 Fix slipiq_nba_orchestrator.py (curation step + token fix)
🔲 Add NBA markets to slipiq_odds_api.py
🔲 Integrate NBA pipeline to slipiq_orchestrator.py (11am ET)
🔲 Add NBA to slipiq_sharp_review.py

PHASE 4 — SGP + ADVANCED (Future)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔲 SGP Correlation Agent
🔲 CLV Tracking Agent
🔲 SGP Pricing Model
🔲 Arb detection engine
🔲 SlipIQ → TradeIQ intelligence bridge (Kalshi event contracts)
```

---

*End of ARCHITECTURE_MAP.md — Update this file whenever a new file is added, a pipeline step changes, or a channel is modified. This is the living structural reference.*

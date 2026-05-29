# SlipIQ REPO_INDEX — Brain vs codebase

> Updated May 26, 2026 after NBA Phase 3 build.

## Env / config

All runtime config: **`slipiq_env.py`** (matches live `.env`). See `.env.example`.

| Key | Role |
|-----|------|
| `CHANNEL_BASKETBALL_PROPS` | NBA daily pick cards + brief |
| `DISCORD_DAILY_PICKS_CHANNEL` | MLB daily pick cards |
| `DISCORD_LIVE_ALERTS_CHANNEL` | Line moves + NBA breakout alerts |

## Quick status

| Area | Brain says | Repo has |
|------|------------|----------|
| MLB pipeline | orchestrator → model → curate → discord | ✅ `slipiq_orchestrator.py` + `slipiq_curate.py` |
| NBA pipeline | 4 modules + orchestrator integration | ✅ **Full stack** (see below) |
| Grading | `slipiq_grading.py` | ✅ Shared `calc_grade()` — NBA imports it |
| Parlay API NBA | `basketball_nba` markets | ✅ `get_nba_player_props()`, `get_all_nba_props()` |
| Sharp review NBA | separate log | ✅ `run_nba_sharp_review()` → `cache/nba_record.json` |

## Phase 1 new modules (MLB Summer 2026)

| Module | Role |
|--------|------|
| `slipiq_ev_engine.py` | EV math foundation — no-vig, edge, Kelly, CLV, breakeven |
| `slipiq_propline.py` | Prop-Line API — 1000 cr/day, Pinnacle source, dynamic polling |
| `slipiq_prizepicks.py` | PrizePicks EV engine — fixed multiplier math, intraday scanner |
| `slipiq_montecarlo.py` | Monte Carlo — correlated SGP validation, bankroll simulation |
| `slipiq_calibration.py` | Brier score, reliability curve, CLV logging |
| `slipiq_slip_router.py` | Routes curated picks to correct slip type |
| `slipiq_independent_parlay.py` | Lotto slip + ML/RL independent parlay builder |
| `slipiq_player_ids.py` | Static MLB ID lookup, normalize_name, fuzzy_match, is_batter_on_team |
| `slipiq_pitcher_props.py` | Shim resolving broken enrich_picks import |
| `slipiq_sharp_api.py` | Sharp API supplement client |
| `slipiq_propline_scanner.py` | Intraday scanner daemon — `intraday_scanner()`, `start_scanner()` |

## NBA module map (Phase 3 — implemented)

| Module | Role |
|--------|------|
| `slipiq_nba_data.py` | Schedule, game logs, pace/def, `build_player_object()`, `detect_breakout_candidates()` |
| `slipiq_nba_player_model.py` | Per-minute projection, neg binomial confidence, `run_nba_model()` |
| `slipiq_nba_confidence_agent.py` | NBA context modifiers + POST/HOLD/SKIP gate |
| `slipiq_nba_curate.py` | Morning curation → `cache/nba_latest_picks.json` |
| `slipiq_nba_discord.py` | Posts to **`CHANNEL_BASKETBALL_PROPS`** (+ live alerts for breakouts) |
| `slipiq_nba_orchestrator.py` | `run_nba_pipeline()`, `run_nba_confirm()`, `run_breakout_check()` |

## Orchestrator schedule (AZ)

| Slot | Time | Action |
|------|------|--------|
| MLB early/main/confirm | 6:30 / 8:30 / 9:15 | Existing MLB pipeline |
| **NBA main** | **11:00** | `run_nba_pipeline()` |
| **NBA confirm** | **11:45** | `run_nba_confirm()` |
| **NBA breakout** | **16:30** | Injury-window breakout scan |
| Sharp review | 23:00 | MLB + NBA via `run_all_sharp_reviews()` |

Manual: `python slipiq_orchestrator.py --nba` | `python slipiq_nba_orchestrator.py --no-discord`

## MLB gaps (unchanged)

- `slipiq_odds_api.py` — logic in `slipiq_parlayapi.py`
- `enrich_picks` / `slipiq_pitcher_props` — broken imports in legacy paths (shim added)

## Dependencies added

- `nba_api` — NBA Stats API
- `scipy` — negative binomial simulations (NBA + brain spec for MLB Ks)

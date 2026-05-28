# SlipIQ REPO_INDEX — Brain vs codebase

> Indexed from `PROJECT_BRAIN.md` (May 25, 2026) on May 26, 2026.  
> Update this file when you add/rename modules so Cursor stays aligned with reality.

## Quick status

| Area | Brain says | Repo has |
|------|------------|----------|
| Master spec | `PROJECT_BRAIN.md` | ✅ Same file in repo root |
| Orchestrator | 8am ET, `run_morning_pipeline` | ✅ `slipiq_orchestrator.py` — **12:00 + 16:00 ET**, `run_pipeline()` |
| MLB data | `pull_todays_data()` etc. | ✅ `slipiq_mlb_data.py` — `get_todays_games()` |
| Pitcher model | `run_pitcher_model(name, line)` | ✅ `slipiq_pitcher_model.py` |
| Odds/lines | `slipiq_odds_api.py` | ⚠️ Logic in **`slipiq_lines.py`** (`get_mlb_pitcher_props`, `run_full_analysis`) |
| Curation | `run_curation()` | ✅ `slipiq_curate.py` — also `select_daily_best()` used by orchestrator |
| Confidence | `enrich_picks()` + Groq | ⚠️ **`enrich_picks` missing**; agent uses `run_confidence_agent()` + `rescore_confidence()` |
| Grading | `slipiq_grading.py` | ❌ Not created — grades inline in confidence/curate |
| Book slips | `slipiq_book_slip_builder.py` | ❌ Not created — parlays in `slipiq_slate_parlay.py`, `slipiq_ml_parlay.py` |
| Sharp review | `slipiq_sharp_review.py` + `picks_log.json` | ⚠️ **`slipiq_sharp_review_agent.py`** + `slipiq_results.py` / Supabase via `slipiq_db.py` |
| Discord | `post_daily_brief()` | ✅ `slipiq_discord.py` — `SlipIQBot` class, channel env vars |
| Normalization | `slipiq_normalization.py` | ❌ Not in repo |
| Pinnacle supplement | `slipiq_pinnacle_props.py` | ⚠️ Imported inside `slipiq_lines.py` |
| Player IDs | `slipiq_player_ids.py` | ❌ Phase 1 todo |
| Supabase | Phase 2 | ⚠️ **`slipiq_db.py`** + `supabase_schema.sql` started |
| NBA stack | 4 files + bugs | ❌ Not in repo yet |

## Module map (brain filename → actual file)

| PROJECT_BRAIN module | Status | Notes |
|---------------------|--------|-------|
| `slipiq_orchestrator.py` | ✅ | Imports `slipiq_pitcher_props` — **file missing** (should call `slipiq_lines.run_full_analysis`) |
| `slipiq_mlb_data.py` | ✅ | |
| `slipiq_pitcher_model.py` | ✅ | |
| `slipiq_batter_model.py` | ✅ | Orchestrator uses `slipiq_batter_lines.run_batter_analysis` |
| `slipiq_odds_api.py` | ❌ | Split from `slipiq_lines.py` when ready |
| `slipiq_curate.py` | ✅ | Two paths: orchestrator `select_daily_best` vs `run_curation` |
| `slipiq_grading.py` | ❌ | Create per brain §2 |
| `slipiq_confidence_agent.py` | ✅ | No `enrich_picks`; `slipiq_lines` still imports it — **broken** |
| `slipiq_lines.py` | ✅ | Brain’s `run_full_analysis()` lives here |
| `slipiq_book_slip_builder.py` | ❌ | |
| `slipiq_discord.py` | ✅ | |
| `slipiq_sharp_review.py` | ❌ | Use `slipiq_sharp_review_agent.py` + `slipiq_results.py` |
| `slipiq_normalization.py` | ❌ | |
| `slipiq_pinnacle_props.py` | ❌ | Inline in lines module |
| `slipiq_pitcher_props.py` | ❌ | Referenced by orchestrator + `slipiq_ml_parlay.py` — **add wrapper or fix imports** |

## Extra modules (not in brain layout)

| File | Role |
|------|------|
| `slipiq_slip_review.py` | 6-step checklist before Discord |
| `slipiq_writer.py` | Groq/Claude daily brief text |
| `slipiq_db.py` | Supabase + JSON fallback |
| `slipiq_cache.py` | Cache helpers |
| `slipiq_batter_lines.py` | Batter prop pull + analysis |
| `slipiq_slate_parlay.py` | Multi-leg slate parlay |
| `slipiq_ml_parlay.py` | ML-tagged parlay builder |
| `slipiq_parlayapi.py` | Parlay API integration |
| `slipiq_results.py` | Pick logging + hit rates |

## Broken imports (fix before production run)

1. `slipiq_orchestrator.py` → `from slipiq_pitcher_props import run_full_pitcher_props_analysis`
2. `slipiq_ml_parlay.py` → same
3. `slipiq_lines.py` → `from slipiq_confidence_agent import enrich_picks`

**Suggested fix:** Add `slipiq_pitcher_props.py` wrapping `slipiq_lines.run_full_analysis`, and `enrich_picks()` in confidence agent (or rewire lines to `run_confidence_agent`).

## Phase 1 roadmap (from brain §7)

- [ ] `slipiq_player_ids.py`
- [ ] `slipiq_grading.py` + refactor grade calls
- [ ] `slipiq_odds_api.py` (extract from lines)
- [ ] `slipiq_book_slip_builder.py`
- [ ] Repair pitcher props + enrich_picks wiring
- [ ] Full Supabase migration off JSON-only logs

## Phase 3 — NBA (October)

All NBA files in brain §5 are **not present** in this repo; follow brain build order when season starts.

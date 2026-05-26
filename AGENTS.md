# SlipIQ — Agent instructions

This repo is the **SlipIQ** MLB/NBA sports-prop Discord bot. New sessions must load context before editing code.

## Required reading (in order)

1. **[PROJECT_BRAIN.md](./PROJECT_BRAIN.md)** — authoritative architecture, data flow, schemas, immutable rules, roadmap.
2. **[REPO_INDEX.md](./REPO_INDEX.md)** — what exists today vs what the brain describes (renames, missing modules, broken imports).
3. **[ARCHITECTURE_MAP.md](./ARCHITECTURE_MAP.md)** — visual layer map (optional).

## Run locally

```bash
cd slipiq
.\venv\Scripts\activate   # Windows
.\venv\Scripts\python.exe slipiq_orchestrator.py              # one-shot (use venv — has numpy)
.\venv\Scripts\python.exe slipiq_orchestrator.py --schedule   # scheduler
.\venv\Scripts\python.exe slipiq_curate.py                    # curation + Discord only
```

## Entry point

`slipiq_orchestrator.py` — do not add parallel schedulers; use APScheduler inside this file.

## Environment (`.env`)

All config is read through **`slipiq_env.py`** — match the keys in `.env.example` (same layout as your live `.env`). Do not introduce new env var names without adding them to `slipiq_env.py` first.

| Key | Role |
|-----|------|
| `PARLAY_API_KEY` | Primary props (3 cr `/props`) |
| `ODDS_API_KEY` / `ODDS_API_2` / `ODDS_API_3` | Failsafe odds chain |
| `ODDS_PAPI` / `SHARP_API_KEY` | Sharp / historical supplements |
| `TOMORROW_IO_API_KEY` | Weather primary |
| `DISCORD_DAILY_PICKS_CHANNEL` | MLB daily pick cards |
| `CHANNEL_TEAM_PARLAY` | Private parlay menu + suggested slips |
| `DISCORD_SHARP_REVIEW_CHANNEL` | Post-game review |
| `SUPABASE_URL` / `SUPABASE_KEY` | Optional persistence (URL auto-strips `/rest/v1`) |

## Where to work next

See **Phase 1** checkboxes in `PROJECT_BRAIN.md` §7 and the **Gaps** table in `REPO_INDEX.md`.

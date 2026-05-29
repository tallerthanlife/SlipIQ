-- ═══════════════════════════════════════════════════════════════
-- SlipIQ — Supabase Schema
-- Run this against your Supabase project SQL editor.
-- Existing tables: picks, results, pitcher_stats, batter_stats,
--   sharp_review_runs (designed in Phase 1)
-- NEW IN THIS UPDATE:
--   calibration_log   → slipiq_calibration.py prediction/result log
--   closing_lines     → Pinnacle closing lines for CLV calculation
-- ═══════════════════════════════════════════════════════════════

-- ─── Enable UUID extension ────────────────────────────────────
create extension if not exists "uuid-ossp";

-- ═══════════════════════════════════════════════════════════════
-- TABLE: calibration_log
-- Populated by slipiq_calibration.log_prediction() + log_result()
-- Primary feedback loop for model quality tracking.
-- Brier score and CLV both computed from this table.
-- ═══════════════════════════════════════════════════════════════

create table if not exists calibration_log (
    id           uuid primary key default uuid_generate_v4(),

    -- Prediction identity
    pred_id      text not null unique,      -- format: player_market_direction_timestamp
    player       text not null,
    market       text not null,             -- e.g. player_pitcher_strikeouts
    direction    text not null,             -- "over" | "under"
    line         numeric(6,2),
    sport        text not null default 'mlb',
    game_date    date,

    -- Model output at prediction time
    model_prob   numeric(8,6),              -- calibrated probability 0-1
    book_odds    integer,                   -- American odds at time of pick
    ev           numeric(8,6),             -- edge from assess_leg()
    grade        text,                     -- A / B+ / B / B- / C+ / C / D

    -- Settlement
    result       text,                     -- "WIN" | "LOSS" | "PUSH" | "NO_ACTION"
    settled      boolean not null default false,
    actual_val   numeric(6,2),             -- actual stat value post-game
    clv          numeric(8,3),             -- closing line value %

    -- Timestamps
    logged_at    timestamptz not null default now(),
    settled_at   timestamptz,

    -- Constraints
    constraint calibration_log_direction_check
        check (direction in ('over', 'under')),
    constraint calibration_log_result_check
        check (result is null or result in ('WIN', 'LOSS', 'PUSH', 'NO_ACTION'))
);

-- Indexes for calibration queries
create index if not exists idx_cal_player_date
    on calibration_log (player, game_date);
create index if not exists idx_cal_settled
    on calibration_log (settled, game_date);
create index if not exists idx_cal_sport_date
    on calibration_log (sport, game_date);
create index if not exists idx_cal_grade
    on calibration_log (grade);

-- Row-level security (optional — enables per-user access if needed)
alter table calibration_log enable row level security;
create policy "Service role full access" on calibration_log
    using (true) with check (true);


-- ═══════════════════════════════════════════════════════════════
-- TABLE: closing_lines
-- Populated by slipiq_sharp_review.fetch_closing_line()
-- Stores Pinnacle closing prices per player/market/date.
-- Used by CLV calculation: bet_price vs closing_price.
-- Cached here so we only fetch historical endpoint once per day.
-- ═══════════════════════════════════════════════════════════════

create table if not exists closing_lines (
    id             uuid primary key default uuid_generate_v4(),

    player         text not null,
    market         text not null,
    game_date      date not null,
    sport          text not null default 'mlb',

    -- Pinnacle closing prices
    over_price     integer,                 -- American odds
    under_price    integer,
    closing_line   numeric(6,2),           -- prop line at close

    -- Source metadata
    source         text default 'parlayapi_historical',
    fetched_at     timestamptz not null default now(),

    -- Unique constraint: one closing line per player/market/date
    constraint closing_lines_unique
        unique (player, market, game_date, sport)
);

create index if not exists idx_closing_player_date
    on closing_lines (player, game_date);
create index if not exists idx_closing_date
    on closing_lines (game_date);

alter table closing_lines enable row level security;
create policy "Service role full access" on closing_lines
    using (true) with check (true);


-- ═══════════════════════════════════════════════════════════════
-- TABLE: picks (existing — schema reference only, do not re-run)
-- ═══════════════════════════════════════════════════════════════

-- create table if not exists picks (
--     id           uuid primary key default uuid_generate_v4(),
--     player_name  text not null,
--     prop         text not null,
--     direction    text not null,
--     line         numeric(6,2),
--     adj_floor    numeric(6,2),
--     confidence   integer,
--     platform     text,
--     scenario     text,
--     best_edge    numeric(8,4),
--     type         text,
--     date         date not null,
--     graded       boolean default false,
--     hit          boolean,
--     created_at   timestamptz default now()
-- );


-- ═══════════════════════════════════════════════════════════════
-- VIEW: calibration_summary_30d
-- Pre-computed view for nightly calibration report.
-- slipiq_calibration.calibration_summary() can query this
-- instead of computing from raw records.
-- ═══════════════════════════════════════════════════════════════

create or replace view calibration_summary_30d as
select
    sport,
    count(*) filter (where settled)                              as n_settled,
    count(*) filter (where settled and result = 'WIN')           as n_wins,
    count(*) filter (where settled and result = 'LOSS')          as n_losses,
    round(
        count(*) filter (where settled and result = 'WIN')::numeric
        / nullif(count(*) filter (where settled and result in ('WIN','LOSS')), 0)
        * 100, 1
    )                                                             as hit_rate_pct,
    round(
        avg(
            case when settled and result in ('WIN','LOSS')
            then (model_prob - case when result = 'WIN' then 1.0 else 0.0 end)^2
            end
        )::numeric, 6
    )                                                             as brier_score,
    round(avg(clv) filter (where clv is not null)::numeric, 3)  as avg_clv_pct,
    round(avg(ev)  filter (where ev  is not null)::numeric, 4)  as avg_ev,
    grade
from calibration_log
where game_date >= current_date - interval '30 days'
group by sport, grade
order by sport, grade;


-- ═══════════════════════════════════════════════════════════════
-- FUNCTION: upsert_calibration_log
-- Called by slipiq_calibration._supabase_upsert()
-- Handles both insert (new prediction) and update (result settlement).
-- ═══════════════════════════════════════════════════════════════

create or replace function upsert_calibration_log(
    p_pred_id    text,
    p_player     text,
    p_market     text,
    p_direction  text,
    p_line       numeric,
    p_model_prob numeric,
    p_book_odds  integer,
    p_ev         numeric,
    p_grade      text,
    p_sport      text,
    p_game_date  date,
    p_result     text    default null,
    p_settled    boolean default false,
    p_actual_val numeric default null,
    p_clv        numeric default null
)
returns void
language plpgsql
security definer
as $$
begin
    insert into calibration_log (
        pred_id, player, market, direction, line,
        model_prob, book_odds, ev, grade, sport, game_date,
        result, settled, actual_val, clv
    )
    values (
        p_pred_id, p_player, p_market, p_direction, p_line,
        p_model_prob, p_book_odds, p_ev, p_grade, p_sport, p_game_date,
        p_result, p_settled, p_actual_val, p_clv
    )
    on conflict (pred_id) do update set
        result      = coalesce(excluded.result,      calibration_log.result),
        settled     = coalesce(excluded.settled,     calibration_log.settled),
        actual_val  = coalesce(excluded.actual_val,  calibration_log.actual_val),
        clv         = coalesce(excluded.clv,         calibration_log.clv),
        settled_at  = case when excluded.settled then now() else calibration_log.settled_at end;
end;
$$;

-- SlipIQ picks table — run once in Supabase SQL Editor

create table if not exists picks (
  id bigint generated always as identity primary key,
  pick_date date not null,
  pitcher text not null,
  direction text not null,
  line numeric not null,
  projection numeric,
  grade text,
  confidence numeric,
  model_confidence numeric,
  ev_score numeric,
  trend text,
  bookmaker text,
  result text,
  actual_strikeouts integer,
  actual_game_date date,
  logged_at text,
  updated_at text,
  settled_by text,
  slip_review jsonb,
  unique (pick_date, pitcher, direction, line)
);

create index if not exists picks_pick_date_idx on picks (pick_date desc);
create index if not exists picks_result_idx on picks (result);

alter table picks enable row level security;

-- Service role bypasses RLS; for anon key allow read/write (tighten in production)
create policy "Allow all for service" on picks
  for all using (true) with check (true);

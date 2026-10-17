-- Applied to project pycaryznonirjmwkybsb on 18 Oct 2026 (migration: init_schema).
create table public.leads (
  id text primary key, business_name text not null,
  lane text not null check (lane in ('established','new_opening')), lane_label text,
  category text, emirate text not null, area text, cluster text, maps_link text, website text,
  rating numeric, review_count integer, price_level integer, has_phone boolean default false,
  oldest_review_date date, licence_issue_date date, card_signal text, card_signal_note text,
  restricted boolean default false, chain_check text, date_collected date,
  data_source text not null default 'google_places' check (data_source in ('google_places','licence_data','example')),
  is_example boolean not null default false,
  status text not null default 'open' check (status in ('open','interested','not_now','has_terminal','signed')),
  score integer, score_parts jsonb, reason text, draft_message text, updated_at timestamptz default now()
);
create index leads_emirate_idx on public.leads (emirate);
create index leads_cluster_idx on public.leads (cluster);
create table public.visits (
  id bigint generated always as identity primary key,
  lead_id text not null references public.leads(id) on delete cascade,
  outcome text not null check (outcome in ('interested','not_now','has_terminal','signed')),
  note text, created_at timestamptz not null default now()
);
create index visits_lead_idx on public.visits (lead_id);
create table public.merchants (
  id text primary key, name text not null, category text, emirate text, area text,
  activation_date date, terminals integer, courtesy_call_done boolean,
  vol_apr integer, vol_may integer, vol_jun integer, vol_jul integer, vol_aug integer, vol_sep integer,
  is_simulated boolean not null default true
);
-- RLS on, no policies: only the server-side secret key can read/write.
alter table public.leads enable row level security;
alter table public.visits enable row level security;
alter table public.merchants enable row level security;

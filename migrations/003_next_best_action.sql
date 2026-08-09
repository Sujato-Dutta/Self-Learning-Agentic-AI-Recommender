-- Conversion-oriented next-best-action policy, grounded market evidence, and reward attribution.
alter table products add column if not exists prerequisite_product_ids jsonb not null default '[]';
alter table products add column if not exists career_outcomes jsonb not null default '[]';
alter table products add column if not exists social_proof_text text;
alter table products add column if not exists promotion_ends_at timestamptz;

create table if not exists market_signals (
  id uuid primary key default gen_random_uuid(), name text not null unique, claim text not null,
  source_name text not null, source_url text not null, skills jsonb not null default '[]',
  published_at timestamptz not null, valid_until timestamptz not null,
  is_active boolean not null default true, created_at timestamptz not null default now()
);
create index if not exists ix_market_signals_active_valid on market_signals(is_active, valid_until);
create index if not exists ix_market_signals_skills on market_signals using gin(skills);

create table if not exists next_best_actions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  recommendation_id uuid not null unique references recommendations(id) on delete cascade,
  product_id uuid references products(id) on delete set null,
  action_type text not null, persuasion_strategy text not null,
  headline text not null, message text not null, rationale text not null,
  intent_stage text not null, purchase_propensity double precision not null,
  expected_conversion_probability double precision not null,
  expected_revenue numeric(12,2) not null default 0,
  incremental_expected_revenue numeric(12,2) not null default 0,
  evidence_event_ids jsonb not null default '[]', market_signal_ids jsonb not null default '[]',
  policy_context jsonb not null default '{}', policy_version text not null default 'nba-v1',
  status text not null default 'active', deliver_at timestamptz, expires_at timestamptz not null,
  cumulative_reward double precision not null default 0, outcome_type text, outcome_at timestamptz,
  created_at timestamptz not null default now(),
  constraint ck_nba_propensity check (purchase_propensity between 0 and 1),
  constraint ck_nba_conversion_probability check (expected_conversion_probability between 0 and 1)
);
create index if not exists ix_nba_user_created on next_best_actions(user_id, created_at desc);
create index if not exists ix_nba_status_delivery on next_best_actions(status, deliver_at);
create index if not exists ix_nba_product on next_best_actions(product_id, created_at desc);
create index if not exists ix_nba_action_strategy on next_best_actions(action_type, persuasion_strategy, created_at desc);

create table if not exists next_best_action_rewards (
  id uuid primary key default gen_random_uuid(),
  decision_id uuid not null references next_best_actions(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  product_id uuid references products(id) on delete set null,
  event_id text not null unique, event_type text not null, reward double precision not null,
  metadata jsonb not null default '{}', created_at timestamptz not null default now(),
  constraint ck_nba_reward_range check (reward between -2 and 2)
);
create index if not exists ix_nba_rewards_decision on next_best_action_rewards(decision_id, created_at);
create index if not exists ix_nba_rewards_user on next_best_action_rewards(user_id, created_at desc);

alter table market_signals enable row level security;
alter table next_best_actions enable row level security;
alter table next_best_action_rewards enable row level security;
drop policy if exists nba_own_rows on next_best_actions;
create policy nba_own_rows on next_best_actions for select using (auth.uid() = user_id);
drop policy if exists nba_rewards_own_rows on next_best_action_rewards;
create policy nba_rewards_own_rows on next_best_action_rewards for select using (auth.uid() = user_id);

-- SmartReco production schema for Supabase PostgreSQL.
create extension if not exists pgcrypto;

do $$ begin
  create type user_role as enum ('user', 'admin');
exception when duplicate_object then null; end $$;
do $$ begin
  create type outbox_status as enum ('pending', 'processing', 'complete', 'failed');
exception when duplicate_object then null; end $$;

create table if not exists users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  password_hash text not null,
  role user_role not null default 'user',
  personalization_enabled boolean not null default true,
  created_at timestamptz not null default now(),
  last_login_at timestamptz
);

create table if not exists products (
  id uuid primary key default gen_random_uuid(), title text not null, slug text not null unique,
  description text not null, category text not null, difficulty text not null,
  price numeric(10,2) not null check (price >= 0),
  original_price numeric(10,2) check (original_price is null or original_price >= price),
  duration_hours double precision not null,
  skills jsonb not null default '[]', tags jsonb not null default '[]', image_url text not null default '',
  is_bundle boolean not null default false, bundled_product_ids jsonb not null default '[]',
  is_active boolean not null default true, version integer not null default 1,
  popularity double precision not null default .5, created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists ix_products_active_category on products(is_active, category);
create index if not exists ix_products_difficulty_price on products(difficulty, price);
create index if not exists ix_products_is_bundle on products(is_bundle);

create table if not exists events (
  id uuid primary key default gen_random_uuid(), event_id text not null unique,
  user_id uuid not null references users(id) on delete cascade, session_id text not null,
  event_type text not null, product_id uuid references products(id) on delete set null,
  search_query text, metadata jsonb not null default '{}', occurred_at timestamptz not null,
  received_at timestamptz not null default now()
);
create index if not exists ix_events_user_occurred on events(user_id, occurred_at desc);
create index if not exists ix_events_session_occurred on events(session_id, occurred_at desc);
create index if not exists ix_events_type_received on events(event_type, received_at desc);

create table if not exists behavior_profiles (
  id uuid primary key default gen_random_uuid(), user_id uuid not null unique references users(id) on delete cascade,
  version integer not null default 1, profile_hash text not null,
  profile_data jsonb not null default '{}', evidence_event_ids jsonb not null default '[]',
  corrections jsonb not null default '{}', timeline jsonb not null default '[]', updated_at timestamptz not null default now()
);
create index if not exists ix_behavior_profile_hash on behavior_profiles(profile_hash);

create table if not exists recommendations (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references users(id) on delete cascade,
  behavior_profile_version integer not null, profile_hash text not null, headline text not null,
  narrative text not null, reason_summary text not null, confidence double precision not null,
  status text not null default 'active', trigger_type text not null, model_name text not null,
  prompt_version text not null, degraded boolean not null default false,
  created_at timestamptz not null default now(), expires_at timestamptz not null
);
create index if not exists ix_recommendations_user_created on recommendations(user_id, created_at desc);
create index if not exists ix_recommendations_profile_expiry on recommendations(profile_hash, expires_at);

create table if not exists recommendation_items (
  id uuid primary key default gen_random_uuid(), recommendation_id uuid not null references recommendations(id) on delete cascade,
  product_id uuid not null references products(id) on delete cascade, rank integer not null,
  retrieval_score double precision not null, rerank_score double precision not null,
  final_score double precision not null, reason text not null,
  evidence_event_ids jsonb not null default '[]', score_breakdown jsonb not null default '{}',
  unique(recommendation_id, product_id)
);

create table if not exists recommendation_feedback (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references users(id) on delete cascade,
  recommendation_id uuid references recommendations(id) on delete set null,
  product_id uuid not null references products(id) on delete cascade,
  feedback_type text not null, created_at timestamptz not null default now()
);
create index if not exists ix_feedback_user_product on recommendation_feedback(user_id, product_id, created_at desc);

create table if not exists product_vector_outbox (
  id uuid primary key default gen_random_uuid(), product_id uuid not null references products(id) on delete cascade,
  operation text not null check(operation in ('upsert','delete')), product_version integer not null,
  status outbox_status not null default 'pending', attempts integer not null default 0,
  last_error text, next_attempt_at timestamptz not null default now(), created_at timestamptz not null default now(),
  completed_at timestamptz
);
create index if not exists ix_outbox_status_next on product_vector_outbox(status, next_attempt_at);

create table if not exists product_sync_failures (
  id uuid primary key default gen_random_uuid(), outbox_id uuid not null, product_id uuid not null,
  error text not null, attempts integer not null, created_at timestamptz not null default now()
);

create table if not exists agent_runs (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references users(id) on delete cascade,
  trigger_type text not null, status text not null, trace_url text, node_trace jsonb not null default '[]',
  retrieval_query text, candidates jsonb not null default '[]', cache_hit boolean not null default false,
  mesh_called boolean not null default false, latency_ms double precision not null default 0,
  error text, created_at timestamptz not null default now()
);
create index if not exists ix_agent_runs_user_created on agent_runs(user_id, created_at desc);

create table if not exists notification_preferences (
  user_id uuid primary key references users(id) on delete cascade, email_enabled boolean not null default false,
  preferred_hour integer not null default 17 check(preferred_hour between 0 and 23),
  timezone text not null default 'UTC', unsubscribe_token text not null unique
);

create table if not exists scheduled_deliveries (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references users(id) on delete cascade,
  recommendation_id uuid references recommendations(id) on delete set null, status text not null,
  attempts integer not null default 0, error text, scheduled_at timestamptz not null, delivered_at timestamptz
);

alter table users enable row level security;
alter table events enable row level security;
alter table behavior_profiles enable row level security;
alter table recommendations enable row level security;
alter table recommendation_items enable row level security;
alter table recommendation_feedback enable row level security;

-- The service role bypasses RLS. These policies also support deployments that map
-- application user UUIDs into the Supabase JWT `sub` claim.
drop policy if exists users_read_self on users;
create policy users_read_self on users for select using (auth.uid() = id);
drop policy if exists events_own_rows on events;
create policy events_own_rows on events for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists profiles_own_rows on behavior_profiles;
create policy profiles_own_rows on behavior_profiles for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists recommendations_own_rows on recommendations;
create policy recommendations_own_rows on recommendations for select using (auth.uid() = user_id);
drop policy if exists feedback_own_rows on recommendation_feedback;
create policy feedback_own_rows on recommendation_feedback for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
drop policy if exists recommendation_items_through_owner on recommendation_items;
create policy recommendation_items_through_owner on recommendation_items for select using (
  exists(select 1 from recommendations r where r.id = recommendation_id and r.user_id = auth.uid())
);

-- Durable, per-user course access created by demo purchases.
create table if not exists user_enrollments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  product_id uuid not null references products(id) on delete cascade,
  source_product_id uuid references products(id) on delete set null,
  purchased_at timestamptz not null default now(),
  constraint uq_user_enrollment_product unique(user_id, product_id)
);

create index if not exists ix_user_enrollments_user_purchased
  on user_enrollments(user_id, purchased_at desc);
create index if not exists ix_user_enrollments_product
  on user_enrollments(product_id);
create index if not exists ix_user_enrollments_source_product
  on user_enrollments(source_product_id);

-- Preserve any purchases recorded before durable enrollment storage existed.
insert into user_enrollments (id, user_id, product_id, source_product_id, purchased_at)
select gen_random_uuid(), legacy.user_id, legacy.product_id, legacy.product_id, legacy.occurred_at
from (
  select distinct on (user_id, product_id)
    user_id, product_id, occurred_at
  from events
  where event_type = 'enrollment' and product_id is not null
  order by user_id, product_id, occurred_at asc
) as legacy
on conflict (user_id, product_id) do nothing;

-- A legacy bundle purchase also grants each catalog component. Keep the bundle
-- as the provenance source while the uniqueness constraint prevents duplicates.
insert into user_enrollments (id, user_id, product_id, source_product_id, purchased_at)
select
  gen_random_uuid(),
  legacy.user_id,
  component.product_id::uuid,
  legacy.product_id,
  legacy.occurred_at
from (
  select distinct on (user_id, product_id)
    user_id, product_id, occurred_at
  from events
  where event_type = 'enrollment' and product_id is not null
  order by user_id, product_id, occurred_at asc
) as legacy
join products as root on root.id = legacy.product_id and root.is_bundle
cross join lateral jsonb_array_elements_text(root.bundled_product_ids) as component(product_id)
on conflict (user_id, product_id) do nothing;

alter table user_enrollments enable row level security;

drop policy if exists user_enrollments_own_rows on user_enrollments;
create policy user_enrollments_own_rows on user_enrollments for select
  using (auth.uid() = user_id);

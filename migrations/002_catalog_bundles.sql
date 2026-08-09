-- Add first-class course bundle metadata to an existing SmartReco catalog.
alter table products add column if not exists original_price numeric(10,2);
alter table products add column if not exists is_bundle boolean not null default false;
alter table products add column if not exists bundled_product_ids jsonb not null default '[]';
create index if not exists ix_products_is_bundle on products(is_bundle);

alter table products drop constraint if exists ck_products_discount_price;
alter table products add constraint ck_products_discount_price check (
  original_price is null or original_price >= price
);

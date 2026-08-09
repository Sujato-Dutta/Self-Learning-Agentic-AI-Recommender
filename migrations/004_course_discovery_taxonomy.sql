-- Managed skill bundles and long-form, behavior-tracked course discovery pages.
alter table products add column if not exists skill_bundle text not null default 'Python Engineering';
alter table products add column if not exists content_sections jsonb not null default '[]';
create index if not exists ix_products_skill_bundle on products(skill_bundle);

from collections.abc import Generator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine_kwargs = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def apply_sqlite_catalog_migration() -> None:
    """Upgrade an existing local database without deleting learner data."""
    if not settings.database_url.startswith("sqlite") or not inspect(engine).has_table("products"):
        return
    columns = {column["name"] for column in inspect(engine).get_columns("products")}
    statements = {
        "original_price": "ALTER TABLE products ADD COLUMN original_price NUMERIC(10, 2)",
        "is_bundle": "ALTER TABLE products ADD COLUMN is_bundle BOOLEAN NOT NULL DEFAULT 0",
        "bundled_product_ids": "ALTER TABLE products ADD COLUMN bundled_product_ids JSON NOT NULL DEFAULT '[]'",
        "prerequisite_product_ids": "ALTER TABLE products ADD COLUMN prerequisite_product_ids JSON NOT NULL DEFAULT '[]'",
        "career_outcomes": "ALTER TABLE products ADD COLUMN career_outcomes JSON NOT NULL DEFAULT '[]'",
        "social_proof_text": "ALTER TABLE products ADD COLUMN social_proof_text VARCHAR(500)",
        "promotion_ends_at": "ALTER TABLE products ADD COLUMN promotion_ends_at DATETIME",
        "skill_bundle": "ALTER TABLE products ADD COLUMN skill_bundle VARCHAR(80) NOT NULL DEFAULT 'Python Engineering'",
        "content_sections": "ALTER TABLE products ADD COLUMN content_sections JSON NOT NULL DEFAULT '[]'",
    }
    with engine.begin() as connection:
        for name, statement in statements.items():
            if name not in columns:
                connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_products_skill_bundle ON products (skill_bundle)"
        )
        # Base.metadata.create_all creates the entitlement table for local
        # installs. Backfill any purchase events written by older builds so a
        # local-to-current upgrade preserves course access.
        connection_inspector = inspect(connection)
        if connection_inspector.has_table("user_enrollments") and connection_inspector.has_table("events"):
            connection.exec_driver_sql(
                """
                INSERT OR IGNORE INTO user_enrollments
                    (id, user_id, product_id, source_product_id, purchased_at)
                SELECT id, user_id, product_id, product_id, occurred_at
                FROM events
                WHERE event_type = 'enrollment' AND product_id IS NOT NULL
                """
            )
            connection.exec_driver_sql(
                """
                INSERT OR IGNORE INTO user_enrollments
                    (id, user_id, product_id, source_product_id, purchased_at)
                SELECT lower(hex(randomblob(16))), events.user_id, components.value,
                       events.product_id, events.occurred_at
                FROM events
                JOIN products ON products.id = events.product_id AND products.is_bundle = 1
                JOIN json_each(products.bundled_product_ids) AS components
                WHERE events.event_type = 'enrollment' AND events.product_id IS NOT NULL
                """
            )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

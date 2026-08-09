"""Idempotently load the production catalog without creating demo accounts."""

from src.database import SessionLocal
from src.seed import seed_database

if __name__ == "__main__":
    with SessionLocal() as session:
        seed_database(session, catalog_only=True)
    print("SmartReco catalog, bundles, taxonomy, content, and market signals are ready.")

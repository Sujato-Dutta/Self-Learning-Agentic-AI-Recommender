from src.database import Base, SessionLocal, apply_sqlite_catalog_migration, engine
from src.seed import seed_database

if __name__ == "__main__":
    Base.metadata.create_all(engine)
    apply_sqlite_catalog_migration()
    with SessionLocal() as session:
        seed_database(session)
    print("SmartReco demo catalog and accounts are ready.")

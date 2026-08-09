import os
import tempfile
from pathlib import Path

import pytest

TEST_DB = Path(tempfile.gettempdir()) / "smartreco-tests.sqlite3"
os.environ.update({
    "APP_ENV": "test",
    "DATABASE_URL": f"sqlite:///{TEST_DB.as_posix()}",
    "SECRET_KEY": "test-secret-key-that-is-at-least-thirty-two-characters",
    "SCHEDULER_ENABLED": "false",
    "MESH_CALLS_ENABLED": "false",
    "MESH_API_KEY": "",
    "PINECONE_API_KEY": "",
    "LANGSMITH_TRACING": "false",
})

from src.database import (
    Base,
    SessionLocal,
    engine,
)
from src.seed import seed_database


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        seed_database(session)
    yield


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect

from src.config import Settings
from src.main import app
from src.models import OutboxStatus, Product, ProductVectorOutbox, User
from src.services.outbox_service import OutboxService


def _login_learner(client: TestClient) -> None:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    client.post("/auth/login", data={
        "email": "learner@smartreco.dev",
        "password": "LearnerDemo123!",
        "csrf_token": csrf,
    }, follow_redirects=False)


def test_orm_ids_and_enums_match_the_supabase_schema():
    dialect = postgresql_dialect()
    assert User.__table__.c.id.type.compile(dialect=dialect) == "UUID"
    assert ProductVectorOutbox.__table__.c.id.type.compile(dialect=dialect) == "UUID"
    assert User.__table__.c.role.type.name == "user_role"
    assert ProductVectorOutbox.__table__.c.status.type.name == "outbox_status"


def test_low_value_event_batch_does_not_rebuild_profile(monkeypatch):
    calls = []
    monkeypatch.setattr("src.api.events.aggregate_profile", lambda *_args, **_kwargs: calls.append(True))
    with TestClient(app) as client:
        _login_learner(client)
        product_id = client.get("/api/products").json()[0]["id"]
        response = client.post("/api/events/batch", json={"events": [{
            "event_id": "cost-card-impression-001",
            "session_id": "cost-control-session",
            "event_type": "card_impression",
            "product_id": product_id,
            "metadata": {"position": 1},
            "occurred_at": "2026-08-08T10:00:00Z",
        }]})
        assert response.status_code == 200
        assert response.json()["accepted"] == 1
        assert calls == []


def test_counterfactual_preview_never_uses_vector_or_mesh(monkeypatch):
    with TestClient(app) as client:
        _login_learner(client)
        monkeypatch.setattr(
            app.state.retrieval.vector_service,
            "query",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("vector call not allowed")),
        )
        response = client.post("/api/recommendations/simulate", json={
            "career_goal": "Build Agentic AI systems",
            "budget_max": 7000,
            "weekly_hours": 8,
            "difficulty": "advanced",
            "exploration": .5,
            "mastery": .5,
        })
        assert response.status_code == 200
        assert response.json()["retrieval_mode"] == "sql_preview"


def test_reading_next_best_action_never_triggers_generation(monkeypatch):
    with TestClient(app) as client:
        _login_learner(client)
        monkeypatch.setattr(
            app.state.recommendations,
            "generate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("generation not allowed")),
        )
        response = client.get("/api/next-best-action")
        assert response.status_code == 200
        assert response.json() is None


def test_vector_outbox_coalesces_versions_and_batches_embeddings(db):
    product = db.scalar(select(Product).where(Product.is_bundle.is_(False)))
    db.query(ProductVectorOutbox).delete()
    product.version = 3
    db.add_all([
        ProductVectorOutbox(product_id=product.id, operation="upsert", product_version=version)
        for version in (1, 2, 3)
    ])
    db.commit()

    class RecordingVectors:
        def __init__(self):
            self.batches = []

        def upsert_products(self, products):
            self.batches.append([item.id for item in products])

        def delete_products(self, _ids):
            raise AssertionError("no delete expected")

    vectors = RecordingVectors()
    service = OutboxService(Settings(app_env="test", mesh_calls_enabled=True), vectors)
    result = service.process_pending(db)
    jobs = list(db.scalars(select(ProductVectorOutbox).order_by(ProductVectorOutbox.product_version)))

    assert result["superseded"] == 2
    assert result["succeeded"] == 1
    assert vectors.batches == [[product.id]]
    assert all(job.status == OutboxStatus.complete for job in jobs)

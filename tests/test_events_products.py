from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from src.models import OutboxStatus, Product, ProductVectorOutbox, User
from src.repositories.events import ingest_events
from src.repositories.products import archive_product, create_product, update_product
from src.schemas.events import EventIn
from src.schemas.products import ProductInput


def product_input(title="Reliable AI Systems"):
    return ProductInput(title=title, description="A practical production course with enough useful detail.",
        category="Agentic AI", difficulty="advanced", price=3000, duration_hours=12,
        skills=["RAG"], tags=["production"], image_url="/assets/course%20covers/agentic_ai.png")


def test_product_crud_creates_transactional_outbox(db):
    product = create_product(db, product_input())
    job = db.scalar(select(ProductVectorOutbox).where(ProductVectorOutbox.product_id == product.id,
                    ProductVectorOutbox.product_version == 1))
    assert job and job.operation == "upsert"
    update_product(db, product, product_input("Reliable Agent Systems"))
    assert product.version == 2
    archive_product(db, product)
    assert not product.is_active and product.version == 3
    operations = list(db.scalars(select(ProductVectorOutbox.operation).where(ProductVectorOutbox.product_id == product.id)))
    assert operations == ["upsert", "upsert", "delete"]


def test_bundle_crud_verifies_components_and_computes_original_price(db):
    components = list(db.scalars(select(Product).where(Product.is_bundle.is_(False)).limit(2)))
    bundle = create_product(db, ProductInput(
        title="Verified Test Bundle", description="A connected test bundle with real catalog membership.",
        category="Professional Bundle", difficulty="all-levels", price=100,
        duration_hours=20, skills=["Python"], tags=["bundle"], is_bundle=True,
        bundled_product_ids=[product.id for product in components],
        career_outcomes=["complete a verified learning path"],
    ))
    assert bundle.original_price == sum((product.price for product in components), start=0)
    with pytest.raises(ValueError, match="referenced products"):
        create_product(db, ProductInput(
            title="Broken Test Bundle", description="A bundle whose component does not exist in the catalog.",
            category="Professional Bundle", difficulty="all-levels", price=100,
            duration_hours=10, is_bundle=True, bundled_product_ids=["missing-product-id"],
        ))


def test_event_ingestion_is_idempotent(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    event = EventIn(event_id="event-test-idempotent", session_id="session-test-id", event_type="search",
                    search_query="agentic ai", occurred_at=datetime.now(timezone.utc))
    assert ingest_events(db, user.id, [event]) == (1, 0)
    assert ingest_events(db, user.id, [event]) == (0, 1)


def test_outbox_retry_backoff_and_dead_letter(db, monkeypatch):
    from src.config import Settings
    from src.services.outbox_service import OutboxService
    product = db.scalar(select(Product).limit(1))
    db.query(ProductVectorOutbox).delete()
    job = ProductVectorOutbox(product_id=product.id, operation="upsert", product_version=1)
    db.add(job)
    db.commit()
    class FailingVectors:
        def upsert_product(self, _product): raise RuntimeError("pinecone down")
        def delete_product(self, _id): raise RuntimeError("pinecone down")
    service = OutboxService(Settings(app_env="test", outbox_max_retries=1, mesh_calls_enabled=True), FailingVectors())
    result = service.process_pending(db)
    db.refresh(job)
    assert result["failed"] == 1 and job.status == OutboxStatus.failed

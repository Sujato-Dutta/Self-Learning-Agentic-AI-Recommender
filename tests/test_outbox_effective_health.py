from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.models import OutboxStatus, Product, ProductSyncFailure, ProductVectorOutbox
from src.observability.metrics import SYNC_DEAD_LETTER, SYNC_LAG, SYNC_PENDING
from src.services.outbox_service import OutboxService


def _isolate_active_product(db, *, version: int = 3) -> Product:
    db.query(ProductSyncFailure).delete()
    db.query(ProductVectorOutbox).delete()
    products = list(db.scalars(select(Product)))
    product = products[0]
    for item in products:
        item.is_active = item.id == product.id
    product.version = version
    db.commit()
    return product


def _job(product: Product, version: int, status: OutboxStatus, created_at: datetime,
         operation: str = "upsert") -> ProductVectorOutbox:
    return ProductVectorOutbox(
        product_id=product.id,
        operation=operation,
        product_version=version,
        status=status,
        created_at=created_at,
        next_attempt_at=created_at,
    )


def test_effective_health_ignores_superseded_jobs_and_keeps_failure_history(db):
    product = _isolate_active_product(db)
    now = datetime.now(timezone.utc)
    obsolete_failed = _job(product, 1, OutboxStatus.failed, now - timedelta(hours=3))
    db.add_all([
        obsolete_failed,
        _job(product, 2, OutboxStatus.pending, now - timedelta(hours=2)),
        _job(product, 3, OutboxStatus.complete, now - timedelta(hours=1)),
    ])
    db.flush()
    db.add(ProductSyncFailure(
        outbox_id=obsolete_failed.id,
        product_id=product.id,
        error="historical provider outage",
        attempts=5,
    ))
    db.commit()

    assert OutboxService.effective_health(db) == {
        "pending": 0,
        "processing": 0,
        "failed": 0,
        "complete": 1,
        "active_synced": 1,
        "active_unsynced": 0,
        "history_failures": 1,
    }
    assert [job.status for job in OutboxService.effective_jobs(db)] == [OutboxStatus.complete]


def test_effective_health_uses_created_at_for_same_version(db):
    product = _isolate_active_product(db)
    now = datetime.now(timezone.utc)
    db.add_all([
        _job(product, 3, OutboxStatus.complete, now - timedelta(minutes=2)),
        _job(product, 3, OutboxStatus.processing, now - timedelta(minutes=1)),
    ])
    db.commit()

    health = OutboxService.effective_health(db)
    assert health["complete"] == 0
    assert health["processing"] == 1
    assert health["active_synced"] == 0
    assert health["active_unsynced"] == 1


def test_active_product_without_current_complete_upsert_is_unsynced(db):
    product = _isolate_active_product(db, version=4)
    db.add(_job(
        product,
        3,
        OutboxStatus.complete,
        datetime.now(timezone.utc),
    ))
    db.commit()

    health = OutboxService.effective_health(db)
    assert health["complete"] == 1
    assert health["active_synced"] == 0
    assert health["active_unsynced"] == 1


def test_effective_health_counts_a_current_dead_letter(db):
    product = _isolate_active_product(db)
    current_failed = _job(
        product,
        3,
        OutboxStatus.failed,
        datetime.now(timezone.utc),
    )
    db.add(current_failed)
    db.flush()
    db.add(ProductSyncFailure(
        outbox_id=current_failed.id,
        product_id=product.id,
        error="current provider outage",
        attempts=5,
    ))
    db.commit()

    health = OutboxService.effective_health(db)
    assert health["failed"] == 1
    assert health["active_unsynced"] == 1
    assert health["history_failures"] == 1
    OutboxService.refresh_metrics(db)
    assert SYNC_DEAD_LETTER._value.get() == 1


def test_refresh_metrics_uses_effective_jobs_and_resets_lag(db):
    product = _isolate_active_product(db, version=2)
    now = datetime.now(timezone.utc)
    obsolete_failed = _job(product, 1, OutboxStatus.failed, now - timedelta(hours=4))
    db.add_all([
        obsolete_failed,
        _job(product, 2, OutboxStatus.complete, now - timedelta(hours=1)),
    ])
    db.flush()
    db.add(ProductSyncFailure(
        outbox_id=obsolete_failed.id,
        product_id=product.id,
        error="old failure",
        attempts=5,
    ))
    db.commit()

    SYNC_LAG.set(123)
    OutboxService.refresh_metrics(db)
    assert SYNC_PENDING._value.get() == 0
    assert SYNC_DEAD_LETTER._value.get() == 0
    assert SYNC_LAG._value.get() == 0

    product.version = 3
    db.add(_job(product, 3, OutboxStatus.processing, now - timedelta(minutes=5)))
    db.commit()
    OutboxService.refresh_metrics(db)
    assert SYNC_PENDING._value.get() == 1
    assert SYNC_DEAD_LETTER._value.get() == 0
    assert SYNC_LAG._value.get() >= 290

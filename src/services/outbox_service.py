from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config import Settings
from src.models import OutboxStatus, Product, ProductSyncFailure, ProductVectorOutbox
from src.observability.logging import logger
from src.observability.metrics import (
    SYNC_DEAD_LETTER,
    SYNC_LAG,
    SYNC_PENDING,
    SYNC_SUCCESS,
)
from src.services.cache_service import cache
from src.services.vector_service import VectorService


class OutboxService:
    def __init__(self, settings: Settings, vectors: VectorService) -> None:
        self.settings = settings
        self.vectors = vectors

    def process_pending(self, db: Session, limit: int = 25) -> dict[str, int]:
        # Product embeddings are generated through Mesh. Leave jobs pending while
        # the cost-control gate is off instead of burning retries or credits.
        if not self.settings.mesh_calls_enabled:
            self.refresh_metrics(db)
            return {"processed": 0, "succeeded": 0, "failed": 0, "paused": 1}
        now = datetime.now(timezone.utc)
        jobs = list(db.scalars(select(ProductVectorOutbox).where(
            ProductVectorOutbox.status == OutboxStatus.pending,
            ProductVectorOutbox.next_attempt_at <= now,
        ).order_by(ProductVectorOutbox.created_at).limit(limit)))
        result = {"processed": len(jobs), "succeeded": 0, "failed": 0, "superseded": 0}

        # Keep only the newest operation for each product. This prevents repeated
        # edits from purchasing repeated embeddings for obsolete versions.
        product_ids = {job.product_id for job in jobs}
        latest_versions = dict(db.execute(select(
            ProductVectorOutbox.product_id, func.max(ProductVectorOutbox.product_version)
        ).where(
            ProductVectorOutbox.product_id.in_(product_ids),
            ProductVectorOutbox.status == OutboxStatus.pending,
        ).group_by(ProductVectorOutbox.product_id)).all()) if product_ids else {}
        newest: dict[str, ProductVectorOutbox] = {}
        for job in jobs:
            if job.product_version < latest_versions.get(job.product_id, job.product_version):
                job.status = OutboxStatus.complete
                job.completed_at = now
                job.last_error = f"Superseded by product version {latest_versions[job.product_id]}"
                result["superseded"] += 1
                continue
            previous = newest.get(job.product_id)
            if not previous or (job.product_version, job.created_at) > (previous.product_version, previous.created_at):
                if previous:
                    previous.status = OutboxStatus.complete
                    previous.completed_at = now
                    previous.last_error = f"Superseded by product version {job.product_version}"
                    result["superseded"] += 1
                newest[job.product_id] = job
            else:
                job.status = OutboxStatus.complete
                job.completed_at = now
                job.last_error = f"Superseded by product version {previous.product_version}"
                result["superseded"] += 1

        active_jobs = list(newest.values())
        for job in active_jobs:
            job.status = OutboxStatus.processing
        db.commit()

        upsert_jobs: list[tuple[ProductVectorOutbox, Product]] = []
        delete_jobs: list[ProductVectorOutbox] = []
        for job in active_jobs:
            product = db.get(Product, job.product_id)
            if job.operation == "delete" or not product or not product.is_active:
                delete_jobs.append(job)
            else:
                upsert_jobs.append((job, product))

        def succeed(group: list[ProductVectorOutbox]) -> None:
            for job in group:
                job.status = OutboxStatus.complete
                job.completed_at = now
                job.last_error = None
                result["succeeded"] += 1
                SYNC_SUCCESS.labels(status="success", operation=job.operation).inc()

        def fail(group: list[ProductVectorOutbox], exc: Exception) -> None:
            for job in group:
                job.attempts += 1
                job.last_error = str(exc)[:1000]
                job.next_attempt_at = now + timedelta(seconds=min(3600, 2 ** job.attempts * 15))
                if job.attempts >= self.settings.outbox_max_retries:
                    job.status = OutboxStatus.failed
                    db.add(ProductSyncFailure(outbox_id=job.id, product_id=job.product_id,
                                              error=job.last_error, attempts=job.attempts))
                else:
                    job.status = OutboxStatus.pending
                result["failed"] += 1
                SYNC_SUCCESS.labels(status="failure", operation=job.operation).inc()
                logger.warning("vector_sync_failed", product_id=job.product_id, attempts=job.attempts)

        if upsert_jobs:
            try:
                self.vectors.upsert_products([product for _, product in upsert_jobs])
                succeed([job for job, _ in upsert_jobs])
            except Exception as exc:  # noqa: BLE001 - adapter failures enter durable retry state
                fail([job for job, _ in upsert_jobs], exc)
        if delete_jobs:
            try:
                self.vectors.delete_products([job.product_id for job in delete_jobs])
                succeed(delete_jobs)
            except Exception as exc:  # noqa: BLE001 - adapter failures enter durable retry state
                fail(delete_jobs, exc)
        db.commit()
        if result["succeeded"]:
            cache.delete_prefix("retrieval:")
        self.refresh_metrics(db)
        return result

    def retry(self, db: Session, outbox_id: str) -> bool:
        job = db.get(ProductVectorOutbox, outbox_id)
        if not job:
            return False
        job.status = OutboxStatus.pending
        job.attempts = 0
        job.last_error = None
        job.next_attempt_at = datetime.now(timezone.utc)
        db.commit()
        return True

    @staticmethod
    def refresh_metrics(db: Session) -> None:
        pending = db.scalar(select(func.count()).select_from(ProductVectorOutbox).where(
            ProductVectorOutbox.status == OutboxStatus.pending)) or 0
        failed = db.scalar(select(func.count()).select_from(ProductSyncFailure)) or 0
        oldest = db.scalar(select(func.min(ProductVectorOutbox.created_at)).where(
            ProductVectorOutbox.status == OutboxStatus.pending))
        SYNC_PENDING.set(pending)
        SYNC_DEAD_LETTER.set(failed)
        if oldest:
            when = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
            SYNC_LAG.set(max(0, (datetime.now(timezone.utc) - when).total_seconds()))

    def reconcile(self, db: Session) -> dict:
        sql_ids = set(db.scalars(select(Product.id).where(Product.is_active.is_(True))))
        try:
            vector_ids = self.vectors.vector_ids()
        except Exception as exc:  # noqa: BLE001 - reconciliation reports provider unavailability
            return {"status": "unavailable", "error": type(exc).__name__, "sql_count": len(sql_ids), "missing": []}
        missing = sql_ids - vector_ids if vector_ids else sql_ids
        already_queued = set(db.scalars(select(ProductVectorOutbox.product_id).where(
            ProductVectorOutbox.product_id.in_(missing),
            ProductVectorOutbox.status.in_([OutboxStatus.pending, OutboxStatus.processing]),
        ))) if missing else set()
        missing -= already_queued
        for product_id in missing:
            product = db.get(Product, product_id)
            db.add(ProductVectorOutbox(product_id=product_id, operation="upsert", product_version=product.version))
        db.commit()
        return {"status": "queued", "sql_count": len(sql_ids), "missing": sorted(missing)}

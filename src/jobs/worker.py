"""Run SmartReco scheduled work once, outside every web process."""

import signal
from threading import Event
from types import FrameType

from apscheduler.schedulers.background import BackgroundScheduler
from prometheus_client import start_http_server

from src.config import Settings, get_settings
from src.jobs.scheduler import start_scheduler
from src.observability.langsmith import configure_langsmith
from src.observability.logging import configure_logging, logger
from src.services.embedding_service import EmbeddingService
from src.services.mesh_client import MeshClient
from src.services.notification_service import NotificationService
from src.services.outbox_service import OutboxService
from src.services.recommendation_service import RecommendationService
from src.services.retrieval_service import RetrievalService
from src.services.vector_service import VectorService


def build_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    """Build the same grounded services as the API without starting an HTTP server."""
    configure_logging()
    settings = settings or get_settings()
    settings.validate_production()
    if not settings.scheduler_enabled:
        raise RuntimeError("The scheduler worker requires SCHEDULER_ENABLED=true")

    configure_langsmith(settings)

    mesh = MeshClient(settings)
    embeddings = EmbeddingService(mesh)
    vectors = VectorService(settings, embeddings)
    recommendations = RecommendationService(settings, mesh, RetrievalService(vectors))
    scheduler = start_scheduler(
        settings,
        OutboxService(settings, vectors),
        recommendations,
        NotificationService(settings),
    )
    if scheduler is None:
        raise RuntimeError("Scheduler did not start")
    return scheduler


def main() -> None:
    settings = get_settings()
    stop_requested = Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("scheduler_shutdown_requested", signal=signum)
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    scheduler = build_scheduler(settings)
    metrics_server, metrics_thread = start_http_server(settings.scheduler_metrics_port, addr="0.0.0.0")
    logger.info("scheduler_worker_started", metrics_port=settings.scheduler_metrics_port)
    try:
        stop_requested.wait()
    finally:
        scheduler.shutdown(wait=True)
        metrics_server.shutdown()
        metrics_server.server_close()
        metrics_thread.join(timeout=5)
        logger.info("scheduler_worker_stopped")


if __name__ == "__main__":
    main()

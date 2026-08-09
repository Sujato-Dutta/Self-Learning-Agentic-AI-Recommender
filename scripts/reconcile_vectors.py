from src.config import get_settings
from src.database import SessionLocal
from src.services.embedding_service import EmbeddingService
from src.services.mesh_client import MeshClient
from src.services.outbox_service import OutboxService
from src.services.vector_service import VectorService

if __name__ == "__main__":
    settings = get_settings()
    mesh = MeshClient(settings)
    vectors = VectorService(settings, EmbeddingService(mesh))
    service = OutboxService(settings, vectors)
    with SessionLocal() as session:
        print(service.reconcile(session))
        print(service.process_pending(session, limit=100))


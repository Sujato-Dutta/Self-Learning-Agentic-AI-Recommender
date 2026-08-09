from sqlalchemy import select

from src.config import Settings
from src.models import AgentRun, NextBestAction, User
from src.services.embedding_service import EmbeddingService
from src.services.mesh_client import MeshClient
from src.services.recommendation_service import RecommendationService
from src.services.retrieval_service import RetrievalService
from src.services.vector_service import VectorService


def build_service() -> RecommendationService:
    settings = Settings(app_env="test", scheduler_enabled=False)
    mesh = MeshClient(settings)
    vectors = VectorService(settings, EmbeddingService(mesh))
    return RecommendationService(settings, mesh, RetrievalService(vectors))


def test_langgraph_full_run_persists_catalog_grounded_result(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    service = build_service()
    recommendation = service.generate(db, user.id, trigger_type="integration_test", force=True)
    assert recommendation.degraded is True
    assert len(recommendation.items) == 5
    assert all(item.product and item.evidence_event_ids for item in recommendation.items)
    assert recommendation.next_best_action is not None
    assert recommendation.next_best_action.policy_version == "nba-v1"
    assert db.scalar(select(NextBestAction).where(
        NextBestAction.recommendation_id == recommendation.id
    )) is not None
    run = db.scalar(select(AgentRun).where(AgentRun.user_id == user.id).order_by(AgentRun.created_at.desc()))
    assert run.status == "degraded"
    assert run.mesh_called is False
    assert [node["node"] for node in run.node_trace] == [
        "assess_intent", "retrieve", "rerank", "decide_action", "select_persuasion",
        "generate", "validate", "store",
    ]


def test_profile_hash_cache_reuses_stored_recommendation(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    service = build_service()
    first = service.generate(db, user.id, trigger_type="cache_test")
    second = service.generate(db, user.id, trigger_type="cache_test")
    assert second.id == first.id

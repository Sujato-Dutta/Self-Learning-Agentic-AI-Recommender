import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import delete, select

from src.dependencies import CurrentUser, DbSession
from src.models import (
    BehaviorProfile,
    Event,
    NextBestAction,
    NextBestActionReward,
    Product,
    Recommendation,
    RecommendationFeedback,
)
from src.observability.metrics import (
    EVENT_BATCHES,
    EVENT_LATENCY,
    EVENTS_RECEIVED,
    RECO_ENGAGEMENT,
)
from src.repositories.events import delete_user_events, ingest_events
from src.schemas.events import EventBatch, FeedbackIn
from src.services.behavior_service import aggregate_profile
from src.services.cache_service import cache
from src.services.reward_service import (
    attribute_event_rewards,
    attribute_feedback_reward,
)

router = APIRouter(prefix="/api", tags=["events"])


@router.post("/events/batch")
def event_batch(payload: EventBatch, user: CurrentUser, db: DbSession, request: Request):
    started = time.perf_counter()
    maximum = request.app.state.settings.event_batch_max_size
    if len(payload.events) > maximum:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=f"Maximum batch size is {maximum}")
    inserted, duplicates = ingest_events(db, user.id, payload.events)
    EVENTS_RECEIVED.inc(inserted)
    EVENT_BATCHES.inc()
    EVENT_LATENCY.observe(time.perf_counter() - started)
    if inserted:
        cache.delete_prefix(f"profile:{user.id}")
    profile_events = {
        "search", "category_filter", "difficulty_filter", "price_filter", "product_view",
        "search_click", "wishlist_add", "wishlist_remove",
        "cta_click", "enrollment", "recommendation_click", "recommendation_dismiss",
        "not_interested", "time_spent", "profile_correction",
    }
    profile = aggregate_profile(db, user.id) if inserted and any(
        event.event_type in profile_events for event in payload.events
    ) else None
    rewards_attributed = attribute_event_rewards(db, user.id, [event.event_id for event in payload.events])
    return {"accepted": inserted, "duplicates": duplicates,
            "profile_version": profile.get("profile_version") if profile else None,
            "rewards_attributed": rewards_attributed}


@router.post("/recommendations/feedback")
def recommendation_feedback(payload: FeedbackIn, user: CurrentUser, db: DbSession):
    product = db.get(Product, payload.product_id)
    if not product or not product.is_active:
        raise HTTPException(404, "Course not found")
    if payload.recommendation_id and not db.scalar(select(Recommendation.id).where(
        Recommendation.id == payload.recommendation_id, Recommendation.user_id == user.id,
    )):
        raise HTTPException(404, "Recommendation not found")
    feedback_event_id = f"feedback-{uuid.uuid4()}"
    db.add(RecommendationFeedback(user_id=user.id, recommendation_id=payload.recommendation_id,
                                  product_id=payload.product_id, feedback_type=payload.feedback_type))
    db.add(Event(
        event_id=feedback_event_id,
        user_id=user.id,
        session_id="direct-recommendation-feedback",
        event_type={
            "dismiss": "recommendation_dismiss",
            "not_interested": "not_interested",
            "helpful": "recommendation_click",
            "clicked": "recommendation_click",
        }[payload.feedback_type],
        product_id=payload.product_id,
        event_metadata={"recommendation_id": payload.recommendation_id, "source": "recommendation_feedback"},
        occurred_at=datetime.now(timezone.utc),
    ))
    db.commit()
    attribute_feedback_reward(
        db, user.id, payload.recommendation_id, payload.product_id,
        payload.feedback_type, feedback_event_id,
    )
    RECO_ENGAGEMENT.labels(action=payload.feedback_type).inc()
    cache.delete_prefix(f"profile:{user.id}")
    cache.delete_prefix(f"recommendation:{user.id}")
    return {"status": "recorded"}


@router.delete("/behavior")
def erase_behavior(user: CurrentUser, db: DbSession):
    deleted = delete_user_events(db, user.id)
    db.execute(delete(BehaviorProfile).where(BehaviorProfile.user_id == user.id))
    db.execute(delete(NextBestActionReward).where(NextBestActionReward.user_id == user.id))
    db.execute(delete(NextBestAction).where(NextBestAction.user_id == user.id))
    db.execute(delete(Recommendation).where(Recommendation.user_id == user.id))
    db.commit()
    cache.delete_prefix(f"profile:{user.id}")
    cache.delete_prefix(f"recommendation:{user.id}")
    return {"deleted_events": deleted}

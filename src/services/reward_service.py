from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.models import Event, NextBestAction, NextBestActionReward
from src.observability.metrics import NBA_REWARDS

EVENT_REWARDS = {
    "recommendation_impression": 0.0,
    "recommendation_click": 0.15,
    "wishlist_add": 0.30,
    "wishlist_remove": -0.15,
    "enrollment": 1.0,
    "recommendation_dismiss": -0.40,
    "not_interested": -0.65,
}

FEEDBACK_REWARDS = {
    "helpful": 0.35,
    "clicked": 0.15,
    "dismiss": -0.40,
    "not_interested": -0.65,
}

DIRECT_ACTION_REWARDS = {
    "impression": 0.0,
    "clicked": 0.15,
    "saved": 0.30,
    "added_to_cart": 0.55,
    "purchased": 1.0,
    "dismissed": -0.40,
    "not_interested": -0.65,
}


def _decision_for_event(
    db: Session,
    user_id: str,
    product_id: str | None,
    metadata: dict[str, Any],
    occurred_at: datetime,
) -> NextBestAction | None:
    decision_id = metadata.get("decision_id")
    if decision_id:
        decision = db.get(NextBestAction, str(decision_id))
        if decision and decision.user_id == user_id:
            return decision

    query = select(NextBestAction).where(
        NextBestAction.user_id == user_id,
        NextBestAction.created_at >= occurred_at - timedelta(days=7),
        NextBestAction.created_at <= occurred_at + timedelta(minutes=5),
        NextBestAction.expires_at >= occurred_at - timedelta(minutes=5),
    )
    recommendation_id = metadata.get("recommendation_id")
    if recommendation_id:
        query = query.where(NextBestAction.recommendation_id == str(recommendation_id))
    elif product_id:
        query = query.where(NextBestAction.product_id == product_id)
    else:
        return None
    return db.scalar(query.order_by(NextBestAction.created_at.desc()))


def _reward_value(event_type: str, metadata: dict[str, Any]) -> float | None:
    if event_type == "cta_click":
        source = str(metadata.get("source", ""))
        return {
            "add_to_cart": 0.55,
            "remove_from_cart": -0.25,
            "buy_now": 0.35,
            "journey_twin_add_to_cart": 0.55,
            "journey_twin_buy_now": 0.35,
        }.get(source, 0.10)
    return EVENT_REWARDS.get(event_type)


def attribute_event_rewards(db: Session, user_id: str, event_ids: list[str]) -> int:
    """Attribute only persisted, user-owned events; event IDs make retries safe."""
    if not event_ids:
        return 0
    events = list(db.scalars(select(Event).where(Event.user_id == user_id, Event.event_id.in_(event_ids))))
    existing = set(db.scalars(select(NextBestActionReward.event_id).where(
        NextBestActionReward.event_id.in_([event.event_id for event in events])
    )))
    attributed = 0
    for event in events:
        if event.event_id in existing:
            continue
        reward = _reward_value(event.event_type, event.event_metadata or {})
        if reward is None:
            continue
        occurred_at = event.occurred_at if event.occurred_at.tzinfo else event.occurred_at.replace(tzinfo=timezone.utc)
        decision = _decision_for_event(
            db, user_id, event.product_id, event.event_metadata or {}, occurred_at
        )
        if not decision:
            continue
        db.add(NextBestActionReward(
            decision_id=decision.id,
            user_id=user_id,
            product_id=event.product_id,
            event_id=event.event_id,
            event_type=event.event_type,
            reward=reward,
            event_metadata=event.event_metadata or {},
        ))
        decision.cumulative_reward = round(float(decision.cumulative_reward or 0) + reward, 4)
        if event.event_type == "enrollment":
            decision.status = "converted"
            decision.outcome_type = "purchase"
            decision.outcome_at = occurred_at
        elif event.event_type in {"recommendation_dismiss", "not_interested"}:
            decision.status = "dismissed"
            decision.outcome_type = event.event_type
            decision.outcome_at = occurred_at
        NBA_REWARDS.labels(event_type=event.event_type, sign="positive" if reward > 0 else "negative" if reward < 0 else "neutral").inc()
        attributed += 1
    if attributed:
        db.commit()
    return attributed


def attribute_feedback_reward(
    db: Session,
    user_id: str,
    recommendation_id: str | None,
    product_id: str,
    feedback_type: str,
    event_id: str,
) -> bool:
    if db.scalar(select(NextBestActionReward.id).where(NextBestActionReward.event_id == event_id)):
        return False
    decision = db.scalar(select(NextBestAction).where(
        NextBestAction.user_id == user_id,
        or_(
            NextBestAction.recommendation_id == recommendation_id if recommendation_id else False,
            NextBestAction.product_id == product_id,
        ),
    ).order_by(NextBestAction.created_at.desc()))
    reward = FEEDBACK_REWARDS.get(feedback_type)
    if not decision or reward is None:
        return False
    now = datetime.now(timezone.utc)
    db.add(NextBestActionReward(
        decision_id=decision.id,
        user_id=user_id,
        product_id=product_id,
        event_id=event_id,
        event_type=f"feedback:{feedback_type}",
        reward=reward,
        event_metadata={"recommendation_id": recommendation_id},
    ))
    decision.cumulative_reward = round(float(decision.cumulative_reward or 0) + reward, 4)
    if feedback_type in {"dismiss", "not_interested"}:
        decision.status = "dismissed"
        decision.outcome_type = feedback_type
        decision.outcome_at = now
    NBA_REWARDS.labels(
        event_type=f"feedback:{feedback_type}",
        sign="positive" if reward > 0 else "negative" if reward < 0 else "neutral",
    ).inc()
    db.commit()
    return True


def record_direct_action_feedback(
    db: Session,
    user_id: str,
    decision_id: str,
    outcome: str,
    event_id: str,
) -> NextBestActionReward | None:
    decision = db.get(NextBestAction, decision_id)
    if not decision or decision.user_id != user_id or outcome not in DIRECT_ACTION_REWARDS:
        return None
    existing = db.scalar(select(NextBestActionReward).where(NextBestActionReward.event_id == event_id))
    if existing:
        return existing if existing.user_id == user_id else None
    reward_value = DIRECT_ACTION_REWARDS[outcome]
    now = datetime.now(timezone.utc)
    event_type, source = {
        "impression": ("recommendation_impression", "journey_twin"),
        "clicked": ("recommendation_click", "journey_twin"),
        "saved": ("wishlist_add", "journey_twin"),
        "added_to_cart": ("cta_click", "journey_twin_add_to_cart"),
        "purchased": ("enrollment", "journey_twin"),
        "dismissed": ("recommendation_dismiss", "journey_twin"),
        "not_interested": ("not_interested", "journey_twin"),
    }[outcome]
    stored_event = db.scalar(select(Event).where(Event.event_id == event_id))
    if stored_event and stored_event.user_id != user_id:
        return None
    if not stored_event:
        db.add(Event(
            event_id=event_id,
            user_id=user_id,
            session_id="direct-action-feedback",
            event_type=event_type,
            product_id=decision.product_id,
            event_metadata={
                "decision_id": decision.id,
                "recommendation_id": decision.recommendation_id,
                "action_type": decision.action_type,
                "persuasion_strategy": decision.persuasion_strategy,
                "source": source,
            },
            occurred_at=now,
        ))
    reward = NextBestActionReward(
        decision_id=decision.id,
        user_id=user_id,
        product_id=decision.product_id,
        event_id=event_id,
        event_type=f"direct:{outcome}",
        reward=reward_value,
        event_metadata={"decision_id": decision.id},
    )
    db.add(reward)
    decision.cumulative_reward = round(float(decision.cumulative_reward or 0) + reward_value, 4)
    if outcome == "purchased":
        decision.status = "converted"
        decision.outcome_type = "purchase"
        decision.outcome_at = now
    elif outcome in {"dismissed", "not_interested"}:
        decision.status = "dismissed"
        decision.outcome_type = outcome
        decision.outcome_at = now
    NBA_REWARDS.labels(
        event_type=f"direct:{outcome}",
        sign="positive" if reward_value > 0 else "negative" if reward_value < 0 else "neutral",
    ).inc()
    db.commit()
    return reward

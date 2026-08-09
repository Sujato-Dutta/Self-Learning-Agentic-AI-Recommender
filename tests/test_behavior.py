from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.models import Event, Product, User
from src.services.behavior_service import (
    aggregate_profile,
    recency_decay,
    should_trigger,
)
from src.services.skill_taxonomy import SKILL_BUNDLES, infer_skill_bundles


def test_recency_decay_halves_after_half_life():
    now = datetime.now(timezone.utc)
    assert recency_decay(now, now) == 1
    assert round(recency_decay(now - timedelta(days=14), now), 2) == .5


def test_behavior_aggregation_uses_frequency_dwell_and_evidence(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    product = db.scalar(select(Product).where(Product.title == "Advanced Agentic AI"))
    db.add(Event(event_id="behavior-test-extra", user_id=user.id, session_id="test-session",
                 event_type="time_spent", product_id=product.id, event_metadata={"dwell_seconds": 300},
                 occurred_at=datetime.now(timezone.utc)))
    db.commit()
    profile = aggregate_profile(db, user.id)
    assert "Agentic AI" in profile["emerging_interests"]
    assert profile["confidence"] > .5
    assert "behavior-test-extra" in profile["evidence_event_ids"]
    assert profile["profile_hash"]
    assert set(profile["emerging_interests"]).issubset(SKILL_BUNDLES)


def test_search_terms_map_to_compact_managed_skill_bundles():
    assert infer_skill_bundles("agentic ai and production rag")[:2] == ["Agentic AI", "Generative AI"]
    assert infer_skill_bundles("power bi dashboard") == ["Data Analytics"]


def test_trigger_rules_prioritize_high_intent():
    assert should_trigger({"evidence_event_ids": []}, ["wishlist_add"], True) == (True, "high_intent")
    assert should_trigger({"evidence_event_ids": ["1", "2", "3"]}, [], False)[0]
    assert not should_trigger({"evidence_event_ids": []}, [], False)[0]

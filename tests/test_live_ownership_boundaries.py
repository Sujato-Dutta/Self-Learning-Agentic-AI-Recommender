from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api.recommendations import serialize_recommendation
from src.main import app
from src.models import (
    Event,
    NextBestAction,
    Product,
    Recommendation,
    RecommendationItem,
    User,
    UserEnrollment,
)
from src.services.behavior_service import aggregate_profile
from src.services.cache_service import cache


def _learner(db) -> User:
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    assert user is not None
    return user


def _product(db, slug: str) -> Product:
    product = db.scalar(select(Product).where(Product.slug == slug))
    assert product is not None
    return product


def _login(client: TestClient) -> None:
    client.get("/login")
    response = client.post(
        "/auth/login",
        data={
            "email": "learner@smartreco.dev",
            "password": "LearnerDemo123!",
            "csrf_token": client.cookies.get("csrf_token"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _stored_recommendation(
    db,
    user: User,
    action_product: Product,
    related_product: Product,
    *,
    action_expires_at: datetime,
    copy_title: str | None = None,
) -> Recommendation:
    recommendation = Recommendation(
        user_id=user.id,
        behavior_profile_version=1,
        profile_hash="live-ownership-boundary".ljust(64, "0"),
        headline=f"Take {copy_title or action_product.title} next",
        narrative=f"The best next step is {copy_title or action_product.title}.",
        reason_summary="Boundary regression fixture",
        confidence=0.8,
        trigger_type="test",
        model_name="test",
        prompt_version="test",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    db.add(recommendation)
    db.flush()
    for rank, product in enumerate((action_product, related_product), 1):
        db.add(RecommendationItem(
            recommendation_id=recommendation.id,
            product_id=product.id,
            rank=rank,
            retrieval_score=0.9 - rank / 100,
            rerank_score=0.9 - rank / 100,
            final_score=0.9 - rank / 100,
            reason=f"Build {product.skill_bundle} with {product.title}.",
            evidence_event_ids=[],
            score_breakdown={"interest": 0.9},
        ))
    db.add(NextBestAction(
        user_id=user.id,
        recommendation_id=recommendation.id,
        product_id=action_product.id,
        action_type="recommend_course",
        persuasion_strategy="skill_gap",
        headline=f"Choose {copy_title or action_product.title}",
        message=f"Continue with {copy_title or action_product.title}.",
        rationale="Stored policy result",
        intent_stage="high_intent",
        purchase_propensity=0.7,
        expected_conversion_probability=0.5,
        expected_revenue=Decimal(1000),
        incremental_expected_revenue=Decimal(100),
        evidence_event_ids=[],
        market_signal_ids=[],
        policy_context={},
        status="active",
        expires_at=action_expires_at,
    ))
    db.commit()
    db.refresh(recommendation)
    return recommendation


def test_cached_profile_rechecks_durable_entitlements_and_keeps_legacy_enrollment_events(db):
    user = _learner(db)
    legacy_product = _product(db, "python-foundations")
    newly_owned = _product(db, "agentic-ai-foundations")
    cache.delete_prefix(f"profile:{user.id}")
    db.add(Event(
        event_id="legacy-enrollment-cache-boundary",
        user_id=user.id,
        session_id="legacy-import",
        event_type="enrollment",
        product_id=legacy_product.id,
        event_metadata={"source": "legacy_import"},
        occurred_at=datetime.now(timezone.utc),
    ))
    db.commit()

    warm = aggregate_profile(db, user.id)
    assert legacy_product.id in warm["purchased_product_ids"]
    assert newly_owned.id not in warm["purchased_product_ids"]

    # Simulates a purchase committed by another cloud worker, which cannot
    # invalidate this process's in-memory profile cache.
    db.add(UserEnrollment(
        user_id=user.id,
        product_id=newly_owned.id,
        source_product_id=newly_owned.id,
    ))
    db.commit()

    refreshed = aggregate_profile(db, user.id)
    assert newly_owned.id in refreshed["purchased_product_ids"]
    assert newly_owned.id in refreshed["excluded_product_ids"]
    assert legacy_product.id in refreshed["purchased_product_ids"]


def test_serializer_replaces_copy_when_owned_items_and_action_are_removed(db):
    user = _learner(db)
    owned = _product(db, "agentic-ai-foundations")
    related = _product(db, "advanced-agentic-ai")
    recommendation = _stored_recommendation(
        db,
        user,
        owned,
        related,
        action_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        copy_title=owned.title,
    )

    payload = serialize_recommendation(
        recommendation,
        db,
        excluded_product_ids={owned.id},
        profile={"emerging_interests": ["Agentic AI"]},
    )

    assert payload["next_best_action"] is None
    assert [item["product_id"] for item in payload["items"]] == [related.id]
    assert owned.title.casefold() not in payload["headline"].casefold()
    assert owned.title.casefold() not in payload["narrative"].casefold()
    assert related.title in payload["headline"]


def test_current_action_replaces_an_expired_decision_with_a_related_unowned_course(db):
    user = _learner(db)
    expired_target = _product(db, "agentic-ai-foundations")
    related = _product(db, "advanced-agentic-ai")
    recommendation = _stored_recommendation(
        db,
        user,
        expired_target,
        related,
        action_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    decision_id = recommendation.next_best_action.id
    cache.delete_prefix(f"profile:{user.id}")

    with TestClient(app) as client:
        _login(client)
        response = client.get("/api/next-best-action")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] is None
    assert payload["source_decision_id"] == decision_id
    assert payload["is_fallback"] is True
    assert payload["feedback_enabled"] is False
    assert payload["product"]["id"] == related.id
    assert payload["product"]["id"] != expired_target.id


def test_current_action_uses_stored_related_item_when_target_was_just_purchased(db, monkeypatch):
    user = _learner(db)
    owned_target = _product(db, "agentic-ai-foundations")
    related = _product(db, "advanced-agentic-ai")
    recommendation = _stored_recommendation(
        db,
        user,
        owned_target,
        related,
        action_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(UserEnrollment(
        user_id=user.id,
        product_id=owned_target.id,
        source_product_id=owned_target.id,
    ))
    recommendation.next_best_action.status = "converted"
    db.commit()
    cache.delete_prefix(f"profile:{user.id}")

    with TestClient(app) as client:
        _login(client)
        monkeypatch.setattr(
            app.state.recommendations,
            "generate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("read fallback must not generate or call Mesh")
            ),
        )
        response = client.get("/api/next-best-action")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_decision_id"] == recommendation.next_best_action.id
    assert payload["status"] == "fallback"
    assert payload["product"]["id"] == related.id
    assert owned_target.title not in payload["headline"]
    assert owned_target.title not in payload["message"]

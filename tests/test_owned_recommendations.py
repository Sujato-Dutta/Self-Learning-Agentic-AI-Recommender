import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.api.recommendations import serialize_next_best_action, serialize_recommendation
from src.main import app
from src.models import (
    NextBestAction,
    Product,
    Recommendation,
    RecommendationItem,
    User,
    UserEnrollment,
)
from src.repositories.enrollments import complete_demo_purchase, purchased_product_ids
from src.services.bundle_pricing_service import calculate_personalized_offer
from src.services.ownership_service import is_product_recommendable
from src.services.reranking_service import rerank
from src.services.retrieval_service import Candidate, RetrievalService

LEARNER_EMAIL = "learner@smartreco.dev"
LEARNER_PASSWORD = "LearnerDemo123!"


def _product(db, slug: str) -> Product:
    product = db.scalar(select(Product).where(Product.slug == slug))
    assert product is not None
    return product


def _learner(db) -> User:
    user = db.scalar(select(User).where(User.email == LEARNER_EMAIL))
    assert user is not None
    return user


def _login(client: TestClient) -> None:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    assert csrf
    response = client.post(
        "/auth/login",
        data={
            "email": LEARNER_EMAIL,
            "password": LEARNER_PASSWORD,
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def _enroll(db, user: User, product: Product) -> None:
    db.add(UserEnrollment(
        user_id=user.id,
        product_id=product.id,
        source_product_id=product.id,
    ))
    db.commit()


def _recommendation(
    db,
    user: User,
    products: list[Product],
    *,
    action_product: Product | None = None,
) -> Recommendation:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
    recommendation = Recommendation(
        user_id=user.id,
        behavior_profile_version=1,
        profile_hash="stale-owned-recommendation".ljust(64, "0"),
        headline="A stale recommendation",
        narrative="This stored result deliberately predates the ownership guard.",
        reason_summary="Regression fixture",
        confidence=.88,
        trigger_type="test_fixture",
        model_name="test-policy",
        prompt_version="test-v1",
        expires_at=expires_at,
    )
    db.add(recommendation)
    db.flush()
    for rank, product in enumerate(products, 1):
        db.add(RecommendationItem(
            recommendation_id=recommendation.id,
            product_id=product.id,
            rank=rank,
            retrieval_score=.9 - rank / 100,
            rerank_score=.9 - rank / 100,
            final_score=.9 - rank / 100,
            reason=f"Stored reason for {product.title}",
            evidence_event_ids=[],
            score_breakdown={"interest": .9},
        ))
    if action_product:
        db.add(NextBestAction(
            user_id=user.id,
            recommendation_id=recommendation.id,
            product_id=action_product.id,
            action_type="recommend_course",
            persuasion_strategy="skill_gap",
            headline=f"Take {action_product.title} next",
            message=f"This stale action still points to {action_product.title}.",
            rationale="Regression fixture",
            intent_stage="high_intent",
            purchase_propensity=.7,
            expected_conversion_probability=.6,
            expected_revenue=Decimal("1000.00"),
            incremental_expected_revenue=Decimal("100.00"),
            evidence_event_ids=[],
            market_signal_ids=[],
            policy_context={},
            status="active",
            expires_at=expires_at,
        ))
    db.commit()
    db.refresh(recommendation)
    return recommendation


def test_shared_serializer_keeps_partial_bundle_with_live_credit_and_backfills_three(db):
    user = _learner(db)
    owned_course = _product(db, "agentic-ai-foundations")
    owned_bundle = _product(db, "data-science-professional")
    overlapping_bundle = _product(db, "agentic-ai-professional")
    assert owned_course.id in overlapping_bundle.bundled_product_ids

    stale = _recommendation(
        db,
        user,
        [owned_course, owned_bundle, overlapping_bundle],
    )
    owned_ids = {owned_course.id, owned_bundle.id}
    profile = {
        "emerging_interests": ["Agentic AI"],
        "skill_gaps": ["AI Agents", "LLMs", "RAG"],
        "difficulty_preference": "advanced",
        "budget_max": 20_000,
    }

    payload = serialize_recommendation(
        stale,
        db,
        excluded_product_ids=owned_ids,
        profile=profile,
        minimum_standalone=3,
    )

    item_ids = [item["product_id"] for item in payload["items"]]
    assert len(item_ids) == 4
    assert owned_course.id not in item_ids
    assert owned_bundle.id not in item_ids
    assert overlapping_bundle.id in item_ids

    partial_item = next(
        item for item in payload["items"]
        if item["product_id"] == overlapping_bundle.id
    )
    components = list(db.scalars(select(Product).where(
        Product.id.in_(overlapping_bundle.bundled_product_ids)
    )))
    offer = calculate_personalized_offer(overlapping_bundle, components, owned_ids)
    assert offer.eligible is True
    assert partial_item["price"] == float(offer.personalized_price)
    assert partial_item["original_price"] == float(offer.remaining_standalone_subtotal)
    assert partial_item["catalog_price"] == float(offer.catalog_price)
    assert partial_item["ownership_credit"] == float(offer.ownership_credit)
    assert set(partial_item["owned_component_ids"]) == {owned_course.id}
    assert set(partial_item["remaining_component_ids"]) == {
        product_id for product_id in overlapping_bundle.bundled_product_ids
        if product_id != owned_course.id
    }
    assert partial_item["owned_component_count"] == 1
    assert partial_item["remaining_component_count"] == len(
        overlapping_bundle.bundled_product_ids
    ) - 1

    replacement_ids = set(item_ids) - {overlapping_bundle.id}
    replacements = list(db.scalars(select(Product).where(Product.id.in_(replacement_ids))))
    assert len(replacements) == 3
    assert all(not product.is_bundle for product in replacements)
    assert all(is_product_recommendable(product, owned_ids) for product in replacements)
    assert any(product.skill_bundle == "Agentic AI" for product in replacements)


def test_shared_serializer_excludes_a_bundle_when_all_components_are_owned(db):
    user = _learner(db)
    bundle = _product(db, "agentic-ai-professional")
    owned_ids = set(bundle.bundled_product_ids)
    stale = _recommendation(db, user, [bundle], action_product=bundle)

    payload = serialize_recommendation(
        stale,
        db,
        excluded_product_ids=owned_ids,
        profile={"purchased_product_ids": sorted(owned_ids)},
    )

    assert payload["items"] == []
    assert payload["next_best_action"] is None


def test_next_best_action_endpoint_replaces_owned_stale_action_without_generation(db, monkeypatch):
    user = _learner(db)
    owned = _product(db, "agentic-ai-foundations")
    _enroll(db, user, owned)
    _recommendation(db, user, [], action_product=owned)

    with TestClient(app) as client:
        _login(client)
        monkeypatch.setattr(
            app.state.recommendations,
            "generate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("read endpoint must not generate a replacement")
            ),
        )
        response = client.get("/api/next-best-action")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_fallback"] is True
    assert payload["feedback_enabled"] is False
    assert payload["product"]["id"] != owned.id
    replacement = db.get(Product, payload["product"]["id"])
    assert is_product_recommendable(replacement, {owned.id})


def test_partial_bundle_action_serializes_live_completion_price_and_credit(db):
    user = _learner(db)
    owned = _product(db, "agentic-ai-foundations")
    bundle = _product(db, "agentic-ai-professional")
    assert owned.id in bundle.bundled_product_ids
    _enroll(db, user, owned)
    recommendation = _recommendation(db, user, [], action_product=bundle)
    recommendation.next_best_action.action_type = "offer_bundle"
    db.commit()

    components = list(db.scalars(select(Product).where(
        Product.id.in_(bundle.bundled_product_ids)
    )))
    offer = calculate_personalized_offer(bundle, components, {owned.id})
    payload = serialize_next_best_action(
        recommendation.next_best_action,
        db,
        excluded_product_ids={owned.id},
    )

    assert payload is not None
    product = payload["product"]
    assert product["id"] == bundle.id
    assert product["price"] == float(offer.personalized_price)
    assert product["original_price"] == float(offer.remaining_standalone_subtotal)
    assert product["catalog_price"] == float(offer.catalog_price)
    assert product["ownership_credit"] == float(offer.ownership_credit)
    assert set(product["owned_component_ids"]) == {owned.id}
    assert product["remaining_component_count"] == len(bundle.bundled_product_ids) - 1
    message = payload["message"].lower()
    assert "already own" in message
    assert "credited" in message
    assert "remaining track" in message


def test_course_detail_related_section_excludes_owned_and_backfills_three(db):
    user = _learner(db)
    current = _product(db, "agentic-ai-foundations")
    owned_related = _product(db, "advanced-agentic-ai")
    expected_related = _product(db, "self-improving-ai-systems")
    _enroll(db, user, owned_related)

    with TestClient(app) as client:
        _login(client)
        response = client.get(f"/courses/{current.slug}")

    assert response.status_code == 200
    match = re.search(
        r'<section class="related-courses">(.*?)</section>',
        response.text,
        re.DOTALL,
    )
    assert match is not None
    related_ids = re.findall(r'data-product-id="([^"]+)"', match.group(1))
    assert len(related_ids) == 3
    assert owned_related.id not in related_ids
    assert current.id not in related_ids
    assert expected_related.id in related_ids
    related_products = list(db.scalars(select(Product).where(Product.id.in_(related_ids))))
    assert all(is_product_recommendable(product, {owned_related.id}) for product in related_products)


def test_landing_substitutes_an_eligible_track_when_featured_track_is_owned(db):
    user = _learner(db)
    owned_track = _product(db, "agentic-ai-professional")
    complete_demo_purchase(db, user.id, [owned_track.id], "course_detail_demo_purchase")
    owned_ids = purchased_product_ids(db, user.id)

    with TestClient(app) as client:
        _login(client)
        response = client.get("/")

    assert response.status_code == 200
    opening_tag = re.search(r'<div class="landing-twin-demo"[^>]*>', response.text)
    assert opening_tag is not None
    featured_id_match = re.search(r'data-product-id="([^"]+)"', opening_tag.group(0))
    assert featured_id_match is not None
    substitute_id = featured_id_match.group(1)
    substitute = db.get(Product, substitute_id)
    assert substitute is not None
    assert substitute_id != owned_track.id
    assert is_product_recommendable(substitute, owned_ids)
    assert "data-nba-card" in opening_tag.group(0)

    demo_start = opening_tag.start()
    demo_end = response.text.find('<section class="production-section"', demo_start)
    demo = response.text[demo_start:demo_end]
    assert "Already purchased" not in demo
    item_kind = "track" if substitute.is_bundle else "course"
    assert f'data-cart-label="Add {item_kind} to cart"' in demo
    assert f'href="/courses/{substitute.slug}"' in demo


def test_retrieval_keeps_bundles_that_partially_overlap_an_owned_component(db):
    owned_component = _product(db, "agentic-ai-foundations")
    direct_bundle = _product(db, "agentic-ai-professional")
    cross_bundle = _product(db, "generative-ai-engineer-professional")
    eligible_bundle = _product(db, "machine-learning-professional")
    related_course = _product(db, "advanced-agentic-ai")
    assert owned_component.id in direct_bundle.bundled_product_ids
    assert owned_component.id in cross_bundle.bundled_product_ids

    matches = [
        SimpleNamespace(product_id=product.id, score=score)
        for product, score in (
            (direct_bundle, .99),
            (cross_bundle, .98),
            (owned_component, .97),
            (related_course, .96),
            (eligible_bundle, .95),
        )
    ]

    class VectorMatches:
        def query(self, *_args, **_kwargs):
            return matches

    profile = {
        "primary_goal": "Build production AI agents",
        "emerging_interests": ["Agentic AI"],
        "skill_gaps": ["AI Agents", "RAG"],
        "difficulty_preference": "advanced",
        "budget_max": 50_000,
        "excluded_product_ids": [owned_component.id],
        "purchased_product_ids": [owned_component.id],
    }
    retrieval = RetrievalService(VectorMatches())

    _, candidates, degraded = retrieval.retrieve(db, profile)
    candidate_ids = {candidate.product.id for candidate in candidates}
    _, preview_candidates = retrieval.preview(db, profile)
    preview_ids = {candidate.product.id for candidate in preview_candidates}

    assert degraded is False
    assert related_course.id in candidate_ids
    assert eligible_bundle.id in candidate_ids
    for ids in (candidate_ids, preview_ids):
        assert owned_component.id not in ids
        assert direct_bundle.id in ids
        assert cross_bundle.id in ids


def test_partial_bundle_receives_a_completion_rank_boost_and_live_budget_price(db):
    owned_component = _product(db, "agentic-ai-foundations")
    bundle = _product(db, "agentic-ai-professional")
    candidate = Candidate(bundle, .82, "test")
    base_profile = {
        "emerging_interests": ["Agentic AI"],
        "skill_gaps": ["AI Agents", "RAG"],
        "difficulty_preference": "advanced",
        "budget_max": 10_000,
        "purchased_product_ids": [],
        "excluded_product_ids": [],
    }

    unowned = rerank([candidate], base_profile, limit=1, db=db)[0]
    partial = rerank(
        [candidate],
        {**base_profile, "purchased_product_ids": [owned_component.id]},
        limit=1,
        db=db,
    )[0]

    assert unowned.breakdown["bundle_completion"] == 0
    assert partial.breakdown["bundle_completion"] == 1 / len(bundle.bundled_product_ids)
    assert partial.breakdown["budget"] > unowned.breakdown["budget"]
    assert partial.final_score > unowned.final_score
    assert rerank(
        [candidate],
        {**base_profile, "purchased_product_ids": bundle.bundled_product_ids},
        limit=1,
        db=db,
    ) == []

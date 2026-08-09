import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from src.main import app, format_inr
from src.models import (
    Event,
    NextBestAction,
    Product,
    Recommendation,
    RecommendationItem,
    User,
    UserEnrollment,
)
from src.services.bundle_pricing_service import calculate_personalized_offer
from src.services.ownership_service import is_product_recommendable

LEARNER_EMAIL = "learner@smartreco.dev"
LEARNER_PASSWORD = "LearnerDemo123!"


def _learner(db) -> User:
    user = db.scalar(select(User).where(User.email == LEARNER_EMAIL))
    assert user is not None
    return user


def _product(db, slug: str) -> Product:
    product = db.scalar(select(Product).where(Product.slug == slug))
    assert product is not None
    return product


def _own(db, user: User, product: Product) -> None:
    db.add(UserEnrollment(
        user_id=user.id,
        product_id=product.id,
        source_product_id=product.id,
    ))
    db.commit()


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


def _journey_demo(html: str) -> tuple[str, str]:
    start = html.index('<div class="landing-twin-demo')
    end = html.index('<section class="production-section"', start)
    demo = html[start:end]
    return demo.split(">", 1)[0] + ">", demo


def _stale_owned_recommendation(db, user: User, product: Product) -> Recommendation:
    expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    recommendation = Recommendation(
        user_id=user.id,
        behavior_profile_version=1,
        profile_hash="discover-owned-stale".ljust(64, "0"),
        headline="Stored recommendation",
        narrative="This stored result predates the learner's purchase.",
        reason_summary="Ownership regression fixture",
        confidence=.84,
        trigger_type="test_fixture",
        model_name="test-policy",
        prompt_version="test-v1",
        expires_at=expiry,
    )
    db.add(recommendation)
    db.flush()
    db.add(RecommendationItem(
        recommendation_id=recommendation.id,
        product_id=product.id,
        rank=1,
        retrieval_score=.92,
        rerank_score=.91,
        final_score=.90,
        reason=f"A stale reason for {product.title}",
        evidence_event_ids=[],
        score_breakdown={"interest": .9},
    ))
    db.add(NextBestAction(
        user_id=user.id,
        recommendation_id=recommendation.id,
        product_id=product.id,
        action_type="recommend_course",
        persuasion_strategy="skill_gap",
        headline=f"Take {product.title} next",
        message=f"A stale action for {product.title}.",
        rationale="Ownership regression fixture",
        intent_stage="high_intent",
        purchase_propensity=.7,
        expected_conversion_probability=.6,
        expected_revenue=Decimal("1000.00"),
        incremental_expected_revenue=Decimal("100.00"),
        evidence_event_ids=[],
        market_signal_ids=[],
        policy_context={},
        status="active",
        expires_at=expiry,
    ))
    db.commit()
    db.refresh(recommendation)
    return recommendation


def test_component_owned_landing_keeps_track_with_completion_price_and_copy(db):
    user = _learner(db)
    owned_component = _product(db, "agentic-ai-foundations")
    featured_bundle = _product(db, "agentic-ai-professional")
    components = list(db.scalars(select(Product).where(
        Product.id.in_(featured_bundle.bundled_product_ids)
    )))
    offer = calculate_personalized_offer(
        featured_bundle,
        components,
        {owned_component.id},
    )
    _own(db, user, owned_component)

    with TestClient(app) as client:
        _login(client)
        response = client.get("/")

    assert response.status_code == 200
    opening, demo = _journey_demo(response.text)
    product_id = re.search(r'data-product-id="([^"]+)"', opening)
    assert product_id is not None
    assert product_id.group(1) == featured_bundle.id
    assert is_product_recommendable(featured_bundle, {owned_component.id})

    assert 'data-action-type="offer_bundle"' in opening
    assert f'data-price="{offer.personalized_price}"' in opening
    assert f'data-catalog-price="{offer.catalog_price}"' in opening
    assert f'data-ownership-credit="{offer.ownership_credit}"' in opening
    assert owned_component.id in opening
    assert f'alt="{featured_bundle.title} bundle cover"' in demo
    assert featured_bundle.title in demo
    assert all(skill in demo for skill in featured_bundle.skills[:3])
    assert "COMPLETE YOUR BUNDLE" in demo
    assert "without paying twice" in demo
    assert "ownership credit applied" in demo
    assert format_inr(offer.personalized_price) in demo
    assert format_inr(offer.ownership_credit) in demo
    assert 'data-cart-label="Complete bundle"' in demo
    assert f'href="/courses/{featured_bundle.slug}"' in demo


def test_landing_renders_neutral_state_when_no_eligible_catalog_product_remains(db):
    user = _learner(db)
    products = list(db.scalars(select(Product).where(Product.is_active.is_(True))))
    db.add_all([
        UserEnrollment(
            user_id=user.id,
            product_id=product.id,
            source_product_id=product.id,
        )
        for product in products
    ])
    db.commit()

    with TestClient(app) as client:
        _login(client)
        response = client.get("/")

    assert response.status_code == 200
    opening, demo = _journey_demo(response.text)
    assert "data-product-id" not in opening
    assert "data-nba-card" not in opening
    assert "data-price" not in opening
    assert "No recommendation is necessary right now." in demo
    assert "NO COMMERCIAL ACTION" in demo
    assert "Nothing new to recommend" in demo
    assert "No eligible unowned product remains" in demo
    assert "Agentic AI Professional" not in demo
    assert "₹" not in demo
    assert "data-cart" not in demo
    assert "/courses/" not in demo


def test_landing_substitutes_an_active_product_when_default_feature_is_archived(db):
    default_track = _product(db, "agentic-ai-professional")
    default_track.is_active = False
    db.commit()

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    opening, demo = _journey_demo(response.text)
    substitute_match = re.search(r'data-product-id="([^"]+)"', opening)
    assert substitute_match is not None
    substitute = db.get(Product, substitute_match.group(1))
    assert substitute is not None
    assert substitute.id != default_track.id
    assert substitute.is_active is True
    assert substitute.title in demo
    assert "NO COMMERCIAL ACTION" not in demo


def test_discover_filters_and_backfills_stale_owned_result_without_generation(db, monkeypatch):
    user = _learner(db)
    owned = _product(db, "agentic-ai-foundations")
    _own(db, user, owned)
    _stale_owned_recommendation(db, user, owned)

    now = datetime.now(timezone.utc)
    db.add_all([
        Event(
            event_id="recent-order-older",
            user_id=user.id,
            session_id="recent-order-test",
            event_type="card_hover",
            product_id=owned.id,
            occurred_at=now + timedelta(minutes=1),
        ),
        Event(
            event_id="recent-order-newer",
            user_id=user.id,
            session_id="recent-order-test",
            event_type="product_view",
            product_id=owned.id,
            occurred_at=now + timedelta(minutes=2),
        ),
    ])
    db.commit()
    observed_event_types: list[str] = []

    def no_refresh(_profile, event_types, _has_active):
        observed_event_types.extend(event_types)
        return False, "test_no_refresh"

    monkeypatch.setattr("src.main.should_trigger", no_refresh)
    with TestClient(app) as client:
        _login(client)
        monkeypatch.setattr(
            app.state.recommendations,
            "generate",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("Discover must reuse and filter the stored result")
            ),
        )
        response = client.get("/discover")

    assert response.status_code == 200
    assert observed_event_types[:2] == ["product_view", "card_hover"]
    recommended = response.text.split('id="recommended-for-you"', 1)[1].split(
        'id="all-courses"', 1
    )[0]
    recommended_ids = re.findall(
        r'<article class="course-card recommended[^"]*"[^>]*data-product-id="([^"]+)"',
        recommended,
    )
    assert len(recommended_ids) == 3
    assert owned.id not in recommended_ids
    assert owned.title not in recommended
    replacements = list(db.scalars(select(Product).where(Product.id.in_(recommended_ids))))
    assert len(replacements) == 3
    assert all(is_product_recommendable(product, {owned.id}) for product in replacements)

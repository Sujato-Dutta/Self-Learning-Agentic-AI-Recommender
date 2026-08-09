from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from src.config import Settings
from src.jobs.scheduler import _personalized_delivery_offers
from src.models import NextBestAction, Product, Recommendation, User, UserEnrollment
from src.repositories.enrollments import purchased_product_ids
from src.services.notification_service import NotificationService, _format_inr


def test_partial_bundle_email_uses_live_credit_and_completion_copy(monkeypatch, db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    bundle = db.scalar(select(Product).where(Product.title == "Agentic AI Professional"))
    assert user and bundle and bundle.is_bundle
    owned_course = db.get(Product, bundle.bundled_product_ids[0])
    assert owned_course
    db.add(UserEnrollment(
        user_id=user.id,
        product_id=owned_course.id,
        source_product_id=owned_course.id,
    ))

    now = datetime.now(timezone.utc)
    recommendation = Recommendation(
        user_id=user.id,
        behavior_profile_version=1,
        profile_hash="partial-bundle-delivery",
        headline="A relevant next step",
        narrative="This track closes the learner's remaining production gaps.",
        reason_summary="Partial ownership delivery regression",
        confidence=.8,
        trigger_type="test",
        model_name="test",
        prompt_version="test",
        expires_at=now + timedelta(days=1),
    )
    db.add(recommendation)
    db.flush()
    db.add(NextBestAction(
        user_id=user.id,
        recommendation_id=recommendation.id,
        product_id=bundle.id,
        action_type="offer_bundle",
        persuasion_strategy="bundle_value",
        headline="Complete the professional track",
        message="A grounded recommendation for the remaining courses.",
        rationale="The learner already owns one relevant foundation.",
        intent_stage="high_intent",
        purchase_propensity=.7,
        expected_conversion_probability=.5,
        expected_revenue=Decimal("4000.00"),
        incremental_expected_revenue=Decimal("1000.00"),
        status="active",
        expires_at=now + timedelta(days=1),
    ))
    db.commit()
    db.refresh(recommendation)

    owned_ids = purchased_product_ids(db, user.id)
    offers = _personalized_delivery_offers(db, recommendation, owned_ids)
    offer = offers[bundle.id]
    assert offer.eligible is True
    assert offer.has_ownership_credit is True

    messages = []

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def starttls(self):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            messages.append(message)

    monkeypatch.setattr("src.services.notification_service.smtplib.SMTP", FakeSMTP)
    NotificationService(Settings(app_env="test", smtp_host="smtp.invalid")).send_digest(
        user,
        recommendation,
        "unsubscribe-token",
        owned_product_ids=owned_ids,
        personalized_offers=offers,
    )

    assert len(messages) == 1
    assert "without paying twice" in messages[0]["Subject"]
    html = messages[0].get_body(preferencelist=("html",)).get_content()
    plain = messages[0].get_body(preferencelist=("plain",)).get_content()
    assert "without paying twice" in html
    for body in (html, plain):
        assert owned_course.title in body
        assert f"₹{_format_inr(offer.personalized_price)}" in body
        assert f"₹{_format_inr(offer.ownership_credit)}" in body
        assert "owned-course credit" in body

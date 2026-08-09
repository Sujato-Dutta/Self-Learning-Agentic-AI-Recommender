from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from src.config import Settings
from src.database import SessionLocal
from src.jobs import scheduler as scheduler_module
from src.main import app
from src.models import (
    NextBestAction,
    NotificationPreference,
    Product,
    Recommendation,
    RecommendationItem,
    ScheduledDelivery,
    User,
)
from src.repositories.enrollments import complete_demo_purchase
from src.services.notification_service import NotificationService


def _recommendation(db, user: User, *, created_at: datetime) -> Recommendation:
    recommendation = Recommendation(
        user_id=user.id,
        behavior_profile_version=1,
        profile_hash=f"ownership-{created_at.timestamp()}",
        headline="A relevant next step",
        narrative="A grounded recommendation based on the learner's current direction.",
        reason_summary="Ownership boundary test",
        confidence=.8,
        trigger_type="test",
        model_name="test",
        prompt_version="test",
        created_at=created_at,
        expires_at=created_at + timedelta(days=1),
    )
    db.add(recommendation)
    db.flush()
    return recommendation


def _action(
    db,
    user: User,
    recommendation: Recommendation,
    product: Product | None,
    *,
    status: str,
    created_at: datetime,
    action_type: str = "recommend_course",
) -> NextBestAction:
    action = NextBestAction(
        user_id=user.id,
        recommendation_id=recommendation.id,
        product_id=product.id if product else None,
        action_type=action_type,
        persuasion_strategy="skill_gap",
        headline="Learn this next",
        message="A grounded recommendation message for the selected next step.",
        rationale="Ownership boundary test",
        intent_stage="high_intent",
        purchase_propensity=.7,
        expected_conversion_probability=.5,
        expected_revenue=Decimal("1999.50"),
        incremental_expected_revenue=Decimal(0),
        status=status,
        deliver_at=created_at - timedelta(minutes=1) if status == "scheduled" else None,
        expires_at=created_at + timedelta(days=1),
        created_at=created_at,
    )
    db.add(action)
    db.flush()
    return action


def _login(client: TestClient) -> str:
    client.get("/login")
    csrf = client.cookies.get("csrf_token")
    assert csrf
    response = client.post(
        "/auth/login",
        data={
            "email": "learner@smartreco.dev",
            "password": "LearnerDemo123!",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return csrf


def test_partial_bundle_action_stays_live_until_every_component_is_owned():
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
        course = db.scalar(select(Product).where(Product.title == "Agentic AI Foundations"))
        bundle = db.scalar(select(Product).where(Product.title == "Agentic AI Professional"))
        assert user and course and bundle and course.id in bundle.bundled_product_ids

        older_recommendation = _recommendation(db, user, created_at=now - timedelta(hours=2))
        older_action = _action(
            db, user, older_recommendation, course, status="active",
            created_at=now - timedelta(hours=2),
        )
        attributed_recommendation = _recommendation(db, user, created_at=now - timedelta(hours=1))
        attributed_action = _action(
            db, user, attributed_recommendation, course, status="scheduled",
            created_at=now - timedelta(hours=1), action_type="email_later",
        )
        bundle_recommendation = _recommendation(db, user, created_at=now - timedelta(minutes=30))
        bundle_action = _action(
            db, user, bundle_recommendation, bundle, status="scheduled",
            created_at=now - timedelta(minutes=30), action_type="email_later",
        )
        action_ids = (older_action.id, attributed_action.id, bundle_action.id)
        course_id = course.id
        remaining_component_ids = [
            product_id for product_id in bundle.bundled_product_ids
            if product_id != course_id
        ]
        db.commit()

    with TestClient(app) as client:
        csrf = _login(client)
        partial_response = client.post(
            "/api/demo-purchases",
            headers={"X-CSRF-Token": csrf},
            json={"product_ids": [course_id], "source": "cart_checkout"},
        )

        assert partial_response.status_code == 200
        assert partial_response.json()["rewards_attributed"] == 1
        with SessionLocal() as db:
            older, attributed, bundle_action = (
                db.get(NextBestAction, action_id) for action_id in action_ids
            )
            assert attributed.status == "converted"
            assert attributed.outcome_type == "purchase"
            assert older.status == "cancelled"
            assert older.outcome_type == "already_owned"
            assert bundle_action.status == "scheduled"
            assert bundle_action.outcome_type is None

        completed_response = client.post(
            "/api/demo-purchases",
            headers={"X-CSRF-Token": csrf},
            json={"product_ids": remaining_component_ids, "source": "cart_checkout"},
        )

    assert completed_response.status_code == 200
    with SessionLocal() as db:
        bundle_action = db.get(NextBestAction, action_ids[2])
        assert bundle_action.status == "cancelled"
        assert bundle_action.outcome_type == "already_owned"


class _FakeScheduler:
    def __init__(self, *args, **kwargs):
        self.jobs = {}
        self.started = False

    def add_job(self, function, *args, id: str, **kwargs):
        self.jobs[id] = function

    def start(self):
        self.started = True


class _NoopOutbox:
    def process_pending(self, db):
        raise AssertionError("outbox must not run in this test")


class _NoopRecommendations:
    def generate(self, db, user_id: str, trigger_type: str):
        raise AssertionError("digest generation must not run in this test")


class _RecordingNotifications:
    eligible_items = staticmethod(NotificationService.eligible_items)

    def __init__(self):
        self.sent = []

    def send_digest(self, *args, **kwargs):
        self.sent.append((args, kwargs))


def test_due_productless_email_is_cancelled_when_every_item_is_owned(monkeypatch):
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
        course = db.scalar(select(Product).where(Product.title == "Agentic AI Foundations"))
        assert user and course
        complete_demo_purchase(db, user.id, [course.id], "test_purchase")
        preference = db.get(NotificationPreference, user.id)
        preference.email_enabled = True

        recommendation = _recommendation(db, user, created_at=now - timedelta(hours=1))
        db.add(RecommendationItem(
            recommendation_id=recommendation.id,
            product_id=course.id,
            rank=1,
            retrieval_score=.9,
            rerank_score=.9,
            final_score=.9,
            reason="Previously relevant, but now owned.",
        ))
        action = _action(
            db, user, recommendation, None, status="scheduled",
            created_at=now - timedelta(hours=1), action_type="email_later",
        )
        action_id = action.id
        db.commit()

    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", _FakeScheduler)
    notifications = _RecordingNotifications()
    scheduler = scheduler_module.start_scheduler(
        Settings(app_env="test", scheduler_enabled=True, mesh_calls_enabled=False),
        _NoopOutbox(),
        _NoopRecommendations(),
        notifications,
    )
    assert scheduler and scheduler.started
    scheduler.jobs["due-next-best-actions"]()

    with SessionLocal() as db:
        action = db.get(NextBestAction, action_id)
        assert action.status == "cancelled"
        assert action.outcome_type == "no_eligible_products"
        assert db.scalar(select(func.count()).select_from(ScheduledDelivery)) == 0
    assert notifications.sent == []


def test_notification_digest_filters_owned_items_before_rendering(monkeypatch, db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    owned = db.scalar(select(Product).where(Product.title == "Agentic AI Foundations"))
    unowned = db.scalar(select(Product).where(Product.title == "Production RAG Systems"))
    assert user and owned and unowned
    recommendation = _recommendation(db, user, created_at=datetime.now(timezone.utc))
    for rank, product in enumerate((owned, unowned), start=1):
        db.add(RecommendationItem(
            recommendation_id=recommendation.id,
            product_id=product.id,
            rank=rank,
            retrieval_score=.9,
            rerank_score=.9,
            final_score=.9,
            reason=f"Continue with {product.title}.",
        ))
    db.commit()

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
    service = NotificationService(Settings(app_env="test", smtp_host="smtp.invalid"))
    service.send_digest(
        user,
        recommendation,
        "unsubscribe-token",
        owned_product_ids={owned.id},
    )

    assert len(messages) == 1
    html = messages[0].get_body(preferencelist=("html",)).get_content()
    assert owned.title not in html
    assert unowned.title in html

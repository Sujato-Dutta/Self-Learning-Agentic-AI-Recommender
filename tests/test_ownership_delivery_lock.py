import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from src.config import Settings
from src.database import SessionLocal
from src.jobs import scheduler as scheduler_module
from src.models import (
    NextBestAction,
    NotificationPreference,
    Product,
    Recommendation,
    ScheduledDelivery,
    User,
)
from src.services.notification_service import NotificationService
from src.services.ownership_service import ownership_delivery_guard


def _recommendation(db, user: User, now: datetime) -> Recommendation:
    recommendation = Recommendation(
        user_id=user.id,
        behavior_profile_version=1,
        profile_hash=f"delivery-lock-{now.timestamp()}",
        headline="A relevant next step",
        narrative="A grounded next step for this learner.",
        reason_summary="Ownership delivery regression",
        confidence=.8,
        trigger_type="test",
        model_name="test",
        prompt_version="test",
        created_at=now,
        expires_at=now + timedelta(days=1),
    )
    db.add(recommendation)
    db.flush()
    return recommendation


def _scheduled_action(
    db,
    user: User,
    recommendation: Recommendation,
    product: Product,
    now: datetime,
) -> NextBestAction:
    action = NextBestAction(
        user_id=user.id,
        recommendation_id=recommendation.id,
        product_id=product.id,
        action_type="email_later",
        persuasion_strategy="skill_gap",
        headline="Take this next",
        message="This closes the next relevant skill gap.",
        rationale="The learner's recent activity supports this next step.",
        intent_stage="high_intent",
        purchase_propensity=.7,
        expected_conversion_probability=.5,
        expected_revenue=Decimal("2499.50"),
        incremental_expected_revenue=Decimal(0),
        status="scheduled",
        deliver_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
        created_at=now - timedelta(hours=1),
    )
    db.add(action)
    db.flush()
    return action


def test_process_guard_serializes_one_learner_and_releases_after_failure():
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    failures: list[BaseException] = []

    def first_worker() -> None:
        try:
            with SessionLocal() as db, ownership_delivery_guard(db, "shared-learner"):
                first_entered.set()
                assert release_first.wait(timeout=2)
        except BaseException as exc:  # noqa: BLE001 - surface thread failures to pytest
            failures.append(exc)

    def second_worker() -> None:
        try:
            assert first_entered.wait(timeout=2)
            second_started.set()
            with SessionLocal() as db, ownership_delivery_guard(db, "shared-learner"):
                second_entered.set()
        except BaseException as exc:  # noqa: BLE001 - surface thread failures to pytest
            failures.append(exc)

    first = threading.Thread(target=first_worker)
    second = threading.Thread(target=second_worker)
    first.start()
    second.start()
    assert first_entered.wait(timeout=2)
    assert second_started.wait(timeout=2)
    assert not second_entered.wait(timeout=.1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
    assert failures == []

    with SessionLocal() as db:
        with (
            pytest.raises(RuntimeError, match="release regression"),
            ownership_delivery_guard(db, "shared-learner"),
        ):
            raise RuntimeError("release regression")
        with ownership_delivery_guard(db, "shared-learner"):
            pass


def test_postgres_guard_uses_dedicated_session_and_always_unlocks():
    calls = []

    class FakeConnection:
        def __enter__(self):
            calls.append("connection_entered")
            return self

        def __exit__(self, *args):
            calls.append("connection_closed")
            return False

        def execute(self, statement, parameters):
            calls.append((str(statement), parameters["lock_key"]))

    class FakeEngine:
        def connect(self):
            calls.append("dedicated_connect")
            return FakeConnection()

    class FakeBind:
        dialect = type("Dialect", (), {"name": "postgresql"})()
        engine = FakeEngine()

    class FakeSession:
        @staticmethod
        def get_bind():
            return FakeBind()

    with (
        pytest.raises(RuntimeError, match="provider failed"),
        ownership_delivery_guard(FakeSession(), "postgres-learner"),
    ):
        calls.append("guard_body")
        raise RuntimeError("provider failed")

    assert calls[:2] == ["dedicated_connect", "connection_entered"]
    assert calls[2][0] == "SELECT pg_advisory_lock(:lock_key)"
    assert calls[3] == "guard_body"
    assert calls[4][0] == "SELECT pg_advisory_unlock(:lock_key)"
    assert calls[2][1] == calls[4][1]
    assert calls[5] == "connection_closed"


class _FakeScheduler:
    def __init__(self, *args, **kwargs):
        self.jobs = {}

    def add_job(self, function, *args, id: str, **kwargs):
        self.jobs[id] = function

    def start(self):
        pass


class _NoopOutbox:
    def process_pending(self, db):
        raise AssertionError("outbox must not run")


class _NoopRecommendations:
    def generate(self, db, user_id: str, trigger_type: str):
        raise AssertionError("daily recommendation generation must not run")


class _RejectingNotifications:
    eligible_items = staticmethod(NotificationService.eligible_items)

    def __init__(self):
        self.send_count = 0

    def send_digest(self, *args, **kwargs):
        self.send_count += 1
        raise AssertionError("a checkout-cancelled action must never be sent")


def test_due_action_refresh_preserves_checkout_cancellation_after_lock(monkeypatch):
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
        product = db.scalar(select(Product).where(Product.title == "Production RAG Systems"))
        assert user and product
        preference = db.get(NotificationPreference, user.id)
        assert preference
        preference.email_enabled = True
        recommendation = _recommendation(db, user, now - timedelta(hours=1))
        action = _scheduled_action(db, user, recommendation, product, now)
        action_id = action.id
        db.commit()

    original_guard = scheduler_module.ownership_delivery_guard
    checkout_committed = False

    @contextmanager
    def checkout_wins_before_scheduler_guard(db, user_id: str):
        nonlocal checkout_committed
        # The scheduler already enumerated its due list. Persist a checkout-style
        # cancellation at the lock boundary; its post-lock get must observe it.
        db.execute(
            update(NextBestAction).where(NextBestAction.id == action_id).values(
                status="cancelled",
                outcome_type="already_owned",
                outcome_at=now,
            ).execution_options(synchronize_session=False)
        )
        db.commit()
        checkout_committed = True
        with original_guard(db, user_id):
            yield

    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", _FakeScheduler)
    monkeypatch.setattr(
        scheduler_module,
        "ownership_delivery_guard",
        checkout_wins_before_scheduler_guard,
    )
    notifications = _RejectingNotifications()
    scheduler = scheduler_module.start_scheduler(
        Settings(app_env="test", scheduler_enabled=True, mesh_calls_enabled=False),
        _NoopOutbox(),
        _NoopRecommendations(),
        notifications,
    )
    assert scheduler
    scheduler.jobs["due-next-best-actions"]()

    assert checkout_committed
    assert notifications.send_count == 0
    with SessionLocal() as db:
        action = db.get(NextBestAction, action_id)
        assert action and action.status == "cancelled"
        assert action.outcome_type == "already_owned"
        assert db.scalar(select(func.count()).select_from(ScheduledDelivery)) == 0


def test_direct_bundle_action_email_renders_product_link_and_details(monkeypatch, db):
    now = datetime.now(timezone.utc)
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    bundle = db.scalar(select(Product).where(Product.title == "Agentic AI Professional"))
    assert user and bundle and bundle.is_bundle
    recommendation = _recommendation(db, user, now)
    action = _scheduled_action(db, user, recommendation, bundle, now)
    action.action_type = "offer_bundle"
    action.status = "active"
    db.commit()
    assert recommendation.items == []

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
    service.send_digest(user, recommendation, "unsubscribe-token")

    assert len(messages) == 1
    html = messages[0].get_body(preferencelist=("html",)).get_content()
    plain = messages[0].get_body(preferencelist=("plain",)).get_content()
    expected_path = f"/courses/{bundle.slug}?utm_source=smartreco_digest"
    assert bundle.title in html
    assert expected_path in html
    assert "Bundle" in html
    assert "<ol><li" in html
    assert bundle.title in plain
    assert expected_path in plain

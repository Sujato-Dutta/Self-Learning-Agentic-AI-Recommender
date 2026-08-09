from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func, select

from src.config import Settings
from src.database import SessionLocal
from src.models import (
    Event,
    NextBestAction,
    NotificationPreference,
    Product,
    Recommendation,
    ScheduledDelivery,
    User,
)
from src.observability.logging import logger
from src.observability.metrics import SCHEDULER_RUNS
from src.repositories.enrollments import purchased_product_ids
from src.services.bundle_pricing_service import (
    PersonalizedProductOffer,
    calculate_personalized_offer,
)
from src.services.notification_service import NotificationService
from src.services.outbox_service import OutboxService
from src.services.ownership_service import (
    invalidate_action_for_ownership,
    ownership_delivery_guard,
)
from src.services.recommendation_service import RecommendationService


def _personalized_delivery_offers(
    db,
    recommendation: Recommendation,
    owned_product_ids: set[str],
) -> dict[str, PersonalizedProductOffer]:
    """Build authoritative offers for every product that an email may render."""
    products = {
        item.product.id: item.product
        for item in recommendation.items
        if item.product
    }
    action = recommendation.next_best_action
    if action and action.product:
        products[action.product.id] = action.product
    component_ids = {
        component_id
        for product in products.values()
        if product.is_bundle
        for component_id in (product.bundled_product_ids or [])
    }
    components = (
        {
            product.id: product
            for product in db.scalars(select(Product).where(
                Product.id.in_(component_ids),
                Product.is_active.is_(True),
            ))
        }
        if component_ids else {}
    )
    offers: dict[str, PersonalizedProductOffer] = {}
    for product in products.values():
        bundle_components = [
            components[component_id]
            for component_id in (product.bundled_product_ids or [])
            if component_id in components
        ]
        try:
            offers[product.id] = calculate_personalized_offer(
                product,
                bundle_components,
                owned_product_ids,
            )
        except ValueError:
            # A malformed bundle is omitted; NotificationService fails closed
            # when the scheduled delivery attempts to render it.
            continue
    return offers


def digest_preference_is_due(
    preference: NotificationPreference,
    now: datetime,
) -> bool:
    """Match a digest hour in the learner's validated IANA timezone."""
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    try:
        learner_now = now_utc.astimezone(ZoneInfo(preference.timezone or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "digest_timezone_invalid",
            user_id=preference.user_id,
        )
        return False
    return learner_now.hour == preference.preferred_hour


def start_scheduler(settings: Settings, outbox: OutboxService, recommendations: RecommendationService,
                    notifications: NotificationService) -> BackgroundScheduler | None:
    if not settings.scheduler_enabled:
        return None
    scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1})

    def process_outbox() -> None:
        with SessionLocal() as db:
            try:
                outbox.process_pending(db)
                SCHEDULER_RUNS.labels(job="outbox", status="success").inc()
            except Exception as exc:  # noqa: BLE001 - scheduler boundary isolates failed jobs
                logger.error("outbox_job_failed", error_type=type(exc).__name__)
                SCHEDULER_RUNS.labels(job="outbox", status="failed").inc()

    def send_digests() -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            preferences = [
                preference
                for preference in db.scalars(select(NotificationPreference).where(
                    NotificationPreference.email_enabled.is_(True),
                ))
                if digest_preference_is_due(preference, now)
            ]
            for preference in preferences:
                user_id = preference.user_id
                activity = db.scalar(select(func.count()).select_from(Event).where(
                    Event.user_id == user_id,
                    Event.occurred_at >= now - timedelta(hours=24),
                )) or 0
                if activity < 2:
                    continue
                delivery = None
                try:
                    user = db.get(User, user_id)
                    if not user:
                        continue
                    recommendation = recommendations.generate(db, user.id, trigger_type="daily_digest")
                    recommendation_id = recommendation.id
                    # Do not wait on the cross-worker lock while retaining the
                    # generation transaction. The post-lock get below is the
                    # authoritative ownership/action snapshot.
                    db.rollback()
                    with ownership_delivery_guard(db, user_id):
                        # Generation ran before the ownership lock. Reload after
                        # acquiring it so a checkout cancellation wins cleanly.
                        recommendation = db.get(
                            Recommendation,
                            recommendation_id,
                            populate_existing=True,
                        )
                        preference = db.get(NotificationPreference, user_id)
                        user = db.get(User, user_id)
                        if not recommendation or not preference or not preference.email_enabled or not user:
                            db.rollback()
                            continue
                        action = recommendation.next_best_action
                        if action and action.status not in {"active", "scheduled"}:
                            db.rollback()
                            continue
                        if action and action.action_type in {
                            "show_nothing",
                            "delay_recommendation",
                            "email_later",
                        }:
                            db.rollback()
                            continue
                        owned_ids = purchased_product_ids(db, user_id)
                        personalized_offers = _personalized_delivery_offers(
                            db,
                            recommendation,
                            owned_ids,
                        )
                        if action and invalidate_action_for_ownership(action, owned_ids, outcome_at=now):
                            db.commit()
                            continue
                        if (not action or not action.product_id) and not notifications.eligible_items(
                            recommendation, owned_ids
                        ):
                            if action:
                                action.status = "cancelled"
                                action.outcome_type = "no_eligible_products"
                                action.outcome_at = now
                                db.commit()
                            else:
                                db.rollback()
                            continue
                        delivery = ScheduledDelivery(
                            user_id=user.id,
                            recommendation_id=recommendation.id,
                            status="sending",
                            scheduled_at=now,
                        )
                        db.add(delivery)
                        db.commit()
                        try:
                            notifications.send_digest(
                                user,
                                recommendation,
                                preference.unsubscribe_token,
                                owned_product_ids=owned_ids,
                                personalized_offers=personalized_offers,
                            )
                            delivery.status = "delivered"
                            delivery.delivered_at = datetime.now(timezone.utc)
                            db.commit()
                        except Exception as exc:  # noqa: BLE001 - provider boundary
                            delivery.status = "failed"
                            delivery.attempts += 1
                            delivery.error = str(exc)[:1000]
                            db.commit()
                except Exception as exc:  # noqa: BLE001 - record delivery failure and continue users
                    db.rollback()
                    if delivery:
                        delivery.status = "failed"
                        delivery.attempts += 1
                        delivery.error = str(exc)[:1000]
                    else:
                        delivery = ScheduledDelivery(
                            user_id=user_id,
                            status="failed",
                            attempts=1,
                            error=str(exc)[:1000],
                            scheduled_at=now,
                        )
                        db.add(delivery)
                    db.commit()
            SCHEDULER_RUNS.labels(job="digest", status="success").inc()

    def send_due_actions() -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            expired = list(db.scalars(select(NextBestAction).where(
                NextBestAction.status == "scheduled", NextBestAction.expires_at <= now,
            )))
            for action in expired:
                action.status = "expired"
            if expired:
                db.commit()
            queued_actions = [
                (action.id, action.user_id)
                for action in db.scalars(select(NextBestAction).where(
                    NextBestAction.action_type == "email_later",
                    NextBestAction.status == "scheduled",
                    NextBestAction.deliver_at <= now,
                    NextBestAction.expires_at > now,
                ).limit(100))
            ]
            # Release the enumeration transaction before waiting on checkout;
            # otherwise a SQLite reader and writer could block one another in
            # the opposite order of the process lock.
            db.rollback()
            for action_id, user_id in queued_actions:
                with ownership_delivery_guard(db, user_id):
                    # The initial due-action query intentionally happens before
                    # acquiring each learner lock. Reload after the lock so an
                    # intervening checkout cancellation cannot be overwritten by
                    # a stale ORM instance.
                    action = db.get(NextBestAction, action_id, populate_existing=True)
                    if (
                        not action
                        or action.action_type != "email_later"
                        or action.status != "scheduled"
                    ):
                        db.rollback()
                        continue

                    expires_at = action.expires_at
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    if expires_at <= now:
                        action.status = "expired"
                        db.commit()
                        continue
                    deliver_at = action.deliver_at
                    if deliver_at is None:
                        db.rollback()
                        continue
                    if deliver_at.tzinfo is None:
                        deliver_at = deliver_at.replace(tzinfo=timezone.utc)
                    if deliver_at > now:
                        db.rollback()
                        continue

                    owned_ids = purchased_product_ids(db, user_id)
                    personalized_offers = _personalized_delivery_offers(
                        db,
                        action.recommendation,
                        owned_ids,
                    )
                    if invalidate_action_for_ownership(action, owned_ids, outcome_at=now):
                        db.commit()
                        continue
                    if not action.product_id and not notifications.eligible_items(
                        action.recommendation, owned_ids
                    ):
                        action.status = "cancelled"
                        action.outcome_type = "no_eligible_products"
                        action.outcome_at = now
                        db.commit()
                        continue
                    preference = db.get(NotificationPreference, user_id)
                    user = db.get(User, user_id)
                    if not preference or not preference.email_enabled or not user:
                        action.status = "cancelled"
                        db.commit()
                        continue
                    attempts = db.scalar(select(func.count()).select_from(ScheduledDelivery).where(
                        ScheduledDelivery.recommendation_id == action.recommendation_id,
                    )) or 0
                    if attempts >= 3:
                        action.status = "failed"
                        db.commit()
                        continue
                    delivery = ScheduledDelivery(
                        user_id=user.id,
                        recommendation_id=action.recommendation_id,
                        status="sending",
                        attempts=attempts + 1,
                        scheduled_at=action.deliver_at,
                    )
                    db.add(delivery)
                    db.commit()
                    try:
                        notifications.send_digest(
                            user,
                            action.recommendation,
                            preference.unsubscribe_token,
                            owned_product_ids=owned_ids,
                            personalized_offers=personalized_offers,
                        )
                        delivery.status = "delivered"
                        delivery.delivered_at = datetime.now(timezone.utc)
                        action.status = "delivered"
                        db.commit()
                    except Exception as exc:  # noqa: BLE001 - provider boundary
                        delivery.status = "failed"
                        delivery.error = str(exc)[:1000]
                        db.commit()
            SCHEDULER_RUNS.labels(job="due_next_best_actions", status="success").inc()

    if settings.mesh_calls_enabled:
        scheduler.add_job(process_outbox, "interval", seconds=20, id="vector-outbox", replace_existing=True)
    else:
        logger.info("vector_outbox_paused", reason="mesh_calls_disabled")
    scheduler.add_job(send_digests, "cron", minute=0, id="daily-digests", replace_existing=True)
    scheduler.add_job(send_due_actions, "interval", minutes=15, id="due-next-best-actions", replace_existing=True)
    scheduler.start()
    return scheduler

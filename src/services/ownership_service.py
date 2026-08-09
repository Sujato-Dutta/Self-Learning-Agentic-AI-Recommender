import hashlib
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from src.models import NextBestAction, Product
from src.services.bundle_pricing_service import is_product_offer_eligible

ACTIONABLE_RECOMMENDATION_STATUSES = ("active", "scheduled")
OWNERSHIP_CANCELLATION_OUTCOME = "already_owned"

_ownership_delivery_locks: dict[str, threading.RLock] = {}
_ownership_delivery_locks_guard = threading.Lock()


def _ownership_delivery_lock(user_id: str) -> threading.RLock:
    """Return the process-local half of a learner's checkout/delivery lock."""
    with _ownership_delivery_locks_guard:
        return _ownership_delivery_locks.setdefault(user_id, threading.RLock())


def _ownership_advisory_lock_key(user_id: str) -> int:
    """Build a stable, namespaced signed bigint for PostgreSQL advisory locks."""
    digest = hashlib.sha256(f"smartreco:ownership-delivery:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def ownership_delivery_guard(db: Session, user_id: str) -> Iterator[None]:
    """Serialize checkout and outbound recommendation delivery for one learner.

    The process lock covers SQLite and single-worker deployments. PostgreSQL
    deployments additionally take a session-level advisory lock on a dedicated
    connection, so the guard also coordinates independent web and scheduler
    processes. Closing that dedicated connection is a final safety net if the
    explicit unlock itself fails.
    """
    with _ownership_delivery_lock(user_id):
        bind = db.get_bind()
        if bind.dialect.name != "postgresql":
            yield
            return

        lock_key = _ownership_advisory_lock_key(user_id)
        lock_engine = getattr(bind, "engine", bind)
        with lock_engine.connect() as lock_connection:
            lock_connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
            try:
                yield
            finally:
                lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lock_key},
                )


def product_conflicts_with_ownership(
    product: Product | None,
    owned_product_ids: Iterable[str],
) -> bool:
    """Return whether an offer duplicates learning the learner fully owns.

    Partial bundle ownership is intentionally *not* a conflict.  Those bundles
    remain useful completion offers and receive an ownership credit at pricing
    time.  Only the exact SKU, or a bundle whose every component is already
    owned, is redundant.
    """
    if not product:
        return False
    owned = owned_product_ids if isinstance(owned_product_ids, set) else set(owned_product_ids)
    if product.id in owned:
        return True
    component_ids = set(product.bundled_product_ids or [])
    return bool(product.is_bundle and component_ids and component_ids.issubset(owned))


def is_product_recommendable(product: Product | None, owned_product_ids: Iterable[str]) -> bool:
    """Return whether a product can be used on a recommendation surface.

    Catalog pages may still display owned products as purchased. Recommendation
    surfaces reject an exact owned SKU and a fully covered bundle, while keeping
    partially owned bundles eligible for personalized completion pricing.
    """
    return is_product_offer_eligible(product, owned_product_ids)


def invalidate_action_for_ownership(
    action: NextBestAction,
    owned_product_ids: Iterable[str],
    *,
    outcome_at: datetime | None = None,
) -> bool:
    """Cancel one actionable product action when live ownership makes it redundant."""
    if action.status not in ACTIONABLE_RECOMMENDATION_STATUSES or not action.product_id:
        return False
    if not product_conflicts_with_ownership(action.product, owned_product_ids):
        return False
    action.status = "cancelled"
    action.outcome_type = OWNERSHIP_CANCELLATION_OUTCOME
    action.outcome_at = outcome_at or datetime.now(timezone.utc)
    return True


def cancel_owned_recommendation_actions(
    db: Session,
    user_id: str,
    owned_product_ids: Iterable[str],
    *,
    outcome_at: datetime | None = None,
) -> int:
    """Invalidate every still-actionable recommendation made redundant by ownership.

    The caller owns the transaction so reward attribution can be committed before
    this cleanup. Converted actions are deliberately left untouched.
    """
    owned = owned_product_ids if isinstance(owned_product_ids, set) else set(owned_product_ids)
    actions = db.scalars(select(NextBestAction).where(
        NextBestAction.user_id == user_id,
        NextBestAction.status.in_(ACTIONABLE_RECOMMENDATION_STATUSES),
        NextBestAction.product_id.is_not(None),
    ))
    return sum(
        invalidate_action_for_ownership(action, owned, outcome_at=outcome_at)
        for action in actions
    )


def related_unowned_products(
    products: Iterable[Product],
    anchor: Product,
    owned_product_ids: Iterable[str],
    *,
    limit: int = 3,
) -> list[Product]:
    """Rank catalog-related products while enforcing live ownership guards."""
    owned = owned_product_ids if isinstance(owned_product_ids, set) else set(owned_product_ids)
    anchor_skills = {skill.lower() for skill in anchor.skills or []}

    eligible = [
        product
        for product in products
        if product.id != anchor.id and is_product_recommendable(product, owned)
    ]

    def relevance(product: Product) -> tuple[float, float, str]:
        shared_skills = len(anchor_skills.intersection(
            skill.lower() for skill in product.skills or []
        ))
        score = (
            (5 if product.skill_bundle == anchor.skill_bundle else 0)
            + (2 if product.category == anchor.category else 0)
            + shared_skills * 1.25
            + (0.4 if product.difficulty == anchor.difficulty else 0)
            + float(product.popularity)
        )
        return score, float(product.popularity), product.title

    return sorted(eligible, key=relevance, reverse=True)[:limit]

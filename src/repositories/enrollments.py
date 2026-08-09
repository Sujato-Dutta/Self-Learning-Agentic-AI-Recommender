import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src.models import Event, Product, UserEnrollment
from src.services.bundle_pricing_service import (
    PersonalizedProductOffer,
    calculate_personalized_offer,
)


class PurchaseUnavailableError(ValueError):
    def __init__(self, product_ids: list[str]) -> None:
        self.product_ids = product_ids
        super().__init__("one or more courses are unavailable")


@dataclass(frozen=True)
class DemoPurchaseResult:
    purchased_product_ids: list[str]
    enrolled_product_ids: list[str]
    newly_enrolled_product_ids: list[str]
    already_enrolled_product_ids: list[str]
    event_ids: list[str]
    offers: list[PersonalizedProductOffer]
    catalog_total: Decimal
    ownership_credit_total: Decimal
    payable_total: Decimal
    savings_total: Decimal


def purchased_product_ids(db: Session, user_id: str) -> set[str]:
    return set(db.scalars(select(UserEnrollment.product_id).where(
        UserEnrollment.user_id == user_id
    )))


def list_user_enrollments(db: Session, user_id: str) -> list[UserEnrollment]:
    return list(db.scalars(select(UserEnrollment).where(
        UserEnrollment.user_id == user_id
    ).order_by(UserEnrollment.purchased_at.desc())))


def list_learning_enrollments(db: Session, user_id: str) -> list[UserEnrollment]:
    return list(db.scalars(select(UserEnrollment).join(
        Product, UserEnrollment.product_id == Product.id
    ).where(
        UserEnrollment.user_id == user_id,
        Product.is_active.is_(True),
        Product.is_bundle.is_(False),
    ).order_by(UserEnrollment.purchased_at.desc())))


def _active_products(db: Session, product_ids: list[str]) -> dict[str, Product]:
    if not product_ids:
        return {}
    products = list(db.scalars(select(Product).where(
        Product.id.in_(product_ids), Product.is_active.is_(True)
    )))
    return {product.id: product for product in products}


def complete_demo_purchase(
    db: Session,
    user_id: str,
    requested_product_ids: list[str],
    source: str,
) -> DemoPurchaseResult:
    """Atomically persist purchased SKUs, expanded access, and behavior events.

    The `(user_id, product_id)` entitlement and deterministic root event IDs make
    retries safe. Bundle components grant access but do not create synthetic
    purchase events, avoiding inflated recommendation intent.
    """
    requested_ids = list(dict.fromkeys(requested_product_ids))
    requested = _active_products(db, requested_ids)
    unavailable = [product_id for product_id in requested_ids if product_id not in requested]
    if unavailable:
        raise PurchaseUnavailableError(unavailable)

    selected_component_ids = {
        component_id
        for product in requested.values()
        if product.is_bundle
        for component_id in (product.bundled_product_ids or [])
    }
    root_ids = [
        product_id for product_id in requested_ids
        if requested[product_id].is_bundle or product_id not in selected_component_ids
    ]
    root_products = [requested[product_id] for product_id in root_ids]

    component_ids = list(dict.fromkeys(
        component_id
        for product in root_products
        if product.is_bundle
        for component_id in (product.bundled_product_ids or [])
    ))
    components = _active_products(db, component_ids)
    unavailable_components = [product_id for product_id in component_ids if product_id not in components]
    if unavailable_components or any(product.is_bundle for product in components.values()):
        raise PurchaseUnavailableError(unavailable_components or component_ids)

    # Quote from the authoritative catalog and the learner's live entitlements.
    # Effective ownership grows across the normalized cart so overlapping
    # bundles cannot charge twice for the same component in one checkout.
    def bundle_price_multiplier(product: Product) -> Decimal:
        subtotal = sum(
            (max(components[component_id].price, Decimal(0))
             for component_id in product.bundled_product_ids or []),
            start=Decimal(0),
        )
        if subtotal <= 0:
            return Decimal(0)
        return min(max(product.price, Decimal(0)), subtotal) / subtotal

    # Apply standalone roots in their normalized order, followed by overlapping
    # bundles from strongest discount to weakest. This stable server-side order
    # prevents client-controlled product-ID ordering from changing the total.
    quote_order = [product for product in root_products if not product.is_bundle]
    quote_order.extend(sorted(
        (product for product in root_products if product.is_bundle),
        key=lambda product: (bundle_price_multiplier(product), product.id),
    ))

    effective_owned_ids = purchased_product_ids(db, user_id)
    offer_by_product_id: dict[str, PersonalizedProductOffer] = {}
    for product in quote_order:
        bundled_products = (
            [components[component_id] for component_id in product.bundled_product_ids or []]
            if product.is_bundle else []
        )
        offer = calculate_personalized_offer(
            product,
            bundled_products,
            effective_owned_ids,
        )
        offer_by_product_id[product.id] = offer
        if offer.eligible:
            effective_owned_ids.add(product.id)
            effective_owned_ids.update(product.bundled_product_ids or [])

    offers = [offer_by_product_id[product.id] for product in root_products]

    def pricing_metadata(product_id: str) -> dict:
        offer = offer_by_product_id[product_id]
        return {
            "currency": "INR",
            "eligible": offer.eligible,
            "eligibility_reason": offer.eligibility_reason,
            "catalog_price": str(offer.catalog_price),
            "full_standalone_subtotal": str(offer.full_standalone_subtotal),
            "owned_standalone_subtotal": str(offer.owned_standalone_subtotal),
            "ownership_credit": str(offer.ownership_credit),
            "remaining_standalone_subtotal": str(offer.remaining_standalone_subtotal),
            "payable": str(offer.personalized_price),
            "savings": str(offer.savings),
            "discount_rate": str(offer.discount_rate),
            "discount_percent": offer.discount_percent,
            "owned_component_ids": [item.product_id for item in offer.owned_components],
            "remaining_component_ids": [
                item.product_id for item in offer.remaining_components
            ],
        }

    entitlement_sources: dict[str, str] = {}
    for product in root_products:
        entitlement_sources[product.id] = product.id
        if product.is_bundle:
            for component_id in product.bundled_product_ids or []:
                entitlement_sources.setdefault(component_id, product.id)

    now = datetime.now(timezone.utc)
    enrollment_rows = [{
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "product_id": product_id,
        "source_product_id": source_product_id,
        "purchased_at": now,
    } for product_id, source_product_id in entitlement_sources.items()]
    dialect = db.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError(f"Demo purchases do not support the {dialect} SQL dialect")
    insert = postgresql_insert if dialect == "postgresql" else sqlite_insert

    try:
        enrollment_statement = insert(UserEnrollment.__table__).values(enrollment_rows).on_conflict_do_nothing(
            index_elements=["user_id", "product_id"]
        ).returning(UserEnrollment.product_id)
        newly_enrolled = set(db.scalars(enrollment_statement))

        new_root_ids = [product_id for product_id in root_ids if product_id in newly_enrolled]
        event_rows = [{
            "id": str(uuid.uuid4()),
            "event_id": f"enrollment:{user_id}:{product_id}",
            "user_id": user_id,
            "session_id": f"demo-purchase:{user_id}",
            "event_type": "enrollment",
            "product_id": product_id,
            "search_query": None,
            "metadata": {
                "source": source,
                "pricing": pricing_metadata(product_id),
            },
            "occurred_at": now,
            "received_at": now,
        } for product_id in new_root_ids]
        event_ids: list[str] = []
        if event_rows:
            event_statement = insert(Event.__table__).values(event_rows).on_conflict_do_nothing(
                index_elements=["event_id"]
            ).returning(Event.event_id)
            event_ids = list(db.scalars(event_statement))
        db.commit()
    except Exception:
        db.rollback()
        raise

    entitlement_ids = list(entitlement_sources)
    return DemoPurchaseResult(
        purchased_product_ids=root_ids,
        enrolled_product_ids=entitlement_ids,
        newly_enrolled_product_ids=[
            product_id for product_id in entitlement_ids if product_id in newly_enrolled
        ],
        already_enrolled_product_ids=[
            product_id for product_id in entitlement_ids if product_id not in newly_enrolled
        ],
        event_ids=event_ids,
        offers=offers,
        catalog_total=sum((offer.catalog_price for offer in offers), start=Decimal("0.00")),
        ownership_credit_total=sum(
            (offer.ownership_credit for offer in offers),
            start=Decimal("0.00"),
        ),
        payable_total=sum(
            (offer.personalized_price for offer in offers),
            start=Decimal("0.00"),
        ),
        savings_total=sum((offer.savings for offer in offers), start=Decimal("0.00")),
    )

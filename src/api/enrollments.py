from fastapi import APIRouter, HTTPException, Request

from src.dependencies import CurrentUser, DbSession
from src.observability.logging import logger
from src.observability.metrics import EVENTS_RECEIVED
from src.repositories.enrollments import (
    PurchaseUnavailableError,
    complete_demo_purchase,
    purchased_product_ids,
)
from src.schemas.enrollments import (
    DemoPurchaseIn,
    DemoPurchaseOfferOut,
    DemoPurchaseOut,
)
from src.security import validate_csrf
from src.services.behavior_service import aggregate_profile
from src.services.cache_service import cache
from src.services.ownership_service import (
    cancel_owned_recommendation_actions,
    ownership_delivery_guard,
)
from src.services.reward_service import attribute_event_rewards

router = APIRouter(prefix="/api", tags=["enrollments"])


@router.post("/demo-purchases", response_model=DemoPurchaseOut)
def create_demo_purchase(
    payload: DemoPurchaseIn,
    request: Request,
    user: CurrentUser,
    db: DbSession,
) -> DemoPurchaseOut:
    validate_csrf(request, request.headers.get("X-CSRF-Token"))
    with ownership_delivery_guard(db, user.id):
        try:
            result = complete_demo_purchase(
                db,
                user.id,
                payload.product_ids,
                payload.source,
            )
        except PurchaseUnavailableError as exc:
            raise HTTPException(
                status_code=404,
                detail="One or more courses are unavailable",
            ) from exc

        EVENTS_RECEIVED.inc(len(result.event_ids))
        if result.newly_enrolled_product_ids:
            cache.delete_prefix(f"profile:{user.id}")
            cache.delete_prefix(f"recommendation:{user.id}")
        try:
            rewards_attributed = attribute_event_rewards(db, user.id, result.event_ids)
        except Exception as exc:  # noqa: BLE001 - purchase access must survive optional attribution work
            db.rollback()
            rewards_attributed = 0
            logger.exception(
                "purchase_reward_attribution_failed",
                user_id=user.id,
                error_type=type(exc).__name__,
            )
        try:
            owned_ids = purchased_product_ids(db, user.id)
            actions_cancelled = cancel_owned_recommendation_actions(db, user.id, owned_ids)
            if actions_cancelled:
                db.commit()
                logger.info(
                    "owned_recommendation_actions_cancelled",
                    user_id=user.id,
                    action_count=actions_cancelled,
                )
        except Exception as exc:  # noqa: BLE001 - purchase access must survive derived-action cleanup
            db.rollback()
            logger.exception(
                "owned_recommendation_action_cleanup_failed",
                user_id=user.id,
                error_type=type(exc).__name__,
            )
        try:
            profile_version = aggregate_profile(db, user.id).get("profile_version")
        except Exception as exc:  # noqa: BLE001 - durable purchase already committed; refresh can retry later
            db.rollback()
            profile_version = None
            logger.exception(
                "purchase_profile_refresh_failed",
                user_id=user.id,
                error_type=type(exc).__name__,
            )

        return DemoPurchaseOut(
            purchased_product_ids=result.purchased_product_ids,
            enrolled_product_ids=result.enrolled_product_ids,
            newly_enrolled_product_ids=result.newly_enrolled_product_ids,
            already_enrolled_product_ids=result.already_enrolled_product_ids,
            event_ids=result.event_ids,
            profile_version=profile_version,
            rewards_attributed=rewards_attributed,
            offers=[DemoPurchaseOfferOut(
                product_id=offer.product_id,
                is_bundle=offer.is_bundle,
                eligible=offer.eligible,
                eligibility_reason=offer.eligibility_reason,
                catalog_price=offer.catalog_price,
                full_standalone_subtotal=offer.full_standalone_subtotal,
                owned_standalone_subtotal=offer.owned_standalone_subtotal,
                remaining_standalone_subtotal=offer.remaining_standalone_subtotal,
                ownership_credit=offer.ownership_credit,
                personalized_price=offer.personalized_price,
                savings=offer.savings,
                discount_rate=offer.discount_rate,
                discount_percent=offer.discount_percent,
                owned_component_ids=[
                    component.product_id for component in offer.owned_components
                ],
                remaining_component_ids=[
                    component.product_id for component in offer.remaining_components
                ],
            ) for offer in result.offers],
            catalog_total=result.catalog_total,
            ownership_credit_total=result.ownership_credit_total,
            payable_total=result.payable_total,
            savings_total=result.savings_total,
        )

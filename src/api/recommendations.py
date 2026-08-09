import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.dependencies import CurrentUser, DbSession
from src.models import MarketSignal, NextBestAction, NotificationPreference, Product
from src.repositories.enrollments import purchased_product_ids
from src.services.behavior_service import aggregate_profile, correct_profile
from src.services.bundle_pricing_service import (
    PersonalizedProductOffer,
    calculate_personalized_offer,
)
from src.services.cache_service import cache
from src.services.ownership_service import is_product_recommendable
from src.services.reranking_service import rerank
from src.services.retrieval_service import RetrievalService, build_retrieval_query
from src.services.reward_service import record_direct_action_feedback
from src.services.skill_taxonomy import SKILL_BUNDLES

router = APIRouter(prefix="/api", tags=["recommendations"])

TERMINAL_ACTION_STATUSES = {
    "dismissed", "converted", "expired", "cancelled", "failed", "delivered",
}


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _format_money(value: Decimal) -> str:
    return f"{value:,.0f}" if value == value.to_integral() else f"{value:,.2f}"


def _action_is_live(decision: NextBestAction, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if decision.status in TERMINAL_ACTION_STATUSES:
        return False
    if _aware_utc(decision.expires_at) <= now:
        return False
    recommendation = decision.recommendation
    if recommendation:
        if recommendation.status != "active":
            return False
        if _aware_utc(recommendation.expires_at) <= now:
            return False
    return True


def _personalized_offers(
    db: Session | None,
    products: Iterable[Product],
    purchased_ids: set[str],
) -> Mapping[str, PersonalizedProductOffer]:
    products = list(products)
    if not db or not products:
        return {}
    component_ids = {
        component_id
        for product in products
        if product.is_bundle
        for component_id in (product.bundled_product_ids or [])
    }
    components_by_id = {
        component.id: component
        for component in db.scalars(select(Product).where(Product.id.in_(component_ids)))
    } if component_ids else {}
    offers: dict[str, PersonalizedProductOffer] = {}
    for product in products:
        try:
            offers[product.id] = calculate_personalized_offer(
                product,
                (
                    components_by_id[component_id]
                    for component_id in (product.bundled_product_ids or [])
                    if component_id in components_by_id
                ),
                purchased_ids,
            )
        except ValueError:
            continue
    return offers


def _serialized_product(
    product: Product,
    offer: PersonalizedProductOffer | None = None,
) -> dict:
    display_price = offer.personalized_price if offer else product.price
    return {
        "id": product.id,
        "title": product.title,
        "slug": product.slug,
        "description": product.description,
        "category": product.category,
        "difficulty": product.difficulty,
        "price": float(display_price),
        "catalog_price": float(offer.catalog_price) if offer else float(product.price),
        "original_price": (
            float(offer.remaining_standalone_subtotal)
            if offer and offer.is_bundle
            else float(product.original_price) if product.original_price else None
        ),
        "savings_percent": offer.discount_percent if offer else product.savings_percent,
        "duration_hours": product.duration_hours,
        "skill_bundle": product.skill_bundle,
        "skills": product.skills,
        "image_url": product.image_url,
        "is_bundle": product.is_bundle,
        "bundle_size": len(product.bundled_product_ids or []),
        "bundled_product_ids": product.bundled_product_ids or [],
        "career_outcomes": product.career_outcomes or [],
        "ownership_credit": float(offer.ownership_credit) if offer else 0,
        "owned_component_ids": [
            component.product_id for component in offer.owned_components
        ] if offer else [],
        "owned_component_count": len(offer.owned_components) if offer else 0,
        "remaining_component_ids": [
            component.product_id for component in offer.remaining_components
        ] if offer else list(product.bundled_product_ids or []),
        "remaining_component_count": len(offer.remaining_components) if offer else (
            len(product.bundled_product_ids or []) if product.is_bundle else 0
        ),
        "remaining_standalone_subtotal": (
            float(offer.remaining_standalone_subtotal) if offer else None
        ),
        "personalized_savings": float(offer.savings) if offer else None,
        "personalized_bundle_price": bool(
            offer and offer.is_bundle and offer.has_ownership_credit and offer.eligible
        ),
    }


class ProfileCorrection(BaseModel):
    primary_goal: str | None = Field(None, max_length=160)
    difficulty_preference: str | None = None
    budget_max: float | None = Field(None, ge=0, le=1_000_000)
    weekly_hours: float | None = Field(None, ge=1, le=80)
    emerging_interests: list[str] | None = Field(None, max_length=8)

    @field_validator("emerging_interests")
    @classmethod
    def managed_interests(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(item not in SKILL_BUNDLES for item in value):
            raise ValueError("emerging interests must use managed skill bundles")
        return value


class SimulationInput(BaseModel):
    career_goal: str = Field(max_length=160)
    budget_max: float = Field(ge=0, le=1_000_000)
    weekly_hours: float = Field(ge=1, le=80)
    difficulty: str
    exploration: float = Field(ge=0, le=1)
    mastery: float = Field(ge=0, le=1)


class PrivacyPreferences(BaseModel):
    personalization_enabled: bool
    email_digest_enabled: bool = False
    preferred_hour: int = Field(17, ge=0, le=23)
    timezone: str | None = Field(None, min_length=1, max_length=80)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        timezone_name = value.strip()
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return timezone_name


class ActionFeedback(BaseModel):
    outcome: Literal[
        "impression", "clicked", "saved", "added_to_cart", "purchased", "dismissed", "not_interested"
    ]
    event_id: str | None = Field(None, min_length=8, max_length=100)


def serialize_next_best_action(
    decision,
    db: Session | None = None,
    *,
    excluded_product_ids: set[str] | None = None,
    purchased_product_ids: set[str] | None = None,
) -> dict | None:
    if not decision or not _action_is_live(decision):
        return None
    product = decision.product
    excluded = excluded_product_ids or set()
    purchased = purchased_product_ids if purchased_product_ids is not None else excluded
    if product and (
        product.id in excluded or not is_product_recommendable(product, purchased)
    ):
        return None
    offer = (
        _personalized_offers(db, [product], purchased).get(product.id)
        if product else None
    )
    if product and product.is_bundle and db and (not offer or not offer.eligible):
        return None
    signal_by_id = {}
    if db and decision.market_signal_ids:
        signal_by_id = {
            signal.id: signal for signal in db.scalars(
                select(MarketSignal).where(MarketSignal.id.in_(decision.market_signal_ids))
            )
        }
    cta_labels = {
        "offer_bundle": "Add bundle to cart",
        "cheaper_alternative": "Choose lower-cost course",
        "prerequisite_first": "Start prerequisite",
        "legitimate_urgency": "Review verified offer",
    }
    bundle_context = (decision.policy_context or {}).get("bundle_optimizer") or {}
    action_message = decision.message
    if (
        offer
        and offer.is_bundle
        and offer.has_ownership_credit
        and "already own" not in action_message.casefold()
    ):
        owned_count = len(offer.owned_components)
        action_message = (
            f"{action_message.rstrip()} You already own {owned_count} included course"
            f"{'s' if owned_count != 1 else ''}, so ₹{_format_money(offer.ownership_credit)} "
            f"is credited and you pay ₹{_format_money(offer.personalized_price)} "
            "for the remaining track."
        )
    value_comparison = ({
        "interested_components": bundle_context.get("interested_components"),
        "component_count": bundle_context.get("component_count"),
        "interest_coverage": bundle_context.get("interest_coverage"),
        "discount_percent": bundle_context.get("discount_percent"),
        "catalog_price": float(offer.catalog_price) if offer else bundle_context.get("catalog_price"),
        "personalized_price": float(offer.personalized_price) if offer else bundle_context.get("personalized_price"),
        "ownership_credit": float(offer.ownership_credit) if offer else bundle_context.get("ownership_credit"),
        "owned_component_count": len(offer.owned_components) if offer else bundle_context.get("owned_component_count", 0),
        "remaining_component_count": len(offer.remaining_components) if offer else bundle_context.get("remaining_component_count"),
    } if bundle_context or (offer and offer.is_bundle) else None)
    return {
        "id": decision.id,
        "action_type": decision.action_type,
        "persuasion_strategy": decision.persuasion_strategy,
        "headline": decision.headline,
        "message": action_message,
        "rationale": decision.rationale,
        "intent_stage": decision.intent_stage,
        "purchase_propensity": decision.purchase_propensity,
        "cta_label": (
            "Complete this bundle"
            if offer and offer.is_bundle and offer.has_ownership_credit
            else cta_labels.get(decision.action_type, "Add to cart")
        ),
        "status": decision.status,
        "is_fallback": False,
        "feedback_enabled": True,
        "deliver_at": decision.deliver_at,
        "expires_at": decision.expires_at,
        "evidence_event_ids": decision.evidence_event_ids,
        "market_signals": [{
            "claim": signal_by_id[signal_id].claim,
            "source_name": signal_by_id[signal_id].source_name,
            "source_url": signal_by_id[signal_id].source_url,
        } for signal_id in decision.market_signal_ids if signal_id in signal_by_id],
        "value_comparison": value_comparison,
        "product": _serialized_product(product, offer) if product else None,
    }


def _serialized_item(
    product: Product,
    *,
    rank: int,
    match_score: int,
    reason: str,
    evidence_event_ids: list[str],
    score_breakdown: dict,
    offer: PersonalizedProductOffer | None = None,
) -> dict:
    display_price = offer.personalized_price if offer else product.price
    return {
        "product_id": product.id,
        "title": product.title,
        "slug": product.slug,
        "image_url": product.image_url,
        "category": product.category,
        "skill_bundle": product.skill_bundle,
        "difficulty": product.difficulty,
        "price": float(display_price),
        "catalog_price": float(offer.catalog_price) if offer else float(product.price),
        "original_price": (
            float(offer.remaining_standalone_subtotal)
            if offer and offer.is_bundle
            else float(product.original_price) if product.original_price else None
        ),
        "savings_percent": offer.discount_percent if offer else product.savings_percent,
        "skills": product.skills,
        "is_bundle": product.is_bundle,
        "bundle_size": len(product.bundled_product_ids or []),
        "rank": rank,
        "match_score": match_score,
        "reason": reason,
        "evidence_event_ids": evidence_event_ids,
        "score_breakdown": score_breakdown,
        "ownership_credit": float(offer.ownership_credit) if offer else 0,
        "owned_component_ids": [
            component.product_id for component in offer.owned_components
        ] if offer else [],
        "owned_component_count": len(offer.owned_components) if offer else 0,
        "remaining_component_ids": [
            component.product_id for component in offer.remaining_components
        ] if offer else list(product.bundled_product_ids or []),
        "remaining_component_count": len(offer.remaining_components) if offer else (
            len(product.bundled_product_ids or []) if product.is_bundle else 0
        ),
        "remaining_standalone_subtotal": (
            float(offer.remaining_standalone_subtotal) if offer else None
        ),
        "personalized_savings": float(offer.savings) if offer else None,
        "personalized_bundle_price": bool(
            offer and offer.is_bundle and offer.has_ownership_credit and offer.eligible
        ),
    }


def _related_replacement_items(
    db: Session,
    profile: dict,
    excluded_product_ids: set[str],
    existing_product_ids: set[str],
    count: int,
    start_rank: int,
) -> list[dict]:
    if count <= 0:
        return []
    blocked = (
        excluded_product_ids
        | existing_product_ids
        | set(profile.get("excluded_product_ids", []))
    )
    products = list(db.scalars(select(Product).where(
        Product.is_active.is_(True),
        Product.is_bundle.is_(False),
    )))
    candidates = RetrievalService._lexical_fallback(
        build_retrieval_query(profile),
        products,
        blocked,
        set(profile.get("purchased_product_ids", [])) | excluded_product_ids,
    )
    ranked = rerank(candidates, profile, limit=count, db=db)
    evidence = list(profile.get("evidence_event_ids", []))[:10]
    replacements: list[dict] = []
    for position, item in enumerate(ranked, start=1):
        product = item.candidate.product
        skill_names = list(product.skills or [])[:2] or [product.skill_bundle]
        replacements.append(_serialized_item(
            product,
            rank=start_rank + position,
            match_score=round(item.final_score * 100),
            reason=(
                f"A related next step for your {product.skill_bundle} direction, "
                f"building {', '.join(skill_names)}."
            ),
            evidence_event_ids=evidence,
            score_breakdown=item.breakdown,
        ))
    return replacements


def _ownership_safe_recommendation_copy(
    items: list[dict],
    profile: dict | None,
) -> tuple[str, str]:
    """Build copy only from products that survived the live ownership guard."""
    if not items:
        return (
            "Explore a related next step",
            "Your current catalog and learning profile will be used to surface another active course.",
        )
    lead = items[0]
    skills = list(lead.get("skills") or [])[:2]
    skill_text = ", ".join(skills) if skills else lead["skill_bundle"]
    direction = lead["skill_bundle"]
    if profile:
        interests = list(profile.get("emerging_interests") or [])
        if direction in interests:
            direction = interests[0]
    return (
        f"Continue with {lead['title']}",
        (
            f"Based on your current {direction} direction, {lead['title']} is a related "
            f"{lead['difficulty']} next step that develops {skill_text}."
        ),
    )


def serialize_recommendation(
    recommendation,
    db: Session | None = None,
    *,
    excluded_product_ids: set[str] | None = None,
    purchased_product_ids: set[str] | None = None,
    profile: dict | None = None,
    minimum_standalone: int = 0,
) -> dict:
    excluded = excluded_product_ids or set()
    purchased = (
        purchased_product_ids
        if purchased_product_ids is not None
        else set(profile.get("purchased_product_ids", []))
        if profile and "purchased_product_ids" in profile
        else excluded
    )
    source_items = list(recommendation.items)
    eligible_source_items = [
        item for item in source_items
        if item.product_id not in excluded
        and is_product_recommendable(item.product, purchased)
    ]
    offers = _personalized_offers(
        db,
        [item.product for item in eligible_source_items],
        purchased,
    )
    items = []
    for item in eligible_source_items:
        offer = offers.get(item.product_id)
        if db and item.product.is_bundle and (not offer or not offer.eligible):
            continue
        reason = item.reason
        if offer and offer.is_bundle and offer.has_ownership_credit:
            reason = (
                f"{reason.rstrip()} Your {len(offer.owned_components)} owned course"
                f"{'s' if len(offer.owned_components) != 1 else ''} increase this track's fit, "
                f"and ₹{_format_money(offer.ownership_credit)} is deducted from the bundle price."
            )
        items.append(_serialized_item(
            item.product,
            rank=item.rank,
            match_score=round(item.final_score * 100),
            reason=reason,
            evidence_event_ids=item.evidence_event_ids,
            score_breakdown=item.score_breakdown,
            offer=offer,
        ))
    filtered_item_count = len(source_items) - len(items)
    standalone_count = sum(not item["is_bundle"] for item in items)
    if db and profile and standalone_count < minimum_standalone:
        items.extend(_related_replacement_items(
            db,
            profile,
            excluded,
            {item["product_id"] for item in items},
            minimum_standalone - standalone_count,
            max((item["rank"] for item in items), default=0),
        ))
    serialized_action = serialize_next_best_action(
        recommendation.next_best_action,
        db,
        excluded_product_ids=excluded,
        purchased_product_ids=purchased,
    )
    ownership_filtered = (
        filtered_item_count > 0
        or (recommendation.next_best_action is not None and serialized_action is None)
    )
    headline = recommendation.headline
    narrative = recommendation.narrative
    if ownership_filtered:
        headline, narrative = _ownership_safe_recommendation_copy(items, profile)
    return {
        "id": recommendation.id,
        "headline": headline,
        "narrative": narrative,
        "confidence": recommendation.confidence,
        "degraded": recommendation.degraded,
        "expires_at": recommendation.expires_at,
        "next_best_action": serialized_action,
        "items": items,
    }


def _related_action_fallback(
    decision: NextBestAction,
    db: Session,
    profile: dict,
    owned_product_ids: set[str],
) -> dict | None:
    recommendation = decision.recommendation
    if not recommendation:
        return None
    fallback_exclusions = set(owned_product_ids)
    if decision.product_id:
        fallback_exclusions.add(decision.product_id)
    fallback_profile = {
        **profile,
        "excluded_product_ids": sorted(
            set(profile.get("excluded_product_ids", [])) | fallback_exclusions
        ),
        "purchased_product_ids": sorted(owned_product_ids),
    }
    recommendation_payload = serialize_recommendation(
        recommendation,
        db,
        excluded_product_ids=fallback_exclusions,
        purchased_product_ids=owned_product_ids,
        profile=fallback_profile,
        minimum_standalone=1,
    )
    if not recommendation_payload["items"]:
        return None
    item = recommendation_payload["items"][0]
    product = db.get(Product, item["product_id"])
    if not is_product_recommendable(product, owned_product_ids):
        return None
    offer = _personalized_offers(db, [product], owned_product_ids).get(product.id)
    if product.is_bundle and (not offer or not offer.eligible):
        return None
    headline, message = _ownership_safe_recommendation_copy([item], fallback_profile)
    action_type = "offer_bundle" if product.is_bundle else "recommend_course"
    return {
        "id": None,
        "source_decision_id": decision.id,
        "action_type": action_type,
        "persuasion_strategy": "skill_gap",
        "headline": headline,
        "message": message,
        "rationale": (
            "Selected from the current learning profile and stored related candidates "
            "after applying live ownership and availability checks."
        ),
        "intent_stage": decision.intent_stage,
        "purchase_propensity": float(profile.get("confidence", 0.5)),
        "cta_label": "Add bundle to cart" if product.is_bundle else "Add to cart",
        "status": "fallback",
        "is_fallback": True,
        "feedback_enabled": False,
        "deliver_at": None,
        "expires_at": None,
        "evidence_event_ids": list(profile.get("evidence_event_ids", []))[:10],
        "market_signals": [],
        "value_comparison": None,
        "product": _serialized_product(product, offer),
    }


@router.post("/recommendations/generate")
def generate_recommendation(request: Request, user: CurrentUser, db: DbSession):
    recommendation = request.app.state.recommendations.generate(db, user.id, "user_refresh")
    profile = aggregate_profile(db, user.id)
    owned = (
        purchased_product_ids(db, user.id)
        | set(profile.get("purchased_product_ids", []))
    )
    return serialize_recommendation(
        recommendation,
        db,
        excluded_product_ids=owned,
        purchased_product_ids=owned,
        profile=profile,
        minimum_standalone=3,
    )


@router.get("/next-best-action")
def current_next_best_action(user: CurrentUser, db: DbSession):
    decision = db.scalar(select(NextBestAction).where(
        NextBestAction.user_id == user.id,
    ).order_by(NextBestAction.created_at.desc()))
    if not decision:
        return None
    profile = aggregate_profile(db, user.id)
    owned = purchased_product_ids(db, user.id)
    product_is_eligible = (
        decision.product is None
        or is_product_recommendable(decision.product, owned)
    )
    if _action_is_live(decision) and product_is_eligible:
        return serialize_next_best_action(
            decision,
            db,
            excluded_product_ids=owned,
            purchased_product_ids=owned,
        )
    if decision.status in {"dismissed", "failed"}:
        return None
    if decision.status == "cancelled" and product_is_eligible:
        return None
    return _related_action_fallback(decision, db, profile, owned)


@router.post("/next-best-action/{decision_id}/feedback")
def next_best_action_feedback(
    decision_id: str, payload: ActionFeedback, user: CurrentUser, db: DbSession
):
    reward = record_direct_action_feedback(
        db, user.id, decision_id, payload.outcome, payload.event_id or f"nba-{uuid.uuid4()}"
    )
    if not reward:
        raise HTTPException(404, "Next-best action not found")
    cache.delete_prefix(f"profile:{user.id}")
    cache.delete_prefix(f"recommendation:{user.id}")
    return {"status": "recorded", "reward": reward.reward, "event_id": reward.event_id}


@router.get("/journey-twin")
def journey_twin(user: CurrentUser, db: DbSession):
    return aggregate_profile(db, user.id)


@router.put("/journey-twin")
def update_journey_twin(payload: ProfileCorrection, request: Request, user: CurrentUser, db: DbSession):
    profile = correct_profile(db, user.id, payload.model_dump(exclude_none=True))
    recommendation = request.app.state.recommendations.generate(db, user.id, "profile_correction", force=True)
    return {
        "profile": profile,
        "recommendation": serialize_recommendation(
            recommendation,
            db,
            excluded_product_ids=set(profile.get("purchased_product_ids", [])),
            purchased_product_ids=set(profile.get("purchased_product_ids", [])),
            profile=profile,
            minimum_standalone=3,
        ),
    }


@router.post("/recommendations/simulate")
def simulate(payload: SimulationInput, request: Request, user: CurrentUser, db: DbSession):
    profile = aggregate_profile(db, user.id, persist=False)
    profile.update({"primary_goal": payload.career_goal, "budget_max": payload.budget_max,
                    "weekly_hours": payload.weekly_hours, "difficulty_preference": payload.difficulty})
    query, candidates = request.app.state.retrieval.preview(db, profile)
    from src.services.reranking_service import rerank
    ranked = rerank(candidates, profile, limit=5, db=db)
    return {"simulated": True, "query": query, "retrieval_mode": "sql_preview", "degraded": False,
            "items": [{"product_id": item.candidate.product.id, "title": item.candidate.product.title,
                       "score": round(item.final_score * 100), "breakdown": item.breakdown} for item in ranked]}


@router.put("/preferences")
def update_preferences(payload: PrivacyPreferences, user: CurrentUser, db: DbSession):
    user.personalization_enabled = payload.personalization_enabled
    preference = db.get(NotificationPreference, user.id)
    if not preference:
        preference = NotificationPreference(user_id=user.id)
        db.add(preference)
    preference.email_enabled = payload.email_digest_enabled
    preference.preferred_hour = payload.preferred_hour
    if payload.timezone is not None:
        preference.timezone = payload.timezone
    db.commit()
    return {"personalization_enabled": user.personalization_enabled,
            "email_digest_enabled": preference.email_enabled,
            "preferred_hour": preference.preferred_hour,
            "timezone": preference.timezone}

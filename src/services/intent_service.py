import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import Event, Product

PROPENSITY_VERSION = "propensity-v1"


@dataclass(frozen=True)
class IntentAssessment:
    score: float
    purchase_propensity: float
    stage: str
    price_sensitivity: float
    speed_priority: float
    career_orientation: float
    recommendation_fatigue: float
    strongest_product_ids: list[str]
    evidence_event_ids: list[str]
    factors: dict[str, float]
    model_version: str = PROPENSITY_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


POSITIVE_WEIGHTS = {
    "card_impression": .02,
    "card_hover": .08,
    "product_view": .18,
    "search": .22,
    "search_click": .32,
    "time_spent": .25,
    "wishlist_add": .70,
    "cta_click": .95,
    "recommendation_click": .50,
    "enrollment": 1.20,
}
NEGATIVE_WEIGHTS = {
    "wishlist_remove": -.35,
    "recommendation_dismiss": -.75,
    "not_interested": -1.10,
}


def _clamp(value: float, low: float = 0, high: float = 1) -> float:
    return max(low, min(high, value))


def _decay(occurred_at: datetime, now: datetime) -> float:
    when = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
    age_days = max(0, (now - when).total_seconds() / 86400)
    return math.exp(-math.log(2) * age_days / 10)


def assess_purchase_intent(db: Session, user_id: str, profile: dict, now: datetime | None = None) -> IntentAssessment:
    """Transparent propensity model over recent behavioral evidence.

    This deliberately estimates likelihood rather than claiming causal uplift. The
    policy stores every factor so later rewards can be used to recalibrate it.
    """
    now = now or datetime.now(timezone.utc)
    events = list(db.scalars(select(Event).where(
        Event.user_id == user_id,
        Event.occurred_at >= now - timedelta(days=45),
    ).order_by(Event.occurred_at.desc()).limit(500)))
    products = {product.id: product for product in db.scalars(select(Product))}
    raw_intent = 0.0
    product_scores: dict[str, float] = {}
    evidence_scores: list[tuple[str, float]] = []
    counts: dict[str, int] = {}
    price_filter_count = 0
    short_path_terms = 0
    exposure_fatigue = 0.0
    negative_fatigue = 0.0
    cart_add_count = 0

    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1
        contribution = POSITIVE_WEIGHTS.get(event.event_type, NEGATIVE_WEIGHTS.get(event.event_type, 0.0))
        if event.event_type == "cta_click" and event.event_metadata.get("source") == "remove_from_cart":
            contribution = -.25
        elif event.event_type == "cta_click":
            cart_add_count += 1
        if event.event_type == "time_spent":
            dwell = min(float(event.event_metadata.get("dwell_seconds", 0)), 600)
            contribution *= _clamp(dwell / 120, .15, 2.5)
        event_decay = _decay(event.occurred_at, now)
        contribution *= event_decay
        raw_intent += contribution
        if event.product_id:
            product_scores[event.product_id] = product_scores.get(event.product_id, 0) + contribution
        if abs(contribution) >= .15:
            evidence_scores.append((event.event_id, abs(contribution)))
        if event.event_type == "price_filter":
            price_filter_count += 1
        if event.event_type == "recommendation_impression":
            exposure_fatigue += .08 * event_decay
        elif event.event_type in {"recommendation_dismiss", "not_interested"}:
            negative_fatigue += .28 * event_decay
        query = (event.search_query or "").lower()
        if any(term in query for term in ("fast", "quick", "short", "crash course", "weekend")):
            short_path_terms += 1

    repeated_interest = sum(1 for score in product_scores.values() if score >= .45)
    diversity_bonus = min(.35, repeated_interest * .07)
    raw_intent += diversity_bonus
    negative_events = counts.get("recommendation_dismiss", 0) + counts.get("not_interested", 0)
    fatigue = _clamp(exposure_fatigue + negative_fatigue)
    raw_intent -= fatigue * 1.15

    # Logistic calibration produces a stable 0..1 probability-like score. It is
    # versioned and logged as a policy input, not presented as a guarantee.
    propensity = 1 / (1 + math.exp(-(raw_intent - 1.55)))
    propensity = _clamp(propensity)
    intent_score = _clamp(raw_intent / 4.0)
    if propensity >= .72:
        stage = "purchase_ready"
    elif propensity >= .48:
        stage = "high_intent"
    elif propensity >= .27:
        stage = "considering"
    else:
        stage = "exploring"

    interacted_prices = [float(products[pid].price) for pid in product_scores if pid in products and product_scores[pid] > 0]
    average_price = sum(interacted_prices) / len(interacted_prices) if interacted_prices else 0
    budget = float(profile.get("budget_max", 5000) or 5000)
    price_sensitivity = _clamp(
        price_filter_count * .22
        + (.28 if budget <= 3500 else .12 if budget <= 5500 else 0)
        + (.20 if average_price and average_price > budget else 0)
    )
    weekly_hours = float(profile.get("weekly_hours", 8) or 8)
    speed_priority = _clamp((.65 if weekly_hours <= 4 else .35 if weekly_hours <= 7 else .15) + short_path_terms * .2)
    goal_text = " ".join([str(profile.get("primary_goal", "")), *profile.get("emerging_interests", [])]).lower()
    career_orientation = _clamp(
        (.72 if any(term in goal_text for term in ("career", "job", "professional", "engineer", "promotion")) else .28)
        + (.12 if any(term in goal_text for term in ("ai", "data", "machine learning")) else 0)
    )
    strongest = [pid for pid, score in sorted(product_scores.items(), key=lambda item: item[1], reverse=True) if score > 0][:6]
    evidence = [event_id for event_id, _ in sorted(evidence_scores, key=lambda item: item[1], reverse=True)][:12]
    return IntentAssessment(
        score=round(intent_score, 4),
        purchase_propensity=round(propensity, 4),
        stage=stage,
        price_sensitivity=round(price_sensitivity, 4),
        speed_priority=round(speed_priority, 4),
        career_orientation=round(career_orientation, 4),
        recommendation_fatigue=round(fatigue, 4),
        strongest_product_ids=strongest,
        evidence_event_ids=evidence,
        factors={
            "raw_intent": round(raw_intent, 4),
            "high_intent_products": repeated_interest,
            "wishlist_adds": counts.get("wishlist_add", 0),
            "cart_actions": cart_add_count,
            "searches": counts.get("search", 0),
            "meaningful_views": counts.get("product_view", 0) + counts.get("time_spent", 0),
            "negative_feedback": negative_events,
        },
    )

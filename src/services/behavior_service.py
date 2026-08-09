import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import BehaviorProfile, Product
from src.repositories.enrollments import purchased_product_ids
from src.repositories.events import user_events
from src.services.cache_service import cache
from src.services.skill_taxonomy import canonical_skills, infer_skill_bundles

EVENT_WEIGHTS = {
    "card_impression": 0.1, "card_hover": 0.4, "product_view": 1.5, "search": 1.8,
    "category_filter": 1.3, "difficulty_filter": .45, "price_filter": .65,
    "search_click": 2.0, "wishlist_add": 4.0, "wishlist_remove": -2.5, "cta_click": 3.0,
    "enrollment": 7.0, "recommendation_click": 2.5, "recommendation_dismiss": -4.0,
    "not_interested": -6.0, "time_spent": 1.0,
}


def recency_decay(occurred_at: datetime, now: datetime | None = None, half_life_days: float = 14) -> float:
    now = now or datetime.now(timezone.utc)
    when = occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc)
    age_days = max(0, (now - when).total_seconds() / 86400)
    return math.exp(-math.log(2) * age_days / half_life_days)


def _profile_hash(data: dict) -> str:
    def stable(value):
        if isinstance(value, float):
            return round(value, 2)
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    return hashlib.sha256(json.dumps(stable(data), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def aggregate_profile(db: Session, user_id: str, persist: bool = True) -> dict:
    durable_purchased: set[str] | None = None
    profile_cache_key = f"profile:{user_id}"
    ownership_cache_key = f"{profile_cache_key}:durable-purchases"
    if persist:
        cached = cache.get(profile_cache_key)
        if cached is not None:
            # The profile cache is process-local, while entitlements are durable
            # database state. Another cloud worker can complete a purchase without
            # being able to invalidate this worker's cache, so verify the cheap,
            # indexed ownership projection before reusing a cached profile.
            durable_purchased = purchased_product_ids(db, user_id)
            cached_durable = cache.get(ownership_cache_key)
            if cached_durable is not None and set(cached_durable) == durable_purchased:
                return cached
    events = user_events(db, user_id)
    products = {p.id: p for p in db.scalars(select(Product))}
    skill_bundles: defaultdict[str, float] = defaultdict(float)
    skills: defaultdict[str, float] = defaultdict(float)
    difficulties: defaultdict[str, float] = defaultdict(float)
    prices: list[float] = []
    dismissed: set[str] = set()
    completed: set[str] = set()
    purchased = set(
        durable_purchased
        if durable_purchased is not None
        else purchased_product_ids(db, user_id)
    )
    durable_purchased = set(purchased)
    saved: set[str] = set()
    cart: set[str] = set()
    evidence: list[str] = []
    signals: list[str] = []

    for event in events:
        weight = EVENT_WEIGHTS.get(event.event_type, 0.25) * recency_decay(event.occurred_at)
        product = products.get(event.product_id or "")
        if event.event_type == "time_spent":
            dwell = min(float(event.event_metadata.get("dwell_seconds", 0)), 600)
            weight *= min(dwell / 90, 3)
        if product:
            skill_bundles[product.skill_bundle] += weight
            difficulties[product.difficulty] += max(weight, 0)
            for skill in product.skills:
                skills[skill] += weight
            observed_price = float(product.price)
            if event.event_type == "enrollment":
                pricing = event.event_metadata.get("pricing")
                if isinstance(pricing, dict):
                    try:
                        observed_price = max(0, float(pricing.get("payable", observed_price)))
                    except (TypeError, ValueError):
                        pass
            prices.append(observed_price)
            if weight >= 1.5:
                evidence.append(event.event_id)
            if event.event_type in {"product_view", "wishlist_add", "enrollment", "time_spent"}:
                signals.append(f"{event.event_type.replace('_', ' ')}: {product.title}")
        if event.event_type == "search" and event.search_query:
            matched_bundles = infer_skill_bundles(event.search_query)
            for position, bundle in enumerate(matched_bundles):
                adjusted_weight = weight / (position + 1)
                skill_bundles[bundle] += adjusted_weight
                for skill in canonical_skills(bundle):
                    skills[skill] += adjusted_weight * .6
            if matched_bundles:
                evidence.append(event.event_id)
            signals.append(f"searched for {event.search_query}")
        if event.event_type == "category_filter" and event.event_metadata.get("category"):
            category = str(event.event_metadata["category"])
            matched_bundles = infer_skill_bundles(category)
            for bundle in matched_bundles:
                skill_bundles[bundle] += weight
                for skill in canonical_skills(bundle):
                    skills[skill] += weight * .45
            if matched_bundles:
                evidence.append(event.event_id)
                signals.append(f"filtered for {category}")
        if event.event_type == "difficulty_filter" and event.event_metadata.get("difficulty"):
            difficulties[str(event.event_metadata["difficulty"])] += max(weight, 0)
        if event.product_id:
            if event.event_type in {"recommendation_dismiss", "not_interested"}:
                dismissed.add(event.product_id)
            elif event.event_type == "enrollment":
                # A purchase is a strong intent signal, but it does not mean the
                # learner has completed the course. Durable access is stored in
                # user_enrollments; legacy enrollment events remain compatible.
                purchased.add(event.product_id)
            elif event.event_type == "wishlist_add":
                saved.add(event.product_id)
            elif event.event_type == "wishlist_remove":
                saved.discard(event.product_id)
            elif event.event_type == "cta_click":
                source = event.event_metadata.get("source")
                if source in {"add_to_cart", "journey_twin_add_to_cart"}:
                    cart.add(event.product_id)
                elif source == "remove_from_cart":
                    cart.discard(event.product_id)

    top_bundles = [k for k, v in sorted(skill_bundles.items(), key=lambda x: x[1], reverse=True) if v > 0][:4]
    top_skills = [k for k, v in sorted(skills.items(), key=lambda x: x[1], reverse=True) if v > 0][:5]
    difficulty = max(difficulties, key=difficulties.get) if difficulties else "intermediate"
    profile = {
        "primary_goal": f"Build practical {top_bundles[0]} skills" if top_bundles else "Explore a high-impact learning path",
        "emerging_interests": top_bundles or ["Data Science"],
        "skill_gaps": top_skills or ["Python", "Analytics"],
        "difficulty_preference": difficulty,
        "budget_max": round(max(prices) * 1.15, 2) if prices else 5000,
        "weekly_hours": 8,
        "high_intent_signals": signals[:5],
        "skill_bundle_scores": dict(skill_bundles),
        "category_scores": dict(skill_bundles),
        "skill_scores": dict(skills),
        "excluded_product_ids": sorted(dismissed | purchased | completed),
        "saved_product_ids": sorted(saved),
        "cart_product_ids": sorted(cart),
        "purchased_product_ids": sorted(purchased),
        "completed_product_ids": sorted(completed),
        "evidence_event_ids": evidence[:20],
        "confidence": min(0.96, 0.35 + len(evidence) * 0.06),
    }

    existing = db.scalar(select(BehaviorProfile).where(BehaviorProfile.user_id == user_id))
    corrections = existing.corrections if existing else {}
    profile.update({k: v for k, v in corrections.items() if v not in (None, "")})
    profile_hash = _profile_hash(profile)
    version = existing.version if existing else 0
    if not existing or existing.profile_hash != profile_hash:
        version += 1
    profile["profile_version"] = version
    profile["profile_hash"] = profile_hash

    if persist:
        timeline = list(existing.timeline if existing else [])
        if not existing or existing.profile_hash != profile_hash:
            timeline = ([{"version": version, "at": datetime.now(timezone.utc).isoformat(),
                          "interests": profile["emerging_interests"], "evidence": evidence[:3]}] + timeline)[:20]
        if not existing:
            existing = BehaviorProfile(user_id=user_id, profile_hash=profile_hash)
            db.add(existing)
        existing.version = version
        existing.profile_hash = profile_hash
        existing.profile_data = profile
        existing.evidence_event_ids = evidence[:20]
        existing.timeline = timeline
        db.commit()
        # The companion cache entry tracks only durable rows. Legacy enrollment
        # events remain in the public purchased union without forcing a rebuild
        # on every cache hit.
        cache.set(ownership_cache_key, sorted(durable_purchased), 120)
        cache.set(profile_cache_key, profile, 120)
    return profile


def correct_profile(db: Session, user_id: str, corrections: dict) -> dict:
    allowed = {"primary_goal", "difficulty_preference", "budget_max", "weekly_hours", "emerging_interests"}
    clean = {key: value for key, value in corrections.items() if key in allowed}
    stored = db.scalar(select(BehaviorProfile).where(BehaviorProfile.user_id == user_id))
    if not stored:
        aggregate_profile(db, user_id)
        stored = db.scalar(select(BehaviorProfile).where(BehaviorProfile.user_id == user_id))
    stored.corrections = {**stored.corrections, **clean}
    stored.profile_hash = "pending"
    db.commit()
    cache.delete_prefix(f"profile:{user_id}")
    cache.delete_prefix(f"recommendation:{user_id}")
    return aggregate_profile(db, user_id)


def should_trigger(profile: dict, recent_event_types: list[str], has_active_recommendation: bool) -> tuple[bool, str]:
    high_intent = {"search", "category_filter", "wishlist_add", "wishlist_remove", "enrollment", "cta_click",
                   "time_spent", "recommendation_dismiss", "not_interested", "profile_correction"}
    if any(event in high_intent for event in recent_event_types):
        return True, "high_intent"
    if len(profile.get("evidence_event_ids", [])) >= 3 and not has_active_recommendation:
        return True, "meaningful_activity_threshold"
    return False, "cooldown_or_insufficient_signal"

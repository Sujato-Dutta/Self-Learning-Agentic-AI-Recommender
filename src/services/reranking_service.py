import time
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import Product
from src.observability.metrics import RERANK_LATENCY
from src.services.bundle_pricing_service import (
    PersonalizedProductOffer,
    calculate_personalized_offer,
)
from src.services.ownership_service import is_product_recommendable
from src.services.retrieval_service import Candidate


@dataclass
class RankedCandidate:
    candidate: Candidate
    final_score: float
    breakdown: dict[str, float]


def score_candidate(
    candidate: Candidate,
    profile: dict,
    offer: PersonalizedProductOffer | None = None,
) -> RankedCandidate:
    product = candidate.product
    interests = {i.lower() for i in profile.get("emerging_interests", [])}
    gaps = {s.lower() for s in profile.get("skill_gaps", [])}
    bundle_name = product.skill_bundle.lower()
    category_fit = 1.0 if bundle_name in interests else max(
        [0.75 for interest in interests if interest in bundle_name or bundle_name in interest] or [0.35]
    )
    product_skills = {s.lower() for s in product.skills}
    skill_fit = len(gaps & product_skills) / max(len(gaps), 1) if gaps else 0.5
    preferred = profile.get("difficulty_preference", "intermediate")
    difficulty_fit = 1.0 if product.difficulty == preferred or product.difficulty == "all-levels" else 0.55
    budget = float(profile.get("budget_max", 10**9))
    effective_price = float(offer.personalized_price if offer else product.price)
    budget_fit = 1.0 if effective_price <= budget else max(
        0, 1 - (effective_price - budget) / max(budget, 1)
    )
    novelty = 0.9 if product.id not in profile.get("saved_product_ids", []) else 0.6
    owned = set(profile.get("purchased_product_ids", []))
    component_ids = set(product.bundled_product_ids or [])
    bundle_completion = (
        len(component_ids & owned) / len(component_ids)
        if product.is_bundle and component_ids and not component_ids.issubset(owned)
        else 0
    )
    breakdown = {
        "semantic": candidate.retrieval_score,
        "interest": category_fit,
        "skill_gap": skill_fit,
        "difficulty": difficulty_fit,
        "budget": budget_fit,
        "novelty": novelty,
        "popularity": product.popularity,
        "bundle_completion": bundle_completion,
    }
    weights = {"semantic": .32, "interest": .2, "skill_gap": .14, "difficulty": .1,
               "budget": .1, "novelty": .06, "popularity": .08}
    # Partial ownership is a high-quality completion signal: it makes the track
    # more relevant while its personalized price improves budget fit.  Keep the
    # boost bounded so relevance and user-value checks still dominate.
    final = min(
        1.0,
        sum(breakdown[key] * weights[key] for key in weights)
        + bundle_completion * .12,
    )
    return RankedCandidate(candidate, round(final, 5), breakdown)


def _candidate_offers(
    db: Session | None,
    candidates: list[Candidate],
    owned: set[str],
) -> Mapping[str, PersonalizedProductOffer]:
    if db is None:
        return {}
    products = [candidate.product for candidate in candidates]
    component_ids = {
        component_id
        for product in products
        if product.is_bundle
        for component_id in (product.bundled_product_ids or [])
    }
    components_by_id = {
        product.id: product
        for product in db.scalars(select(Product).where(Product.id.in_(component_ids)))
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
                owned,
            )
        except ValueError:
            # An unresolved bundle cannot be priced safely and is omitted below.
            continue
    return offers


def rerank(
    candidates: list[Candidate],
    profile: dict,
    limit: int = 5,
    *,
    db: Session | None = None,
) -> list[RankedCandidate]:
    started = time.perf_counter()
    owned = set(profile.get("purchased_product_ids", []))
    excluded = set(profile.get("excluded_product_ids", []))
    offers = _candidate_offers(db, candidates, owned)
    ranked = sorted(
        (
            score_candidate(candidate, profile, offers.get(candidate.product.id))
            for candidate in candidates
            if candidate.product.id not in excluded
            and is_product_recommendable(candidate.product, owned)
            and (db is None or not candidate.product.is_bundle
                 or candidate.product.id in offers)
        ),
        key=lambda candidate: candidate.final_score,
        reverse=True,
    )
    selected: list[RankedCandidate] = []
    category_counts: dict[str, int] = {}
    for item in ranked:
        category = item.candidate.product.skill_bundle
        if category_counts.get(category, 0) >= 2 and len({x.candidate.product.skill_bundle for x in ranked}) > 1:
            continue
        selected.append(item)
        category_counts[category] = category_counts.get(category, 0) + 1
        if len(selected) >= limit:
            break
    RERANK_LATENCY.observe(time.perf_counter() - started)
    return selected

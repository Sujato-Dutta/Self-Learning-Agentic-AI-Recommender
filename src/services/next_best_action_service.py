from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import MarketSignal, NextBestAction, NotificationPreference, Product
from src.services.bundle_pricing_service import (
    PersonalizedProductOffer,
    calculate_personalized_offer,
)
from src.services.intent_service import IntentAssessment
from src.services.ownership_service import is_product_recommendable
from src.services.reranking_service import RankedCandidate

POLICY_VERSION = "nba-v1"
PRODUCT_ACTIONS = {
    "recommend_course", "cheaper_alternative", "prerequisite_first", "offer_bundle",
    "show_social_proof", "career_outcome", "legitimate_urgency", "email_later",
}


@dataclass
class ActionDecision:
    action_type: str
    persuasion_strategy: str
    product: Product | None
    headline: str
    message: str
    rationale: str
    intent_stage: str
    purchase_propensity: float
    expected_conversion_probability: float = 0
    expected_revenue: Decimal = Decimal(0)
    incremental_expected_revenue: Decimal = Decimal(0)
    evidence_event_ids: list[str] = field(default_factory=list)
    market_signal_ids: list[str] = field(default_factory=list)
    deliver_at: datetime | None = None
    policy_context: dict[str, Any] = field(default_factory=dict)
    policy_version: str = POLICY_VERSION

    def as_state(self) -> dict[str, Any]:
        data = asdict(self)
        data["product"] = self.product
        return data


def _clamp(value: float, low: float = 0, high: float = 1) -> float:
    return max(low, min(high, value))


def _money(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


class NextBestActionPolicy:
    """Deterministic, auditable policy that decides before copy is generated."""

    def decide(
        self,
        db: Session,
        user_id: str,
        profile: dict,
        intent: IntentAssessment,
        ranked: list[RankedCandidate],
        now: datetime | None = None,
    ) -> ActionDecision:
        now = now or datetime.now(timezone.utc)
        purchased_product_ids = set(profile.get("purchased_product_ids", []))
        covered_product_ids = purchased_product_ids | set(
            profile.get("completed_product_ids", [])
        )
        # Treat ownership as a hard policy constraint as well as a retrieval
        # filter. This prevents stale or directly supplied candidate lists from
        # selecting a product the learner already owns or has fully covered.
        # A partially owned bundle remains eligible and is priced below.
        ranked = [
            item for item in ranked
            if is_product_recommendable(item.candidate.product, covered_product_ids)
        ]
        evidence = list(dict.fromkeys(intent.evidence_event_ids + profile.get("evidence_event_ids", [])))[:12]
        recent_decisions = list(db.scalars(select(NextBestAction).where(
            NextBestAction.user_id == user_id,
            NextBestAction.created_at >= now - timedelta(hours=24),
        )))
        context: dict[str, Any] = {
            "intent_model": intent.model_version,
            "intent_factors": intent.factors,
            "price_sensitivity": intent.price_sensitivity,
            "speed_priority": intent.speed_priority,
            "career_orientation": intent.career_orientation,
            "recommendation_fatigue": intent.recommendation_fatigue,
            "recent_decision_count": len(recent_decisions),
            "guardrails": ["catalog_only", "verified_prices", "no_unverified_urgency", "frequency_cap"],
        }

        if intent.recommendation_fatigue >= .72 or len(recent_decisions) >= 8 or (
            len(recent_decisions) >= 4 and not intent.factors.get("cart_actions") and not intent.factors.get("wishlist_adds")
        ):
            return self._no_product_decision(
                "show_nothing", "respectful_pause", intent, evidence, context,
                "No recommendation right now",
                "If I were in your position, I would keep learning without another sales prompt right now. Your recent recommendation exposure is high, so SmartReco will wait for a stronger signal.",
                "Frequency and fatigue guardrails suppressed a commercial action.",
            )

        preference = db.get(NotificationPreference, user_id)
        if intent.purchase_propensity < .24:
            if preference and preference.email_enabled and evidence:
                decision = self._no_product_decision(
                    "email_later", "low_pressure_follow_up", intent, evidence, context,
                    "I would revisit this later",
                    "Your direction is forming, but I would not interrupt your exploration yet. SmartReco can revisit the strongest fit in your preferred digest instead.",
                    "Intent is early and opted-in email provides a lower-pressure channel.",
                )
                decision.deliver_at = now + timedelta(hours=4)
                return decision
            return self._no_product_decision(
                "delay_recommendation", "exploration_first", intent, evidence, context,
                "I would explore a little more first",
                "There is not enough purchase intent to make a useful commercial recommendation yet. A few more course views or a save will make the next step more precise.",
                "Intent is below the policy threshold, so the engine delays instead of adding noise.",
            )

        if not ranked:
            return self._no_product_decision(
                "show_nothing", "no_grounded_candidate", intent, evidence, context,
                "No grounded action available",
                "I would not recommend anything until SmartReco can verify a catalog match for your current constraints.",
                "Retrieval returned no eligible catalog product.",
            )

        baseline = next((item for item in ranked if not item.candidate.product.is_bundle), ranked[0])
        baseline_product = baseline.candidate.product
        baseline_offer = self._personalized_offer(
            db, baseline_product, purchased_product_ids
        )
        baseline_probability = self._conversion_probability(
            intent, baseline.final_score, baseline_product, profile, baseline_offer
        )
        baseline_price = float(baseline_offer.personalized_price)
        baseline_revenue = baseline_price * baseline_probability
        context["baseline"] = {
            "product_id": baseline_product.id,
            "personalized_price": round(baseline_price, 2),
            "conversion_probability": round(baseline_probability, 4),
            "expected_revenue": round(baseline_revenue, 2),
        }

        bundle_choice = self._optimize_bundle(db, profile, intent, ranked, baseline_revenue)
        if bundle_choice and intent.purchase_propensity >= .45:
            product, probability, revenue, incremental, bundle_context = bundle_choice
            context["bundle_optimizer"] = bundle_context
            return self._product_decision(
                db, "offer_bundle", "bundle_value", product, intent, profile, evidence, context,
                probability, revenue, incremental,
            )

        prerequisite = self._missing_prerequisite(db, baseline_product, profile)
        if prerequisite and intent.purchase_propensity >= .36:
            prerequisite_offer = self._personalized_offer(
                db, prerequisite, purchased_product_ids
            )
            probability = self._conversion_probability(
                intent,
                min(1, baseline.final_score + .08),
                prerequisite,
                profile,
                prerequisite_offer,
            )
            revenue = float(prerequisite_offer.personalized_price) * probability
            context["prerequisite_for_product_id"] = baseline_product.id
            return self._product_decision(
                db, "prerequisite_first", "prerequisite", prerequisite, intent, profile, evidence, context,
                probability, revenue, revenue - baseline_revenue,
            )

        target = baseline_product
        target_score = baseline.final_score
        action_type = "recommend_course"
        strategy = "skill_gap"
        if intent.price_sensitivity >= .58 or float(target.price) > float(profile.get("budget_max", 10**9)):
            cheaper = self._cheaper_alternative(ranked, target, profile)
            if cheaper:
                target = cheaper.candidate.product
                target_score = cheaper.final_score
                action_type = "cheaper_alternative"
                strategy = "price_value"
        elif intent.speed_priority >= .62:
            faster = self._faster_alternative(ranked, target)
            if faster:
                target = faster.candidate.product
                target_score = faster.final_score
            strategy = "fast_track"
        if action_type == "recommend_course" and target.promotion_ends_at:
            end = target.promotion_ends_at if target.promotion_ends_at.tzinfo else target.promotion_ends_at.replace(tzinfo=timezone.utc)
            if now < end <= now + timedelta(hours=72):
                action_type, strategy = "legitimate_urgency", "verified_deadline"
                context["verified_promotion_ends_at"] = end.isoformat()
        if action_type == "recommend_course":
            if intent.career_orientation >= .58 and target.career_outcomes:
                action_type, strategy = "career_outcome", "career_advancement"
            elif target.social_proof_text and .30 <= intent.purchase_propensity <= .68:
                action_type, strategy = "show_social_proof", "social_proof"
            elif intent.speed_priority >= .62:
                strategy = "fast_track"

        target_offer = self._personalized_offer(db, target, purchased_product_ids)
        probability = self._conversion_probability(
            intent, target_score, target, profile, target_offer
        )
        revenue = float(target_offer.personalized_price) * probability
        return self._product_decision(
            db, action_type, strategy, target, intent, profile, evidence, context,
            probability, revenue, revenue - baseline_revenue,
        )

    @staticmethod
    def select_persuasion(decision: ActionDecision, intent: IntentAssessment) -> ActionDecision:
        """Make the persuasion dimension explicit after the commercial action is fixed."""
        fixed = {
            "offer_bundle": "bundle_value",
            "cheaper_alternative": "price_value",
            "prerequisite_first": "prerequisite",
            "show_social_proof": "social_proof",
            "career_outcome": "career_advancement",
            "legitimate_urgency": "verified_deadline",
            "email_later": "low_pressure_follow_up",
            "delay_recommendation": "exploration_first",
            "show_nothing": "respectful_pause",
        }
        decision.persuasion_strategy = fixed.get(decision.action_type, decision.persuasion_strategy)
        if decision.action_type == "recommend_course":
            if intent.price_sensitivity >= .58:
                decision.persuasion_strategy = "price_value"
            elif intent.speed_priority >= .62:
                decision.persuasion_strategy = "fast_track"
            elif intent.career_orientation >= .58:
                decision.persuasion_strategy = "career_advancement"
        decision.policy_context["persuasion_inputs"] = {
            "price_sensitivity": intent.price_sensitivity,
            "speed_priority": intent.speed_priority,
            "career_orientation": intent.career_orientation,
        }
        return decision

    @staticmethod
    def _personalized_offer(
        db: Session,
        product: Product,
        purchased_product_ids: set[str],
    ) -> PersonalizedProductOffer:
        components = (
            list(db.scalars(select(Product).where(
                Product.id.in_(product.bundled_product_ids or [])
            )))
            if product.is_bundle else []
        )
        return calculate_personalized_offer(product, components, purchased_product_ids)

    @staticmethod
    def _conversion_probability(
        intent: IntentAssessment,
        fit: float,
        product: Product,
        profile: dict,
        offer: PersonalizedProductOffer | None = None,
    ) -> float:
        budget = float(profile.get("budget_max", 10**9) or 10**9)
        effective_price = float(offer.personalized_price if offer else product.price)
        affordability = 1.0 if effective_price <= budget else max(
            .28, budget / max(effective_price, 1)
        )
        discount_percent = offer.discount_percent if offer else product.savings_percent
        bundle_bonus = 1.06 if product.is_bundle and discount_percent >= 20 else 1.0
        return round(_clamp(intent.purchase_propensity * (.58 + .42 * fit) * affordability ** .65 * bundle_bonus), 4)

    def _optimize_bundle(
        self,
        db: Session,
        profile: dict,
        intent: IntentAssessment,
        ranked: list[RankedCandidate],
        baseline_revenue: float,
    ) -> tuple[Product, float, float, float, dict] | None:
        bundles = list(db.scalars(select(Product).where(Product.is_active.is_(True), Product.is_bundle.is_(True))))
        ranked_by_id = {item.candidate.product.id: item for item in ranked}
        interest_ids = set(intent.strongest_product_ids) | set(profile.get("saved_product_ids", [])) | set(profile.get("cart_product_ids", []))
        purchased_ids = set(profile.get("purchased_product_ids", []))
        completed_ids = set(profile.get("completed_product_ids", []))
        interest_bundles = {str(item).lower() for item in profile.get("emerging_interests", [])}
        all_component_ids = {
            component_id
            for bundle in bundles
            for component_id in (bundle.bundled_product_ids or [])
        }
        components_by_id = {
            component.id: component
            for component in db.scalars(select(Product).where(
                Product.id.in_(all_component_ids)
            ))
        } if all_component_ids else {}
        best: tuple[Product, float, float, float, dict] | None = None
        for bundle in bundles:
            component_ids = set(bundle.bundled_product_ids or [])
            direct_hits = len(component_ids & interest_ids)
            components = [
                components_by_id[component_id]
                for component_id in (bundle.bundled_product_ids or [])
                if component_id in components_by_id
            ]
            try:
                offer = calculate_personalized_offer(bundle, components, purchased_ids)
            except ValueError:
                continue
            if not offer.eligible:
                continue
            owned_component_ids = component_ids & purchased_ids
            completed_component_ids = component_ids & completed_ids
            completion_ratio = len(owned_component_ids) / max(len(component_ids), 1)
            taxonomy_hits = sum(
                1 for component in components if component.skill_bundle.lower() in interest_bundles
            )
            # Purchases and completions are strong relevance evidence.  Only
            # durable purchases affect the monetary offer calculated above.
            behavioral_component_hits = len(
                component_ids & (interest_ids | purchased_ids | completed_ids)
            )
            effective_hits = max(behavioral_component_hits, taxonomy_hits)
            if effective_hits < 2:
                continue
            component_scores = [ranked_by_id[pid].final_score for pid in component_ids if pid in ranked_by_id]
            if bundle.id in ranked_by_id:
                component_scores.append(ranked_by_id[bundle.id].final_score)
            semantic_fit = sum(component_scores) / len(component_scores) if component_scores else .58
            coverage = effective_hits / max(len(component_ids), 1)
            fit = _clamp(
                .50 * semantic_fit
                + .38 * min(1, coverage * 1.5)
                + .12 * completion_ratio
            )
            probability = self._conversion_probability(intent, fit, bundle, profile, offer)
            probability *= 1 + min(.14, completion_ratio * .18)
            # High price sensitivity slightly reduces bundle conversion even when
            # the percentage saving is attractive.
            probability *= 1 - intent.price_sensitivity * .16
            probability = round(_clamp(probability), 4)
            personalized_price = float(offer.personalized_price)
            expected_revenue = personalized_price * probability
            incremental = expected_revenue - baseline_revenue
            budget = float(profile.get("budget_max", 10**9) or 10**9)
            affordability_guard = (
                personalized_price <= budget
                or (intent.price_sensitivity < .45 and personalized_price <= budget * 1.75)
            )
            user_value = (
                offer.discount_percent >= 15
                # At least half the track must be supported by behavior,
                # ownership, completion, or managed-interest evidence. This
                # prevents a larger, weakly related bundle from winning only
                # because its ticket produces more expected revenue.
                and coverage >= .50
                and bool(offer.remaining_components)
                and affordability_guard
            )
            context = {
                "bundle_id": bundle.id,
                "interested_components": direct_hits,
                "taxonomy_matched_components": taxonomy_hits,
                "component_count": len(component_ids),
                "owned_component_ids": sorted(owned_component_ids),
                "owned_component_count": len(owned_component_ids),
                "completed_component_count": len(completed_component_ids),
                "remaining_component_ids": [
                    component.product_id for component in offer.remaining_components
                ],
                "remaining_component_count": len(offer.remaining_components),
                "ownership_completion": round(completion_ratio, 4),
                "interest_coverage": round(coverage, 4),
                "fit": round(fit, 4),
                "catalog_price": float(offer.catalog_price),
                "full_standalone_subtotal": float(offer.full_standalone_subtotal),
                "owned_standalone_subtotal": float(offer.owned_standalone_subtotal),
                "personalized_price": personalized_price,
                "ownership_credit": float(offer.ownership_credit),
                "remaining_standalone_subtotal": float(offer.remaining_standalone_subtotal),
                "personalized_savings": float(offer.savings),
                "discount_rate": float(offer.discount_rate),
                "discount_percent": offer.discount_percent,
                "owned_components": [{
                    "product_id": component.product_id,
                    "title": component.title,
                    "standalone_price": float(component.standalone_price),
                } for component in offer.owned_components],
                "remaining_components": [{
                    "product_id": component.product_id,
                    "title": component.title,
                    "standalone_price": float(component.standalone_price),
                } for component in offer.remaining_components],
                "affordability_guard_passed": affordability_guard,
                "expected_conversion_probability": probability,
                "expected_revenue": round(expected_revenue, 2),
                "baseline_expected_revenue": round(baseline_revenue, 2),
                "incremental_expected_revenue": round(incremental, 2),
                "user_value_guard_passed": user_value,
            }
            if (user_value and incremental > max(100, baseline_revenue * .04)
                    and (not best or expected_revenue > best[2])):
                best = (bundle, probability, expected_revenue, incremental, context)
        return best

    @staticmethod
    def _missing_prerequisite(db: Session, product: Product, profile: dict) -> Product | None:
        owned_or_completed = (
            set(profile.get("completed_product_ids", []))
            | set(profile.get("purchased_product_ids", []))
        )
        for product_id in product.prerequisite_product_ids or []:
            if product_id in owned_or_completed:
                continue
            prerequisite = db.get(Product, product_id)
            if is_product_recommendable(prerequisite, owned_or_completed):
                return prerequisite
        return None

    @staticmethod
    def _cheaper_alternative(ranked: list[RankedCandidate], target: Product, profile: dict) -> RankedCandidate | None:
        budget = float(profile.get("budget_max", float(target.price)))
        eligible = [item for item in ranked if not item.candidate.product.is_bundle
                    and float(item.candidate.product.price) < float(target.price)
                    and float(item.candidate.product.price) <= budget
                    and item.final_score >= .55]
        return max(eligible, key=lambda item: item.final_score, default=None)

    @staticmethod
    def _faster_alternative(ranked: list[RankedCandidate], target: Product) -> RankedCandidate | None:
        target_item = next((item for item in ranked if item.candidate.product.id == target.id), None)
        minimum_fit = max(.55, (target_item.final_score - .08) if target_item else .55)
        eligible = [item for item in ranked if not item.candidate.product.is_bundle
                    and item.candidate.product.duration_hours < target.duration_hours
                    and item.final_score >= minimum_fit]
        return min(eligible, key=lambda item: (item.candidate.product.duration_hours, -item.final_score), default=None)

    def _product_decision(
        self,
        db: Session,
        action_type: str,
        strategy: str,
        product: Product,
        intent: IntentAssessment,
        profile: dict,
        evidence: list[str],
        context: dict,
        probability: float,
        revenue: float,
        incremental: float,
    ) -> ActionDecision:
        signals = self._market_signals(db, product)
        market_clause = "".join(
            f" {signal.claim.rstrip('.')} ({signal.source_name})." for signal in signals
        )
        gap_skills = [skill for skill in product.skills if skill.lower() in {
            str(item).lower() for item in profile.get("skill_gaps", [])
        }] or list(product.skills[:2])
        goal = profile.get("primary_goal", "your learning goal")
        hours = profile.get("weekly_hours", 8)
        action_phrases = {
            "offer_bundle": f"take the {product.title} bundle",
            "cheaper_alternative": f"choose the focused {product.title} course",
            "prerequisite_first": f"complete {product.title} first",
            "show_social_proof": f"consider {product.title}",
            "career_outcome": f"take {product.title} next",
            "legitimate_urgency": f"review {product.title} before its verified offer ends",
            "email_later": f"revisit {product.title} later",
            "recommend_course": f"take {product.title} next",
        }
        why = self._strategy_reason(strategy, product, gap_skills, profile, context)
        observed = self._observed_signal_clause(profile)
        message = (
            f"{observed}If I were working toward {goal} with about {hours} hours a week, I would "
            f"{action_phrases[action_type]}. {why}{market_clause}"
        )
        return ActionDecision(
            action_type=action_type,
            persuasion_strategy=strategy,
            product=product,
            headline=self._headline(action_type, product),
            message=message[:1200],
            rationale=(f"Selected by {POLICY_VERSION} from propensity, catalog fit, affordability, "
                       f"fatigue, and expected-revenue checks. Key skills: {', '.join(gap_skills)}."),
            intent_stage=intent.stage,
            purchase_propensity=intent.purchase_propensity,
            expected_conversion_probability=probability,
            expected_revenue=_money(revenue),
            incremental_expected_revenue=_money(incremental),
            evidence_event_ids=evidence,
            market_signal_ids=[signal.id for signal in signals],
            policy_context=context,
        )

    @staticmethod
    def _strategy_reason(
        strategy: str,
        product: Product,
        gaps: list[str],
        profile: dict,
        context: dict | None = None,
    ) -> str:
        joined = " and ".join(gaps[:2])
        if strategy == "bundle_value":
            bundle_context = (context or {}).get("bundle_optimizer") or {}
            owned_count = int(bundle_context.get("owned_component_count") or 0)
            credit = float(bundle_context.get("ownership_credit") or 0)
            personalized_price = float(
                bundle_context.get("personalized_price") or product.price
            )
            ownership_clause = (
                f" You already own {owned_count} included course"
                f"{'s' if owned_count != 1 else ''}, so ₹{credit:,.0f} is credited and "
                f"the remaining track costs ₹{personalized_price:,.0f}."
                if owned_count and credit > 0 else ""
            )
            discount_percent = int(
                bundle_context.get("discount_percent") or product.savings_percent
            )
            return (
                f"Your activity spans several included courses, so this closes the connected "
                f"gaps in {joined} while preserving a {discount_percent}% bundle discount."
                f"{ownership_clause}"
            )
        if strategy == "price_value":
            return f"It preserves the strongest skill match in {joined} while staying closer to your budget."
        if strategy == "prerequisite":
            return f"It builds {joined} before the more advanced material, reducing the risk of paying for a course too early."
        if strategy == "fast_track":
            return f"Its {round(product.duration_hours)}-hour path is the most direct strong match for building {joined}."
        if strategy == "career_advancement" and product.career_outcomes:
            return f"It targets {product.career_outcomes[0].lower()} and develops {joined}, which are central gaps in your current path."
        if strategy == "social_proof" and product.social_proof_text:
            return f"{product.social_proof_text} It also develops {joined}."
        if strategy == "verified_deadline":
            end = product.promotion_ends_at
            if end:
                end = end.replace(tzinfo=timezone.utc) if not end.tzinfo else end.astimezone(timezone.utc)
            deadline = f"{end.strftime('%d %b %Y, %H:%M')} UTC" if end else "at the verified catalog deadline"
            return f"The verified offer ends {deadline}; the course develops {joined}."
        return f"It directly develops {joined}, the strongest gaps inferred from your recent activity."

    @staticmethod
    def _observed_signal_clause(profile: dict) -> str:
        observations: list[str] = []
        for signal in profile.get("high_intent_signals", [])[:3]:
            if signal.startswith("time spent: "):
                observations.append(f"spent meaningful time exploring {signal.removeprefix('time spent: ')}")
            elif signal.startswith("product view: "):
                observations.append(f"opened {signal.removeprefix('product view: ')}")
            elif signal.startswith("wishlist add: "):
                observations.append(f"saved {signal.removeprefix('wishlist add: ')}")
            elif signal.startswith("enrollment: "):
                observations.append(f"made a demo purchase for {signal.removeprefix('enrollment: ')}")
            elif signal.startswith(("searched for ", "filtered for ")):
                observations.append(signal)
        if not observations:
            return "Your strongest recent signals point to this skill path. "
        if len(observations) == 1:
            return f"I noticed you {observations[0]}. "
        return f"I noticed you {', '.join(observations[:-1])}, and {observations[-1]}. "

    @staticmethod
    def _headline(action_type: str, product: Product) -> str:
        labels = {
            "offer_bundle": "I would consolidate this into one professional track",
            "cheaper_alternative": "I would take the focused, lower-cost route",
            "prerequisite_first": "I would build this prerequisite first",
            "show_social_proof": "I would consider this learner-popular path next",
            "career_outcome": "I would make this the next career-building move",
            "legitimate_urgency": "I would review this verified offer now",
            "recommend_course": "I would learn this next",
        }
        return labels.get(action_type, f"I would choose {product.title} next")

    @staticmethod
    def _market_signals(db: Session, product: Product) -> list[MarketSignal]:
        now = datetime.now(timezone.utc)
        signals = list(db.scalars(select(MarketSignal).where(
            MarketSignal.is_active.is_(True), MarketSignal.valid_until > now,
        ).order_by(MarketSignal.published_at.desc())))
        product_terms = {skill.lower() for skill in product.skills} | {product.category.lower(), product.title.lower()}
        matches = []
        for signal in signals:
            signal_terms = {skill.lower() for skill in signal.skills}
            if any(a == b or a in b or b in a for a in product_terms for b in signal_terms):
                matches.append(signal)
        return matches[:2]

    @staticmethod
    def _no_product_decision(
        action_type: str,
        strategy: str,
        intent: IntentAssessment,
        evidence: list[str],
        context: dict,
        headline: str,
        message: str,
        rationale: str,
    ) -> ActionDecision:
        context["suppression_reason"] = action_type
        return ActionDecision(
            action_type=action_type,
            persuasion_strategy=strategy,
            product=None,
            headline=headline,
            message=message,
            rationale=rationale,
            intent_stage=intent.stage,
            purchase_propensity=intent.purchase_propensity,
            evidence_event_ids=evidence,
            policy_context=context,
        )

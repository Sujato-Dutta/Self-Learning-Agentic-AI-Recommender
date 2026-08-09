from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from src.models import (
    Event,
    MarketSignal,
    NextBestAction,
    NextBestActionReward,
    Product,
    Recommendation,
    User,
)
from src.services.behavior_service import aggregate_profile
from src.services.intent_service import IntentAssessment, assess_purchase_intent
from src.services.next_best_action_service import NextBestActionPolicy
from src.services.reranking_service import RankedCandidate
from src.services.retrieval_service import Candidate
from src.services.reward_service import (
    attribute_event_rewards,
    record_direct_action_feedback,
)


def ranked(product: Product, score: float = .84) -> RankedCandidate:
    return RankedCandidate(Candidate(product, score, "test"), score, {"semantic": score})


def intent(**overrides) -> IntentAssessment:
    values = {
        "score": .65,
        "purchase_propensity": .62,
        "stage": "high_intent",
        "price_sensitivity": .1,
        "speed_priority": .1,
        "career_orientation": .2,
        "recommendation_fatigue": 0,
        "strongest_product_ids": [],
        "evidence_event_ids": ["event-evidence-1"],
        "factors": {"cart_actions": 0, "wishlist_adds": 0},
    }
    values.update(overrides)
    return IntentAssessment(**values)


def test_demo_intent_selects_revenue_positive_agentic_bundle(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    products = {product.title: product for product in db.scalars(select(Product))}
    profile = aggregate_profile(db, user.id)
    assessment = assess_purchase_intent(db, user.id, profile)
    candidates = [
        ranked(products["Agentic AI Foundations"], .90),
        ranked(products["Advanced Agentic AI"], .87),
        ranked(products["Production RAG Systems"], .84),
    ]

    decision = NextBestActionPolicy().decide(db, user.id, profile, assessment, candidates)

    assert assessment.stage in {"high_intent", "purchase_ready"}
    assert decision.action_type == "offer_bundle"
    assert decision.product.title == "Agentic AI Professional"
    assert decision.incremental_expected_revenue > 0
    optimizer = decision.policy_context["bundle_optimizer"]
    assert optimizer["user_value_guard_passed"] is True
    assert optimizer["affordability_guard_passed"] is True
    assert optimizer["expected_revenue"] > optimizer["baseline_expected_revenue"]

    partial_owner = NextBestActionPolicy().decide(
        db,
        user.id,
        {
            **profile,
            "purchased_product_ids": [products["Agentic AI Foundations"].id],
            "completed_product_ids": [],
        },
        assessment, candidates,
    )
    assert partial_owner.action_type == "offer_bundle"
    assert partial_owner.product.id == decision.product.id
    partial_optimizer = partial_owner.policy_context["bundle_optimizer"]
    assert partial_optimizer["owned_component_count"] == 1
    assert partial_optimizer["remaining_component_count"] == 2
    assert Decimal(str(partial_optimizer["ownership_credit"])) > 0
    assert Decimal(str(partial_optimizer["personalized_price"])) < Decimal(
        str(partial_optimizer["catalog_price"])
    )
    assert (
        partial_optimizer["expected_conversion_probability"]
        > optimizer["expected_conversion_probability"]
    )


def test_policy_recommends_missing_prerequisite_before_advanced_course(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    advanced = db.scalar(select(Product).where(Product.title == "Advanced Agentic AI"))
    profile = {"primary_goal": "Build AI systems", "budget_max": 10000, "weekly_hours": 8,
               "completed_product_ids": [], "saved_product_ids": [], "cart_product_ids": [],
               "evidence_event_ids": ["event-evidence-1"], "skill_gaps": ["Agents"]}

    decision = NextBestActionPolicy().decide(
        db, user.id, profile,
        intent(strongest_product_ids=[advanced.id], purchase_propensity=.55),
        [ranked(advanced, .89)],
    )

    assert decision.action_type == "prerequisite_first"
    assert decision.product.title == "Agentic AI Foundations"
    assert decision.policy_context["prerequisite_for_product_id"] == advanced.id


def test_policy_defensively_skips_a_purchased_candidate(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    foundation = db.scalar(select(Product).where(Product.title == "Agentic AI Foundations"))
    advanced = db.scalar(select(Product).where(Product.title == "Advanced Agentic AI"))
    profile = {
        "primary_goal": "Build production AI systems",
        "budget_max": 10_000,
        "weekly_hours": 8,
        "purchased_product_ids": [foundation.id],
        "completed_product_ids": [],
        "saved_product_ids": [],
        "cart_product_ids": [],
        "evidence_event_ids": ["event-evidence-1"],
        "skill_gaps": ["AI Agents", "RAG"],
    }

    decision = NextBestActionPolicy().decide(
        db,
        user.id,
        profile,
        intent(strongest_product_ids=[foundation.id, advanced.id]),
        [ranked(foundation, .95), ranked(advanced, .86)],
    )

    assert decision.product is not None
    assert decision.product.title == "Agentic AI Professional"
    assert decision.action_type == "offer_bundle"
    optimizer = decision.policy_context["bundle_optimizer"]
    assert optimizer["owned_component_ids"] == [foundation.id]
    assert optimizer["ownership_credit"] > 0


def test_policy_can_offer_a_prerequisite_bundle_at_completion_price(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    foundation = db.scalar(select(Product).where(Product.title == "Agentic AI Foundations"))
    advanced = db.scalar(select(Product).where(Product.title == "Advanced Agentic AI"))
    overlapping_bundle = db.scalar(select(Product).where(Product.title == "Agentic AI Professional"))
    assert foundation.id in overlapping_bundle.bundled_product_ids
    advanced.prerequisite_product_ids = [overlapping_bundle.id]
    db.commit()
    profile = {
        "primary_goal": "Build production AI systems",
        "budget_max": 20_000,
        "weekly_hours": 8,
        "purchased_product_ids": [foundation.id],
        "completed_product_ids": [],
        "saved_product_ids": [],
        "cart_product_ids": [],
        "evidence_event_ids": ["event-evidence-1"],
        "skill_gaps": ["AI Agents", "RAG"],
    }

    decision = NextBestActionPolicy().decide(
        db,
        user.id,
        profile,
        intent(strongest_product_ids=[advanced.id]),
        [ranked(advanced, .9)],
    )

    assert decision.product is not None
    assert decision.product.id == overlapping_bundle.id
    assert decision.action_type == "offer_bundle"
    optimizer = decision.policy_context["bundle_optimizer"]
    assert optimizer["owned_component_ids"] == [foundation.id]
    assert optimizer["personalized_price"] < float(overlapping_bundle.price)


def test_adaptive_persuasion_changes_route_for_goal_price_and_speed(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    foundation = db.scalar(select(Product).where(Product.title == "Agentic AI Foundations"))
    python = db.scalar(select(Product).where(Product.title == "Python Foundations"))
    # Prerequisite routing has dedicated coverage above. Remove it here so this
    # case isolates how persuasion changes for the same eligible catalog.
    foundation.prerequisite_product_ids = []
    db.commit()
    base_profile = {"primary_goal": "Become an AI engineer", "budget_max": 8000, "weekly_hours": 8,
                    "completed_product_ids": [], "saved_product_ids": [], "cart_product_ids": [],
                    "evidence_event_ids": ["event-evidence-1"], "skill_gaps": ["Agents", "RAG"]}
    catalog = [ranked(foundation, .90), ranked(python, .84)]
    policy = NextBestActionPolicy()

    career = policy.decide(db, user.id, base_profile,
                           intent(career_orientation=.9, strongest_product_ids=[foundation.id]), catalog)
    price = policy.decide(db, user.id, {**base_profile, "budget_max": 2000},
                          intent(price_sensitivity=.9, strongest_product_ids=[foundation.id]), catalog)
    speed = policy.decide(db, user.id, base_profile,
                          intent(speed_priority=.9, strongest_product_ids=[foundation.id]), catalog)

    assert (career.action_type, career.persuasion_strategy) == ("career_outcome", "career_advancement")
    assert "reliable tool-using agent workflows" in career.message.lower()
    assert (price.action_type, price.persuasion_strategy, price.product.title) == (
        "cheaper_alternative", "price_value", "Python Foundations"
    )
    assert "budget" in price.message.lower()
    assert (speed.action_type, speed.persuasion_strategy, speed.product.title) == (
        "recommend_course", "fast_track", "Python Foundations"
    )
    assert "14-hour" in speed.message


def test_fatigue_suppresses_and_urgency_requires_a_real_deadline(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    power_bi = db.scalar(select(Product).where(Product.title == "Power BI Analytics"))
    profile = {"primary_goal": "Improve analytics", "budget_max": 5000, "weekly_hours": 5,
               "completed_product_ids": [], "saved_product_ids": [], "cart_product_ids": [],
               "evidence_event_ids": ["event-evidence-1"], "skill_gaps": ["Power BI"]}
    policy = NextBestActionPolicy()

    suppressed = policy.decide(
        db, user.id, profile, intent(recommendation_fatigue=.9), [ranked(power_bi)]
    )
    assert suppressed.action_type == "show_nothing"
    assert suppressed.product is None

    no_deadline = policy.decide(db, user.id, profile, intent(), [ranked(power_bi)])
    assert no_deadline.action_type != "legitimate_urgency"
    power_bi.promotion_ends_at = datetime.now(timezone.utc) + timedelta(hours=24)
    verified = policy.decide(db, user.id, profile, intent(), [ranked(power_bi)])
    assert verified.action_type == "legitimate_urgency"
    assert "verified_promotion_ends_at" in verified.policy_context


def test_market_evidence_is_seeded_with_source_and_expiry(db):
    signals = list(db.scalars(select(MarketSignal).where(MarketSignal.is_active.is_(True))))
    assert len(signals) == 2
    assert all(signal.source_url.startswith("https://") for signal in signals)
    assert all(signal.valid_until > signal.published_at for signal in signals)
    assert any("AI Agents" in signal.claim for signal in signals)


def test_reward_attribution_is_idempotent_and_updates_outcome(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    product = db.scalar(select(Product).where(Product.title == "Agentic AI Foundations"))
    now = datetime.now(timezone.utc)
    recommendation = Recommendation(
        user_id=user.id, behavior_profile_version=1, profile_hash="reward-profile",
        headline="Next step", narrative="A grounded next step for this learner's current direction.",
        reason_summary="Test evidence", confidence=.8, trigger_type="test", model_name="test",
        prompt_version="test", expires_at=now + timedelta(hours=6),
    )
    db.add(recommendation)
    db.flush()
    decision = NextBestAction(
        user_id=user.id, recommendation_id=recommendation.id, product_id=product.id,
        action_type="recommend_course", persuasion_strategy="skill_gap",
        headline="Learn this next", message="A grounded recommendation message for the selected course.",
        rationale="Policy test", intent_stage="high_intent", purchase_propensity=.7,
        expected_conversion_probability=.5, expected_revenue=Decimal("1999.50"),
        incremental_expected_revenue=Decimal(0), expires_at=now + timedelta(hours=6),
    )
    db.add(decision)
    db.flush()
    events = [
        Event(event_id="reward-cart-event", user_id=user.id, session_id="reward-session",
              event_type="cta_click", product_id=product.id,
              event_metadata={"source": "journey_twin_add_to_cart", "decision_id": decision.id},
              occurred_at=now),
        Event(event_id="reward-enroll-event", user_id=user.id, session_id="reward-session",
              event_type="enrollment", product_id=product.id,
              event_metadata={"decision_id": decision.id}, occurred_at=now + timedelta(minutes=1)),
    ]
    db.add_all(events)
    db.commit()

    assert attribute_event_rewards(db, user.id, [event.event_id for event in events]) == 2
    assert attribute_event_rewards(db, user.id, [event.event_id for event in events]) == 0
    db.refresh(decision)
    assert decision.cumulative_reward == 1.55
    assert decision.status == "converted"
    assert decision.outcome_type == "purchase"
    assert len(list(db.scalars(select(NextBestActionReward).where(
        NextBestActionReward.decision_id == decision.id
    )))) == 2


def test_direct_feedback_becomes_behavior_evidence_and_suppresses_product(db):
    user = db.scalar(select(User).where(User.email == "learner@smartreco.dev"))
    product = db.scalar(select(Product).where(Product.title == "Agentic AI Foundations"))
    now = datetime.now(timezone.utc)
    recommendation = Recommendation(
        user_id=user.id, behavior_profile_version=1, profile_hash="feedback-profile",
        headline="Next step", narrative="A grounded next step for this learner's current direction.",
        reason_summary="Test evidence", confidence=.8, trigger_type="test", model_name="test",
        prompt_version="test", expires_at=now + timedelta(hours=6),
    )
    db.add(recommendation)
    db.flush()
    decision = NextBestAction(
        user_id=user.id, recommendation_id=recommendation.id, product_id=product.id,
        action_type="recommend_course", persuasion_strategy="skill_gap",
        headline="Learn this next", message="A grounded recommendation message for the selected course.",
        rationale="Policy test", intent_stage="high_intent", purchase_propensity=.7,
        expected_conversion_probability=.5, expected_revenue=Decimal("1999.50"),
        incremental_expected_revenue=Decimal(0), expires_at=now + timedelta(hours=6),
    )
    db.add(decision)
    db.commit()

    reward = record_direct_action_feedback(
        db, user.id, decision.id, "not_interested", "direct-feedback-event"
    )
    repeated = record_direct_action_feedback(
        db, user.id, decision.id, "not_interested", "direct-feedback-event"
    )
    db.refresh(decision)
    profile = aggregate_profile(db, user.id)

    assert reward.id == repeated.id
    assert decision.status == "dismissed"
    assert decision.cumulative_reward == -.65
    assert product.id in profile["excluded_product_ids"]
    assert db.scalar(select(Event).where(Event.event_id == "direct-feedback-event")) is not None

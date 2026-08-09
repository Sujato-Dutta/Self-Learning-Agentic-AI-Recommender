import hashlib
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from src.agents.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from src.agents.state import RecommendationState
from src.agents.validators import validate_action_copy, validate_grounding
from src.config import Settings
from src.models import (
    AgentRun,
    MarketSignal,
    NextBestAction,
    Recommendation,
    RecommendationItem,
)
from src.observability.metrics import (
    GROUNDING_FAILURES,
    MESH_AVOIDED,
    NBA_DECISIONS,
    NBA_EXPECTED_REVENUE,
    NBA_PROPENSITY,
    RECOMMENDATION_LATENCY,
    RECOMMENDATION_RUNS,
)
from src.services.behavior_service import aggregate_profile
from src.services.cache_service import cache, generation_lock
from src.services.intent_service import assess_purchase_intent
from src.services.mesh_client import MeshClient, MeshUnavailable
from src.services.next_best_action_service import ActionDecision, NextBestActionPolicy
from src.services.ownership_service import is_product_recommendable
from src.services.reranking_service import RankedCandidate, rerank
from src.services.retrieval_service import RetrievalService


@contextmanager
def generation_guard(db: Session, user_id: str):
    """Coalesce generation in-process and across PostgreSQL worker processes."""
    with generation_lock(user_id):
        if db.get_bind().dialect.name != "postgresql":
            yield
            return
        lock_key = int.from_bytes(hashlib.sha256(user_id.encode()).digest()[:8], "big", signed=True)
        with db.get_bind().connect() as lock_connection:
            lock_connection.execute(text("SELECT pg_advisory_lock(:lock_key)"), {"lock_key": lock_key})
            try:
                yield
            finally:
                lock_connection.execute(text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key})


class RecommendationService:
    def __init__(self, settings: Settings, mesh: MeshClient, retrieval: RetrievalService) -> None:
        self.settings = settings
        self.mesh = mesh
        self.retrieval = retrieval
        self.action_policy = NextBestActionPolicy()

    def generate(self, db: Session, user_id: str, trigger_type: str = "manual", force: bool = False) -> Recommendation:
        started = time.perf_counter()
        with generation_guard(db, user_id):
            profile = aggregate_profile(db, user_id)
            if not force:
                cached = self._cached(db, user_id, profile)
                if cached:
                    MESH_AVOIDED.labels(reason="profile_cache_hit").inc()
                    return cached
                now = datetime.now(timezone.utc)
                latest = db.scalar(select(Recommendation).where(Recommendation.user_id == user_id)
                                   .order_by(Recommendation.created_at.desc()))
                latest_is_usable = self._recommendation_is_eligible(latest, profile)
                latest_run = db.scalar(select(AgentRun).where(AgentRun.user_id == user_id)
                                       .order_by(AgentRun.created_at.desc()))
                immediate_triggers = {"user_refresh", "profile_correction", "discover_high_intent"}
                if latest_is_usable and latest_run and trigger_type not in immediate_triggers:
                    run_at = latest_run.created_at
                    run_at = run_at if run_at.tzinfo else run_at.replace(tzinfo=timezone.utc)
                    if run_at > now - timedelta(minutes=self.settings.recommendation_cooldown_minutes):
                        MESH_AVOIDED.labels(reason="generation_cooldown").inc()
                        return latest
                hourly_runs = db.scalar(select(func.count()).select_from(AgentRun).where(
                    AgentRun.user_id == user_id, AgentRun.created_at >= now - timedelta(hours=1))) or 0
                daily_runs = db.scalar(select(func.count()).select_from(AgentRun).where(
                    AgentRun.user_id == user_id, AgentRun.created_at >= now - timedelta(days=1))) or 0
                if (
                    hourly_runs >= self.settings.recommendation_hourly_limit
                    or daily_runs >= self.settings.recommendation_daily_limit
                ) and latest_is_usable:
                    MESH_AVOIDED.labels(reason="generation_rate_limit").inc()
                    return latest
            run = AgentRun(user_id=user_id, trigger_type=trigger_type, status="running")
            db.add(run)
            db.commit()
            try:
                graph = self._build_graph(db, run.id)
                result = graph.invoke({
                    "user_id": user_id,
                    "trigger_type": trigger_type,
                    "behavior_profile": profile,
                    "profile_hash": profile["profile_hash"],
                    "errors": [], "retry_count": 0, "node_trace": [], "degraded": False,
                })
                recommendation = db.get(Recommendation, result["recommendation_id"])
                run.status = "degraded" if result.get("degraded") else "success"
                run.node_trace = result.get("node_trace", [])
                run.retrieval_query = result.get("retrieval_query")
                run.candidates = [
                    {"product_id": item.candidate.product.id, "score": item.final_score}
                    for item in result.get("reranked_products", [])
                ]
                run.mesh_called = any(
                    node.get("node") == "generate" and node.get("mode") == "mesh"
                    for node in result.get("node_trace", [])
                )
                run.latency_ms = round((time.perf_counter() - started) * 1000, 2)
                db.commit()
                cache.set(f"recommendation:{user_id}:{profile['profile_hash']}", recommendation.id,
                          self.settings.recommendation_ttl_minutes * 60)
                RECOMMENDATION_RUNS.labels(status=run.status).inc()
                return recommendation
            except Exception as exc:
                run.status = "failed"
                run.error = str(exc)[:1000]
                run.latency_ms = round((time.perf_counter() - started) * 1000, 2)
                db.commit()
                RECOMMENDATION_RUNS.labels(status="failed").inc()
                raise
            finally:
                RECOMMENDATION_LATENCY.observe(time.perf_counter() - started)

    @staticmethod
    def _recommendation_is_eligible(
        recommendation: Recommendation | None,
        profile: dict,
        *,
        require_profile_hash: bool = False,
    ) -> bool:
        if not recommendation or not recommendation.next_best_action:
            return False
        if require_profile_hash and recommendation.profile_hash != profile.get("profile_hash"):
            return False
        if recommendation.status != "active":
            return False
        expiry = recommendation.expires_at
        expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            return False
        action = recommendation.next_best_action
        if action.status in {
            "dismissed", "converted", "expired", "cancelled", "failed", "delivered",
        }:
            return False
        action_expiry = action.expires_at
        action_expiry = (
            action_expiry
            if action_expiry.tzinfo
            else action_expiry.replace(tzinfo=timezone.utc)
        )
        if action_expiry <= datetime.now(timezone.utc):
            return False
        owned = set(profile.get("purchased_product_ids", []))
        if action.product and not is_product_recommendable(action.product, owned):
            return False
        return all(is_product_recommendable(item.product, owned) for item in recommendation.items)

    def _cached(self, db: Session, user_id: str, profile: dict) -> Recommendation | None:
        profile_hash = profile["profile_hash"]
        cache_key = f"recommendation:{user_id}:{profile_hash}"
        cached_id = cache.get(cache_key)
        if cached_id:
            recommendation = db.get(Recommendation, cached_id)
            if self._recommendation_is_eligible(
                recommendation, profile, require_profile_hash=True
            ):
                return recommendation
        recommendation = db.scalar(select(Recommendation).where(
            Recommendation.user_id == user_id,
            Recommendation.profile_hash == profile_hash,
            Recommendation.status == "active",
            Recommendation.expires_at > datetime.now(timezone.utc),
        ).order_by(Recommendation.created_at.desc()))
        if self._recommendation_is_eligible(
            recommendation, profile, require_profile_hash=True
        ):
            return recommendation
        return None

    def _build_graph(self, db: Session, run_id: str):
        workflow = StateGraph(RecommendationState)

        def assess_intent_node(state: RecommendationState) -> dict:
            started = time.perf_counter()
            assessment = assess_purchase_intent(db, state["user_id"], state["behavior_profile"])
            return {
                "intent_assessment": assessment,
                "node_trace": state["node_trace"] + [{
                    "node": "assess_intent",
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "stage": assessment.stage,
                    "purchase_propensity": assessment.purchase_propensity,
                    "model_version": assessment.model_version,
                }],
            }

        def retrieve_node(state: RecommendationState) -> dict:
            started = time.perf_counter()
            query, candidates, degraded = self.retrieval.retrieve(db, state["behavior_profile"])
            trace = state["node_trace"] + [{"node": "retrieve", "latency_ms": (time.perf_counter()-started)*1000,
                                             "candidate_count": len(candidates)}]
            return {"retrieval_query": query, "candidates": candidates, "degraded": degraded,
                    "retrieval_quality": "strong" if len(candidates) >= 3 else "weak", "node_trace": trace}

        def rerank_node(state: RecommendationState) -> dict:
            started = time.perf_counter()
            ranked = rerank(
                state["candidates"], state["behavior_profile"], limit=5, db=db
            )
            return {"reranked_products": ranked,
                    "node_trace": state["node_trace"] + [{"node": "rerank", "latency_ms": (time.perf_counter()-started)*1000}]}

        def decide_action_node(state: RecommendationState) -> dict:
            started = time.perf_counter()
            decision = self.action_policy.decide(
                db,
                state["user_id"],
                state["behavior_profile"],
                state["intent_assessment"],
                state["reranked_products"],
            )
            owned_or_completed = (
                set(state["behavior_profile"].get("purchased_product_ids", []))
                | set(state["behavior_profile"].get("completed_product_ids", []))
            )
            if decision.product and not is_product_recommendable(
                decision.product, owned_or_completed
            ):
                raise RuntimeError(
                    "next-best-action policy selected a product blocked by ownership"
                )
            return {
                "action_decision": decision,
                "node_trace": state["node_trace"] + [{
                    "node": "decide_action",
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "action_type": decision.action_type,
                    "product_id": decision.product.id if decision.product else None,
                    "expected_revenue": float(decision.expected_revenue),
                    "policy_version": decision.policy_version,
                }],
            }

        def select_persuasion_node(state: RecommendationState) -> dict:
            started = time.perf_counter()
            decision = self.action_policy.select_persuasion(
                state["action_decision"], state["intent_assessment"]
            )
            return {
                "action_decision": decision,
                "persuasion_context": decision.policy_context.get("persuasion_inputs", {}),
                "node_trace": state["node_trace"] + [{
                    "node": "select_persuasion",
                    "latency_ms": (time.perf_counter() - started) * 1000,
                    "strategy": decision.persuasion_strategy,
                }],
            }

        def generate_node(state: RecommendationState) -> dict:
            started = time.perf_counter()
            candidates: list[RankedCandidate] = state["reranked_products"]
            profile = state["behavior_profile"]
            decision: ActionDecision = state["action_decision"]
            product = decision.product
            bundle_context = decision.policy_context.get("bundle_optimizer") or {}
            selected_price = (
                float(bundle_context["personalized_price"])
                if product and product.is_bundle and "personalized_price" in bundle_context
                else float(product.price) if product else None
            )
            market_signals = [db.get(MarketSignal, signal_id) for signal_id in decision.market_signal_ids]
            payload = {
                "profile": {k: profile.get(k) for k in ["primary_goal", "emerging_interests", "skill_gaps",
                    "difficulty_preference", "budget_max", "weekly_hours", "high_intent_signals"]},
                "verified_evidence_event_ids": profile.get("evidence_event_ids", []),
                "candidates": [{"product_id": c.candidate.product.id, "title": c.candidate.product.title,
                    "category": c.candidate.product.category, "difficulty": c.candidate.product.difficulty,
                    "price": float(c.candidate.product.price), "skills": c.candidate.product.skills,
                    "score": c.final_score} for c in candidates],
                "fixed_next_best_action": {
                    "action_type": decision.action_type,
                    "persuasion_strategy": decision.persuasion_strategy,
                    "policy_headline": decision.headline,
                    "grounded_draft": decision.message,
                    "product": ({
                        "product_id": product.id,
                        "title": product.title,
                        "price": selected_price,
                        "catalog_price": float(product.price),
                        "ownership_credit": bundle_context.get("ownership_credit", 0),
                        "owned_component_count": bundle_context.get("owned_component_count", 0),
                        "remaining_component_count": bundle_context.get("remaining_component_count"),
                        "original_price": float(product.original_price) if product.original_price else None,
                        "duration_hours": product.duration_hours,
                        "skills": product.skills,
                        "career_outcomes": product.career_outcomes or [],
                        "social_proof_text": product.social_proof_text,
                        "verified_promotion_ends_at": (
                            product.promotion_ends_at.isoformat() if product.promotion_ends_at else None
                        ),
                    } if product else None),
                    "market_signals": [{
                        "claim": signal.claim,
                        "source_name": signal.source_name,
                        "source_url": signal.source_url,
                    } for signal in market_signals if signal],
                },
            }
            degraded = state.get("degraded", False)
            try:
                output = self.mesh.structured_completion(SYSTEM_PROMPT, json.dumps(payload))
            except MeshUnavailable:
                degraded = True
                MESH_AVOIDED.labels(reason="mesh_unavailable_deterministic_copy").inc()
                output = self._deterministic_output(profile, candidates, decision)
            return {"generated_output": output, "degraded": degraded,
                    "node_trace": state["node_trace"] + [{"node": "generate", "latency_ms": (time.perf_counter()-started)*1000,
                                                          "mode": "fallback" if degraded else "mesh"}]}

        def validate_node(state: RecommendationState) -> dict:
            allowed = {x.candidate.product.id for x in state["reranked_products"]}
            evidence = set(state["behavior_profile"].get("evidence_event_ids", []))
            decision: ActionDecision = state["action_decision"]
            allow_empty = not allowed and decision.action_type == "show_nothing"
            valid, errors = validate_grounding(
                state["generated_output"], allowed, evidence, allow_empty=allow_empty
            )
            action_valid, action_errors = validate_action_copy(
                state["generated_output"],
                decision.product.title if decision.product else None,
                self._allowed_action_numbers(decision, state["behavior_profile"], db),
            )
            valid = valid and action_valid
            errors.extend(action_errors)
            if not valid:
                GROUNDING_FAILURES.inc()
                output = self._deterministic_output(
                    state["behavior_profile"], state["reranked_products"], decision
                )
                valid, errors = validate_grounding(output, allowed, evidence, allow_empty=allow_empty)
                action_valid, action_errors = validate_action_copy(
                    output,
                    decision.product.title if decision.product else None,
                    self._allowed_action_numbers(decision, state["behavior_profile"], db),
                )
                valid = valid and action_valid
                errors.extend(action_errors)
                return {"generated_output": output, "grounding_valid": valid, "grounding_errors": errors,
                        "degraded": True, "node_trace": state["node_trace"] + [{"node": "validate_and_repair", "valid": valid}]}
            return {"grounding_valid": True, "grounding_errors": [],
                    "node_trace": state["node_trace"] + [{"node": "validate", "valid": True}]}

        def store_node(state: RecommendationState) -> dict:
            output = state["generated_output"]
            profile = state["behavior_profile"]
            decision: ActionDecision = state["action_decision"]
            ranked_by_id = {x.candidate.product.id: x for x in state["reranked_products"]}
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.settings.recommendation_ttl_minutes)
            recommendation = Recommendation(
                user_id=state["user_id"], behavior_profile_version=profile["profile_version"],
                profile_hash=profile["profile_hash"], headline=output["headline"][:180],
                narrative=output["narrative"][:700], reason_summary=f"Based on {len(profile.get('evidence_event_ids', []))} recent signals",
                confidence=float(profile.get("confidence", .5)), trigger_type=state["trigger_type"],
                model_name=(self.settings.mesh_model if self.mesh.available else "deterministic-policy"),
                prompt_version=PROMPT_VERSION, degraded=state.get("degraded", False),
                expires_at=expires_at,
            )
            db.add(recommendation)
            db.flush()
            for rank, item in enumerate(output["recommendations"][:5], 1):
                candidate = ranked_by_id.get(item["product_id"])
                if not candidate:
                    continue
                db.add(RecommendationItem(
                    recommendation_id=recommendation.id, product_id=item["product_id"], rank=rank,
                    retrieval_score=candidate.candidate.retrieval_score, rerank_score=candidate.final_score,
                    final_score=candidate.final_score, reason=item["reason"][:500],
                    evidence_event_ids=item.get("evidence_event_ids", [])[:10], score_breakdown=candidate.breakdown,
                ))
            action_copy = output["action_copy"]
            next_best_action = NextBestAction(
                user_id=state["user_id"],
                recommendation_id=recommendation.id,
                product_id=decision.product.id if decision.product else None,
                action_type=decision.action_type,
                persuasion_strategy=decision.persuasion_strategy,
                headline=action_copy["headline"][:180],
                message=action_copy["message"][:1200],
                rationale=decision.rationale[:1200],
                intent_stage=decision.intent_stage,
                purchase_propensity=decision.purchase_propensity,
                expected_conversion_probability=decision.expected_conversion_probability,
                expected_revenue=decision.expected_revenue,
                incremental_expected_revenue=decision.incremental_expected_revenue,
                evidence_event_ids=decision.evidence_event_ids,
                market_signal_ids=decision.market_signal_ids,
                policy_context=decision.policy_context,
                policy_version=decision.policy_version,
                status=(
                    "scheduled" if decision.deliver_at else
                    "suppressed" if decision.action_type in {"show_nothing", "delay_recommendation"} else
                    "active"
                ),
                deliver_at=decision.deliver_at,
                expires_at=expires_at,
            )
            db.add(next_best_action)
            db.commit()
            NBA_DECISIONS.labels(
                action=decision.action_type, strategy=decision.persuasion_strategy
            ).inc()
            NBA_PROPENSITY.labels(stage=decision.intent_stage).observe(decision.purchase_propensity)
            NBA_EXPECTED_REVENUE.labels(action=decision.action_type).observe(float(decision.expected_revenue))
            return {
                "recommendation_id": recommendation.id,
                "next_best_action_id": next_best_action.id,
                "node_trace": state["node_trace"] + [{
                    "node": "store",
                    "recommendation_id": recommendation.id,
                    "next_best_action_id": next_best_action.id,
                }],
            }

        workflow.add_node("assess_intent", assess_intent_node)
        workflow.add_node("retrieve", retrieve_node)
        workflow.add_node("rerank", rerank_node)
        workflow.add_node("decide_action", decide_action_node)
        workflow.add_node("select_persuasion", select_persuasion_node)
        workflow.add_node("generate", generate_node)
        workflow.add_node("validate", validate_node)
        workflow.add_node("store", store_node)
        workflow.add_edge(START, "assess_intent")
        workflow.add_edge("assess_intent", "retrieve")
        workflow.add_edge("retrieve", "rerank")
        workflow.add_edge("rerank", "decide_action")
        workflow.add_edge("decide_action", "select_persuasion")
        workflow.add_edge("select_persuasion", "generate")
        workflow.add_edge("generate", "validate")
        workflow.add_edge("validate", "store")
        workflow.add_edge("store", END)
        return workflow.compile()

    @staticmethod
    def _deterministic_output(
        profile: dict, candidates: list[RankedCandidate], decision: ActionDecision
    ) -> dict:
        interests = ", ".join(profile.get("emerging_interests", [])[:2]) or "your recent interests"
        evidence = profile.get("evidence_event_ids", [])[:3]
        narrative = (
            f"Your recent learning signals point toward {interests}. These courses match that direction while respecting your current difficulty and budget preferences."
            if candidates else
            "SmartReco is waiting because it cannot verify a useful catalog match for your current direction and constraints yet."
        )
        return {
            "headline": f"A focused next step for {interests}",
            "narrative": narrative,
            "recommendations": [{
                "product_id": item.candidate.product.id,
                "reason": f"Matches your interest in {item.candidate.product.category} and develops {', '.join(item.candidate.product.skills[:2])}.",
                "evidence_event_ids": evidence,
                "confidence": round(item.final_score, 3),
            } for item in candidates[:5]],
            "action_copy": {
                "headline": decision.headline,
                "message": decision.message,
            },
        }

    @staticmethod
    def _allowed_action_numbers(
        decision: ActionDecision, profile: dict, db: Session
    ) -> set[str]:
        values = {str(profile.get("weekly_hours", ""))}
        if decision.product:
            product = decision.product
            values.update({
                str(float(product.price)), str(round(float(product.price))),
                str(product.duration_hours), str(round(product.duration_hours)),
                str(product.savings_percent), f"{product.savings_percent}%",
            })
            if product.original_price:
                values.update({str(float(product.original_price)), str(round(float(product.original_price)))})
            bundle_context = decision.policy_context.get("bundle_optimizer") or {}
            for key in (
                "catalog_price",
                "personalized_price",
                "ownership_credit",
                "remaining_standalone_subtotal",
                "owned_component_count",
                "remaining_component_count",
                "discount_percent",
            ):
                value = bundle_context.get(key)
                if value is not None:
                    values.update({str(value), str(round(float(value)))})
            if product.promotion_ends_at:
                values.update(re.findall(r"\d+", product.promotion_ends_at.isoformat()))
        for signal_id in decision.market_signal_ids:
            signal = db.get(MarketSignal, signal_id)
            if signal:
                values.update(re.findall(r"\d[\d,]*(?:\.\d+)?%?", signal.claim))
        return {value for value in values if value}

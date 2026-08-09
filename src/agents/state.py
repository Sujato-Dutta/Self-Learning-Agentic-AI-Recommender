from typing import Any, TypedDict


class RecommendationState(TypedDict, total=False):
    user_id: str
    trigger_type: str
    behavior_profile: dict[str, Any]
    profile_hash: str
    cached_recommendation_id: str | None
    retrieval_query: str
    candidates: list[Any]
    reranked_products: list[Any]
    retrieval_quality: str
    intent_assessment: Any
    action_decision: Any
    persuasion_context: dict[str, Any]
    generated_output: dict[str, Any]
    grounding_valid: bool
    grounding_errors: list[str]
    recommendation_id: str
    next_best_action_id: str
    degraded: bool
    errors: list[str]
    retry_count: int
    node_trace: list[dict[str, Any]]

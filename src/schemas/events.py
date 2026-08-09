from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ALLOWED_EVENT_TYPES = {
    "landing_visit", "card_impression", "card_hover", "product_view", "search",
    "search_click", "category_filter", "difficulty_filter", "price_filter", "sort_change",
    "wishlist_add", "wishlist_remove", "cta_click", "enrollment", "recommendation_impression",
    "recommendation_click", "recommendation_dismiss", "not_interested", "time_spent",
    "scroll_depth", "return_visit", "studio_change", "profile_correction",
}
ALLOWED_METADATA = {
    "category", "difficulty", "price", "sort", "dwell_seconds", "scroll_percent", "source",
    "position", "recommendation_id", "value", "label", "progress", "trigger", "simulated",
    "decision_id", "action_type", "persuasion_strategy",
}


class EventIn(BaseModel):
    event_id: str = Field(min_length=8, max_length=100)
    session_id: str = Field(min_length=8, max_length=100)
    event_type: str
    product_id: str | None = Field(None, max_length=36)
    search_query: str | None = Field(None, max_length=250)
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("event_type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        if value not in ALLOWED_EVENT_TYPES:
            raise ValueError("unsupported event type")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in value.items() if k in ALLOWED_METADATA}
        if len(str(clean)) > 4000:
            raise ValueError("metadata is too large")
        return clean


class EventBatch(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=100)


class FeedbackIn(BaseModel):
    product_id: str
    recommendation_id: str | None = None
    feedback_type: Literal["dismiss", "not_interested", "helpful", "clicked"]

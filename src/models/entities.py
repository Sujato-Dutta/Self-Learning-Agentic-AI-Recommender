import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from src.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid4str() -> str:
    return str(uuid.uuid4())


class GUID(TypeDecorator):
    """String UUIDs locally and native UUID parameters on PostgreSQL/Supabase."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PostgreSQLUUID(as_uuid=False))
        return dialect.type_descriptor(String(36))


class Role(str, enum.Enum):
    user = "user"
    admin = "admin"


class OutboxStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    complete = "complete"
    failed = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role"), default=Role.user, index=True)
    personalization_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    title: Mapped[str] = mapped_column(String(180), index=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(80), index=True)
    difficulty: Mapped[str] = mapped_column(String(30), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    duration_hours: Mapped[float] = mapped_column(Float, default=1)
    skill_bundle: Mapped[str] = mapped_column(String(80), default="Python Engineering", index=True)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    content_sections: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    image_url: Mapped[str] = mapped_column(String(500), default="")
    is_bundle: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    bundled_product_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    prerequisite_product_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    career_outcomes: Mapped[list[str]] = mapped_column(JSON, default=list)
    social_proof_text: Mapped[str | None] = mapped_column(String(500))
    promotion_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    popularity: Mapped[float] = mapped_column(Float, default=0.5)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def savings_percent(self) -> int:
        if not self.original_price or self.original_price <= self.price:
            return 0
        return round((1 - float(self.price / self.original_price)) * 100)


class UserEnrollment(Base):
    __tablename__ = "user_enrollments"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_user_enrollment_product"),
        Index("ix_user_enrollments_user_purchased", "user_id", "purchased_at"),
    )

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    source_product_id: Mapped[str | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True
    )
    purchased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    product: Mapped[Product] = relationship(foreign_keys=[product_id], lazy="joined")
    source_product: Mapped[Product | None] = relationship(
        foreign_keys=[source_product_id], lazy="joined"
    )


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_events_event_id"),
        Index("ix_events_user_occurred", "user_id", "occurred_at"),
        Index("ix_events_session", "session_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    event_id: Mapped[str] = mapped_column(String(100))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    search_query: Mapped[str | None] = mapped_column(String(250))
    event_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BehaviorProfile(Base):
    __tablename__ = "behavior_profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_behavior_profile_user"),)

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    profile_hash: Mapped[str] = mapped_column(String(64), index=True)
    profile_data: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    corrections: Mapped[dict] = mapped_column(JSON, default=dict)
    timeline: Mapped[list[dict]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (Index("ix_reco_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    behavior_profile_version: Mapped[int] = mapped_column(Integer)
    profile_hash: Mapped[str] = mapped_column(String(64), index=True)
    headline: Mapped[str] = mapped_column(String(180))
    narrative: Mapped[str] = mapped_column(Text)
    reason_summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="active")
    trigger_type: Mapped[str] = mapped_column(String(60))
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(30), default="v1")
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    items: Mapped[list["RecommendationItem"]] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan", order_by="RecommendationItem.rank"
    )
    next_best_action: Mapped["NextBestAction"] = relationship(
        back_populates="recommendation", cascade="all, delete-orphan", uselist=False
    )


class RecommendationItem(Base):
    __tablename__ = "recommendation_items"
    __table_args__ = (UniqueConstraint("recommendation_id", "product_id", name="uq_reco_product"),)

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id", ondelete="CASCADE"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    rank: Mapped[int] = mapped_column(Integer)
    retrieval_score: Mapped[float] = mapped_column(Float)
    rerank_score: Mapped[float] = mapped_column(Float)
    final_score: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    evidence_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    recommendation: Mapped[Recommendation] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()


class RecommendationFeedback(Base):
    __tablename__ = "recommendation_feedback"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recommendation_id: Mapped[str | None] = mapped_column(ForeignKey("recommendations.id", ondelete="SET NULL"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    feedback_type: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarketSignal(Base):
    __tablename__ = "market_signals"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    name: Mapped[str] = mapped_column(String(180), unique=True)
    claim: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(String(180))
    source_url: Mapped[str] = mapped_column(String(700))
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NextBestAction(Base):
    __tablename__ = "next_best_actions"
    __table_args__ = (
        UniqueConstraint("recommendation_id", name="uq_nba_recommendation"),
        Index("ix_nba_user_created", "user_id", "created_at"),
        Index("ix_nba_status_delivery", "status", "deliver_at"),
        CheckConstraint("purchase_propensity >= 0 AND purchase_propensity <= 1", name="ck_nba_propensity"),
        CheckConstraint(
            "expected_conversion_probability >= 0 AND expected_conversion_probability <= 1",
            name="ck_nba_conversion_probability",
        ),
    )

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recommendation_id: Mapped[str] = mapped_column(ForeignKey("recommendations.id", ondelete="CASCADE"))
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    action_type: Mapped[str] = mapped_column(String(50), index=True)
    persuasion_strategy: Mapped[str] = mapped_column(String(50), index=True)
    headline: Mapped[str] = mapped_column(String(180))
    message: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    intent_stage: Mapped[str] = mapped_column(String(30), index=True)
    purchase_propensity: Mapped[float] = mapped_column(Float)
    expected_conversion_probability: Mapped[float] = mapped_column(Float)
    expected_revenue: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    incremental_expected_revenue: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    evidence_event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    market_signal_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy_context: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_version: Mapped[str] = mapped_column(String(30), default="nba-v1")
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    deliver_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cumulative_reward: Mapped[float] = mapped_column(Float, default=0)
    outcome_type: Mapped[str | None] = mapped_column(String(50))
    outcome_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    recommendation: Mapped[Recommendation] = relationship(back_populates="next_best_action")
    product: Mapped[Product | None] = relationship()
    rewards: Mapped[list["NextBestActionReward"]] = relationship(
        back_populates="decision", cascade="all, delete-orphan"
    )


class NextBestActionReward(Base):
    __tablename__ = "next_best_action_rewards"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_nba_reward_event"),
        CheckConstraint("reward >= -2 AND reward <= 2", name="ck_nba_reward_range"),
    )

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    decision_id: Mapped[str] = mapped_column(ForeignKey("next_best_actions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    event_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    reward: Mapped[float] = mapped_column(Float)
    event_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decision: Mapped[NextBestAction] = relationship(back_populates="rewards")


class ProductVectorOutbox(Base):
    __tablename__ = "product_vector_outbox"
    __table_args__ = (Index("ix_outbox_status_next", "status", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    operation: Mapped[str] = mapped_column(String(20))
    product_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status"), default=OutboxStatus.pending
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductSyncFailure(Base):
    __tablename__ = "product_sync_failures"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    outbox_id: Mapped[str] = mapped_column(GUID(), index=True)
    product_id: Mapped[str] = mapped_column(GUID(), index=True)
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    trigger_type: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(30))
    trace_url: Mapped[str | None] = mapped_column(String(500))
    node_trace: Mapped[list[dict]] = mapped_column(JSON, default=list)
    retrieval_query: Mapped[str | None] = mapped_column(Text)
    candidates: Mapped[list[dict]] = mapped_column(JSON, default=list)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    mesh_called: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_hour: Mapped[int] = mapped_column(Integer, default=17)
    timezone: Mapped[str] = mapped_column(String(80), default="UTC")
    unsubscribe_token: Mapped[str] = mapped_column(String(64), default=lambda: uuid.uuid4().hex, unique=True)


class ScheduledDelivery(Base):
    __tablename__ = "scheduled_deliveries"

    id: Mapped[str] = mapped_column(GUID(), primary_key=True, default=uuid4str)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recommendation_id: Mapped[str | None] = mapped_column(ForeignKey("recommendations.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(30))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

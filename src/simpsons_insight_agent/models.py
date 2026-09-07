from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def uuid4_str() -> str:
    return str(uuid.uuid4())


ACTIVE_JOB_STATUSES = {
    "PENDING_COLLECTION",
    "COLLECTING",
    "LOCAL_ANALYSIS",
    "EMBEDDING",
    "CLOUD_ANALYSIS",
    "REPORTING",
    "WAITING_FOR_USER",
    "ANALYSIS_PENDING",
}

TERMINAL_JOB_STATUSES = {
    "COMPLETED",
    "PARTIAL",
    "INTERRUPTED",
    "BLOCKED",
    "FAILED",
    "CANCELED",
}

STREAM_END_JOB_STATUSES = TERMINAL_JOB_STATUSES | {
    "READY_FOR_ANALYSIS",
    "COLLECTION_INTERRUPTED",
}


class Base(DeclarativeBase):
    pass


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    maps_url: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(20), default="business")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    subject_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    address: Mapped[str | None] = mapped_column(Text)
    average_rating: Mapped[float | None] = mapped_column(Float)
    total_review_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    jobs: Mapped[list[CrawlJob]] = relationship(back_populates="business")
    reviews: Mapped[list[Review]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    auto_plan: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    planning_options: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(40), default="PENDING_COLLECTION", index=True)
    max_reviews: Mapped[int] = mapped_column(Integer, default=500)
    sort_order: Mapped[str] = mapped_column(String(20), default="newest")
    headless: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_model: Mapped[str] = mapped_column(String(100))
    collected_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    collection_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    collection_stop_reason: Mapped[str | None] = mapped_column(String(100))
    last_completed_stage: Mapped[str | None] = mapped_column(String(40))
    degraded_reasons: Mapped[list] = mapped_column(JSON, default=list)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped[Business] = relationship(back_populates="jobs")
    report: Mapped[Report | None] = relationship(
        back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    review_links: Mapped[list[JobReview]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    source_runs: Mapped[list[JobSource]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobSource.ordinal"
    )
    source_imports: Mapped[list[SourceImport]] = relationship(back_populates="job")


class JobSource(Base):
    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("job_id", "source", name="uq_job_source"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(30), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    collected_count: Mapped[int] = mapped_column(Integer, default=0)
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    target_count: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    collection_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_reason: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    job: Mapped[CrawlJob] = relationship(back_populates="source_runs")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("business_id", "content_hash", name="uq_review_business_hash"),
        UniqueConstraint("business_id", "source_review_id", name="uq_review_business_source"),
        UniqueConstraint(
            "business_id", "source", "source_item_id", name="uq_content_subject_source_item"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    source_review_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="google_maps", index=True)
    content_type: Mapped[str] = mapped_column(String(20), default="review", index=True)
    source_item_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thread_source_id: Mapped[str | None] = mapped_column(String(500), index=True)
    parent_source_id: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(Text)
    board: Mapped[str | None] = mapped_column(String(200), index=True)
    author_hash: Mapped[str | None] = mapped_column(String(64))
    platform_data: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    author_name: Mapped[str | None] = mapped_column(String(500))
    rating: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="")
    relative_date: Mapped[str | None] = mapped_column(String(200))
    published_at_estimated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_precision: Mapped[str] = mapped_column(String(20), default="unknown")
    owner_reply: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(20))
    redacted_text: Mapped[str | None] = mapped_column(Text)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    business: Mapped[Business] = relationship(back_populates="reviews")
    job_links: Mapped[list[JobReview]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )


class SourceImport(Base):
    __tablename__ = "source_imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source: Mapped[str] = mapped_column(String(30), default="dcard", index=True)
    filename: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[list] = mapped_column(JSON, default=list)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    validation_status: Mapped[str] = mapped_column(String(30), default="VALID")
    validation_errors: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[CrawlJob | None] = relationship(back_populates="source_imports")


class JobReview(Base):
    __tablename__ = "job_reviews"

    job_id: Mapped[str] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[CrawlJob] = relationship(back_populates="review_links")
    review: Mapped[Review] = relationship(back_populates="job_links")


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewAnalysis(Base):
    __tablename__ = "job_review_analyses"
    __table_args__ = (
        UniqueConstraint("job_id", "review_id", name="uq_job_review_analysis"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), index=True
    )
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )
    sentiment: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sentiment_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    rating_sentiment: Mapped[str | None] = mapped_column(String(30))
    rating_text_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    negative_aspects: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    aspects: Mapped[list] = mapped_column(JSON, default=list)
    key_points: Mapped[list] = mapped_column(JSON, default=list)
    local_model_id: Mapped[str] = mapped_column(String(300))
    cloud_model_id: Mapped[str | None] = mapped_column(String(100))
    cloud_status: Mapped[str] = mapped_column(String(30), default="PENDING")
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("crawl_jobs.id", ondelete="CASCADE"), unique=True, index=True
    )
    business_id: Mapped[str] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), index=True
    )
    model_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="READY")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    job: Mapped[CrawlJob] = relationship(back_populates="report")
    chat_sessions: Mapped[list[ChatSession]] = relationship(
        back_populates="report", cascade="all, delete-orphan"
    )


class ReviewEmbedding(Base):
    __tablename__ = "review_embedding_versions"
    __table_args__ = (
        UniqueConstraint("review_id", "model_id", name="uq_review_embedding_model"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )
    model_id: Mapped[str] = mapped_column(String(300))
    dimension: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    report: Mapped[Report] = relationship(back_populates="chat_sessions")
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    evidence_review_ids: Mapped[list] = mapped_column(JSON, default=list)
    limitations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class TopicVersion(Base):
    __tablename__ = "topic_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), index=True)
    topic_key: Mapped[str] = mapped_column(String(36), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class DecisionPlan(Base):
    __tablename__ = "decision_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    diagnosis: Mapped[dict] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (UniqueConstraint("plan_id", "stage", name="uq_plan_stage"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    plan_id: Mapped[str] = mapped_column(ForeignKey("decision_plans.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    tools: Mapped[list] = mapped_column(JSON, default=list)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class ImprovementTask(Base):
    __tablename__ = "improvement_tasks"
    __table_args__ = (UniqueConstraint("plan_id", "task_key", name="uq_plan_task"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    plan_id: Mapped[str] = mapped_column(ForeignKey("decision_plans.id", ondelete="CASCADE"), index=True)
    task_key: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class PlanEvaluation(Base):
    __tablename__ = "plan_evaluations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    plan_id: Mapped[str] = mapped_column(ForeignKey("decision_plans.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BrandComparison(Base):
    __tablename__ = "brand_comparisons"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4_str)
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

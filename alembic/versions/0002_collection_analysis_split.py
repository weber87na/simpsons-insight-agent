"""Split collection snapshots from job-scoped analysis results.

Revision ID: 0002_collection_analysis_split
Revises: 0001_initial
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict

import sqlalchemy as sa

from alembic import op

revision = "0002_collection_analysis_split"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns("crawl_jobs")
    additions = (
        ("collection_complete", sa.Boolean(), sa.false()),
        ("collection_stop_reason", sa.String(length=100), None),
        ("last_completed_stage", sa.String(length=40), None),
        ("degraded_reasons", sa.JSON(), sa.text("'[]'")),
        ("attempt_count", sa.Integer(), sa.text("'0'")),
    )
    for name, type_, default in additions:
        if name not in columns:
            op.add_column(
                "crawl_jobs",
                sa.Column(name, type_, nullable=default is None, server_default=default),
            )

    tables = _tables()
    if "job_reviews" not in tables:
        op.create_table(
            "job_reviews",
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("review_id", sa.String(length=36), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["crawl_jobs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("job_id", "review_id"),
        )
    if "job_review_analyses" not in tables:
        op.create_table(
            "job_review_analyses",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("job_id", sa.String(length=36), nullable=False),
            sa.Column("review_id", sa.String(length=36), nullable=False),
            sa.Column("sentiment", sa.String(length=30), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("sentiment_scores", sa.JSON(), nullable=False),
            sa.Column("rating_sentiment", sa.String(length=30)),
            sa.Column("rating_text_conflict", sa.Boolean(), nullable=False),
            sa.Column("aspects", sa.JSON(), nullable=False),
            sa.Column("key_points", sa.JSON(), nullable=False),
            sa.Column("local_model_id", sa.String(length=300), nullable=False),
            sa.Column("cloud_model_id", sa.String(length=100)),
            sa.Column("cloud_status", sa.String(length=30), nullable=False),
            sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["crawl_jobs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("job_id", "review_id", name="uq_job_review_analysis"),
        )
        op.create_index("ix_job_review_analyses_job_id", "job_review_analyses", ["job_id"])
        op.create_index("ix_job_review_analyses_review_id", "job_review_analyses", ["review_id"])
    if "review_embedding_versions" not in tables:
        op.create_table(
            "review_embedding_versions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("review_id", sa.String(length=36), nullable=False),
            sa.Column("model_id", sa.String(length=300), nullable=False),
            sa.Column("dimension", sa.Integer(), nullable=False),
            sa.Column("vector", sa.LargeBinary(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["review_id"], ["reviews.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("review_id", "model_id", name="uq_review_embedding_model"),
        )
        op.create_index(
            "ix_review_embedding_versions_review_id",
            "review_embedding_versions",
            ["review_id"],
        )

    _backfill_job_reviews(bind)
    _backfill_legacy_results(bind)
    _normalize_legacy_jobs(bind)


def _backfill_job_reviews(bind: sa.Connection) -> None:
    existing = bind.execute(sa.text("SELECT COUNT(*) FROM job_reviews")).scalar_one()
    if existing:
        return
    jobs_by_business: dict[str, list[str]] = defaultdict(list)
    for row in bind.execute(
        sa.text("SELECT id, business_id FROM crawl_jobs ORDER BY created_at, id")
    ).mappings():
        jobs_by_business[row["business_id"]].append(row["id"])

    links: dict[str, list[str]] = defaultdict(list)
    for business_id, job_ids in jobs_by_business.items():
        if len(job_ids) == 1:
            rows = bind.execute(
                sa.text(
                    "SELECT id FROM reviews WHERE business_id=:business_id "
                    "ORDER BY scraped_at, id"
                ),
                {"business_id": business_id},
            )
            links[job_ids[0]].extend(row[0] for row in rows)

    if "reports" in _tables():
        for row in bind.execute(sa.text("SELECT job_id, payload FROM reports")).mappings():
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            for review_id in (payload or {}).get("review_ids", []):
                if review_id not in links[row["job_id"]]:
                    links[row["job_id"]].append(review_id)

    for job_id, review_ids in links.items():
        for ordinal, review_id in enumerate(review_ids, start=1):
            bind.execute(
                sa.text(
                    "INSERT INTO job_reviews(job_id, review_id, ordinal, observed_at) "
                    "VALUES (:job_id, :review_id, :ordinal, CURRENT_TIMESTAMP)"
                ),
                {"job_id": job_id, "review_id": review_id, "ordinal": ordinal},
            )


def _backfill_legacy_results(bind: sa.Connection) -> None:
    tables = _tables()
    if "review_analyses" in tables:
        old = sa.Table("review_analyses", sa.MetaData(), autoload_with=bind)
        for row in bind.execute(sa.select(old)).mappings():
            job_ids = bind.execute(
                sa.text("SELECT job_id FROM job_reviews WHERE review_id=:review_id"),
                {"review_id": row["review_id"]},
            ).scalars()
            for job_id in job_ids:
                values = dict(row)
                values.update(id=str(uuid.uuid4()), job_id=job_id)
                columns = ", ".join(values)
                placeholders = ", ".join(f":{key}" for key in values)
                bind.execute(
                    sa.text(
                        f"INSERT INTO job_review_analyses ({columns}) VALUES ({placeholders})"
                    ),
                    values,
                )
    if "review_embeddings" in tables:
        old = sa.Table("review_embeddings", sa.MetaData(), autoload_with=bind)
        for row in bind.execute(sa.select(old)).mappings():
            exists = bind.execute(
                sa.text(
                    "SELECT 1 FROM review_embedding_versions "
                    "WHERE review_id=:review_id AND model_id=:model_id"
                ),
                {"review_id": row["review_id"], "model_id": row["model_id"]},
            ).first()
            if not exists:
                bind.execute(
                    sa.text(
                        "INSERT INTO review_embedding_versions"
                        "(id, review_id, model_id, dimension, vector, created_at) "
                        "VALUES (:id, :review_id, :model_id, :dimension, :vector, :created_at)"
                    ),
                    dict(row),
                )


def _normalize_legacy_jobs(bind: sa.Connection) -> None:
    bind.execute(
        sa.text(
            "UPDATE crawl_jobs SET status='PENDING_COLLECTION' "
            "WHERE status IN ('PENDING', 'SEARCHING', 'WAITING_SELECTION')"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE crawl_jobs SET status='COLLECTION_INTERRUPTED', "
            "collection_complete=0, collection_stop_reason='legacy_interrupted', "
            "last_completed_stage=NULL "
            "WHERE collected_count > 0 AND status IN "
            "('CRAWLING', 'INTERRUPTED', 'BLOCKED', 'FAILED', 'PARTIAL') "
            "AND NOT EXISTS (SELECT 1 FROM reports WHERE reports.job_id=crawl_jobs.id)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE crawl_jobs SET collection_complete=1, last_completed_stage='REPORTING' "
            "WHERE EXISTS (SELECT 1 FROM reports WHERE reports.job_id=crawl_jobs.id)"
        )
    )


def downgrade() -> None:
    tables = _tables()
    if "review_embedding_versions" in tables:
        op.drop_table("review_embedding_versions")
    if "job_review_analyses" in tables:
        op.drop_table("job_review_analyses")
    if "job_reviews" in tables:
        op.drop_table("job_reviews")
    columns = _columns("crawl_jobs")
    with op.batch_alter_table("crawl_jobs") as batch:
        for name in (
            "attempt_count",
            "degraded_reasons",
            "last_completed_stage",
            "collection_stop_reason",
            "collection_complete",
        ):
            if name in columns:
                batch.drop_column(name)

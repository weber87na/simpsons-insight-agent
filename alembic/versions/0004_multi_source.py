"""add multi-source collection support

Revision ID: 0004_multi_source
Revises: 0003_job_events
"""

from __future__ import annotations

import hashlib
import json
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0004_multi_source"
down_revision = "0003_job_events"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    names = {item["name"] for item in inspector.get_indexes(table)}
    names.update(item.get("name") for item in inspector.get_unique_constraints(table))
    return {name for name in names if name}


def upgrade() -> None:
    connection = op.get_bind()
    maps_url_column = next(
        item for item in sa.inspect(connection).get_columns("businesses") if item["name"] == "maps_url"
    )
    if not maps_url_column["nullable"]:
        with op.batch_alter_table("businesses") as batch:
            batch.alter_column("maps_url", existing_type=sa.Text(), nullable=True)
    business_columns = _columns("businesses")
    business_additions = (
        (
            "subject_kind",
            sa.Column(
                "subject_kind",
                sa.String(length=20),
                nullable=False,
                server_default="business",
            ),
        ),
        ("aliases", sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]")),
        ("subject_key", sa.Column("subject_key", sa.String(length=64), nullable=True)),
    )
    for name, column in business_additions:
        if name not in business_columns:
            op.add_column("businesses", column)
    if "uq_business_subject_key" not in _indexes("businesses"):
        op.create_index(
            "uq_business_subject_key", "businesses", ["subject_key"], unique=True
        )
    businesses = connection.execute(
        sa.text("SELECT id, name, address FROM businesses WHERE subject_key IS NULL")
    ).fetchall()
    used_keys = set(
        connection.execute(
            sa.text("SELECT subject_key FROM businesses WHERE subject_key IS NOT NULL")
        ).scalars()
    )
    for business_id, name, address in businesses:
        normalized = "|".join(
            part.strip().casefold() for part in ("business", name or "", address or "")
        )
        subject_key = hashlib.sha256(normalized.encode()).hexdigest()
        if subject_key in used_keys:
            subject_key = hashlib.sha256(f"{normalized}|{business_id}".encode()).hexdigest()
        used_keys.add(subject_key)
        connection.execute(
            sa.text("UPDATE businesses SET subject_key = :key WHERE id = :id"),
            {"key": subject_key, "id": business_id},
        )

    if "job_sources" not in _tables():
        op.create_table(
            "job_sources",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "job_id",
                sa.String(length=36),
                sa.ForeignKey("crawl_jobs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source", sa.String(length=30), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
            sa.Column("collected_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("post_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("comment_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("target_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("checkpoint", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column(
                "collection_complete", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("stop_reason", sa.String(length=100), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("job_id", "source", name="uq_job_source"),
        )
        op.create_index("ix_job_sources_job_id", "job_sources", ["job_id"])
        op.create_index("ix_job_sources_source", "job_sources", ["source"])
        op.create_index("ix_job_sources_status", "job_sources", ["status"])

    review_columns = _columns("reviews")
    review_additions = (
        (
            "source",
            sa.Column(
                "source", sa.String(length=30), nullable=False, server_default="google_maps"
            ),
        ),
        (
            "content_type",
            sa.Column(
                "content_type", sa.String(length=20), nullable=False, server_default="review"
            ),
        ),
        ("source_item_id", sa.Column("source_item_id", sa.String(length=500))),
        ("thread_source_id", sa.Column("thread_source_id", sa.String(length=500))),
        ("parent_source_id", sa.Column("parent_source_id", sa.String(length=500))),
        ("title", sa.Column("title", sa.Text())),
        ("board", sa.Column("board", sa.String(length=200))),
        ("author_hash", sa.Column("author_hash", sa.String(length=64))),
        (
            "platform_data",
            sa.Column("platform_data", sa.JSON(), nullable=False, server_default="{}"),
        ),
    )
    for name, column in review_additions:
        if name not in review_columns:
            op.add_column("reviews", column)
    review_indexes = _indexes("reviews")
    indexes = (
        (
            "uq_content_subject_source_item",
            ["business_id", "source", "source_item_id"],
            True,
        ),
        ("ix_reviews_source", ["source"], False),
        ("ix_reviews_content_type", ["content_type"], False),
        ("ix_reviews_thread_source_id", ["thread_source_id"], False),
        ("ix_reviews_board", ["board"], False),
    )
    for name, columns, unique in indexes:
        if name not in review_indexes:
            op.create_index(name, "reviews", columns, unique=unique)
    connection.execute(
        sa.text(
            "UPDATE reviews SET source_item_id = source_review_id "
            "WHERE source_item_id IS NULL"
        )
    )

    if "source_imports" not in _tables():
        op.create_table(
            "source_imports",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "job_id",
                sa.String(length=36),
                sa.ForeignKey("crawl_jobs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("source", sa.String(length=30), nullable=False, server_default="dcard"),
            sa.Column("filename", sa.String(length=500), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "validation_status",
                sa.String(length=30),
                nullable=False,
                server_default="VALID",
            ),
            sa.Column(
                "validation_errors", sa.JSON(), nullable=False, server_default="[]"
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_source_imports_job_id", "source_imports", ["job_id"])
        op.create_index("ix_source_imports_source", "source_imports", ["source"])
        op.create_index("ix_source_imports_sha256", "source_imports", ["sha256"])
    else:
        import_columns = _columns("source_imports")
        if "job_id" not in import_columns:
            op.add_column(
                "source_imports", sa.Column("job_id", sa.String(length=36), nullable=True)
            )
            op.create_index("ix_source_imports_job_id", "source_imports", ["job_id"])
        if "validation_status" not in import_columns:
            op.add_column(
                "source_imports",
                sa.Column(
                    "validation_status",
                    sa.String(length=30),
                    nullable=False,
                    server_default="VALID",
                ),
            )
        if "validation_errors" not in import_columns:
            op.add_column(
                "source_imports",
                sa.Column(
                    "validation_errors", sa.JSON(), nullable=False, server_default="[]"
                ),
            )

    existing_job_ids = set(
        connection.execute(sa.text("SELECT job_id FROM job_sources WHERE source = 'google_maps'"))
        .scalars()
        .all()
    )
    jobs = connection.execute(
        sa.text(
            "SELECT id, status, max_reviews, sort_order, headless, collected_count, "
            "collection_complete, collection_stop_reason, attempt_count, created_at, updated_at "
            "FROM crawl_jobs"
        )
    ).mappings()
    for job in jobs:
        if job["id"] in existing_job_ids:
            continue
        if job["status"] in {"COLLECTING", "PENDING_COLLECTION", "WAITING_FOR_USER"}:
            source_status = "PENDING"
        elif job["collection_complete"]:
            source_status = "COMPLETE"
        elif job["collected_count"]:
            source_status = "PARTIAL"
        else:
            source_status = "FAILED" if job["status"] == "FAILED" else "PENDING"
        config = {
            "source": "google_maps",
            "maps_url": connection.execute(
                sa.text(
                    "SELECT businesses.maps_url FROM businesses "
                    "JOIN crawl_jobs ON crawl_jobs.business_id = businesses.id "
                    "WHERE crawl_jobs.id = :job_id"
                ),
                {"job_id": job["id"]},
            ).scalar_one(),
            "max_reviews": job["max_reviews"],
            "sort": job["sort_order"],
            "headless": bool(job["headless"]),
        }
        connection.execute(
            sa.text(
                "INSERT INTO job_sources "
                "(id, job_id, source, ordinal, config, status, collected_count, post_count, "
                "comment_count, target_count, checkpoint, collection_complete, stop_reason, "
                "error, attempt_count, created_at, updated_at) VALUES "
                "(:id, :job_id, 'google_maps', 0, :config, :status, :count, 0, 0, :target, "
                "'{}', :complete, :reason, NULL, :attempts, :created, :updated)"
            ),
            {
                "id": str(uuid.uuid4()),
                "job_id": job["id"],
                "config": json.dumps(config, ensure_ascii=False),
                "status": source_status,
                "count": job["collected_count"],
                "target": job["max_reviews"],
                "complete": job["collection_complete"],
                "reason": job["collection_stop_reason"],
                "attempts": job["attempt_count"],
                "created": job["created_at"],
                "updated": job["updated_at"],
            },
        )


def downgrade() -> None:
    # The downgrade is intentionally conservative because 0001 builds from current metadata.
    if "source_imports" in _tables():
        op.drop_table("source_imports")
    if "job_sources" in _tables():
        op.drop_table("job_sources")

"""Add persisted job performance events.

Revision ID: 0003_job_events
Revises: 0002_collection_analysis_split
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_job_events"
down_revision = "0002_collection_analysis_split"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "job_events" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["crawl_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])
    op.create_index("ix_job_events_event_type", "job_events", ["event_type"])


def downgrade() -> None:
    if "job_events" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("job_events")

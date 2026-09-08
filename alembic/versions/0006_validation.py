"""Add isolated service validation experiments; preserve existing decisions."""

import sqlalchemy as sa

from alembic import op

revision = "0006_validation"
down_revision = "0005_decision"
branch_labels = None
depends_on = None


def upgrade():
    # 0001 uses current metadata on fresh installs; existing databases need this DDL.
    def create_table(name, *columns):
        if name not in sa.inspect(op.get_bind()).get_table_names():
            op.create_table(name, *columns)

    def create_index(name, table, columns):
        if name not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
            op.create_index(name, table, columns)

    create_table(
        "validation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("decision_plans.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("stages", sa.JSON(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_table(
        "validation_experiments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("validation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("experiment_key", sa.String(80), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "experiment_key", name="uq_validation_experiment"),
    )
    create_index("ix_validation_experiments_run_id", "validation_experiments", ["run_id"])
    create_table(
        "validation_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("experiment_id", sa.String(36), sa.ForeignKey("validation_experiments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("submission_id", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("verdict", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("experiment_id", "submission_id", name="uq_validation_submission"),
    )
    create_index("ix_validation_results_experiment_id", "validation_results", ["experiment_id"])


def downgrade():
    op.drop_table("validation_results")
    op.drop_table("validation_experiments")
    op.drop_table("validation_runs")

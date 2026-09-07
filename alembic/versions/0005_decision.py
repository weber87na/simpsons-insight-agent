"""Decision workflow and topic snapshots (additive; keeps existing reports)."""

import sqlalchemy as sa

from alembic import op

revision = "0005_decision"
down_revision = "0004_multi_source"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("crawl_jobs")}
    if "auto_plan" not in columns:
        op.add_column(
            "crawl_jobs", sa.Column("auto_plan", sa.Boolean(), nullable=False, server_default="0")
        )
    if "planning_options" not in columns:
        op.add_column(
            "crawl_jobs",
            sa.Column("planning_options", sa.JSON(), nullable=False, server_default="{}"),
        )

    tables = sa.inspect(op.get_bind()).get_table_names()
    if "job_review_analyses" in tables and "negative_aspects" not in {
        c["name"] for c in sa.inspect(op.get_bind()).get_columns("job_review_analyses")
    }:
        op.add_column(
            "job_review_analyses",
            sa.Column("negative_aspects", sa.JSON(), nullable=False, server_default="[]"),
        )

    def table(name, *columns):
        if name in sa.inspect(op.get_bind()).get_table_names():
            return
        op.create_table(name, sa.Column("id", sa.String(36), primary_key=True), *columns)

    def fk(name, target, unique=False):
        return sa.Column(
            name,
            sa.String(36),
            sa.ForeignKey(target, ondelete="CASCADE"),
            nullable=False,
            unique=unique,
        )

    def js(name):
        return sa.Column(name, sa.JSON(), nullable=False)

    def st(name, size=30, nullable=False):
        return sa.Column(name, sa.String(size), nullable=nullable)

    def created():
        return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)

    table("topic_versions", fk("report_id", "reports.id"), st("topic_key", 36), js("payload"))
    table(
        "decision_plans",
        fk("report_id", "reports.id", True),
        st("status"),
        js("options"),
        js("diagnosis"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text()),
        created(),
    )
    table(
        "agent_runs",
        fk("plan_id", "decision_plans.id"),
        st("stage", 40),
        st("status"),
        js("input"),
        js("output"),
        js("tools"),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("plan_id", "stage", name="uq_plan_stage"),
    )
    table(
        "improvement_tasks",
        fk("plan_id", "decision_plans.id"),
        st("task_key", 80),
        js("payload"),
        sa.UniqueConstraint("plan_id", "task_key", name="uq_plan_task"),
    )
    table(
        "plan_evaluations",
        fk("plan_id", "decision_plans.id"),
        st("kind", 20),
        js("payload"),
        created(),
    )
    table("brand_comparisons", st("name", 200), js("config"), created())
    for name, column in [
        ("topic_versions", "report_id"),
        ("topic_versions", "topic_key"),
        ("agent_runs", "plan_id"),
        ("improvement_tasks", "plan_id"),
        ("plan_evaluations", "plan_id"),
    ]:
        if f"ix_{name}_{column}" not in {
            i["name"] for i in sa.inspect(op.get_bind()).get_indexes(name)
        }:
            op.create_index(f"ix_{name}_{column}", name, [column])


def downgrade():
    op.drop_column("job_review_analyses", "negative_aspects")
    for name in [
        "brand_comparisons",
        "plan_evaluations",
        "improvement_tasks",
        "agent_runs",
        "decision_plans",
        "topic_versions",
    ]:
        op.drop_table(name)
    op.drop_column("crawl_jobs", "planning_options")
    op.drop_column("crawl_jobs", "auto_plan")

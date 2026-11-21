"""job run log: every worker task / CLI command, linked from ingestion_run

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATUS = ("running", "ok", "problems", "failed")


def jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "job_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=True),
        sa.Column("worker", sa.String(200), nullable=True),
        sa.Column("status", sa.String(32), server_default="running", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        jsonb("params", "{}"),
        jsonb("result", "{}"),
        jsonb("problems", "[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_job_run"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in JOB_STATUS) + ")", name="ck_job_run_job_status"
        ),
    )
    op.create_index("ix_job_run_name_started", "job_run", ["name", sa.text("started_at DESC")])
    op.create_index("ix_job_run_status", "job_run", ["status"])
    op.create_index("ix_job_run_task_id", "job_run", ["task_id"])

    op.add_column("ingestion_run", sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index("ix_ingestion_run_job_id", "ingestion_run", ["job_id"])
    op.create_foreign_key(
        "fk_ingestion_run_job_id_job_run", "ingestion_run", "job_run", ["job_id"], ["id"], ondelete="SET NULL"
    )


def downgrade() -> None:
    op.drop_constraint("fk_ingestion_run_job_id_job_run", "ingestion_run", type_="foreignkey")
    op.drop_index("ix_ingestion_run_job_id", table_name="ingestion_run")
    op.drop_column("ingestion_run", "job_id")
    op.drop_index("ix_job_run_task_id", table_name="job_run")
    op.drop_index("ix_job_run_status", table_name="job_run")
    op.drop_index("ix_job_run_name_started", table_name="job_run")
    op.drop_table("job_run")

"""ingestion runs, document metadata, passage embedding model

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INGESTION_STATUS = ("running", "ok", "partial", "failed", "skipped")


def jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "ingestion_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), server_default="running", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        jsonb("counts", "{}"),
        jsonb("errors", "[]"),
        jsonb("params", "{}"),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_run"),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name="fk_ingestion_run_source_id_source", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in INGESTION_STATUS) + ")",
            name="ck_ingestion_run_ingestion_status",
        ),
    )
    op.create_index(
        "ix_ingestion_run_source_started", "ingestion_run", ["source_id", sa.text("started_at DESC")]
    )
    op.add_column("document", jsonb("metadata", "{}"))
    op.add_column("passage", sa.Column("embedding_model", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("passage", "embedding_model")
    op.drop_column("document", "metadata")
    op.drop_index("ix_ingestion_run_source_started", table_name="ingestion_run")
    op.drop_table("ingestion_run")

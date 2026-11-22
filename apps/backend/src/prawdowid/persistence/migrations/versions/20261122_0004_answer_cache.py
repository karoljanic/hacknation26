"""answer cache: assessment snapshots valid for a day, image embeddings of submissions

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 768  # keep in sync with models.EMBEDDING_DIM at the time of this revision


def upgrade() -> None:
    # Everything needed to return a stored answer again: the assessment and its cited evidence.
    op.add_column("assessment", sa.Column("snapshot", postgresql.JSONB(), nullable=True))
    op.create_index("ix_assessment_claim_valid_until", "assessment", ["claim_id", "valid_until"])
    op.add_column("submission", sa.Column("media_embedding", Vector(EMBEDDING_DIM), nullable=True))
    op.add_column("submission", sa.Column("media_embedding_model", sa.String(100), nullable=True))
    # The (redacted) text read from the image, reused for the same image.
    op.add_column("submission", sa.Column("media_reading", postgresql.JSONB(), nullable=True))
    op.create_index(
        "ix_submission_media_embedding",
        "submission",
        ["media_embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"media_embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_submission_media_embedding", table_name="submission")
    op.drop_column("submission", "media_reading")
    op.drop_column("submission", "media_embedding_model")
    op.drop_column("submission", "media_embedding")
    op.drop_index("ix_assessment_claim_valid_until", table_name="assessment")
    op.drop_column("assessment", "snapshot")

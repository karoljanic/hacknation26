"""initial schema: source catalogue, corpus, claim graph, submissions, cases

Revision ID: 0001
Revises:
Create Date: 2026-11-21

Hand-written to mirror prawdowid.persistence.models (including its naming
convention). Enum columns are VARCHAR(32) + CHECK, as in the models.
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 768  # keep in sync with models.EMBEDDING_DIM at the time of this revision


def uuid_pk() -> sa.Column:
    return sa.Column(
        "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
    )


def tstz(name: str, *, nullable: bool = False, default_now: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if default_now else None,
        nullable=nullable,
    )


def enum_col(name: str, *, nullable: bool = False, server_default: str | None = None) -> sa.Column:
    return sa.Column(name, sa.String(32), nullable=nullable, server_default=server_default)


def enum_check(table: str, column: str, check_name: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=f"ck_{table}_{check_name}")


def jsonb(name: str, default: str) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), server_default=sa.text(f"'{default}'::jsonb"), nullable=False)


SOURCE_KIND = ("official", "public_data", "fact_checker", "media")
CLAIM_KIND = ("factual_claim", "verification_question", "opinion")
CLAIM_TYPE = ("numeric", "event", "quote", "causal", "prediction")
CLAIM_ORIGIN = ("corpus", "user")
EVIDENCE_RELATION = ("supports", "refutes", "adds_context")
CLAIM_RELATION = ("same_as", "negates", "contradicts", "supersedes", "part_of", "specializes")
DOCUMENT_RELATION = ("cites", "derived_from")
SUBMISSION_CLAIM_RELATION = ("states", "implies", "asks")
VERDICT = ("confirmed", "false", "misleading", "insufficient_evidence", "unverifiable")
STRENGTH = ("strong", "moderate", "weak")
CHANNEL = ("web", "discord", "sms", "whatsapp", "messenger", "demo")
RUN_STATUS = ("pending", "running", "ok", "tech_failure", "needs_clarification", "out_of_scope")
CASE_STATE = ("recommended", "forwarded", "resolved")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # -- source catalogue & corpus ---------------------------------------- #
    op.create_table(
        "source",
        uuid_pk(),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        enum_col("kind"),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("homepage_url", sa.Text(), nullable=True),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("lang", sa.String(8), server_default="pl", nullable=False),
        sa.Column("license", sa.Text(), nullable=False),
        sa.Column("inclusion_rule", sa.Text(), nullable=False),
        sa.Column("status_warning", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        tstz("created_at", default_now=True),
        tstz("reviewed_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_source"),
        sa.UniqueConstraint("slug", name="uq_source_slug"),
        sa.CheckConstraint("tier BETWEEN 1 AND 3", name="ck_source_tier_range"),
        enum_check("source", "kind", "source_kind", SOURCE_KIND),
    )
    op.create_index("ix_source_domain", "source", ["domain"])

    op.create_table(
        "document",
        uuid_pk(),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        tstz("published_at", nullable=True),
        tstz("fetched_at"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("lang", sa.String(8), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_document"),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name="fk_document_source_id_source", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("source_id", "url", "content_hash", name="uq_document_version"),
    )
    op.create_index("ix_document_source_id", "document", ["source_id"])
    op.create_index("ix_document_url", "document", ["url"])
    op.create_index("ix_document_published_at", "document", ["published_at"])

    op.create_table(
        "passage",
        uuid_pk(),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', text)", persisted=True),
            nullable=True,
        ),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(EMBEDDING_DIM), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_passage"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["document.id"], name="fk_passage_document_id_document", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_passage_document_ordinal"),
        sa.CheckConstraint("char_end > char_start AND char_start >= 0", name="ck_passage_span"),
    )
    op.create_index("ix_passage_tsv", "passage", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_passage_embedding",
        "passage",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "document_relation",
        sa.Column("src_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_document_id", postgresql.UUID(as_uuid=True), nullable=False),
        enum_col("relation"),
        sa.PrimaryKeyConstraint(
            "src_document_id", "dst_document_id", "relation", name="pk_document_relation"
        ),
        sa.ForeignKeyConstraint(
            ["src_document_id"],
            ["document.id"],
            name="fk_document_relation_src_document_id_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dst_document_id"],
            ["document.id"],
            name="fk_document_relation_dst_document_id_document",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "src_document_id <> dst_document_id", name="ck_document_relation_no_self_loop"
        ),
        enum_check("document_relation", "relation", "document_relation", DOCUMENT_RELATION),
    )
    op.create_index(
        "ix_document_relation_dst_document_id", "document_relation", ["dst_document_id"]
    )

    # -- claims & evidence graph ------------------------------------------ #
    op.create_table(
        "claim",
        uuid_pk(),
        sa.Column("canonical_text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("lang", sa.String(8), server_default="pl", nullable=False),
        enum_col("kind"),
        enum_col("type", nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("predicate", sa.Text(), nullable=True),
        sa.Column("object", sa.Text(), nullable=True),
        jsonb("qualifiers", "{}"),
        enum_col("origin"),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        tstz("created_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_claim"),
        enum_check("claim", "kind", "claim_kind", CLAIM_KIND),
        enum_check("claim", "type", "claim_type", CLAIM_TYPE),
        enum_check("claim", "origin", "claim_origin", CLAIM_ORIGIN),
    )
    op.create_index("ix_claim_text_hash", "claim", ["text_hash"])
    op.create_index(
        "ix_claim_embedding",
        "claim",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "evidence",
        uuid_pk(),
        sa.Column("passage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        enum_col("relation"),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=True),
        sa.Column("quote_end", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("judged_by", sa.Text(), nullable=False),
        tstz("judged_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_evidence"),
        sa.ForeignKeyConstraint(
            ["passage_id"], ["passage.id"], name="fk_evidence_passage_id_passage", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name="fk_evidence_claim_id_claim", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("passage_id", "claim_id", "relation", "judged_by", name="uq_evidence_edge"),
        sa.CheckConstraint("length(quote) > 0", name="ck_evidence_quote_not_empty"),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="ck_evidence_confidence_range"
        ),
        enum_check("evidence", "relation", "evidence_relation", EVIDENCE_RELATION),
    )
    op.create_index("ix_evidence_claim_id", "evidence", ["claim_id"])

    op.create_table(
        "claim_relation",
        uuid_pk(),
        sa.Column("src_claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dst_claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        enum_col("relation"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("judged_by", sa.Text(), nullable=False),
        tstz("judged_at", default_now=True),
        sa.PrimaryKeyConstraint("id", name="pk_claim_relation"),
        sa.ForeignKeyConstraint(
            ["src_claim_id"], ["claim.id"], name="fk_claim_relation_src_claim_id_claim", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["dst_claim_id"], ["claim.id"], name="fk_claim_relation_dst_claim_id_claim", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("src_claim_id", "dst_claim_id", "relation", name="uq_claim_relation_edge"),
        sa.CheckConstraint("src_claim_id <> dst_claim_id", name="ck_claim_relation_no_self_loop"),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name="ck_claim_relation_confidence_range"
        ),
        enum_check("claim_relation", "relation", "claim_relation", CLAIM_RELATION),
    )
    op.create_index("ix_claim_relation_dst_claim_id", "claim_relation", ["dst_claim_id"])

    # -- submissions & runs ----------------------------------------------- #
    op.create_table(
        "submission",
        uuid_pk(),
        enum_col("channel"),
        sa.Column("sender_hash", sa.String(64), nullable=True),
        tstz("received_at", default_now=True),
        sa.Column("redacted_text", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        tstz("expires_at"),
        sa.PrimaryKeyConstraint("id", name="pk_submission"),
        enum_check("submission", "channel", "channel", CHANNEL),
    )
    op.create_index("ix_submission_sender_hash", "submission", ["sender_hash"])
    op.create_index("ix_submission_expires_at", "submission", ["expires_at"])

    op.create_table(
        "verification_run",
        uuid_pk(),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        enum_col("status", server_default="pending"),
        tstz("started_at", default_now=True),
        tstz("finished_at", nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        jsonb("models", "[]"),
        sa.Column("est_cost_usd", sa.Numeric(10, 6), nullable=True),
        jsonb("injection_flags", "[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("pipeline_version", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_verification_run"),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submission.id"],
            name="fk_verification_run_submission_id_submission",
            ondelete="CASCADE",
        ),
        enum_check("verification_run", "status", "run_status", RUN_STATUS),
    )
    op.create_index("ix_verification_run_submission_id", "verification_run", ["submission_id"])
    op.create_index("ix_verification_run_status", "verification_run", ["status"])

    op.create_table(
        "submission_claim",
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        enum_col("relation"),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        jsonb("references", "[]"),
        jsonb("ambiguities", "[]"),
        sa.Column("match", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("submission_id", "claim_id", name="pk_submission_claim"),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submission.id"],
            name="fk_submission_claim_submission_id_submission",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name="fk_submission_claim_claim_id_claim", ondelete="CASCADE"
        ),
        enum_check(
            "submission_claim", "relation", "submission_claim_relation", SUBMISSION_CLAIM_RELATION
        ),
    )
    op.create_index("ix_submission_claim_claim_id", "submission_claim", ["claim_id"])

    # -- assessments -------------------------------------------------------- #
    op.create_table(
        "assessment",
        uuid_pk(),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        enum_col("verdict"),
        enum_col("strength"),
        jsonb("reasons", "{}"),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("limitations", sa.Text(), nullable=True),
        tstz("as_of", default_now=True),
        tstz("valid_until", nullable=True),
        sa.Column("pipeline_version", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_assessment"),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name="fk_assessment_claim_id_claim", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["verification_run.id"],
            name="fk_assessment_run_id_verification_run",
            ondelete="SET NULL",
        ),
        enum_check("assessment", "verdict", "verdict", VERDICT),
        enum_check("assessment", "strength", "strength", STRENGTH),
    )
    op.create_index("ix_assessment_run_id", "assessment", ["run_id"])
    op.create_index(
        "ix_assessment_claim_as_of", "assessment", ["claim_id", sa.text("as_of DESC")]
    )

    op.create_table(
        "assessment_evidence",
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("assessment_id", "evidence_id", name="pk_assessment_evidence"),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["assessment.id"],
            name="fk_assessment_evidence_assessment_id_assessment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence.id"],
            name="fk_assessment_evidence_evidence_id_evidence",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_assessment_evidence_evidence_id", "assessment_evidence", ["evidence_id"])

    # -- moderation cases --------------------------------------------------- #
    op.create_table(
        "moderation_case",
        uuid_pk(),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        enum_col("state", server_default="recommended"),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("submission_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        tstz("first_seen_at", default_now=True),
        tstz("last_seen_at", default_now=True),
        tstz("forwarded_at", nullable=True),
        sa.Column("forwarded_to", sa.Text(), nullable=True),
        tstz("resolved_at", nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_moderation_case"),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name="fk_moderation_case_claim_id_claim", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "state <> 'forwarded' OR (forwarded_at IS NOT NULL AND forwarded_to IS NOT NULL)",
            name="ck_moderation_case_forwarded_has_target",
        ),
        enum_check("moderation_case", "state", "case_state", CASE_STATE),
    )
    op.create_index(
        "uq_moderation_case_open_claim",
        "moderation_case",
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'resolved'"),
    )

    op.create_table(
        "case_submission",
        sa.Column("case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("case_id", "submission_id", name="pk_case_submission"),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["moderation_case.id"],
            name="fk_case_submission_case_id_moderation_case",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submission.id"],
            name="fk_case_submission_submission_id_submission",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_case_submission_submission_id", "case_submission", ["submission_id"])


def downgrade() -> None:
    for table in (
        "case_submission",
        "moderation_case",
        "assessment_evidence",
        "assessment",
        "submission_claim",
        "verification_run",
        "submission",
        "claim_relation",
        "evidence",
        "claim",
        "document_relation",
        "passage",
        "document",
        "source",
    ):
        op.drop_table(table)
    # The vector extension is left installed (other schemas may use it).

"""Relational model of the evidence graph.

Nodes are ordinary tables (sources, documents, passages, claims, ...); the
graph edges between them are association tables with their own attributes:

    Passage  -[evidence: supports|refutes|adds_context]-> Claim
    Claim    -[claim_relation: same_as|negates|contradicts|...]-> Claim
    Document -[document_relation: cites|derived_from]-> Document
    Submission -[submission_claim: states|implies]-> Claim

Traversals (e.g. the evidence chain for a claim, or its SAME_AS cluster) are
plain joins / recursive CTEs over these tables.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from prawdowid.domain.enums import (
    CaseState,
    Channel,
    ClaimKind,
    ClaimOrigin,
    ClaimRelation,
    ClaimType,
    DocumentRelation,
    EvidenceRelation,
    RunStatus,
    SourceKind,
    Strength,
    SubmissionClaimRelation,
    Verdict,
)

# Vector size stored in the DB. Gemini embeddings are requested truncated to
# this size (768 is one of Google's recommended sizes); a self-hosted
# alternative of the same size is e.g. intfloat/multilingual-e5-base. Changing
# it requires a migration + re-embedding.
EMBEDDING_DIM = 768

# Deterministic constraint names so Alembic migrations are stable.
metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class Base(DeclarativeBase):
    metadata = metadata


def str_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """VARCHAR + CHECK constraint holding the enum *values*."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def json_object() -> Mapped[dict[str, Any]]:
    return mapped_column(JSONB, server_default=text("'{}'::jsonb"))


def confidence() -> Mapped[float | None]:
    return mapped_column()


# --------------------------------------------------------------------------- #
# Source catalogue and corpus
# --------------------------------------------------------------------------- #


class Source(Base):
    """An auditable entry of the source catalogue (one publisher/feed/dataset)."""

    __tablename__ = "source"
    __table_args__ = (CheckConstraint("tier BETWEEN 1 AND 3", name="tier_range"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(100), unique=True)  # stable key in data/sources
    name: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str] = mapped_column(Text)
    kind: Mapped[SourceKind] = mapped_column(str_enum(SourceKind, "source_kind"))
    domain: Mapped[str] = mapped_column(Text, index=True)  # e.g. "gov.pl", "demagog.org.pl"
    homepage_url: Mapped[str | None] = mapped_column(Text)
    # 1 = primary/official, 2 = reputable secondary/fact-checker, 3 = other context
    tier: Mapped[int] = mapped_column(SmallInteger)
    lang: Mapped[str] = mapped_column(String(8), server_default="pl")
    license: Mapped[str] = mapped_column(Text)
    inclusion_rule: Mapped[str] = mapped_column(Text)  # why it qualifies for the catalogue
    # Neutral warning shown next to (never instead of) the evidence, e.g. EU sanctions.
    status_warning: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = created_at()
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    documents: Mapped[list["Document"]] = relationship(back_populates="source")


class Document(Base):
    """One fetched version of a page/report/dataset/fact-check."""

    __tablename__ = "document"
    __table_args__ = (
        # The same URL may be stored again when its content changes.
        UniqueConstraint("source_id", "url", "content_hash", name="uq_document_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source.id", ondelete="RESTRICT"), index=True)
    url: Mapped[str] = mapped_column(Text, index=True)
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex of normalised text
    lang: Mapped[str] = mapped_column(String(8))

    source: Mapped[Source] = relationship(back_populates="documents")
    passages: Mapped[list["Passage"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Passage.ordinal"
    )


class Passage(Base):
    """Retrievable chunk of a document; the unit that gets cited."""

    __tablename__ = "passage"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_passage_document_ordinal"),
        CheckConstraint("char_end > char_start AND char_start >= 0", name="span"),
        Index("ix_passage_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_passage_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("document.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column()
    char_end: Mapped[int] = mapped_column()
    # 'simple' config: Postgres ships no Polish stemmer; lexical recall is
    # complemented by the embedding search.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True), deferred=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), deferred=True)

    document: Mapped[Document] = relationship(back_populates="passages")


class DocumentLink(Base):
    """Document -[cites|derived_from]-> Document."""

    __tablename__ = "document_relation"
    __table_args__ = (CheckConstraint("src_document_id <> dst_document_id", name="no_self_loop"),)

    src_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True
    )
    dst_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    relation: Mapped[DocumentRelation] = mapped_column(
        str_enum(DocumentRelation, "document_relation"), primary_key=True
    )


# --------------------------------------------------------------------------- #
# Claims and the evidence graph
# --------------------------------------------------------------------------- #


class Claim(Base):
    """A normalised, checkable statement (from the corpus or from a user)."""

    __tablename__ = "claim"
    __table_args__ = (
        Index(
            "ix_claim_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    canonical_text: Mapped[str] = mapped_column(Text)
    # sha256 of the normalised text: exact-duplicate lookup before vector search.
    text_hash: Mapped[str] = mapped_column(String(64), index=True)
    lang: Mapped[str] = mapped_column(String(8), server_default="pl")
    kind: Mapped[ClaimKind] = mapped_column(str_enum(ClaimKind, "claim_kind"))
    type: Mapped[ClaimType | None] = mapped_column(str_enum(ClaimType, "claim_type"))
    subject: Mapped[str | None] = mapped_column(Text)
    predicate: Mapped[str | None] = mapped_column(Text)
    object: Mapped[str | None] = mapped_column(Text)
    # {"time": "2024-03", "place": "PL", "value": 4.9, "unit": "%"}
    qualifiers: Mapped[dict[str, Any]] = json_object()
    origin: Mapped[ClaimOrigin] = mapped_column(str_enum(ClaimOrigin, "claim_origin"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), deferred=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at()

    evidence: Mapped[list["Evidence"]] = relationship(back_populates="claim")
    assessments: Mapped[list["Assessment"]] = relationship(
        back_populates="claim", order_by="Assessment.as_of.desc()"
    )


class Evidence(Base):
    """Passage -[supports|refutes|adds_context]-> Claim, with the cited quote."""

    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("passage_id", "claim_id", "relation", "judged_by", name="uq_evidence_edge"),
        CheckConstraint("length(quote) > 0", name="quote_not_empty"),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    passage_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("passage.id", ondelete="RESTRICT"))
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim.id", ondelete="CASCADE"), index=True)
    relation: Mapped[EvidenceRelation] = mapped_column(str_enum(EvidenceRelation, "evidence_relation"))
    # Verbatim excerpt of the passage; offsets let us verify the quote exists.
    quote: Mapped[str] = mapped_column(Text)
    quote_start: Mapped[int | None] = mapped_column()
    quote_end: Mapped[int | None] = mapped_column()
    confidence: Mapped[float | None] = confidence()
    judged_by: Mapped[str] = mapped_column(Text)  # "model:<id>@<pipeline>" | "human:<role>"
    judged_at: Mapped[datetime] = created_at()

    passage: Mapped[Passage] = relationship()
    claim: Mapped[Claim] = relationship(back_populates="evidence")


class ClaimLink(Base):
    """Claim -[same_as|negates|contradicts|supersedes|part_of|specializes]-> Claim."""

    __tablename__ = "claim_relation"
    __table_args__ = (
        UniqueConstraint("src_claim_id", "dst_claim_id", "relation", name="uq_claim_relation_edge"),
        CheckConstraint("src_claim_id <> dst_claim_id", name="no_self_loop"),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence_range"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    src_claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim.id", ondelete="CASCADE"))
    dst_claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim.id", ondelete="CASCADE"), index=True)
    relation: Mapped[ClaimRelation] = mapped_column(str_enum(ClaimRelation, "claim_relation"))
    confidence: Mapped[float | None] = confidence()
    judged_by: Mapped[str] = mapped_column(Text)
    judged_at: Mapped[datetime] = created_at()


class Assessment(Base):
    """A verdict on a claim at a point in time, derived from explicit evidence."""

    __tablename__ = "assessment"
    __table_args__ = (Index("ix_assessment_claim_as_of", "claim_id", text("as_of DESC")),)

    id: Mapped[uuid.UUID] = uuid_pk()
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim.id", ondelete="CASCADE"))
    # The run that produced it; NULL for assessments computed offline for the corpus.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("verification_run.id", ondelete="SET NULL"), index=True
    )
    verdict: Mapped[Verdict] = mapped_column(str_enum(Verdict, "verdict"))
    strength: Mapped[Strength] = mapped_column(str_enum(Strength, "strength"))
    # Explicit premises behind the strength:
    # {"tiers": [1, 2], "recency_days": 12, "independent_sources": 2, "conflicts": 0}
    reasons: Mapped[dict[str, Any]] = json_object()
    # Model-written text, kept apart from the source material.
    explanation: Mapped[str | None] = mapped_column(Text)
    limitations: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[datetime] = created_at()
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pipeline_version: Mapped[str] = mapped_column(String(64))

    claim: Mapped[Claim] = relationship(back_populates="assessments")
    evidence: Mapped[list[Evidence]] = relationship(secondary="assessment_evidence")


class AssessmentEvidence(Base):
    __tablename__ = "assessment_evidence"

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evidence.id", ondelete="RESTRICT"), primary_key=True, index=True
    )


# --------------------------------------------------------------------------- #
# Citizen submissions and processing
# --------------------------------------------------------------------------- #


class Submission(Base):
    """An incoming question. Stores only redacted text and expires (retention)."""

    __tablename__ = "submission"

    id: Mapped[uuid.UUID] = uuid_pk()
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "channel"))
    # Salted hash of the channel user id: rate limiting / dedup without storing it.
    sender_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    received_at: Mapped[datetime] = created_at()
    redacted_text: Mapped[str | None] = mapped_column(Text)  # PII masked before insert
    url: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    claims: Mapped[list["SubmissionClaim"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    runs: Mapped[list["VerificationRun"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )


class SubmissionClaim(Base):
    """Submission -[states|implies]-> Claim."""

    __tablename__ = "submission_claim"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission.id", ondelete="CASCADE"), primary_key=True
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("claim.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    relation: Mapped[SubmissionClaimRelation] = mapped_column(
        str_enum(SubmissionClaimRelation, "submission_claim_relation")
    )
    # Wording as extracted from this submission (may differ from canonical_text
    # when matched to an existing claim).
    extracted_text: Mapped[str] = mapped_column(Text)
    # Provenance in the (redacted) input: [{"segment_id": ..., "quote": ...}]
    references: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    ambiguities: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # How it was linked to the claim: "new" | "exact" | "similar:<cosine distance>"
    match: Mapped[str] = mapped_column(String(32))

    submission: Mapped[Submission] = relationship(back_populates="claims")
    claim: Mapped[Claim] = relationship()


class VerificationRun(Base):
    """One pass of the pipeline over a submission, plus its telemetry."""

    __tablename__ = "verification_run"

    id: Mapped[uuid.UUID] = uuid_pk()
    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[RunStatus] = mapped_column(
        str_enum(RunStatus, "run_status"), server_default=RunStatus.PENDING.value, index=True
    )
    started_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column()
    # [{"role": "claims", "model": "...", "provider": "...", "in_tokens": 1, "out_tokens": 2}]
    models: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    est_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    # [{"where": "document:<id>", "pattern": "ignore previous instructions"}]
    injection_flags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text)  # technical detail, never shown as a verdict
    pipeline_version: Mapped[str] = mapped_column(String(64))

    submission: Mapped[Submission] = relationship(back_populates="runs")
    assessments: Mapped[list[Assessment]] = relationship()


class Case(Base):
    """Moderator case for unresolved / high-risk claims, deduplicated per claim cluster."""

    __tablename__ = "moderation_case"
    __table_args__ = (
        # At most one open case per (representative) claim -> repeated questions
        # update the existing case instead of creating new ones.
        Index(
            "uq_moderation_case_open_claim",
            "claim_id",
            unique=True,
            postgresql_where=text("state <> 'resolved'"),
        ),
        CheckConstraint(
            "state <> 'forwarded' OR (forwarded_at IS NOT NULL AND forwarded_to IS NOT NULL)",
            name="forwarded_has_target",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # Representative claim of the SAME_AS cluster.
    claim_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("claim.id", ondelete="CASCADE"))
    state: Mapped[CaseState] = mapped_column(
        str_enum(CaseState, "case_state"), server_default=CaseState.RECOMMENDED.value
    )
    reason: Mapped[str] = mapped_column(String(32))  # "high_risk" | "repeated_no_evidence"
    submission_count: Mapped[int] = mapped_column(server_default=text("1"))
    first_seen_at: Mapped[datetime] = created_at()
    last_seen_at: Mapped[datetime] = created_at()
    forwarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    forwarded_to: Mapped[str | None] = mapped_column(Text)  # role/channel configured by admin
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    claim: Mapped[Claim] = relationship()
    submissions: Mapped[list[Submission]] = relationship(secondary="case_submission")


class CaseSubmission(Base):
    __tablename__ = "case_submission"

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("moderation_case.id", ondelete="CASCADE"), primary_key=True
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission.id", ondelete="CASCADE"), primary_key=True, index=True
    )

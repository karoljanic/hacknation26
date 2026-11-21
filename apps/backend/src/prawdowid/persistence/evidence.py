"""Persist live evidence and assessments produced by ``POST /check`` as
auditable DB rows.

Only cited evidence is persisted (not every search hit, which would bloat the
corpus with irrelevant results). Every persisted item is attached to an
already-catalogued, audited ``Source`` — never an ad-hoc domain created on the
fly — exactly like the batch ingestion pipeline. Documents are deduplicated
by (source, url, content_hash), so a repeatedly-cited article is stored once.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prawdowid.adapters.sources.gov_pl import content_hash, normalize_text
from prawdowid.domain.enums import EvidenceRelation, Strength, Verdict
from prawdowid.domain.evidence import EvidenceCitation, EvidenceItem
from prawdowid.ingestion.service import domain_matches
from prawdowid.persistence.models import Assessment, AssessmentEvidence, Document, Evidence, Passage, Source
from prawdowid.services.pipeline import ClaimOutcome

GOOGLE_LIVE_SLUG = "google-fact-check-live"
EUVSDISINFO_SLUG = "euvsdisinfo"
PIPELINE_VERSION = "check-0.1"

_RELATION_BY_LITERAL = {
    "supports": EvidenceRelation.SUPPORTS,
    "refutes": EvidenceRelation.REFUTES,
    "adds_context": EvidenceRelation.ADDS_CONTEXT,
}


class SourceNotCatalogued(LookupError):
    """A required catalogue entry (e.g. 'euvsdisinfo') is missing; run
    ``python -m prawdowid.ingestion sync-catalog`` first."""


def _get_source(session: Session, slug: str) -> Source:
    source = session.scalars(select(Source).where(Source.slug == slug)).first()
    if source is None:
        raise SourceNotCatalogued(f"Source '{slug}' is not in the database; run sync-catalog first")
    return source


def _resolve_source(session: Session, item: EvidenceItem) -> Source:
    if item.catalog_slug:
        # Exact, unambiguous attribution: every gov.pl catalogue entry shares
        # the same domain ("gov.pl"), so domain matching cannot tell them
        # apart. The connector that produced this item already knows exactly
        # which catalogued source it came from.
        return _get_source(session, item.catalog_slug)
    if item.source == "euvsdisinfo":
        return _get_source(session, EUVSDISINFO_SLUG)
    if item.publisher_site:
        for candidate in session.scalars(select(Source).where(Source.active)):
            if domain_matches(item.publisher_site, candidate.domain):
                return candidate
    return _get_source(session, GOOGLE_LIVE_SLUG)


def _get_or_create_document(
    session: Session, source: Source, *, url: str, title: str, text: str, published_at: datetime | None
) -> Document:
    text = normalize_text(text)
    digest = content_hash(text)

    def find() -> Document | None:
        return session.scalars(
            select(Document).where(
                Document.source_id == source.id, Document.url == url, Document.content_hash == digest
            )
        ).first()

    existing = find()
    if existing is not None:
        return existing
    document = Document(
        source_id=source.id,
        url=url,
        title=normalize_text(title)[:500] or url,
        published_at=published_at,
        fetched_at=datetime.now(timezone.utc),
        content_hash=digest,
        lang="pl",
        passages=[Passage(ordinal=0, text=text, char_start=0, char_end=len(text))],
    )
    session.add(document)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = find()
        if existing is None:
            raise
        return existing
    return document


def _record_citation(
    session: Session, *, claim_id: uuid.UUID, evidence_text: str, judged_by: str,
    item: EvidenceItem, citation: EvidenceCitation,
) -> Evidence:
    source = _resolve_source(session, item)
    document = _get_or_create_document(
        session, source, url=str(item.url), title=item.title, text=evidence_text, published_at=item.published_at
    )
    passage = document.passages[0]
    relation = _RELATION_BY_LITERAL[citation.relation]
    # (passage, claim, relation, judged_by) is unique: a repeat check citing the
    # same passage the same way reuses the row instead of violating it.
    existing = session.scalars(
        select(Evidence).where(
            Evidence.passage_id == passage.id, Evidence.claim_id == claim_id,
            Evidence.relation == relation, Evidence.judged_by == judged_by,
        )
    ).first()
    if existing is not None:
        return existing
    quote_start = passage.text.find(citation.quote)
    evidence = Evidence(
        passage_id=passage.id,
        claim_id=claim_id,
        relation=relation,
        quote=citation.quote,
        quote_start=quote_start if quote_start >= 0 else None,
        quote_end=(quote_start + len(citation.quote)) if quote_start >= 0 else None,
        judged_by=judged_by,
    )
    session.add(evidence)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.scalars(
            select(Evidence).where(
                Evidence.passage_id == passage.id, Evidence.claim_id == claim_id,
                Evidence.relation == relation, Evidence.judged_by == judged_by,
            )
        ).first()
        if existing is None:
            raise
        return existing
    return evidence


def record_outcome(
    session: Session, *, run_id: uuid.UUID, claim_id: uuid.UUID, outcome: ClaimOutcome
) -> Assessment:
    """Writes one ``Assessment`` row (always) plus ``Evidence``/``Document``/
    ``Passage`` rows for every cited item (only when there are citations)."""
    assessment = Assessment(
        claim_id=claim_id,
        run_id=run_id,
        verdict=Verdict(outcome.assessment.verdict),
        strength=Strength(outcome.assessment.strength),
        reasons=outcome.assessment.reasons.model_dump(),
        explanation=outcome.assessment.explanation,
        limitations=outcome.assessment.limitations,
        pipeline_version=PIPELINE_VERSION,
    )
    session.add(assessment)
    session.flush()
    linked_evidence_ids: set[uuid.UUID] = set()
    for citation in outcome.assessment.citations:
        evidence_item = outcome.evidence[citation.evidence_index]
        evidence = _record_citation(
            session, claim_id=claim_id, evidence_text=evidence_item.text, judged_by=outcome.judged_by,
            item=evidence_item.item, citation=citation,
        )
        if evidence.id in linked_evidence_ids:
            continue  # the model cited the same evidence item twice in one claim
        linked_evidence_ids.add(evidence.id)
        session.add(AssessmentEvidence(assessment_id=assessment.id, evidence_id=evidence.id))
    return assessment

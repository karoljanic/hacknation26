"""Persist live evidence and assessments produced by ``POST /check`` as
auditable DB rows.

Only cited evidence is persisted (not every search hit, which would bloat the
corpus with irrelevant results). Every persisted item is attached to an
already-catalogued, audited ``Source`` — never an ad-hoc domain created on the
fly — exactly like the batch ingestion pipeline. Documents are deduplicated
by (source, url, content_hash), so a repeatedly-cited article is stored once.
Third-party texts are stored only as excerpts around cited quotes; official
documents in full (see ``_stores_full_text``).
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prawdowid.adapters.sources.gov_pl import content_hash, normalize_text
from prawdowid.domain.enums import EvidenceRelation, SourceKind, Strength, Verdict
from prawdowid.domain.evidence import EvidenceCitation, EvidenceItem
from prawdowid.domain.quotes import locate_quote
from prawdowid.ingestion.chunking import MAX_CHARS, chunk_text
from prawdowid.ingestion.service import domain_matches
from prawdowid.persistence.models import Assessment, AssessmentEvidence, Document, Evidence, Passage, Source
from prawdowid.services.pipeline import ClaimOutcome

GOOGLE_LIVE_SLUG = "google-fact-check-live"
EUVSDISINFO_SLUG = "euvsdisinfo"
PIPELINE_VERSION = "check-0.1"

log = logging.getLogger(__name__)

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


# Third-party pages (fact-checkers, media) are not archived: only a bounded
# excerpt around each cited quote is kept, enough to audit the citation.
# Official documents (art. 4 of the Polish copyright act) are kept whole,
# chunked like ingested ones, so they also serve later retrieval.
EXCERPT_CONTEXT_CHARS = 300
EXCERPT_MAX_CHARS = MAX_CHARS


def _stores_full_text(source: Source) -> bool:
    return source.kind == SourceKind.OFFICIAL


def _get_or_create_document(
    session: Session, source: Source, *, item: EvidenceItem, text: str
) -> tuple[Document, str]:
    """Returns the document (one per page version, identified by the hash of
    its full normalized text) and that full text, which is not necessarily
    stored."""
    text = normalize_text(text)
    digest = content_hash(text)
    url = str(item.url)

    def find() -> Document | None:
        return session.scalars(
            select(Document).where(
                Document.source_id == source.id, Document.url == url, Document.content_hash == digest
            )
        ).first()

    existing = find()
    if existing is not None:
        return existing, text
    full = _stores_full_text(source)
    passages = (
        [Passage(ordinal=c.ordinal, text=c.text, char_start=c.char_start, char_end=c.char_end) for c in chunk_text(text)]
        if full
        else []  # excerpts are added per citation (_passage_for)
    )
    document = Document(
        source_id=source.id,
        url=url,
        title=normalize_text(item.title)[:500] or url,
        published_at=item.published_at,
        fetched_at=datetime.now(timezone.utc),
        content_hash=digest,
        lang=item.lang or source.lang or "pl",
        meta={
            "stored": "full" if full else "excerpts",
            "evidence_source": item.source,
            "kind": item.kind,
            **({"rating": item.rating} if item.rating else {}),
            "full_text_chars": len(text),
        },
        passages=passages,
    )
    # A concurrent request may insert the same document first. The savepoint
    # undoes only this insert; a plain rollback() would also discard the
    # assessment rows already written in this transaction.
    try:
        with session.begin_nested():
            session.add(document)
            session.flush()
    except IntegrityError:
        existing = find()
        if existing is None:
            raise
        return existing, text
    return document, text


def _excerpt_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """The quote plus some context on each side, cut at whitespace and capped."""
    if len(text) <= EXCERPT_MAX_CHARS:
        return 0, len(text)  # snippet-sized already (search result texts)
    if end - start >= EXCERPT_MAX_CHARS:
        return start, end
    room = (EXCERPT_MAX_CHARS - (end - start)) // 2
    context = min(EXCERPT_CONTEXT_CHARS, room)
    lo, hi = max(0, start - context), min(len(text), end + context)
    if lo > 0:
        space = text.find(" ", lo, start)
        lo = space + 1 if space >= 0 else lo
    if hi < len(text):
        space = text.rfind(" ", end, hi)
        hi = space if space >= 0 else hi
    return lo, hi


def _passage_for(session: Session, document: Document, full_text: str, quote: str) -> Passage:
    """The stored passage containing ``quote``; adds an excerpt passage when
    none does (always the case for a new third-party document)."""
    span = locate_quote(full_text, quote)
    if span is None:
        # The assessment verified the quote against the un-normalized text;
        # normalization only folds whitespace, so this is not expected.
        log.warning("Cited quote not found in normalized text of %s", document.url)
        start, end = 0, min(len(full_text), EXCERPT_MAX_CHARS)
    else:
        start, end = span
    for passage in document.passages:
        if passage.char_start <= start and end <= passage.char_end:
            return passage
    lo, hi = _excerpt_bounds(full_text, start, end) if span else (start, end)
    for _attempt in range(2):
        ordinal = max((p.ordinal for p in document.passages), default=-1) + 1
        passage = Passage(document_id=document.id, ordinal=ordinal, text=full_text[lo:hi], char_start=lo, char_end=hi)
        try:
            with session.begin_nested():  # savepoint: a concurrent request may take the ordinal
                session.add(passage)
                session.flush()
            document.passages.append(passage)  # keep the in-memory list current for the next citation
            return passage
        except IntegrityError:
            session.refresh(document, ["passages"])
            for existing in document.passages:
                if existing.char_start <= start and end <= existing.char_end:
                    return existing
    raise RuntimeError(f"Could not store an excerpt passage for {document.url}")


def _record_citation(
    session: Session, *, claim_id: uuid.UUID, evidence_text: str, judged_by: str,
    item: EvidenceItem, citation: EvidenceCitation,
) -> Evidence:
    source = _resolve_source(session, item)
    document, full_text = _get_or_create_document(session, source, item=item, text=evidence_text)
    passage = _passage_for(session, document, full_text, citation.quote)
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
    # The stored passage is whitespace-normalized, so the quote is located
    # tolerantly and stored as the passage's own span (offsets stay exact).
    span = locate_quote(passage.text, citation.quote)
    evidence = Evidence(
        passage_id=passage.id,
        claim_id=claim_id,
        relation=relation,
        quote=passage.text[span[0] : span[1]] if span else citation.quote,
        quote_start=span[0] if span else None,
        quote_end=span[1] if span else None,
        judged_by=judged_by,
    )
    try:
        with session.begin_nested():  # savepoint: see _get_or_create_document
            session.add(evidence)
            session.flush()
    except IntegrityError:
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

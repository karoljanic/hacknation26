"""persistence/evidence.py against a real Postgres (skipped when unreachable).

Uses ``isolated_sessions`` from conftest: everything runs inside one rolled-
back transaction, so the dev database is never actually modified.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from prawdowid.domain.enums import Channel, ClaimKind, ClaimOrigin, EvidenceRelation, SourceKind, Strength, Verdict
from prawdowid.domain.evidence import AssessmentReasons, ClaimAssessmentOutput, EvidenceCitation, EvidenceForAssessment, EvidenceItem
from prawdowid.persistence.evidence import SourceNotCatalogued, record_outcome
from prawdowid.persistence.models import (
    Assessment,
    AssessmentEvidence,
    Claim,
    Document,
    Evidence,
    Source,
    Submission,
    VerificationRun,
)
from prawdowid.services.pipeline import ClaimOutcome


def make_source(session, *, slug: str, domain: str, kind: SourceKind = SourceKind.FACT_CHECKER) -> Source:
    """Idempotent: reuses the real catalogued row if sync-catalog already
    created it (e.g. ``euvsdisinfo``), so this test works whether or not the
    dev database has already been migrated/synced."""
    existing = session.scalars(select(Source).where(Source.slug == slug)).first()
    if existing is not None:
        return existing
    source = Source(
        slug=slug, name=slug, publisher=slug, kind=kind, domain=domain, tier=2,
        license="test", inclusion_rule="test",
    )
    session.add(source)
    session.flush()
    return source


def make_claim(session, text: str = "Teza do sprawdzenia.") -> Claim:
    claim = Claim(
        canonical_text=text, text_hash=f"hash-{uuid.uuid4()}", kind=ClaimKind.FACTUAL_CLAIM, origin=ClaimOrigin.USER,
    )
    session.add(claim)
    session.flush()
    return claim


def make_run(session) -> VerificationRun:
    now = datetime.now(timezone.utc)
    submission = Submission(channel=Channel.WEB, received_at=now, expires_at=now)
    session.add(submission)
    session.flush()
    run = VerificationRun(submission_id=submission.id, pipeline_version="test")
    session.add(run)
    session.flush()
    return run


def euvsdisinfo_item() -> EvidenceItem:
    return EvidenceItem(
        source="euvsdisinfo", kind="article", title="Raport EUvsDisinfo", url="https://euvsdisinfo.eu/report/x",
        publisher="EUvsDisinfo", publisher_site="euvsdisinfo.eu", snippet="Analiza narracji.",
    )


def google_item(*, publisher_site: str = "demagog.org.pl") -> EvidenceItem:
    return EvidenceItem(
        source="google_fact_check", kind="claim_review", title="Analiza Demagoga",
        url="https://demagog.org.pl/analizy/123", publisher="Demagog", publisher_site=publisher_site,
        rating="Fałsz", snippet="Demagog ocenił tezę jako fałsz.",
    )


def outcome_for(item: EvidenceItem, *, text: str, quote: str, relation: str = "refutes") -> ClaimOutcome:
    evidence = EvidenceForAssessment(item=item, text=text, fully_fetched=True)
    assessment = ClaimAssessmentOutput(
        claim_id="c1", verdict="false", strength="strong",
        reasons=AssessmentReasons(source_tiers=[2], independent_sources=1, conflicting_sources=False),
        explanation="Demagog ocenił tezę jako fałsz.",
        citations=[EvidenceCitation(evidence_index=0, quote=quote, relation=relation)],
    )
    return ClaimOutcome("c1", "Teza.", "factual_claim", assessment, "model:test", [evidence])


def test_citation_is_persisted_as_document_passage_and_evidence(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        source = make_source(session, slug="euvsdisinfo", domain="euvsdisinfo.eu")
        claim = make_claim(session)
        item = euvsdisinfo_item()
        outcome = outcome_for(item, text="Pełna treść raportu. Kluczowe zdanie.", quote="Kluczowe zdanie.")

        assessment = record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        document = session.scalars(select(Document).where(Document.source_id == source.id)).one()
        assert document.url == str(item.url)
        passage = document.passages[0]
        assert passage.text == "Pełna treść raportu. Kluczowe zdanie."

        evidence = session.scalars(select(Evidence).where(Evidence.claim_id == claim.id)).one()
        assert evidence.quote == "Kluczowe zdanie."
        assert evidence.quote_start == passage.text.index("Kluczowe zdanie.")
        assert evidence.relation == EvidenceRelation.REFUTES
        assert evidence.judged_by == "model:test"

        linked = session.scalars(
            select(AssessmentEvidence).where(AssessmentEvidence.assessment_id == assessment.id)
        ).one()
        assert linked.evidence_id == evidence.id
        assert assessment.verdict == Verdict.FALSE
        assert assessment.strength == Strength.STRONG


def test_repeated_citation_of_the_same_url_reuses_the_document(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        make_source(session, slug="euvsdisinfo", domain="euvsdisinfo.eu")
        claim = make_claim(session)
        item = euvsdisinfo_item()
        text = "Ta sama treść za każdym razem."
        outcome = outcome_for(item, text=text, quote=text)

        record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        documents = session.scalars(select(Document).where(Document.url == str(item.url))).all()
        assert len(documents) == 1
        # Same claim, same passage, same relation, same judge: one shared Evidence row.
        evidence_rows = session.scalars(select(Evidence).where(Evidence.claim_id == claim.id)).all()
        assert len(evidence_rows) == 1
        links = session.scalars(
            select(AssessmentEvidence).where(AssessmentEvidence.evidence_id == evidence_rows[0].id)
        ).all()
        assert len(links) == 2  # both assessments reference the one evidence row


def test_google_result_is_attributed_to_a_matching_catalogued_publisher(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        demagog = make_source(session, slug="fc-demagog", domain="demagog.org.pl")
        make_source(session, slug="google-fact-check-live", domain="toolbox.google.com")
        claim = make_claim(session)
        item = google_item(publisher_site="demagog.org.pl")
        outcome = outcome_for(item, text=item.snippet, quote=item.snippet)

        record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        document = session.scalars(select(Document).where(Document.url == str(item.url))).one()
        assert document.source_id == demagog.id


def test_gov_pl_result_is_attributed_by_exact_catalog_slug_not_domain(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        rcb = make_source(session, slug="gov-rcb", domain="gov.pl", kind=SourceKind.OFFICIAL)
        make_source(session, slug="gov-mswia", domain="gov.pl", kind=SourceKind.OFFICIAL)
        claim = make_claim(session)
        item = EvidenceItem(
            source="gov_pl", kind="official", title="Raport dobowy RCB",
            url="https://www.gov.pl/web/rcb/raport", publisher="RCB", catalog_slug="gov-rcb",
            snippet="Treść raportu.",
        )
        outcome = outcome_for(item, text=item.snippet, quote=item.snippet)

        record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        document = session.scalars(select(Document).where(Document.url == str(item.url))).one()
        assert document.source_id == rcb.id  # not gov-mswia, despite sharing the same domain


def test_google_result_from_an_uncatalogued_publisher_falls_back_to_the_live_source(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        make_source(session, slug="fc-demagog", domain="demagog.org.pl")
        live = make_source(session, slug="google-fact-check-live", domain="toolbox.google.com")
        claim = make_claim(session)
        item = google_item(publisher_site="some-other-fact-checker.example")
        outcome = outcome_for(item, text=item.snippet, quote=item.snippet)

        record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        document = session.scalars(select(Document).where(Document.url == str(item.url))).one()
        assert document.source_id == live.id


def test_missing_catalogue_entry_raises_instead_of_fabricating_a_source(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        # Force the precondition regardless of whether sync-catalog already
        # ran against this database: no "euvsdisinfo" row must exist here.
        session.execute(delete(Source).where(Source.slug == "euvsdisinfo"))
        claim = make_claim(session)
        item = euvsdisinfo_item()
        outcome = outcome_for(item, text=item.snippet, quote=item.snippet)

        with pytest.raises(SourceNotCatalogued):
            record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)


def test_outcome_without_citations_still_records_an_assessment(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        claim = make_claim(session)
        assessment_output = ClaimAssessmentOutput(
            claim_id="c1", verdict="insufficient_evidence", strength="weak",
            reasons=AssessmentReasons(source_tiers=[], independent_sources=0, conflicting_sources=False),
            explanation="Brak dowodów.", citations=[],
        )
        outcome = ClaimOutcome("c1", "Teza.", "factual_claim", assessment_output, "deterministic:no_evidence", [])

        assessment = record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        assert assessment.verdict == Verdict.INSUFFICIENT_EVIDENCE
        assert session.scalars(select(Evidence).where(Evidence.claim_id == claim.id)).first() is None

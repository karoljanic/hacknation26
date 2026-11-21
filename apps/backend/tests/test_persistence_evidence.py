"""persistence/evidence.py against a real Postgres (skipped when unreachable).

Uses ``isolated_sessions`` from conftest: everything runs inside one rolled-
back transaction, so the dev database is never actually modified.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from prawdowid.domain.enums import Channel, ClaimKind, ClaimOrigin, EvidenceRelation, SourceKind, Strength, Verdict
from prawdowid.domain.evidence import AssessmentReasons, ClaimAssessmentOutput, EvidenceCitation, EvidenceForAssessment, EvidenceItem
from prawdowid.persistence.evidence import SourceNotCatalogued, record_outcome
from prawdowid.persistence.models import (
    Assessment,
    AssessmentEvidence,
    Claim,
    Document,
    Evidence,
    Passage,
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

        document = session.scalars(
            select(Document).where(Document.source_id == source.id, Document.url == str(item.url))
        ).one()
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
        # ran against this database: no "euvsdisinfo" row must exist here. It
        # is renamed, not deleted, because an ingested corpus references it.
        session.execute(update(Source).where(Source.slug == "euvsdisinfo").values(slug=f"renamed-{uuid.uuid4()}"))
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


def test_concurrent_document_insert_keeps_the_assessment(isolated_sessions, monkeypatch) -> None:
    """Another request inserts the same document between our lookup and our
    insert: only that insert is undone (savepoint), not the assessment."""
    with isolated_sessions() as session, session.begin():
        source = make_source(session, slug="euvsdisinfo", domain="euvsdisinfo.eu")
        claim = make_claim(session)
        item = euvsdisinfo_item()
        text = "Treść wstawiona równolegle."
        outcome = outcome_for(item, text=text, quote=text)
        # The "other request" already stored this exact document version.
        from prawdowid.adapters.sources.gov_pl import content_hash, normalize_text
        session.add(Document(
            source_id=source.id, url=str(item.url), title="x", fetched_at=datetime.now(timezone.utc),
            content_hash=content_hash(normalize_text(text)), lang="pl",
            passages=[Passage(ordinal=0, text=text, char_start=0, char_end=len(text))],
        ))
        session.flush()
        # ...but our first lookup ran before it was visible.
        real_scalars = session.scalars
        hidden = {"done": False}

        def scalars(statement, *args, **kwargs):
            result = real_scalars(statement, *args, **kwargs)
            if not hidden["done"] and statement.column_descriptions[0]["entity"] is Document:
                hidden["done"] = True
                return real_scalars(select(Document).where(Document.id.is_(None)))
            return result

        monkeypatch.setattr(session, "scalars", scalars)

        assessment = record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        assert hidden["done"]
        assert session.get(Assessment, assessment.id) is not None
        assert len(session.scalars(select(Document).where(Document.url == str(item.url))).all()) == 1
        assert session.scalars(select(Evidence).where(Evidence.claim_id == claim.id)).one().quote == text


LONG_ARTICLE = " ".join(f"Zdanie numer {i} z długiego artykułu redakcji." for i in range(300))  # ~14k chars


def test_third_party_article_is_stored_only_as_an_excerpt_around_the_quote(isolated_sessions) -> None:
    from prawdowid.persistence.evidence import EXCERPT_MAX_CHARS

    with isolated_sessions() as session, session.begin():
        make_source(session, slug="euvsdisinfo", domain="euvsdisinfo.eu")
        claim = make_claim(session)
        item = euvsdisinfo_item().model_copy(update={"url": f"https://euvsdisinfo.eu/report/{uuid.uuid4()}", "lang": "en"})
        quote = "Zdanie numer 150 z długiego artykułu redakcji."
        outcome = outcome_for(item, text=LONG_ARTICLE, quote=quote)

        record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        document = session.scalars(select(Document).where(Document.url == str(item.url))).one()
        assert document.meta["stored"] == "excerpts"
        assert document.lang == "en"
        [passage] = document.passages
        assert quote in passage.text
        assert len(passage.text) <= EXCERPT_MAX_CHARS < len(LONG_ARTICLE)
        # Offsets index into the full (unstored) text, like ingested passages.
        assert LONG_ARTICLE[passage.char_start : passage.char_end] == passage.text
        evidence = session.scalars(select(Evidence).where(Evidence.claim_id == claim.id)).one()
        assert passage.text[evidence.quote_start : evidence.quote_end] == quote


def test_more_citations_of_one_page_add_excerpts_and_reuse_covering_ones(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        make_source(session, slug="euvsdisinfo", domain="euvsdisinfo.eu")
        claim = make_claim(session)
        item = euvsdisinfo_item().model_copy(update={"url": f"https://euvsdisinfo.eu/report/{uuid.uuid4()}"})
        evidence = EvidenceForAssessment(item=item, text=LONG_ARTICLE, fully_fetched=True)
        quotes = [
            "Zdanie numer 10 z długiego artykułu redakcji.",
            "Zdanie numer 250 z długiego artykułu redakcji.",
            "Zdanie numer 11 z długiego artykułu redakcji.",  # inside the first excerpt
        ]
        assessment = ClaimAssessmentOutput(
            claim_id="c1", verdict="false", strength="strong",
            reasons=AssessmentReasons(source_tiers=[2], independent_sources=1, conflicting_sources=False),
            explanation="x",
            citations=[EvidenceCitation(evidence_index=0, quote=q, relation="refutes") for q in quotes],
        )
        outcome = ClaimOutcome("c1", "Teza.", "factual_claim", assessment, "model:test", [evidence])

        record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        document = session.scalars(select(Document).where(Document.url == str(item.url))).one()
        assert [p.ordinal for p in document.passages] == [0, 1]
        assert quotes[0] in document.passages[0].text and quotes[2] in document.passages[0].text
        assert quotes[1] in document.passages[1].text


def test_official_document_is_stored_in_full_as_chunks(isolated_sessions) -> None:
    with isolated_sessions() as session, session.begin():
        make_source(session, slug="gov-rcb", domain="gov.pl", kind=SourceKind.OFFICIAL)
        claim = make_claim(session)
        item = EvidenceItem(
            source="gov_pl", kind="official", title="Komunikat", url=f"https://www.gov.pl/web/rcb/{uuid.uuid4()}",
            publisher="RCB", catalog_slug="gov-rcb", snippet="Komunikat.", lang="pl",
        )
        quote = "Zdanie numer 200 z długiego artykułu redakcji."
        outcome = outcome_for(item, text=LONG_ARTICLE, quote=quote)

        record_outcome(session, run_id=make_run(session).id, claim_id=claim.id, outcome=outcome)
        session.flush()

        document = session.scalars(select(Document).where(Document.url == str(item.url))).one()
        assert document.meta["stored"] == "full"
        assert len(document.passages) > 1
        assert "".join(p.text for p in document.passages).replace(" ", "") == LONG_ARTICLE.replace(" ", "")
        evidence = session.scalars(select(Evidence).where(Evidence.claim_id == claim.id)).one()
        cited = session.get(Passage, evidence.passage_id)
        assert quote in cited.text

"""Submission intake: input -> redaction -> claim extraction -> claims in the DB.

Every submission gets a ``verification_run`` row, so failures are recorded as a
run status (``tech_failure``, ``needs_clarification``) and never as a verdict.
No model calls happen inside a DB transaction.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from prawdowid.ai.adapters.embedding import EmbeddingAdapter
from prawdowid.ai.adapters.llm import StructuredResult
from prawdowid.domain.claims import ClaimExtraction
from prawdowid.domain.enums import (
    Channel,
    ClaimKind,
    ClaimOrigin,
    RunStatus,
    SubmissionClaimRelation,
)
from prawdowid.domain.preparation import PreparedInput
from prawdowid.persistence import claims as claim_repo
from prawdowid.persistence.models import Claim, Submission, SubmissionClaim, VerificationRun
from prawdowid.security.redaction import redact
from prawdowid.services.claims import ClaimExtractor, is_open_question
from prawdowid.services.preparation import InputPreparer, NoExtractableContentError

PIPELINE_VERSION = "intake-0.1"

log = logging.getLogger(__name__)

# Provisional: cosine distance under which two claims of the same kind are
# treated as the same claim. Needs calibration on real data.
DEFAULT_SIMILARITY_THRESHOLD = 0.12

_RELATION_BY_KIND = {
    ClaimKind.FACTUAL_CLAIM: SubmissionClaimRelation.STATES,
    ClaimKind.OPINION: SubmissionClaimRelation.STATES,
    ClaimKind.VERIFICATION_QUESTION: SubmissionClaimRelation.ASKS,
}


@dataclass(frozen=True)
class StoredClaim:
    claim_id: uuid.UUID
    text: str  # wording extracted from this submission
    canonical_text: str  # wording of the stored claim it was linked to
    kind: ClaimKind
    relation: SubmissionClaimRelation
    match: str  # "new" | "exact" | "similar:<distance>"
    # Search hints from this submission's extraction (not persisted).
    search_queries: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    general_knowledge: bool = False
    ambiguities: tuple[str, ...] = ()
    open_question: bool = False


@dataclass(frozen=True)
class IntakeResult:
    submission_id: uuid.UUID
    run_id: uuid.UUID
    status: RunStatus
    claims: list[StoredClaim] = field(default_factory=list)
    message: str | None = None  # user-facing reason for a non-OK status
    # Redacted submission text the claims came from (context for later steps).
    input_text: str = ""


class ClaimIntake:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        preparer: InputPreparer,
        extractor: ClaimExtractor,
        embedder: EmbeddingAdapter | None = None,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        retention: timedelta = timedelta(days=30),
    ) -> None:
        self.session_factory = session_factory
        self.preparer = preparer
        self.extractor = extractor
        self.embedder = embedder
        self.similarity_threshold = similarity_threshold
        self.retention = retention

    # -- entry points (one per input type) -------------------------------- #

    def submit_text(
        self, text: str, *, channel: Channel, question: bool = False, sender_hash: str | None = None
    ) -> IntakeResult:
        redacted, _ = redact(text)
        return self._process(
            lambda: self.preparer.prepare_text(redacted, question=question),
            channel=channel,
            redacted_text=redacted,
            url=None,
            sender_hash=sender_hash,
        )

    def submit_url(self, url: str, *, channel: Channel, sender_hash: str | None = None) -> IntakeResult:
        return self._process(
            lambda: self.preparer.prepare_url(url),
            channel=channel,
            redacted_text=None,
            url=url,
            sender_hash=sender_hash,
        )

    def submit_prepared(
        self, prepared: PreparedInput, *, channel: Channel, sender_hash: str | None = None
    ) -> IntakeResult:
        """For inputs prepared elsewhere (media readings, segment files)."""
        return self._process(
            lambda: prepared, channel=channel, redacted_text=None, url=None, sender_hash=sender_hash
        )

    # -- pipeline ---------------------------------------------------------- #

    def _process(
        self,
        prepare: Callable[[], PreparedInput],
        *,
        channel: Channel,
        redacted_text: str | None,
        url: str | None,
        sender_hash: str | None,
    ) -> IntakeResult:
        started = time.monotonic()
        submission_id, run_id = self._open(channel, redacted_text, url, sender_hash)
        models: list[dict[str, Any]] = []
        try:
            prepared = prepare()
            prepared = self._redact_segments(prepared)
            # Keep the user's own (media-read) text; for URLs store only the
            # link, never a copy of the article.
            if redacted_text is None and url is None:
                redacted_text = "\n\n".join(segment.text for segment in prepared.segments)
            if prepared.preparation_model is not None:
                models.append(_usage("preparation", prepared.preparation_model,
                                     prepared.preparation_model_version,
                                     prepared.input_tokens, prepared.output_tokens))
            extraction = self.extractor.extract(prepared.segments)
            models.append(_usage("extraction", extraction.requested_model,
                                 extraction.model_version,
                                 extraction.input_tokens, extraction.output_tokens))
            embeddings = self._embed(extraction, models)
        except NoExtractableContentError as exc:
            return self._close(submission_id, run_id, started, models, RunStatus.NEEDS_CLARIFICATION,
                               "Nie udało się odczytać treści do sprawdzenia.", error=str(exc),
                               redacted_text=redacted_text)
        except ValidationError as exc:  # malformed model output (subclass of ValueError)
            return self._close(submission_id, run_id, started, models, RunStatus.TECH_FAILURE,
                               "Błąd techniczny podczas analizy. Spróbuj ponownie później.",
                               error=f"invalid model output: {exc.error_count()} errors",
                               redacted_text=redacted_text)
        except ValueError as exc:  # rejected input: bad/non-public URL, size limits, ...
            return self._close(submission_id, run_id, started, models, RunStatus.NEEDS_CLARIFICATION,
                               "Nie można przetworzyć tego zgłoszenia. Sprawdź link lub treść.",
                               error=str(exc), redacted_text=redacted_text)
        except Exception as exc:  # provider outage, rate limit, missing key, timeout, ...
            return self._close(submission_id, run_id, started, models, RunStatus.TECH_FAILURE,
                               "Błąd techniczny podczas analizy. Spróbuj ponownie później.",
                               error=f"{type(exc).__name__}: {exc}", redacted_text=redacted_text)

        if not extraction.data.claims:
            return self._close(submission_id, run_id, started, models, RunStatus.NEEDS_CLARIFICATION,
                               "Nie znaleziono twierdzeń do sprawdzenia. Opisz, co dokładnie chcesz zweryfikować.",
                               redacted_text=redacted_text)

        stored = self._store_claims(submission_id, extraction, embeddings)
        result = self._close(submission_id, run_id, started, models, RunStatus.OK, None,
                             redacted_text=redacted_text)
        input_text = "\n\n".join(segment.text for segment in prepared.segments)
        return IntakeResult(submission_id, run_id, result.status, stored, input_text=input_text)

    @staticmethod
    def _redact_segments(prepared: PreparedInput) -> PreparedInput:
        segments = [
            segment.model_copy(update={"text": redact(segment.text)[0]})
            for segment in prepared.segments
        ]
        return prepared.model_copy(update={"segments": segments})

    def _embed(
        self, extraction: StructuredResult[ClaimExtraction], models: list[dict[str, Any]]
    ) -> list[tuple[list[float], str] | None]:
        """Embeddings only improve deduplication, so a provider failure must not
        fail the submission: those claims are matched by exact text instead."""
        if self.embedder is None:
            return [None] * len(extraction.data.claims)
        vectors: list[tuple[list[float], str] | None] = []
        try:
            for claim in extraction.data.claims:
                result = self.embedder.embed(claim.text)
                vectors.append((result.values, result.model))
        except Exception as exc:
            log.warning("Claim embedding failed; falling back to exact-text matching", exc_info=True)
            models.append({"role": "embedding", "error": f"{type(exc).__name__}", "fallback": "exact_text"})
            return [None] * len(extraction.data.claims)
        if vectors:
            models.append({"role": "embedding", "model": vectors[0][1], "calls": len(vectors)})
        return vectors

    def _open(
        self, channel: Channel, redacted_text: str | None, url: str | None, sender_hash: str | None
    ) -> tuple[uuid.UUID, uuid.UUID]:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session, session.begin():
            submission = Submission(
                channel=channel,
                sender_hash=sender_hash,
                received_at=now,
                redacted_text=redacted_text,
                url=url,
                expires_at=now + self.retention,
            )
            session.add(submission)
            session.flush()
            run = VerificationRun(
                submission_id=submission.id,
                status=RunStatus.RUNNING,
                started_at=now,
                pipeline_version=PIPELINE_VERSION,
            )
            session.add(run)
            session.flush()
            return submission.id, run.id

    def _store_claims(
        self,
        submission_id: uuid.UUID,
        extraction: StructuredResult[ClaimExtraction],
        embeddings: list[tuple[list[float], str] | None],
    ) -> list[StoredClaim]:
        stored: list[StoredClaim] = []
        links: dict[uuid.UUID, SubmissionClaim] = {}
        with self.session_factory() as session, session.begin():
            for extracted, vector in zip(extraction.data.claims, embeddings, strict=True):
                kind = ClaimKind(extracted.kind)
                claim, match = self._get_or_create(session, extracted.text, kind, vector)
                references = [ref.model_dump(mode="json") for ref in extracted.references]
                link = links.get(claim.id)
                if link is None:
                    link = SubmissionClaim(
                        submission_id=submission_id,
                        claim_id=claim.id,
                        relation=_RELATION_BY_KIND[kind],
                        extracted_text=extracted.text,
                        references=references,
                        ambiguities=list(extracted.ambiguities),
                        match=match,
                    )
                    session.add(link)
                    links[claim.id] = link
                else:
                    # Two extracted items resolved to the same claim: merge provenance.
                    link.references = [*link.references, *references]
                    link.ambiguities = [*link.ambiguities, *extracted.ambiguities]
                stored.append(
                    StoredClaim(
                        claim_id=claim.id,
                        text=extracted.text,
                        canonical_text=claim.canonical_text,
                        kind=kind,
                        relation=link.relation,
                        match=match,
                        search_queries=tuple(extracted.search_queries),
                        countries=tuple(extracted.countries),
                        general_knowledge=extracted.general_knowledge,
                        ambiguities=tuple(extracted.ambiguities),
                        open_question=kind == ClaimKind.VERIFICATION_QUESTION
                        and (extracted.open_question or is_open_question(extracted.text)),
                    )
                )
        return stored

    def _get_or_create(
        self,
        session: Session,
        text: str,
        kind: ClaimKind,
        vector: tuple[list[float], str] | None,
    ) -> tuple[Claim, str]:
        existing = claim_repo.find_exact(session, text, kind)
        if existing is not None:
            return existing, "exact"
        if vector is not None:
            similar = claim_repo.find_similar(
                session,
                vector[0],
                kind=kind,
                embedding_model=vector[1],
                max_distance=self.similarity_threshold,
            )
            if similar is not None and claim_repo.same_specifics(text, similar[0].canonical_text):
                return similar[0], f"similar:{similar[1]:.3f}"
        claim = claim_repo.create_claim(
            session,
            text,
            kind=kind,
            origin=ClaimOrigin.USER,
            embedding=vector[0] if vector else None,
            embedding_model=vector[1] if vector else None,
        )
        return claim, "new"

    def _close(
        self,
        submission_id: uuid.UUID,
        run_id: uuid.UUID,
        started: float,
        models: list[dict[str, Any]],
        status: RunStatus,
        message: str | None,
        *,
        error: str | None = None,
        redacted_text: str | None = None,
    ) -> IntakeResult:
        with self.session_factory() as session, session.begin():
            run = session.get_one(VerificationRun, run_id)
            run.status = status
            run.finished_at = datetime.now(timezone.utc)
            run.latency_ms = int((time.monotonic() - started) * 1000)
            run.models = models
            run.error = error
            if redacted_text is not None:
                submission = session.get_one(Submission, submission_id)
                if submission.redacted_text is None:
                    submission.redacted_text = redacted_text
        return IntakeResult(submission_id, run_id, status, message=message)


def _usage(
    role: str, model: str, version: str | None, input_tokens: int | None, output_tokens: int | None
) -> dict[str, Any]:
    return {
        "role": role,
        "model": model,
        "model_version": version,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }

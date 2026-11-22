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
from sqlalchemy import select
from sqlalchemy.orm import Session

from prawdowid.ai.adapters.embedding import EmbeddingAdapter
from prawdowid.ai.adapters.llm import ContentBlocked, StructuredResult
from prawdowid.domain.claims import ClaimExtraction
from prawdowid.domain.enums import (
    Channel,
    ClaimKind,
    ClaimOrigin,
    RunStatus,
    SubmissionClaimRelation,
)
from prawdowid.ai.adapters.decision import DecisionAdapter
from prawdowid.ai.adapters.media import Media
from prawdowid.domain.claims import InputSegment
from prawdowid.domain.preparation import MediaReadout, PreparedInput
from prawdowid.persistence import claims as claim_repo
from prawdowid.persistence.models import Claim, Submission, SubmissionClaim, VerificationRun
from prawdowid.security.injection import dedupe_injection_flags, detect_prompt_injections
from prawdowid.security.redaction import redact
from prawdowid.services.claims import ClaimExtractor, is_open_question
from prawdowid.services.answer_cache import same_claims
from prawdowid.services.preparation import IMAGE_TYPES, InputPreparer, NoExtractableContentError
from prawdowid.services.usage import estimate_cost, model_prices, usage_entry

PIPELINE_VERSION = "intake-0.1"
CONTENT_BLOCKED_MESSAGE = (
    "Nie mogę automatycznie przeanalizować tej treści: model językowy odmówił jej przetworzenia "
    "(np. z powodu drastycznych szczegółów). To nie jest ocena prawdziwości ani błąd techniczny. "
    "Sprawdź komunikaty Policji, prokuratury lub serwisów fact-checkingowych."
)

log = logging.getLogger(__name__)

# Provisional: cosine distance under which two claims of the same kind are
# treated as the same claim. Needs calibration on real data.
DEFAULT_SIMILARITY_THRESHOLD = 0.12
# Image embeddings barely separate a re-encoded copy (0.012) from the same
# post with a changed word and number (0.016): only the very same image reuses
# an earlier reading.
SAME_IMAGE_DISTANCE = 0.003

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
    # Verbatim (redacted) input passages this claim was extracted from.
    source_quotes: tuple[str, ...] = ()
    # English rendering from the extractor, for searching English sources.
    text_en: str | None = None


@dataclass(frozen=True)
class IntakeResult:
    submission_id: uuid.UUID
    run_id: uuid.UUID
    status: RunStatus
    claims: list[StoredClaim] = field(default_factory=list)
    message: str | None = None  # user-facing reason for a non-OK status
    # Redacted submission text the claims came from (context for later steps).
    input_text: str = ""
    # The link the user submitted and its (redacted) page text, if any.
    source_url: str | None = None
    source_text: str = ""
    # Description and full text read from an uploaded image or recording.
    media: MediaReadout | None = None


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
        keep_run_open: bool = False,
        decision: DecisionAdapter | None = None,
    ) -> None:
        """``decision``: confirms that a claim found by vector similarity is
        really the same claim before the two are merged."""
        self.session_factory = session_factory
        self.preparer = preparer
        self.extractor = extractor
        self.embedder = embedder
        self.similarity_threshold = similarity_threshold
        self.retention = retention
        # True when a later stage (VerificationPipeline) continues the run: a
        # successful intake then records its usage but leaves the run
        # ``running``, so a crash mid-check never leaves an "ok" run without
        # verdicts. The later stage closes it.
        self.keep_run_open = keep_run_open
        self.decision = decision

    def read_media(self, media: Media, *, source_name: str) -> PreparedInput:
        """Reads an upload; the very same image submitted before (by embedding)
        reuses that reading instead of calling the reading model again."""
        vector = self._embed_media(media)
        if vector is not None:
            cached = self._cached_reading(*vector)
            if cached is not None:
                return cached
        prepared = self.preparer.prepare_media(media, source_name=source_name)
        if vector is None:
            return prepared
        return prepared.model_copy(update={"media_embedding": vector[0], "media_embedding_model": vector[1]})

    def _embed_media(self, media: Media) -> tuple[list[float], str] | None:
        if self.embedder is None or media.mime_type not in IMAGE_TYPES:
            return None
        try:
            result = self.embedder.embed(media)
        except Exception:
            log.warning("Image embedding failed; reading the image without the cache", exc_info=True)
            return None
        return result.values, result.model

    def _cached_reading(self, vector: list[float], model: str) -> PreparedInput | None:
        with self.session_factory() as session:
            distance = Submission.media_embedding.cosine_distance(vector)
            row = session.execute(
                select(Submission.media_reading, distance)
                .where(
                    Submission.media_embedding_model == model,
                    Submission.media_reading.is_not(None),
                    Submission.expires_at > datetime.now(timezone.utc),
                )
                .order_by(distance)
                .limit(1)
            ).first()
        if row is None or row[1] > SAME_IMAGE_DISTANCE:
            return None
        reading = row[0]
        log.info("Same image submitted before (distance %.4f): reusing its reading", row[1])
        return PreparedInput(
            segments=[InputSegment.model_validate(segment) for segment in reading["segments"]],
            media_description=reading.get("description"),
            media_text_complete=reading.get("text_complete"),
            media_embedding=vector,
            media_embedding_model=model,
            reused_reading=True,
        )

    # -- entry points (one per input type) -------------------------------- #

    def record_blocked(
        self,
        text: str,
        *,
        channel: Channel,
        blocked_by: list[str],
        scores: dict[str, float],
        message: str,
        sender_hash: str | None = None,
    ) -> IntakeResult:
        """Records input rejected by the injection guard: nothing is checked,
        but the attempt shows up for the administrator like any other flag."""
        redacted, _ = redact(text)
        started = time.monotonic()
        submission_id, run_id = self._open(channel, redacted, None, sender_hash)
        # The guard's own flag first: it wins deduplication against a heuristic
        # match on the same excerpt.
        flags = [{
            "where": "input:text",
            "claim_id": None,
            "excerpt": redacted[:500],
            "detected_by": "guard:" + ",".join(blocked_by),
            "scores": {name: round(score, 3) for name, score in scores.items()},
        }]
        flags.extend(detect_prompt_injections(redacted, where="input:text", detected_by="input_heuristic"))
        return self._close(
            submission_id, run_id, started, [], RunStatus.OUT_OF_SCOPE, message,
            error="blocked by the injection guard", redacted_text=redacted,
            injection_flags=dedupe_injection_flags(flags),
        )

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

    def submit_deferred(
        self,
        prepare: Callable[[], PreparedInput],
        *,
        channel: Channel,
        url: str | None = None,
        sender_hash: str | None = None,
    ) -> IntakeResult:
        """Runs ``prepare`` (fetching a URL, reading media) *after* the run row
        is opened, so its failures are recorded like any other stage's."""
        return self._process(prepare, channel=channel, redacted_text=None, url=url, sender_hash=sender_hash)

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
        media: MediaReadout | None = None
        injection_flags: list[dict[str, Any]] = []
        if redacted_text:
            injection_flags.extend(
                detect_prompt_injections(
                    redacted_text,
                    where=url or "input:text",
                    detected_by="input_heuristic",
                )
            )
        try:
            prepared = prepare()
            prepared = self._redact_segments(prepared)
            for hidden_excerpt in prepared.injection_excerpts:
                injection_flags.append(
                    {
                        "where": url or "input:html",
                        "claim_id": None,
                        "excerpt": hidden_excerpt[:500],
                        "detected_by": "hidden_html_heuristic",
                    }
                )
            for segment in prepared.segments:
                where = segment.source_url or url or f"input:{segment.origin}"
                injection_flags.extend(
                    detect_prompt_injections(
                        segment.text,
                        where=where,
                        detected_by="input_heuristic",
                    )
                )
            injection_flags = dedupe_injection_flags(injection_flags)
            media = _media_readout(prepared)
            if prepared.media_embedding is not None:
                self._remember_media(submission_id, prepared, media, models)
            # Keep the user's own (media-read) text; for URLs store only the
            # link, never a copy of the article.
            if redacted_text is None and url is None:
                redacted_text = "\n\n".join(segment.text for segment in prepared.segments)
            if prepared.preparation_model is not None:
                models.append(_usage("preparation", prepared.preparation_model,
                                     prepared.preparation_model_version,
                                     prepared.input_tokens, prepared.output_tokens))
            extraction = self.extractor.extract(prepared.segments)
            for model_excerpt in extraction.data.injection_excerpts:
                injection_flags.append(
                    {
                        "where": url or f"input:{prepared.segments[0].origin}",
                        "claim_id": None,
                        "excerpt": model_excerpt[:500],
                        "detected_by": "extraction_model",
                    }
                )
            injection_flags = dedupe_injection_flags(injection_flags)
            models.append(_usage("extraction", extraction.requested_model,
                                 extraction.model_version,
                                 extraction.input_tokens, extraction.output_tokens))
            embeddings = self._embed(extraction, models)
        except ContentBlocked as exc:  # a refusal, not an outage: retrying will not help
            return self._close(submission_id, run_id, started, models, RunStatus.OUT_OF_SCOPE,
                               CONTENT_BLOCKED_MESSAGE, error=str(exc), redacted_text=redacted_text,
                               injection_flags=injection_flags)
        except NoExtractableContentError as exc:
            description = redact(exc.media_description)[0] if exc.media_description else None
            return self._close(submission_id, run_id, started, models, RunStatus.NEEDS_CLARIFICATION,
                               exc.user_message or "Nie udało się odczytać treści do sprawdzenia.", error=str(exc),
                               redacted_text=redacted_text,
                               media=MediaReadout(description=description, text="", text_complete=True)
                               if description else None,
                               injection_flags=injection_flags)
        except ValidationError as exc:  # malformed model output (subclass of ValueError)
            return self._close(submission_id, run_id, started, models, RunStatus.TECH_FAILURE,
                               "Błąd techniczny podczas analizy. Spróbuj ponownie później.",
                               error=f"invalid model output: {exc.error_count()} errors",
                               redacted_text=redacted_text, injection_flags=injection_flags)
        except ValueError as exc:  # rejected input: bad/non-public URL, size limits, ...
            return self._close(submission_id, run_id, started, models, RunStatus.NEEDS_CLARIFICATION,
                               "Nie można przetworzyć tego zgłoszenia. Sprawdź link lub treść.",
                               error=str(exc), redacted_text=redacted_text,
                               injection_flags=injection_flags)
        except Exception as exc:  # provider outage, rate limit, missing key, timeout, ...
            return self._close(submission_id, run_id, started, models, RunStatus.TECH_FAILURE,
                               "Błąd techniczny podczas analizy. Spróbuj ponownie później.",
                               error=f"{type(exc).__name__}: {exc}", redacted_text=redacted_text,
                               injection_flags=injection_flags)

        if not extraction.data.claims:
            message = (
                "Ta strona nie zawiera twierdzeń do sprawdzenia. "
                "Wklej link ponownie i zadaj pytanie o konkretną informację z tej strony."
                if url is not None
                else "Nie znaleziono twierdzeń do sprawdzenia. Opisz, co dokładnie chcesz zweryfikować."
            )
            return self._close(submission_id, run_id, started, models, RunStatus.NEEDS_CLARIFICATION,
                               message, redacted_text=redacted_text, media=media,
                               injection_flags=injection_flags)

        stored = self._store_claims(submission_id, extraction, embeddings)
        self._close(submission_id, run_id, started, models, RunStatus.OK, None,
                    redacted_text=redacted_text, finish=not self.keep_run_open,
                    injection_flags=injection_flags)
        input_text = "\n\n".join(segment.text for segment in prepared.segments)
        source_text = "\n".join(segment.text for segment in prepared.segments if segment.origin == "article")
        return IntakeResult(
            submission_id, run_id, RunStatus.OK, stored, input_text=input_text,
            source_url=url, source_text=source_text, media=media,
        )

    def _remember_media(
        self, submission_id: uuid.UUID, prepared: PreparedInput, media: MediaReadout | None,
        models: list[dict[str, Any]],
    ) -> None:
        models.append({"role": "embedding", "model": prepared.media_embedding_model, "calls": 1, "input": "image"})
        if prepared.reused_reading:
            models.append({"role": "preparation", "cached": True})
        with self.session_factory() as session, session.begin():
            submission = session.get_one(Submission, submission_id)
            submission.media_embedding = prepared.media_embedding
            submission.media_embedding_model = prepared.media_embedding_model
            submission.media_reading = {
                # Only the (redacted) image text: never the asker's own question.
                "segments": [
                    segment.model_dump(mode="json") for segment in prepared.segments if segment.origin in MEDIA_ORIGINS
                ],
                "description": media.description if media else None,
                "text_complete": media.text_complete if media else True,
            }

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
        confirmed = self._confirm_similar(extraction, embeddings)
        with self.session_factory() as session, session.begin():
            for index, (extracted, vector) in enumerate(zip(extraction.data.claims, embeddings, strict=True)):
                kind = ClaimKind(extracted.kind)
                claim, match = self._get_or_create(
                    session, extracted.text, kind, vector, allow_similar=confirmed is None or index in confirmed
                )
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
                        source_quotes=tuple(ref.quote for ref in extracted.references if ref.quote.strip()),
                        text_en=(extracted.text_en or "").strip() or None,
                    )
                )
        return stored

    def _confirm_similar(
        self, extraction: StructuredResult[ClaimExtraction], embeddings: list[tuple[list[float], str] | None]
    ) -> set[int] | None:
        """Indices of claims whose nearest stored claim the decision model
        confirmed as the same claim; None without a decision model (then the
        vector distance and the numbers/negation check decide alone)."""
        if self.decision is None:
            return None
        pairs: dict[str, tuple[str, str]] = {}
        with self.session_factory() as session:
            for index, (extracted, vector) in enumerate(zip(extraction.data.claims, embeddings, strict=True)):
                kind = ClaimKind(extracted.kind)
                if vector is None or claim_repo.find_exact(session, extracted.text, kind) is not None:
                    continue
                similar = self._similar(session, extracted.text, kind, vector)
                if similar is not None:
                    pairs[str(index)] = (extracted.text, similar.canonical_text)
        return {int(key) for key in same_claims(self.decision, pairs)}

    def _similar(
        self, session: Session, text: str, kind: ClaimKind, vector: tuple[list[float], str]
    ) -> Claim | None:
        similar = claim_repo.find_similar(
            session, vector[0], kind=kind, embedding_model=vector[1], max_distance=self.similarity_threshold
        )
        if similar is not None and claim_repo.same_specifics(text, similar[0].canonical_text):
            return similar[0]
        return None

    def _get_or_create(
        self,
        session: Session,
        text: str,
        kind: ClaimKind,
        vector: tuple[list[float], str] | None,
        *,
        allow_similar: bool = True,
    ) -> tuple[Claim, str]:
        existing = claim_repo.find_exact(session, text, kind)
        if existing is not None:
            return existing, "exact"
        if vector is not None and allow_similar:
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
        finish: bool = True,
        media: MediaReadout | None = None,
        injection_flags: list[dict[str, Any]] | None = None,
    ) -> IntakeResult:
        with self.session_factory() as session, session.begin():
            run = session.get_one(VerificationRun, run_id)
            run.models = models
            if injection_flags:
                run.injection_flags = dedupe_injection_flags(
                    [*(run.injection_flags or []), *injection_flags]
                )
            if finish:
                run.status = status
                run.finished_at = datetime.now(timezone.utc)
                run.latency_ms = int((time.monotonic() - started) * 1000)
                run.est_cost_usd = estimate_cost(models, model_prices())
            run.error = error
            if redacted_text is not None:
                submission = session.get_one(Submission, submission_id)
                if submission.redacted_text is None:
                    submission.redacted_text = redacted_text
        return IntakeResult(submission_id, run_id, status, message=message, media=media)


MEDIA_ORIGINS = {"ocr", "audio_transcript", "video_frame"}


def _media_readout(prepared: PreparedInput) -> MediaReadout | None:
    if prepared.media_description is None and prepared.media_text_complete is None:
        return None
    return MediaReadout(
        description=redact(prepared.media_description)[0] if prepared.media_description else "",
        text="\n\n".join(segment.text for segment in prepared.segments if segment.origin in MEDIA_ORIGINS),
        text_complete=prepared.media_text_complete is not False,
    )


_usage = usage_entry

"""POST /check: verify a piece of information.

One endpoint for every input type – text, link, question, picture or video –
sent as multipart/form-data so files can be uploaded. The request and response
schemas are the contract for the web app and the other channels; they appear
in the OpenAPI docs at /docs.

Before anything runs, every user-typed text field is screened for prompt
injection and jailbreaks by JEV and the Guardrails ``DetectJailbreak``
validator (``security.injection_guard``); a flagged field is rejected with 422
and no run is opened.

Pipeline: ``ClaimIntake`` (redaction, extraction, dedup, storage) is followed
by ``EvidenceSearchOrchestrator`` (Google Fact Check + EUvsDisinfo, always in
parallel), ``SublinkSelector`` (a decision call picks which results to fetch
in full for an exact quote) and one batched ``AssessmentGenerator`` call.
Technical failures (missing keys, provider outages, invalid model output) are
returned as ``status=tech_failure`` with no claims, never as a fabricated
merit result.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, HttpUrl, ValidationError, TypeAdapter

from prawdowid.ai.adapters.media import Media
from prawdowid.api.dependencies import PipelineUnavailable, build_pipeline
from prawdowid.api.rate_limit import admit_check
from prawdowid.domain.claims import InputSegment
from prawdowid.domain.enums import Channel, ClaimKind, RunStatus, Strength, Verdict
from prawdowid.domain.preparation import PreparedInput
from prawdowid.services.pipeline import PipelineResult, VerificationPipeline
from prawdowid.services.preparation import (
    IMAGE_TYPES,
    MAX_MEDIA_BYTES,
    MAX_QUESTION_CHARS,
    MAX_TEXT_CHARS,
    MAX_URL_CHARS,
    VIDEO_TYPES,
    NoExtractableContentError,
)
from prawdowid.services.social import social_link_message, social_platform
from prawdowid.security.attestation import (
    SignableClaim,
    compute_answer_hash,
    compute_claim_hash,
    format_claim_text,
    get_signing_key,
    normalize_for_hash,
)
from prawdowid.security.injection_guard import InjectionGuard, get_injection_guard_factory

router = APIRouter(tags=["check"], dependencies=[Depends(admit_check)])
log = logging.getLogger(__name__)

MAX_TEXT_LENGTH = MAX_TEXT_CHARS
TECH_FAILURE_MESSAGE = "Błąd techniczny podczas sprawdzania. Spróbuj ponownie później."
INVALID_INPUT_MESSAGE = "Nie udało się odczytać treści do sprawdzenia."
INJECTION_BLOCKED_MESSAGE = (
    "Zgłoszenie wygląda na próbę zmiany działania systemu, a nie na treść do sprawdzenia. "
    "Wpisz po prostu informację lub pytanie, które chcesz zweryfikować."
)

_HTTP_URL = TypeAdapter(HttpUrl)


class InputType(StrEnum):
    TEXT = "text"  # a message, post or quote
    URL = "url"  # a link to an article, post or recording
    QUESTION = "question"  # a direct question ("Czy ...?")
    IMAGE = "image"  # a picture or screenshot
    VIDEO = "video"  # a video recording


ALLOWED_MEDIA_TYPES: dict[InputType, frozenset[str]] = {
    InputType.IMAGE: frozenset(IMAGE_TYPES),
    InputType.VIDEO: frozenset(VIDEO_TYPES),
}


# -- response schema (target contract) --------------------------------------- #


class Source(BaseModel):
    title: str
    url: HttpUrl
    publisher: str = Field(description="Wydawca, np. Demagog, gov.pl.")
    published_at: datetime | None = Field(
        default=None, description="Data publikacji (lub weryfikacji) źródła; brak, gdy źródło jej nie podaje."
    )
    quote: str | None = Field(default=None, description="Dosłowny fragment źródła, na którym oparto ocenę.")


class ClaimResult(BaseModel):
    text: str = Field(description="Twierdzenie wyodrębnione ze zgłoszenia.")
    kind: ClaimKind
    verdict: Verdict = Field(description="Ocena. Brak dowodów to `insufficient_evidence`, nie błąd.")
    strength: Strength = Field(description="Siła podstawy odpowiedzi.")
    explanation: str = Field(description="Krótkie uzasadnienie prostym językiem.")
    limitations: str | None = Field(default=None, description="Ograniczenia odpowiedzi i źródeł.")
    basis: Literal["evidence", "general_knowledge"] = Field(
        default="evidence",
        description="`general_knowledge`: odpowiedź z wiedzy ogólnej modelu, bez źródła (zawsze słaba).",
    )
    sources: list[Source] = Field(description="Źródła, na których oparto ocenę. Puste, gdy ich brak.")
    missing_details: list[str] = Field(
        default_factory=list,
        description="Gdy brak werdyktu: co trzeba doprecyzować (kto, kiedy, gdzie), żeby dało się to sprawdzić.",
    )
    answer_hash: str | None = Field(
        default=None,
        description="128-bitowy podpis HMAC-SHA256 (32 znaki hex) liczony wyłącznie dla tego stwierdzenia.",
    )
    signed_text: str | None = Field(
        default=None,
        description="Kanoniczna treść tego stwierdzenia, na podstawie której wyliczono `answer_hash`.",
    )


class MediaInfo(BaseModel):
    description: str = Field(description="Krótki opis, co przedstawia obraz lub nagranie.")
    text: str = Field(description="Cały tekst odczytany z obrazu lub nagrania, w kolejności czytania.")
    text_complete: bool = Field(description="Fałsz, gdy część tekstu była ucięta, rozmazana lub nieczytelna.")


class CheckResponse(BaseModel):
    run_id: str = Field(description="Identyfikator przebiegu sprawdzenia (zapis w bazie).")
    status: RunStatus = Field(
        description="Status przetwarzania. Błąd techniczny (`tech_failure`) nigdy nie jest werdyktem."
    )
    message: str | None = Field(default=None, description="Komunikat dla użytkownika przy statusie innym niż `ok`.")
    claims: list[ClaimResult] = Field(default_factory=list)
    media: MediaInfo | None = Field(default=None, description="Co odczytano z przesłanego obrazu lub nagrania.")
    skipped_claims: list[str] = Field(
        default_factory=list,
        description="Poboczne twierdzenia ze zgłoszenia, których nie sprawdzano (sprawdzane są tylko kluczowe).",
    )
    answer_hash: str | None = Field(
        default=None,
        description="128-bitowy podpis komunikatu (wypełniany tylko gdy `claims` jest puste; gdy są twierdzenia, każde ma własny `answer_hash`).",
    )
    signed_text: str | None = Field(
        default=None,
        description="Kanoniczna treść komunikatu dla `answer_hash` (gdy `claims` jest puste).",
    )


class ErrorResponse(BaseModel):
    detail: str


# -- validation helpers ------------------------------------------------------ #


def _invalid(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


def _matches_signature(data: bytes, mime_type: str) -> bool:
    """Cheap magic-byte check: the declared type is client-controlled."""
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime_type in {"video/mp4", "video/mov", "video/quicktime"}:
        return data[4:8] == b"ftyp"
    if mime_type == "video/webm":
        return data.startswith(b"\x1a\x45\xdf\xa3")
    return False


async def _read_media(file: UploadFile, input_type: InputType) -> bytes:
    allowed = ALLOWED_MEDIA_TYPES[input_type]
    mime_type = (file.content_type or "").lower()
    if mime_type not in allowed:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Nieobsługiwany format pliku. Dozwolone: {', '.join(sorted(allowed))}.",
        )
    data = await file.read(MAX_MEDIA_BYTES + 1)
    if not data:
        raise _invalid("Przesłany plik jest pusty.")
    if len(data) > MAX_MEDIA_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Plik jest za duży. Maksymalny rozmiar to {MAX_MEDIA_BYTES // 1_000_000} MB.",
        )
    if not _matches_signature(data, mime_type):
        raise _invalid("Zawartość pliku nie odpowiada jego formatowi.")
    return data


def get_pipeline_factory() -> Callable[[], AbstractAsyncContextManager[VerificationPipeline]]:
    """Seam for tests: override with ``app.dependency_overrides`` to inject a
    context manager yielding a stub ``VerificationPipeline`` instead of the
    real, environment-configured providers."""
    return build_pipeline


async def _record_blocked(
    pipeline_factory: Callable[[], AbstractAsyncContextManager[VerificationPipeline]],
    text: str,
    channel: Channel,
    screening,
) -> None:
    """Best effort: a failure to record must not change the answer to the user."""
    try:
        async with pipeline_factory() as pipeline:
            await asyncio.to_thread(
                pipeline.intake.record_blocked, text, channel=channel, blocked_by=list(screening.blocked_by),
                scores=dict(screening.scores), message=INJECTION_BLOCKED_MESSAGE,
            )
    except Exception:
        log.warning("Recording a blocked injection attempt failed", exc_info=True)


def _prepend_question(segments: list[InputSegment], question: str) -> list[InputSegment]:
    return [InputSegment(id="question-1", text=question, origin="question"), *segments]


def _to_check_response(result: PipelineResult) -> "CheckResponse":
    media = MediaInfo(**result.media.model_dump()) if result.media is not None else None
    if result.status != RunStatus.OK:
        return CheckResponse(run_id=str(result.run_id), status=result.status, message=result.message, media=media)

    signing_key = get_signing_key()
    claims: list[ClaimResult] = []
    for outcome in result.claims:
        verdict = Verdict(outcome.assessment.verdict)
        strength = Strength(outcome.assessment.strength)
        claim_signed_text: str | None = None
        claim_hash: str | None = None
        if signing_key:
            signable = SignableClaim(
                text=outcome.text,
                verdict=verdict,
                strength=strength,
                explanation=outcome.assessment.explanation,
                limitations=outcome.assessment.limitations,
                missing_details=outcome.assessment.missing_details,
            )
            formatted = format_claim_text(signable)
            if formatted and normalize_for_hash(formatted):
                claim_signed_text = formatted
                claim_hash = compute_claim_hash(signable, secret_key=signing_key)

        claims.append(
            ClaimResult(
                text=outcome.text,
                kind=ClaimKind(outcome.kind),
                verdict=verdict,
                strength=strength,
                explanation=outcome.assessment.explanation,
                limitations=outcome.assessment.limitations,
                basis=outcome.assessment.reasons.basis,
                missing_details=outcome.assessment.missing_details,
                answer_hash=claim_hash,
                signed_text=claim_signed_text,
                sources=[
                    Source(
                        title=outcome.evidence[citation.evidence_index].item.title,
                        url=outcome.evidence[citation.evidence_index].item.url,
                        publisher=(
                            outcome.evidence[citation.evidence_index].item.publisher
                            or outcome.evidence[citation.evidence_index].item.source
                        ),
                        published_at=outcome.evidence[citation.evidence_index].item.published_at,
                        quote=citation.quote,
                    )
                    for citation in outcome.assessment.citations
                ],
            )
        )

    fallback_signed_text: str | None = None
    fallback_hash: str | None = None
    if signing_key and not claims and result.message and normalize_for_hash(result.message):
        fallback_signed_text = result.message.strip()
        fallback_hash = compute_answer_hash(fallback_signed_text, secret_key=signing_key)

    return CheckResponse(
        run_id=str(result.run_id),
        status=result.status,
        message=result.message,
        claims=claims,
        media=media,
        skipped_claims=result.skipped_claims,
        answer_hash=fallback_hash,
        signed_text=fallback_signed_text,
    )


# -- endpoint ---------------------------------------------------------------- #


@router.post(
    "/check",
    response_model=CheckResponse,
    summary="Sprawdź informację",
    description=(
        "Przyjmuje zgłoszenie jednego z typów (`type`):\n\n"
        "| type | wymagane | opcjonalne |\n"
        "| --- | --- | --- |\n"
        "| `text` | `text` – treść | – |\n"
        "| `question` | `text` – pytanie | – |\n"
        "| `url` | `url` – link | `text` – pytanie do linku |\n"
        f"| `image` | `file` – {', '.join(sorted(IMAGE_TYPES))} | `text` – pytanie do obrazu |\n"
        f"| `video` | `file` – {', '.join(sorted(VIDEO_TYPES))} | `text` – pytanie do nagrania |\n\n"
        f"Pliki do {MAX_MEDIA_BYTES // 1_000_000} MB, tekst do {MAX_TEXT_LENGTH} znaków, "
        f"pytanie do linku lub pliku do {MAX_QUESTION_CHARS} znaków, link do {MAX_URL_CHARS} znaków.\n\n"
        "Brak klucza API, limit dostawcy lub awaria sieci dają `status=tech_failure`, "
        "nigdy spreparowany werdykt. Walidacja (`413`, `415`, `422`) działa niezależnie. "
        "Liczba zgłoszeń jest ograniczona (`429`/`503` z `Retry-After`)."
    ),
    responses={
        status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse, "description": "Plik jest za duży."},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse, "description": "Nieobsługiwany format pliku."},
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": "Limit zgłoszeń klienta (na minutę lub dobę). Nagłówek `Retry-After` podaje czas oczekiwania.",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "Usługa przeciążona lub wyczerpany dzienny limit globalny. `Retry-After` jak wyżej.",
        },
        status.HTTP_501_NOT_IMPLEMENTED: {
            "model": ErrorResponse,
            "description": "Sprawdzanie nie jest jeszcze zaimplementowane.",
        },
    },
)
async def check(
    pipeline_factory: Annotated[
        Callable[[], AbstractAsyncContextManager[VerificationPipeline]], Depends(get_pipeline_factory)
    ],
    guard_factory: Annotated[Callable[[], InjectionGuard], Depends(get_injection_guard_factory)],
    input_type: Annotated[InputType, Form(alias="type", description="Rodzaj zgłoszenia.")],
    text: Annotated[
        str | None,
        # No Form(max_length): multipart sends line breaks as CRLF, so the raw
        # value is longer than what the user typed. Checked below after
        # normalising line endings.
        Form(
            description=f"Treść, pytanie albo pytanie do linku/pliku (do {MAX_TEXT_LENGTH} znaków; "
            f"pytanie do linku/pliku do {MAX_QUESTION_CHARS} znaków).",
            examples=["Czy we Włoszech ostatnio odbywał się strajk generalny?"],
        ),
    ] = None,
    url: Annotated[
        str | None,
        Form(
            max_length=MAX_URL_CHARS,
            description="Link (dla `type=url`; przy innych typach ignorowany).",
            examples=["https://example.org/artykul"],
        ),
    ] = None,
    file: Annotated[UploadFile | None, File(description="Obraz lub nagranie (dla `type=image`/`video`).")] = None,
    channel: Annotated[Channel, Form(description="Kanał, z którego przyszło zgłoszenie.")] = Channel.WEB,
) -> CheckResponse:
    # Count characters the way the form shows them: one per line break.
    text = text.replace("\r\n", "\n").replace("\r", "\n") if text else text
    text = text.strip() if text and text.strip() else None
    url = url.strip() if url and url.strip() else None
    has_file = file is not None and bool(file.filename)

    if input_type in (InputType.TEXT, InputType.QUESTION):
        if text is None:
            raise _invalid("Podaj treść do sprawdzenia (pole text).")
        if len(text) > MAX_TEXT_LENGTH:
            raise _invalid(f"Treść może mieć najwyżej {MAX_TEXT_LENGTH} znaków.")
    elif text is not None and len(text) > MAX_QUESTION_CHARS:
        raise _invalid(f"Pytanie do linku lub pliku może mieć najwyżej {MAX_QUESTION_CHARS} znaków.")
    if input_type is InputType.URL:
        if url is None:
            raise _invalid("Podaj link do sprawdzenia (pole url).")
        try:
            _HTTP_URL.validate_python(url)
        except ValidationError as exc:
            raise _invalid("Podany link jest nieprawidłowy.") from exc
        platform = social_platform(url)
        if platform is not None:
            return CheckResponse(
                run_id="", status=RunStatus.NEEDS_CLARIFICATION, message=social_link_message(platform)
            )
    # `url` is ignored for other types (Swagger UI always pre-fills it).

    media: bytes | None = None
    if input_type in ALLOWED_MEDIA_TYPES:
        if file is None or not has_file:
            raise _invalid("Dołącz plik (pole file).")
        media = await _read_media(file, input_type)
    elif has_file:
        raise _invalid("Plik jest dozwolony tylko dla type=image lub type=video.")

    # Screen what the user typed before anything else runs. Fails closed: if
    # the guard cannot run, the request is not processed.
    if text is not None:
        try:
            guard = await asyncio.to_thread(guard_factory)
            screening = await guard.screen(text)
        except Exception:
            log.exception("Injection guard unavailable; request rejected (fail closed)")
            return CheckResponse(run_id="", status=RunStatus.TECH_FAILURE, message=TECH_FAILURE_MESSAGE)
        if screening.blocked:
            # Scores and shape only: the text itself may hold personal data.
            log.warning(
                "Blocked suspected prompt injection: type=%s channel=%s chars=%d blocked_by=%s scores=%s",
                input_type.value, channel.value, len(text), ",".join(screening.blocked_by),
                " ".join(f"{name}={score:.3f}" for name, score in screening.scores.items()),
            )
            await _record_blocked(pipeline_factory, text, channel, screening)
            raise _invalid(INJECTION_BLOCKED_MESSAGE)

    try:
        async with pipeline_factory() as pipeline:
            if input_type is InputType.URL and text is None:
                result = await pipeline.run_url(url, channel=channel)  # type: ignore[arg-type]
            elif input_type in (InputType.TEXT, InputType.QUESTION):
                result = await pipeline.run_text(
                    text, channel=channel, question=input_type is InputType.QUESTION  # type: ignore[arg-type]
                )
            else:
                preparer = pipeline.intake.preparer
                mime_type = (file.content_type or "").lower() if file is not None else ""
                source_name = (file.filename if file is not None else None) or "upload"
                question = text

                def prepare() -> PreparedInput:
                    # Blocking (HTTP fetch / model reading the media): the
                    # pipeline runs it in a worker thread, after the run row
                    # exists, so a failure here is logged like any other.
                    if input_type is InputType.URL:
                        prepared = preparer.prepare_url(url)  # type: ignore[arg-type]
                    else:
                        assert media is not None
                        try:
                            prepared = pipeline.intake.read_media(
                                Media(data=media, mime_type=mime_type), source_name=source_name
                            )
                        except NoExtractableContentError as exc:
                            # A photo without text: the asker's question is all there is to check.
                            if question is None:
                                raise
                            return preparer.prepare_text(question, question=True).model_copy(
                                update={"media_description": exc.media_description, "media_text_complete": True}
                            )
                    if question is not None:
                        prepared = prepared.model_copy(
                            update={"segments": _prepend_question(prepared.segments, question)}
                        )
                    return prepared

                result = await pipeline.run_deferred(
                    prepare, channel=channel, url=url if input_type is InputType.URL else None
                )
    except PipelineUnavailable as exc:
        log.error("Pipeline unavailable: %s", exc)
        return CheckResponse(run_id="", status=RunStatus.TECH_FAILURE, message=str(exc))
    except (NoExtractableContentError, ValueError) as exc:
        log.info("Input could not be prepared: %s", exc)
        return CheckResponse(run_id="", status=RunStatus.NEEDS_CLARIFICATION, message=INVALID_INPUT_MESSAGE)
    except Exception:  # provider outage, SDK bug, ... never a merit result
        # This can happen before a run exists (pipeline construction) or after
        # (an error the pipeline re-raised); in the latter case the pipeline
        # has already closed that run as tech_failure.
        log.warning("Unexpected failure in POST /check; no run_id available here", exc_info=True)
        return CheckResponse(run_id="", status=RunStatus.TECH_FAILURE, message=TECH_FAILURE_MESSAGE)

    return _to_check_response(result)


"""POST /check: verify a piece of information.

One endpoint for every input type – text, link, question, picture or video –
sent as multipart/form-data so files can be uploaded. The request and response
schemas are the contract for the web app and the other channels; they appear
in the OpenAPI docs at /docs.

Pipeline: ``ClaimIntake`` (redaction, extraction, dedup, storage) is followed
by ``EvidenceSearchOrchestrator`` (Google Fact Check + EUvsDisinfo, always in
parallel), ``SublinkSelector`` (a decision call picks which results to fetch
in full for an exact quote) and one batched ``AssessmentGenerator`` call.
Technical failures (missing keys, provider outages, invalid model output) are
returned as ``status=tech_failure`` with no claims, never as a fabricated
merit result.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
import logging
from typing import Literal, Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, HttpUrl, ValidationError, TypeAdapter

from prawdowid.ai.adapters.media import Media
from prawdowid.api.dependencies import PipelineUnavailable, build_pipeline
from prawdowid.api.rate_limit import admit_check
from prawdowid.domain.claims import InputSegment
from prawdowid.domain.enums import Channel, ClaimKind, RunStatus, Strength, Verdict
from prawdowid.services.pipeline import PipelineResult, VerificationPipeline
from prawdowid.services.preparation import IMAGE_TYPES, MAX_MEDIA_BYTES, VIDEO_TYPES, NoExtractableContentError

router = APIRouter(tags=["check"], dependencies=[Depends(admit_check)])
logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 5000
TECH_FAILURE_MESSAGE = "Błąd techniczny podczas sprawdzania. Spróbuj ponownie później."
INVALID_INPUT_MESSAGE = "Nie udało się odczytać treści do sprawdzenia."

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


class CheckResponse(BaseModel):
    run_id: str = Field(description="Identyfikator przebiegu sprawdzenia (zapis w bazie).")
    status: RunStatus = Field(
        description="Status przetwarzania. Błąd techniczny (`tech_failure`) nigdy nie jest werdyktem."
    )
    message: str | None = Field(default=None, description="Komunikat dla użytkownika przy statusie innym niż `ok`.")
    claims: list[ClaimResult] = Field(default_factory=list)


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
            detail=f"Plik jest za duży (maksymalnie {MAX_MEDIA_BYTES // 1_000_000} MB).",
        )
    if not _matches_signature(data, mime_type):
        raise _invalid("Zawartość pliku nie odpowiada jego formatowi.")
    return data


def get_pipeline_factory() -> Callable[[], AbstractAsyncContextManager[VerificationPipeline]]:
    """Seam for tests: override with ``app.dependency_overrides`` to inject a
    context manager yielding a stub ``VerificationPipeline`` instead of the
    real, environment-configured providers."""
    return build_pipeline


def _prepend_question(segments: list[InputSegment], question: str) -> list[InputSegment]:
    return [InputSegment(id="question-1", text=question, origin="question"), *segments]


def _to_check_response(result: PipelineResult) -> "CheckResponse":
    if result.status != RunStatus.OK:
        return CheckResponse(run_id=str(result.run_id), status=result.status, message=result.message)
    claims = [
        ClaimResult(
            text=outcome.text,
            kind=ClaimKind(outcome.kind),
            verdict=Verdict(outcome.assessment.verdict),
            strength=Strength(outcome.assessment.strength),
            explanation=outcome.assessment.explanation,
            limitations=outcome.assessment.limitations,
            basis=outcome.assessment.reasons.basis,
            sources=[
                Source(
                    title=outcome.evidence[citation.evidence_index].item.title,
                    url=outcome.evidence[citation.evidence_index].item.url,
                    publisher=(
                        outcome.evidence[citation.evidence_index].item.publisher
                        or outcome.evidence[citation.evidence_index].item.source
                    ),
                    quote=citation.quote,
                )
                for citation in outcome.assessment.citations
            ],
        )
        for outcome in result.claims
    ]
    return CheckResponse(run_id=str(result.run_id), status=result.status, claims=claims)


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
        f"Pliki do {MAX_MEDIA_BYTES // 1_000_000} MB, tekst do {MAX_TEXT_LENGTH} znaków.\n\n"
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
    input_type: Annotated[InputType, Form(alias="type", description="Rodzaj zgłoszenia.")],
    text: Annotated[
        str | None,
        Form(
            max_length=MAX_TEXT_LENGTH,
            description="Treść, pytanie albo pytanie do linku/pliku.",
            examples=["Czy we Włoszech ostatnio odbywał się strajk generalny?"],
        ),
    ] = None,
    url: Annotated[
        str | None,
        Form(
            description="Link (dla `type=url`; przy innych typach ignorowany).",
            examples=["https://example.org/artykul"],
        ),
    ] = None,
    file: Annotated[UploadFile | None, File(description="Obraz lub nagranie (dla `type=image`/`video`).")] = None,
    channel: Annotated[Channel, Form(description="Kanał, z którego przyszło zgłoszenie.")] = Channel.WEB,
) -> CheckResponse:
    text = text.strip() if text and text.strip() else None
    url = url.strip() if url and url.strip() else None
    has_file = file is not None and bool(file.filename)

    if input_type in (InputType.TEXT, InputType.QUESTION):
        if text is None:
            raise _invalid("Podaj treść do sprawdzenia (pole text).")
    if input_type is InputType.URL:
        if url is None:
            raise _invalid("Podaj link do sprawdzenia (pole url).")
        try:
            _HTTP_URL.validate_python(url)
        except ValidationError as exc:
            raise _invalid("Podany link jest nieprawidłowy.") from exc
    # `url` is ignored for other types (Swagger UI always pre-fills it).

    media: bytes | None = None
    if input_type in ALLOWED_MEDIA_TYPES:
        if file is None or not has_file:
            raise _invalid("Dołącz plik (pole file).")
        media = await _read_media(file, input_type)
    elif has_file:
        raise _invalid("Plik jest dozwolony tylko dla type=image lub type=video.")

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
                if input_type is InputType.URL:
                    prepared = preparer.prepare_url(url)  # type: ignore[arg-type]
                else:
                    assert file is not None and media is not None
                    mime_type = (file.content_type or "").lower()
                    prepared = preparer.prepare_media(Media(data=media, mime_type=mime_type),
                                                       source_name=file.filename or "upload")
                if text is not None:
                    prepared = prepared.model_copy(
                        update={"segments": _prepend_question(prepared.segments, text)}
                    )
                result = await pipeline.run_prepared(prepared, channel=channel)
    except PipelineUnavailable as exc:
        logger.error("Pipeline unavailable: %s", exc)
        return CheckResponse(run_id="", status=RunStatus.TECH_FAILURE, message=str(exc))
    except (NoExtractableContentError, ValueError) as exc:
        logger.info("Input could not be prepared: %s", exc)
        return CheckResponse(run_id="", status=RunStatus.NEEDS_CLARIFICATION, message=INVALID_INPUT_MESSAGE)
    except Exception:  # provider outage, SDK bug, ... never a merit result
        logger.warning("Unexpected failure before a run could be opened", exc_info=True)
        return CheckResponse(run_id="", status=RunStatus.TECH_FAILURE, message=TECH_FAILURE_MESSAGE)

    return _to_check_response(result)


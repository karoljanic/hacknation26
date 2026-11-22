"""POST /verify: sprawdza autentyczność odpowiedzi Prawdowida na podstawie zrzutu ekranu.

Wariant bezstanowy (A), 128-bitowy podpis HMAC-SHA256 (32 znaki hex) osobny dla każdego
kafelka ze stwierdzeniem. Jeśli na zrzucie znajduje się więcej niż jeden kafelek z hashem,
każdy jest weryfikowany niezależnie i raportowany na liście `items`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from prawdowid.ai.adapters.media import Media
from prawdowid.ai.instances.gemini import GeminiAdapter
from prawdowid.api.check import ErrorResponse, InputType, _read_media
from prawdowid.api.dependencies import PREPARATION_MODEL, THINKING_LEVEL
from prawdowid.api.rate_limit import admit_check
from prawdowid.security.attestation import HASH_HEX_LENGTH, SIGNING_KEY_ENV, get_signing_key
from prawdowid.services.attestation import (
    AnswerVerifier,
    ItemVerificationStatus,
    OverallVerificationStatus,
    VerificationResult,
)
from prawdowid.services.preparation import IMAGE_TYPES, MAX_MEDIA_BYTES

router = APIRouter(tags=["verify"], dependencies=[Depends(admit_check)])
logger = logging.getLogger(__name__)


class VerifierUnavailable(RuntimeError):
    """Brak konfiguracji wymaganej do weryfikacji (PRAWDOWID_SIGNING_KEY lub GEMINI_API_KEY)."""


class VerifiedClaimItem(BaseModel):
    claim_text: str = Field(description="Treść stwierdzenia odczytana z kafelka.")
    verdict: str | None = Field(default=None, description="Etykieta werdyktu odczytana z kafelka.")
    authentic: bool = Field(description="True tylko wtedy, gdy 128-bitowy hash tego kafelka zgadza się z jego treścią.")
    status: ItemVerificationStatus = Field(
        description="`verified` | `invalid_hash` | `missing_hash` | `unreadable`."
    )
    message: str = Field(description="Komunikat weryfikacji dla tego kafelka.")
    extracted_hash: str | None = Field(
        default=None,
        description=f"{HASH_HEX_LENGTH}-znakowy (128-bitowy) hash odczytany z tego kafelka.",
    )
    normalized_text: str | None = Field(
        default=None,
        description="Znormalizowana treść tego kafelka, dla której liczono skrót.",
    )


class VerifyResponse(BaseModel):
    authentic: bool = Field(
        description="True tylko wtedy, gdy wszystkie odczytane kafelki przeszły weryfikację podpisu."
    )
    status: OverallVerificationStatus = Field(
        description="`verified` | `partial` | `invalid_hash` | `missing_hash` | `unreadable`."
    )
    message: str = Field(description="Zbiorczy komunikat dla użytkownika.")
    items: list[VerifiedClaimItem] = Field(
        default_factory=list,
        description="Wyniki niezależnej weryfikacji każdego kafelka wykrytego na zrzucie ekranu.",
    )
    extracted_hash: str | None = Field(
        default=None,
        description=f"{HASH_HEX_LENGTH}-znakowy hash (wypełniany dla pojedynczego kafelka/komunikatu).",
    )
    normalized_text: str | None = Field(
        default=None,
        description="Znormalizowana treść (wypełniana dla pojedynczego kafelka/komunikatu).",
    )


@asynccontextmanager
async def build_verifier() -> AsyncIterator[AnswerVerifier]:
    signing_key = get_signing_key()
    if not signing_key:
        raise VerifierUnavailable(
            f"Weryfikacja podpisu nie jest skonfigurowana (brak zmiennej {SIGNING_KEY_ENV})."
        )

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        raise VerifierUnavailable(
            "Weryfikacja zrzutu ekranu wymaga skonfigurowanej zmiennej GEMINI_API_KEY."
        )

    async with AsyncExitStack() as stack:
        genai_client = stack.enter_context(
            genai.Client(api_key=gemini_key, http_options=types.HttpOptions(timeout=30000))
        )
        llm = GeminiAdapter(genai_client, PREPARATION_MODEL, thinking_level=THINKING_LEVEL)
        yield AnswerVerifier(secret_key=signing_key, llm=llm)


def get_verifier_factory() -> Callable[[], AbstractAsyncContextManager[AnswerVerifier]]:
    """Punkt podmiany dla testów (``app.dependency_overrides``)."""
    return build_verifier


def _to_verify_response(result: VerificationResult) -> VerifyResponse:
    return VerifyResponse(
        authentic=result.authentic,
        status=result.status,
        message=result.message,
        items=[
            VerifiedClaimItem(
                claim_text=item.claim_text,
                verdict=item.verdict,
                authentic=item.authentic,
                status=item.status,
                message=item.message,
                extracted_hash=item.extracted_hash,
                normalized_text=item.normalized_text,
            )
            for item in result.items
        ],
        extracted_hash=result.extracted_hash,
        normalized_text=result.normalized_text,
    )


@router.post(
    "/verify",
    response_model=VerifyResponse,
    summary="Zweryfikuj zrzut ekranu odpowiedzi Prawdowida",
    description=(
        f"Sprawdza zrzut ekranu (`file`: {', '.join(sorted(IMAGE_TYPES))}, do "
        f"{MAX_MEDIA_BYTES // 1_000_000} MB) z jednym lub wieloma kafelkami odpowiedzi Prawdowida. "
        f"Każdy kafelek z własnym {HASH_HEX_LENGTH}-znakowym (128-bitowym) hashem jest weryfikowany osobno."
    ),
    responses={
        status.HTTP_413_CONTENT_TOO_LARGE: {"model": ErrorResponse, "description": "Plik jest za duży."},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse, "description": "Nieobsługiwany format obrazu."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse, "description": "Brak lub nieprawidłowy plik obrazu."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse, "description": "Brak konfiguracji klucza lub usługi OCR."},
    },
)
async def verify(
    verifier_factory: Annotated[
        Callable[[], AbstractAsyncContextManager[AnswerVerifier]],
        Depends(get_verifier_factory),
    ],
    file: Annotated[
        UploadFile,
        File(description="Zrzut ekranu z odpowiedzią Prawdowida (PNG/JPEG/WebP)."),
    ],
) -> VerifyResponse:
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Dołącz zrzut ekranu odpowiedzi Prawdowida (pole `file`).",
        )

    media_bytes = await _read_media(file, InputType.IMAGE)
    mime_type = (file.content_type or "").lower()

    try:
        async with verifier_factory() as verifier:
            result = await asyncio.to_thread(
                verifier.verify_screenshot,
                Media(data=media_bytes, mime_type=mime_type),
            )
    except VerifierUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("Unexpected failure in POST /verify", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Błąd techniczny podczas weryfikacji zrzutu ekranu. Spróbuj ponownie później.",
        ) from exc

    return _to_verify_response(result)
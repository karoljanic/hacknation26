"""Panel administratora i analityka FIMI: hot topics, obserwowalność, prompt injection i eksport."""

from __future__ import annotations

from datetime import datetime
import hmac
import os
from typing import Annotated, Any
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from prawdowid.persistence.db import get_session
from prawdowid.services import admin_analytics

ADMIN_TOKEN_ENV = "PRAWDOWID_ADMIN_TOKEN"


def _configured_admin_token() -> str | None:
    value = os.environ.get(ADMIN_TOKEN_ENV, "").strip()
    return value or None


def require_admin(
    x_admin_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Chroni endpointy /admin/*. Bez PRAWDOWID_ADMIN_TOKEN panel jest wyłączony:
    pozwala usuwać zgłoszenia, więc nigdy nie może być otwarty."""
    expected = _configured_admin_token()
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Panel administratora jest wyłączony (brak PRAWDOWID_ADMIN_TOKEN).",
        )
    provided = x_admin_token
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wymagana autoryzacja administratora (prawidłowy PRAWDOWID_ADMIN_TOKEN).",
        )


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# -- schematy odpowiedzi ----------------------------------------------------- #


class OverviewResponse(BaseModel):
    auth_enabled: bool
    window_days: int | None
    total_runs: int
    runs_by_status: dict[str, int]
    tech_failure_rate: float
    total_assessments: int
    verdicts: dict[str, int]
    abstention_rate: float
    channels: dict[str, int]
    latency_median_ms: int | None
    latency_p95_ms: int | None
    latency_avg_ms: int | None
    injection_incidents: int
    expired_submissions: int


class HotTopicItem(BaseModel):
    claim_id: str
    text: str
    kind: str
    submission_count: int
    channels: list[str]
    first_seen_at: datetime | None
    last_seen_at: datetime | None
    latest_verdict: str | None
    latest_strength: str | None
    latest_assessed_at: datetime | None
    needs_attention: bool


class HotTopicsPageResponse(BaseModel):
    items: list[HotTopicItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    min_submissions: int


class RecentRunItem(BaseModel):
    run_id: str
    submission_id: str
    channel: str
    input_preview: str
    url: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    latency_ms: int | None
    verdicts: list[str]
    injection_count: int
    error: str | None
    expires_at: datetime | None


class InjectionAlertItem(BaseModel):
    run_id: str
    submission_id: str
    channel: str
    detected_at: datetime | None
    where: str | None
    claim_id: str | None
    excerpt: str
    detected_by: str


# -- endpointy --------------------------------------------------------------- #


@router.get("/overview", response_model=OverviewResponse, summary="Metryki jakości i bezpieczeństwa")
def overview(
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int | None, Query(ge=1, le=365)] = 30,
) -> OverviewResponse:
    metrics = admin_analytics.get_overview_metrics(session, days=days)
    return OverviewResponse(auth_enabled=_configured_admin_token() is not None, **metrics)


@router.get("/hot-topics", response_model=HotTopicsPageResponse, summary="Hot topics (z paginacją i progiem zgłoszeń)")
def hot_topics(
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int | None, Query(ge=1, le=365)] = 7,
    min_submissions: Annotated[int, Query(ge=1, le=1000)] = 2,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 10,
    only_unresolved: bool = False,
) -> HotTopicsPageResponse:
    data = admin_analytics.get_hot_topics(
        session,
        days=days,
        min_submissions=min_submissions,
        page=page,
        page_size=page_size,
        only_unresolved=only_unresolved,
    )
    return HotTopicsPageResponse(
        items=[HotTopicItem(**item) for item in data["items"]],
        total=data["total"],
        page=data["page"],
        page_size=data["page_size"],
        total_pages=data["total_pages"],
        min_submissions=data["min_submissions"],
    )


@router.get("/runs", response_model=list[RecentRunItem], summary="Dziennik przebiegów weryfikacji")
def recent_runs(
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int | None, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[RecentRunItem]:
    items = admin_analytics.list_recent_runs(session, days=days, limit=limit)
    return [RecentRunItem(**item) for item in items]


@router.get("/injections", response_model=list[InjectionAlertItem], summary="Wykryte próby prompt injection")
def injection_alerts(
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int | None, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[InjectionAlertItem]:
    items = admin_analytics.list_injection_alerts(session, days=days, limit=limit)
    return [InjectionAlertItem(**item) for item in items]


@router.delete("/submissions/{submission_id}", summary="Usuń zgłoszenie / sesję demonstracyjną")
def delete_submission(
    submission_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, Any]:
    deleted = admin_analytics.delete_submission_session(session, submission_id=submission_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nie znaleziono zgłoszenia.")
    session.commit()
    return {"deleted": True, "submission_id": str(submission_id)}


@router.post("/retention/purge", summary="Usuń wygasłe zgłoszenia (retencja)")
def purge_retention(session: Annotated[Session, Depends(get_session)]) -> dict[str, int]:
    deleted_count = admin_analytics.purge_expired_submissions(session)
    session.commit()
    return {"deleted_submissions": deleted_count}


@router.get("/export/runs.csv", summary="Eksport przebiegów do CSV")
def export_runs_csv(
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int | None, Query(ge=1, le=365)] = 30,
) -> Response:
    csv_body = admin_analytics.export_runs_csv(session, days=days)
    return Response(
        content=csv_body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="prawdowid-przebiegi.csv"'},
    )


@router.get("/export/analytics.json", summary="Eksport analityczny JSON (OpenCTI / DISARM)")
def export_analytics_json(
    session: Annotated[Session, Depends(get_session)],
    days: Annotated[int | None, Query(ge=1, le=365)] = 30,
) -> JSONResponse:
    payload = admin_analytics.export_analytical_feed(session, days=days)
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": 'attachment; filename="prawdowid-analiza-fimi.json"'},
    )
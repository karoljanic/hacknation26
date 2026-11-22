"""Zapytania analityczne, retencja, prompt injection i eksport dla panelu admina."""

from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
import io
from typing import Any
import uuid

from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from prawdowid.domain.enums import CaseState, RunStatus, Verdict
from prawdowid.persistence.models import (
    Assessment,
    Case,
    CaseSubmission,
    Claim,
    Submission,
    SubmissionClaim,
    VerificationRun,
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cutoff(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _safe_csv_cell(value: Any) -> str:
    """Chroni eksport CSV przed CSV Formula Injection w arkuszach kalkulacyjnych."""
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def _injection_count_expr():
    """Bezpiecznie liczy długość tablicy JSONB `injection_flags` w PostgreSQL."""
    return case(
        (
            func.jsonb_typeof(VerificationRun.injection_flags) == "array",
            func.jsonb_array_length(VerificationRun.injection_flags),
        ),
        else_=0,
    )


def get_overview_metrics(session: Session, *, days: int | None = 30) -> dict[str, Any]:
    """Zbiorcze KPI jakości, wydajności i bezpieczeństwa."""
    since = _cutoff(days)

    run_filter = [VerificationRun.started_at >= since] if since else []
    sub_filter = [Submission.received_at >= since] if since else []

    status_rows = session.execute(
        select(VerificationRun.status, func.count(VerificationRun.id))
        .where(*run_filter)
        .group_by(VerificationRun.status)
    ).all()
    by_status = {status.value: count for status, count in status_rows}
    total_runs = sum(by_status.values())

    latency_row = session.execute(
        select(
            func.percentile_cont(0.5).within_group(VerificationRun.latency_ms),
            func.percentile_cont(0.95).within_group(VerificationRun.latency_ms),
            func.avg(VerificationRun.latency_ms),
        ).where(*run_filter, VerificationRun.latency_ms.is_not(None))
    ).one()

    injection_incidents = session.scalar(
        select(func.coalesce(func.sum(_injection_count_expr()), 0)).where(*run_filter)
    ) or 0

    verdict_query = select(Assessment.verdict, func.count(Assessment.id)).join(
        VerificationRun, VerificationRun.id == Assessment.run_id
    )
    if since:
        verdict_query = verdict_query.where(VerificationRun.started_at >= since)
    verdict_rows = session.execute(verdict_query.group_by(Assessment.verdict)).all()
    by_verdict = {verdict.value: count for verdict, count in verdict_rows}
    total_assessments = sum(by_verdict.values())
    abstention_count = by_verdict.get(Verdict.INSUFFICIENT_EVIDENCE.value, 0)
    abstention_rate = round(abstention_count / total_assessments, 4) if total_assessments else 0.0

    channel_rows = session.execute(
        select(Submission.channel, func.count(Submission.id))
        .where(*sub_filter)
        .group_by(Submission.channel)
    ).all()
    by_channel = {channel.value: count for channel, count in channel_rows}

    expired_submissions = session.scalar(
        select(func.count(Submission.id)).where(Submission.expires_at <= datetime.now(timezone.utc))
    ) or 0

    return {
        "window_days": days,
        "total_runs": total_runs,
        "runs_by_status": by_status,
        "tech_failure_rate": round(by_status.get(RunStatus.TECH_FAILURE.value, 0) / total_runs, 4)
        if total_runs
        else 0.0,
        "total_assessments": total_assessments,
        "verdicts": by_verdict,
        "abstention_rate": abstention_rate,
        "channels": by_channel,
        "latency_median_ms": round(float(latency_row[0])) if latency_row[0] is not None else None,
        "latency_p95_ms": round(float(latency_row[1])) if latency_row[1] is not None else None,
        "latency_avg_ms": round(float(latency_row[2])) if latency_row[2] is not None else None,
        "injection_incidents": int(injection_incidents),
        "expired_submissions": int(expired_submissions),
    }


def get_hot_topics(
    session: Session,
    *,
    days: int | None = 7,
    min_submissions: int = 2,
    page: int = 1,
    page_size: int = 10,
    only_unresolved: bool = False,
) -> dict[str, Any]:
    """Paginowana lista hot topics spełniających minimalny próg liczby zgłoszeń."""
    since = _cutoff(days)
    min_submissions = max(1, min_submissions)
    page_size = max(1, min(page_size, 100))

    latest_verdict_sq = (
        select(
            Assessment.claim_id.label("claim_id"),
            Assessment.verdict.label("verdict"),
        )
        .distinct(Assessment.claim_id)
        .order_by(Assessment.claim_id, Assessment.as_of.desc())
        .subquery()
    )

    count_expr = func.count(distinct(SubmissionClaim.submission_id))
    last_seen_expr = func.max(Submission.received_at)

    base_query = (
        select(
            Claim.id.label("id"),
            Claim.canonical_text.label("canonical_text"),
            Claim.kind.label("kind"),
            count_expr.label("submission_count"),
            func.min(Submission.received_at).label("first_seen_at"),
            last_seen_expr.label("last_seen_at"),
        )
        .join(SubmissionClaim, SubmissionClaim.claim_id == Claim.id)
        .join(Submission, Submission.id == SubmissionClaim.submission_id)
    )
    if since:
        base_query = base_query.where(Submission.received_at >= since)
    if only_unresolved:
        base_query = base_query.outerjoin(
            latest_verdict_sq, latest_verdict_sq.c.claim_id == Claim.id
        ).where(
            (latest_verdict_sq.c.verdict == Verdict.INSUFFICIENT_EVIDENCE)
            | (latest_verdict_sq.c.verdict.is_(None))
        )

    grouped_query = base_query.group_by(Claim.id, Claim.canonical_text, Claim.kind).having(
        count_expr >= min_submissions
    )

    total = int(session.scalar(select(func.count()).select_from(grouped_query.subquery())) or 0)
    total_pages = max(1, (total + page_size - 1) // page_size) if total > 0 else 1
    safe_page = min(max(1, page), total_pages)
    offset = (safe_page - 1) * page_size

    rows = session.execute(
        grouped_query.order_by(count_expr.desc(), last_seen_expr.desc())
        .offset(offset)
        .limit(page_size)
    ).all()

    if not rows:
        return {
            "items": [],
            "total": total,
            "page": safe_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "min_submissions": min_submissions,
        }

    claim_ids = [row.id for row in rows]

    channel_query = (
        select(SubmissionClaim.claim_id, Submission.channel)
        .join(Submission, Submission.id == SubmissionClaim.submission_id)
        .where(SubmissionClaim.claim_id.in_(claim_ids))
        .distinct()
    )
    if since:
        channel_query = channel_query.where(Submission.received_at >= since)
    channels_by_claim: dict[uuid.UUID, list[str]] = defaultdict(list)
    for claim_id, channel in session.execute(channel_query).all():
        channels_by_claim[claim_id].append(channel.value)

    latest_assessments: dict[uuid.UUID, Assessment] = {}
    assessments = session.scalars(
        select(Assessment)
        .where(Assessment.claim_id.in_(claim_ids))
        .order_by(Assessment.claim_id, Assessment.as_of.desc())
    ).all()
    for assessment in assessments:
        latest_assessments.setdefault(assessment.claim_id, assessment)

    items: list[dict[str, Any]] = []
    for row in rows:
        assessment = latest_assessments.get(row.id)
        verdict = assessment.verdict.value if assessment else None
        is_unresolved = verdict is None or verdict == Verdict.INSUFFICIENT_EVIDENCE.value
        items.append(
            {
                "claim_id": str(row.id),
                "text": row.canonical_text,
                "kind": row.kind.value,
                "submission_count": int(row.submission_count),
                "channels": sorted(channels_by_claim.get(row.id, [])),
                "first_seen_at": row.first_seen_at,
                "last_seen_at": row.last_seen_at,
                "latest_verdict": verdict,
                "latest_strength": assessment.strength.value if assessment else None,
                "latest_assessed_at": assessment.as_of if assessment else None,
                "needs_attention": is_unresolved and int(row.submission_count) >= 2,
            }
        )

    return {
        "items": items,
        "total": total,
        "page": safe_page,
        "page_size": page_size,
        "total_pages": total_pages,
        "min_submissions": min_submissions,
    }


def list_recent_runs(session: Session, *, days: int | None = 30, limit: int = 50) -> list[dict[str, Any]]:
    """Audyt ostatnich przebiegów weryfikacji (pytanie/URL, czas, status, werdykty)."""
    since = _cutoff(days)
    query = select(VerificationRun, Submission).join(
        Submission, Submission.id == VerificationRun.submission_id
    )
    if since:
        query = query.where(VerificationRun.started_at >= since)
    rows = session.execute(query.order_by(VerificationRun.started_at.desc()).limit(limit)).all()
    if not rows:
        return []

    run_ids = [run.id for run, _ in rows]
    verdicts_by_run: dict[uuid.UUID, list[str]] = defaultdict(list)
    for run_id, verdict in session.execute(
        select(Assessment.run_id, Assessment.verdict).where(Assessment.run_id.in_(run_ids))
    ).all():
        if run_id is not None:
            verdicts_by_run[run_id].append(verdict.value)

    items: list[dict[str, Any]] = []
    for run, submission in rows:
        preview = submission.redacted_text or submission.url or "(brak treści)"
        flags = run.injection_flags if isinstance(run.injection_flags, list) else []
        items.append(
            {
                "run_id": str(run.id),
                "submission_id": str(submission.id),
                "channel": submission.channel.value,
                "input_preview": preview[:220],
                "url": submission.url,
                "status": run.status.value,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "latency_ms": run.latency_ms,
                "verdicts": verdicts_by_run.get(run.id, []),
                "injection_count": len(flags),
                "error": run.error,
                "expires_at": submission.expires_at,
            }
        )
    return items


def list_injection_alerts(session: Session, *, days: int | None = 30, limit: int = 50) -> list[dict[str, Any]]:
    """Wykryte próby prompt injection zapisane w ``verification_run.injection_flags``."""
    since = _cutoff(days)
    query = (
        select(VerificationRun, Submission)
        .join(Submission, Submission.id == VerificationRun.submission_id)
        .where(_injection_count_expr() > 0)
    )
    if since:
        query = query.where(VerificationRun.started_at >= since)
    rows = session.execute(query.order_by(VerificationRun.started_at.desc()).limit(limit)).all()

    alerts: list[dict[str, Any]] = []
    for run, submission in rows:
        flags = run.injection_flags if isinstance(run.injection_flags, list) else []
        for flag in flags:
            if isinstance(flag, dict):
                where = flag.get("where") or submission.url or f"input:{submission.channel.value}"
                claim_id = flag.get("claim_id")
                excerpt = str(flag.get("excerpt") or flag.get("pattern") or "").strip()
                detected_by = str(flag.get("detected_by") or "heuristic")
            else:
                where = submission.url or f"input:{submission.channel.value}"
                claim_id = None
                excerpt = str(flag).strip()
                detected_by = "heuristic"
            alerts.append(
                {
                    "run_id": str(run.id),
                    "submission_id": str(submission.id),
                    "channel": submission.channel.value,
                    "detected_at": run.finished_at or run.started_at,
                    "where": where,
                    "claim_id": claim_id,
                    "excerpt": excerpt or "(wykryto wzorzec manipulacji)",
                    "detected_by": detected_by,
                }
            )
            if len(alerts) >= limit:
                return alerts
    return alerts


def delete_submission_session(session: Session, *, submission_id: uuid.UUID) -> bool:
    """Usuwa pojedynczą sesję/zgłoszenie (prawo usunięcia sesji demonstracyjnej)."""
    submission = session.get(Submission, submission_id)
    if submission is None:
        return False

    affected_case_ids = list(
        session.scalars(
            select(CaseSubmission.case_id).where(CaseSubmission.submission_id == submission_id)
        ).all()
    )
    session.delete(submission)
    session.flush()

    for case_id in affected_case_ids:
        case = session.get(Case, case_id)
        if case is None:
            continue
        remaining = session.scalar(
            select(func.count(CaseSubmission.submission_id)).where(CaseSubmission.case_id == case_id)
        ) or 0
        if remaining == 0 and case.state == CaseState.RECOMMENDED:
            session.delete(case)
        else:
            case.submission_count = int(remaining)
    session.flush()
    return True


def purge_expired_submissions(session: Session) -> int:
    """Usuwa wygasłe zgłoszenia zgodnie z polityką retencji (``submission.expires_at``)."""
    now = datetime.now(timezone.utc)
    expired_ids = list(
        session.scalars(select(Submission.id).where(Submission.expires_at <= now)).all()
    )
    if not expired_ids:
        return 0
    for submission_id in expired_ids:
        delete_submission_session(session, submission_id=submission_id)
    return len(expired_ids)


def export_runs_csv(session: Session, *, days: int | None = 30) -> str:
    """Eksport przebiegów sprawdzeń do CSV (UTF-8 z BOM dla Excela)."""
    rows = list_recent_runs(session, days=days, limit=5000)
    buffer = io.StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "run_id",
            "submission_id",
            "channel",
            "status",
            "started_at",
            "latency_ms",
            "verdicts",
            "injection_count",
            "input_preview",
            "url",
            "error",
        ]
    )
    for item in rows:
        writer.writerow(
            [
                _safe_csv_cell(item["run_id"]),
                _safe_csv_cell(item["submission_id"]),
                _safe_csv_cell(item["channel"]),
                _safe_csv_cell(item["status"]),
                _safe_csv_cell(item["started_at"].isoformat() if item["started_at"] else ""),
                _safe_csv_cell(item["latency_ms"]),
                _safe_csv_cell("; ".join(item["verdicts"])),
                _safe_csv_cell(item["injection_count"]),
                _safe_csv_cell(item["input_preview"]),
                _safe_csv_cell(item["url"]),
                _safe_csv_cell(item["error"]),
            ]
        )
    return buffer.getvalue()


def export_analytical_feed(session: Session, *, days: int | None = 30) -> dict[str, Any]:
    """Eksport analityczny JSON zgodny koncepcyjnie z OpenCTI / DISARM."""
    topics_page = get_hot_topics(session, days=days, min_submissions=2, page=1, page_size=100)
    injections = list_injection_alerts(session, days=days, limit=200)
    return {
        "schema": "prawdowid.fimi.analytics/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "compatible_frameworks": ["DISARM", "OpenCTI-STIX-2.1-mapping"],
        "hot_topics": [
            {
                **topic,
                "first_seen_at": topic["first_seen_at"].isoformat() if topic["first_seen_at"] else None,
                "last_seen_at": topic["last_seen_at"].isoformat() if topic["last_seen_at"] else None,
                "latest_assessed_at": topic["latest_assessed_at"].isoformat()
                if topic["latest_assessed_at"]
                else None,
                "disarm_hint": (
                    "T0007/Information-Gap"
                    if topic["latest_verdict"] == Verdict.INSUFFICIENT_EVIDENCE.value
                    else "T0023/Distort-Facts"
                    if topic["latest_verdict"] in (Verdict.FALSE.value, Verdict.MISLEADING.value)
                    else None
                ),
            }
            for topic in topics_page["items"]
        ],
        "prompt_injection_incidents": [
            {
                **alert,
                "detected_at": alert["detected_at"].isoformat() if alert["detected_at"] else None,
            }
            for alert in injections
        ],
    }
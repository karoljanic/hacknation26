"""Zamknięta pętla informacji: automatyczne tworzenie i deduplikacja spraw moderatora.

Zgodnie ze specyfikacją:
- powtarzające się lub nierozstrzygnięte twierdzenia (``insufficient_evidence``) oraz
  przypadki z próbą prompt injection tworzą lub aktualizują JEDNĄ otwartą sprawę
  dla danego ``claim_id`` (częściowy indeks unikalny ``uq_moderation_case_open_claim``),
- automatyczny zapis ZAWSZE ustawia stan ``CaseState.RECOMMENDED`` (rekomendacja),
  a faktyczne przekazanie (``CaseState.FORWARDED`` + ``forwarded_to`` + ``forwarded_at``)
  następuje dopiero po jawnej akcji administratora/moderatora.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from prawdowid.domain.enums import CaseState, Verdict
from prawdowid.persistence.models import Case, CaseSubmission
from prawdowid.services.pipeline import ClaimOutcome


def _find_open_case(session: Session, claim_id: uuid.UUID) -> Case | None:
    return session.scalars(
        select(Case).where(Case.claim_id == claim_id, Case.state != CaseState.RESOLVED)
    ).first()


def record_moderation_case(
    session: Session,
    *,
    submission_id: uuid.UUID,
    claim_id: uuid.UUID,
    outcome: ClaimOutcome,
    has_injection_flag: bool = False,
) -> Case | None:
    """Tworzy lub aktualizuje otwartą sprawę moderatora dla nierozstrzygniętej / ryzykownej tezy."""
    verdict = Verdict(outcome.assessment.verdict)
    should_track = verdict == Verdict.INSUFFICIENT_EVIDENCE or has_injection_flag
    if not should_track:
        return None

    now = datetime.now(timezone.utc)
    initial_reason = "high_risk" if has_injection_flag else "unresolved"

    case = _find_open_case(session, claim_id)
    if case is None:
        case = Case(
            claim_id=claim_id,
            state=CaseState.RECOMMENDED,
            reason=initial_reason,
            submission_count=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        try:
            with session.begin_nested():
                session.add(case)
                session.flush()
                session.add(CaseSubmission(case_id=case.id, submission_id=submission_id))
                session.flush()
            return case
        except IntegrityError:
            # Równoległe zgłoszenie mogło w tym samym momencie otworzyć sprawę dla tego claim_id.
            case = _find_open_case(session, claim_id)
            if case is None:
                raise

    already_linked = session.get(CaseSubmission, (case.id, submission_id))
    if already_linked is None:
        try:
            with session.begin_nested():
                session.add(CaseSubmission(case_id=case.id, submission_id=submission_id))
                session.flush()
            case.submission_count += 1
        except IntegrityError:
            pass

    case.last_seen_at = now
    if has_injection_flag:
        case.reason = "high_risk"
    elif case.submission_count >= 2 and case.reason == "unresolved":
        case.reason = "repeated_no_evidence"
    session.flush()
    return case
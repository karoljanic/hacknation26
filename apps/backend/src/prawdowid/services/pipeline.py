"""End-to-end orchestration behind ``POST /check``: intake -> evidence search
-> sublink selection -> one batched assessment call.

Kept separate from the API layer (FastAPI) so it is independently testable
and reusable by other channels. Blocking calls (DB, HTTP, LLM) run in worker
threads via ``asyncio.to_thread`` so one request does not stall the event loop
for everyone else.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from prawdowid.domain.enums import RunStatus
from prawdowid.domain.evidence import (
    ClaimAssessmentOutput,
    ClaimEvidenceBundle,
    ClaimForVerification,
    EvidenceForAssessment,
)
from prawdowid.domain.preparation import PreparedInput
from prawdowid.persistence.models import VerificationRun
from prawdowid.services.assessment import AssessmentGenerator, no_evidence_assessment, opinion_assessment
from prawdowid.services.evidence_search import EvidenceSearchOrchestrator
from prawdowid.services.intake import ClaimIntake, IntakeResult, StoredClaim
from prawdowid.services.sublinks import SublinkSelector

ASSESSMENT_FAILED_MESSAGE = "Błąd techniczny podczas oceny zgłoszenia. Spróbuj ponownie później."
EVIDENCE_SEARCH_FAILED_MESSAGE = "Błąd techniczny podczas wyszukiwania dowodów. Spróbuj ponownie później."
PERSISTENCE_FAILED_MESSAGE = "Błąd techniczny podczas zapisu wyniku. Spróbuj ponownie później."

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimOutcome:
    claim_id: str
    text: str
    kind: str
    assessment: ClaimAssessmentOutput
    judged_by: str
    evidence: list[EvidenceForAssessment] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineResult:
    submission_id: uuid.UUID
    run_id: uuid.UUID
    status: RunStatus
    message: str | None = None
    claims: list[ClaimOutcome] = field(default_factory=list)


class VerificationPipeline:
    def __init__(
        self,
        *,
        intake: ClaimIntake,
        evidence_search: EvidenceSearchOrchestrator,
        sublinks: SublinkSelector,
        assessment: AssessmentGenerator,
    ) -> None:
        self.intake = intake
        self.evidence_search = evidence_search
        self.sublinks = sublinks
        self.assessment = assessment

    async def run_text(
        self, text: str, *, channel, question: bool = False, sender_hash: str | None = None
    ) -> PipelineResult:
        intake_result = await asyncio.to_thread(
            self.intake.submit_text, text, channel=channel, question=question, sender_hash=sender_hash
        )
        return await self._continue(intake_result)

    async def run_url(self, url: str, *, channel, sender_hash: str | None = None) -> PipelineResult:
        intake_result = await asyncio.to_thread(
            self.intake.submit_url, url, channel=channel, sender_hash=sender_hash
        )
        return await self._continue(intake_result)

    async def run_prepared(
        self, prepared: PreparedInput, *, channel, sender_hash: str | None = None
    ) -> PipelineResult:
        intake_result = await asyncio.to_thread(
            self.intake.submit_prepared, prepared, channel=channel, sender_hash=sender_hash
        )
        return await self._continue(intake_result)

    async def _continue(self, intake_result: IntakeResult) -> PipelineResult:
        if intake_result.status != RunStatus.OK:
            return PipelineResult(
                intake_result.submission_id, intake_result.run_id, intake_result.status, intake_result.message
            )

        by_id = {str(stored.claim_id): stored for stored in intake_result.claims}
        claims = [
            ClaimForVerification(id=claim_id, text=stored.text, kind=stored.kind.value)
            for claim_id, stored in by_id.items()
        ]
        claim_lookup = {claim.id: claim for claim in claims}

        try:
            search_result = await self.evidence_search.search(claims)
        except Exception:
            log.warning("Evidence search failed for run %s", intake_result.run_id, exc_info=True)
            self._fail_run(intake_result.run_id, EVIDENCE_SEARCH_FAILED_MESSAGE)
            return PipelineResult(
                intake_result.submission_id, intake_result.run_id, RunStatus.TECH_FAILURE,
                EVIDENCE_SEARCH_FAILED_MESSAGE,
            )

        outcomes: dict[str, ClaimOutcome] = {}
        for opinion_id in search_result.skipped_opinion_ids:
            stored = by_id[opinion_id]
            outcomes[opinion_id] = ClaimOutcome(
                opinion_id, stored.text, stored.kind.value, opinion_assessment(claim_lookup[opinion_id]),
                judged_by="deterministic:opinion",
            )

        with_evidence = [claim_evidence for claim_evidence in search_result.claims if claim_evidence.items]
        for claim_evidence in search_result.claims:
            if not claim_evidence.items:
                stored = by_id[claim_evidence.claim_id]
                outcomes[claim_evidence.claim_id] = ClaimOutcome(
                    claim_evidence.claim_id, stored.text, stored.kind.value,
                    no_evidence_assessment(
                        claim_lookup[claim_evidence.claim_id],
                        unavailable_sources=claim_evidence.unavailable_sources,
                    ),
                    judged_by="deterministic:no_evidence",
                )

        selected = await asyncio.gather(
            *(
                asyncio.to_thread(self.sublinks.select, claim_lookup[claim_evidence.claim_id].text, claim_evidence.items)
                for claim_evidence in with_evidence
            )
        )
        bundle_evidence = {
            claim_evidence.claim_id: evidence for claim_evidence, evidence in zip(with_evidence, selected, strict=True)
        }
        bundles = [
            ClaimEvidenceBundle(
                claim_id=claim_evidence.claim_id,
                claim_text=claim_lookup[claim_evidence.claim_id].text,
                claim_kind=claim_lookup[claim_evidence.claim_id].kind,  # type: ignore[arg-type]
                evidence=bundle_evidence[claim_evidence.claim_id],
            )
            for claim_evidence in with_evidence
        ]

        if bundles:
            try:
                assessed = await asyncio.to_thread(self.assessment.assess, bundles)
            except Exception:
                log.warning("Assessment failed for run %s", intake_result.run_id, exc_info=True)
                self._fail_run(intake_result.run_id, ASSESSMENT_FAILED_MESSAGE)
                return PipelineResult(
                    intake_result.submission_id, intake_result.run_id, RunStatus.TECH_FAILURE,
                    ASSESSMENT_FAILED_MESSAGE,
                )
            judged_by = f"model:{assessed.requested_model}"
            for claim_assessment in assessed.data.assessments:
                stored = by_id[claim_assessment.claim_id]
                outcomes[claim_assessment.claim_id] = ClaimOutcome(
                    claim_assessment.claim_id, stored.text, stored.kind.value, claim_assessment,
                    judged_by, bundle_evidence[claim_assessment.claim_id],
                )

        ordered = [outcomes[claim_id] for claim_id in by_id if claim_id in outcomes]
        try:
            await asyncio.to_thread(self._persist_outcomes, intake_result.run_id, by_id, ordered)
        except Exception:
            log.warning("Persisting assessments failed for run %s", intake_result.run_id, exc_info=True)
            self._fail_run(intake_result.run_id, PERSISTENCE_FAILED_MESSAGE)
            return PipelineResult(
                intake_result.submission_id, intake_result.run_id, RunStatus.TECH_FAILURE,
                PERSISTENCE_FAILED_MESSAGE,
            )
        return PipelineResult(intake_result.submission_id, intake_result.run_id, RunStatus.OK, claims=ordered)

    def _persist_outcomes(
        self, run_id: uuid.UUID, by_id: dict[str, StoredClaim], ordered: list[ClaimOutcome]
    ) -> None:
        from prawdowid.persistence.evidence import record_outcome

        with self.intake.session_factory() as session, session.begin():
            for outcome in ordered:
                record_outcome(
                    session, run_id=run_id, claim_id=by_id[outcome.claim_id].claim_id, outcome=outcome
                )

    def _fail_run(self, run_id: uuid.UUID, error: str) -> None:
        with self.intake.session_factory() as session, session.begin():
            run = session.get_one(VerificationRun, run_id)
            run.status = RunStatus.TECH_FAILURE
            run.error = error

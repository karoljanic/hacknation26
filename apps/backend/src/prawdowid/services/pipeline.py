"""End-to-end orchestration behind ``POST /check``: intake -> key-claim
selection -> evidence search -> sublink selection -> one batched assessment call.

Kept separate from the API layer (FastAPI) so it is independently testable
and reusable by other channels. Blocking calls (DB, HTTP, LLM) run in worker
threads via ``asyncio.to_thread`` so one request does not stall the event loop
for everyone else.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from prawdowid.ai.adapters.llm import ContentBlocked
from prawdowid.domain.enums import RunStatus
from prawdowid.domain.evidence import (
    ClaimAssessmentOutput,
    ClaimEvidenceBundle,
    ClaimForVerification,
    EvidenceForAssessment,
    EvidenceSearchResult,
    InjectionNote,
)
from prawdowid.domain.preparation import PreparedInput
from prawdowid.persistence.models import VerificationRun
from prawdowid.security.injection import dedupe_injection_flags, detect_prompt_injections
from prawdowid.services.assessment import (
    OPEN_QUESTION_MESSAGE,
    AssessmentGenerator,
    no_evidence_assessment,
    open_question_assessment,
    opinion_assessment,
    with_unavailable_sources,
)
from prawdowid.services.claim_selection import ClaimSelection, ClaimSelector
from prawdowid.services.evidence_search import EvidenceSearchOrchestrator
from prawdowid.services.intake import CONTENT_BLOCKED_MESSAGE, ClaimIntake, IntakeResult, StoredClaim
from prawdowid.services.relevance import claim_terms
from prawdowid.services.sublinks import SublinkSelector
from prawdowid.services.submitted_page import SubmittedPageEvidence
from prawdowid.services.usage import UsageMeter, estimate_cost, model_prices, usage_entry

ASSESSMENT_FAILED_MESSAGE = "Błąd techniczny podczas oceny zgłoszenia. Spróbuj ponownie później."
EVIDENCE_SEARCH_FAILED_MESSAGE = "Błąd techniczny podczas wyszukiwania dowodów. Spróbuj ponownie później."
PERSISTENCE_FAILED_MESSAGE = "Błąd techniczny podczas zapisu wyniku. Spróbuj ponownie później."
UNEXPECTED_FAILURE_MESSAGE = "Błąd techniczny podczas sprawdzania. Spróbuj ponownie później."
CANCELLED_MESSAGE = "Sprawdzanie zostało anulowane przed zakończeniem."

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
    # Side claims not verified because the submission had more than the
    # selector's limit; listed to the user, never silently dropped.
    skipped_claims: list[str] = field(default_factory=list)


class VerificationPipeline:
    def __init__(
        self,
        *,
        intake: ClaimIntake,
        evidence_search: EvidenceSearchOrchestrator,
        sublinks: SublinkSelector,
        assessment: AssessmentGenerator,
        usage_meter: UsageMeter | None = None,
        claim_selector: ClaimSelector | None = None,
        submitted_pages: SubmittedPageEvidence | None = None,
    ) -> None:
        self.usage_meter = usage_meter
        self.intake = intake
        self.evidence_search = evidence_search
        self.sublinks = sublinks
        self.assessment = assessment
        self.claim_selector = claim_selector
        self.submitted_pages = submitted_pages

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

    async def run_deferred(
        self,
        prepare: Callable[[], PreparedInput],
        *,
        channel,
        url: str | None = None,
        sender_hash: str | None = None,
    ) -> PipelineResult:
        """``prepare`` is blocking (HTTP fetch, media reading by a model): it
        runs in a worker thread, inside the run, never on the event loop."""
        intake_result = await asyncio.to_thread(
            self.intake.submit_deferred, prepare, channel=channel, url=url, sender_hash=sender_hash
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
            # Intake already closed the run with its own status.
            return PipelineResult(
                intake_result.submission_id, intake_result.run_id, intake_result.status, intake_result.message
            )
        telemetry = _RunTelemetry()
        try:
            result = await self._verify(intake_result, telemetry)
        except Exception:  # an unforeseen bug must still close the run
            log.warning("Verification failed for run %s", intake_result.run_id, exc_info=True)
            result = PipelineResult(
                intake_result.submission_id, intake_result.run_id, RunStatus.TECH_FAILURE, UNEXPECTED_FAILURE_MESSAGE
            )
        except BaseException:
            # Cancellation (client gone, or a library cancelling its scope) is
            # not an Exception, yet the run must not stay "running" forever.
            # The write is synchronous on purpose: inside a cancelled anyio
            # scope any further ``await`` would be cancelled again.
            log.warning("Verification cancelled for run %s", intake_result.run_id, exc_info=True)
            cancelled = PipelineResult(
                intake_result.submission_id, intake_result.run_id, RunStatus.TECH_FAILURE, CANCELLED_MESSAGE
            )
            try:
                self._finish_run(intake_result.run_id, cancelled, telemetry)
            except Exception:
                log.warning("Closing cancelled run %s failed", intake_result.run_id, exc_info=True)
            raise
        try:
            await asyncio.to_thread(self._finish_run, intake_result.run_id, result, telemetry)
        except Exception:
            log.warning("Closing run %s failed", intake_result.run_id, exc_info=True)
        return result

    async def _verify(self, intake_result: IntakeResult, telemetry: "_RunTelemetry") -> PipelineResult:
        by_id = {str(stored.claim_id): stored for stored in intake_result.claims}
        claims = [
            ClaimForVerification(
                id=claim_id, text=stored.text, kind=stored.kind.value,
                search_queries=list(stored.search_queries), countries=list(stored.countries),
                general_knowledge=stored.general_knowledge, ambiguities=list(stored.ambiguities),
                open_question=stored.open_question,
                source_quotes=list(stored.source_quotes), text_en=stored.text_en,
            )
            for claim_id, stored in by_id.items()
        ]
        selection = await asyncio.to_thread(self._select_claims, intake_result.input_text, claims)
        skipped_texts = [by_id[claim_id].text for claim_id in selection.skipped]
        by_id = {claim_id: by_id[claim_id] for claim_id in selection.selected}
        claims = [claim for claim in claims if claim.id in by_id]
        claim_lookup = {claim.id: claim for claim in claims}

        open_questions = [claim for claim in claims if claim.open_question]
        if open_questions and len(open_questions) == len(claims):
            return PipelineResult(
                intake_result.submission_id, intake_result.run_id, RunStatus.NEEDS_CLARIFICATION,
                OPEN_QUESTION_MESSAGE, skipped_claims=skipped_texts,
            )

        try:
            search_result = await self.evidence_search.search(
                [claim for claim in claims if not claim.open_question]
            )
        except Exception:
            log.warning("Evidence search failed for run %s", intake_result.run_id, exc_info=True)
            return PipelineResult(
                intake_result.submission_id, intake_result.run_id, RunStatus.TECH_FAILURE,
                EVIDENCE_SEARCH_FAILED_MESSAGE,
            )

        if intake_result.source_url and intake_result.source_text and self.submitted_pages is not None:
            search_result = await asyncio.to_thread(self._with_submitted_page, intake_result, claim_lookup, search_result)

        outcomes: dict[str, ClaimOutcome] = {
            claim.id: ClaimOutcome(
                claim.id, by_id[claim.id].text, by_id[claim.id].kind.value, open_question_assessment(claim),
                judged_by="deterministic:open_question",
            )
            for claim in open_questions
        }
        for opinion_id in search_result.skipped_opinion_ids:
            stored = by_id[opinion_id]
            outcomes[opinion_id] = ClaimOutcome(
                opinion_id, stored.text, stored.kind.value, opinion_assessment(claim_lookup[opinion_id]),
                judged_by="deterministic:opinion",
            )

        # General-knowledge claims go to the model even without evidence: it may
        # answer them from general knowledge, clearly labelled as unsourced.
        with_evidence = [
            claim_evidence
            for claim_evidence in search_result.claims
            if claim_evidence.items or claim_lookup[claim_evidence.claim_id].general_knowledge
        ]
        for claim_evidence in search_result.claims:
            if claim_evidence not in with_evidence:
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
        for claim_id, evidence_list in bundle_evidence.items():
            for evidence_item in evidence_list:
                telemetry.injection_flags.extend(
                    detect_prompt_injections(
                        evidence_item.text,
                        where=str(evidence_item.item.url),
                        claim_id=claim_id,
                        detected_by="evidence_heuristic",
                    )
                )
        telemetry.injection_flags = dedupe_injection_flags(telemetry.injection_flags)

        bundles = [
            ClaimEvidenceBundle(
                claim_id=claim_evidence.claim_id,
                claim_text=claim_lookup[claim_evidence.claim_id].text,
                claim_kind=claim_lookup[claim_evidence.claim_id].kind,  # type: ignore[arg-type]
                evidence=bundle_evidence[claim_evidence.claim_id],
                general_knowledge_allowed=claim_lookup[claim_evidence.claim_id].general_knowledge,
                ambiguities=claim_lookup[claim_evidence.claim_id].ambiguities,
            )
            for claim_evidence in with_evidence
        ]

        if bundles:
            try:
                assessed = await asyncio.to_thread(self.assessment.assess, bundles)
            except ContentBlocked:
                log.warning("Assessment refused by the model for run %s", intake_result.run_id, exc_info=True)
                return PipelineResult(
                    intake_result.submission_id, intake_result.run_id, RunStatus.OUT_OF_SCOPE,
                    CONTENT_BLOCKED_MESSAGE,
                )
            except Exception:
                log.warning("Assessment failed for run %s", intake_result.run_id, exc_info=True)
                return PipelineResult(
                    intake_result.submission_id, intake_result.run_id, RunStatus.TECH_FAILURE,
                    ASSESSMENT_FAILED_MESSAGE,
                )
            telemetry.models.append(
                usage_entry("assessment", assessed.requested_model, assessed.model_version,
                            assessed.input_tokens, assessed.output_tokens)
            )
            telemetry.injection_flags = dedupe_injection_flags(
                [*telemetry.injection_flags, *_injection_flags(assessed.data.injection_notes, bundle_evidence)]
            )
            judged_by = f"model:{assessed.requested_model}"
            unavailable = {ce.claim_id: ce.unavailable_sources for ce in with_evidence}
            for claim_assessment in assessed.data.assessments:
                stored = by_id[claim_assessment.claim_id]
                outcomes[claim_assessment.claim_id] = ClaimOutcome(
                    claim_assessment.claim_id, stored.text, stored.kind.value,
                    with_unavailable_sources(claim_assessment, unavailable[claim_assessment.claim_id]),
                    judged_by, bundle_evidence[claim_assessment.claim_id],
                )

        ordered = [outcomes[claim_id] for claim_id in by_id if claim_id in outcomes]
        try:
            await asyncio.to_thread(self._persist_outcomes, intake_result.run_id, by_id, ordered)
        except Exception:
            log.warning("Persisting assessments failed for run %s", intake_result.run_id, exc_info=True)
            return PipelineResult(
                intake_result.submission_id, intake_result.run_id, RunStatus.TECH_FAILURE,
                PERSISTENCE_FAILED_MESSAGE,
            )
        return PipelineResult(
            intake_result.submission_id, intake_result.run_id, RunStatus.OK, claims=ordered,
            skipped_claims=skipped_texts,
        )

    def _with_submitted_page(
        self, intake_result: IntakeResult, claims: dict[str, ClaimForVerification], search_result: EvidenceSearchResult
    ) -> EvidenceSearchResult:
        """Puts the submitted trusted page's relevant passages first in each claim's evidence."""
        assert self.submitted_pages is not None and intake_result.source_url is not None
        wanted = {
            evidence.claim_id: claim_terms([claims[evidence.claim_id].text, *claims[evidence.claim_id].search_queries])
            for evidence in search_result.claims
        }
        try:
            extra = self.submitted_pages.items_for(intake_result.source_url, intake_result.source_text, wanted)
        except Exception:
            log.warning("Submitted page evidence failed for run %s", intake_result.run_id, exc_info=True)
            return search_result
        return search_result.model_copy(
            update={
                "claims": [
                    evidence.model_copy(update={"items": [*extra.get(evidence.claim_id, []), *evidence.items]})
                    for evidence in search_result.claims
                ]
            }
        )

    def _select_claims(self, input_text: str, claims: list[ClaimForVerification]) -> ClaimSelection:
        if self.claim_selector is None:
            return ClaimSelection(selected=[claim.id for claim in claims])
        try:
            return self.claim_selector.select(input_text, claims)
        except Exception:
            # Best effort: an outage must not fail the submission, so fall back
            # to extraction order (the model lists the main claim first).
            log.warning("Claim selection failed; keeping the first claims", exc_info=True)
            limit = self.claim_selector.max_claims
            return ClaimSelection(
                selected=[claim.id for claim in claims[:limit]], skipped=[claim.id for claim in claims[limit:]]
            )

    def _persist_outcomes(
        self, run_id: uuid.UUID, by_id: dict[str, StoredClaim], ordered: list[ClaimOutcome]
    ) -> None:
        from prawdowid.persistence.evidence import record_outcome

        with self.intake.session_factory() as session, session.begin():
            for outcome in ordered:
                record_outcome(
                    session, run_id=run_id, claim_id=by_id[outcome.claim_id].claim_id, outcome=outcome
                )

    def _finish_run(self, run_id: uuid.UUID, result: PipelineResult, telemetry: "_RunTelemetry") -> None:
        """Closes the run once, after the verdicts: status, end-to-end latency,
        usage of every model call (intake + decisions + assessment), the cost
        estimate and any prompt-injection attempts found in the evidence."""
        now = datetime.now(timezone.utc)
        metered = self.usage_meter.entries() if self.usage_meter is not None else []
        with self.intake.session_factory() as session, session.begin():
            run = session.get_one(VerificationRun, run_id)
            run.status = result.status
            run.finished_at = now
            if run.started_at is not None:
                run.latency_ms = int((now - run.started_at).total_seconds() * 1000)
            run.models = [*(run.models or []), *metered, *telemetry.models]
            run.est_cost_usd = estimate_cost(run.models, model_prices())
            run.injection_flags = dedupe_injection_flags(
                [*(run.injection_flags or []), *telemetry.injection_flags]
            )
            if result.status != RunStatus.OK:
                run.error = result.message


@dataclass
class _RunTelemetry:
    models: list[dict[str, Any]] = field(default_factory=list)
    injection_flags: list[dict[str, Any]] = field(default_factory=list)


def _injection_flags(
    notes: list[InjectionNote], evidence: dict[str, list[EvidenceForAssessment]]
) -> list[dict[str, Any]]:
    """Recorded, never obeyed: the verdict is unaffected (scenario E)."""
    flags: list[dict[str, Any]] = []
    for note in notes:
        items = evidence.get(note.claim_id, [])
        if note.evidence_index >= len(items):
            continue
        flags.append(
            {
                "where": str(items[note.evidence_index].item.url),
                "claim_id": note.claim_id,
                "excerpt": note.excerpt[:500],
                "detected_by": "assessment_model",
            }
        )
    return flags

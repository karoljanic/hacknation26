"""Construct the real, environment-configured ``VerificationPipeline`` for
``POST /check``.

Every adapter is built per request (matching the CLI examples' pattern) so
there is no shared mutable client state between requests. A missing Gemini
key raises ``PipelineUnavailable``, which the endpoint turns into an explicit
``tech_failure`` status, never a fabricated merit result. The evidence
providers are optional: an unconfigured one is reported per claim as an
unavailable source.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from google import genai
from google.genai import types
from typesafe_sdk import RetryPolicy, TypeSafeClient

from prawdowid.adapters.sources.euvsdisinfo import EUvsDisinfoAdapter
from prawdowid.adapters.sources.google_fact_check import GoogleFactCheckAdapter
from prawdowid.adapters.sources.web import PublicWebFetcher
from prawdowid.ai.instances.gemini import GeminiAdapter
from prawdowid.ai.instances.gemini_embeddings import GeminiEmbeddingAdapter
from prawdowid.ai.instances.jev import JevAdapter
from prawdowid.domain.euvsdisinfo import EUvsDisinfoConnection
from prawdowid.ingestion.catalog import load_catalog
from prawdowid.persistence.db import session_factory
from prawdowid.persistence.models import EMBEDDING_DIM
from prawdowid.services.assessment import AssessmentGenerator
from prawdowid.adapters.sources.gov_api import GovPlSearchClient, OdyseuszClient
from prawdowid.services.claim_selection import ClaimSelector
from prawdowid.services.claims import ClaimExtractor
from prawdowid.services.evidence_search import EvidenceSearchOrchestrator
from prawdowid.services.gov_connectors import GovConnectorSelector
from prawdowid.services.intake import ClaimIntake
from prawdowid.services.pipeline import VerificationPipeline
from prawdowid.services.preparation import InputPreparer
from prawdowid.services.sublinks import SublinkSelector
from prawdowid.services.usage import MeteredDecision, UsageMeter

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
PREPARATION_MODEL = os.environ.get("GEMINI_PREPARATION_MODEL") or DEFAULT_GEMINI_MODEL
EXTRACTION_MODEL = os.environ.get("GEMINI_EXTRACTION_MODEL") or DEFAULT_GEMINI_MODEL
ASSESSMENT_MODEL = os.environ.get("GEMINI_ASSESSMENT_MODEL") or DEFAULT_GEMINI_MODEL
THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "LOW") or None
DECISION_MODEL = "jev-latest"
REQUIRED_ENV = ("GEMINI_API_KEY",)

log = logging.getLogger(__name__)


class PipelineUnavailable(RuntimeError):
    """Raised instead of running the pipeline with missing/invalid configuration."""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PipelineUnavailable(f"{name} nie jest skonfigurowany.")
    return value


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value:
        log.warning("%s is not configured; continuing without that provider", name)
    return value or None


@asynccontextmanager
async def build_pipeline() -> AsyncIterator[VerificationPipeline]:
    # Gemini does the extraction and the assessment: without it nothing can be
    # checked. Every other provider only adds evidence, so a missing key or an
    # outage degrades the answer (listed as a limitation per claim) instead of
    # turning every request into a technical failure.
    gemini_key = _require_env(REQUIRED_ENV[0])
    typesafe_key = _optional_env("TYPESAFE_API_KEY")
    factcheck_key = _optional_env("FACTCHECK_API_KEY")
    euvsdisinfo_key = _optional_env("EUVSDISINFO_API_KEY")

    meter = UsageMeter()  # one per request: usage of the decision calls
    async with AsyncExitStack() as stack:
        genai_client = stack.enter_context(
            genai.Client(api_key=gemini_key, http_options=types.HttpOptions(timeout=30000))
        )
        google = None
        if factcheck_key:
            google = GoogleFactCheckAdapter(client=stack.enter_context(httpx.Client()), api_key=factcheck_key)
        decision = None
        if typesafe_key:
            typesafe = stack.enter_context(
                TypeSafeClient(api_key=typesafe_key, timeout=30, retry=RetryPolicy(max_retries=0))
            )
            decision = MeteredDecision(JevAdapter(typesafe, DECISION_MODEL), meter)
        # Not opened here: the MCP connection must not wrap the whole check
        # (a transport failure cancels everything inside it). The evidence
        # search opens it only around its EUvsDisinfo queries.
        # Keyless public APIs (Karol's providers): gov.pl site search and MSZ
        # Odyseusz travel advisories.
        gov_search = stack.enter_context(GovPlSearchClient())
        odyseusz = stack.enter_context(OdyseuszClient())
        euvsdisinfo_connect = None
        if euvsdisinfo_key:
            connection = EUvsDisinfoConnection(api_key=euvsdisinfo_key)

            def euvsdisinfo_connect():
                return EUvsDisinfoAdapter.connect(connection)

        preparer = InputPreparer(
            llm=GeminiAdapter(genai_client, PREPARATION_MODEL, thinking_level=THINKING_LEVEL),
            fetcher=PublicWebFetcher(),
        )
        intake = ClaimIntake(
            session_factory=session_factory(),
            preparer=preparer,
            extractor=ClaimExtractor(GeminiAdapter(genai_client, EXTRACTION_MODEL, thinking_level=THINKING_LEVEL)),
            # Semantic dedup of repeated claims; if the provider fails,
            # intake falls back to exact-text matching (see ClaimIntake._embed).
            embedder=GeminiEmbeddingAdapter(genai_client, dimensions=EMBEDDING_DIM),
            keep_run_open=True,  # the pipeline closes the run after the verdicts
        )
        gov_pl_entries = [entry for entry in load_catalog().source if entry.collect.collector == "gov_pl"]
        evidence_search = EvidenceSearchOrchestrator(
            google=google,
            euvsdisinfo_connect=euvsdisinfo_connect,
            gov_selector=GovConnectorSelector(decision=decision, entries=gov_pl_entries)
            if gov_pl_entries and decision is not None
            else None,
            gov_search=gov_search,
            odyseusz=odyseusz,
        )
        sublinks = SublinkSelector(decision=decision, preparer=preparer)
        assessment = AssessmentGenerator(
            llm=GeminiAdapter(genai_client, ASSESSMENT_MODEL, thinking_level=THINKING_LEVEL)
        )
        yield VerificationPipeline(
            intake=intake, evidence_search=evidence_search, sublinks=sublinks, assessment=assessment,
            usage_meter=meter,
            # Without a decision model every extracted claim is checked.
            claim_selector=ClaimSelector(decision=decision) if decision is not None else None,
        )

"""Construct the real, environment-configured ``VerificationPipeline`` for
``POST /check``.

Every adapter is built per request (matching the CLI examples' pattern) so
there is no shared mutable client state between requests. A missing API key
raises ``PipelineUnavailable``, which the endpoint turns into an explicit
``tech_failure`` status, never a fabricated merit result.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
from prawdowid.adapters.sources.wikipedia import WikipediaClient
from prawdowid.services.claims import ClaimExtractor
from prawdowid.services.evidence_search import EvidenceSearchOrchestrator
from prawdowid.services.gov_connectors import GovConnectorSelector
from prawdowid.services.intake import ClaimIntake
from prawdowid.services.pipeline import VerificationPipeline
from prawdowid.services.preparation import InputPreparer
from prawdowid.services.sublinks import SublinkSelector

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
PREPARATION_MODEL = os.environ.get("GEMINI_PREPARATION_MODEL") or DEFAULT_GEMINI_MODEL
EXTRACTION_MODEL = os.environ.get("GEMINI_EXTRACTION_MODEL") or DEFAULT_GEMINI_MODEL
ASSESSMENT_MODEL = os.environ.get("GEMINI_ASSESSMENT_MODEL") or DEFAULT_GEMINI_MODEL
THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "LOW") or None
DECISION_MODEL = "jev-latest"
REQUIRED_ENV = ("GEMINI_API_KEY", "TYPESAFE_API_KEY", "FACTCHECK_API_KEY", "EUVSDISINFO_API_KEY")


class PipelineUnavailable(RuntimeError):
    """Raised instead of running the pipeline with missing/invalid configuration."""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PipelineUnavailable(f"{name} nie jest skonfigurowany.")
    return value


@asynccontextmanager
async def build_pipeline() -> AsyncIterator[VerificationPipeline]:
    keys = {name: _require_env(name) for name in REQUIRED_ENV}

    with (
        genai.Client(api_key=keys["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=30000)) as genai_client,
        httpx.Client() as google_http,
        GovPlSearchClient() as gov_search,
        OdyseuszClient() as odyseusz,
        WikipediaClient() as wikipedia,
        TypeSafeClient(api_key=keys["TYPESAFE_API_KEY"], timeout=30, retry=RetryPolicy(max_retries=0)) as typesafe,
    ):
        connection = EUvsDisinfoConnection(api_key=keys["EUVSDISINFO_API_KEY"])
        async with EUvsDisinfoAdapter.connect(connection) as euvsdisinfo:
            preparer = InputPreparer(llm=GeminiAdapter(genai_client, PREPARATION_MODEL, thinking_level=THINKING_LEVEL), fetcher=PublicWebFetcher())
            intake = ClaimIntake(
                session_factory=session_factory(),
                preparer=preparer,
                extractor=ClaimExtractor(GeminiAdapter(genai_client, EXTRACTION_MODEL, thinking_level=THINKING_LEVEL)),
                # Semantic dedup of repeated claims; if the provider fails,
                # intake falls back to exact-text matching (see ClaimIntake._embed).
                embedder=GeminiEmbeddingAdapter(genai_client, dimensions=EMBEDDING_DIM),
            )
            decision = JevAdapter(typesafe, DECISION_MODEL)
            gov_pl_entries = [entry for entry in load_catalog().source if entry.collect.collector == "gov_pl"]
            evidence_search = EvidenceSearchOrchestrator(
                google=GoogleFactCheckAdapter(client=google_http, api_key=keys["FACTCHECK_API_KEY"]),
                euvsdisinfo=euvsdisinfo,
                gov_selector=GovConnectorSelector(decision=decision, entries=gov_pl_entries)
                if gov_pl_entries
                else None,
                gov_search=gov_search,
                odyseusz=odyseusz,
                wikipedia=wikipedia,
            )
            sublinks = SublinkSelector(decision=decision, preparer=preparer)
            assessment = AssessmentGenerator(llm=GeminiAdapter(genai_client, ASSESSMENT_MODEL, thinking_level=THINKING_LEVEL))
            yield VerificationPipeline(
                intake=intake, evidence_search=evidence_search, sublinks=sublinks, assessment=assessment
            )

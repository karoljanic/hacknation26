"""Vocabularies shared by the domain, persistence and API layers.

Stored as plain strings with CHECK constraints (not native Postgres enums), so
adding a value is a one-line migration instead of an ``ALTER TYPE``.
"""

from enum import StrEnum


class SourceKind(StrEnum):
    OFFICIAL = "official"  # government / institutional primary source
    PUBLIC_DATA = "public_data"  # statistics, registers, open data
    FACT_CHECKER = "fact_checker"  # IFCN/EFCSN signatories etc.
    MEDIA = "media"  # reputable secondary reporting


class ClaimKind(StrEnum):
    """What the extractor found (mirrors ``domain.claims.Claim.kind``)."""

    FACTUAL_CLAIM = "factual_claim"
    VERIFICATION_QUESTION = "verification_question"
    OPINION = "opinion"  # not checkable -> verdict "unverifiable"


class ClaimType(StrEnum):
    """Finer typing of factual claims; optional, filled by a later stage."""

    NUMERIC = "numeric"
    EVENT = "event"
    QUOTE = "quote"
    CAUSAL = "causal"
    PREDICTION = "prediction"


class ClaimOrigin(StrEnum):
    CORPUS = "corpus"  # extracted from a source document / fact-check
    USER = "user"  # extracted from a citizen's submission


class EvidenceRelation(StrEnum):
    """Passage -> Claim edge."""

    SUPPORTS = "supports"
    REFUTES = "refutes"
    ADDS_CONTEXT = "adds_context"


class ClaimRelation(StrEnum):
    """Claim -> Claim edge."""

    SAME_AS = "same_as"
    NEGATES = "negates"
    CONTRADICTS = "contradicts"
    SUPERSEDES = "supersedes"  # newer figure/statement replaces older one
    PART_OF = "part_of"
    SPECIALIZES = "specializes"


class DocumentRelation(StrEnum):
    """Document -> Document edge."""

    CITES = "cites"
    DERIVED_FROM = "derived_from"


class SubmissionClaimRelation(StrEnum):
    STATES = "states"  # the submission asserts the claim (or an opinion)
    IMPLIES = "implies"  # presupposed / implied, not asserted
    ASKS = "asks"  # the submission asks whether the claim holds


class Verdict(StrEnum):
    """The only merit outcomes. Technical problems are *never* a verdict."""

    CONFIRMED = "confirmed"  # potwierdzone
    FALSE = "false"  # fałszywe
    MISLEADING = "misleading"  # wprowadzające w błąd
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # brak wystarczających dowodów
    UNVERIFIABLE = "unverifiable"  # nie podlega weryfikacji


class Strength(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"


class Channel(StrEnum):
    WEB = "web"
    DISCORD = "discord"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    MESSENGER = "messenger"
    DEMO = "demo"
    X = "x"  # direct messages to @prawdowid


class RunStatus(StrEnum):
    """Outcome of processing a submission, kept apart from per-claim verdicts."""

    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    TECH_FAILURE = "tech_failure"  # missing key, rate limit, provider outage
    NEEDS_CLARIFICATION = "needs_clarification"
    OUT_OF_SCOPE = "out_of_scope"


class IngestionStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"  # some documents failed, the rest were stored
    FAILED = "failed"  # the collector itself failed (network, API error, ...)
    SKIPPED = "skipped"  # not attempted, e.g. missing API key


class JobStatus(StrEnum):
    """Outcome of one worker task / CLI command (see ``job_run``)."""

    RUNNING = "running"
    OK = "ok"
    PROBLEMS = "problems"  # finished, but reported errors / skips / partial results
    FAILED = "failed"  # raised an exception


class CaseState(StrEnum):
    # The system must distinguish recommending a hand-off from doing it.
    RECOMMENDED = "recommended"
    FORWARDED = "forwarded"
    RESOLVED = "resolved"

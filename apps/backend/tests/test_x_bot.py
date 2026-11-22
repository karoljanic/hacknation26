import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

from prawdowid.adapters.channels.x_dm import parse_events
from prawdowid.api.rate_limit import CheckGuard, CheckLimits
from prawdowid.domain.enums import RunStatus
from prawdowid.domain.evidence import AssessmentReasons, ClaimAssessmentOutput, EvidenceForAssessment, EvidenceItem
from prawdowid.services.pipeline import ClaimOutcome, PipelineResult
from prawdowid.services.x_bot import XDmBot, format_reply

OWN, USER = "100", "200"


def dm_body(*messages: dict, tweets: list[dict] | None = None, users: list[dict] | None = None) -> dict:
    return {"data": list(messages), "includes": {"tweets": tweets or [], "users": users or []}}


def message(event_id: str, text: str, sender: str = USER, **extra) -> dict:
    return {"id": event_id, "event_type": "MessageCreate", "text": text, "sender_id": sender, **extra}


def answered(text: str = "Sprawdzone.") -> PipelineResult:
    return PipelineResult(uuid.uuid4(), uuid.uuid4(), RunStatus.NEEDS_CLARIFICATION, text)


def make_bot(tmp_path, bodies: list[dict]):
    dm = Mock()
    dm.account_id = OWN
    dm.recent_events.side_effect = [parse_events(body) for body in bodies]
    pipeline = Mock()
    for name in ("run_text", "run_prepared", "run_url", "run_deferred"):
        setattr(pipeline, name, AsyncMock(return_value=answered()))

    @asynccontextmanager
    async def factory():
        yield pipeline

    guard = Mock()
    guard.screen = AsyncMock(return_value=Mock(blocked=False))
    bot = XDmBot(
        dm, pipeline_factory=factory, injection_guard=lambda: guard,
        limits=CheckGuard(CheckLimits(per_minute=100, per_day=100, global_per_day=100, max_concurrent=4)),
        state_file=tmp_path / "state.json",
    )
    return bot, dm, pipeline


def test_each_new_message_is_answered_once_and_the_backlog_is_skipped(tmp_path) -> None:
    old = message("10", "Stara wiadomość")
    new = message("12", "Czy VAT wzrósł?")
    mine = message("13", "Odpowiedź bota", sender=OWN)
    bot, dm, pipeline = make_bot(tmp_path, [dm_body(old), dm_body(mine, new, old), dm_body(mine, new, old)])

    assert asyncio.run(bot.poll_once()) == []  # first run: history is not answered
    first = asyncio.run(bot.poll_once())
    again = asyncio.run(bot.poll_once())

    assert [event.id for event, _ in first] == ["12"] and again == []
    pipeline.run_text.assert_awaited_once()
    assert pipeline.run_text.call_args.args[0] == "Czy VAT wzrósł?"
    dm.reply.assert_called_once_with(USER, "Sprawdzone.")


def test_a_shared_post_is_checked_by_its_text(tmp_path) -> None:
    shared = message(
        "12", "https://t.co/abc", referenced_tweets=[{"id": "7"}],
        entities={"urls": [{"url": "https://t.co/abc", "expanded_url": "https://x.com/kto/status/7"}]},
    )
    body = dm_body(shared, tweets=[{"id": "7", "text": "Rząd podnosi VAT do 30%.", "author_id": "9",
                                     "created_at": "2026-11-21T08:30:00.000Z"}],
                   users=[{"id": "9", "username": "kto"}])
    bot, dm, pipeline = make_bot(tmp_path, [dm_body(message("10", "x")), body])

    asyncio.run(bot.poll_once())
    asyncio.run(bot.poll_once())

    prepared = pipeline.run_prepared.call_args.args[0]
    assert [(s.origin, s.text) for s in prepared.segments] == [
        ("text", "Wpis @kto na X z 21 listopada 2026 r., godz. 09:30:\nRząd podnosi VAT do 30%.")
    ]
    pipeline.run_url.assert_not_called()


def test_reply_lists_verdict_explanation_and_source() -> None:
    item = EvidenceItem(source="google_fact_check", kind="claim_review", title="Analiza",
                        url="https://demagog.org.pl/a", publisher="Demagog", snippet="Fałsz.")
    outcome = ClaimOutcome(
        "c1", "Rząd podnosi VAT do 30%.", "factual_claim",
        ClaimAssessmentOutput(
            claim_id="c1", verdict="false", strength="strong",
            reasons=AssessmentReasons(source_tiers=[2], independent_sources=1, conflicting_sources=False),
            explanation="Nie. Demagog: to fałsz.",
            citations=[{"evidence_index": 0, "quote": "Fałsz.", "relation": "refutes"}],
        ),
        "model:test", [EvidenceForAssessment(item=item, text="Fałsz.", fully_fetched=False)],
    )

    reply = format_reply(PipelineResult(uuid.uuid4(), uuid.uuid4(), RunStatus.OK, claims=[outcome]))

    assert reply.startswith("❌ Fałsz: Rząd podnosi VAT do 30%.\nNie. Demagog: to fałsz.")
    assert "Źródło: Demagog – https://demagog.org.pl/a" in reply

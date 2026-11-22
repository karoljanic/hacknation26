"""@prawdowid on X: answers direct messages with a fact-check.

Polls the account's DM events, checks every new message from someone else
through the same pipeline as ``POST /check`` and replies in the same
conversation. A message may be text, a link, an attached image, or a shared
X post (its text comes with the DM, so no screenshot is needed).

Each message is answered at most once: the newest handled event id is saved
next to the OAuth tokens. On the first run the existing backlog is skipped.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from prawdowid.adapters.channels.x_dm import DMBatch, DMEvent, XApiError, XDirectMessages
from prawdowid.ai.adapters.media import Media
from prawdowid.api.rate_limit import CheckGuard, RateLimited
from prawdowid.domain.claims import InputSegment
from prawdowid.domain.enums import Channel, RunStatus
from prawdowid.domain.preparation import PreparedInput
from prawdowid.security.injection_guard import InjectionGuard
from prawdowid.services.pipeline import PipelineResult, VerificationPipeline
from prawdowid.services.clock import POLAND
from prawdowid.services.preparation import IMAGE_TYPES, MAX_MEDIA_BYTES, MAX_QUESTION_CHARS, MAX_TEXT_CHARS
from prawdowid.services.social import social_link_message, social_platform

log = logging.getLogger(__name__)

POLL_SECONDS_ENV = "X_DM_POLL_SECONDS"

VERDICT_LABEL = {
    "confirmed": "✅ Prawda",
    "false": "❌ Fałsz",
    "misleading": "⚠️ Częściowo prawdziwe",
    "insufficient_evidence": "❔ Brak wystarczających dowodów",
    "unverifiable": "❔ Nie podlega weryfikacji",
}
MAX_SOURCES = 2
HELP_MESSAGE = (
    "Cześć! Napisz, co chcesz sprawdzić: wklej treść, link albo zdjęcie, albo udostępnij mi wpis z X."
)
BUSY_MESSAGE = "Nie mogę teraz sprawdzić tej wiadomości: {reason}"
TECH_MESSAGE = "Błąd techniczny podczas sprawdzania. Spróbuj ponownie później."
INJECTION_MESSAGE = (
    "Nie mogę tego sprawdzić: wiadomość wygląda na próbę zmiany działania systemu. "
    "Napisz samo twierdzenie albo pytanie do sprawdzenia."
)


def sender_hash(sender_id: str) -> str:
    """Salted, so the stored hash cannot be matched to an X account id."""
    salt = os.environ.get("PRAWDOWID_SIGNING_KEY", "").encode()
    return hmac.new(salt or b"prawdowid", f"x:{sender_id}".encode(), hashlib.sha256).hexdigest()


MONTHS = ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
          "października", "listopada", "grudnia")


def polish_date(moment: datetime) -> str:
    local = moment.astimezone(POLAND)
    return f"{local.day} {MONTHS[local.month - 1]} {local.year} r., godz. {local:%H:%M}"


def _strip_links(text: str, event: DMEvent) -> str:
    for short, _ in event.urls:
        text = text.replace(short, " ")
    return re.sub(r"\s+", " ", text).strip()


def format_reply(result: PipelineResult) -> str:
    """Plain-text answer for a DM: one block per claim with its sources."""
    from prawdowid.api.check import _to_check_response

    response = _to_check_response(result)
    if response.status != RunStatus.OK or not response.claims:
        return response.message or "Nie znalazłem w tej wiadomości nic do sprawdzenia. " + HELP_MESSAGE
    blocks = []
    for claim in response.claims:
        lines = [f"{VERDICT_LABEL.get(claim.verdict.value, claim.verdict.value)}: {claim.text}", claim.explanation]
        if claim.limitations:
            lines.append(claim.limitations)
        for question in claim.missing_details:
            lines.append(f"• {question}")
        for source in claim.sources[:MAX_SOURCES]:
            lines.append(f"Źródło: {source.publisher} – {source.url}")
        if claim.answer_hash:
            lines.append(f"Podpis odpowiedzi: {claim.answer_hash}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class XDmBot:
    def __init__(
        self,
        dm: XDirectMessages,
        *,
        pipeline_factory: Callable[[], AbstractAsyncContextManager[VerificationPipeline]],
        injection_guard: Callable[[], InjectionGuard],
        limits: CheckGuard,
        state_file: Path,
    ) -> None:
        self.dm = dm
        self.pipeline_factory = pipeline_factory
        self.injection_guard = injection_guard
        self.limits = limits
        self.state_file = state_file

    def _last_handled(self) -> int | None:
        try:
            return int(json.loads(self.state_file.read_text())["last_event_id"])
        except (OSError, ValueError, KeyError):
            return None

    def _mark_handled(self, event_id: str) -> None:
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"last_event_id": event_id}))
        os.replace(temporary, self.state_file)

    async def poll_once(self, *, send: bool = True) -> list[tuple[DMEvent, str]]:
        """Answers every new message; returns (message, reply) pairs."""
        batch = await asyncio.to_thread(self.dm.recent_events)
        if not batch.events:
            return []
        last = self._last_handled()
        if last is None:
            # First run: start after the existing conversation history.
            self._mark_handled(max(batch.events, key=lambda e: int(e.id)).id)
            log.info("X DM bot started; %d earlier messages left unanswered", len(batch.events))
            return []
        own = self.dm.account_id
        new = sorted(
            (e for e in batch.events if int(e.id) > last and e.sender_id and e.sender_id != own),
            key=lambda e: int(e.id),
        )
        handled: list[tuple[DMEvent, str]] = []
        for event in new:
            reply = await self.answer(event, batch)
            if send:
                try:
                    await asyncio.to_thread(self.dm.reply, event.sender_id, reply)  # type: ignore[arg-type]
                except XApiError:
                    log.warning("Reply to DM %s failed", event.id, exc_info=True)
            # Marked even when the reply failed: retrying would re-run (and pay for) the check.
            self._mark_handled(event.id)
            handled.append((event, reply))
        return handled

    async def answer(self, event: DMEvent, batch: DMBatch) -> str:
        typed = _strip_links(event.text, event)
        if typed:
            try:
                screening = await (await asyncio.to_thread(self.injection_guard)).screen(typed)
            except Exception:
                log.exception("Injection guard unavailable; DM %s not checked", event.id)
                return TECH_MESSAGE
            if screening.blocked:
                log.warning("Blocked suspected prompt injection in DM %s: %s", event.id, screening.blocked_by)
                return INJECTION_MESSAGE
        try:
            self.limits.admit(f"x:{sender_hash(event.sender_id or '')}")
        except RateLimited as exc:
            return BUSY_MESSAGE.format(reason=exc.message)
        try:
            return await self._check(event, batch, typed)
        except Exception:
            log.warning("Checking DM %s failed", event.id, exc_info=True)
            return TECH_MESSAGE
        finally:
            self.limits.release()

    async def _check(self, event: DMEvent, batch: DMBatch, typed: str) -> str:
        who = sender_hash(event.sender_id or "")
        posts = [batch.posts[i] for i in event.referenced_tweet_ids if i in batch.posts]
        photos = [
            batch.media[key] for key in event.media_keys
            if key in batch.media and batch.media[key].type == "photo" and batch.media[key].url
        ]
        links = [url for _, url in event.urls if social_platform(url) is None]
        question = typed[:MAX_QUESTION_CHARS] or None

        async with self.pipeline_factory() as pipeline:
            if posts:
                post = posts[0]
                segments = [InputSegment(id="question-1", text=question, origin="question")] if question else []
                author = f"@{post.author}" if post.author else "użytkownika"
                # The post's own date resolves its "dziś", "w nocy", "jutro".
                when = f" z {polish_date(post.created_at)}" if post.created_at else ""
                segments.append(
                    InputSegment(
                        id="post-1", text=f"Wpis {author} na X{when}:\n{post.text[:MAX_TEXT_CHARS]}", origin="text"
                    )
                )
                result = await pipeline.run_prepared(PreparedInput(segments=segments), channel=Channel.X, sender_hash=who)
            elif photos:
                data, mime_type = await asyncio.to_thread(self.dm.download, photos[0].url, MAX_MEDIA_BYTES)  # type: ignore[arg-type]
                if mime_type not in IMAGE_TYPES:
                    return "Obsługuję zdjęcia JPG, PNG i WebP. " + HELP_MESSAGE

                def prepare() -> PreparedInput:
                    prepared = pipeline.intake.read_media(Media(data=data, mime_type=mime_type), source_name="x-dm")
                    if question is None:
                        return prepared
                    head = InputSegment(id="question-1", text=question, origin="question")
                    return prepared.model_copy(update={"segments": [head, *prepared.segments]})

                result = await pipeline.run_deferred(prepare, channel=Channel.X, sender_hash=who)
            elif links:
                url = links[0]
                if question is None:
                    result = await pipeline.run_url(url, channel=Channel.X, sender_hash=who)
                else:

                    def prepare() -> PreparedInput:
                        prepared = pipeline.intake.preparer.prepare_url(url)
                        head = InputSegment(id="question-1", text=question, origin="question")
                        return prepared.model_copy(update={"segments": [head, *prepared.segments]})

                    result = await pipeline.run_deferred(prepare, channel=Channel.X, url=url, sender_hash=who)
            elif event.urls:
                platform = social_platform(event.urls[0][1]) or "X"
                return social_link_message(platform)
            elif typed:
                result = await pipeline.run_text(typed[:MAX_TEXT_CHARS], channel=Channel.X, sender_hash=who)
            else:
                return HELP_MESSAGE
        return format_reply(result)


async def run_forever(bot: XDmBot, interval: float) -> None:
    log.info("X DM bot polling every %.0f s", interval)
    while True:
        try:
            await bot.poll_once()
        except XApiError as exc:
            log.warning("X DM poll failed: %s", exc)
            if exc.status == 429:
                await asyncio.sleep(15 * 60)  # X rate limit windows are 15 minutes
        except Exception:
            log.exception("X DM poll failed")
        await asyncio.sleep(interval)

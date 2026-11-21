import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

import httpx
from google import genai
from google.genai import types
from pydantic import TypeAdapter

from prawdowid.ai.adapters.media import Media
from prawdowid.ai.instances.gemini import GeminiAdapter
from prawdowid.ai.instances.gemini_embeddings import GeminiEmbeddingAdapter
from prawdowid.adapters.sources.google_fact_check import GoogleFactCheckAdapter
from prawdowid.adapters.sources.web import PublicWebFetcher
from prawdowid.domain.claims import InputSegment
from prawdowid.domain.enums import Channel
from prawdowid.domain.preparation import PreparedInput
from prawdowid.persistence.db import session_factory
from prawdowid.persistence.models import EMBEDDING_DIM
from prawdowid.services.claims import ClaimExtractor
from prawdowid.services.fact_checks import FactCheckTool
from prawdowid.services.intake import ClaimIntake
from prawdowid.services.preparation import MAX_MEDIA_BYTES, InputPreparer

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/mov",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract claims, not truth verdicts")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--text")
    inputs.add_argument("--question")
    inputs.add_argument("--url")
    inputs.add_argument("--image", type=Path)
    inputs.add_argument("--audio", type=Path)
    inputs.add_argument("--video", type=Path)
    inputs.add_argument("--segments", type=Path, help="JSON file containing normalized segments")
    parser.add_argument("--model", default="gemini-2.5-flash")
    parser.add_argument("--preparation-model", default="gemini-2.5-flash")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--fact-check", action="store_true", help="Search extracted claims in Google Fact Check"
    )
    actions.add_argument(
        "--store",
        action="store_true",
        help="Save the submission and deduplicated claims to DATABASE_URL (redacts PII first)",
    )
    parser.add_argument("--channel", default=Channel.DEMO.value, choices=[c.value for c in Channel])
    args = parser.parse_args()
    fact_check_key = os.environ["FACTCHECK_API_KEY"] if args.fact_check else None
    if fact_check_key is not None and not fact_check_key.strip():
        raise ValueError("Google Fact Check API key must not be empty")

    with genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(timeout=30000),
    ) as client:
        preparer = InputPreparer(
            llm=GeminiAdapter(client, args.preparation_model), fetcher=PublicWebFetcher()
        )
        if args.store:
            intake = ClaimIntake(
                session_factory=session_factory(),
                preparer=preparer,
                extractor=ClaimExtractor(GeminiAdapter(client, args.model)),
                embedder=GeminiEmbeddingAdapter(client, dimensions=EMBEDDING_DIM),
            )
            channel = Channel(args.channel)
            if args.url is not None:
                outcome = intake.submit_url(args.url, channel=channel)
            elif args.text is not None or args.question is not None:
                outcome = intake.submit_text(
                    args.question if args.question is not None else args.text,
                    channel=channel,
                    question=args.question is not None,
                )
            else:
                outcome = None  # media / segments: prepared below, then stored
            if outcome is not None:
                print(json.dumps(asdict(outcome), default=str, ensure_ascii=False, indent=2))
                return
        media_path = args.image or args.audio or args.video
        if media_path is not None:
            mime_type = MIME_BY_SUFFIX.get(media_path.suffix.lower())
            expected_kind = "image" if args.image else "audio" if args.audio else "video"
            if mime_type is None or not mime_type.startswith(expected_kind + "/"):
                parser.error("Unsupported file extension for the selected media input")
            with media_path.open("rb") as file:
                media = Media(data=file.read(MAX_MEDIA_BYTES + 1), mime_type=mime_type)
            prepared = preparer.prepare_media(media, source_name=media_path.name)
        elif args.url is not None:
            prepared = preparer.prepare_url(args.url)
        elif args.segments is not None:
            prepared = PreparedInput(
                segments=TypeAdapter(list[InputSegment]).validate_json(
                    args.segments.read_text(encoding="utf-8")
                )
            )
        else:
            prepared = preparer.prepare_text(
                args.question if args.question is not None else args.text,
                question=args.question is not None,
            )
        if args.store:
            outcome = intake.submit_prepared(prepared, channel=channel)
            print(json.dumps(asdict(outcome), default=str, ensure_ascii=False, indent=2))
            return
        result = ClaimExtractor(GeminiAdapter(client, args.model)).extract(prepared.segments)

    fact_checks = None
    if fact_check_key is not None:
        with httpx.Client() as http_client:
            source = GoogleFactCheckAdapter(
                client=http_client, api_key=fact_check_key
            )
            fact_checks = FactCheckTool(source).search_claims(result.data)

    output = {
        "segments": [segment.model_dump(mode="json") for segment in prepared.segments],
        "preparation": prepared.model_dump(mode="json", exclude={"segments"}),
        "extraction": result.data.model_dump(mode="json"),
        "requested_model": result.requested_model,
        "model_version": result.model_version,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }
    if fact_checks is not None:
        output["fact_checks"] = fact_checks.model_dump(mode="json")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

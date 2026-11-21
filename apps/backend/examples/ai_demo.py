import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from google import genai
from google.genai import types
from typesafe_sdk import RetryPolicy, TypeSafeClient

from prawdowid.ai.adapters.decision import (
    BooleanQuestion,
    BoxQuestion,
    ChoiceQuestion,
    DecisionAdapter,
    ScoreQuestion,
)
from prawdowid.ai.adapters.embedding import EmbeddingAdapter
from prawdowid.ai.adapters.llm import LLMAdapter
from prawdowid.ai.adapters.media import Media
from prawdowid.ai.instances.gemini import GeminiAdapter
from prawdowid.ai.instances.gemini_embeddings import GeminiEmbeddingAdapter
from prawdowid.ai.instances.jev import JevAdapter


def read_image(path: Path) -> Media:
    suffixes = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    if path.suffix.lower() not in suffixes:
        raise ValueError("Choose a PNG or JPEG file")
    return Media(path.read_bytes(), suffixes[path.suffix.lower()])


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit, billable AI adapter examples")
    parser.add_argument("example", choices=("fast", "search", "decision", "image", "embed-text", "embed-image"))
    parser.add_argument("--model", help="Override the example's model")
    parser.add_argument("--image", type=Path, help="PNG or JPEG for the experimental Box example")
    args = parser.parse_args()

    if args.example in ("image", "embed-image") and args.image is None:
        parser.error("image and embed-image require --image PATH")
    if args.example not in ("image", "embed-image") and args.image is not None:
        parser.error("--image is only valid for image and embed-image")

    if args.example in ("fast", "search", "embed-text", "embed-image"):
        default_model = "gemini-2.5-flash" if args.example == "fast" else "gemini-3.8-flash"
        prompt = (
            "Wypisz tezy do sprawdzenia: Według wiadomości decyzja weszła w życie w 2025 roku."
            if args.example == "fast"
            else "Znajdź oficjalne informacje o tym, czym jest EUR-Lex. Podaj źródła."
        )
        with genai.Client(
            api_key=os.environ["GEMINI_API_KEY"],
            http_options=types.HttpOptions(timeout=30000),
        ) as client:
            if args.example in ("embed-text", "embed-image"):
                embedder: EmbeddingAdapter = GeminiEmbeddingAdapter(
                    client, args.model or "gemini-embedding-2"
                )
                if args.example == "embed-image":
                    assert args.image is not None
                    result = embedder.embed(read_image(args.image))
                else:
                    result = embedder.embed("Europejski dokument urzędowy")
            else:
                llm: LLMAdapter = GeminiAdapter(client, args.model or default_model)
                result = llm.generate(prompt, search=args.example == "search")
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return

    with TypeSafeClient(
        api_key=os.environ["TYPESAFE_API_KEY"],
        timeout=30,
        retry=RetryPolicy(max_retries=0),
    ) as client:
        adapter: DecisionAdapter = JevAdapter(client, args.model or "jev-latest")
        if args.example == "image":
            assert args.image is not None
            result = adapter.decide(
                state="Inspect the image.",
                questions={
                    "text_present": BooleanQuestion(instructions="Is any text visible in `image`?"),
                    "text_region": BoxQuestion(instructions="The main text region in `image`."),
                },
                image=read_image(args.image),
            )
        else:
            result = adapter.decide(
                state="Wiadomość twierdzi, że decyzja weszła w życie w 2025 roku.",
                questions={
                    "checkable": BooleanQuestion(instructions="Does the message contain a checkable claim?"),
                    "topic": ChoiceQuestion(
                        instructions="Which topic best fits the message?",
                        criteria={
                            "law": "A legal act or administrative decision.",
                            "other": "Another subject or not enough context.",
                        },
                    ),
                    "clarity": ScoreQuestion(
                        instructions="How clear is the claim's context?",
                        criteria=["Unclear", "Some context", "Clear and specific"],
                    ),
                },
            )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

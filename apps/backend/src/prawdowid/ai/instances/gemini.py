from google import genai
from google.genai import types
from pydantic import ValidationError

from prawdowid.ai.adapters.llm import ContentBlocked, GenerationResult, ResultT, StructuredResult
from prawdowid.ai.adapters.media import Media


# Fact-checking is about violence, war, crime and hate speech by nature: block
# only content rated highly likely harmful. Prohibited content (e.g. child
# abuse) stays blocked by the provider regardless of these settings.
STRUCTURED_ATTEMPTS = 2

SAFETY_SETTINGS = [
    types.SafetySetting(category=category, threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH)
    for category in (
        types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    )
]
_BLOCKING_FINISH_REASONS = {
    types.FinishReason.SAFETY,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.SPII,
    types.FinishReason.IMAGE_SAFETY,
    types.FinishReason.IMAGE_PROHIBITED_CONTENT,
}


def _raise_if_blocked(response: types.GenerateContentResponse) -> None:
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and feedback.block_reason is not None:
        raise ContentBlocked(f"Gemini blocked the input: {feedback.block_reason}")
    candidates = getattr(response, "candidates", None)
    candidate = candidates[0] if candidates else None
    if candidate is not None and candidate.finish_reason in _BLOCKING_FINISH_REASONS:
        raise ContentBlocked(f"Gemini blocked the answer: {candidate.finish_reason}")


class GeminiAdapter:
    def __init__(self, client: genai.Client, model: str, *, thinking_level: str | None = None) -> None:
        """``thinking_level`` (e.g. ``LOW``) caps reasoning effort to cut latency on Gemini 3 models."""
        self.client = client
        self.model = model
        self.thinking_level = thinking_level

    def _thinking(self) -> types.ThinkingConfig | None:
        if not self.thinking_level:
            return None
        return types.ThinkingConfig(thinking_level=self.thinking_level)

    def generate(self, prompt: str, *, search: bool = False) -> GenerationResult:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")

        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())] if search else None,
            thinking_config=self._thinking(),
            safety_settings=SAFETY_SETTINGS,
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
        _raise_if_blocked(response)
        text = response.text
        if not text:
            raise RuntimeError("Gemini returned no text")

        candidate = response.candidates[0] if response.candidates else None
        grounding = candidate.grounding_metadata if candidate else None
        usage = response.usage_metadata
        return GenerationResult(
            text=text,
            requested_model=self.model,
            model_version=response.model_version,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=usage.candidates_token_count if usage else None,
            grounding=grounding.model_dump(mode="json", exclude_none=True) if grounding else None,
        )

    def generate_structured(
        self,
        prompt: str,
        schema: type[ResultT],
        *,
        instructions: str,
        media: Media | None = None,
    ) -> StructuredResult[ResultT]:
        if not prompt.strip() or not instructions.strip():
            raise ValueError("Prompt and instructions must not be empty")
        if media is not None and (not media.data or not media.mime_type.strip()):
            raise ValueError("Media data and MIME type must not be empty")

        parts = [types.Part.from_text(text=prompt)]
        if media is not None:
            parts.append(types.Part.from_bytes(data=media.data, mime_type=media.mime_type))

        input_tokens = output_tokens = 0
        # Structured output occasionally breaks the schema; one fresh attempt usually fixes it.
        for attempt in range(STRUCTURED_ATTEMPTS):
            response = self.client.models.generate_content(
                model=self.model,
                contents=types.Content(role="user", parts=parts),
                config=types.GenerateContentConfig(
                    system_instruction=instructions,
                    response_mime_type="application/json",
                    response_json_schema=schema.model_json_schema(),
                    thinking_config=self._thinking(),
                    safety_settings=SAFETY_SETTINGS,
                ),
            )
            _raise_if_blocked(response)
            usage = getattr(response, "usage_metadata", None)
            input_tokens += (usage.prompt_token_count or 0) if usage else 0
            output_tokens += (usage.candidates_token_count or 0) if usage else 0
            text = response.text
            try:
                if not text:
                    raise RuntimeError("Gemini returned no structured output")
                data = schema.model_validate_json(text)
                break
            except (RuntimeError, ValidationError) as error:
                if attempt + 1 == STRUCTURED_ATTEMPTS:
                    if isinstance(error, RuntimeError):
                        raise
                    raise RuntimeError("Gemini returned output that does not match the schema") from error

        return StructuredResult(
            data=data,
            requested_model=self.model,
            model_version=response.model_version,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
        )

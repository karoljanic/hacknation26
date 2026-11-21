from google import genai
from google.genai import types
from pydantic import ValidationError

from prawdowid.ai.adapters.llm import GenerationResult, ResultT, StructuredResult
from prawdowid.ai.adapters.media import Media


class GeminiAdapter:
    def __init__(self, client: genai.Client, model: str) -> None:
        self.client = client
        self.model = model

    def generate(self, prompt: str, *, search: bool = False) -> GenerationResult:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")

        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())] if search else None
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
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

        response = self.client.models.generate_content(
            model=self.model,
            contents=types.Content(role="user", parts=parts),
            config=types.GenerateContentConfig(
                system_instruction=instructions,
                response_mime_type="application/json",
                response_json_schema=schema.model_json_schema(),
            ),
        )
        text = response.text
        if not text:
            raise RuntimeError("Gemini returned no structured output")
        try:
            data = schema.model_validate_json(text)
        except ValidationError as error:
            raise RuntimeError("Gemini returned output that does not match the schema") from error

        usage = response.usage_metadata
        return StructuredResult(
            data=data,
            requested_model=self.model,
            model_version=response.model_version,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=usage.candidates_token_count if usage else None,
        )

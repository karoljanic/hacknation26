from typesafe_sdk import Box, Choice, Image, Noul, Question, Score, TypeSafeClient

from prawdowid.ai.adapters.decision import (
    BooleanQuestion,
    BoxQuestion,
    ChoiceQuestion,
    DecisionQuestion,
    DecisionResult,
    ScoreQuestion,
)
from prawdowid.ai.adapters.media import Media


class JevAdapter:
    def __init__(self, client: TypeSafeClient, model: str = "jev-latest") -> None:
        self.client = client
        self.model = model

    def decide(
        self,
        state: str,
        questions: dict[str, DecisionQuestion],
        *,
        image: Media | None = None,
    ) -> DecisionResult:
        if not questions:
            raise ValueError("At least one question is required")
        if image is None and any(isinstance(question, BoxQuestion) for question in questions.values()):
            raise ValueError("Box questions require an image")
        if image is not None and (not image.data or image.mime_type not in {"image/png", "image/jpeg"}):
            raise ValueError("Jev supports non-empty PNG or JPEG images")

        sdk_questions: dict[str, Question] = {}
        for name, question in questions.items():
            if isinstance(question, BooleanQuestion):
                sdk_questions[name] = Noul(instructions=question.instructions)
            elif isinstance(question, ChoiceQuestion):
                sdk_questions[name] = Choice(
                    instructions=question.instructions, criteria=question.criteria
                )
            elif isinstance(question, ScoreQuestion):
                sdk_questions[name] = Score(
                    instructions=question.instructions, criteria=question.criteria
                )
            elif isinstance(question, BoxQuestion):
                sdk_questions[name] = Box(instructions=question.instructions)
            else:
                raise TypeError(f"Unsupported decision question: {type(question).__name__}")

        response = self.client.system_one(
            state={"text": state, "image": Image(image.data)} if image else state,
            questions=sdk_questions,
            model=self.model,
            extra_headers={"TypeSafe-Danger-Zone-Images": "true"} if image else None,
        )
        if set(response.answers) != set(questions):
            raise RuntimeError("Jev response does not match the requested questions")
        if any(response.answers[name].type != question.type for name, question in questions.items()):
            raise RuntimeError("Jev response contains an unexpected answer type")
        return DecisionResult.model_validate(
            {
                "model": response.model,
                "answers": {name: answer.model_dump() for name, answer in response.answers.items()},
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }
        )

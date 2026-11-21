from typing import Literal, Protocol

from pydantic import BaseModel, JsonValue

from prawdowid.ai.adapters.media import Media


class BooleanQuestion(BaseModel):
    type: Literal["noul"] = "noul"
    instructions: str


class ChoiceQuestion(BaseModel):
    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str | None]


class ScoreQuestion(BaseModel):
    type: Literal["score"] = "score"
    instructions: str
    criteria: list[str]


class BoxQuestion(BaseModel):
    type: Literal["box"] = "box"
    instructions: str


DecisionQuestion = BooleanQuestion | ChoiceQuestion | ScoreQuestion | BoxQuestion


class BooleanAnswer(BaseModel):
    type: Literal["noul"]
    noul: float


class ChoiceAnswer(BaseModel):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float]
    confidence: float


class ScoreAnswer(BaseModel):
    type: Literal["score"]
    score: float
    confidence: float
    probabilities: dict[int, float]
    legend: dict[int, JsonValue]


class BoxCoordinates(BaseModel):
    top: float
    bottom: float
    left: float
    right: float


class BoxAnswer(BaseModel):
    type: Literal["box"]
    box: BoxCoordinates
    confidence: float | None


class DecisionResult(BaseModel):
    model: str
    answers: dict[str, BooleanAnswer | ChoiceAnswer | ScoreAnswer | BoxAnswer]
    input_tokens: int | None
    output_tokens: int | None


class DecisionAdapter(Protocol):
    def decide(
        self,
        state: str,
        questions: dict[str, DecisionQuestion],
        *,
        image: Media | None = None,
    ) -> DecisionResult: ...

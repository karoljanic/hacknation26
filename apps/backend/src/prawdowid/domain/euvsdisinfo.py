from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator, model_validator

EUvsDisinfoToolName = Literal["cases", "articles", "search", "narratives"]


class EUvsDisinfoConnection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_key: SecretStr
    mode: Literal["stateful", "stateless"] = "stateful"
    auth: Literal["bearer", "x-api-key", "query"] = "bearer"

    @field_validator("api_key")
    @classmethod
    def reject_blank_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("EUvsDisinfo API key must not be empty")
        return value


class EUvsDisinfoPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=10, ge=1, le=100, strict=True)


class EUvsDisinfoDateRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def validate_order(self) -> "EUvsDisinfoDateRange":
        if self.date_from is not None and self.date_to is not None and self.date_from > self.date_to:
            raise ValueError("date_from must not be later than date_to")
        return self


class EUvsDisinfoSearchRequest(EUvsDisinfoDateRange):
    query: str = Field(min_length=3)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("Search query must contain at least 3 non-padding characters")
        return value


class EUvsDisinfoTool(BaseModel):
    name: str
    description: str | None
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue] | None


class EUvsDisinfoToolList(BaseModel):
    tools: list[EUvsDisinfoTool]
    next_cursor: str | None


class EUvsDisinfoResult(BaseModel):
    tool: EUvsDisinfoToolName
    content: list[dict[str, JsonValue]]
    structured_content: dict[str, JsonValue] | None
    fetched_at: datetime


class EUvsDisinfoHealth(BaseModel):
    status: Literal["ok"]
    timestamp: datetime
    auth_required: bool = Field(alias="authRequired")

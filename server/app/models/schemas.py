"""저장 계층과 개인정보·API 모듈이 공유하는 자료형."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from .enums import EndReason
from .llm_results import Rule

PDId = int


class Schema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )


class PDItem(Schema):
    pd_id: PDId | None = None
    text: str
    category: Literal[0, 1, 2]
    allowed: bool = False
    allowed_by: str = "none"


PDTable = list[PDItem]


class RunConfig(Schema):
    call_limit: int = Field(ge=0)
    regen_limit: int = Field(ge=0)
    budget_limit: float = Field(ge=0, allow_inf_nan=False)


class StartedRun(Schema):
    user_message_id: int
    run_id: int


class PrivacySetting(Schema):
    setting_id: int | None = None
    text: str = ""
    rules: list[Rule] = Field(default_factory=list)
    updated_at: str | None = None


class Message(Schema):
    message_id: int
    role: str
    content: str
    created_at: str


class ChatSummary(Schema):
    chat_id: int
    title: str


class RunSummary(Schema):
    run_id: int
    user_message_id: int
    status: Literal["running", "finished"]
    notice: str | None = None


class ChatDetail(ChatSummary):
    messages: list[Message]
    runs: list[RunSummary]


class ConsentItem(Schema):
    pd_id: int
    label: str
    text: str
    category: Literal[0, 1, 2]


class ConsentDecision(Schema):
    pd_id: int
    allow: bool


class ConsentRequest(Schema):
    consent_id: int
    run_id: int
    items: list[ConsentItem]
    purpose: str
    benefit: str
    risk: str
    result: Literal["answered", "cancelled"] | None = None
    response_text: str | None = None
    decisions: list[ConsentDecision] = Field(default_factory=list)
    created_at: str
    responded_at: str | None = None


class QueryItem(Schema):
    pd_id: int
    allowed: bool
    allowed_by: str


class FrontierQueryInput(Schema):
    request: str
    answer: str | None = None
    status: Literal["ok", "failed", "uncertain"] = "uncertain"
    context_mode: Literal["keep", "reset"] | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: float = Field(default=0, ge=0, allow_inf_nan=False)
    elapsed_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class FrontierQuery(FrontierQueryInput):
    query_id: int
    round: int
    created_at: str
    items: list[QueryItem]


class RunDetail(RunSummary):
    chat_id: int
    answer_message_id: int | None
    setting_id: int | None
    config: RunConfig
    end_reason: EndReason | None
    error_code: str | None
    frontier_calls: int
    regenerations: int
    spent_cost: float
    memo_final: str | None
    started_at: str
    ended_at: str | None
    frontier_queries: list[FrontierQuery]
    consents: list[ConsentRequest]


class RunEvent(Schema):
    event_id: int
    run_id: int
    type: str
    payload: JsonValue
    created_at: str

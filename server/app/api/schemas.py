"""API 요청·응답 형식. 코드에서는 snake_case, JSON에서는 명세서의 camelCase로 주고받는다."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """필드 이름을 JSON에서 camelCase(chatId 등)로 바꾸는 공통 부모."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# 공통 자료형


class PDItem(ApiModel):
    """동의 요청에 들어가는 개인정보 항목. category는 0 식별 · 1 민감 · 2 속성."""

    pd_id: int
    label: str
    text: str
    category: Literal[0, 1, 2]


class Decision(ApiModel):
    pd_id: int
    allow: bool


class Rule(ApiModel):
    target: str
    scope: Literal["item", "category"]
    action: Literal["allow", "deny"]


class ErrorBody(ApiModel):
    code: str
    message: str


# API-01 · 02 · 03


class ChatCreated(ApiModel):
    chat_id: int


class ChatSummary(ApiModel):
    chat_id: int
    title: str


class MessageOut(ApiModel):
    message_id: int
    role: Literal["user", "assistant"]
    content: str
    created_at: str


class RunSummary(ApiModel):
    run_id: int
    user_message_id: int
    status: Literal["running", "finished"]
    notice: str | None


class ChatDetail(ApiModel):
    chat_id: int
    title: str
    messages: list[MessageOut]
    runs: list[RunSummary]


# API-05 · 08


class SendBody(ApiModel):
    text: str


class RunStarted(ApiModel):
    run_id: int


class ConsentBody(ApiModel):
    consent_id: int
    text: str


class ConsentReply(ApiModel):
    ok: bool
    decisions: list[Decision] | None = None
    message: str


# API-09 · 10


class PrivacySetting(ApiModel):
    setting_id: int | None
    text: str
    rules: list[Rule]
    updated_at: str | None


class SettingBody(ApiModel):
    text: str


class SettingSaved(ApiModel):
    ok: bool
    rules: list[Rule]
    message: str


# API-11


class FrontierQueryOut(ApiModel):
    query_id: int
    round: int
    request: str
    answer: str | None
    status: Literal["ok", "failed", "uncertain"]


class ConsentOut(ApiModel):
    consent_id: int
    items: list[PDItem]
    purpose: str
    benefit: str
    risk: str
    result: Literal["answered", "cancelled"] | None
    response_text: str | None
    decisions: list[Decision]
    created_at: str
    responded_at: str | None


class RunDetail(ApiModel):
    run_id: int
    chat_id: int
    user_message_id: int
    answer_message_id: int | None
    status: Literal["running", "finished"]
    end_reason: str | None
    notice: str | None
    error_code: str | None
    frontier_queries: list[FrontierQueryOut]
    consents: list[ConsentOut]

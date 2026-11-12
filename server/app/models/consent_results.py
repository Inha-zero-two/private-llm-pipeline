"""동의 안내, 자연어 해석 및 실행 중 응답의 공유 자료형."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ConsentModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        strict=True,
        extra="forbid",
    )


class ConsentTarget(ConsentModel):
    pd_id: int
    label: str
    text: str
    category: Literal[0, 1, 2]


class ConsentDecision(ConsentModel):
    pd_id: int
    allow: bool


class ConsentMessage(ConsentModel):
    """저장 전 안내. consent_id는 request()가 저장 후 채운다."""

    run_id: int | str
    consent_id: int | None = None
    items: list[ConsentTarget]
    purpose: str
    benefit: str
    risk: str


class ConsentInterpretation(ConsentModel):
    """명확한 응답에서 언급된 항목만 반환. 미언급 항목은 서버에서 금지한다."""

    ok: bool
    decisions: list[ConsentDecision]
    message: str = Field(min_length=1)


class ConsentResult(ConsentModel):
    run_id: int | str
    consent_id: int
    decisions: list[ConsentDecision]


class ConsentReply(ConsentModel):
    """API-08 반환값. 실패 시 exclude_none=True로 decisions를 생략한다."""

    ok: bool
    message: str
    decisions: list[ConsentDecision] | None = None

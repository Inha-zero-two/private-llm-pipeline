"""5.2의 작업별 반환 객체. DB ID와 실행 중 허용 상태는 호출 모듈이 붙인다."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Index = Annotated[int, Field(ge=0)]
ContextMode = Literal["keep", "reset"]


class DetectedItem(BaseModel):
    text: str
    category: Annotated[int, Field(ge=0, le=2)]


class Plan(BaseModel):
    # 모델의 camelCase 출력과 Python의 snake_case 필드를 대응한다.
    model_config = ConfigDict(populate_by_name=True)

    enough: bool
    need_consent: bool = Field(alias="needConsent")
    consent_items: list[int] = Field(alias="consentItems")
    need: list[str]


class Rule(BaseModel):
    target: str
    scope: Literal["item", "category"]
    action: Literal["allow", "deny"]


class RuleMatch(BaseModel):
    # 입력 목록의 0부터 시작하는 번호이며 DB의 pdId가 아니다.
    item: Index
    rules: list[Index]


class ConsentText(BaseModel):
    purpose: str
    benefit: str
    risk: str


ParsedResult = (
    str
    | list[DetectedItem]
    | list[str]
    | Plan
    | list[int]
    | list[Rule]
    | list[RuleMatch]
    | ConsentText
)

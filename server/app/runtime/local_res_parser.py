"""설계 5.2: 상태 없는 parser() 하나로 출력 형식을 검사하고 결과를 반환한다."""

import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


# 작업 이름과 반환 타입은 이 파서 모듈에서 함께 정의한다.
class PromptIdx(StrEnum):
    DETECT = "DETECT"
    NEEDS = "NEEDS"
    JUDGE = "JUDGE"
    WRITE = "WRITE"
    REWRITE = "REWRITE"
    GUARD = "GUARD"
    ANSWER = "ANSWER"
    PARSE_RULES = "PARSE_RULES"
    MATCH_RULES = "MATCH_RULES"
    CONSENT = "CONSENT"
    DECIDE_CONTEXT = "DECIDE_CONTEXT"
    FRONTIER = "FRONTIER"
    MEMO_SUMMARY = "MEMO_SUMMARY"


Index = Annotated[int, Field(ge=0)]
ContextMode = Literal["keep", "reset"]


class DetectedItem(BaseModel):
    # DB ID와 실행 중 허용 상태는 호출 모듈이 붙인다.
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


class OutputFormatError(ValueError):
    """모델 출력의 형식 오류. Runner가 이 예외만 잡아 재생성한다."""


# None은 텍스트 작업, 나머지는 검증 자료형과 JSON에서 꺼낼 필드다.
# 검증기는 한 번만 만들고 요청마다 JSON 읽기·검증을 한 번씩 수행한다.
OUTPUT_FORMAT: dict[PromptIdx, tuple[TypeAdapter[ParsedResult], str | None] | None] = {
    PromptIdx.DETECT: (TypeAdapter(list[DetectedItem]), "items"),
    PromptIdx.NEEDS: (TypeAdapter(list[str]), "needs"),
    PromptIdx.JUDGE: (TypeAdapter(Plan), None),
    PromptIdx.GUARD: (TypeAdapter(list[Index]), "hits"),
    PromptIdx.PARSE_RULES: (TypeAdapter(list[Rule]), "rules"),
    PromptIdx.MATCH_RULES: (TypeAdapter(list[RuleMatch]), "matches"),
    PromptIdx.CONSENT: (TypeAdapter(ConsentText), None),
    PromptIdx.DECIDE_CONTEXT: (TypeAdapter(ContextMode), "context"),
    PromptIdx.WRITE: None,
    PromptIdx.REWRITE: None,
    PromptIdx.ANSWER: None,
    PromptIdx.MEMO_SUMMARY: None,
}


def parser(raw_output: str, prompt_idx: PromptIdx | str) -> ParsedResult:
    """모델 원문을 작업별 형식으로 검사하고 텍스트·객체·목록 중 해당 값을 반환한다.

    텍스트 작업은 빈 출력만 검사해 원문을 유지하고, JSON 작업은 바깥 코드 블록을
    제거한 뒤 한 번 읽어 필수 필드와 자료형을 엄격하게 검사한다.
    출력 형식 오류는 OutputFormatError, 알 수 없거나 외부 모델용인 작업은 ValueError다.
    재생성·로그·컨텍스트 누적은 이 함수를 호출하는 Runner가 담당한다.
    """
    # 1. 작업을 확인하고 텍스트 응답은 원문 그대로 반환한다.
    prompt_idx = PromptIdx(prompt_idx)
    if prompt_idx not in OUTPUT_FORMAT:
        # FRONTIER는 외부 모델용 프롬프트다. 설정 오류를 재생성으로 감추지 않는다.
        raise ValueError(f"로컬 파싱 대상이 아닌 작업: {prompt_idx}")
    output_format = OUTPUT_FORMAT[prompt_idx]
    if not raw_output.strip():
        raise OutputFormatError("출력이 비어 있음")
    if output_format is None:
        return raw_output  # 텍스트의 줄바꿈·들여쓰기는 그대로 보존한다.

    # 2. JSON 전체를 감싼 코드 블록만 제거한다. 설명문 속 JSON은 추출하지 않는다.
    source = raw_output.strip()
    # JSON 문자열 안의 U+0085/U+2028/U+2029를 줄바꿈으로 바꾸지 않는다.
    lines = source.split("\n")
    if (
        len(lines) >= 3
        and lines[0].rstrip("\r") in {"```", "```json"}
        and lines[-1] == "```"
    ):
        source = "\n".join(lines[1:-1])

    # 3. JSON을 한 번 읽고 작업별 반환값이 들어 있는 필드를 꺼낸다.
    try:
        data = json.loads(source)
    except (ValueError, RecursionError) as exc:
        # JSON 문법뿐 아니라 정수 자릿수·중첩 제한도 이 입력의 형식 오류다.
        # 이 범위 밖의 코드나 래퍼에서 발생한 ValueError는 처리하지 않는다.
        raise OutputFormatError(str(exc)) from exc
    if not isinstance(data, dict):
        raise OutputFormatError("JSON 최상위 값은 객체여야 함")
    adapter, field = output_format
    if field is not None:
        if field not in data:
            raise OutputFormatError(f"{field} 항목이 없음")
        data = data[field]
    # 4. 작업별 타입으로 검증하고 검증된 값만 반환한다.
    try:
        # '1'·true를 정수로 바꾸는 등의 자동 타입 보정은 하지 않는다.
        return adapter.validate_python(data, strict=True)
    except ValidationError as exc:
        raise OutputFormatError(str(exc)) from exc

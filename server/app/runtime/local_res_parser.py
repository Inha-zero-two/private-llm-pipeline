"""설계 5.2: 모델 출력 형식 검사 및 작업별 결과 반환."""

import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


# 작업 종류와 반환 자료형
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
    # 데이터베이스 ID와 실행 중 허용 상태는 호출자가 정의
    text: str  # 검출한 개인정보 문자열
    # 개인정보 분류 번호(0 식별, 1 민감, 2 속성)
    category: Annotated[int, Field(ge=0, le=2)]


class Plan(BaseModel):
    # 모델 출력의 필드 이름과 파이썬 변수 이름 대응 설정
    model_config = ConfigDict(populate_by_name=True)

    enough: bool  # 답변에 필요한 정보 충족 여부
    need_consent: bool = Field(alias="needConsent")  # 추가 동의 필요 여부
    consent_items: list[int] = Field(alias="consentItems")  # 동의 대상 개인정보 ID 목록
    need: list[str]  # 추가로 필요한 정보 목록


class Rule(BaseModel):
    target: str  # 규칙 적용 대상
    scope: Literal["item", "category"]  # 항목 또는 분류 단위의 규칙 적용 범위
    action: Literal["allow", "deny"]  # 허용·거절 구분


class RuleMatch(BaseModel):
    # 입력 목록에서 0부터 시작하는 항목 번호. 데이터베이스의 개인정보 ID와 구분
    item: Index  # 입력 개인정보 항목 번호
    rules: list[Index]  # 일치하는 규칙 번호 목록


class ConsentText(BaseModel):
    purpose: str  # 개인정보 사용 목적
    benefit: str  # 동의 시 기대 효과
    risk: str  # 동의 시 예상 위험


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
    """출력 형식 오류. Runner에서 재생성을 결정할 때 구분하는 예외 종류."""


# 작업별 출력 검사 설정. None은 텍스트, 나머지는 (자료형 검사 객체, JSON 필드 이름)
# 자료형 검사 객체는 모듈을 불러올 때 한 번 생성
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
    """모델 출력의 형식 검사 후 작업에 맞는 문자열, 객체 또는 목록 반환.

    raw_output: 모델이 생성한 답변. prompt_idx: 답변 형식을 결정할 작업 종류.
    텍스트 작업은 빈 출력만 검사하고 원문 유지. JSON 작업은 코드 블록 표시를 제거한 뒤
    JSON을 한 번 읽고 필수 필드와 자료형 검사. 자동 자료형 변환은 생략.
    형식 오류는 OutputFormatError, 알 수 없거나 외부 모델용인 작업은 ValueError로 전달.
    재생성, 로그 기록 및 이전 대화 저장은 호출자인 Runner에서 처리.
    """
    # 1. 작업 종류 확인 후 텍스트 응답은 원문 그대로 반환
    prompt_idx = PromptIdx(prompt_idx)
    if prompt_idx not in OUTPUT_FORMAT:
        # FRONTIER는 외부 모델용 작업. 잘못된 작업 선택은 재생성 없이 예외 전달
        raise ValueError(f"로컬 파싱 대상이 아닌 작업: {prompt_idx}")
    output_format = OUTPUT_FORMAT[prompt_idx]
    if not raw_output.strip():
        raise OutputFormatError("출력이 비어 있음")
    if output_format is None:
        return raw_output  # 원래 줄바꿈과 들여쓰기를 유지한 답변

    # 2. JSON 전체를 감싼 코드 블록 표시만 제거. 설명문 속 JSON 추출은 생략
    source = raw_output.strip()
    # JSON 문자열에 포함된 유니코드 문자 보존을 위해 실제 줄바꿈 문자로만 분리
    lines = source.split("\n")
    if (
        len(lines) >= 3
        and lines[0].rstrip("\r") in {"```", "```json"}
        and lines[-1] == "```"
    ):
        source = "\n".join(lines[1:-1])

    # 3. JSON을 한 번 읽고 작업에 해당하는 필드의 값 추출
    try:
        data = json.loads(source)
    except (ValueError, RecursionError) as exc:
        # JSON 문법, 정수 자릿수 및 중첩 한도 초과는 출력 형식 오류로 전달
        # 처리 범위는 JSON 읽기에서 발생한 예외로 제한
        raise OutputFormatError(str(exc)) from exc
    if not isinstance(data, dict):
        raise OutputFormatError("JSON 최상위 값은 객체여야 함")
    adapter, field = output_format
    if field is not None:
        if field not in data:
            raise OutputFormatError(f"{field} 항목이 없음")
        data = data[field]
    # 4. 작업별 자료형 검사 후 통과한 값 반환
    try:
        # 문자열 '1'이나 참·거짓 값을 정수로 바꾸는 등의 자동 변환 방지
        return adapter.validate_python(data, strict=True)
    except ValidationError as exc:
        raise OutputFormatError(str(exc)) from exc

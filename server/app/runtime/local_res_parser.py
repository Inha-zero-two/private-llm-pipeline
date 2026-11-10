"""설계 5.2: 모델 출력 형식 검사 및 작업별 결과 반환."""

import json

from pydantic import TypeAdapter, ValidationError

from ..models.enums import PromptIdx
from ..models.llm_results import (
    ConsentText,
    ContextMode,
    DetectedItem,
    Index,
    ParsedResult,
    Rule,
    RuleMatch,
)
from ..pipeline.planner import Plan


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
        result = adapter.validate_python(data, strict=True)
    except ValidationError as exc:
        raise OutputFormatError(str(exc)) from exc
    # Planner의 기본값과 별개로 모델 응답에는 판단 필드 네 개가 모두 필요
    if isinstance(result, Plan) and result.model_fields_set != set(Plan.model_fields):
        raise OutputFormatError("판단 결과의 필수 항목이 없음")
    return result

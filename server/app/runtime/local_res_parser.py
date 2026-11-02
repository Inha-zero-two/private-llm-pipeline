"""설계 5.2: 상태 없는 parser() 하나로 출력 형식을 검사하고 결과를 반환한다."""

import json

from pydantic import TypeAdapter, ValidationError

from app.models.enums import PromptIdx
from app.models.local_llm_outputs import (
    ConsentText,
    ContextMode,
    DetectedItem,
    Index,
    ParsedResult,
    Plan,
    Rule,
    RuleMatch,
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
    """정상이면 결과만 반환하고, JSON·필드·타입 오류는 OutputFormatError로 올린다."""
    prompt_idx = PromptIdx(prompt_idx)
    if prompt_idx not in OUTPUT_FORMAT:
        # FRONTIER는 외부 모델용 프롬프트다. 설정 오류를 재생성으로 감추지 않는다.
        raise ValueError(f"로컬 파싱 대상이 아닌 작업: {prompt_idx}")
    output_format = OUTPUT_FORMAT[prompt_idx]
    if not raw_output.strip():
        raise OutputFormatError("출력이 비어 있음")
    if output_format is None:
        return raw_output  # 텍스트의 줄바꿈·들여쓰기는 그대로 보존한다.

    source = raw_output.strip()
    # JSON 문자열 안의 U+0085/U+2028/U+2029를 줄바꿈으로 바꾸지 않는다.
    lines = source.split("\n")
    if (
        len(lines) >= 3
        and lines[0].rstrip("\r") in {"```", "```json"}
        and lines[-1] == "```"
    ):
        source = "\n".join(lines[1:-1])

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
    try:
        # '1'·true를 정수로 바꾸는 등의 자동 타입 보정은 하지 않는다.
        return adapter.validate_python(data, strict=True)
    except ValidationError as exc:
        raise OutputFormatError(str(exc)) from exc

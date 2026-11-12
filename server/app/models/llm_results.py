"""모델 실행·결과 파싱·호출 모듈에서 공유하는 반환 자료형."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from ..pipeline.planner import Plan
from .consent_results import ConsentInterpretation

Index = Annotated[int, Field(ge=0)]
ContextMode = Literal["keep", "reset"]


class DetectedItem(BaseModel):
    # 데이터베이스 ID와 실행 중 허용 상태는 호출자가 정의
    text: str  # 검출한 개인정보 문자열
    category: Annotated[int, Field(ge=0, le=2)]  # 개인정보 분류(0 식별, 1 민감, 2 속성)


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
    | ConsentInterpretation
)
# 직접 요청한 결과: 답변, 성공 여부, 입력 토큰 수, 출력 토큰 수, 종료 사유
RequestResult = tuple[str, bool, int | None, int | None, str]
# 작업별 요청 결과: 파싱 결과, 성공 여부, 입력 토큰 수, 출력 토큰 수, 종료 사유
ParsedRequestResult = tuple[ParsedResult | None, bool, int | None, int | None, str]

"""모델 실행·결과 파싱·호출 모듈에서 공유하는 반환 자료형."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    rules: list[Index]  # 최종 적용 규칙 하나의 번호 또는 빈 목록. 호출자가 개수 검증


class JudgeResult(BaseModel):
    """JUDGE 모델 출력. need는 이름 대신 [필요 정보]의 0부터 시작하는 번호로 고른다.

    번호가 필요 정보 목록 범위 안인지 확인하고 이름으로 바꾸는 일은 Planner.judge가 한다.
    """

    model_config = ConfigDict(populate_by_name=True, from_attributes=True, strict=True)

    enough: bool  # 지금 메모로 답할 수 있는지
    need_consent: bool = Field(default=False, alias="needConsent")  # 추가 동의 필요
    consent_items: list[int] = Field(default_factory=list, alias="consentItems")
    need_idx: list[Index] = Field(default_factory=list, alias="needIdx")  # 남은 need


class GuardResult(BaseModel):
    # LeakGuard.detect() 결과. Main은 passed로 전송 여부, hits로 재작성 대상을 정함
    passed: bool  # True면 프론티어로 전송 가능, False면 전송하지 않음
    hits: list[int] = Field(default_factory=list)  # 요청문에 드러난 금지 항목의 PDId


class ConsentText(BaseModel):
    purpose: str  # 개인정보 사용 목적
    benefit: str  # 동의 시 기대 효과
    risk: str  # 동의 시 예상 위험


class FrontierReply(BaseModel):
    # FrontierClient.send() 결과. Main은 answer·cost 사용
    answer: str  # 프론티어 답변
    cost: float  # 이번 질의 비용(달러)
    input_tokens: int | None  # 입력 토큰 수. 사용량을 받지 못하면 None
    output_tokens: int | None  # 출력 토큰 수
    elapsed_ms: int  # 재시도를 포함한 소요 시간(ms)
    context_mode: ContextMode  # 이전 프론티어 대화 유지(keep)·초기화(reset)
    model: str  # 호출한 모델 이름


ParsedResult = (
    str
    | list[DetectedItem]
    | list[str]
    | JudgeResult
    | list[int]
    | list[Rule]
    | list[RuleMatch]
    | ConsentText
)
# 직접 요청한 결과: 답변, 성공 여부, 입력 토큰 수, 출력 토큰 수, 종료 사유
RequestResult = tuple[str, bool, int | None, int | None, str]
# 작업별 요청 결과: 파싱 결과, 성공 여부, 입력 토큰 수, 출력 토큰 수, 종료 사유
ParsedRequestResult = tuple[ParsedResult | None, bool, int | None, int | None, str]

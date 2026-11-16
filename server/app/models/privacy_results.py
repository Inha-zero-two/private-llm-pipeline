"""공개 허용 설정 저장 결과. HTTP 계층에서도 같은 필드를 사용한다."""

from pydantic import BaseModel

from app.models.llm_results import Rule


class SaveResult(BaseModel):
    """성공 시 새 규칙, 해석 실패 시 기존 규칙과 안내를 반환한다."""

    ok: bool
    rules: list[Rule]
    message: str

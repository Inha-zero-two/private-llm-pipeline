"""설계 4.1: 질문에서 개인정보 검출."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# 개인정보 분류 번호와 이름
CATEGORY_LABEL: dict[int, str] = {
    0: "식별정보",
    1: "민감정보",
    2: "속성정보",
}

PDId = int


class PDItem(BaseModel):
    """검출한 개인정보 항목과 허용 상태."""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    pd_id: PDId | None = Field(default=None, alias="pdId")
    text: str
    category: int
    allowed: bool = False
    allowed_by: str = Field(default="none", alias="allowedBy")


PDTable = list[PDItem]


class PDDetector:
    """질문에서 개인정보를 찾아내는 클래스."""

    def __init__(self, local_llm_runner: Any = None) -> None:
        """로컬 모델 실행기 저장."""
        self._runner = local_llm_runner

    def detect(
        self,
        run_id: str,
        chat_id: str,
        message_id: str,
        question: str,
        *,
        found_items: list[Any] | None = None,
        ok: bool = True,
    ) -> list[PDItem]:
        """질문에서 개인정보를 찾아 기본 금지 상태(allowed=False)로 반환."""
        # 1. 질문이 비어 있는지 확인
        if not isinstance(question, str) or not question.strip():
            raise ValueError("질문이 비어 있습니다")

        # 2. 로컬 모델에 개인정보 검출 요청 (PR #13 LocalLLMRunner 병합 후 주석 해제)
        # found_items, ok, *_ = self._runner.request(
        #     run_id, question, PromptIdx.DETECT, chat_id
        # )
        # 현재는 함수 인자로 검출 결과와 성공 여부 전달
        if found_items is None and ok:
            raise NotImplementedError(
                "LocalLLMRunner is not connected; pass found_items"
            )
        if not ok or not isinstance(found_items, list):
            raise RuntimeError("개인정보 검출에 실패했습니다")

        # 3. 찾은 항목을 기본 금지 상태(allowed=False)로 묶어 반환
        items: list[PDItem] = []
        for raw in found_items:
            text = raw.get("text") if isinstance(raw, dict) else getattr(raw, "text", None)
            category = (
                raw.get("category")
                if isinstance(raw, dict)
                else getattr(raw, "category", None)
            )
            # 항목 내용이나 분류 번호(0~2)가 잘못됐으면 예외 발생
            if (
                not isinstance(text, str)
                or not text.strip()
                or isinstance(category, bool)
                or category not in CATEGORY_LABEL
            ):
                raise RuntimeError("개인정보 검출 결과 형식이 맞지 않습니다")

            items.append(PDItem(text=text, category=category))

        # 미구현: 검출 항목 DB 저장 및 동작 로그 기록 (ChatManager 구현 후 주석 해제)
        # self._chat_manager.set_pd_items(chat_id, message_id, items)
        # self._chat_manager.log_event(run_id, "detect", {"count": len(items)})
        return items

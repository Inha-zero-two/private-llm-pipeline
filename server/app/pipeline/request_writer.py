"""설계 3.2: 프론티어에 보낼 도움 요청문 생성(write)과 재작성(rewrite)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models.enums import PromptIdx
from app.privacy.pd_detector import CATEGORY_LABEL, PDItem

if TYPE_CHECKING:
    from app.runtime.local_llm_runner import LocalLLMRunner

# 모델이 요청문 앞뒤에 붙이는 머리말. 첫 줄이 이 중 하나로 시작하면 떼어 냄
_LABELS = ("요청문:", "수정한 요청문:", "수정된 요청문:", "Request:")
# 요청문을 감싸는 따옴표 쌍
_QUOTES = (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"))


class RequestWriter:
    """로컬 모델로 도움 요청문을 쓰고, 누출 검사에 걸린 표현을 고치는 클래스.

    결과만으로 전송을 허가하지 않음. 모든 요청문은 Main에서 LeakGuard 검사를 거침.
    """

    def __init__(self, local_llm_runner: LocalLLMRunner | None = None) -> None:
        """로컬 모델 실행기 저장."""
        self._runner = local_llm_runner

    def write(
        self,
        question: str,
        memo: str,
        pd_table: list[Any],
        need: list[str],
        run_id: str = "",
        chat_id: str | None = None,
    ) -> str | None:
        """need의 미해결 항목을 묻는 요청문 작성. 생성에 실패하면 None 반환.

        memo에 이미 답이 있는 항목은 반복하지 않고, 허용된 개인정보는 유지하며
        비허용 개인정보는 빼거나 일반화하도록 모델에 지시.
        None은 Main에서 검사에 걸린 것과 같이 재생성 횟수에 포함.
        """
        # 1. 입력값 확인. 잘못된 호출은 모델 실패와 구분해 예외로 전달
        if not isinstance(question, str) or not question.strip():
            raise ValueError("질문이 비어 있습니다")
        if not isinstance(memo, str):
            raise TypeError("memo는 문자열이어야 합니다")
        items = _to_items(pd_table)
        need_text = _need_text(need)

        # 2. 질문, 필요 정보, 메모, 개인정보 허용 상태를 모델 입력 하나로 합치기
        write_input = (
            f"[질문]\n{question.strip()}\n\n"
            f"[필요 정보]\n{need_text}\n\n"
            f"[메모]\n{memo.strip() or '없음'}\n\n"
            f"[허용된 개인정보]\n{_pd_lines(items, allowed=True)}\n\n"
            f"[금지된 개인정보]\n{_pd_lines(items, allowed=False)}"
        )

        # 3. 로컬 모델로 요청문 생성
        result = self._generate(run_id, write_input, PromptIdx.WRITE, chat_id)
        # 미구현: 요청문 동작 로그 기록 (ChatManager 구현 후 주석 해제)
        # self._chat_manager.log_event(
        #     run_id, "draft", {"kind": "write", "request_text": result}
        # )
        return result

    def rewrite(
        self,
        previous_request: str,
        hits: list[int],
        need: list[str],
        pd_table: list[Any],
        run_id: str = "",
        chat_id: str | None = None,
    ) -> str | None:
        """누출 검사에 걸린 항목의 표현만 고친 요청문 반환. 생성에 실패하면 None 반환.

        hits: LeakGuard가 돌려준 비허용 개인정보 ID 목록.
        need의 질문 내용과 이미 허용된 정보는 바꾸지 않도록 모델에 지시.
        """
        # 1. 입력값 확인
        if not isinstance(previous_request, str) or not previous_request.strip():
            raise ValueError("직전 요청문이 비어 있습니다")
        items = _to_items(pd_table)
        need_text = _need_text(need)

        # 2. 검사에 걸린 ID를 개인정보 항목(원문·종류)으로 바꾸기
        hit_ids = set(hits)
        hit_items = [item for item in items if item.pd_id in hit_ids]
        if not hit_items:
            # 고칠 항목을 알 수 없으면 모델을 부르지 않고 실패로 처리
            return None

        rewrite_input = (
            f"[직전 요청문]\n{previous_request.strip()}\n\n"
            f"[고칠 개인정보]\n{_pd_lines(hit_items, allowed=False)}\n\n"
            f"[필요 정보]\n{need_text}\n\n"
            f"[허용된 개인정보]\n{_pd_lines(items, allowed=True)}"
        )

        # 3. 로컬 모델로 요청문 재작성
        result = self._generate(run_id, rewrite_input, PromptIdx.REWRITE, chat_id)
        # 미구현: 요청문 동작 로그 기록 (ChatManager 구현 후 주석 해제)
        # self._chat_manager.log_event(
        #     run_id, "draft", {"kind": "rewrite", "request_text": result}
        # )
        return result

    def _generate(
        self, run_id: str, input_: str, prompt_idx: PromptIdx, chat_id: str | None
    ) -> str | None:
        """Runner로 텍스트를 생성하고 정리. 실패·중지·빈 답변은 None."""
        if self._runner is None:
            return None
        output, ok, *_ = self._runner.request(run_id, input_, prompt_idx, chat_id)
        if not ok or not isinstance(output, str):
            return None
        return _clean(output)


def _to_items(pd_table: list[Any]) -> list[PDItem]:
    """개인정보 테이블을 PDItem 목록으로 변환. 형식이 틀리면 예외 발생."""
    if not isinstance(pd_table, list):
        raise TypeError("pd_table은 목록이어야 합니다")
    try:
        return [PDItem.model_validate(item) for item in pd_table]
    except ValueError as exc:
        raise RuntimeError("개인정보 테이블 형식이 맞지 않습니다") from exc


def _need_text(need: list[str]) -> str:
    """need 목록을 줄 목록 문자열로 변환. 비어 있으면 예외 발생."""
    if not isinstance(need, list):
        raise TypeError("need는 목록이어야 합니다")
    lines = [f"- {n.strip()}" for n in need if isinstance(n, str) and n.strip()]
    if not lines:
        raise ValueError("물어볼 정보(need)가 비어 있습니다")
    return "\n".join(lines)


def _pd_lines(items: list[PDItem], *, allowed: bool) -> str:
    """허용 또는 금지 항목만 골라 '- 원문 (종류)' 줄로 변환. 없으면 '없음'."""
    lines = [
        f"- {item.text} ({CATEGORY_LABEL.get(item.category, '개인정보')})"
        for item in items
        if item.allowed is allowed
    ]
    return "\n".join(lines) if lines else "없음"


def _clean(output: str) -> str | None:
    """모델이 덧붙인 코드 블록·머리말·따옴표를 떼어 낸 요청문 본문. 비면 None."""
    text = output.strip()
    # 코드 블록 표시 제거
    if text.startswith("```") and text.endswith("```") and len(text) >= 6:
        text = text[3:-3]
        first, _, rest = text.partition("\n")
        # ```text 처럼 언어 이름이 붙은 첫 줄 제거
        text = rest if first.strip().isalpha() and rest.strip() else text
        text = text.strip()
    # 머리말 제거
    for label in _LABELS:
        if text.startswith(label):
            text = text[len(label) :].strip()
            break
    # 전체를 감싼 따옴표 제거
    for left, right in _QUOTES:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            text = text[len(left) : -len(right)].strip()
            break
    return text or None

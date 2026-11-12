"""설계 3.3: 도움 요청문의 비허용 개인정보 누출 검사."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.models.enums import PromptIdx
from app.models.llm_results import GuardResult
from app.privacy.pd_detector import CATEGORY_LABEL, PDItem

if TYPE_CHECKING:
    from app.runtime.local_llm_runner import LocalLLMRunner


class LeakGuard:
    """요청문에 허용되지 않은 개인정보가 드러났는지 로컬 모델로 판단하는 클래스.

    통과(passed=True)한 요청문만 Main이 FrontierClient로 보냄.
    """

    def __init__(self, local_llm_runner: LocalLLMRunner | None = None) -> None:
        """로컬 모델 실행기 저장."""
        self._runner = local_llm_runner

    def detect(
        self,
        run_id: str,
        request_text: str,
        pd_table: list[Any],
        chat_id: str | None = None,
    ) -> GuardResult:
        """요청문에 금지 개인정보가 드러났는지 검사.

        검사 대상(비허용 항목)이 없으면 모델 없이 통과.
        모델 실패·형식 오류·중지는 passed=False, hits=[] (무엇을 고칠지 모름).
        """
        # 1. 입력값 확인
        if not isinstance(request_text, str) or not request_text.strip():
            raise ValueError("검사할 요청문이 비어 있습니다")
        if not isinstance(pd_table, list):
            raise TypeError("pd_table은 목록이어야 합니다")
        try:
            items = [PDItem.model_validate(item) for item in pd_table]
        except ValueError as exc:
            raise RuntimeError("개인정보 테이블 형식이 맞지 않습니다") from exc

        # 2. 허용되지 않은 항목만 검사 대상. 이 순서가 모델에 보이는 번호
        target_items = [item for item in items if not item.allowed]
        if not target_items:
            return GuardResult(passed=True, hits=[])

        # 3. 번호 붙은 금지 목록 + 요청문으로 모델 호출. 이전 대화 없이 매번 새로 판단
        guard_input = self.build_guard_input(request_text, target_items)
        hit_idx: Any = None
        ok = False
        if self._runner is not None:
            hit_idx, ok, *_ = self._runner.request(
                run_id, guard_input, PromptIdx.GUARD, chat_id
            )

        # 4. 형식을 끝내 못 맞췄으면 실패. 고칠 항목을 모르므로 hits는 빈 목록
        if not ok or not isinstance(hit_idx, list):
            result = GuardResult(passed=False, hits=[])
        else:
            result = self.to_guard_result(hit_idx, target_items)

        # 미구현: 검사 동작 로그 기록 (ChatManager 구현 후 주석 해제)
        # self._chat_manager.log_event(
        #     run_id,
        #     "guard",
        #     {"request_text": request_text, "hits": result.hits, "passed": result.passed},
        # )
        return result

    def build_guard_input(self, request_text: str, target_items: list[PDItem]) -> str:
        """금지 항목에 0부터 번호와 종류를 붙인 목록을 만들고 요청문과 합침."""
        lines = [
            f"{idx}.[{CATEGORY_LABEL.get(item.category, '개인정보')}] {item.text}"
            for idx, item in enumerate(target_items)
        ]
        return (
            "[금지 목록]\n" + "\n".join(lines) + f"\n\n[요청문]\n{request_text.strip()}"
        )

    def to_guard_result(
        self, hit_idx: list[int], target_items: list[PDItem]
    ) -> GuardResult:
        """모델이 돌려준 번호를 PDId로 바꿈. hits가 비었을 때만 통과.

        설계 보완: 번호를 돌려줬는데 모두 범위 밖이거나 ID가 없는 항목이면
        누출 여부를 확신할 수 없으므로 통과시키지 않고 형식 실패와 같이 처리.
        """
        hits: list[int] = []
        flagged = False  # 모델이 무언가 걸렸다고 답했는지
        for idx in hit_idx:
            if isinstance(idx, bool) or not isinstance(idx, int):
                continue
            flagged = True
            if 0 <= idx < len(target_items):
                pd_id = target_items[idx].pd_id
                if pd_id is None:
                    # 걸린 항목을 ID로 가리킬 수 없으면 고칠 수 없으므로 실패
                    return GuardResult(passed=False, hits=[])
                if pd_id not in hits:
                    hits.append(pd_id)

        if hits:
            return GuardResult(passed=False, hits=hits)
        if flagged:
            # 번호는 돌려줬지만 쓸 수 있는 번호가 없음 → 통과시키지 않음
            return GuardResult(passed=False, hits=[])
        return GuardResult(passed=True, hits=[])

"""파이프라인 실행 중 질문 맥락과 답변을 보관하는 메모 저장소."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..runtime.local_llm_runner import LocalLLMRunner
    from .prompt_manager import PromptManager

COMPACTION_REMAINING_TOKENS = 5_000  # 남은 입력 공간이 이 값 이하이면 미리 압축한다.
SUMMARY_MAX_TOKENS = 5_000  # 요약 요청의 생성 상한. 메모 저장 한도와는 별개다.


@dataclass
class Memo:
    """한 번의 실행에서 질문 맥락, 필요한 정보 목록, 수집한 답변을 보관한다."""

    question: str = ""  # 사용자 질문 원문
    # 답변이 도착해도 삭제하지 않는 전체 필요 정보 목록.
    needs: list[str] = field(default_factory=list)
    # need별로 기존 요약과 이후 수집한 답변을 함께 보관한다.
    answers: dict[str, list[str]] = field(default_factory=dict)


class MemoManager:
    """현재 파이프라인 실행의 메모를 메모리에서 관리한다."""

    def __init__(
        self,
        runner: LocalLLMRunner,
        prompt_manager: PromptManager,
        max_tokens: int | None = None,
    ) -> None:
        """공유 Runner와 PromptManager를 받아 등록된 요약 프롬프트로 메모 한도를 정한다.

        max_tokens가 None이면 Runner의 입력 한도를 사용한다.
        직접 지정한 값도 Runner 한도를 넘길 수 없다.
        """
        self._runner = runner
        self._prompt_manager = prompt_manager
        self._memo: Memo | None = None
        self._run_id: str | None = None
        # 현재 호출부의 (전체 입력 한도, 메모 앞 문자열, 메모 뒤 문자열).
        # prepare_for_request()에서 설정하며 새 실행마다 초기화한다.
        self._request_budget: tuple[int, str, str] | None = None
        input_limit = runner.get_input_limit(
            prompt_manager.get_prompt("MEMO_SUMMARY"), max_tokens=SUMMARY_MAX_TOKENS
        )
        self._max_tokens = (
            input_limit if max_tokens is None else min(max_tokens, input_limit)
        )
        if self._max_tokens <= 0:
            raise ValueError("메모 토큰 한도는 양수여야 합니다")

    def start(self, run_id: str, question: str) -> None:
        """실행 ID를 설정하고 질문을 담은 새 메모로 실행을 시작한다."""
        self._run_id = run_id
        self._memo = Memo(question=question)
        self._request_budget = None

    def set_needs(self, needs: list[str]) -> None:
        """실행 초기의 전체 필요 정보 목록을 등록한다.

        매 왕복에서 선택한 plan.need로 호출하는 함수가 아니다.
        재호출하면 유지되는 need의 답변은 보존하고, 제외된 need의 답변은 제거한다.
        """
        memo = self._require_memo()
        memo.needs = list(dict.fromkeys(needs))
        memo.answers = {need: memo.answers.get(need, []) for need in memo.needs}

    def add_answer(self, needs: list[str], answer: str) -> None:
        """이번 질문의 대상 need들에 답변을 연결한다.

        needs는 전체 목록에 등록된 문자열이어야 하며, 보통 Main이 plan.need를 전달한다.
        한 응답이 여러 need를 다루면 각 묶음에 저장한다. 추가 후 예상 크기를 먼저
        검사하고 기존 묶음을 압축한 뒤 재검사하며, 여전히 초과하면 새 답변은 저장하지 않는다.
        """
        memo = self._require_memo()
        targets = list(dict.fromkeys(needs))
        if not targets or any(need not in memo.answers for need in targets):
            raise ValueError("답변을 연결할 need는 등록된 필요 정보여야 합니다")

        # 새 답변은 아직 실제 메모에 넣지 않는다.
        candidate = self._with_answer(targets, answer)
        remaining_tokens = self._remaining_tokens(self._format_memo(candidate))
        if remaining_tokens <= COMPACTION_REMAINING_TOKENS:
            self.compact_answer()
            # 압축으로 기존 묶음이 바뀌었으므로 추가 후보를 다시 만든다.
            candidate = self._with_answer(targets, answer)
            remaining_tokens = self._remaining_tokens(self._format_memo(candidate))
        if remaining_tokens < 0:
            raise ValueError("압축 후에도 새 답변이 메모 토큰 한도를 초과합니다")

        memo.answers = candidate

    def _with_answer(self, needs: list[str], answer: str) -> dict[str, list[str]]:
        """기존 메모를 바꾸지 않고 지정한 need들에 답변을 추가한 후보를 만든다."""
        return {
            need: [*items, answer] if need in needs else list(items)
            for need, items in self._require_memo().answers.items()
        }

    def compact_answer(self) -> None:
        """need마다 누적 답변들을 한 번에 요약하고 해당 묶음만 교체한다.

        기존 요약과 이후에 쌓인 답변도 함께 입력한다. 실패하거나 더 길어진 묶음은
        원문을 유지한다. 중지되면 이번 압축에서 만든 변경을 모두 반영하지 않는다.
        """
        memo = self._require_memo()
        # 처리 중에는 복사본만 바꾸고, 끝까지 확인한 뒤 한 번에 반영한다.
        answers = {need: list(items) for need, items in memo.answers.items()}
        for need, items in memo.answers.items():
            if not items:
                continue
            summary, reason = self._summarize(need, items)
            if reason == "stopped":
                return
            if summary is not None and self._count_tokens(
                self._format_need(need, [summary])
            ) < self._count_tokens(self._format_need(need, items)):
                answers[need] = [summary]

        if self._count_tokens(self._format_memo(answers)) < self._count_tokens(
            self.read_all()
        ):
            memo.answers = answers

    def read_all(self) -> str:
        """질문, 필요한 정보 목록, 답변을 읽기 쉬운 하나의 문자열로 반환한다."""
        return self._format_memo(self._require_memo().answers)

    def prepare_for_request(
        self,
        system_prompt: str,
        *,
        prefix: str = "",
        suffix: str = "",
        max_output_tokens: int = SUMMARY_MAX_TOKENS,
    ) -> str:
        """Runner의 실제 입력 예산으로 미리 압축한 뒤 전체 요청 문자열을 반환한다.

        호출부는 메모 앞뒤에 붙일 질문·개인정보·구분 문구를 그대로 전달한다.
        이 예산은 이후 add_answer에도 적용하며, 다음 호출에서 갱신할 수 있다.
        질문이나 개인정보 등 부가 입력이 바뀌면 다시 호출해야 한다.
        반환값은 prefix + 메모 + suffix이며, 부가 입력은 요약 모델에 전달하지 않는다.
        """
        self._require_memo()
        limit = self._runner.get_input_limit(
            system_prompt, max_tokens=max_output_tokens
        )
        self._request_budget = (limit, prefix, suffix)
        if self._remaining_tokens(self.read_all()) <= COMPACTION_REMAINING_TOKENS:
            self.compact_answer()
        memo_text = self.read_all()
        if self._remaining_tokens(memo_text) < 0:
            raise ValueError("압축 후에도 전체 요청이 모델 입력 한도를 초과합니다")
        return prefix + memo_text + suffix

    def _remaining_tokens(self, memo_text: str) -> int:
        """메모 자체와 현재 요청 전체 중 더 적게 남은 입력 공간을 반환한다."""
        remaining = self._max_tokens - self._count_tokens(memo_text)
        if self._request_budget is not None:
            limit, prefix, suffix = self._request_budget
            remaining = min(
                remaining, limit - self._count_tokens(prefix + memo_text + suffix)
            )
        return remaining

    def _format_memo(self, answers: dict[str, list[str]]) -> str:
        """저장된 메모와 추가·요약 후보에 같은 문자열 형식을 적용한다."""
        memo = self._require_memo()
        needs = "\n".join(f"- {need}" for need in memo.needs)
        answer_text = "\n\n".join(
            self._format_need(need, items) for need, items in answers.items()
        )
        return f"Question: {memo.question}\nNeeds:\n{needs}\nAnswers:\n{answer_text}"

    @staticmethod
    def _format_need(need: str, answers: list[str]) -> str:
        """need 하나와 그 항목에 수집한 답변들을 독립적인 입력으로 묶는다."""
        original = "\n".join(
            f"[답변 {index}]\n{answer}" for index, answer in enumerate(answers, 1)
        )
        return f"Need:\n{need}\n원문:\n{original}"

    def clear(self) -> None:
        """현재 실행 ID와 메모를 초기화한다."""
        self._run_id = None
        self._memo = None
        self._request_budget = None

    def _count_tokens(self, text: str) -> int:
        """PR #13 Runner의 모델 토크나이저로 메모 크기를 계산한다."""
        return len(self._runner.tokenize(text))

    def _summarize(self, need: str, answers: list[str]) -> tuple[str | None, str]:
        """need와 누적 답변만 전달하며 이전 대화를 조회하거나 저장하지 않는다."""
        memo_text = self._format_need(need, answers)
        summary, success, _, _, reason = self._runner.request(
            self._run_id,
            memo_text,
            "MEMO_SUMMARY",
            None,
            use_context=False,
            system_prompt=self._prompt_manager.get_prompt("MEMO_SUMMARY"),
            prompt_version=self._prompt_manager.get_version(),
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        return (summary if success and isinstance(summary, str) else None), reason

    def _require_memo(self) -> Memo:
        """현재 메모를 반환하고, 메모가 시작되지 않았다면 RuntimeError를 발생시킨다."""
        if self._memo is None:
            raise RuntimeError("메모가 시작되지 않았습니다")
        return self._memo

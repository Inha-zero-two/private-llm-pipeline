"""파이프라인 실행 중 질문 맥락과 답변을 보관하는 메모 저장소."""

from __future__ import annotations

from dataclasses import dataclass, field


# TODO: 이 여유 토큰 수를 PR #13의 최종 토큰 제한 정책에 맞춰 조정한다.
COMPACTION_REMAINING_TOKENS = 5_000


@dataclass
class Memo:
    """한 번의 실행에서 질문 맥락, 필요한 정보 목록, 수집한 답변을 보관한다."""

    question: str = ""
    needs: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)


class MemoManager:
    """현재 파이프라인 실행의 메모를 메모리에서 관리한다."""

    def __init__(self, max_tokens: int | None = None) -> None:
        """메모와 실행 ID를 비어 있는 상태로 초기화하고 토큰 제한을 저장한다."""
        self._memo: Memo | None = None
        self._run_id: str | None = None
        # None은 외부 토큰 제한 설정이 아직 연결되지 않았음을 뜻한다.
        self._max_tokens = max_tokens

    def start(self, run_id: str, question: str) -> None:
        """실행 ID를 설정하고 질문을 담은 새 메모로 실행을 시작한다."""
        self._run_id = run_id
        self._memo = Memo(question=question)

    def set_needs(self, needs: list[str]) -> None:
        """현재 메모의 필요한 정보 목록을 전달받은 목록의 복사본으로 교체한다."""
        self._require_memo().needs = list(needs)

    def add_answer(self, answer: str) -> None:
        """토큰 제한이 설정된 경우 남은 토큰이 임계값 이하이면 압축을 시도한 뒤 새 답변을 추가한다."""
        memo = self._require_memo()

        # TODO: _count_tokens를 공용 str -> int 토큰 계산 함수로 교체한다.
        if self._max_tokens is not None:
            current_tokens = self._count_tokens(self.read_all())
            remaining_tokens = self._max_tokens - current_tokens
            if remaining_tokens <= COMPACTION_REMAINING_TOKENS:
                self.compact_answer()

        memo.answers.append(answer)

    def compact_answer(self) -> None:
        """메모 전체의 요약을 요청하고 결과가 None이 아니면 답변 목록을 요약 하나로 교체한다."""
        memo = self._require_memo()
        memo_text = self.read_all()

        summary = self._summarize(memo_text)

        if summary is not None:
            memo.answers = [summary]

    def read_all(self) -> str:
        """질문, 필요한 정보 목록, 답변을 읽기 쉬운 하나의 문자열로 반환한다."""
        memo = self._require_memo()
        needs = "\n".join(f"- {need}" for need in memo.needs)
        answers = "\n".join(f"- {answer}" for answer in memo.answers)
        return f"Question: {memo.question}\nNeeds:\n{needs}\nAnswers:\n{answers}"

    def clear(self) -> None:
        """현재 실행 ID와 메모를 초기화한다."""
        self._run_id = None
        self._memo = None

    def _count_tokens(self, text: str) -> int:
        """공용 토큰 계산 함수의 연동 지점으로, 현재는 입력과 관계없이 0을 반환한다."""
        # TODO: 향후 토큰 계산 함수를 여기에 연결한다. 토크나이저를 직접 구현하지 않는다.
        del text
        return 0

    def _summarize(self, memo_text: str) -> str | None:
        """로컬 LLM을 통한 메모 요약의 연동 지점으로, 현재는 None을 반환한다."""
        # TODO: PromptIdx.MEMO_SUMMARY를 사용해 프롬프트를 구성한다.
        # TODO: memo_text를 LocalLLMRunner에 전달하고 생성된 요약을 반환한다.
        del memo_text
        return None

    def _require_memo(self) -> Memo:
        """현재 메모를 반환하고, 메모가 시작되지 않았다면 RuntimeError를 발생시킨다."""
        if self._memo is None:
            raise RuntimeError("memo has not been started")
        return self._memo

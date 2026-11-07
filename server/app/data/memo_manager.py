"""Memo storage used while a pipeline run is being assembled."""

from __future__ import annotations

from dataclasses import dataclass, field


# TODO: reconcile this buffer with the final PR #13 token-limit policy.
COMPACTION_REMAINING_TOKENS = 5_000


@dataclass
class Memo:
    """The question context and answers collected for one run."""

    question: str = ""
    needs: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)


class MemoManager:
    """Manage the in-memory memo for the current pipeline run."""

    def __init__(self, max_tokens: int | None = None) -> None:
        self._memo: Memo | None = None
        self._run_id: str | None = None
        # None means the external token-limit configuration is not connected yet.
        self._max_tokens = max_tokens

    def start(self, run_id: str, question: str) -> None:
        """Start a run with a new, empty memo."""
        self._run_id = run_id
        self._memo = Memo(question=question)

    def set_needs(self, needs: list[str]) -> None:
        """Replace the information needs for the current memo."""
        self._require_memo().needs = list(needs)

    def add_answer(self, answer: str) -> None:
        """Compact near the token limit, then append the new answer."""
        memo = self._require_memo()

        # TODO: replace _count_tokens with the shared str -> int token counter.
        if self._max_tokens is not None:
            current_tokens = self._count_tokens(self.read_all())
            remaining_tokens = self._max_tokens - current_tokens
            if remaining_tokens <= COMPACTION_REMAINING_TOKENS:
                self.compact_answer()

        memo.answers.append(answer)

    def compact_answer(self) -> None:
        """Summarize all memo content and keep the summary as its only answer."""
        memo = self._require_memo()
        memo_text = self.read_all()

        summary = self._summarize(memo_text)

        if summary is not None:
            memo.answers = [summary]

    def read_all(self) -> str:
        """Return the question, needs, and answers in one readable string."""
        memo = self._require_memo()
        needs = "\n".join(f"- {need}" for need in memo.needs)
        answers = "\n".join(f"- {answer}" for answer in memo.answers)
        return f"Question: {memo.question}\nNeeds:\n{needs}\nAnswers:\n{answers}"

    def clear(self) -> None:
        """Remove the current run and memo."""
        self._run_id = None
        self._memo = None

    def _count_tokens(self, text: str) -> int:
        """Return the memo token count once the shared counter is available."""
        # TODO: connect the future token counter here; do not implement a tokenizer.
        del text
        return 0

    def _summarize(self, memo_text: str) -> str | None:
        """Return a local summary once the local LLM modules are available."""
        # TODO: build the prompt with PromptIdx.MEMO_SUMMARY.
        # TODO: pass memo_text to LocalLLMRunner and return its summary.
        del memo_text
        return None

    def _require_memo(self) -> Memo:
        if self._memo is None:
            raise RuntimeError("memo has not been started")
        return self._memo

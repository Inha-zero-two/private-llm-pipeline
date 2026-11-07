"""Tests for memo lifecycle and answer collection."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from server.app.data.memo_manager import (
    COMPACTION_REMAINING_TOKENS,
    MemoManager,
)


def test_start_initializes_a_new_memo() -> None:
    manager = MemoManager()

    manager.start("run-1", "How does this work?")

    assert manager.read_all() == (
        "Question: How does this work?\nNeeds:\n\nAnswers:\n"
    )


def test_set_needs_and_add_answer_are_reflected_in_read_all() -> None:
    manager = MemoManager()
    manager.start("run-1", "Question")

    needs = ["first source", "second source"]
    manager.set_needs(needs)
    manager.add_answer("first answer")
    manager.add_answer("second answer")

    assert manager.read_all() == (
        "Question: Question\n"
        "Needs:\n"
        "- first source\n"
        "- second source\n"
        "Answers:\n"
        "- first answer\n"
        "- second answer"
    )
    needs.append("must not alias")
    assert "must not alias" not in manager.read_all()


def test_add_answer_compacts_when_remaining_tokens_reach_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoManager(max_tokens=10_000)
    manager.start("run-1", "Question")
    compact = Mock(wraps=manager.compact_answer)
    monkeypatch.setattr(manager, "compact_answer", compact)
    monkeypatch.setattr(
        manager,
        "_count_tokens",
        lambda text: 10_000 - COMPACTION_REMAINING_TOKENS,
    )

    manager.add_answer("answer")

    compact.assert_called_once_with()


def test_add_answer_compacts_before_appending_new_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoManager(max_tokens=10_000)
    manager.start("run-1", "Question")
    order: list[str] = []

    class TrackingAnswers(list[str]):
        def append(self, answer: str) -> None:
            order.append("append")
            super().append(answer)

    assert manager._memo is not None
    manager._memo.answers = TrackingAnswers(["existing answer"])

    def compact() -> None:
        order.append("compact")

    monkeypatch.setattr(manager, "compact_answer", compact)
    monkeypatch.setattr(
        manager,
        "_count_tokens",
        lambda text: 10_000 - COMPACTION_REMAINING_TOKENS,
    )

    manager.add_answer("new answer")

    assert order == ["compact", "append"]
    assert manager._memo is not None
    assert manager._memo.answers == ["existing answer", "new answer"]


def test_compact_answer_keeps_answers_when_runner_is_not_available() -> None:
    manager = MemoManager()
    manager.start("run-1", "Question")
    manager.add_answer("answer")

    manager.compact_answer()

    assert "- answer" in manager.read_all()


def test_compact_answer_replaces_answers_with_a_successful_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MemoManager()
    manager.start("run-1", "Question")
    manager.add_answer("first answer")
    manager.add_answer("second answer")
    monkeypatch.setattr(manager, "_summarize", lambda memo_text: "summary")

    manager.compact_answer()

    assert manager.read_all().endswith("Answers:\n- summary")


def test_clear_removes_the_current_memo() -> None:
    manager = MemoManager()
    manager.start("run-1", "Question")

    manager.clear()

    with pytest.raises(RuntimeError, match="memo has not been started"):
        manager.read_all()


def test_operations_require_start() -> None:
    manager = MemoManager()

    with pytest.raises(RuntimeError, match="memo has not been started"):
        manager.set_needs(["need"])
    with pytest.raises(RuntimeError, match="memo has not been started"):
        manager.add_answer("answer")

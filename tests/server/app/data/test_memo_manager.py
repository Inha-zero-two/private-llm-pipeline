"""need별 답변 연결, 독립적인 요약 호출 및 메모 한도 검증."""

from __future__ import annotations

from unittest.mock import Mock, call

import pytest

from server.app.data import memo_manager as memo_module
from server.app.data.memo_manager import (
    COMPACTION_REMAINING_TOKENS,
    SUMMARY_MAX_TOKENS,
    MemoManager,
)


@pytest.fixture
def prompt_manager() -> Mock:
    """요약 프롬프트와 버전을 제공하는 공용 관리자 대역."""
    prompt_manager = Mock()
    prompt_manager.get_prompt.return_value = "테스트용 요약 프롬프트"
    prompt_manager.get_version.return_value = "test-v1"
    return prompt_manager


@pytest.fixture
def runner() -> Mock:
    """모델 없이 토큰 수와 요약 결과를 제어한다."""
    runner = Mock()
    runner.tokenize.side_effect = str.split
    runner.get_input_limit.return_value = 26_000
    runner.request.return_value = ("summary", True, 10, 1, "stop")
    return runner


@pytest.fixture
def manager(runner: Mock, prompt_manager: Mock) -> MemoManager:
    """두 개의 필요 정보가 등록된 메모를 만든다."""
    manager = MemoManager(runner, prompt_manager)
    manager.start("run-1", "Question")
    manager.set_needs(["need-a", "need-b"])
    return manager


def require_reduction(manager: MemoManager, tokens: int) -> None:
    """현재 메모에서 지정한 만큼 줄어야 임계값 아래가 되도록 시험 한도를 정한다."""
    current = len(manager.read_all().split())
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + current - tokens + 1


def test_start_initializes_a_new_memo(runner: Mock, prompt_manager: Mock) -> None:
    """새 실행은 이전 질문, 필요 정보와 답변을 모두 초기화한다."""
    manager = MemoManager(runner, prompt_manager)
    manager.start("old-run", "Old question")
    manager.set_needs(["old-need"])
    manager.add_answer(["old-need"], "old answer")

    manager.start("run-1", "Question")

    assert manager.read_all() == "Question: Question\nNeeds:\n\nAnswers:\n"


def test_set_needs_copies_list_and_keeps_matching_answers(manager: MemoManager) -> None:
    """need 목록 교체 시 유지되는 항목의 답변을 보존하고 새 항목을 준비한다."""
    manager.add_answer(["need-a"], "existing answer")
    needs = ["need-a", "need-c", "need-a"]

    manager.set_needs(needs)
    needs.append("not registered")

    memo = manager._require_memo()
    assert memo.needs == ["need-a", "need-c"]
    assert memo.answers == {"need-a": ["existing answer"], "need-c": []}


def test_answers_are_collected_and_rendered_by_need(manager: MemoManager) -> None:
    """같은 need의 여러 답변과 다른 need의 답변을 분리해 저장·조회한다."""
    manager.add_answer(["need-a"], "first answer")
    manager.add_answer(["need-b"], "other answer")
    manager.add_answer(["need-a"], "second answer")

    assert manager._require_memo().answers == {
        "need-a": ["first answer", "second answer"],
        "need-b": ["other answer"],
    }
    assert manager.read_all() == (
        "Question: Question\nNeeds:\n- need-a\n- need-b\nAnswers:\n"
        "Need:\nneed-a\n원문:\n[답변 1]\nfirst answer\n[답변 2]\nsecond answer\n\n"
        "Need:\nneed-b\n원문:\n[답변 1]\nother answer"
    )


def test_one_reply_can_be_linked_to_multiple_needs(manager: MemoManager) -> None:
    """여러 need를 함께 물은 응답은 각 묶음에 한 번씩 연결한다."""
    manager.add_answer(["need-a", "need-b", "need-a"], "shared answer")

    assert manager._require_memo().answers == {
        "need-a": ["shared answer"],
        "need-b": ["shared answer"],
    }


@pytest.mark.parametrize("needs", [[], ["unknown"], ["need-a", "unknown"]])
def test_invalid_answer_targets_do_not_modify_memo(
    manager: MemoManager, needs: list[str]
) -> None:
    """등록되지 않은 연결 대상은 일부 항목에도 답변을 넣지 않고 거부한다."""
    original = manager.read_all()

    with pytest.raises(ValueError, match="등록된 필요 정보"):
        manager.add_answer(needs, "answer")

    assert manager.read_all() == original


def test_compaction_combines_answers_per_need_without_context(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """need마다 모든 원문을 한 요청에 넣고 이전 대화 없이 문자열 요약을 받는다."""
    manager.add_answer(["need-a"], "first long answer")
    manager.add_answer(["need-a"], "second long answer")
    manager.add_answer(["need-b"], "other long answer")
    runner.request.side_effect = [
        ("summary-a", True, 10, 1, "stop"),
        ("summary-b", True, 10, 1, "stop"),
    ]

    require_reduction(manager, 8)
    manager.compact_answer()

    assert manager._require_memo().answers == {
        "need-a": ["summary-a"],
        "need-b": ["summary-b"],
    }
    assert manager._require_memo().question == "Question"
    assert manager._require_memo().needs == ["need-a", "need-b"]
    assert runner.request.call_args_list == [
        call(
            "run-1",
            text,
            "MEMO_SUMMARY",
            None,
            use_context=False,
            system_prompt=prompt_manager.get_prompt.return_value,
            prompt_version=prompt_manager.get_version.return_value,
            max_tokens=SUMMARY_MAX_TOKENS,
        )
        for text in [
            "Need:\nneed-a\n원문:\n[답변 1]\nfirst long answer\n[답변 2]\nsecond long answer",
            "Need:\nneed-b\n원문:\n[답변 1]\nother long answer",
        ]
    ]


def test_new_reply_is_summarized_with_previous_summary(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """이전 요약과 새 답변을 같은 need의 다음 압축 입력으로 전달한다."""
    manager.add_answer(["need-a"], "first detailed source")
    manager.add_answer(["need-a"], "second detailed source")
    runner.request.return_value = ("previous summary", True, 10, 2, "stop")
    require_reduction(manager, 3)
    manager.compact_answer()
    runner.request.reset_mock()
    runner.request.return_value = ("updated", True, 10, 1, "stop")
    manager.add_answer(["need-a"], "new detailed source")

    manager.compact_answer()

    assert runner.request.call_count == 1
    assert runner.request.call_args.args[1] == (
        "Need:\nneed-a\n원문:\n[답변 1]\nprevious summary\n[답변 2]\nnew detailed source"
    )
    assert manager._require_memo().answers == {"need-a": ["updated"], "need-b": []}


@pytest.mark.parametrize("reason", ["parse_failed", "length_failed"])
def test_failed_group_is_preserved_while_other_group_shrinks(
    manager: MemoManager, runner: Mock, prompt_manager: Mock, reason: str
) -> None:
    """요약 실패는 해당 need의 원문만 유지하며 다른 묶음과 섞지 않는다."""
    manager.add_answer(["need-a"], "first detailed source")
    manager.add_answer(["need-a"], "second detailed source")
    manager.add_answer(["need-b"], "other detailed source")
    runner.request.side_effect = [
        (None, False, 10, 1, reason),
        ("summary-b", True, 10, 1, "stop"),
    ]

    require_reduction(manager, 1)
    manager.compact_answer()

    assert manager._require_memo().answers == {
        "need-a": ["first detailed source", "second detailed source"],
        "need-b": ["summary-b"],
    }


def test_stop_preserves_all_groups_and_skips_remaining_requests(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """중지되면 남은 need를 요청하지 않고 압축 전 메모를 보존한다."""
    manager.set_needs(["need-a", "need-b", "need-c"])
    for need in manager._require_memo().needs:
        manager.add_answer([need], "existing detailed answer")
    original = manager.read_all()
    runner.request.side_effect = [
        ("summary", True, 10, 1, "stop"),
        ("partial", False, 10, 1, "stopped"),
    ]

    require_reduction(manager, 4)
    with pytest.raises(InterruptedError, match="중지"):
        manager.compact_answer()

    assert manager.read_all() == original
    assert runner.request.call_count == 2


def test_empty_groups_do_not_call_runner(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """아직 답변이 없는 need는 요약하지 않는다."""
    manager.compact_answer()

    runner.request.assert_not_called()


def test_summary_that_does_not_shrink_group_is_ignored(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """더 긴 요약으로 need의 기존 답변을 교체하지 않는다."""
    manager.add_answer(["need-a"], "existing answer")
    original = manager.read_all()
    runner.request.return_value = ("a much longer summary", True, 10, 4, "stop")

    require_reduction(manager, 1)
    with pytest.raises(ValueError, match="압축 임계값"):
        manager.compact_answer()
    with pytest.raises(ValueError, match="압축 임계값"):
        manager.compact_answer()
    assert runner.request.call_count == 1

    assert manager.read_all() == original


def test_new_answer_triggers_compaction_before_being_added(
    runner: Mock, prompt_manager: Mock
) -> None:
    """새 답변을 기존 묶음과 함께 압축한 뒤 요약 결과를 저장한다."""
    manager = MemoManager(runner, prompt_manager, max_tokens=10_000)
    manager.start("run-1", "Question")
    manager.set_needs(["need"])
    manager.add_answer(["need"], " ".join(["old"] * 4400))
    assert len(manager.read_all().split()) < 5000
    new_answer = " ".join(["new"] * 6000)

    manager.add_answer(["need"], new_answer)

    runner.request.assert_called_once()
    assert "new" in runner.request.call_args.args[1]
    assert "old" in runner.request.call_args.args[1]
    assert manager._require_memo().answers["need"] == ["summary"]
    assert len(manager.read_all().split()) < 5000


def test_exact_compaction_threshold_continues_to_next_need(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """첫 요약으로 정확히 임계값에 닿아도 다음 need를 처리해 그 아래로 줄인다."""
    manager.add_answer(["need-a"], "one two three four")
    manager.add_answer(["need-b"], "five six seven eight")
    current = len(manager.read_all().split())
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + current - 3
    runner.request.side_effect = [("a", True, 1, 1, "stop"), ("b", True, 1, 1, "stop")]

    manager.compact_answer()

    assert runner.request.call_count == 2
    assert manager._remaining_tokens(manager.read_all()) > COMPACTION_REMAINING_TOKENS


def test_multi_need_copy_is_included_in_total_limit(
    runner: Mock, prompt_manager: Mock
) -> None:
    """한 답변이 여러 need에 저장되는 크기를 계산하고 충분해지면 즉시 멈춘다."""
    manager = MemoManager(runner, prompt_manager, max_tokens=10_000)
    manager.start("run-1", "Question")
    manager.set_needs(["need-a", "need-b"])
    answer = " ".join(["word"] * 3000)

    manager.add_answer(["need-a", "need-b"], answer)

    assert manager._require_memo().answers == {
        "need-a": ["summary"],
        "need-b": [answer],
    }
    runner.request.assert_called_once()
    assert len(manager.read_all().split()) < 5000


def test_failed_compaction_does_not_allow_overflow(
    runner: Mock, prompt_manager: Mock
) -> None:
    """요약에 실패해 공간이 부족하면 새 답변을 추가하지 않는다."""
    manager = MemoManager(runner, prompt_manager, max_tokens=10_000)
    manager.start("run-1", "Question")
    manager.set_needs(["need"])
    manager.add_answer(["need"], " ".join(["old"] * 4400))
    original = manager.read_all()
    runner.request.return_value = (None, False, 10, 1, "length_failed")

    with pytest.raises(ValueError, match="압축 임계값"):
        manager.add_answer(["need"], " ".join(["new"] * 6000))

    assert manager.read_all() == original


@pytest.mark.parametrize("max_tokens", [None, 30_000])
def test_memo_limit_respects_runner_input_budget(
    runner: Mock, prompt_manager: Mock, max_tokens: int | None
) -> None:
    """설정한 메모 한도가 모델 실행기의 입력 예산을 넘지 않는다."""
    runner.get_input_limit.return_value = 100
    manager = MemoManager(runner, prompt_manager, max_tokens=max_tokens)
    manager.start("run-1", "Question")
    manager.set_needs(["need"])

    with pytest.raises(ValueError, match="압축 임계값"):
        manager.add_answer(["need"], " ".join(["new"] * 100))

    runner.get_input_limit.assert_called_once_with(
        prompt_manager.get_prompt.return_value, max_tokens=SUMMARY_MAX_TOKENS
    )


def test_prepare_request_compacts_for_full_input_budget(
    manager: MemoManager,
    runner: Mock,
    prompt_manager: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """메모는 한도 안이어도 질문·부가 입력을 합쳐 넘치면 모델 요청 전에 압축한다."""
    monkeypatch.setattr(memo_module, "COMPACTION_REMAINING_TOKENS", 20)
    manager.add_answer(["need-a"], " ".join(["source"] * 80))
    runner.get_input_limit.return_value = 100
    prefix = " ".join(["question"] * 30) + "\n"
    suffix = "\n" + " ".join(["personal-data"] * 20)
    assert len((prefix + manager.read_all() + suffix).split()) > 100

    request = manager.prepare_for_request(
        "judge prompt", prefix=prefix, suffix=suffix, max_output_tokens=1000
    )

    assert request == prefix + manager.read_all() + suffix
    assert len(request.split()) < 80
    assert manager._require_memo().answers["need-a"] == ["summary"]
    runner.get_input_limit.assert_called_with("judge prompt", max_tokens=1000)
    assert "personal-data" not in runner.request.call_args.args[1]


def test_add_answer_uses_current_request_budget_before_storage(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """다음 답변 저장도 메모 자체가 아닌 전체 요청 예산으로 검사한다."""
    runner.get_input_limit.return_value = 6000
    prefix = " ".join(["question"] * 300) + "\n"
    manager.prepare_for_request("judge prompt", prefix=prefix)
    manager.add_answer(["need-a"], " ".join(["old"] * 500))
    new_answer = " ".join(["new"] * 5300)
    assert (
        len(
            (
                prefix
                + manager._format_memo(manager._with_answer(["need-a"], new_answer))
            ).split()
        )
        > 6000
    )

    manager.add_answer(["need-a"], new_answer)

    assert manager._require_memo().answers["need-a"] == ["summary"]
    assert len((prefix + manager.read_all()).split()) < 1000
    runner.request.assert_called_once()
    assert "new" in runner.request.call_args.args[1]


def test_failed_summary_cannot_overflow_current_request_budget(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """요약 실패 시 전체 입력 한도를 넘는 답변은 보관하지 않는다."""
    runner.get_input_limit.return_value = 6000
    prefix = " ".join(["question"] * 300) + "\n"
    manager.prepare_for_request("judge prompt", prefix=prefix)
    manager.add_answer(["need-a"], " ".join(["old"] * 500))
    original = manager.read_all()
    runner.request.return_value = (None, False, 0, 0, "length_failed")

    with pytest.raises(ValueError, match="압축 임계값"):
        manager.add_answer(["need-a"], " ".join(["new"] * 5300))

    assert manager.read_all() == original


def test_oversized_non_memo_input_is_rejected(
    manager: MemoManager, runner: Mock, prompt_manager: Mock
) -> None:
    """질문·개인정보만으로 입력 예산을 넘으면 메모 압축으로 해결했다고 처리하지 않는다."""
    runner.get_input_limit.return_value = 50

    with pytest.raises(ValueError, match="고정 입력만으로 압축 임계값"):
        manager.prepare_for_request("judge prompt", prefix="word " * 60)

    runner.request.assert_not_called()


def test_start_discards_previous_request_budget(
    manager: MemoManager,
    runner: Mock,
    prompt_manager: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이전 실행의 질문·개인정보 입력 예산을 새 실행에 재사용하지 않는다."""
    monkeypatch.setattr(memo_module, "COMPACTION_REMAINING_TOKENS", 20)
    runner.get_input_limit.return_value = 100
    manager.prepare_for_request("judge prompt")
    runner.request.reset_mock()

    manager.start("run-2", "New question")
    manager.set_needs(["new-need"])
    manager.add_answer(["new-need"], "word " * 200)

    assert manager._request_budget is None
    runner.request.assert_not_called()


def test_clear_removes_memo_and_groups(manager: MemoManager) -> None:
    """초기화 후에는 메모를 조회할 수 없다."""
    manager.add_answer(["need-a"], "answer")
    manager.clear()

    with pytest.raises(RuntimeError, match="메모가 시작되지 않았습니다"):
        manager.read_all()


def test_operations_require_start(runner: Mock, prompt_manager: Mock) -> None:
    """실행을 시작하기 전에는 필요 정보나 답변을 저장할 수 없다."""
    manager = MemoManager(runner, prompt_manager)

    with pytest.raises(RuntimeError, match="메모가 시작되지 않았습니다"):
        manager.set_needs(["need"])
    with pytest.raises(RuntimeError, match="메모가 시작되지 않았습니다"):
        manager.add_answer(["need"], "answer")


def test_incoming_need_is_first_and_other_needs_are_skipped_when_room_is_ready(
    manager: MemoManager, runner: Mock
) -> None:
    """새 답변이 들어가는 need를 먼저 처리하고 충분해지면 다른 need를 건드리지 않는다."""
    manager.add_answer(["need-a"], "first old source")
    manager.add_answer(["need-b"], "second old source")
    pending = "new incoming source"
    candidate_size = len(
        manager._format_memo(manager._with_answer(["need-b"], pending)).split()
    )
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + candidate_size - 4 + 1

    manager.add_answer(["need-b"], pending)

    runner.request.assert_called_once()
    assert runner.request.call_args.args[1] == (
        "Need:\nneed-b\n원문:\n[답변 1]\nsecond old source\n[답변 2]\nnew incoming source"
    )
    assert manager._require_memo().answers == {
        "need-a": ["first old source"],
        "need-b": ["summary"],
    }


def test_compaction_rotates_until_threshold_is_crossed(
    manager: MemoManager, runner: Mock
) -> None:
    """한 바퀴로 부족하면 갱신된 요약을 다음 바퀴에서 다시 줄인다."""
    manager.add_answer(["need-a"], "a1 a2 a3 a4 a5 a6 a7 a8")
    manager.add_answer(["need-b"], "b1 b2 b3 b4 b5 b6 b7 b8")
    candidate_size = len(
        manager._format_memo(manager._with_answer(["need-b"], "new")).split()
    )
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + candidate_size - 12 + 1
    runner.request.side_effect = [
        ("b1 b2 b3 b4 b5", True, 1, 5, "stop"),
        ("a1 a2 a3 a4 a5", True, 1, 5, "stop"),
        ("b", True, 1, 1, "stop"),
    ]

    manager.add_answer(["need-b"], "new")

    assert [c.args[1].splitlines()[1] for c in runner.request.call_args_list] == [
        "need-b",
        "need-a",
        "need-b",
    ]
    assert "b1 b2 b3 b4 b5" in runner.request.call_args_list[2].args[1]
    assert manager._require_memo().answers == {
        "need-a": ["a1 a2 a3 a4 a5"],
        "need-b": ["b"],
    }
    assert manager._remaining_tokens(manager.read_all()) > COMPACTION_REMAINING_TOKENS


def test_multiple_incoming_needs_are_prioritized_before_other_groups(
    manager: MemoManager, runner: Mock
) -> None:
    """여러 need에 새 답변을 연결하면 입력 대상들을 먼저 압축한다."""
    manager.set_needs(["need-a", "need-b", "need-c"])
    for need in manager._require_memo().needs:
        manager.add_answer([need], "one two three four five six seven eight")
    targets = ["need-c", "need-a"]
    candidate_size = len(
        manager._format_memo(manager._with_answer(targets, "new")).split()
    )
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + candidate_size - 25 + 1

    manager.add_answer(targets, "new")

    assert [c.args[1].splitlines()[1] for c in runner.request.call_args_list] == [
        "need-c",
        "need-a",
        "need-b",
    ]


def test_stalled_round_stops_and_preserves_original_memo(
    manager: MemoManager, runner: Mock
) -> None:
    """일부가 줄어도 이후 한 바퀴가 정체되면 무한 반복 없이 추가를 거부한다."""
    manager.add_answer(["need-a"], "a1 a2 a3 a4 a5 a6 a7 a8")
    manager.add_answer(["need-b"], "b1 b2 b3 b4 b5 b6 b7 b8")
    original = manager.read_all()
    candidate_size = len(
        manager._format_memo(manager._with_answer(["need-b"], "new")).split()
    )
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + candidate_size - 12 + 1
    runner.request.side_effect = [
        ("b1 b2 b3 b4 b5", True, 1, 5, "stop"),
        (None, False, 1, 1, "length_failed"),
        ("b", True, 1, 1, "stop"),
        ("b", True, 1, 1, "stop"),
    ]

    with pytest.raises(ValueError, match="압축 임계값 아래로 줄일 수 없습니다"):
        manager.add_answer(["need-b"], "new")
    # 일부가 줄었다가 실패한 후보를 그대로 재시도해도 모델 호출을 반복하지 않는다.
    with pytest.raises(ValueError, match="압축 임계값 아래로 줄일 수 없습니다"):
        manager.add_answer(["need-b"], "new")

    assert manager.read_all() == original
    assert [c.args[1].splitlines()[1] for c in runner.request.call_args_list] == [
        "need-b",
        "need-a",
        "need-b",
        "need-b",
    ]


def test_unchanged_failed_candidate_is_skipped_until_input_changes(
    manager: MemoManager, runner: Mock
) -> None:
    """같은 실패 입력은 다시 호출하지 않고 새 원문이 들어왔을 때만 재시도한다."""
    pending = "one two three four five"
    candidate_size = len(
        manager._format_memo(manager._with_answer(["need-a"], pending)).split()
    )
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + candidate_size - 3 + 1
    runner.request.return_value = (None, False, 1, 1, "parse_failed")

    for _ in range(2):
        with pytest.raises(ValueError, match="압축 임계값"):
            manager.add_answer(["need-a"], pending)
    assert runner.request.call_count == 1

    runner.request.return_value = ("summary", True, 1, 1, "stop")
    manager.add_answer(["need-a"], "different two three four five")

    assert runner.request.call_count == 2
    assert manager._require_memo().answers["need-a"] == ["summary"]


@pytest.mark.parametrize("reset", ["start", "set_needs"])
def test_reset_allows_a_previously_failed_input_again(
    manager: MemoManager, runner: Mock, reset: str
) -> None:
    """새 실행이나 필요 정보 재등록 시 이전 실패 기록을 재사용하지 않는다."""
    pending = "one two three four five"
    candidate_size = len(
        manager._format_memo(manager._with_answer(["need-a"], pending)).split()
    )
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + candidate_size - 3 + 1
    runner.request.return_value = (None, False, 1, 1, "parse_failed")
    with pytest.raises(ValueError, match="압축 임계값"):
        manager.add_answer(["need-a"], pending)

    if reset == "start":
        manager.start("run-2", "Question")
    manager.set_needs(["need-a", "need-b"])
    runner.request.return_value = ("summary", True, 1, 1, "stop")
    manager.add_answer(["need-a"], pending)

    assert runner.request.call_count == 2


def test_stopping_pending_input_does_not_store_it(
    manager: MemoManager, runner: Mock
) -> None:
    """새 답변을 포함한 요약이 중지되면 새 답변과 중간 요약을 모두 반영하지 않는다."""
    manager.add_answer(["need-a"], "old answer")
    original = manager.read_all()
    pending = "one two three four five"
    candidate_size = len(
        manager._format_memo(manager._with_answer(["need-a"], pending)).split()
    )
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + candidate_size - 3 + 1
    runner.request.return_value = ("partial", False, 1, 1, "stopped")

    with pytest.raises(InterruptedError, match="중지"):
        manager.add_answer(["need-a"], pending)

    assert manager.read_all() == original


def test_first_large_answer_is_compacted_with_no_previous_answers(
    runner: Mock, prompt_manager: Mock
) -> None:
    """기존 답변이 없어도 새 원문을 포함해 압축한 결과를 저장한다."""
    manager = MemoManager(runner, prompt_manager, max_tokens=10_000)
    manager.start("run-1", "Question")
    manager.set_needs(["need"])
    answer = "new " * 6000

    manager.add_answer(["need"], answer)

    assert answer in runner.request.call_args.args[1]
    assert manager._require_memo().answers["need"] == ["summary"]


def test_below_threshold_does_not_summarize_again(
    manager: MemoManager, runner: Mock
) -> None:
    """공간이 충분하면 반복 조회 준비와 수동 압축에서 모델을 호출하지 않는다."""
    manager.add_answer(["need-a"], "short answer")
    manager.compact_answer()
    manager.prepare_for_request("judge prompt")
    manager.prepare_for_request("judge prompt")

    runner.request.assert_not_called()


def test_readiness_check_does_not_forget_failed_pending_candidate(
    manager: MemoManager, runner: Mock
) -> None:
    """저장된 메모가 작아도 실패한 추가 후보의 재시도 방지 기록은 유지한다."""
    pending = "one two three four five six seven eight nine"
    candidate_size = len(
        manager._format_memo(manager._with_answer(["need-a"], pending)).split()
    )
    manager._max_tokens = COMPACTION_REMAINING_TOKENS + candidate_size - 3 + 1
    runner.request.side_effect = [
        ("one two three four five six seven", True, 1, 7, "stop"),
        ("one two three four five six seven", True, 1, 7, "stop"),
    ]

    with pytest.raises(ValueError, match="압축 임계값"):
        manager.add_answer(["need-a"], pending)
    manager.compact_answer()
    with pytest.raises(ValueError, match="압축 임계값"):
        manager.add_answer(["need-a"], pending)

    assert runner.request.call_count == 2

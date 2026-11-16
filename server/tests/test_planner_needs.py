"""Planner.judge가 need를 번호(needIdx)로 받아 메모에 등록된 이름으로 바꾸는지 검사.

실제 모델은 need 이름을 바꿔 쓰는 일이 있다 (예: "이직을 고려하는 이유" →
"이직을 고려하는 구체적인 이유"). MemoManager.add_answer는 등록된 이름만 받으므로,
judge는 [필요 정보]에 0부터 번호를 붙여 넣고 모델이 고른 번호를 이름으로 바꾼다.
"""

from typing import Any
from unittest.mock import Mock

import pytest

from app.pipeline.planner import NEED_RETRIES, Planner

NEEDS = ["이직을 고려하는 이유", "현재 직무 및 경력 분석"]


def judged(need_idx: list[int], enough: bool = False) -> dict[str, Any]:
    return {
        "enough": enough,
        "needConsent": False,
        "consentItems": [],
        "needIdx": need_idx,
    }


def planner_with(*outputs: dict[str, Any]) -> tuple[Planner, Mock]:
    runner = Mock()
    runner.request.side_effect = [(o, True, 10, 5, "stop") for o in outputs]
    return Planner(runner), runner


def judge(planner: Planner, needs: list[str] | None = NEEDS) -> Any:
    return planner.judge("질문", "메모", [], "run-1", "chat-1", needs)


def test_input_lists_needs_with_zero_based_numbers() -> None:
    planner, runner = planner_with(judged([1]))
    judge(planner)
    judge_input = runner.request.call_args.args[1]
    assert (
        "[필요 정보]\n0. 이직을 고려하는 이유\n1. 현재 직무 및 경력 분석\n\n[메모]"
        in (judge_input)
    )


def test_numbers_become_registered_names_in_order_without_duplicates() -> None:
    planner, runner = planner_with(judged([1, 0, 1]))
    assert judge(planner).need == ["현재 직무 및 경력 분석", "이직을 고려하는 이유"]
    assert runner.request.call_count == 1


def test_out_of_range_number_is_asked_again() -> None:
    planner, runner = planner_with(judged([5]), judged([0]))
    assert judge(planner).need == ["이직을 고려하는 이유"]
    assert runner.request.call_count == 2


def test_out_of_range_after_retries_raises() -> None:
    planner, runner = planner_with(*[judged([2])] * (NEED_RETRIES + 1))
    with pytest.raises(RuntimeError, match="범위"):
        judge(planner)
    assert runner.request.call_count == NEED_RETRIES + 1


def test_enough_ignores_numbers() -> None:
    planner, _ = planner_with(judged([9], enough=True))
    result = judge(planner)
    assert result.enough and result.need == []


def test_empty_numbers_mean_enough() -> None:
    # 설계서 보정 규칙: 물어볼 need가 없으면 답변 가능
    planner, _ = planner_with(judged([]))
    assert judge(planner).enough


def test_without_needs_lists_none_and_any_number_is_out_of_range() -> None:
    planner, runner = planner_with(*[judged([0])] * (NEED_RETRIES + 1))
    with pytest.raises(RuntimeError):
        judge(planner, needs=None)
    assert "[필요 정보]\n없음" in runner.request.call_args.args[1]


def test_direct_raw_plan_is_not_retried() -> None:
    runner = Mock()
    planner = Planner(runner)
    ok = planner.judge("질문", "", [], "r", "c", NEEDS, raw_plan=judged([1]))
    assert ok.need == ["현재 직무 및 경력 분석"]
    with pytest.raises(RuntimeError):
        planner.judge("질문", "", [], "r", "c", NEEDS, raw_plan=judged([7]))
    runner.request.assert_not_called()


def test_failed_request_still_raises() -> None:
    runner = Mock()
    runner.request.return_value = (None, False, None, None, "parse_failed")
    with pytest.raises(RuntimeError):
        judge(Planner(runner))

"""개인정보 검출 평가 스크립트 검사. Runner 대신 정해 둔 검출 결과를 돌려주는 대역을 쓴다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.models.llm_results import DetectedItem
from qa.pd_eval import (
    SERVER_DIR,
    Label,
    Sample,
    SampleResult,
    grade,
    load_samples,
    run,
    summarize,
)


class FakeRunner:
    """문장별로 정해 둔 (검출 목록, 성공 여부, 사유)를 돌려준다."""

    def __init__(self, outputs: dict[str, tuple[Any, bool, str]]) -> None:
        self.outputs = outputs

    def request(self, run_id: Any, text: str, *args: Any, **kwargs: Any) -> tuple:
        found, ok, reason = self.outputs[text]
        if isinstance(found, Exception):
            raise found
        return found, ok, 10, 5, reason


def result(expected: list[Label], detected: list[Label]) -> SampleResult:
    return SampleResult(
        id="x", ok=True, elapsed_s=0, expected=expected, detected=detected
    )


def test_grade_ignores_separators_and_counts_errors() -> None:
    graded = grade(
        result(
            [Label(text="010-1234-5678", category=0), Label(text="우울증", category=1)],
            [
                Label(text="01012345678", category=0),
                Label(text="우울증", category=2),
                Label(text="서울", category=2),
            ],
        )
    )
    assert graded.tp == 2
    assert graded.fn == []
    assert graded.fp == ["서울"]
    assert graded.wrong_category == ["우울증"]


def test_one_detection_covers_each_expected_item() -> None:
    graded = grade(
        result(
            [Label(text="연세대", category=2), Label(text="심리학과", category=2)],
            [Label(text="연세대 심리학과", category=2)],
        )
    )
    assert graded.tp == 2
    assert graded.matched_detections == 1
    assert graded.fn == []
    summary = summarize([graded], 0)
    assert summary["precision"] == summary["recall"] == summary["f1"] == 1
    assert summary["per_category"] == {"속성정보": {"expected": 2, "found": 2}}


@pytest.mark.parametrize("reverse", [False, True])
def test_overlaps_have_order_independent_bounded_scores(reverse: bool) -> None:
    expected = [Label(text="연세대", category=2), Label(text="심리학과", category=2)]
    detected = [
        Label(text="연세대 심리학과", category=2),
        Label(text="연세대", category=0),
        Label(text="과검출"),
    ]
    if reverse:
        expected.reverse()
        detected.reverse()
    summary = summarize([grade(result(expected, detected))], 0)
    assert summary["precision"] == pytest.approx(2 / 3)
    assert summary["recall"] == 1
    assert summary["f1"] == pytest.approx(0.8)
    assert summary["category_accuracy"] == 1


def test_repeated_expected_text_counts_each_item() -> None:
    graded = grade(
        result(
            [Label(text="김철수", category=0), Label(text="김철수", category=2)],
            [Label(text="김철수", category=0)],
        )
    )
    summary = summarize([graded], 0)
    assert graded.tp == 2
    assert graded.wrong_category == ["김철수"]
    assert summary["category_accuracy"] == 0.5
    assert summary["per_category"] == {
        "식별정보": {"expected": 1, "found": 1},
        "속성정보": {"expected": 1, "found": 1},
    }


def test_load_samples_checks_format(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text('{"text": "a"}\n\n{"id": 7, "text": "b", "pd": []}\n', "utf-8")
    assert [s.id for s in load_samples(path)] == ["1", "7"]

    path.write_text('{"id": "a", "text": "x"}\n{"id": "a", "text": "y"}\n', "utf-8")
    with pytest.raises(ValueError, match="겹칩니다"):
        load_samples(path)

    path.write_text('{"text": "x", "pd": [{"text": "y", "category": 5}]}\n', "utf-8")
    with pytest.raises(ValueError, match=":1 형식 오류"):
        load_samples(path)


def test_sample_dataset_is_valid() -> None:
    assert load_samples(SERVER_DIR / "qa" / "datasets" / "pd_sample.jsonl")


@pytest.mark.parametrize(
    ("expected", "detected", "f1"),
    [
        ([Label(text="김하늘")], [Label(text="박바다")], 0.0),
        ([Label(text="김하늘")], [], 0.0),
        ([], [Label(text="박바다")], 0.0),
        ([], [], None),
    ],
)
def test_f1_is_zero_for_errors_and_undefined_only_without_items(
    expected: list[Label], detected: list[Label], f1: float | None
) -> None:
    assert summarize([grade(result(expected, detected))], 0)["f1"] == f1


def test_run_preserves_unknown_usage_and_writes_partial_totals(tmp_path: Path) -> None:
    class UsageRunner:
        def request(self, *args: Any, **kwargs: Any) -> tuple:
            text = args[1]
            if text == "failed":
                return None, False, None, None, "unavailable"
            if text == "unknown":
                return [], True, None, 7, "stop"
            return [], True, 11, 0, "stop"

    samples = [Sample(id=text, text=text) for text in ("known", "unknown", "failed")]
    summary = run(samples, UsageRunner(), tmp_path, Path("usage.jsonl"))
    assert (summary["succeeded"], summary["failed"]) == (2, 1)
    assert (summary["input_tokens"], summary["output_tokens"]) == (11, 7)
    assert (summary["input_tokens_missing"], summary["output_tokens_missing"]) == (2, 1)
    rows = [
        json.loads(line)
        for line in (tmp_path / "results.jsonl").read_text("utf-8").splitlines()
    ]
    assert rows[0]["output_tokens"] == 0
    assert rows[1]["input_tokens"] is None
    assert rows[2]["input_tokens"] is None
    assert rows[2]["output_tokens"] is None
    assert "토큰 미제공 문장 수" in (tmp_path / "summary.md").read_text("utf-8")


def test_run_writes_results_and_summary(tmp_path: Path) -> None:
    data = tmp_path / "d.jsonl"
    rows = [
        {"id": "hit", "text": "번호 010-1", "pd": [{"text": "010-1", "category": 0}]},
        {
            "id": "miss",
            "text": "저는 김하늘",
            "pd": [{"text": "김하늘", "category": 0}],
        },
        {"id": "fail", "text": "형식 오류", "pd": []},
        {"id": "boom", "text": "연결 끊김", "pd": []},
    ]
    data.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), "utf-8")
    runner = FakeRunner(
        {
            "번호 010-1": ([DetectedItem(text="010-1", category=0)], True, "stop"),
            "저는 김하늘": ([DetectedItem(text="서울", category=2)], True, "stop"),
            "형식 오류": (None, False, "parse_failed"),
            "연결 끊김": (ConnectionError("down"), False, ""),
        }
    )
    out = tmp_path / "out"
    summary = run(load_samples(data), runner, out, data)

    assert summary["succeeded"] == 2 and summary["failed"] == 2
    assert summary["failure_reasons"] == {
        "parse_failed": 1,
        "ConnectionError: down": 1,
    }
    assert (summary["tp"], summary["fp"], summary["fn"]) == (1, 1, 1)
    assert summary["precision"] == 0.5 and summary["recall"] == 0.5
    assert summary["exact_match"] == 0.5
    assert summary["per_category"] == {"식별정보": {"expected": 2, "found": 1}}
    assert len((out / "results.jsonl").read_text("utf-8").splitlines()) == 4
    report = (out / "summary.md").read_text("utf-8")
    assert "`miss`: 김하늘" in report
    assert "`fail`: parse_failed" in report

"""RequestWriter의 입력 크기 한도 테스트. 실제 모델 없이 가짜 Runner 사용."""

from __future__ import annotations

from typing import Any

from app.models.enums import PromptIdx
from app.pipeline.request_writer import INPUT_TOKEN_LIMIT, RequestWriter

PD_TABLE = [
    {"pd_id": 1, "text": "컴공과", "category": 2, "allowed": True},
    {"pd_id": 2, "text": "인하대", "category": 2, "allowed": False},
]


class FakePromptManager:
    def get_prompt(self, prompt_idx: PromptIdx) -> str:
        return "system"


class FakeRunner:
    """글자 하나를 토큰 하나로 세는 가짜 Runner."""

    def __init__(self, runner_limit: int = 10**9) -> None:
        self.prompt_manager = FakePromptManager()
        self.runner_limit = runner_limit
        self.requests: list[tuple[Any, ...]] = []

    def tokenize(self, text: str) -> list[int]:
        return [0] * len(text)

    def get_input_limit(self, system_prompt: str) -> int:
        return self.runner_limit

    def request(
        self, run_id: str, input_: str, prompt_idx: PromptIdx, chat_id: str | None
    ) -> tuple[Any, ...]:
        self.requests.append((run_id, prompt_idx, chat_id))
        return "요청문: 대학교 학생입니다.", True, 1, 1, "stop"


def test_write_within_limit_calls_model() -> None:
    runner = FakeRunner()
    writer = RequestWriter(runner)  # type: ignore[arg-type]
    result = writer.write("질문", "", PD_TABLE, ["지원동기"], 1, 1)
    assert result == "대학교 학생입니다."
    assert runner.requests == [(1, PromptIdx.WRITE, 1)]


def test_write_over_limit_skips_model() -> None:
    runner = FakeRunner()
    writer = RequestWriter(runner)  # type: ignore[arg-type]
    memo = "가" * (INPUT_TOKEN_LIMIT + 1)
    assert writer.write("질문", memo, PD_TABLE, ["지원동기"], 1) is None
    assert runner.requests == []


def test_rewrite_uses_smaller_runner_limit() -> None:
    runner = FakeRunner(runner_limit=50)
    writer = RequestWriter(runner)  # type: ignore[arg-type]
    previous = "인하대 학생입니다. " * 20
    assert writer.rewrite(previous, [2], ["지원동기"], PD_TABLE, 1) is None
    assert runner.requests == []

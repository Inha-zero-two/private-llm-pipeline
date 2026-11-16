"""FrontierClient 테스트. 실제 게이트웨이 대신 httpx.MockTransport로 응답을 흉내 낸다."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.runtime import frontier_client as fc
from app.runtime.frontier_client import (
    FrontierCancelledError,
    FrontierClient,
)


def reply(content: str, prompt_tokens: int, completion_tokens: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        },
    )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    runner: Any = None,
    base_url: str = "https://gateway.test/v1",
) -> FrontierClient:
    return FrontierClient(
        local_llm_runner=runner,
        api_key="sk-test",
        base_url=base_url,
        env_file=None,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fc, "RETRY_DELAYS", (0.0, 0.0))


class StoppingRunner:
    """컨텍스트 판단 도중 사용자가 중지한 상황을 흉내 내는 로컬 모델."""

    def __init__(self) -> None:
        self.client: FrontierClient | None = None

    def request(self, run_id: str, *args: Any) -> tuple[str, bool]:
        assert self.client is not None
        self.client.stop(run_id)
        return "keep", True


def test_stop_during_decide_context_sends_nothing() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return reply("답", 1, 1)

    runner = StoppingRunner()
    client = make_client(handler, runner=runner)
    runner.client = client
    client.send("run-1", "첫 질문")  # 이전 대화가 있어야 로컬 판단을 부름

    with pytest.raises(FrontierCancelledError):
        client.send("run-1", "두 번째 질문")
    assert len(posts) == 1


def test_stop_before_send_blocks_until_clear() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return reply("답", 1, 1)

    client = make_client(handler)
    client.stop("run-1")  # 진행 중인 전송이 없을 때 중지

    with pytest.raises(FrontierCancelledError):
        client.send("run-1", "질문")
    assert posts == []

    client.clear("run-1")
    assert client.send("run-1", "질문").answer == "답"
    assert len(posts) == 1

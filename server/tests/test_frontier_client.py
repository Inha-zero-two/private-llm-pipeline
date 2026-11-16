"""FrontierClient 테스트. 실제 게이트웨이 대신 httpx.MockTransport로 응답을 흉내 낸다."""

import json
from collections.abc import Callable

import httpx
import pytest

from app.runtime import frontier_client as fc
from app.runtime.frontier_client import (
    FrontierCancelledError,
    FrontierClient,
    FrontierError,
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
    base_url: str = "https://gateway.test/v1",
) -> FrontierClient:
    return FrontierClient(
        api_key="sk-test",
        base_url=base_url,
        env_file=None,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fc, "RETRY_DELAYS", (0.0, 0.0))


def test_invalid_base_url_means_offline() -> None:
    client = make_client(lambda request: httpx.Response(200), base_url="https://h:x")
    assert client.check_connection() is False
    with pytest.raises(FrontierError):
        client.send(1, "질문")


def test_empty_answer_usage_is_added_to_cost() -> None:
    responses = iter([reply("", 100, 4096), reply("답", 100, 50)])
    client = make_client(lambda request: next(responses))

    result = client.send(1, "질문")

    assert result.answer == "답"
    assert (result.input_tokens, result.output_tokens) == (200, 4146)
    assert result.cost == pytest.approx(client._cost(200, 4146))


def test_all_empty_answers_still_record_usage() -> None:
    client = make_client(lambda request: reply("", 10, 20))

    with pytest.raises(FrontierError):
        client.send(1, "질문")

    assert client.last_request is not None
    assert client.last_request.status == "failed"
    assert client.last_request.output_tokens == 20 * fc.MAX_ATTEMPTS
    assert client.last_request.cost > 0


def sent_messages(request: httpx.Request) -> list[dict[str, str]]:
    return json.loads(request.content)["messages"]


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (["경력"], ["경력"], "keep"),
        (["경력", "희망 직무"], ["희망 직무", "경력"], "keep"),  # 순서는 보지 않음
        (["경력"], ["희망 직무"], "reset"),
        (["경력", "희망 직무"], ["경력"], "reset"),  # 일부만 겹치면 reset
        (["경력"], None, "reset"),
    ],
)
def test_keep_context_only_when_need_matches(
    first: list[str], second: list[str] | None, expected: str
) -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return reply(f"답{len(posts)}", 1, 1)

    client = make_client(handler)
    assert client.send(1, "첫 질문", need=first).context_mode == "reset"

    result = client.send(1, "두 번째 질문", need=second)

    assert result.context_mode == expected
    contents = [m["content"] for m in sent_messages(posts[1])]
    assert ("답1" in contents) == (expected == "keep")


def test_context_is_not_shared_between_runs() -> None:
    client = make_client(lambda request: reply("답", 1, 1))
    client.send(1, "질문", need=["경력"])

    assert client.send(2, "질문", need=["경력"]).context_mode == "reset"


def test_clear_forgets_need() -> None:
    client = make_client(lambda request: reply("답", 1, 1))
    client.send(1, "질문", need=["경력"])
    client.clear(1)

    assert client.send(1, "질문", need=["경력"]).context_mode == "reset"


def test_stop_before_send_blocks_until_clear() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return reply("답", 1, 1)

    client = make_client(handler)
    client.stop(1)  # 진행 중인 전송이 없을 때 중지

    with pytest.raises(FrontierCancelledError):
        client.send(1, "질문")
    assert posts == []

    client.clear(1)
    assert client.send(1, "질문").answer == "답"
    assert len(posts) == 1

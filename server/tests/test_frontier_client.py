"""FrontierClient 테스트. 실제 게이트웨이 대신 httpx.MockTransport로 응답을 흉내 낸다."""

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

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
            "id": f"resp_{content or 'empty'}",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                }
            ],
            "usage": {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
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
    for name in (
        "FRONTIER_API_KEY",
        "FRONTIER_BASE_URL",
        "FRONTIER_MODEL",
        "FRONTIER_WEB_SEARCH",
    ):
        monkeypatch.delenv(name, raising=False)


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
    payload = json.loads(posts[1].content)
    history = [
        {"role": "user", "content": "첫 질문"},
        *reply("답1", 1, 1).json()["output"],
    ]
    assert payload["input"] == [
        *(history if expected == "keep" else []),
        {"role": "user", "content": "두 번째 질문"},
    ]
    assert "previous_response_id" not in payload
    assert payload["store"] is False


def test_context_is_not_shared_between_runs() -> None:
    client = make_client(lambda request: reply("답", 1, 1))
    client.send(1, "질문", need=["경력"])

    assert client.send(2, "질문", need=["경력"]).context_mode == "reset"


def test_backend_replays_full_outputs_and_discards_history_on_reset() -> None:
    posts: list[dict] = []
    data = reply("검색 결과 [1]", 10, 20).json()
    # 응답 ID는 필요 없음. 검색·추론·출처 원본은 백엔드가 보관해 다시 전송한다.
    del data["id"]
    data["output"].insert(
        0,
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "encrypted-reasoning",
        },
    )
    data["output"].insert(
        1,
        {"type": "web_search_call", "id": "ws_1", "action": {"type": "search"}},
    )
    data["output"][2]["content"][0]["annotations"] = [
        {
            "type": "url_citation",
            "start_index": 6,
            "end_index": 9,
            "url": "https://example.com/news",
            "title": "검색 출처",
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json=data)

    client = make_client(handler)
    first = client.send(1, "첫 질문", need=["경력"])
    assert "[출처](https://example.com/news)" in first.answer
    client.send(1, "두 번째 질문", need=["경력"])
    client.send(1, "세 번째 질문", need=["경력"])
    client.send(1, "새 주제", need=["직무"])
    client.send(1, "후속 질문", need=["직무"])

    assert posts[2]["input"] == [
        {"role": "user", "content": "첫 질문"},
        *data["output"],
        {"role": "user", "content": "두 번째 질문"},
        *data["output"],
        {"role": "user", "content": "세 번째 질문"},
    ]
    assert posts[3]["input"] == [{"role": "user", "content": "새 주제"}]
    assert posts[4]["input"] == [
        {"role": "user", "content": "새 주제"},
        *data["output"],
        {"role": "user", "content": "후속 질문"},
    ]
    assert all("previous_response_id" not in payload for payload in posts)
    assert all(payload["store"] is False for payload in posts)


def test_failed_reset_does_not_revive_old_history() -> None:
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        if len(posts) == 2:
            return httpx.Response(400)
        return reply("답", 1, 1)

    client = make_client(handler)
    client.send(1, "첫 질문", need=["경력"])
    with pytest.raises(FrontierError):
        client.send(1, "새 주제", need=["직무"])
    assert client.send(1, "다시 질문", need=["경력"]).context_mode == "reset"
    assert posts[-1]["input"] == [{"role": "user", "content": "다시 질문"}]


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


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {}, "FRONTIER_AUTH_ERROR"),
        (403, {}, "FRONTIER_AUTH_ERROR"),
        (429, {}, "FRONTIER_RATE_LIMITED"),
        (503, {}, "FRONTIER_UNAVAILABLE"),
        (422, {}, "FRONTIER_REQUEST_REJECTED"),
        (422, {"error": {"code": "quota_exceeded"}}, "FRONTIER_QUOTA_EXCEEDED"),
        (400, {}, "FRONTIER_REQUEST_REJECTED"),
        (200, {}, "FRONTIER_RESPONSE_INVALID"),
        (
            200,
            {
                "id": "resp_invalid",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": ["wrong type"]}],
                    }
                ],
            },
            "FRONTIER_RESPONSE_INVALID",
        ),
    ],
)
def test_failure_codes_survive_retry_and_connection_check(
    status: int, body: dict, code: str
) -> None:
    client = make_client(lambda request: httpx.Response(status, json=body))
    if status != 200:
        assert client.check_connection() is False
        assert client.connection_failure.code == code
    with pytest.raises(FrontierError) as caught:
        client.send(1, "question")
    assert caught.value.code == code
    assert caught.value.stage == "FRONTIER"


def test_external_timeout_keeps_its_code() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private host", request=request)

    client = make_client(timeout)
    assert client.check_connection() is False
    assert client.connection_failure.code == "FRONTIER_TIMEOUT"
    with pytest.raises(FrontierError) as caught:
        client.send(1, "question")
    assert caught.value.code == "FRONTIER_TIMEOUT"


def test_env_file_and_environment_keep_existing_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FRONTIER_API_KEY=sk-file\n"
        "FRONTIER_BASE_URL=https://file.test/v1\n"
        "FRONTIER_MODEL=gpt-6-luna\n"
        "FRONTIER_WEB_SEARCH=false\n",
        encoding="utf-8",
    )
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return reply("답", 1, 1)

    client = FrontierClient(env_file=env_file, transport=httpx.MockTransport(handler))
    client.send(1, "질문")
    assert posts[-1].url == "https://file.test/v1/responses"
    assert posts[-1].headers["Authorization"] == "Bearer sk-file"
    assert "tools" not in json.loads(posts[-1].content)

    monkeypatch.setenv("FRONTIER_API_KEY", "sk-environment")
    monkeypatch.setenv("FRONTIER_BASE_URL", "https://environment.test/v1")
    monkeypatch.setenv("FRONTIER_WEB_SEARCH", "true")
    client = FrontierClient(env_file=env_file, transport=httpx.MockTransport(handler))
    client.send(1, "질문")
    assert posts[-1].url == "https://environment.test/v1/responses"
    assert posts[-1].headers["Authorization"] == "Bearer sk-environment"
    payload = json.loads(posts[-1].content)
    assert payload["model"] == "gpt-6-luna"
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == "auto"
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["max_output_tokens"] == fc.MAX_OUTPUT_TOKENS
    assert "max_tokens" not in payload


def test_instructions_are_sent_on_every_request() -> None:
    from app.data.prompt_manager import PromptManager

    posts: list[httpx.Request] = []
    prompts = PromptManager()

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return reply("답", 1, 1)

    client = make_client(handler)
    client.prompt_manager = prompts
    client.send(1, "첫 질문", need=["역량"])
    client.send(1, "두 번째 질문", need=["역량"])
    for request in posts:
        assert json.loads(request.content)["instructions"] == prompts.get_prompt(
            fc.PromptIdx.FRONTIER
        )


def test_invalid_search_setting_fails_before_network(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FRONTIER_WEB_SEARCH=maybe\n", encoding="utf-8")
    with pytest.raises(FrontierError) as caught:
        FrontierClient(env_file=env_file)
    assert caught.value.code == "CONFIGURATION_ERROR"


def test_search_usage_cache_and_citations_are_preserved() -> None:
    data = reply("최신 정보 [1]", 1000, 100).json()
    data["output"].insert(0, {"type": "web_search_call", "action": {"type": "search"}})
    data["tool_usage"] = {"web_search": {"num_requests": 2}}
    data["usage"]["input_tokens_details"] = {
        "cached_tokens": 500,
        "cache_write_tokens": 200,
        "cache_write_tokens_details": {"ttl_12h_tokens": 50, "ttl_30m_tokens": 150},
    }
    data["usage"]["output_tokens_details"] = {"reasoning_tokens": 80}
    data["output"][1]["content"][0]["annotations"] = [
        {
            "type": "url_citation",
            "start_index": 6,
            "end_index": 9,
            "url": "https://example.com/news(1)",
            "title": "출처 제목",
        }
    ]
    client = make_client(lambda request: httpx.Response(200, json=data))

    result = client.send(1, "질문")

    assert result.answer == "최신 정보 [출처](https://example.com/news%281%29)"
    assert (result.input_tokens, result.output_tokens) == (1000, 100)
    assert result.cost == pytest.approx(
        (300 * 0.1 + 500 * 0.01 + 150 * 0.125 + 50 * 0.25 + 100 * 0.5) / 1_000_000
        + 0.02
    )


def test_long_context_prices_are_applied_per_attempt() -> None:
    responses = iter([reply("", 200_000, 10), reply("답", 100_000, 20)])
    client = make_client(lambda request: next(responses))
    result = client.send(1, "질문")
    assert result.cost == pytest.approx((300_000 * 0.1 + 30 * 0.5) / 1_000_000)
    assert client._cost(300_000, 30) == pytest.approx(
        (300_000 * 0.2 + 30 * 0.75) / 1_000_000
    )


@pytest.mark.parametrize(
    "provider_code",
    [
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_usage_limit_exceeded",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
    ],
)
def test_quota_errors_are_not_retried(provider_code: str) -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(429, json={"error": {"code": provider_code}})

    client = make_client(handler)
    with pytest.raises(FrontierError) as caught:
        client.send(1, "질문")
    assert caught.value.code == "FRONTIER_QUOTA_EXCEEDED"
    assert len(posts) == 1


def test_incomplete_answer_does_not_advance_context() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        data = reply("첫 답" if len(posts) == 1 else "잘린 답", 10, 20).json()
        if len(posts) > 1:
            data["status"] = "incomplete"
        return httpx.Response(200, json=data)

    client = make_client(handler)
    client.send(1, "첫 질문", need=["역량"])
    with pytest.raises(FrontierError):
        client.send(1, "다음 질문", need=["역량"])
    assert client.last_request.output_tokens == 20 * fc.MAX_ATTEMPTS
    assert client.last_request.status == "failed"
    expected_input = [
        {"role": "user", "content": "첫 질문"},
        *reply("첫 답", 10, 20).json()["output"],
        {"role": "user", "content": "다음 질문"},
    ]
    assert all(
        json.loads(request.content)["input"] == expected_input for request in posts[1:]
    )
    assert client._contexts[1] == expected_input[:-1]


def test_parallel_runs_keep_histories_separate() -> None:
    barrier = Barrier(2)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        barrier.wait(timeout=5)
        question = payload["input"][-1]["content"]
        assert all(
            item["content"] == question
            for item in payload["input"]
            if item["role"] == "user"
        )
        if len(payload["input"]) > 1:
            assert payload["input"][1] == reply(question, 1, 1).json()["output"][0]
        return reply(question, 1, 1)

    client = make_client(handler)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(2):
            futures = [
                pool.submit(client.send, run_id, str(run_id), need=["역량"])
                for run_id in (1, 2)
            ]
            assert [f.result(timeout=10).answer for f in futures] == ["1", "2"]


def test_stop_during_request_does_not_restore_cleared_context() -> None:
    started, release = Event(), Event()

    def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        assert release.wait(timeout=5)
        return reply("답", 1, 1)

    client = make_client(handler)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.send, 1, "질문", need=["역량"])
            assert started.wait(timeout=5)
            client.stop(1)
            client.clear(1)
            with pytest.raises(FrontierCancelledError):
                future.result(timeout=5)
            assert 1 not in client._contexts
    finally:
        release.set()

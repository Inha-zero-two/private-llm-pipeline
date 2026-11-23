"""실제 Runner 재시도 경로에서 원인이 소실되지 않는지 전송 대역으로 검사."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock

import pytest

from app.data.prompt_manager import PromptManager
from app.models.enums import PromptIdx
from app.runtime.local_llm_runner import MAX_ATTEMPTS, N_CTX, LocalLLMRunner


class ScriptedModel:
    def __init__(self, responses: list[Exception | tuple[str, str | None]]) -> None:
        self.responses = iter(responses)

    def tokenize(self, text: bytes, **kwargs: Any) -> list[int]:
        return list(range(len(text)))

    def create_chat_completion(
        self, *, messages: list[dict[str, str]], **kwargs: Any
    ) -> Iterator[dict[str, Any]]:
        if messages[0]["content"] == "Answer briefly.":
            text, reason = "OK", "stop"
        else:
            response = next(self.responses)
            if isinstance(response, Exception):
                raise response
            text, reason = response
        yield {"choices": [{"delta": {"content": text}, "finish_reason": reason}]}

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (TimeoutError("private timeout"), "timeout"),
        (ConnectionError("private host"), "unavailable"),
        (("{", "length"), "length_failed"),
        (("invalid", "stop"), "parse_failed"),
        (("", None), "parse_failed"),
    ],
)
def test_runner_final_reason_and_attempt_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    response: Exception | tuple[str, str | None],
    reason: str,
) -> None:
    model = ScriptedModel([response] * MAX_ATTEMPTS)
    monkeypatch.setattr(
        "src.localLLM_API_proxy.llama_wrapper.Llama", Mock(return_value=model)
    )
    store = Mock()
    with LocalLLMRunner(prompt_manager=PromptManager(), chat_manager=store) as runner:
        result = runner.request(1, "question", PromptIdx.NEEDS, 1)
    assert result[1] is False and result[-1] == reason
    attempts = [call.args[2] for call in store.log_event.call_args_list]
    assert [item["attempt"] for item in attempts] == list(range(1, MAX_ATTEMPTS + 1))
    assert all(item["failure_reason"] in {reason, "length"} for item in attempts)
    assert all(item["error"]["stage"] == "NEEDS" for item in attempts)


def test_retry_success_keeps_attempt_failure_without_run_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ScriptedModel([TimeoutError("private"), ('{"needs":["need"]}', "stop")])
    monkeypatch.setattr(
        "src.localLLM_API_proxy.llama_wrapper.Llama", Mock(return_value=model)
    )
    store = Mock()
    with LocalLLMRunner(prompt_manager=PromptManager(), chat_manager=store) as runner:
        result = runner.request(1, "question", PromptIdx.NEEDS, 1)
    assert result[0] == ["need"] and result[1] is True
    events = store.log_event.call_args_list
    assert all(call.args[1] == "llm_call" for call in events)
    assert events[0].args[2]["error"]["code"] == "LOCAL_LLM_TIMEOUT"
    assert events[1].args[2]["error"] is None


def test_input_limit_is_not_an_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    model = ScriptedModel([])
    monkeypatch.setattr(
        "src.localLLM_API_proxy.llama_wrapper.Llama", Mock(return_value=model)
    )
    with LocalLLMRunner(prompt_manager=PromptManager()) as runner:
        assert runner.request(1, "x" * N_CTX, PromptIdx.NEEDS, 1)[-1] == "context_full"

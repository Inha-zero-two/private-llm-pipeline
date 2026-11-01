"""프록시를 대역으로 바꾸어 키 탐색, 호출, 스레드 중지와 예외 전파를 검증한다."""

from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import Any

import pytest

from app.runtime.local_llm_runner import LocalLLMRunner
from src.localLLM_API_proxy import llama_wrapper


def normal_stream(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield {"choices": [{"delta": {"content": "OK"}, "finish_reason": None}]}
    yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    yield {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}


class FakeLlama:
    def __init__(self, **kwargs: Any) -> None:
        self.options = kwargs
        self.calls: list[dict[str, Any]] = []
        self.stream_factory: Callable[[dict[str, Any]], Iterator[dict[str, Any]]] = (
            normal_stream
        )
        self.closed = False

    def create_chat_completion(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.calls.append(kwargs)
        yield from self.stream_factory(kwargs)

    def tokenize(self, text: bytes, **_: Any) -> list[int]:
        return list(text)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[LocalLLMRunner]:
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys/llama_api_key_07.key").write_text("test-only-token")
    monkeypatch.setattr(llama_wrapper, "Llama", FakeLlama)
    instance = LocalLLMRunner(repository_root=tmp_path)
    yield instance
    instance.close()


def test_constructor_probes_remote_and_finds_numbered_key(
    runner: LocalLLMRunner,
) -> None:
    assert runner._llm.options["backend"] == "remote"
    assert runner._llm.options["n_ctx"] == 32768
    assert runner._llm.options["server_token_file"].name == "llama_api_key_07.key"
    assert len(runner._llm.calls) == 1
    assert runner.model_load() is True
    assert len(runner._llm.calls) == 1


def test_key_search_is_numeric_and_does_not_search_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(llama_wrapper, "Llama", FakeLlama)
    repository = tmp_path / "repo"
    repository.mkdir()
    (tmp_path / "llama_api_key_01.key").write_text("test-outside")
    with pytest.raises(FileNotFoundError):
        LocalLLMRunner(repository_root=repository)
    for number in (10, 2):
        (repository / f"llama_api_key_{number}.key").write_text("test-inside")
    with LocalLLMRunner(repository_root=repository) as instance:
        assert instance._llm.options["server_token_file"].name == "llama_api_key_2.key"


def test_manual_request_uses_explicit_messages_and_collects_final_usage(
    runner: LocalLLMRunner,
) -> None:
    assert runner.manual_request("hello", "system", max_tokens=12) == (
        "OK",
        True,
        7,
        2,
        "stop",
    )
    call = runner._llm.calls[-1]
    assert call["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]
    assert call["stream_options"] == {"include_usage": True}
    assert call["chat_template_kwargs"] == {"enable_thinking": False}
    assert call["max_tokens"] == 12


def test_missing_usage_is_unknown_not_zero(runner: LocalLLMRunner) -> None:
    runner._llm.stream_factory = lambda _: iter(
        [{"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]}]
    )
    assert runner.manual_request("input", "system")[2:4] == (None, None)


@pytest.mark.parametrize("empty", [True, False])
def test_empty_or_incomplete_response_is_exception(
    runner: LocalLLMRunner, empty: bool
) -> None:
    runner._llm.stream_factory = lambda _: iter(
        [] if empty else [{"choices": [{"delta": {"content": "partial"}}]}]
    )
    with pytest.raises(RuntimeError, match="no response|without a finish"):
        runner.manual_request("input", "system")


def test_proxy_exception_is_propagated_without_replacement(
    runner: LocalLLMRunner,
) -> None:
    error = OSError("synthetic proxy failure")

    def fail(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        raise error

    runner._llm.stream_factory = fail
    with pytest.raises(OSError) as raised:
        runner.manual_request("input", "system")
    assert raised.value is error
    runner._llm.stream_factory = normal_stream
    assert runner.manual_request("next", "system")[1]


def test_constructor_failure_closes_proxy_and_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = FakeLlama()
    error = ConnectionError("synthetic connection failure")

    def fail(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        raise error

    backend.stream_factory = fail
    monkeypatch.setattr(llama_wrapper, "Llama", lambda **_: backend)
    with pytest.raises(ConnectionError) as raised:
        LocalLLMRunner(server_token_file=tmp_path / "explicit.key")
    assert raised.value is error
    assert backend.closed


def test_constructor_rejects_empty_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = FakeLlama()
    backend.stream_factory = lambda _: iter(())
    monkeypatch.setattr(llama_wrapper, "Llama", lambda **_: backend)
    with pytest.raises(RuntimeError, match="no response"):
        LocalLLMRunner(server_token_file=tmp_path / "explicit.key")
    assert backend.closed


def test_stop_interrupts_waiting_caller_and_prevents_overlapping_generation(
    runner: LocalLLMRunner,
) -> None:
    entered, release, cleaned = Event(), Event(), Event()

    def blocked(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        try:
            entered.set()
            release.wait(3)
            yield {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}
        finally:
            cleaned.set()

    runner._llm.stream_factory = blocked
    results = []
    call = Thread(
        target=lambda: results.append(runner.manual_request("input", "system"))
    )
    call.start()
    try:
        assert entered.wait(1)
        before = monotonic()
        interrupter = Thread(target=runner.stop)
        interrupter.start()
        interrupter.join(0.5)
        call.join(0.5)
        assert not interrupter.is_alive() and not call.is_alive()
        assert monotonic() - before < 1
        assert results[0][1] is False and results[0][4] == "stopped"
        with pytest.raises(RuntimeError, match="still running"):
            runner.manual_request("next", "system")
    finally:
        release.set()
        call.join(1)
    assert cleaned.wait(1)


def test_total_wait_has_timeout_even_without_chunks(runner: LocalLLMRunner) -> None:
    release = Event()

    def blocked(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        release.wait(3)
        yield from ()

    runner._llm.stream_factory = blocked
    runner._timeout = 0.1
    try:
        with pytest.raises(TimeoutError):
            runner.manual_request("input", "system")
    finally:
        release.set()


def test_request_context_is_opt_in_and_failed_generation_does_not_append(
    runner: LocalLLMRunner,
) -> None:
    runner.context_tasks = frozenset({"ANSWER"})
    runner.request("r1", "first", "ANSWER", "chat", system_prompt="system")
    runner.request("r2", "second", "ANSWER", "chat", system_prompt="system")
    assert "first" in runner._llm.calls[-1]["messages"][1]["content"]
    context_id = runner.context_manager.get_id("chat", "ANSWER")
    original = runner.context_manager.get_context(context_id)
    runner._llm.stream_factory = lambda _: iter(())
    with pytest.raises(RuntimeError):
        runner.request("r3", "failed", "ANSWER", "chat", system_prompt="system")
    assert runner.context_manager.get_context(context_id) == original
    runner._llm.stream_factory = normal_stream
    runner.request("r4", "stateless", "GUARD", "chat", system_prompt="system")
    assert runner._llm.calls[-1]["messages"][1]["content"] == "stateless"


def test_missing_prompt_dependency_fails_explicitly(runner: LocalLLMRunner) -> None:
    with pytest.raises(NotImplementedError, match="PromptManager"):
        runner.request("run", "input", "ANSWER", "chat")


def test_regen_keeps_original_input_and_system_prompt(runner: LocalLLMRunner) -> None:
    runner.regen("original", "bad output", "schema error", "system", max_tokens=55)
    call = runner._llm.calls[-1]
    assert call["messages"][0]["content"] == "system"
    body = call["messages"][1]["content"]
    assert body.startswith("original")
    assert "bad output" in body and "schema error" in body
    assert call["max_tokens"] == 55


def test_close_and_invalid_generation_parameters(runner: LocalLLMRunner) -> None:
    with pytest.raises(ValueError):
        runner.manual_request("input", "system", max_tokens=0)
    runner.close()
    with pytest.raises(RuntimeError, match="closed"):
        runner.manual_request("input", "system")
    with pytest.raises(RuntimeError, match="closed"):
        runner.model_load()


def test_log_hook_gets_only_new_input_and_can_be_disabled(
    runner: LocalLLMRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner, "_log_call", lambda text, output: logged.append((text, output))
    )
    runner.context_tasks = frozenset({"ANSWER"})
    runner.request("r1", "old secret context", "ANSWER", "chat", system_prompt="system")
    runner.request("r2", "new question", "ANSWER", "chat", system_prompt="system")
    assert logged == [("old secret context", "OK"), ("new question", "OK")]
    assert "old secret context" in runner._llm.calls[-1]["messages"][1]["content"]
    runner.manual_request("compression source", "system", log_call=False)
    assert len(logged) == 2


def test_failed_wrapper_call_still_reaches_log_hook_without_swallowing_error(
    runner: LocalLLMRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    logged = []
    monkeypatch.setattr(
        runner, "_log_call", lambda text, output: logged.append((text, output))
    )
    error = OSError("synthetic failure")

    def fail(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "partial"}}]}
        raise error

    runner._llm.stream_factory = fail
    with pytest.raises(OSError) as raised:
        runner.manual_request("new input", "system")
    assert raised.value is error
    assert logged == [("new input", "partial")]

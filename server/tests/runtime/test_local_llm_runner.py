"""프록시를 대역으로 바꾸어 키 탐색, 호출, 스레드 중지와 예외 전파를 검증한다."""

import ast
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from queue import Queue
from threading import Event, Thread
from time import monotonic
from types import ModuleType
from typing import Any

import pytest

from app.models.local_llm_outputs import Plan
from app.runtime import local_llm_runner as runner_module
from app.runtime.local_llm_runner import LocalLLMRunner
from app.runtime.local_res_parser import OutputFormatError
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
def runner(monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalLLMRunner]:
    monkeypatch.setattr(llama_wrapper, "Llama", FakeLlama)
    instance = LocalLLMRunner()
    yield instance
    instance.close()


def test_constructor_uses_only_common_llama_arguments(
    runner: LocalLLMRunner,
) -> None:
    assert runner._llm.options == {
        "model_path": "models/gemma.gguf",
        "n_ctx": 32768,
        "n_gpu_layers": -1,
    }
    assert len(runner._llm.calls) == 1
    assert runner.model_load() is True
    assert len(runner._llm.calls) == 1


def test_runner_leaves_key_errors_to_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = FileNotFoundError("synthetic wrapper key error")

    def fail(**_: Any) -> None:
        raise error

    monkeypatch.setattr(llama_wrapper, "Llama", fail)
    with pytest.raises(FileNotFoundError) as raised:
        LocalLLMRunner()
    assert raised.value is error


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
    assert set(call) == {"messages", "stream", "max_tokens"}
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeLlama()
    error = ConnectionError("synthetic connection failure")

    def fail(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        raise error

    backend.stream_factory = fail
    monkeypatch.setattr(llama_wrapper, "Llama", lambda **_: backend)
    with pytest.raises(ConnectionError) as raised:
        LocalLLMRunner()
    assert raised.value is error
    assert backend.closed


def test_constructor_rejects_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeLlama()
    backend.stream_factory = lambda _: iter(())
    monkeypatch.setattr(llama_wrapper, "Llama", lambda **_: backend)
    with pytest.raises(RuntimeError, match="no response"):
        LocalLLMRunner()
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
    runner.request("r4", "stateless", "WRITE", "chat", system_prompt="system")
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


def test_only_import_change_accepts_native_llama_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """임포트 한 줄만 바꾼 모듈을, 전용 인자를 거부하는 네이티브 규격 대역으로 실행."""

    class NativeLlama:
        def __init__(self, model_path: str, *, n_ctx: int, n_gpu_layers: int) -> None:
            assert model_path == "test-model.gguf"
            assert n_ctx == 32768 and n_gpu_layers == -1

        def create_chat_completion(
            self, messages: list[dict[str, str]], *, stream: bool, max_tokens: int
        ) -> Iterator[dict[str, Any]]:
            assert messages and stream and max_tokens > 0
            # 원본 스트림이 usage를 제공하지 않을 때도 정상 응답을 처리한다.
            yield {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}

        def tokenize(self, text: bytes, add_bos: bool, special: bool) -> list[int]:
            assert not add_bos and special
            return list(text)

        def close(self) -> None:
            pass

    native_module = ModuleType("llama_cpp")
    native_module.Llama = NativeLlama
    monkeypatch.setitem(sys.modules, "llama_cpp", native_module)
    tree = ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "src.localLLM_API_proxy.llama_wrapper"
    ]
    assert len(imports) == 1
    imports[0].module = "llama_cpp"
    swapped = ModuleType("app.runtime.native_import_test")
    swapped.__package__ = "app.runtime"
    exec(compile(tree, runner_module.__file__, "exec"), swapped.__dict__)
    with swapped.LocalLLMRunner(
        model_path="test-model.gguf", context_tasks=frozenset({"ANSWER"})
    ) as instance:
        assert instance.manual_request("input", "system") == (
            "OK",
            True,
            None,
            None,
            "stop",
        )
        instance.request("run", "new input", "ANSWER", "chat", system_prompt="system")
        assert instance.context_manager.get_context_length(0) > 0
        assert instance.regen("input", "old", "error", "system")[1]


def test_close_waits_for_receiver_before_freeing_model(
    runner: LocalLLMRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, freed = Event(), Event(), Event()

    def blocked(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        entered.set()
        release.wait(3)
        yield {"choices": [{"delta": {"content": "late"}, "finish_reason": "stop"}]}

    runner._llm.stream_factory = blocked
    monkeypatch.setattr(runner._llm, "close", freed.set)
    results = []
    call = Thread(
        target=lambda: results.append(runner.manual_request("input", "system"))
    )
    closing_thread = Thread(target=runner.close)
    call.start()
    try:
        assert entered.wait(1)
        closing_thread.start()
        call.join(0.5)
        assert not call.is_alive() and results[0][4] == "stopped"
        assert not freed.wait(0.1)
    finally:
        release.set()
        call.join(1)
        if closing_thread.ident is not None:
            closing_thread.join(1)
    assert not closing_thread.is_alive()
    assert freed.is_set()


def test_stop_during_queue_wait_is_not_an_incomplete_stream_error(
    runner: LocalLLMRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StopAtEndQueue(Queue[Any]):
        def get(self, block: bool = True, timeout: float | None = None) -> Any:
            value = super().get(block=block, timeout=timeout)
            if value is None:
                runner.stop()
            return value

    # 수신 대기 중 stop이 도착하고 정상 종료 청크 없이 수신이 끝나는 순서를 재현한다.
    runner._llm.stream_factory = lambda _: iter(
        [{"choices": [{"delta": {"content": "partial"}}]}]
    )
    monkeypatch.setattr(runner_module, "Queue", StopAtEndQueue)
    result = runner.manual_request("input", "system")
    assert result == ("partial", False, None, None, "stopped")


def test_token_callback_runs_before_completion_and_stop_preserves_emitted_text(
    runner: LocalLLMRunner,
) -> None:
    release, emitted, cleaned = Event(), Event(), Event()
    pieces: list[str] = []
    results = []

    def stream(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        try:
            yield {"choices": [{"delta": {"content": "안녕"}}]}
            release.wait(3)
            yield {
                "choices": [{"delta": {"content": "하세요"}, "finish_reason": "stop"}]
            }
        finally:
            cleaned.set()

    def show(piece: str) -> None:
        pieces.append(piece)
        emitted.set()

    runner._llm.stream_factory = stream
    call = Thread(
        target=lambda: results.append(
            runner.manual_request("input", "system", on_token=show)
        )
    )
    call.start()
    try:
        assert emitted.wait(1)
        assert not results  # 완료 전에 첫 본문이 전달되어야 한다.
        runner.stop()
        call.join(0.5)
        assert not call.is_alive()
        assert pieces == ["안녕"]
        assert results[0][0] == "".join(pieces)
        assert results[0][4] == "stopped"
    finally:
        release.set()
        call.join(1)
    assert cleaned.wait(1)


def test_request_and_regen_forward_body_tokens_only(runner: LocalLLMRunner) -> None:
    def stream(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"role": "assistant"}}]}
        yield {"choices": [{"delta": {"content": "A"}}]}
        yield {"choices": [{"delta": {"content": "B"}, "finish_reason": "stop"}]}
        yield {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}

    runner._llm.stream_factory = stream
    pieces: list[str] = []
    result = runner.request(
        "run", "input", "ANSWER", "chat", system_prompt="system", on_token=pieces.append
    )
    assert pieces == ["A", "B"]
    assert result == ("AB", True, 7, 2, "stop")
    assert set(runner._llm.calls[-1]) == {"messages", "stream", "max_tokens"}
    pieces.clear()
    assert (
        runner.regen("input", "old", "error", "system", on_token=pieces.append)[0]
        == "AB"
    )
    assert pieces == ["A", "B"]


def test_callback_exception_propagates_and_keeps_received_output_for_log(
    runner: LocalLLMRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = OSError("synthetic display error")
    logged = []
    monkeypatch.setattr(
        runner, "_log_call", lambda text, output: logged.append((text, output))
    )

    def show(_: str) -> None:
        raise error

    with pytest.raises(OSError) as raised:
        runner.manual_request("input", "system", on_token=show)
    assert raised.value is error
    assert logged == [("input", "OK")]


@pytest.fixture
def generated_outputs(runner: LocalLLMRunner) -> list[str | Exception]:
    outputs: list[str | Exception] = []

    def stream(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        output = outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        yield {"choices": [{"delta": {"content": output}, "finish_reason": "stop"}]}
        yield {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}}

    runner._llm.stream_factory = stream
    return outputs


@pytest.mark.parametrize("failures", [0, 1, 2])
def test_request_retries_only_invalid_outputs_and_sums_usage(
    runner: LocalLLMRunner, generated_outputs: list[str | Exception], failures: int
) -> None:
    generated_outputs.extend(["not json"] * failures + ['{"hits":[0,2]}'])
    result = runner.request(
        "run", "original", "GUARD", "chat", system_prompt="system", max_tokens=55
    )
    assert result == ([0, 2], True, 7 * (failures + 1), 2 * (failures + 1), "stop")
    assert len(runner._llm.calls) == failures + 2  # 생성자 검사 1회는 별도다.
    assert not generated_outputs
    for call in runner._llm.calls[1:]:
        assert call["messages"][0]["content"] == "system"
        assert call["messages"][1]["content"].startswith("original")
        assert call["max_tokens"] == 55
    if failures:
        assert "not json" in runner._llm.calls[-1]["messages"][1]["content"]
        assert "Format error:" in runner._llm.calls[-1]["messages"][1]["content"]


def test_third_format_failure_returns_failure_without_fourth_generation_or_context_change(
    runner: LocalLLMRunner, generated_outputs: list[str | Exception]
) -> None:
    runner.context_tasks = frozenset({"JUDGE"})
    context_id = runner.context_manager.get_id("chat", "JUDGE")
    runner.context_manager.append(context_id, "original history")
    generated_outputs.extend(["not json", "{}", '{"enough":true}', "unused"])
    result = runner.request("run", "new input", "JUDGE", "chat", system_prompt="system")
    assert result == (None, False, 21, 6, "parse_failed")
    assert len(runner._llm.calls) == 4
    assert generated_outputs == ["unused"]
    assert runner.context_manager.get_context(context_id) == "original history"


def test_context_receives_only_valid_final_raw_response(
    runner: LocalLLMRunner, generated_outputs: list[str | Exception]
) -> None:
    runner.context_tasks = frozenset({"JUDGE"})
    valid = '{"enough":true,"needConsent":false,"consentItems":[],"need":[]}'
    generated_outputs.extend(["bad output", valid])
    result = runner.request("run", "input", "JUDGE", "chat", system_prompt="system")
    assert isinstance(result[0], Plan)
    assert result[0].enough is True
    assert (
        runner.context_manager.get_context(0) == f"\nUser: input\nAssistant: {valid}\n"
    )


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("proxy"),
        ValueError("wrapper"),
        OutputFormatError("raised by wrapper"),
    ],
)
def test_request_does_not_catch_wrapper_errors_even_of_matching_type(
    runner: LocalLLMRunner, generated_outputs: list[str | Exception], error: Exception
) -> None:
    generated_outputs.append(error)
    with pytest.raises(type(error)) as raised:
        runner.request("run", "input", "GUARD", "chat", system_prompt="system")
    assert raised.value is error
    assert len(runner._llm.calls) == 2


def test_parser_programming_error_is_not_retried(
    runner: LocalLLMRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = TypeError("parser programming error")

    def fail(*_: object) -> None:
        raise error

    monkeypatch.setattr(runner_module, "parser", fail)
    with pytest.raises(TypeError) as raised:
        runner.request("run", "input", "ANSWER", "chat", system_prompt="system")
    assert raised.value is error
    assert len(runner._llm.calls) == 2


def test_stop_during_format_retry_does_not_parse_retry_or_append(
    runner: LocalLLMRunner,
) -> None:
    emitted, release, cleaned = Event(), Event(), Event()
    results = []
    runner.context_tasks = frozenset({"GUARD"})

    def stream(_: dict[str, Any]) -> Iterator[dict[str, Any]]:
        if len(runner._llm.calls) == 2:
            yield {"choices": [{"delta": {"content": "bad"}, "finish_reason": "stop"}]}
            return
        try:
            yield {"choices": [{"delta": {"content": '{"hits":['}}]}
            release.wait(3)
            yield {"choices": [{"delta": {"content": "0]}"}, "finish_reason": "stop"}]}
        finally:
            cleaned.set()

    def show(piece: str) -> None:
        if piece == '{"hits":[':
            emitted.set()

    runner._llm.stream_factory = stream
    call = Thread(
        target=lambda: results.append(
            runner.request(
                "run", "input", "GUARD", "chat", system_prompt="system", on_token=show
            )
        )
    )
    call.start()
    try:
        assert emitted.wait(1)
        runner.stop()
        call.join(0.5)
        assert not call.is_alive()
        assert results == [('{"hits":[', False, None, None, "stopped")]
        assert len(runner._llm.calls) == 3
        assert runner.context_manager.get_context(0) == ""
    finally:
        release.set()
        call.join(1)
    assert cleaned.wait(1)

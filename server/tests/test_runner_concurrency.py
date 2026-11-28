"""실제 원격 wrapper와 Runner를 연결해 동시 수신·취소·장애 복구를 검증한다."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event, Lock
from time import monotonic
from unittest.mock import Mock

import httpx
import pytest

from app.models.enums import PromptIdx
from app.runtime.context_manager import ContextManager
from app.runtime.local_llm_runner import LocalLLMRunner
from src.localLLM_API_proxy import llama_wrapper as wrapper


class ControlledStream(httpx.AsyncByteStream):
    def __init__(
        self, server: ModelServer, text: str, *, tokenize: bool = False
    ) -> None:
        self.server = server
        self.text = text
        self.tokenize = tokenize
        self.released = Event()
        self.closed = Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with self.server.lock:
            self.server.active += 1
            self.server.peak = max(self.server.peak, self.server.active)
        try:
            if self.text == "trickle-tokenize":
                self.server.started.put(self)
                while not self.released.is_set():
                    yield b" "
                    await asyncio.sleep(0.01)
                yield b'{"tokens": [1]}'
                return
            if self.text.startswith("block"):
                self.server.started.put(self)
                while not self.released.is_set():
                    await asyncio.sleep(0.005)
                if self.closed.is_set():
                    return
            if self.text == "disconnect":
                raise httpx.ReadError("simulated disconnect")
            if self.text == "partial-disconnect":
                yield self.chunk("partial", finish=None)
                raise httpx.ReadError("simulated mid-stream disconnect")
            if self.tokenize:
                yield json.dumps({"tokens": [1]}).encode()
            else:
                yield self.chunk('{"needs": ["item"]}')
                yield b"data: [DONE]\n\n"
        finally:
            with self.server.lock:
                self.server.active -= 1

    @staticmethod
    def chunk(text: str, finish: str | None = "stop") -> bytes:
        body = {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}
        return ("data: " + json.dumps(body) + "\n\n").encode()

    async def aclose(self) -> None:
        self.closed.set()
        self.released.set()


class ModelServer:
    def __init__(self) -> None:
        self.lock = Lock()
        self.started: Queue[ControlledStream] = Queue()
        self.active = 0
        self.peak = 0
        self.fail_next = False
        self.block_context_initialization = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            if self.block_context_initialization and body["content"].startswith(
                "Summarize the conversation"
            ):
                return httpx.Response(
                    200,
                    stream=ControlledStream(
                        self, "block-context-initialization", tokenize=True
                    ),
                )
            if body["content"].startswith("header-tokenize"):
                await self.wait_for_headers(body["content"])
            if body["content"] in {"block-tokenize", "trickle-tokenize"}:
                return httpx.Response(
                    200, stream=ControlledStream(self, body["content"], tokenize=True)
                )
            return httpx.Response(
                200, json={"tokens": list(range(len(body["content"])))}
            )
        assert request.url.path == "/v1/chat/completions"
        with self.lock:
            fail = self.fail_next
            self.fail_next = False
        text = "disconnect" if fail else body["messages"][-1]["content"]
        if text.startswith("header-"):
            await self.wait_for_headers(text)
        return httpx.Response(200, stream=ControlledStream(self, text))

    async def wait_for_headers(self, text: str) -> None:
        pending = ControlledStream(self, text)
        self.started.put(pending)
        try:
            while not pending.released.is_set():
                await asyncio.sleep(0.005)
        finally:
            pending.closed.set()


@pytest.fixture
def remote_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[LocalLLMRunner, ModelServer]]:
    server = ModelServer()
    client = wrapper.Llama(
        backend="remote",
        server_url="https://example.invalid",
        server_token="fake-test-key",
        transport=httpx.MockTransport(server),
    )
    monkeypatch.setattr(wrapper, "Llama", Mock(return_value=client))
    with LocalLLMRunner(timeout=1) as runner:
        yield runner, server


def test_remote_requests_overlap_and_stop_is_isolated(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    with ThreadPoolExecutor(max_workers=5) as pool:
        requests = {
            i: pool.submit(runner.manual_request, f"block-{i}", "system", run_id=i)
            for i in range(1, 5)
        }
        streams = {
            stream.text: stream
            for stream in (server.started.get(timeout=2) for _ in range(4))
        }
        try:
            queued = pool.submit(runner.manual_request, "block-5", "system", run_id=5)
            with pytest.raises(Empty):
                server.started.get(timeout=0.1)
            runner.stop(5)
            assert queued.result(timeout=2)[4] == "stopped"
            runner.stop(1)
            assert requests[1].result(timeout=2)[4] == "stopped"
            assert streams["block-1"].closed.wait(1)
            assert not any(streams[f"block-{i}"].closed.is_set() for i in (2, 3, 4))
            assert server.peak == 4
        finally:
            for stream in streams.values():
                stream.released.set()
        assert all(requests[i].result(timeout=2)[1] for i in (2, 3, 4))


def test_same_run_remains_serial(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.manual_request, "block-first", "system", run_id=1)
        stream = server.started.get(timeout=2)
        second = pool.submit(runner.manual_request, "block-second", "system", run_id=1)
        try:
            with pytest.raises(Empty):
                server.started.get(timeout=0.1)
            assert server.peak == 1
        finally:
            stream.released.set()
        next_stream = server.started.get(timeout=2)
        next_stream.released.set()
        assert first.result(timeout=2)[1] and second.result(timeout=2)[1]


def test_manual_requests_without_run_ids_can_overlap(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [
            pool.submit(runner.manual_request, f"block-{i}", "system") for i in (1, 2)
        ]
        streams = [server.started.get(timeout=2) for _ in range(2)]
        for stream in streams:
            stream.released.set()
        assert all(request.result(timeout=2)[1] for request in requests)
        assert server.peak == 2


def test_nested_context_compaction_does_not_acquire_another_request_slot(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, _ = remote_runner
    runner._max_concurrent_requests = 1
    manager = ContextManager(runner, 1, compaction_limit=80, compaction_target=10)
    runner.context_managers[1] = manager
    context_id = manager.get_id(1, PromptIdx.NEEDS)
    assert manager.append(context_id, "x" * 50)
    result = runner.request(
        1, "normal", PromptIdx.NEEDS, 1, use_context=True, system_prompt="system"
    )
    assert result[:2] == (["item"], True)
    assert "x" * 50 not in manager.get_context(context_id)


def test_slow_context_initialization_does_not_block_existing_context_or_stop(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    runner._timeout = 5
    existing = runner.get_context_manager(2)
    server.block_context_initialization = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        initializing = pool.submit(runner.get_context_manager, 1)
        initialization_stream = server.started.get(timeout=1)
        other_stream = None
        try:
            # 생성 중인 실행과 무관한 기존 객체 조회는 즉시 끝나야 한다.
            assert (
                pool.submit(runner.get_context_manager, 2).result(timeout=1) is existing
            )
            request = pool.submit(
                runner.request,
                2,
                "block-generation",
                PromptIdx.NEEDS,
                1,
                use_context=True,
                system_prompt="system",
            )
            other_stream = server.started.get(timeout=1)
            runner.stop(2)
            assert request.result(timeout=1)[4] == "stopped"
            assert other_stream.closed.wait(1)
            assert not initializing.done()
            assert not initialization_stream.closed.is_set()
        finally:
            initialization_stream.released.set()
            if other_stream is not None:
                other_stream.released.set()
        assert initializing.result(timeout=1) is runner.get_context_manager(1)


def test_different_runs_can_initialize_contexts_concurrently(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    runner._timeout = 5
    server.block_context_initialization = True
    streams: list[ControlledStream] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.get_context_manager, 1)
        streams.append(server.started.get(timeout=1))
        second = pool.submit(runner.get_context_manager, 2)
        try:
            streams.append(server.started.get(timeout=1))
            # 생성자가 끝나기 전의 객체는 외부에 등록하지 않는다.
            assert 1 not in runner.context_managers
            assert 2 not in runner.context_managers
        finally:
            server.block_context_initialization = False
            for stream in streams:
                stream.released.set()
        assert first.result(timeout=1) is runner.get_context_manager(1)
        assert second.result(timeout=1) is runner.get_context_manager(2)


def test_same_run_context_is_initialized_once(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    runner._timeout = 5
    server.block_context_initialization = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.get_context_manager, 1)
        stream = server.started.get(timeout=1)
        second = pool.submit(runner.get_context_manager, 1)
        try:
            with pytest.raises(Empty):
                server.started.get(timeout=0.1)
            assert 1 not in runner.context_managers
        finally:
            server.block_context_initialization = False
            stream.released.set()
        assert first.result(timeout=1) is second.result(timeout=1)


def test_failed_context_initialization_can_be_retried(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    runner._timeout = 0.1
    server.block_context_initialization = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        initializing = pool.submit(runner.get_context_manager, 1)
        stream = server.started.get(timeout=1)
        try:
            with pytest.raises(TimeoutError):
                initializing.result(timeout=1)
            assert stream.closed.wait(1)
            assert 1 not in runner.context_managers
        finally:
            server.block_context_initialization = False
            stream.released.set()
    manager = runner.get_context_manager(1)
    assert manager is runner.get_context_manager(1)
    assert manager.run_id == 1


@pytest.mark.parametrize("text", ["disconnect", "partial-disconnect"])
def test_disconnect_preserves_shared_client_and_next_request_recovers(
    remote_runner: tuple[LocalLLMRunner, ModelServer], text: str
) -> None:
    runner, server = remote_runner
    with ThreadPoolExecutor(max_workers=2) as pool:
        other = pool.submit(runner.manual_request, "block-other", "system", run_id=1)
        stream = server.started.get(timeout=2)
        try:
            with pytest.raises(httpx.ReadError):
                runner.manual_request(text, "system", run_id=2)
            assert runner.manual_request("normal", "system", run_id=3)[1]
            assert not stream.closed.is_set()
        finally:
            stream.released.set()
        assert other.result(timeout=2)[1]


def test_parsed_request_retries_transient_disconnect(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    server.fail_next = True
    result = runner.request(1, "normal", PromptIdx.NEEDS, 1, system_prompt="system")
    assert result[:2] == (["item"], True)


@pytest.mark.parametrize("text", ["block-generation", "block-tokenize"])
def test_timeout_closes_only_timed_out_response_and_releases_capacity(
    remote_runner: tuple[LocalLLMRunner, ModelServer], text: str
) -> None:
    runner, server = remote_runner
    runner._max_concurrent_requests = 1
    runner._timeout = 0.1
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(runner.manual_request, text, "system", run_id=1)
        stream = server.started.get(timeout=2)
        with pytest.raises(TimeoutError):
            request.result(timeout=2)
        assert stream.closed.wait(1)
    assert runner.manual_request("normal", "system", run_id=2)[1]


def test_close_cancels_all_receivers(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [
            pool.submit(runner.manual_request, f"block-{i}", "system", run_id=i)
            for i in (1, 2)
        ]
        streams = [server.started.get(timeout=2) for _ in range(2)]
        runner.close()
        assert all(stream.closed.is_set() for stream in streams)
        assert all(request.result(timeout=2)[4] == "stopped" for request in requests)


@pytest.mark.parametrize("native", [False, True])
def test_flag_off_or_native_backend_preserves_serial_requests(
    monkeypatch: pytest.MonkeyPatch, native: bool
) -> None:
    monkeypatch.setattr(wrapper, "EXPERIMENTAL_BUILD", native)
    server = ModelServer()
    client = wrapper.Llama(
        backend="remote",
        server_url="https://example.invalid",
        server_token="fake-test-key",
        transport=httpx.MockTransport(server),
    )
    if native:
        # Runner의 네이티브 분기를 검증한다. 실제 GPU 대신 같은 제어 가능한 전송 대역 사용.
        client.backend = "local"
    monkeypatch.setattr(wrapper, "Llama", Mock(return_value=client))
    with LocalLLMRunner(timeout=1) as runner, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.manual_request, "block-first", "system", run_id=1)
        stream = server.started.get(timeout=2)
        second = pool.submit(runner.manual_request, "block-second", "system", run_id=2)
        try:
            with pytest.raises(Empty):
                server.started.get(timeout=0.1)
        finally:
            stream.released.set()
        next_stream = server.started.get(timeout=2)
        next_stream.released.set()
        assert first.result(timeout=2)[1] and second.result(timeout=2)[1]
        assert server.peak == 1


@pytest.mark.parametrize("text", ["header-generation", "header-tokenize"])
def test_stop_before_headers_releases_slot_and_preserves_other_request(
    remote_runner: tuple[LocalLLMRunner, ModelServer], text: str
) -> None:
    runner, server = remote_runner
    runner._timeout = 5
    runner._max_concurrent_requests = 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        other = pool.submit(runner.manual_request, "block-other", "system", run_id=2)
        other_stream = server.started.get(timeout=1)
        stopped = pool.submit(runner.manual_request, text, "system", run_id=1)
        pending = server.started.get(timeout=1)
        try:
            runner.stop(1)
            assert stopped.result(timeout=1)[4] == "stopped"
            assert pending.closed.wait(1)
            # 새 요청은 취소한 수신 슬롯을 재사용해야 한다.
            assert runner.manual_request("normal", "system", run_id=3)[1]
            assert not other_stream.closed.is_set()
        finally:
            pending.released.set()
            other_stream.released.set()
        assert other.result(timeout=1)[1]


def test_all_header_waits_can_be_cancelled_without_exhausting_capacity(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    runner._timeout = 5
    with ThreadPoolExecutor(max_workers=4) as pool:
        requests = [
            pool.submit(runner.manual_request, f"header-{i}", "system", run_id=i)
            for i in range(4)
        ]
        pending = [server.started.get(timeout=1) for _ in requests]
        try:
            for i in range(4):
                runner.stop(i)
            assert all(
                request.result(timeout=1)[4] == "stopped" for request in requests
            )
            assert all(wait.closed.wait(1) for wait in pending)
            assert runner.manual_request("normal", "system", run_id=5)[1]
            with runner._state_lock:
                assert not runner._receivers
        finally:
            for wait in pending:
                wait.released.set()


@pytest.mark.parametrize("direct", [False, True])
def test_header_timeout_releases_capacity(
    remote_runner: tuple[LocalLLMRunner, ModelServer], direct: bool
) -> None:
    runner, server = remote_runner
    runner._timeout = 0.1
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = (
            pool.submit(runner.tokenize, "header-tokenize")
            if direct
            else pool.submit(runner.manual_request, "header-generation", "system")
        )
        pending = server.started.get(timeout=1)
        try:
            with pytest.raises(TimeoutError):
                request.result(timeout=1)
            assert pending.closed.wait(1)
            assert runner.manual_request("normal", "system")[1]
        finally:
            pending.released.set()


@pytest.mark.parametrize("text", ["header-tokenize", "block-tokenize"])
@pytest.mark.parametrize("system_prompt", [False, True])
def test_stop_interrupts_context_preparation_tokenization(
    remote_runner: tuple[LocalLLMRunner, ModelServer], text: str, system_prompt: bool
) -> None:
    runner, server = remote_runner
    runner._timeout = 5
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            runner.request,
            1,
            "normal" if system_prompt else text,
            PromptIdx.NEEDS,
            1,
            use_context=True,
            system_prompt=text if system_prompt else "system",
        )
        pending = server.started.get(timeout=1)
        try:
            runner.stop(1)
            assert request.result(timeout=1)[4] == "stopped"
            assert pending.closed.wait(1)
            assert not runner._tokenizers
            assert runner.manual_request("normal", "system", run_id=1)[1]
        finally:
            pending.released.set()


def test_direct_tokenization_has_total_deadline_despite_continuous_chunks(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
) -> None:
    runner, server = remote_runner
    runner._timeout = 0.1
    with ThreadPoolExecutor(max_workers=1) as pool:
        started = monotonic()
        request = pool.submit(runner.tokenize, "trickle-tokenize")
        stream = server.started.get(timeout=1)
        try:
            with pytest.raises(TimeoutError):
                request.result(timeout=1)
            assert monotonic() - started < 1
            assert stream.closed.wait(1)
            assert not runner._tokenizers
            assert runner.tokenize("normal")
        finally:
            stream.released.set()


@pytest.mark.parametrize("direct", [False, True])
def test_close_cancels_header_waits_and_stops_http_loop(
    remote_runner: tuple[LocalLLMRunner, ModelServer], direct: bool
) -> None:
    runner, server = remote_runner
    runner._timeout = 5
    with ThreadPoolExecutor(max_workers=2) as pool:
        request = (
            pool.submit(runner.tokenize, "header-tokenize")
            if direct
            else pool.submit(runner.manual_request, "header-generation", "system")
        )
        pending = server.started.get(timeout=1)
        try:
            closing = pool.submit(runner.close)
            closing.result(timeout=1)
            assert pending.closed.is_set()
            assert not runner._llm._http._thread.is_alive()
            if direct:
                with pytest.raises(TimeoutError):
                    request.result(timeout=1)
            else:
                assert request.result(timeout=1)[4] == "stopped"
        finally:
            pending.released.set()


@pytest.mark.parametrize("before_generation", [False, True])
def test_stop_defers_until_context_compaction_and_save_finish(
    remote_runner: tuple[LocalLLMRunner, ModelServer],
    monkeypatch: pytest.MonkeyPatch,
    before_generation: bool,
) -> None:
    runner, server = remote_runner
    runner._timeout = 5
    manager = ContextManager(runner, 1, compaction_limit=80, compaction_target=10)
    runner.context_managers[1] = manager
    context_id = manager.get_id(1, PromptIdx.NEEDS)
    assert manager.append(context_id, "block-" + "x" * 44)
    if before_generation:
        monkeypatch.setattr(runner, "get_input_limit", Mock(return_value=50))
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            runner.request,
            1,
            "normal",
            PromptIdx.NEEDS,
            1,
            use_context=True,
            system_prompt="system",
        )
        stream = server.started.get(timeout=1)
        try:
            runner.stop(1)
            assert not request.done()
            assert not stream.closed.is_set()
        finally:
            stream.released.set()
        assert request.result(timeout=1)[4] == "stopped"
        expected = '{"needs": ["item"]}'
        if not before_generation:
            expected += '\nUser: normal\nAssistant: {"needs": ["item"]}\n'
        assert manager.get_context(context_id) == expected

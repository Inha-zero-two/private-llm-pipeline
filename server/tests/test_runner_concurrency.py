"""실제 원격 wrapper와 Runner를 연결해 동시 수신·취소·장애 복구를 검증한다."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event, Lock
from unittest.mock import Mock

import httpx
import pytest

from app.models.enums import PromptIdx
from app.runtime.context_manager import ContextManager
from app.runtime.local_llm_runner import LocalLLMRunner
from src.localLLM_API_proxy import llama_wrapper as wrapper


class ControlledStream(httpx.SyncByteStream):
    def __init__(
        self, server: ModelServer, text: str, *, tokenize: bool = False
    ) -> None:
        self.server = server
        self.text = text
        self.tokenize = tokenize
        self.released = Event()
        self.closed = Event()

    def __iter__(self) -> Iterator[bytes]:
        with self.server.lock:
            self.server.active += 1
            self.server.peak = max(self.server.peak, self.server.active)
        try:
            if self.text.startswith("block"):
                self.server.started.put(self)
                assert self.released.wait(3), "Test stream was not released"
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

    def close(self) -> None:
        self.closed.set()
        self.released.set()


class ModelServer:
    def __init__(self) -> None:
        self.lock = Lock()
        self.started: Queue[ControlledStream] = Queue()
        self.active = 0
        self.peak = 0
        self.fail_next = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            if body["content"] == "block-tokenize":
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
        return httpx.Response(200, stream=ControlledStream(self, text))


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

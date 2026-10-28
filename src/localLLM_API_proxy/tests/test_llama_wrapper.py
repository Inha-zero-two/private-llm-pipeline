"""Public call, wire-format, routing, and single-file compatibility contracts."""

from __future__ import annotations

import inspect
import json
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

import llama_wrapper as wrapper


def test_existing_call_signatures() -> None:
    expected = json.loads(Path(__file__).with_name("public_api.json").read_text())
    for name, parameters in expected.items():
        target = wrapper
        for part in name.split("."):
            target = getattr(target, part)
        actual = [
            [
                param.name,
                param.kind.name,
                None
                if param.default is inspect.Parameter.empty
                else repr(param.default),
            ]
            for param in inspect.signature(target).parameters.values()
        ]
        assert actual == parameters, name
    assert wrapper.Llama is wrapper.AdaptiveLlama


def test_frozen_routing_records_keep_positional_construction() -> None:
    device = wrapper.CudaDevice(0, "test GPU", wrapper.MIN_VRAM_BYTES)
    info = wrapper.RoutingInfo("local", "reason", (device,), 0)
    assert asdict(info)["devices"][0]["name"] == "test GPU"
    with pytest.raises(FrozenInstanceError):
        info.backend = "remote"


@pytest.mark.parametrize(
    ("system", "vram", "gpu", "expected"),
    [
        ("Darwin", 16, None, "remote"),
        ("Linux", 6, None, "remote"),
        ("Linux", 7, None, "local"),
        ("Windows", 8, 0, "local"),
        ("Linux", 8, 1, "remote"),
    ],
)
def test_backend_selection(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    vram: int,
    gpu: int | None,
    expected: str,
) -> None:
    monkeypatch.setattr(wrapper.platform, "system", lambda: system)
    monkeypatch.setattr(
        wrapper,
        "detect_cuda_devices",
        lambda: (wrapper.CudaDevice(0, "GPU", vram * 1024**3),),
    )
    monkeypatch.setattr(wrapper, "_native_cuda_available", lambda: True)
    assert wrapper.choose_backend(main_gpu=gpu).backend == expected


def test_cuda_failure_does_not_silently_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wrapper.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        wrapper,
        "detect_cuda_devices",
        lambda: (wrapper.CudaDevice(0, "GPU", wrapper.MIN_VRAM_BYTES),),
    )
    monkeypatch.setattr(wrapper, "_native_cuda_available", lambda: False)
    with pytest.raises(RuntimeError, match="no usable CUDA backend"):
        wrapper.choose_backend()


@pytest.mark.parametrize("pretrained", [False, True])
def test_local_native_forwarding(
    monkeypatch: pytest.MonkeyPatch, pretrained: bool
) -> None:
    native_llama = Mock()
    native_llama.from_pretrained = Mock()
    native = SimpleNamespace(Llama=native_llama, LLAMA_SPLIT_MODE_NONE=0)
    monkeypatch.setattr(wrapper, "_load_native_module", lambda: native)
    monkeypatch.setattr(
        wrapper,
        "choose_backend",
        lambda *args: wrapper.RoutingInfo("local", "GPU", (), 2),
    )
    monkeypatch.setattr(
        wrapper, "resolve_server_token", Mock(side_effect=AssertionError)
    )
    if pretrained:
        result = wrapper.Llama.from_pretrained(
            "repo/model", filename="model.gguf", n_ctx=1024, seed=17
        )
        call = native_llama.from_pretrained
        assert call.call_args.kwargs["repo_id"] == "repo/model"
        assert call.call_args.kwargs["filename"] == "model.gguf"
    else:
        result = wrapper.Llama(Path("model.gguf"), n_ctx=1024, seed=17)
        call = native_llama
        assert call.call_args.kwargs["model_path"] == "model.gguf"
    assert result is call.return_value
    assert call.call_args.kwargs["n_ctx"] == 1024
    assert call.call_args.kwargs["n_gpu_layers"] == -1
    assert call.call_args.kwargs["main_gpu"] == 2
    assert call.call_args.kwargs["split_mode"] == 0
    assert call.call_args.kwargs["seed"] == 17
    assert "server_token" not in call.call_args.kwargs


def remote_client(
    body: Any, *, status: int = 200, sse: str | None = None, **kwargs: Any
) -> tuple[wrapper.AdaptiveLlama, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if sse is not None:
            return httpx.Response(
                status, text=sse, headers={"Content-Type": "text/event-stream"}
            )
        return httpx.Response(status, json=body)

    client = wrapper.Llama(
        backend="remote",
        server_url="https://example.invalid/v1/",
        server_token="test-key",
        transport=httpx.MockTransport(handle),
        **kwargs,
    )
    return client, requests


def test_chat_preserves_dict_and_extension_fields() -> None:
    body = {"choices": [{"message": {"content": "ok"}}], "custom_field": {"x": 1}}
    client, requests = remote_client(body, seed=5)
    with client:
        result = client.create_chat_completion(
            [{"role": "user", "content": "hello"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
            max_tokens=22,
            stop="END",
            chat_template_kwargs={"enable_thinking": False},
        )
    assert type(result) is dict
    assert result == body
    request = requests[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://example.invalid/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer test-key"
    assert payload["max_tokens"] == 22
    assert payload["stop"] == ["END"]
    assert payload["seed"] == 5
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["cache_prompt"] is True


def test_completion_call_and_positional_arguments() -> None:
    client, requests = remote_client(
        {"content": "world", "tokens_evaluated": 2, "tokens_predicted": 1}
    )
    with client:
        result = client("hello ", None, 3, echo=True, seed=-1)
    assert result["choices"][0] == {
        "text": "hello world",
        "index": 0,
        "logprobs": None,
        "finish_reason": "stop",
    }
    assert result["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
    }
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/completion"
    assert payload["n_predict"] == 3
    assert payload["seed"] == 0xFFFFFFFF


def test_infill_keeps_endpoint_and_suffix() -> None:
    client, requests = remote_client({"content": "middle"})
    with client:
        client.create_completion("prefix", suffix="suffix")
    payload = json.loads(requests[0].content)
    assert requests[0].url.path == "/infill"
    assert (payload["input_prefix"], payload["input_suffix"], payload["prompt"]) == (
        "prefix",
        "suffix",
        "",
    )


@pytest.mark.parametrize("chat", [False, True])
def test_stream_chunks_and_done(chat: bool) -> None:
    first = (
        {"choices": [{"delta": {"content": "hello"}}]}
        if chat
        else {"content": "hello", "stop": False}
    )
    second = (
        {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        if chat
        else {"content": "", "stop": True}
    )
    sse = f": ping\ndata: {json.dumps(first)}\n\ndata: {json.dumps(second)}\n\ndata: [DONE]\n\n"
    client, requests = remote_client(None, sse=sse)
    with client:
        result = (
            client.create_chat_completion([], stream=True)
            if chat
            else client("prompt", stream=True)
        )
        assert not isinstance(result, dict)
        chunks = list(result)
    assert len(chunks) == 2
    if chat:
        assert chunks == [first, second]
    else:
        assert chunks[0]["choices"][0]["text"] == "hello"
        assert chunks[1]["choices"][0]["finish_reason"] == "stop"
        assert chunks[0]["id"] == chunks[1]["id"]
    assert json.loads(requests[0].content)["stream"] is True


@pytest.mark.parametrize("stream", [False, True])
def test_remote_errors(stream: bool) -> None:
    client, _ = remote_client(
        {"error": {"message": "rejected"}},
        status=200 if stream else 401,
        sse='data: {"error":"rejected"}\n\n' if stream else None,
    )
    with client, pytest.raises(wrapper.RemoteServerError, match="rejected") as error:
        result = client.create_chat_completion([], stream=stream)
        if stream:
            list(result)
    assert error.value.status_code == (None if stream else 401)


def test_embedding_input_keyword_is_preserved() -> None:
    body = {"data": [{"embedding": [0.1, 0.2]}]}
    client, requests = remote_client(body)
    with client:
        assert client.create_embedding(input=["a", "b"], model="test") == body
    assert json.loads(requests[0].content) == {"input": ["a", "b"], "model": "test"}


def test_tokenize_and_detokenize_keep_bytes_contract() -> None:
    client, requests = remote_client({"tokens": [1, 2]})
    with client:
        assert client.tokenize("한글".encode(), add_bos=False, special=True) == [1, 2]
        with pytest.raises(TypeError, match="text must be bytes"):
            client.tokenize("text")
    assert json.loads(requests[0].content) == {
        "content": "한글",
        "add_special": False,
        "parse_special": True,
    }
    client, requests = remote_client({"content": "한글"})
    with client:
        assert client.detokenize([1, 2]) == "한글".encode()


def test_remote_pretrained_skips_native_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wrapper, "_load_native_module", Mock(side_effect=AssertionError)
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "ok"})
    )
    with wrapper.Llama.from_pretrained(
        "nonexistent/repo",
        filename="missing.gguf",
        backend="remote",
        server_token="test-key",
        transport=transport,
    ) as client:
        assert client.model_path is None
        assert client.health() == {"status": "ok"}


def test_lifecycle_and_unsupported_callbacks() -> None:
    client, _ = remote_client({})
    with client:
        with pytest.raises(
            wrapper.UnsupportedRemoteOperation, match="logits_processor"
        ):
            client("prompt", logits_processor=object())
        with pytest.raises(wrapper.UnsupportedRemoteOperation):
            client.save_state()
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        client("prompt")


def test_key_discovery_and_explicit_token_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wrapper, "execution_directory", lambda: tmp_path)
    (tmp_path / "keys").mkdir()
    key = tmp_path / "keys/llama_api_key_01.key"
    key.write_text("\ufefffake-key\n", encoding="utf-8")
    assert wrapper.resolve_server_token() == ("fake-key", key)
    assert wrapper.resolve_server_token(
        server_token_file="keys/llama_api_key_01.key"
    ) == ("fake-key", key)
    assert wrapper.resolve_server_token("explicit", "missing.key") == ("explicit", None)
    with pytest.raises(ValueError, match="single line"):
        wrapper.resolve_server_token("bad\nkey")

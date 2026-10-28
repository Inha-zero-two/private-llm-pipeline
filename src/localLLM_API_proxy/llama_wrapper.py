"""내부 테스트용 단일 파일 Llama 어댑터.

적격 로컬 CUDA에서는 원본 llama_cpp.Llama 인스턴스를 직접 반환한다.
그 외 환경에서는 고정된 서버와 실행 파일 주변의 키 파일을 사용한다.
"""

from __future__ import annotations

import __main__
import ctypes
import fnmatch
import importlib
import json
import math
import os
import platform
import stat
import subprocess
import sys
import time
import uuid
import warnings
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Iterator, NoReturn
from urllib.parse import urlsplit, urlunsplit

import httpx

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion, ChatCompletionChunk


# 실행 파일 주변의 인증키 탐색
KEY_FILE_PATTERN = "llama_api_key*.key"
_EXCLUDED_DIRECTORIES = {".venv", "venv", ".git", "node_modules", "__pycache__"}


def _script_path(value: object) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("<") or text.startswith("-"):
        return None
    return Path(text).resolve()


def execution_directory() -> Path:
    """실행 파일의 폴더를 반환하고 대화형 실행일 때만 현재 폴더를 사용한다."""
    main_path = _script_path(getattr(__main__, "__file__", None))
    if main_path is not None:
        return main_path.parent
    argv_path = _script_path(sys.argv[0] if sys.argv else None)
    if argv_path is not None and not argv_path.is_relative_to(
        Path(sys.prefix).resolve()
    ):
        return argv_path.parent
    return Path.cwd().resolve()


def _is_link(path: Path) -> bool:
    # Windows 정션도 따라가지 않아 외부 폴더 탐색과 순환을 막는다.
    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def find_token_file(root: str | Path) -> Path | None:
    """직계 파일부터 확인하고 하위 폴더를 깊이·경로·파일명 순서로 탐색한다."""
    root = Path(root).resolve()
    directories = deque([root])
    while directories:
        directory = directories.popleft()
        try:
            if _is_link(directory):
                continue
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        children = []
        for path in entries:
            try:
                if _is_link(path):
                    continue
                if path.is_file() and fnmatch.fnmatchcase(path.name, KEY_FILE_PATTERN):
                    return path.resolve()
                if path.is_dir() and path.name.casefold() not in _EXCLUDED_DIRECTORIES:
                    children.append(path)
            except OSError:
                continue
        directories.extend(children)
    return None


def _validate_token(token: str) -> str:
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Server access key must not be empty")
    if len(token.splitlines()) > 1 or "\r" in token or "\n" in token:
        raise ValueError("Server access key must contain a single line")
    return token


def resolve_server_token(
    server_token: str | None = None,
    server_token_file: str | Path | None = None,
) -> tuple[str, Path | None]:
    """명시한 토큰을 우선하고, 없으면 실행 코드 옆의 키 파일을 읽는다."""
    if server_token is not None:
        return _validate_token(server_token), None
    if server_token_file is not None:
        selected = Path(server_token_file).expanduser()
        if not selected.is_absolute():
            selected = execution_directory() / selected
    else:
        selected = find_token_file(execution_directory())
        if selected is None:
            raise FileNotFoundError(
                "No llama_api_key*.key file was found next to the executing Python script "
                "or in its subdirectories. Add a key file or set server_token_file."
            )
    try:
        selected = selected.resolve(strict=True)
        text = selected.read_text(encoding="utf-8-sig").strip()
    except UnicodeError:
        raise ValueError("Server key file must contain valid UTF-8 text") from None
    except (OSError, RuntimeError):
        raise OSError(
            "Server key file could not be read; check server_token_file and file permissions"
        ) from None
    return _validate_token(text), selected


# 원격 호출 오류
class UnsupportedRemoteOperation(NotImplementedError):  # noqa: N818 — public API name
    """Python 또는 네이티브 연산을 HTTP API로 표현할 수 없을 때 발생한다."""


class RemoteServerError(RuntimeError):
    """서버가 요청을 거부하거나 SSE 스트림 안에서 오류를 보냈을 때 발생한다."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# CUDA 및 VRAM 확인
MIN_VRAM_BYTES = 7 * 1024**3


@dataclass(frozen=True)
class CudaDevice:
    index: int
    name: str
    total_bytes: int


@dataclass(frozen=True)
class RoutingInfo:
    backend: str
    reason: str
    devices: tuple[CudaDevice, ...] = ()
    selected_gpu: int | None = None


def _driver_devices() -> tuple[CudaDevice, ...]:
    # 드라이버 호출로 NVIDIA 하드웨어가 실제로 CUDA를 초기화할 수 있는지 확인한다.
    lib = ctypes.CDLL(
        "nvcuda.dll" if platform.system() == "Windows" else "libcuda.so.1"
    )
    lib.cuInit.argtypes = [ctypes.c_uint]
    lib.cuInit.restype = ctypes.c_int
    if lib.cuInit(0):
        return ()
    count = ctypes.c_int()
    lib.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
    lib.cuDeviceGetCount.restype = ctypes.c_int
    if lib.cuDeviceGetCount(ctypes.byref(count)):
        return ()
    mem_fn = getattr(lib, "cuDeviceTotalMem_v2", None) or lib.cuDeviceTotalMem
    mem_fn.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.c_int]
    mem_fn.restype = ctypes.c_int
    lib.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    lib.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    devices = []
    for ordinal in range(count.value):
        device, memory = ctypes.c_int(), ctypes.c_size_t()
        name = ctypes.create_string_buffer(256)
        if lib.cuDeviceGet(ctypes.byref(device), ordinal) or mem_fn(
            ctypes.byref(memory), device
        ):
            continue
        lib.cuDeviceGetName(name, len(name), device)
        devices.append(
            CudaDevice(ordinal, name.value.decode("utf-8", "replace"), memory.value)
        )
    return tuple(devices)


def _smi_devices() -> tuple[CudaDevice, ...]:
    executable = "nvidia-smi"
    if platform.system() == "Windows":
        candidate = os.path.join(
            os.environ.get("WINDIR", r"C:\Windows"), "System32", "nvidia-smi.exe"
        )
        if os.path.isfile(candidate):
            executable = candidate
    result = subprocess.run(
        [
            executable,
            "--query-gpu=index,uuid,name,memory.total",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    records = []
    for line in result.stdout.splitlines():
        index, uuid, name, memory = [part.strip() for part in line.split(",", 3)]
        records.append((index, uuid, name, int(memory) * 1024**2))
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is not None:
        if not visible.strip() or visible.strip() == "-1":
            return ()
        selected = []
        for identifier in visible.split(","):
            identifier = identifier.strip()
            match = next(
                (
                    r
                    for r in records
                    if r[0] == identifier or r[1].startswith(identifier)
                ),
                None,
            )
            if (
                match is None
            ):  # 잘못된 장치 지정이 나오면 CUDA의 표시 장치 목록이 끝난다.
                break
            selected.append(match)
        records = selected
    return tuple(
        CudaDevice(i, record[2], record[3]) for i, record in enumerate(records)
    )


def detect_cuda_devices() -> tuple[CudaDevice, ...]:
    if platform.system() not in {"Windows", "Linux"}:
        return ()
    if os.environ.get("CUDA_VISIBLE_DEVICES", "unset").strip() in {"", "-1"}:
        return ()
    try:
        return _driver_devices()
    except (OSError, AttributeError, ValueError):
        try:
            return _smi_devices()
        except (OSError, ValueError, subprocess.SubprocessError):
            return ()


def _native_cuda_available() -> bool:
    try:
        native = importlib.import_module("llama_cpp")
        native.llama_backend_init()
        if not native.llama_supports_gpu_offload():
            return False
        info = native.llama_print_system_info()
        if isinstance(info, bytes):
            info = info.decode("utf-8", "replace")
        # GPU 오프로딩 지원 여부만 검사하면 Vulkan, ROCm, Metal 빌드도 통과한다.
        return "CUDA" in str(info).upper()
    except (ImportError, OSError, AttributeError, RuntimeError):
        return False


def choose_backend(backend: str = "auto", main_gpu: int | None = None) -> RoutingInfo:
    if backend not in {"auto", "local", "remote"}:
        raise ValueError("backend must be 'auto', 'local', or 'remote'")
    if backend == "remote":
        return RoutingInfo("remote", "remote backend explicitly requested")
    system = platform.system()
    reason = None
    devices: tuple[CudaDevice, ...] = ()
    if system == "Darwin":
        reason = "macOS always uses the remote server"
    elif system not in {"Windows", "Linux"}:
        reason = f"local inference is unavailable on {system}"
    else:
        devices = detect_cuda_devices()
        candidates = [d for d in devices if d.total_bytes >= MIN_VRAM_BYTES]
        if main_gpu is not None:
            candidates = [d for d in candidates if d.index == main_gpu]
        if not candidates:
            reason = "no visible CUDA GPU with at least 7 GiB total VRAM"
        elif not _native_cuda_available():
            raise RuntimeError(
                "A CUDA GPU with at least 7 GiB VRAM is available, but llama-cpp-python "
                "has no usable CUDA backend. Install a CUDA-enabled llama-cpp-python build. "
                "Use backend='remote' only if you explicitly want the server."
            )
        else:
            return RoutingInfo(
                "local",
                "CUDA GPU has at least 7 GiB VRAM and native CUDA is available",
                devices,
                candidates[0].index,
            )
    if backend == "local":
        raise RuntimeError(f"Cannot use local backend: {reason}")
    return RoutingInfo("remote", reason or "local CUDA is unavailable", devices)


# 원본 직접 생성 또는 원격 HTTP 호출
DEFAULT_SERVER_URL = "https://satisshin.iptime.org:4000"

_COMPLETION_DEFAULTS = {
    "suffix": None,
    "max_tokens": 16,
    "temperature": 0.8,
    "top_p": 0.95,
    "min_p": 0.05,
    "typical_p": 1.0,
    "logprobs": None,
    "echo": False,
    "stop": None,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "repeat_penalty": 1.0,
    "top_k": 40,
    "stream": False,
    "seed": None,
    "tfs_z": 1.0,
    "mirostat_mode": 0,
    "mirostat_tau": 5.0,
    "mirostat_eta": 0.1,
    "model": None,
    "stopping_criteria": None,
    "logits_processor": None,
    "grammar": None,
    "logit_bias": None,
}
_CHAT_DEFAULTS = {
    "functions": None,
    "function_call": None,
    "tools": None,
    "tool_choice": None,
    "temperature": 0.2,
    "top_p": 0.95,
    "top_k": 40,
    "min_p": 0.05,
    "typical_p": 1.0,
    "stream": False,
    "stop": None,
    "seed": None,
    "response_format": None,
    "max_tokens": None,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "repeat_penalty": 1.0,
    "tfs_z": 1.0,
    "mirostat_mode": 0,
    "mirostat_tau": 5.0,
    "mirostat_eta": 0.1,
    "model": None,
    "logits_processor": None,
    "grammar": None,
    "logit_bias": None,
    "logprobs": None,
    "top_logprobs": None,
}
_REMOTE_UNSUPPORTED = {
    "eval",
    "sample",
    "generate",
    "save_state",
    "load_state",
    "reset",
    "eval_tokens",
    "eval_logits",
    "scores",
    "input_ids",
    "n_tokens",
    "ctx",
    "model",
    "model_params",
    "context_params",
    "tokenizer_",
    "metadata",
    "_ctx",
    "_model",
    "_batch",
    "_scores",
    "_input_ids",
    "token_bos",
    "token_eos",
    "token_nl",
    "token_prefix",
    "token_middle",
    "token_suffix",
    "token_eot",
    "pooling_type",
    "tokenizer",
}


def _load_native_module() -> ModuleType:
    return importlib.import_module("llama_cpp")


def _normalize_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("server_url must be a nonempty domain or HTTP(S) URL")
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("server_url must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "server_url cannot contain credentials, query parameters, or fragments"
        )
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _bind(
    defaults: dict[str, Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    if len(args) > len(defaults):
        raise TypeError("too many positional arguments")
    result = dict(defaults)
    for name, value in zip(defaults, args):
        if name in kwargs:
            raise TypeError(f"multiple values for argument {name!r}")
        result[name] = value
    result.update(kwargs)
    return result


def _grammar_text(grammar: Any) -> str:
    if isinstance(grammar, str):
        return grammar
    text = getattr(grammar, "_grammar", None)
    if isinstance(text, str):
        return text
    raise UnsupportedRemoteOperation(
        "Remote grammar must be a GBNF string or LlamaGrammar with its original GBNF text"
    )


def _reject_callbacks(params: dict[str, Any]) -> None:
    for name in (
        "logits_processor",
        "stopping_criteria",
        "chat_handler",
        "draft_model",
        "tokenizer",
    ):
        if params.get(name) is not None:
            raise UnsupportedRemoteOperation(
                f"{name} executes Python/native code and cannot run through llama-server HTTP"
            )
        params.pop(name, None)


class AdaptiveLlama:
    def __new__(
        cls,
        model_path: str | Path | None = None,
        *,
        server_url: str = DEFAULT_SERVER_URL,
        server_token: str | None = None,
        server_token_file: str | Path | None = None,
        backend: str = "auto",
        timeout: float | httpx.Timeout = 300.0,
        transport: httpx.BaseTransport | None = None,
        cache_prompt: bool = True,
        cache_capacity_bytes: int = 1024**3,
        remote_model: str | None = None,
        **llama_kwargs: Any,
    ) -> Any:
        decision = choose_backend(backend, llama_kwargs.get("main_gpu"))
        if decision.backend == "local":
            # 서버 접속 인자와 HTTP 전용 설정은 원본 생성자에 전달하지 않는다.
            options = dict(llama_kwargs)
            options.setdefault("n_ctx", 65536)
            options.setdefault("n_gpu_layers", -1)
            native = _load_native_module()
            if decision.selected_gpu is not None:
                options.setdefault("main_gpu", decision.selected_gpu)
                options["split_mode"] = native.LLAMA_SPLIT_MODE_NONE
            return native.Llama(
                model_path=str(model_path) if model_path is not None else None,
                **options,
            )
        instance = super().__new__(cls)
        instance._routing_decision = decision
        return instance

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        server_url: str = DEFAULT_SERVER_URL,
        server_token: str | None = None,
        server_token_file: str | Path | None = None,
        backend: str = "auto",
        timeout: float | httpx.Timeout = 300.0,
        transport: httpx.BaseTransport | None = None,
        cache_prompt: bool = True,
        cache_capacity_bytes: int = 1024**3,
        remote_model: str | None = None,
        **llama_kwargs: Any,
    ) -> None:
        self.server_url = _normalize_url(server_url)
        server_token, self.server_token_file = resolve_server_token(
            server_token, server_token_file
        )
        # 원격 model_path는 기록만 하며 파일 검사·다운로드·요청 모델 선택에 사용하지 않는다.
        self.model_path = str(model_path) if model_path is not None else None
        self._http: httpx.Client | None = None
        self._closed = False
        self._props_cache: dict | None = None
        self._model_info_cache: dict | None = None
        self._remote_model = remote_model
        self._cache_prompt = bool(cache_prompt)
        self._seed = llama_kwargs.get("seed")
        self._requested_n_ctx = llama_kwargs.get("n_ctx", 65536)
        self._repeat_last_n = llama_kwargs.get("last_n_tokens_size", 64)
        self.routing_info = self._routing_decision
        del self._routing_decision
        self.backend = self.routing_info.backend
        unsupported_init = {
            key: value
            for key, value in llama_kwargs.items()
            if key
            in {
                "chat_handler",
                "draft_model",
                "tokenizer",
                "chat_format",
                "lora_path",
                "lora_base",
                "kv_overrides",
            }
            and value is not None
        }
        if unsupported_init:
            raise UnsupportedRemoteOperation(
                "These constructor settings require a local model or server-side configuration: "
                + ", ".join(sorted(unsupported_init))
            )
        # 메모리 배치, 스레드 수, GPU 레이어 설정은 서버 프로세스에서 관리한다.
        # 실제로 적용되지 않는 API 옵션으로 보내지 않고 호출자가 확인할 수 있게 보관한다.
        self.remote_ignored_parameters = tuple(
            sorted(
                set(llama_kwargs)
                - {"seed", "n_ctx", "last_n_tokens_size", "embedding", "verbose"}
            )
        )
        if self.remote_ignored_parameters:
            warnings.warn(
                "Remote server controls these model-loading settings: "
                + ", ".join(self.remote_ignored_parameters),
                UserWarning,
                stacklevel=2,
            )
        self._http = httpx.Client(
            base_url=self.server_url + "/",
            headers={"Authorization": "Bearer " + server_token},
            timeout=timeout,
            transport=transport,
            verify=False,
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(backend={self.backend!r}, server_url={self.server_url!r})"

    def __getattr__(self, name: str) -> NoReturn:
        if name in _REMOTE_UNSUPPORTED:
            raise UnsupportedRemoteOperation(
                f"{name} requires local llama-cpp-python state; the server has shared native state"
            )
        raise AttributeError(f"{type(self).__name__!s} has no attribute {name!r}")

    @classmethod
    def from_pretrained(
        cls,
        repo_id: str,
        filename: str | None = None,
        additional_files: list[str] | None = None,
        local_dir: str | Path | None = None,
        local_dir_use_symlinks: bool | str = "auto",
        cache_dir: str | Path | None = None,
        *,
        server_url: str = DEFAULT_SERVER_URL,
        server_token: str | None = None,
        server_token_file: str | Path | None = None,
        backend: str = "auto",
        timeout: float | httpx.Timeout = 300.0,
        transport: httpx.BaseTransport | None = None,
        cache_prompt: bool = True,
        cache_capacity_bytes: int = 1024**3,
        remote_model: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """로컬이면 원본 다운로드 생성자를 호출하고 원격이면 다운로드를 건너뛴다."""
        decision = choose_backend(backend, kwargs.get("main_gpu"))
        if decision.backend == "local":
            options = dict(kwargs)
            options.setdefault("n_ctx", 65536)
            options.setdefault("n_gpu_layers", -1)
            native = _load_native_module()
            if decision.selected_gpu is not None:
                options.setdefault("main_gpu", decision.selected_gpu)
                options["split_mode"] = native.LLAMA_SPLIT_MODE_NONE
            return native.Llama.from_pretrained(
                repo_id=repo_id,
                filename=filename,
                additional_files=additional_files,
                local_dir=local_dir,
                local_dir_use_symlinks=local_dir_use_symlinks,
                cache_dir=cache_dir,
                **options,
            )
        instance = cls(
            server_url=server_url,
            server_token=server_token,
            server_token_file=server_token_file,
            backend="remote",
            timeout=timeout,
            transport=transport,
            cache_prompt=cache_prompt,
            cache_capacity_bytes=cache_capacity_bytes,
            remote_model=remote_model,
            **kwargs,
        )
        instance.routing_info = decision
        return instance

    def _client(self) -> httpx.Client:
        self._ensure_open()
        if self._http is None:
            raise RuntimeError("This operation requires the remote backend")
        return self._http

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Llama instance is closed")

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.is_error or response.is_redirect:
            try:
                body = response.json()
                error = body.get("error", body)
                detail = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
            except (ValueError, UnicodeError):
                detail = response.text[:1000]
            raise RemoteServerError(
                f"llama-server HTTP {response.status_code}: {detail}",
                response.status_code,
            )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client().request(method, path.lstrip("/"), **kwargs)
        self._check_response(response)
        result = response.json()
        if isinstance(result, dict) and "error" in result:
            raise RemoteServerError(str(result["error"]))
        return result

    def _sse(self, path: str, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        with self._client().stream("POST", path.lstrip("/"), json=payload) as response:
            if response.is_error or response.is_redirect:
                response.read()
                self._check_response(response)
            data_lines = []
            for line in response.iter_lines():
                if line == "":
                    if not data_lines:
                        continue
                    data = "\n".join(data_lines)
                    data_lines.clear()
                    if data == "[DONE]":
                        return
                    event = json.loads(data)
                    if "error" in event:
                        raise RemoteServerError(str(event["error"]))
                    yield event
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
                # event/id/retry 필드와 주석 및 연결 유지용 행은 무시해도 된다.
            if data_lines:
                data = "\n".join(data_lines)
                if data != "[DONE]":
                    event = json.loads(data)
                    if "error" in event:
                        raise RemoteServerError(str(event["error"]))
                    yield event

    def _sampling_params(
        self, values: dict[str, Any], *, chat: bool = False
    ) -> dict[str, Any]:
        values = dict(values)
        _reject_callbacks(values)
        if values.pop("tfs_z", 1.0) != 1.0:
            raise UnsupportedRemoteOperation(
                "Current llama-server does not implement tail-free sampling (tfs_z)"
            )
        grammar = values.pop("grammar", None)
        if grammar is not None:
            values["grammar"] = _grammar_text(grammar)
        mode = values.pop("mirostat_mode", 0)
        values["mirostat"] = mode
        if values.get("seed") is None:
            values["seed"] = self._seed
        if values.get("seed") == -1:
            values["seed"] = 0xFFFFFFFF
        if values.get("model") is None:
            values["model"] = self._remote_model
        if isinstance(values.get("stop"), str):
            values["stop"] = [values["stop"]]
        if values.get("logit_bias") is not None and not chat:
            values["logit_bias"] = [
                [int(token), bias] for token, bias in values["logit_bias"].items()
            ]
        values.setdefault("cache_prompt", self._cache_prompt)
        values.setdefault("repeat_last_n", self._repeat_last_n)
        return {key: value for key, value in values.items() if value is not None}

    def create_completion(
        self, prompt: str | list[int], *args: Any, **kwargs: Any
    ) -> dict[str, Any] | Iterator[dict[str, Any]]:
        self._ensure_open()
        values = _bind(_COMPLETION_DEFAULTS, args, kwargs)
        suffix = values.pop("suffix")
        if suffix is not None and not isinstance(suffix, str):
            raise TypeError("suffix must be a string or None")
        echo = values.pop("echo")
        logprobs = values.pop("logprobs")
        if echo and logprobs is not None:
            raise UnsupportedRemoteOperation(
                "llama-server does not expose prompt logprobs for echo=True"
            )
        max_tokens = values.pop("max_tokens")
        payload = self._sampling_params(values)
        payload["prompt"] = prompt
        endpoint = "/completion"
        if suffix is not None:
            endpoint = "/infill"
            payload["input_prefix"] = (
                prompt
                if isinstance(prompt, str)
                else self.detokenize(prompt).decode("utf-8", "replace")
            )
            payload["input_suffix"] = suffix
            payload["prompt"] = ""
        payload["n_predict"] = (
            -1 if max_tokens is None or max_tokens <= 0 else max_tokens
        )
        if logprobs is not None:
            payload["n_probs"] = logprobs
        prompt_text = prompt if isinstance(prompt, str) else None
        if prompt_text is None and (echo or logprobs is not None):
            prompt_text = self.detokenize(prompt).decode("utf-8", "replace")
        prefix = prompt_text if echo else ""
        logprob_offset = 0 if echo else len(prompt_text or "")
        if payload.get("stream"):
            return self._completion_stream(payload, prefix, logprob_offset, endpoint)
        raw = self._request("POST", endpoint, json=payload)
        if not isinstance(raw, dict):
            raise RemoteServerError(
                "Expected one completion; batched prompts require the server's native API"
            )
        return self._completion_response(
            raw, prefix + raw.get("content", ""), payload, offset=logprob_offset
        )

    __call__ = create_completion

    def _completion_response(
        self,
        raw: dict[str, Any],
        text: str,
        payload: dict[str, Any],
        *,
        completion_id: str | None = None,
        created: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        stopped = raw.get("stop", True)
        reason = (
            "length"
            if raw.get("stop_type") == "limit" or raw.get("stopped_limit")
            else "stop"
        )
        result = {
            "id": completion_id or "cmpl-" + uuid.uuid4().hex,
            "object": "text_completion",
            "created": int(time.time()) if created is None else created,
            "model": raw.get("model") or payload.get("model") or "remote",
            "choices": [
                {
                    "text": text,
                    "index": 0,
                    "logprobs": self._convert_logprobs(raw, offset),
                    "finish_reason": reason if stopped else None,
                }
            ],
        }
        if stopped:
            timings = raw.get("timings", {})
            prompt_count = raw.get("tokens_evaluated", timings.get("prompt_n", 0))
            generated_count = raw.get("tokens_predicted", timings.get("predicted_n", 0))
            result["usage"] = {
                "prompt_tokens": prompt_count,
                "completion_tokens": generated_count,
                "total_tokens": prompt_count + generated_count,
            }
        return result

    @staticmethod
    def _convert_logprobs(raw: dict[str, Any], offset: int) -> dict[str, Any] | None:
        probabilities = raw.get("completion_probabilities", raw.get("probs"))
        if probabilities is None:
            return None
        result = {
            "tokens": [],
            "token_logprobs": [],
            "top_logprobs": [],
            "text_offset": [],
        }
        for entry in probabilities:
            text = entry.get("token", entry.get("content", ""))
            result["tokens"].append(text)
            result["token_logprobs"].append(entry.get("logprob"))
            result["top_logprobs"].append(
                {
                    item.get("token", item.get("tok_str", "")): item.get(
                        "logprob", math.log(max(item.get("prob", 1e-45), 1e-45))
                    )
                    for item in entry.get("top_logprobs", entry.get("probs", []))
                }
            )
            result["text_offset"].append(offset)
            offset += len(text)
        return result

    def _completion_stream(
        self,
        payload: dict[str, Any],
        prefix: str,
        offset: int = 0,
        endpoint: str = "/completion",
    ) -> Iterator[dict[str, Any]]:
        completion_id, created = "cmpl-" + uuid.uuid4().hex, int(time.time())
        if prefix:
            yield self._completion_response(
                {"stop": False},
                prefix,
                payload,
                completion_id=completion_id,
                created=created,
            )
            offset = len(prefix)
        for event in self._sse(endpoint, payload):
            text = event.get("content", "")
            yield self._completion_response(
                event,
                text,
                payload,
                completion_id=completion_id,
                created=created,
                offset=offset,
            )
            offset += len(text)

    def create_chat_completion(
        self, messages: list[dict[str, Any]], *args: Any, **kwargs: Any
    ) -> dict[str, Any] | Iterator[dict[str, Any]]:
        self._ensure_open()
        values = _bind(_CHAT_DEFAULTS, args, kwargs)
        # 기존 Python 함수 호출 형식을 tools API 형식으로 변환한다.
        functions = values.pop("functions")
        function_call = values.pop("function_call")
        if functions is not None:
            if values.get("tools") is not None:
                raise ValueError("Specify either functions or tools")
            values["tools"] = [{"type": "function", "function": fn} for fn in functions]
        if function_call is not None:
            values["tool_choice"] = (
                {"type": "function", "function": function_call}
                if isinstance(function_call, dict)
                else function_call
            )
        payload = self._sampling_params(values, chat=True)
        payload["messages"] = messages
        if payload.get("max_tokens", 1) <= 0:
            payload.pop("max_tokens", None)
        if payload.get("stream"):
            return self._sse("/v1/chat/completions", payload)
        return self._request("POST", "/v1/chat/completions", json=payload)

    def create_chat_completion_openai_v1(
        self, *args: Any, **kwargs: Any
    ) -> ChatCompletion | Iterator[ChatCompletionChunk]:
        self._ensure_open()
        try:
            from openai.types.chat import ChatCompletion, ChatCompletionChunk
        except ImportError as error:
            raise ImportError(
                "Optional OpenAI response objects require the SDK; install it with: pip install openai"
            ) from error
        result = self.create_chat_completion(*args, **kwargs)
        if isinstance(result, dict):
            return ChatCompletion.model_validate(result)
        return (ChatCompletionChunk.model_validate(chunk) for chunk in result)

    def tokenize(
        self, text: bytes, add_bos: bool = True, special: bool = False
    ) -> list[int]:
        self._ensure_open()
        if not isinstance(text, bytes):
            raise TypeError("text must be bytes, matching llama-cpp-python.tokenize")
        try:
            content = text.decode("utf-8")
        except UnicodeDecodeError as error:
            raise UnsupportedRemoteOperation(
                "HTTP tokenization requires valid UTF-8; arbitrary byte input requires a local tokenizer"
            ) from error
        return self._request(
            "POST",
            "/tokenize",
            json={"content": content, "add_special": add_bos, "parse_special": special},
        )["tokens"]

    def detokenize(
        self,
        tokens: list[int],
        prev_tokens: list[int] | None = None,
        special: bool = False,
    ) -> bytes:
        self._ensure_open()
        if special:
            raise UnsupportedRemoteOperation(
                "The server detokenize endpoint cannot select special-token rendering"
            )
        # 기본 엔드포인트는 Unicode 문자열을 반환하므로 불완전한 UTF-8 토큰열은
        # 네이티브 바이트 표현과 정확히 동일하게 복원할 수 없다.
        if prev_tokens:
            previous = self._request(
                "POST", "/detokenize", json={"tokens": list(prev_tokens)}
            )["content"]
            combined = self._request(
                "POST", "/detokenize", json={"tokens": list(prev_tokens) + list(tokens)}
            )["content"]
            return combined.encode("utf-8")[len(previous.encode("utf-8")) :]
        return self._request("POST", "/detokenize", json={"tokens": list(tokens)})[
            "content"
        ].encode("utf-8")

    # Keep input= for llama-cpp-python keyword compatibility.
    def create_embedding(
        self, input: str | list[str], model: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self._ensure_open()
        payload = {"input": input, **kwargs}
        if model or self._remote_model:
            payload["model"] = model or self._remote_model
        return self._request("POST", "/v1/embeddings", json=payload)

    # Keep input= for llama-cpp-python keyword compatibility.
    def embed(
        self,
        input: str | list[str],
        normalize: bool = False,
        truncate: bool = True,
        return_count: bool = False,
    ) -> list[Any] | tuple[list[Any], int]:
        self._ensure_open()
        inputs = [input] if isinstance(input, str) else input
        vectors, count = [], 0
        for text in inputs:
            tokens = self.tokenize(text.encode("utf-8"))
            limit = self.n_ctx()
            if len(tokens) > limit and not truncate:
                raise ValueError(
                    f"Embedding input has {len(tokens)} tokens, exceeding n_ctx={limit}"
                )
            if len(tokens) > limit:
                tokens = tokens[:limit]
                text = self.detokenize(tokens).decode("utf-8")
            payload = {"content": text, "embd_normalize": 2 if normalize else -1}
            if self._remote_model:
                payload["model"] = self._remote_model
            raw = self._request("POST", "/embedding", json=payload)
            item = raw[0] if isinstance(raw, list) else raw
            vector = item["embedding"]
            if vector and isinstance(vector[0], list) and len(vector) == 1:
                vector = vector[0]
            vectors.append(vector)
            count += len(tokens)
        value = vectors[0] if isinstance(input, str) else vectors
        return (value, count) if return_count else value

    @property
    def server_properties(self) -> dict[str, Any]:
        params = {"model": self._remote_model} if self._remote_model else None
        if self._props_cache is None:
            self._props_cache = self._request("GET", "/props", params=params)
        return self._props_cache

    def n_ctx(self) -> int:
        self._ensure_open()
        props = self.server_properties
        settings = props.get("default_generation_settings", {})
        server_limit = settings.get("n_ctx", props.get("n_ctx", 65536))
        return min(self._requested_n_ctx or server_limit, server_limit)

    def _model_meta(self) -> dict[str, Any]:
        if self._model_info_cache is None:
            models = self._request("GET", "/v1/models")["data"]
            candidates = [
                item
                for item in models
                if self._remote_model is None or item["id"] == self._remote_model
            ]
            if len(candidates) != 1 or not candidates[0].get("meta"):
                raise RemoteServerError(
                    "The server did not return unambiguous loaded model metadata"
                )
            self._model_info_cache = candidates[0]["meta"]
        return self._model_info_cache

    def n_vocab(self) -> int:
        self._ensure_open()
        return self._model_meta()["n_vocab"]

    def n_embd(self) -> int:
        self._ensure_open()
        return self._model_meta()["n_embd"]

    def validate_remote_context(self) -> int:
        """서버가 슬롯마다 요청한 크기 이상의 컨텍스트를 확보했는지 확인한다."""
        actual = self.n_ctx()
        if self._requested_n_ctx and actual < self._requested_n_ctx:
            raise ValueError(
                f"Server context is {actual}; requested n_ctx={self._requested_n_ctx}. Change the server configuration."
            )
        return actual

    def health(self) -> dict[str, Any]:
        self._ensure_open()
        return self._request("GET", "/health")

    def apply_chat_template(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        self._ensure_open()
        payload = {"messages": messages, **kwargs}
        if self._remote_model:
            payload["model"] = self._remote_model
        return self._request("POST", "/apply-template", json=payload)["prompt"]

    def set_seed(self, seed: int) -> None:
        self._ensure_open()
        self._seed = seed

    def set_cache(self, cache: Any) -> None:
        self._ensure_open()
        if cache is not None:
            raise UnsupportedRemoteOperation(
                "Python cache objects require local inference; use cache_prompt=True for server caching"
            )
        self._cache_prompt = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._http is not None:
            self._http.close()

    def __enter__(self) -> AdaptiveLlama:
        if self._closed:
            raise RuntimeError("Llama instance is closed")
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


Llama = AdaptiveLlama


# 공개 API와 선택적 원본 도우미
__version__ = "0.1.0"
__all__ = [
    "Llama",
    "AdaptiveLlama",
    "DEFAULT_SERVER_URL",
    "resolve_server_token",
    "find_token_file",
    "execution_directory",
    "CudaDevice",
    "RoutingInfo",
    "choose_backend",
    "detect_cuda_devices",
    "RemoteServerError",
    "UnsupportedRemoteOperation",
]


def __getattr__(name: str) -> Any:
    """LlamaGrammar, 캐시, 저수준 API 등 선택적 네이티브 도우미를 계속 제공한다.

    원격 클라이언트에서 이 모듈을 가져올 때는 네이티브 라이브러리가 필요 없다.
    도우미는 원본 패키지의 동작을 유지하며, Llama만 실행 위치를 선택한다.
    """
    if name.startswith("__"):
        raise AttributeError(name)
    try:
        native = importlib.import_module("llama_cpp")
    except ImportError as error:
        raise AttributeError(
            f"{name} requires the optional llama-cpp-python installation"
        ) from error
    try:
        return getattr(native, name)
    except AttributeError as error:
        raise AttributeError(
            f"module llama_wrapper has no attribute {name!r}"
        ) from error

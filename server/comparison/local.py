"""로컬 단독 호출. 파이프라인의 LocalLLMRunner를 쓰지 않고 어댑터를 직접 부른다.

system 메시지 없이 user 메시지 하나만 보내고, 항상 원격 모델 서버에서 실행한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing
from typing import Any

import httpx

from .local_context import CONTEXT_TOKENS, MAX_OUTPUT_TOKENS, fit_input
from .models import (
    TIMEOUT_SECONDS,
    Cancellation,
    Cancelled,
    ErrorCode,
    ModelFailure,
    StreamEvent,
    Turn,
)

LlamaFactory = Callable[[], Any]


def _default_llama() -> Any:
    from src.localLLM_API_proxy.llama_wrapper import Llama

    return Llama(backend="remote", n_ctx=CONTEXT_TOKENS, timeout=TIMEOUT_SECONDS)


def _http_code(status: int) -> ErrorCode:
    if status in {401, 403}:
        return ErrorCode.AUTH_ERROR
    if status == 429:
        return ErrorCode.RATE_LIMITED
    if status >= 500:
        return ErrorCode.UNAVAILABLE
    return ErrorCode.REJECTED


def _failure(error: Exception, started: bool) -> ModelFailure:
    """예외를 화면 오류 코드로 바꾼다. 응답 본문이 담긴 예외 원문은 옮기지 않는다."""
    name = type(error).__name__
    status = getattr(error, "status_code", None)  # RemoteServerError
    if isinstance(status, int):
        return ModelFailure(_http_code(status), f"HTTP {status}")
    if isinstance(error, httpx.TimeoutException):
        return ModelFailure(ErrorCode.TIMEOUT, name)
    if started:
        return ModelFailure(ErrorCode.STREAM_BROKEN, name)
    if isinstance(error, httpx.HTTPError) or hasattr(error, "status_code"):
        return ModelFailure(ErrorCode.UNAVAILABLE, name)
    return ModelFailure(ErrorCode.INTERNAL, name)


class LocalStandalone:
    def __init__(self, llama_factory: LlamaFactory = _default_llama) -> None:
        self._llama_factory = llama_factory

    @property
    def model_name(self) -> str | None:
        return None  # 응답의 model 값으로만 안다

    def cost(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        return None

    def stream(
        self, turns: list[Turn], question: str, cancellation: Cancellation
    ) -> Iterator[StreamEvent]:
        """요청할 때마다 어댑터를 새로 만들고 끝나면 닫는다.

        중지하면 어댑터를 닫아 모델 서버와의 연결을 끊는다. 다른 요청과 함께 쓰면
        그 요청까지 끊기므로 공유하지 않는다.
        """
        cancellation.check()
        try:
            llama = self._llama_factory()
        except (OSError, ValueError) as error:
            # 만들 때는 네트워크를 쓰지 않는다. 키 파일이 없거나 읽지 못한 경우다.
            raise ModelFailure(ErrorCode.KEY_MISSING, type(error).__name__) from None
        with cancellation.closing(llama.close):
            started = False
            try:
                for event in self._generate(llama, turns, question):
                    started = started or bool(event.text)
                    yield event
            except (ModelFailure, Cancelled):
                raise
            except Exception as error:
                cancellation.check()  # 우리가 닫아서 난 예외는 실패가 아니다
                raise _failure(error, started) from None

    def _generate(
        self, llama: Any, turns: list[Turn], question: str
    ) -> Iterator[StreamEvent]:
        def count_tokens(text: str) -> int:
            return len(
                llama.tokenize(text.encode("utf-8"), add_bos=False, special=True)
            )

        text, dropped = fit_input(turns, question, count_tokens)
        yield StreamEvent(dropped_pairs=dropped)
        chunks = llama.create_chat_completion(
            messages=[{"role": "user", "content": text}],
            stream=True,
            max_tokens=MAX_OUTPUT_TOKENS,
        )
        with closing(chunks):
            for chunk in chunks:
                usage = chunk.get("usage") or {}
                choice = (chunk.get("choices") or [{}])[0]
                yield StreamEvent(
                    text=(choice.get("delta") or {}).get("content") or "",
                    finish_reason=choice.get("finish_reason"),
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    model_name=chunk.get("model"),
                )

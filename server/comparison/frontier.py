"""프론티어 단독 호출 · 앞 대화 만들기.

질문 원문을 그대로 보낸다. 파이프라인의 FrontierClient를 쓰지 않는다.
앞 대화는 user/assistant로 나눠 매번 다시 보내고 system 메시지는 넣지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from .models import (
    TIMEOUT_SECONDS,
    Cancellation,
    ErrorCode,
    ModelFailure,
    StreamEvent,
    Turn,
)

MAX_OUTPUT_TOKENS = 4096
# 모든 content의 글자 수 합. 토큰 계산기가 없어 글자로 센다.
# 임시값이며 모델의 입력 한도를 확인한 뒤 조정한다.
INPUT_LIMIT_CHARS = 100_000
# 처음 요청을 포함한 최대 시도 횟수. 글이 나오기 전의 일시 오류만 다시 시도한다.
MAX_ATTEMPTS = 3
RETRY_DELAYS = (1.0, 2.0)
CONNECT_TIMEOUT = 10.0
QUOTA_CODES = frozenset({"insufficient_quota", "quota_exceeded", "budget_exceeded"})


class _Transient(Exception):  # noqa: N818 — 다시 시도할 수 있는 실패
    def __init__(self, failure: ModelFailure) -> None:
        super().__init__(str(failure))
        self.failure = failure


def build_messages(turns: list[Turn], question: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for turn in turns:
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer})
    messages.append({"role": "user", "content": question})
    return messages


def fit_messages(turns: list[Turn], question: str) -> tuple[list[dict[str, str]], int]:
    """한도에 맞는 메시지와 뺀 쌍의 수. 오래된 쌍부터 뺀다."""
    total = len(question) + sum(len(t.question) + len(t.answer) for t in turns)
    dropped = 0
    while total > INPUT_LIMIT_CHARS and dropped < len(turns):
        total -= len(turns[dropped].question) + len(turns[dropped].answer)
        dropped += 1
    if total > INPUT_LIMIT_CHARS:
        raise ModelFailure(
            ErrorCode.INPUT_TOO_LONG, "frontier input", dropped_pairs=dropped
        )
    return build_messages(turns[dropped:], question), dropped


def _provider_code(body: Any) -> str | None:
    """오류 본문의 코드. 본문 자체는 질문을 되풀이할 수 있어 쓰지 않는다."""
    detail = body.get("error") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) else None


def _http_error(response: httpx.Response) -> ModelFailure | _Transient:
    status = response.status_code
    try:
        code = _provider_code(json.loads(response.read()))
    except (ValueError, httpx.HTTPError):
        code = None
    detail = f"HTTP {status}"
    # 이 게이트웨이의 422는 1인 한도 또는 교육 총 한도 소진이다.
    if status == 422 or code in QUOTA_CODES:
        return ModelFailure(ErrorCode.QUOTA_EXCEEDED, detail)
    if status in {401, 403}:
        return ModelFailure(ErrorCode.AUTH_ERROR, detail)
    if status == 429:
        return _Transient(ModelFailure(ErrorCode.RATE_LIMITED, detail))
    if status >= 500:
        return _Transient(ModelFailure(ErrorCode.UNAVAILABLE, detail))
    return ModelFailure(ErrorCode.REJECTED, detail)


def _events(response: httpx.Response) -> Iterator[StreamEvent]:
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            chunk = json.loads(data)
        except ValueError:
            chunk = None
        if not isinstance(chunk, dict):
            raise ModelFailure(ErrorCode.STREAM_BROKEN, "invalid chunk")
        if chunk.get("error"):
            if _provider_code(chunk) in QUOTA_CODES:
                raise ModelFailure(ErrorCode.QUOTA_EXCEEDED, "stream error")
            raise _Transient(ModelFailure(ErrorCode.UNAVAILABLE, "stream error"))
        usage = chunk.get("usage") or {}
        choice = (chunk.get("choices") or [{}])[0]
        yield StreamEvent(
            text=(choice.get("delta") or {}).get("content") or "",
            finish_reason=choice.get("finish_reason"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


class FrontierStandalone:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        input_price: float | None = None,
        output_price: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """transport: 테스트에서 가짜 응답을 넣을 httpx 전송 계층."""
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._input_price = input_price
        self._output_price = output_price
        self._transport = transport

    @property
    def model_name(self) -> str | None:
        return self._model

    def cost(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        """사용량과 단가로 계산한 비용(달러). 하나라도 모르면 None."""
        if input_tokens is None or output_tokens is None:
            return None
        if self._input_price is None or self._output_price is None:
            return None
        total = input_tokens * self._input_price + output_tokens * self._output_price
        return total / 1_000_000

    def stream(
        self, turns: list[Turn], question: str, cancellation: Cancellation
    ) -> Iterator[StreamEvent]:
        if not self._api_key:
            raise ModelFailure(ErrorCode.KEY_MISSING, "FRONTIER_API_KEY")
        messages, dropped = fit_messages(turns, question)
        yield StreamEvent(dropped_pairs=dropped)
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                cancellation.wait(RETRY_DELAYS[attempt - 1])
            try:
                yield from self._attempt(messages, cancellation)
                return
            except _Transient as error:
                if attempt == MAX_ATTEMPTS - 1:
                    raise error.failure from None

    def _attempt(
        self, messages: list[dict[str, str]], cancellation: Cancellation
    ) -> Iterator[StreamEvent]:
        """한 번 요청한다. 다시 시도할 수 있는 실패만 _Transient로 알린다.

        요청할 때마다 연결을 새로 만든다. 중지하면 그 연결을 닫는다.
        """
        started = False
        client = httpx.Client(
            timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT),
            transport=self._transport,
        )
        with cancellation.closing(client.close):
            try:
                for event in self._request(client, messages):
                    started = started or bool(event.text)
                    yield event
            except (ModelFailure, _Transient) as error:
                cancellation.check()
                if started:
                    raise ModelFailure(
                        ErrorCode.STREAM_BROKEN, "stream error"
                    ) from None
                raise error from None
            except (httpx.HTTPError, httpx.InvalidURL, RuntimeError) as error:
                cancellation.check()  # 우리가 닫아서 난 예외는 실패가 아니다
                name = type(error).__name__
                if started:
                    raise ModelFailure(ErrorCode.STREAM_BROKEN, name) from None
                if isinstance(error, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
                    raise ModelFailure(ErrorCode.REJECTED, name) from None
                if isinstance(error, httpx.TimeoutException):
                    raise _Transient(ModelFailure(ErrorCode.TIMEOUT, name)) from None
                if isinstance(error, httpx.HTTPError):
                    raise _Transient(
                        ModelFailure(ErrorCode.UNAVAILABLE, name)
                    ) from None
                raise ModelFailure(ErrorCode.INTERNAL, name) from None

    def _request(
        self, client: httpx.Client, messages: list[dict[str, str]]
    ) -> Iterator[StreamEvent]:
        with client.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "messages": messages,
                "max_completion_tokens": MAX_OUTPUT_TOKENS,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as response:
            if response.status_code != 200:
                raise _http_error(response)
            yield from _events(response)

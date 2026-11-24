"""비교 전용 자료형 · 오류 코드 · 취소 신호."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

Kind = Literal["local", "frontier"]
KINDS: tuple[Kind, ...] = ("local", "frontier")

# 요청 하나의 전체 제한 시간(초). 길이 계산과 자동 재시도 대기를 포함한다.
TIMEOUT_SECONDS = 120.0


class Status(StrEnum):
    RUNNING = "running"
    OK = "ok"
    TRUNCATED = "truncated"
    FAILED = "failed"
    STOPPED = "stopped"


# 다시 시도할 수 있는 상태
RETRYABLE_STATUS = frozenset({Status.FAILED, Status.TRUNCATED, Status.STOPPED})


class ErrorCode(StrEnum):
    KEY_MISSING = "KEY_MISSING"
    INPUT_TOO_LONG = "INPUT_TOO_LONG"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    AUTH_ERROR = "AUTH_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    REJECTED = "REJECTED"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    STREAM_BROKEN = "STREAM_BROKEN"
    INTERRUPTED = "INTERRUPTED"
    INTERNAL = "INTERNAL"


# 화면에 보여 줄 고정 문구. 예외 원문과 응답 본문은 화면에 내보내지 않는다.
MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.KEY_MISSING: "모델에 접속할 키가 없습니다.",
    ErrorCode.INPUT_TOO_LONG: "질문이 모델의 입력 한도를 넘었습니다.",
    ErrorCode.TIMEOUT: "응답 시간이 초과되었습니다.",
    ErrorCode.UNAVAILABLE: "모델에 연결하지 못했습니다.",
    ErrorCode.AUTH_ERROR: "모델의 인증 또는 접근 권한을 확인해야 합니다.",
    ErrorCode.RATE_LIMITED: "모델의 요청 빈도 제한에 도달했습니다.",
    ErrorCode.QUOTA_EXCEEDED: "모델의 사용 한도가 소진되었습니다.",
    ErrorCode.REJECTED: "모델이 요청을 거부했습니다.",
    ErrorCode.EMPTY_RESPONSE: "모델이 빈 답변을 보냈습니다.",
    ErrorCode.STREAM_BROKEN: "답변을 받는 도중 연결이 끊겼습니다.",
    ErrorCode.INTERRUPTED: "서버가 다시 시작되어 중단되었습니다.",
    ErrorCode.INTERNAL: "비교 답변을 만드는 중 오류가 발생했습니다.",
}
# 모델에 입력을 보내지 못하고 끝난 경우. 사용한 앞 대화 수를 표시하지 않는다.
NOT_SENT = frozenset({ErrorCode.KEY_MISSING, ErrorCode.INPUT_TOO_LONG})


class ComparisonSettings(BaseModel):
    """app.py가 기존 설정 함수와 상수로 만들어 넘기는 값."""

    enabled: bool
    db_path: Path
    frontier_api_key: str | None
    frontier_base_url: str
    frontier_model: str
    # 100만 토큰당 달러. 단가를 모르면 비용을 기록하지 않는다.
    input_price: float | None = None
    output_price: float | None = None


class Turn(BaseModel):
    """앞 대화 한 쌍. 같은 모델의 질문과 답변만 담는다."""

    question: str
    answer: str


class StreamEvent(BaseModel):
    """모델 호출에서 나오는 조각. 모르는 값은 None이며 앞서 받은 값을 유지한다."""

    text: str = ""
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_name: str | None = None
    dropped_pairs: int | None = None


class Result(BaseModel):
    """모델 하나의 최종 결과."""

    status: Status
    answer: str | None = None
    error_code: ErrorCode | None = None
    model_name: str | None = None
    dropped_pairs: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    elapsed_ms: float | None = None


class Record(Result):
    """comparisons 표의 한 줄."""

    chat_id: int
    run_id: int
    kind: Kind
    question: str
    attempts: int
    created_at: str
    ended_at: str | None
    # 같은 채팅·모델에서 앞서 정상으로 끝난 줄의 수 (저장하지 않고 조회할 때 센다)
    previous_ok: int


class CamelModel(BaseModel):
    """JSON에서 camelCase로 주고받는 공통 부모."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RunningRef(CamelModel):
    """진행 중인 비교."""

    chat_id: int
    run_id: int


class Item(CamelModel):
    """화면에 보내는 비교 결과 하나."""

    run_id: int
    kind: Kind
    question: str
    answer: str | None
    status: Status
    error_code: ErrorCode | None
    message: str | None
    model_name: str | None
    attempts: int
    used_pairs: int | None
    dropped_pairs: int
    input_tokens: int | None
    output_tokens: int | None
    cost: float | None
    elapsed_ms: float | None
    created_at: str
    ended_at: str | None
    retryable: bool


def to_item(record: Record, *, retryable: bool = False) -> Item:
    code = record.error_code
    used = None
    if code not in NOT_SENT:
        used = max(0, record.previous_ok - record.dropped_pairs)
    return Item(
        **record.model_dump(exclude={"chat_id", "previous_ok"}),
        message=MESSAGES[code] if code is not None else None,
        used_pairs=used,
        retryable=retryable,
    )


class ModelFailure(Exception):  # noqa: N818 — 화면 오류 코드를 담는 실패 신호
    """모델 호출이 정해진 오류 코드로 실패함.

    메시지에는 원인 종류만 적는다. 응답 본문은 질문을 되풀이할 수 있어 담지 않는다.
    """

    def __init__(
        self, code: ErrorCode, detail: str = "", *, dropped_pairs: int | None = None
    ) -> None:
        super().__init__(f"{code}: {detail}" if detail else str(code))
        self.code = code
        self.dropped_pairs = dropped_pairs


class Cancelled(Exception):  # noqa: N818 — 중지·시간 초과로 끝난 작업 스레드의 신호
    """중지나 시간 초과로 호출을 그만둠. 실패로 기록하지 않는다."""


class Cancellation:
    """모델 호출 하나의 취소 표시와 열어 둔 연결.

    취소 표시를 먼저 남기고 연결을 닫는다. 그래야 닫힌 뒤 작업 스레드에서 나는 예외를
    실패로 잘못 기록하지 않는다.
    """

    def __init__(self) -> None:
        self._event = Event()
        self._lock = Lock()
        self._close: Callable[[], None] | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled

    def wait(self, seconds: float) -> None:
        """재시도 전 대기. 그동안 취소되면 바로 Cancelled."""
        if self._event.wait(seconds):
            raise Cancelled

    @contextlib.contextmanager
    def closing(self, close: Callable[[], None]) -> Iterator[None]:
        """연결을 등록하고, 끝나면 닫는다. 이미 취소됐으면 닫고 Cancelled."""
        with self._lock:
            self._close = close
        try:
            self.check()
            yield
        finally:
            with self._lock:
                self._close = None
            with contextlib.suppress(Exception):
                close()

    def cancel(self) -> None:
        """취소 표시를 남기고 연결을 닫는다. 닫는 일이 호출한 쪽을 막지 않는다."""
        with self._lock:
            self._event.set()
            close = self._close
        if close is None:
            return

        def run() -> None:
            with contextlib.suppress(Exception):
                close()

        Thread(target=run, daemon=True).start()


class StandaloneModel(Protocol):
    """모델 하나를 단독으로 부르는 쪽. 작업 스레드에서 차례로 조각을 낸다."""

    @property
    def model_name(self) -> str | None:
        """응답을 받기 전에도 아는 모델 이름. 모르면 None."""
        ...

    def stream(
        self, turns: list[Turn], question: str, cancellation: Cancellation
    ) -> Iterator[StreamEvent]: ...

    def cost(
        self, input_tokens: int | None, output_tokens: int | None
    ) -> float | None: ...

"""API-06 진행 상황 스트림(SSE).

이벤트의 원본은 ChatManager의 run_events 표다. Main과 ConsentManager는 이벤트를 먼저 저장하고
저장된 ID로 EventHub에 알린다. 스트림은 DB에서 놓친 이벤트를 다시 읽어 보내고, 그다음부터는
새 이벤트가 저장될 때마다 깨어나 이어서 보낸다. 서버를 다시 켜도 기록에서 다시 보낼 수 있다.

    Main · ConsentManager ──log_event──▶ run_events(DB) ──get_events──▶ stream_run() ──▶ 화면
                         └──notify──▶ EventHub ──깨움──┘
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app.models.errors import safe_message

if TYPE_CHECKING:
    from app.data.chat_manager import ChatManager
    from app.models.schemas import RunEvent

# 이 이벤트가 오면 실행이 끝난 것이다.
TERMINAL_EVENTS = ("done", "error")
# 화면(API-06)으로 보내는 이벤트. run·run_error·llm_call 등 내부 기록은 보내지 않는다.
SCREEN_EVENTS = frozenset(
    {"step", "token", "frontier", "consent_request", "consent_result", *TERMINAL_EVENTS}
)
# 새 이벤트가 없을 때 연결 유지용 주석을 보내는 간격(초)
KEEPALIVE_SECONDS = 15.0


class EventHub:
    """새 이벤트가 저장될 때마다 기다리는 스트림을 깨운다.

    로컬 모델이 하나라 실행도 하나씩 돌므로, 실행을 구분하지 않고 '무언가 저장됨'만 알린다.
    깨어난 스트림은 자기 실행의 새 이벤트를 DB에서 다시 읽는다.
    """

    def __init__(self) -> None:
        self._version = 0
        self._changed = asyncio.Condition()

    @property
    def version(self) -> int:
        return self._version

    async def notify(self) -> None:
        async with self._changed:
            self._version += 1
            self._changed.notify_all()

    async def wait_after(self, version: int, timeout: float) -> bool:
        """version 이후 새 알림이 오면 True, timeout 동안 없으면 False."""
        async with self._changed:
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: self._version > version), timeout
                )
            except TimeoutError:
                return False
        return True


def to_screen(event: RunEvent) -> tuple[str, dict[str, Any]] | None:
    """저장된 이벤트를 화면 이벤트(이름, 데이터)로 바꾼다. 화면에 보내지 않는 기록은 None."""
    if event.type not in SCREEN_EVENTS or not isinstance(event.payload, dict):
        return None
    data = {key: value for key, value in event.payload.items() if key != "run_id"}
    if event.type == "error":
        data["message"] = safe_message(str(data.get("code", "INTERNAL_ERROR")))
    return event.type, data


def pending_consent_id(events: list[RunEvent]) -> int | None:
    """주어진 이벤트까지 봤을 때 아직 결과가 없는 동의 요청 ID."""
    pending: int | None = None
    for event in events:
        if not isinstance(event.payload, dict):
            continue
        if event.type == "consent_request":
            pending = event.payload.get("consentId")
        elif event.type == "consent_result":
            if event.payload.get("consentId") == pending:
                pending = None
    return pending


async def stream_run(
    chat_manager: ChatManager,
    hub: EventHub,
    run_id: int,
    after_id: int,
    *,
    keepalive: float = KEEPALIVE_SECONDS,
) -> AsyncIterator[str]:
    """after_id 다음 이벤트부터 SSE 형식 문자열로 내보낸다.

    1) 연결 시점까지 저장된 기록을 다시 보낸다.
    2) 이미 끝난 실행이면 종료 이벤트까지 보내고 닫는다.
    3) 진행 중인 실행이면 replay_complete를 한 번 보낸다(ID 없음, 저장하지 않음).
    4) 이후 새 이벤트가 저장될 때마다 보내고, done·error를 보내면 끝낸다.
    """
    history = await asyncio.to_thread(chat_manager.get_events, run_id, 0)
    for event in history:
        if event.event_id <= after_id:
            continue
        screen = to_screen(event)
        if screen is not None:
            yield format_sse(*screen, event.event_id)
    if any(event.type in TERMINAL_EVENTS for event in history):
        return  # 이미 끝난 실행은 마지막 종료 이벤트까지만 보내고 닫는다

    cursor = history[-1].event_id if history else 0
    yield format_sse(
        "replay_complete",
        {"lastEventId": cursor, "pendingConsentId": pending_consent_id(history)},
    )

    while True:
        # 조회 전에 알림 번호를 기억해, 조회와 대기 사이에 저장된 이벤트도 놓치지 않는다.
        version = hub.version
        new = await asyncio.to_thread(chat_manager.get_events, run_id, cursor)
        if not new:
            if not await hub.wait_after(version, keepalive):
                yield ": keepalive\n\n"
            continue
        for event in new:
            cursor = event.event_id
            screen = to_screen(event)
            if screen is None:
                continue
            yield format_sse(*screen, event.event_id)
            if event.type in TERMINAL_EVENTS:
                return


def format_sse(name: str, data: dict[str, Any], event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    body = json.dumps(data, ensure_ascii=False)
    return f"{head}event: {name}\ndata: {body}\n\n"

"""API-06 진행 상황 스트림(SSE).

실행 하나마다 이벤트를 모두 기록해 둔다(RunEvents). 화면이 언제 연결하든, 끊겼다가 다시 연결하든
기록에서 놓친 이벤트를 다시 보내고 그다음부터는 새 이벤트를 실시간으로 보낸다.

    실행(fake_run 또는 Main) ──push──▶ RunEvents.log ──stream()──▶ 화면의 EventSource
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# 이 이벤트가 오면 실행이 끝난 것이다.
TERMINAL_EVENTS = ("done", "error")


@dataclass(frozen=True)
class Event:
    id: int  # 실행별로 1부터 증가
    name: str  # step, token, done ...
    data: dict[str, Any]


@dataclass
class RunEvents:
    """실행 하나의 이벤트 기록."""

    log: list[Event] = field(default_factory=list)
    _changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    @property
    def closed(self) -> bool:
        return bool(self.log) and self.log[-1].name in TERMINAL_EVENTS

    @property
    def last_id(self) -> int:
        return len(self.log)

    async def push(self, name: str, data: dict[str, Any]) -> None:
        """이벤트를 기록하고, 기다리던 스트림들을 깨운다."""
        async with self._changed:
            if self.closed:
                return  # 끝난 실행에는 더 쌓지 않는다
            self.log.append(Event(len(self.log) + 1, name, data))
            self._changed.notify_all()

    def pending_consent_id(self, upto: int) -> int | None:
        """upto번 이벤트까지 봤을 때 아직 결과가 없는 동의 요청 ID."""
        pending: int | None = None
        for event in self.log[:upto]:
            if event.name == "consent_request":
                pending = event.data["consentId"]
            elif event.name == "consent_result":
                if event.data["consentId"] == pending:
                    pending = None
        return pending

    async def stream(self, after_id: int) -> AsyncIterator[str]:
        """after_id 다음 이벤트부터 SSE 형식 문자열로 내보낸다.

        1) 연결 시점까지 쌓인 기록을 다시 보낸다.
        2) 진행 중인 실행이면 replay_complete를 한 번 보낸다(ID 없음, 기록하지 않음).
        3) 이후 새 이벤트를 기다렸다가 보내고, done·error를 보내면 끝낸다.
        """
        async with self._changed:
            snapshot = self.last_id
            closed = self.closed
        for event in self.log[after_id:snapshot]:
            yield format_event(event)
        if closed:
            return  # 이미 끝난 실행은 마지막 종료 이벤트까지만 보내고 닫는다
        yield format_sse(
            "replay_complete",
            {
                "lastEventId": snapshot,
                "pendingConsentId": self.pending_consent_id(snapshot),
            },
        )

        cursor = snapshot
        while True:
            async with self._changed:
                await self._changed.wait_for(lambda: self.last_id > cursor)
                new = self.log[cursor:]
            for event in new:
                yield format_event(event)
                cursor = event.id
                if event.name in TERMINAL_EVENTS:
                    return


def format_sse(name: str, data: dict[str, Any], event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    body = json.dumps(data, ensure_ascii=False)
    return f"{head}event: {name}\ndata: {body}\n\n"


def format_event(event: Event) -> str:
    return format_sse(event.name, event.data, event.id)

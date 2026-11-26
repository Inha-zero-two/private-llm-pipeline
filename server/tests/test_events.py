"""SSE 스트림(stream_run) 단위 테스트. ChatManager 대신 이벤트 목록을 쥔 저장소 대역 사용."""

from __future__ import annotations

import asyncio
from typing import Any

from app.api.events import EventHub, pending_consent_id, stream_run, to_screen
from app.models.schemas import RunEvent


def event(event_id: int, type_: str, payload: Any) -> RunEvent:
    return RunEvent(
        event_id=event_id,
        run_id=1,
        type=type_,
        payload=payload,
        created_at="2026-11-17T00:00:00+00:00",
    )


class EventStore:
    """get_events만 있는 ChatManager 대역."""

    def __init__(self, events: list[RunEvent]) -> None:
        self.events = events

    def get_events(self, run_id: int, after_event_id: int = 0) -> list[RunEvent]:
        return [e for e in self.events if e.event_id > after_event_id]


REQUEST = event(3, "consent_request", {"consentId": 7, "items": []})


def test_to_screen_drops_internal_records_and_run_id() -> None:
    assert to_screen(event(1, "run", {"run_id": 1})) is None
    assert to_screen(event(2, "llm_call", {"input": "원문"})) is None
    assert to_screen(event(3, "run_error", {"code": "X"})) is None
    assert to_screen(event(4, "step", {"run_id": 1, "label": "검출 중"})) == (
        "step",
        {"label": "검출 중", "createdAt": "2026-11-17T00:00:00+00:00"},
    )


def test_pending_consent_id_follows_request_and_result() -> None:
    assert pending_consent_id([]) is None
    assert pending_consent_id([REQUEST]) == 7
    result = event(4, "consent_result", {"consentId": 7, "result": "answered"})
    assert pending_consent_id([REQUEST, result]) is None


def test_running_stream_replays_then_waits_for_new_events() -> None:
    store = EventStore(
        [event(1, "run", {"run_id": 1}), event(2, "step", {"label": "a"}), REQUEST]
    )

    async def scenario() -> list[str]:
        hub = EventHub()
        stream = stream_run(store, hub, 1, 0)  # type: ignore[arg-type]
        out = [await anext(stream) for _ in range(3)]  # 기록 2개 + replay_complete
        # 새 이벤트가 저장되고 알림이 오면 이어서 보내고, done에서 끝낸다
        store.events.append(event(4, "llm_call", {"input": "원문"}))
        store.events.append(event(5, "done", {"endReason": "stopped"}))
        await hub.notify()
        out += [chunk async for chunk in stream]
        return out

    out = asyncio.run(scenario())
    assert out[0].startswith("id: 2\nevent: step\n")
    assert out[1].startswith("id: 3\nevent: consent_request\n")
    # 진행 중인 실행: 마지막 기록 ID와 응답을 기다리는 동의 요청을 알려 준다
    assert out[2] == (
        'event: replay_complete\ndata: {"lastEventId": 3, "pendingConsentId": 7}\n\n'
    )
    assert out[3:] == [
        'id: 5\nevent: done\ndata: {"endReason": "stopped", '
        '"createdAt": "2026-11-17T00:00:00+00:00"}\n\n'
    ]


def test_finished_stream_sends_only_after_id_without_replay_complete() -> None:
    store = EventStore(
        [
            event(1, "step", {"label": "a"}),
            event(2, "token", {"text": "답"}),
            event(3, "done", {"endReason": "info_enough"}),
        ]
    )

    async def scenario() -> list[str]:
        stream = stream_run(store, EventHub(), 1, 1)  # type: ignore[arg-type]
        return [chunk async for chunk in stream]

    out = asyncio.run(scenario())
    assert [chunk.split("\n")[0] for chunk in out] == ["id: 2", "id: 3"]


def test_stream_sends_keepalive_while_waiting() -> None:
    store = EventStore([event(1, "step", {"label": "a"})])

    async def scenario() -> list[str]:
        stream = stream_run(store, EventHub(), 1, 0, keepalive=0.01)  # type: ignore[arg-type]
        return [await anext(stream) for _ in range(3)]

    assert asyncio.run(scenario())[2] == ": keepalive\n\n"

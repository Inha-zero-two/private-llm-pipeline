"""Main이 화면(API-06)으로 보내는 이벤트 검사. 가짜 모듈은 test_main.py의 것을 쓴다."""

import asyncio
from typing import Any

from test_main import FakePlanner, make_main

from app.models.enums import EndReason


def collect(main: Any, question: str = "질문") -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append((event, data))

    main._emit = emit
    asyncio.run(main.run_pipeline("chat-1", question))
    return events


def test_round_trip_emits_steps_frontier_tokens_and_done() -> None:
    log: list[tuple[Any, ...]] = []
    events = collect(make_main(log, planner=FakePlanner(log, enough_after=1)))
    names = [name for name, _ in events]

    assert names[0] == "run"
    assert all(data["run_id"] == "run-1" for _, data in events)
    steps = [data["label"] for name, data in events if name == "step"]
    assert steps == [
        "개인정보 검출 중",
        "허용 설정 적용 중",
        "필요한 정보 정리 중",
        "답변 가능 여부 판단 중",
        "도움 요청문 작성 중",
        "누출 검사 중",
        "외부 모델에 묻는 중 (1회차)",
        "답변 가능 여부 판단 중",
        "최종 답변 생성 중",
    ]
    frontier = [data for name, data in events if name == "frontier"]
    assert len(frontier) == 1
    assert set(frontier[0]) == {"run_id", "request", "answer"}
    assert "token" in names
    # 완료는 정리(finish_run)까지 끝난 뒤 마지막에 한 번
    assert names[-1] == "done"
    assert names.count("done") == 1
    assert events[-1][1]["end_reason"] == EndReason.INFO_ENOUGH
    assert events[-1][1]["answer_message_id"] is not None


def test_stop_emits_done_stopped_after_cleanup() -> None:
    log: list[tuple[Any, ...]] = []
    main = make_main(log, planner=FakePlanner(log, enough_after=99))
    events: list[tuple[str, dict[str, Any]]] = []

    async def scenario() -> None:
        asked = asyncio.Event()

        async def emit(event: str, data: dict[str, Any]) -> None:
            events.append((event, data))
            if event == "step" and data["label"].startswith("외부 모델에 묻는 중"):
                asked.set()
                await asyncio.sleep(10)  # 여기서 멈춰 중지를 기다린다

        main._emit = emit
        task = asyncio.create_task(main.run_pipeline("chat-1", "질문"))
        await asked.wait()
        await main.stop("run-1")
        assert task.cancelled()

    asyncio.run(scenario())
    assert events[-1][0] == "done"
    assert events[-1][1]["end_reason"] == EndReason.STOPPED
    assert "stopped" not in [name for name, _ in events]

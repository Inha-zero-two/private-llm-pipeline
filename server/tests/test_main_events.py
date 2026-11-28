"""Main이 화면(API-06)으로 보내는 이벤트 검사. 가짜 모듈은 test_main.py의 것을 쓴다."""

import asyncio
from typing import Any

from test_main import FakePlanner, make_main

from app.models.enums import EndReason

Events = list[tuple[str, dict[str, Any], int | None]]


def collector() -> tuple[Events, Any]:
    events: Events = []

    async def emit(event: str, data: dict[str, Any], event_id: int | None) -> None:
        events.append((event, data, event_id))

    return events, emit


def test_round_trip_emits_steps_frontier_tokens_and_done() -> None:
    log: list[tuple[Any, ...]] = []
    events, emit = collector()
    main = make_main(log, planner=FakePlanner(log, enough_after=1), emit=emit)
    asyncio.run(main.run_pipeline(1, "질문"))
    names = [name for name, _, _ in events]

    assert names[0] == "run"
    # 진행 이벤트는 실행 ID를 담고, 저장된 이벤트 ID가 1부터 차례로 붙는다
    assert all(data["run_id"] == 1 for name, data, _ in events if name != "done")
    assert [event_id for _, _, event_id in events] == list(range(1, len(events) + 1))
    steps = [data["label"] for name, data, _ in events if name == "step"]
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
    frontier = [data for name, data, _ in events if name == "frontier"]
    assert len(frontier) == 1
    assert set(frontier[0]) == {"run_id", "request", "answer"}
    request = next(data for name, data, _ in events if name == "frontier_request")
    result = next(data for name, data, _ in events if name == "frontier_result")
    assert request["request"] == frontier[0]["request"]
    assert request["queryId"] == result["queryId"]
    assert request["round"] == result["round"] == 1
    assert result["status"] == "ok"
    assert names.index("frontier_request") < names.index("frontier_result")
    assert "token" in names
    # 완료는 정리(finish_run)가 저장한 종료 이벤트를 마지막에 한 번 보낸다
    assert names[-1] == "done"
    assert names.count("done") == 1
    assert events[-1][1]["endReason"] == EndReason.INFO_ENOUGH
    assert events[-1][1]["answerMessageId"] is not None


def test_stop_emits_done_stopped_after_cleanup() -> None:
    log: list[tuple[Any, ...]] = []
    events: Events = []
    asked = asyncio.Event()

    async def emit(event: str, data: dict[str, Any], event_id: int | None) -> None:
        events.append((event, data, event_id))
        if event == "step" and data["label"].startswith("외부 모델에 묻는 중"):
            asked.set()
            await asyncio.sleep(10)  # 여기서 멈춰 중지를 기다린다

    main = make_main(log, planner=FakePlanner(log, enough_after=99), emit=emit)

    async def scenario() -> None:
        task = asyncio.create_task(main.run_pipeline(1, "질문"))
        await asked.wait()
        await main.stop(1)
        assert task.cancelled()

    asyncio.run(scenario())
    assert events[-1][0] == "done"
    assert events[-1][1]["endReason"] == EndReason.STOPPED
    assert "stopped" not in [name for name, _, _ in events]


def test_failed_query_keeps_request_and_emits_failed_result() -> None:
    from test_main import FakeFrontierClient

    log: list[tuple[Any, ...]] = []
    events, emit = collector()
    frontier = FakeFrontierClient(log)
    frontier.fail_send = True
    main = make_main(
        log,
        planner=FakePlanner(log, enough_after=1),
        frontier_client=frontier,
        emit=emit,
    )
    asyncio.run(main.run_pipeline(1, "질문"))
    request = next(data for name, data, _ in events if name == "frontier_request")
    result = next(data for name, data, _ in events if name == "frontier_result")
    assert request["request"] == "요청문"
    assert request["queryId"] == result["queryId"]
    assert result["status"] == "failed"
    assert result["answer"] is None
    assert not any(name == "frontier" for name, _, _ in events)
    assert events[-1][0] == "done"  # 기존 offline 종료 정책은 유지

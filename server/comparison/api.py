"""비교 결과 조회 · 실시간 전송 · 재시도 API.

app의 오류 클래스를 쓸 수 없으므로 명세서 형식 {"code", "message"}의 오류를 직접 만든다.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import APIRouter, Path
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .manager import (
    ComparisonBusyError,
    ComparisonManager,
    NotRetryableError,
    RecordNotFoundError,
)
from .models import CamelModel, Item, Kind, RunningRef

BUSY_CODE = "COMPARISON_RUNNING"
BUSY_MESSAGE = "비교 답변을 만드는 중입니다."

ManagerGetter = Callable[[], ComparisonManager | None]


class StatusOut(CamelModel):
    enabled: bool
    running: RunningRef | None


class ChatComparisonsOut(CamelModel):
    registered: bool
    items: list[Item]


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def _sse(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream(manager: ComparisonManager | None, run_id: int) -> AsyncIterator[str]:
    if manager is None:
        yield _sse("snapshot", {"items": []})
        yield _sse("done", {})
        return
    async for event in manager.events(run_id):
        yield ": keepalive\n\n" if event is None else _sse(*event)


def create_router(get_manager: ManagerGetter) -> APIRouter:
    """get_manager: 비교 기능. 없거나 준비하지 못했으면 None."""
    router = APIRouter()

    @router.get("/api/comparisons/status")
    async def comparison_status() -> StatusOut:
        manager = get_manager()
        if manager is None or not manager.enabled:
            return StatusOut(enabled=False, running=None)
        return StatusOut(enabled=True, running=manager.busy())

    @router.get("/api/chats/{chat_id}/comparisons")
    async def chat_comparisons(chat_id: int = Path(ge=1)) -> ChatComparisonsOut:
        manager = get_manager()
        if manager is None:
            return ChatComparisonsOut(registered=False, items=[])
        registered, items = await manager.list_chat(chat_id)
        return ChatComparisonsOut(registered=registered, items=items)

    @router.post("/api/runs/{run_id}/comparisons/{kind}/retry")
    async def retry_comparison(kind: Kind, run_id: int = Path(ge=1)) -> Response:
        manager = get_manager()
        try:
            if manager is None:
                raise NotRetryableError
            await manager.retry(run_id, kind)
        except RecordNotFoundError:
            return _error(404, "NOT_FOUND", "비교 기록을 찾을 수 없습니다.")
        except NotRetryableError:
            return _error(409, "NOT_RETRYABLE", "다시 시도할 수 없는 비교입니다.")
        except ComparisonBusyError:
            return _error(
                409, BUSY_CODE, "답변을 만드는 중에는 다시 시도할 수 없습니다."
            )
        return Response(status_code=200)

    @router.get("/api/runs/{run_id}/comparisons/events")
    async def comparison_events(run_id: int = Path(ge=1)) -> StreamingResponse:
        return StreamingResponse(
            _stream(get_manager(), run_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router

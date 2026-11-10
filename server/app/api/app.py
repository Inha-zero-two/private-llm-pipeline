"""화면 ↔ 서버 API (docs/screen-api-spec.md의 API-01 ~ API-11).

실행 (server/ 폴더에서):
    python -m uvicorn app.api.app:app --reload --port 8000

web은 vite 프록시로 /api 요청을 이 서버(127.0.0.1:8000)에 보낸다.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.fake_interpret import interpret_consent, parse_rules
from app.api.fake_run import fake_run
from app.api.schemas import (
    ChatCreated,
    ChatDetail,
    ChatSummary,
    ConsentBody,
    ConsentReply,
    PrivacySetting,
    RunDetail,
    RunStarted,
    RunSummary,
    SendBody,
    SettingBody,
    SettingSaved,
)
from app.api.store import ChatRecord, RunRecord, Store, now_iso

# 로컬 LLM이 동의·설정문을 해석하는 시간 흉내 (초)
INTERPRET_SECONDS = 0.5


class ApiError(Exception):
    """명세서 형식 {"code", "message"}로 내보낼 오류."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def create_app(delay: float = 1.0) -> FastAPI:
    """delay: 가짜 실행의 대기 시간 배율. 테스트는 0으로 기다림 없이 돌린다.

    환경변수 FAKE_DELAY로도 바꿀 수 있다 (예: FAKE_DELAY=3 이면 세 배 느리게).
    """
    store = Store()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        # 서버를 끌 때 아직 도는 실행을 정리한다
        tasks = [r.task for r in store.runs.values() if r.task and not r.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="private-llm-pipeline API (가짜 실행)", lifespan=lifespan)
    app.state.store = store

    # 오류 응답

    @app.exception_handler(ApiError)
    async def on_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(
        _: Request, __: RequestValidationError
    ) -> JSONResponse:
        return _error(422, "VALIDATION_ERROR", "요청 형식이 올바르지 않습니다.")

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # FastAPI가 직접 내는 오류도 명세서 형식으로 바꾼다.
        # 본문 JSON을 읽지 못한 400은 명세서의 요청 형식 오류(422)로 본다.
        if exc.status_code == 400:
            return _error(422, "VALIDATION_ERROR", "요청 본문을 읽지 못했습니다.")
        if exc.status_code == 404:
            return _error(404, "NOT_FOUND", "없는 주소입니다.")
        return _error(exc.status_code, "HTTP_ERROR", str(exc.detail))

    @app.exception_handler(Exception)
    async def on_unexpected(_: Request, __: Exception) -> JSONResponse:
        return _error(500, "INTERNAL_ERROR", "서버에서 요청을 처리하지 못했습니다.")

    def get_chat(chat_id: int) -> ChatRecord:
        chat = store.chats.get(chat_id)
        if chat is None:
            raise ApiError(404, "NOT_FOUND", "채팅을 찾을 수 없습니다.")
        return chat

    def get_run(run_id: int) -> RunRecord:
        run = store.runs.get(run_id)
        if run is None:
            raise ApiError(404, "NOT_FOUND", "실행을 찾을 수 없습니다.")
        return run

    # API-01 ~ 04 채팅

    @app.post("/api/chats")
    async def create_chat() -> ChatCreated:
        return ChatCreated(chat_id=store.create_chat().chat_id)

    @app.get("/api/chats")
    async def list_chats() -> list[ChatSummary]:
        return [
            ChatSummary(chat_id=c.chat_id, title=c.title) for c in store.list_chats()
        ]

    @app.get("/api/chats/{chat_id}")
    async def get_chat_detail(chat_id: int = Path(ge=1)) -> ChatDetail:
        chat = get_chat(chat_id)
        runs = [store.runs[r] for r in chat.run_ids]
        return ChatDetail(
            chat_id=chat.chat_id,
            title=chat.title,
            messages=[store.messages[m] for m in chat.message_ids],
            runs=[
                RunSummary(
                    run_id=r.run_id,
                    user_message_id=r.user_message_id,
                    status=r.status,
                    notice=r.notice,
                )
                for r in runs
            ],
        )

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: int = Path(ge=1)) -> Response:
        chat = get_chat(chat_id)
        if store.is_running(chat):
            raise ApiError(409, "CHAT_RUNNING", "실행 중인 채팅은 삭제할 수 없습니다.")
        store.delete_chat(chat_id)
        return Response(status_code=200)

    # API-05 ~ 08, 11 실행

    @app.post("/api/chats/{chat_id}/messages")
    async def send_message(body: SendBody, chat_id: int = Path(ge=1)) -> RunStarted:
        get_chat(chat_id)
        text = body.text.strip()
        if not text:
            raise ApiError(422, "VALIDATION_ERROR", "질문을 입력해 주세요.")
        message = store.save_message(chat_id, "user", text)
        run = store.start_run(chat_id, message.message_id)
        # 설정은 실행을 시작할 때의 것으로 고정한다 (새 설정은 다음 실행부터)
        rules = list(store.setting.rules)
        run.task = asyncio.create_task(fake_run(store, run, text, rules, delay))
        return RunStarted(run_id=run.run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: int = Path(ge=1),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        run = get_run(run_id)
        after = 0
        if last_event_id is not None:
            try:
                after = int(last_event_id)
            except ValueError:
                after = -1
            if not 0 <= after <= run.events.last_id:
                raise ApiError(
                    422, "VALIDATION_ERROR", "재접속 이벤트 ID가 올바르지 않습니다."
                )
        return StreamingResponse(
            run.events.stream(after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: int = Path(ge=1)) -> Response:
        run = get_run(run_id)
        if run.status == "finished" or run.task is None:
            raise ApiError(409, "RUN_FINISHED", "이미 종료된 실행입니다.")
        run.task.cancel()  # 실제 종료는 스트림의 done(stopped)으로 알린다
        return Response(status_code=200)

    @app.post("/api/runs/{run_id}/consent")
    async def answer_consent(
        body: ConsentBody, run_id: int = Path(ge=1)
    ) -> ConsentReply:
        run = get_run(run_id)

        def check() -> None:
            if run.status == "finished":
                raise ApiError(409, "RUN_FINISHED", "이미 종료된 실행입니다.")
            pending = run.pending_consent()
            if pending is None or pending.consent_id != body.consent_id:
                raise ApiError(
                    409,
                    "NO_PENDING_CONSENT",
                    "현재 응답할 수 있는 동의 요청이 아닙니다.",
                )

        check()
        text = body.text.strip()
        if not text:
            raise ApiError(422, "VALIDATION_ERROR", "응답을 입력해 주세요.")

        await asyncio.sleep(INTERPRET_SECONDS * delay)  # 로컬 LLM이 해석하는 시간
        check()  # 그사이 중지됐거나 다른 응답이 먼저 들어왔으면 적용하지 않는다
        pending = run.pending_consent()
        assert pending is not None

        result = interpret_consent(text, pending.items)
        if not result.ok:
            return ConsentReply(ok=False, message=result.message)

        pending.result = "answered"
        pending.response_text = text
        pending.decisions = result.decisions
        pending.responded_at = now_iso()
        await run.events.push(
            "consent_result",
            {
                "consentId": pending.consent_id,
                "result": "answered",
                "text": text,
                "decisions": [d.model_dump(by_alias=True) for d in result.decisions],
            },
        )
        if run.consent_waiter is not None and not run.consent_waiter.done():
            run.consent_waiter.set_result(result.decisions)
        return ConsentReply(ok=True, decisions=result.decisions, message=result.message)

    @app.get("/api/runs/{run_id}")
    async def get_run_detail(run_id: int = Path(ge=1)) -> RunDetail:
        run = get_run(run_id)
        return RunDetail(
            run_id=run.run_id,
            chat_id=run.chat_id,
            user_message_id=run.user_message_id,
            answer_message_id=run.answer_message_id,
            status=run.status,
            end_reason=run.end_reason,
            notice=run.notice,
            error_code=run.error_code,
            frontier_queries=run.frontier_queries,
            consents=run.consents,
        )

    # API-09 · 10 공개 허용 설정

    @app.get("/api/settings/privacy")
    async def get_setting() -> PrivacySetting:
        return store.setting

    @app.put("/api/settings/privacy")
    async def save_setting(body: SettingBody) -> SettingSaved:
        await asyncio.sleep(INTERPRET_SECONDS * delay)  # 로컬 LLM이 해석하는 시간
        text = body.text.strip()
        rules = parse_rules(text) if text else []
        if text and not rules:
            return SettingSaved(
                ok=False,
                rules=store.setting.rules,
                message=(
                    "로컬 LLM이 공개 허용 대상을 확인하지 못했습니다. "
                    '"직무는 공개해도 돼"처럼 대상과 허용 여부를 적어 주세요.'
                ),
            )
        store.save_setting(text, rules)
        return SettingSaved(
            ok=True,
            rules=rules,
            message="설정을 저장했습니다. 다음 실행부터 적용됩니다.",
        )

    return app


app = create_app(delay=float(os.environ.get("FAKE_DELAY", "1")))

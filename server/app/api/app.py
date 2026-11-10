"""화면 ↔ 서버 API (docs/screen-api-spec.md의 API-01 ~ API-11).

실행 (server/ 폴더에서, 저장소 루트를 PYTHONPATH에 넣어야 래퍼 src를 찾는다):
    PYTHONPATH=.. python -m uvicorn app.api.app:app --port 8000

환경변수 PIPELINE으로 실행 방식을 고른다.
    main (기본): 실제 Main + 있는 모듈은 실제 것 + 없는 모듈은 대역. 모델 접속이 필요하다.
                 원격 서버를 쓰려면 src/localLLM_API_proxy/keys/에 llama_api_key*.key를 두고
                 CUDA_VISIBLE_DEVICES=-1로 실행한다. (CUDA GPU가 보이는데 CUDA용
                 llama-cpp-python이 없으면 래퍼가 원격으로 넘어가지 않고 멈추기 때문)
    fake       : 모델 없이 fake_run이 흐름만 흉내 낸다. FAKE_DELAY로 속도 조절.

환경변수 LLM=fake를 함께 주면 PIPELINE=main에서 모델 자리만 규칙 기반 가짜 모델(fake_llm.py)로
바꾼다. 모델 파일·키 없이 실제 Main · 검출 · 판단 · 메모 · Runner를 화면에서 확인할 때 쓴다.

web은 vite 프록시로 /api 요청을 이 서버(127.0.0.1:8000)에 보낸다.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Header, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.backends import Backend, BusyError, FakeBackend, MainBackend
from app.api.fake_interpret import interpret_consent, parse_rules
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


def create_app(
    pipeline: Literal["main", "fake"] = "main",
    *,
    delay: float = 1.0,
    runner_factory: Callable[..., Any] | None = None,
    fake_llm: bool = False,
) -> FastAPI:
    """pipeline: 실행 방식 (모듈 설명 참고).
    delay: 가짜 실행·해석의 대기 시간 배율. 테스트는 0으로 기다림 없이 돌린다.
    runner_factory: MainBackend가 LocalLLMRunner 대신 쓸 생성 함수 (테스트용).
    fake_llm: MainBackend의 모델 자리에 규칙 기반 가짜 모델을 끼운다.
    """
    store = Store()
    backend: Backend = (
        MainBackend(store, runner_factory, fake_llm=fake_llm)
        if pipeline == "main"
        else FakeBackend(store, delay)
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await backend.startup()
        yield
        await backend.shutdown()  # 아직 도는 실행을 정리한다

    app = FastAPI(title=f"private-llm-pipeline API ({backend.name})", lifespan=lifespan)
    app.state.store = store
    app.state.backend = backend

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
        try:
            run_id = await backend.start(chat_id, text)
        except BusyError:
            # 명세서에 없는 경우: 로컬 모델이 하나라 실행을 동시에 돌리지 않는다
            raise ApiError(
                409, "RUN_IN_PROGRESS", "다른 질문에 답하는 중입니다."
            ) from None
        return RunStarted(run_id=run_id)

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
        await backend.stop(run)  # 실제 종료는 스트림의 done(stopped)으로 알린다
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


app = create_app(
    "fake" if os.environ.get("PIPELINE") == "fake" else "main",
    delay=float(os.environ.get("FAKE_DELAY", "1")),
    fake_llm=os.environ.get("LLM") == "fake",
)

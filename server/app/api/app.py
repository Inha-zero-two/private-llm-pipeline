"""화면 ↔ 서버 API (docs/screen-api-spec.md의 API-01 ~ API-11).

실행 (server/ 폴더에서, 저장소 루트를 PYTHONPATH에 넣어야 래퍼 src를 찾는다):
    PYTHONPATH=.. python -m uvicorn app.api.app:app --port 8000

모든 단계가 실제 모듈로 돈다. 실행에 필요한 것:
    - 로컬 LLM: 원격 서버를 쓰려면 src/localLLM_API_proxy/keys/에 llama_api_key*.key를 두고
      CUDA_VISIBLE_DEVICES=-1로 실행한다. (CUDA GPU가 보이는데 CUDA용
      llama-cpp-python이 없으면 래퍼가 원격으로 넘어가지 않고 멈추기 때문)
    - 프론티어: server/.env의 FRONTIER_API_KEY (server/.env.example 참고).
      키가 없거나 연결되지 않으면 실행은 offline으로 끝나고 로컬 모델만으로 답한다.
    - 채팅·실행 기록 DB: CHAT_DB_PATH (기본 server/data/chat.sqlite3)

web은 vite 프록시로 /api 요청을 이 서버(127.0.0.1:8000)에 보낸다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path as FilePath
from typing import Any, TypeVar

from fastapi import FastAPI, Header, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.events import stream_run
from app.api.schemas import (
    ChatCreated,
    ChatDetail,
    ChatSummary,
    ConsentBody,
    ConsentReply,
    PrivacySetting,
    RunDetail,
    RunStarted,
    SendBody,
    SettingBody,
    SettingSaved,
)
from app.api.services import (
    BusyError,
    FrontierFactory,
    Pipeline,
    PromptChooserFactory,
    RunnerFactory,
)
from app.config import db_path as configured_db_path
from app.config import setting
from app.data.chat_manager import StorageError
from app.models.errors import ErrorCode, ErrorStage, PipelineError, describe_error
from app.privacy.consent_manager import NoPendingConsentError
from app.runtime.frontier_client import DEFAULT_BASE_URL, DEFAULT_MODEL, MODEL_PRICES
from comparison.api import BUSY_CODE, BUSY_MESSAGE, create_router
from comparison.manager import ComparisonManager, create_manager
from comparison.models import ComparisonSettings

T = TypeVar("T")
# 비교 기능을 만드는 함수. create_manager와 같은 인자를 받는다.
ComparisonFactory = Callable[..., ComparisonManager]

log = logging.getLogger(__name__)

# ChatManager 오류 코드 → HTTP 상태
STORAGE_STATUS = {"NOT_FOUND": 404}


class ApiError(Exception):
    """명세서 형식 {"code", "message"}로 내보낼 오류."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


async def db(func: Callable[..., T], *args: Any) -> T:
    """ChatManager 호출 등 파일 I/O·모델 호출은 별도 스레드에서 한다."""
    return await asyncio.to_thread(func, *args)


def _local_llm_unavailable() -> ApiError:
    return ApiError(
        503,
        "LOCAL_LLM_UNAVAILABLE",
        "로컬 LLM에 요청하지 못했습니다. 다시 시도해 주세요.",
    )


def comparison_settings(db_path: FilePath) -> ComparisonSettings:
    """비교 기능에 넘길 값. 프론티어 주소·모델·단가는 파이프라인과 같은 설정을 쓴다."""
    enabled = (setting("COMPARISON_ENABLED", default="on") or "").strip().lower()
    model = setting("FRONTIER_MODEL", default=DEFAULT_MODEL) or DEFAULT_MODEL
    prices = MODEL_PRICES.get(model)
    return ComparisonSettings(
        enabled=enabled not in {"0", "false", "off", "no"},
        db_path=db_path,
        frontier_api_key=setting("FRONTIER_API_KEY"),
        frontier_base_url=setting("FRONTIER_BASE_URL", default=DEFAULT_BASE_URL)
        or DEFAULT_BASE_URL,
        frontier_model=model,
        input_price=prices[0] if prices else None,
        output_price=prices[1] if prices else None,
    )


def create_app(
    *,
    db_path: FilePath | None = None,
    runner_factory: RunnerFactory | None = None,
    frontier_factory: FrontierFactory | None = None,
    prompt_chooser_factory: PromptChooserFactory | None = None,
    comparison_factory: ComparisonFactory | None = None,
) -> FastAPI:
    """db_path: 채팅·실행 기록 DB. 생략하면 CHAT_DB_PATH 설정(기본 server/data/chat.sqlite3).
    runner_factory · frontier_factory · prompt_chooser_factory: 모듈 생성 함수.
    생략하면 실제 LocalLLMRunner·FrontierClient·DynamicPromptChooser를 만든다.
    comparison_factory: 비교 기능을 만드는 함수. 생략하면 비교 기능이 없다.
    """
    db_file = db_path or configured_db_path()
    pipeline = Pipeline(
        db_file,
        runner_factory=runner_factory,
        frontier_factory=frontier_factory,
        prompt_chooser_factory=prompt_chooser_factory,
    )
    comparison: ComparisonManager | None = None

    def latest_run_id(chat_id: int) -> int | None:
        try:
            runs = pipeline.services.chat_manager.get_chat(chat_id).runs
        except StorageError:
            return None
        return max((run.run_id for run in runs), default=None)

    def prepare_comparison() -> ComparisonManager | None:
        """비교 기능을 준비한다. 실패해도 파이프라인은 비교 없이 시작한다."""
        if comparison_factory is None:
            return None
        try:
            return comparison_factory(
                comparison_settings(db_file),
                pipeline_busy=lambda: pipeline.is_busy,
                latest_run_id=latest_run_id,
            )
        except Exception as error:
            log.error("Comparison startup failed: %s", type(error).__name__)
            return None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal comparison
        await pipeline.startup()
        comparison = await db(prepare_comparison)
        yield
        if comparison is not None:
            try:
                await comparison.shutdown()
            except Exception as error:
                log.error("Comparison shutdown failed: %s", type(error).__name__)
        await pipeline.shutdown()  # 아직 도는 실행을 정리한다

    app = FastAPI(title="private-llm-pipeline API", lifespan=lifespan)
    app.state.pipeline = pipeline
    app.include_router(create_router(lambda: comparison))

    # 오류 응답

    @app.exception_handler(ApiError)
    async def on_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(StorageError)
    async def on_storage_error(_: Request, exc: StorageError) -> JSONResponse:
        # NOT_FOUND 외의 코드(CHAT_RUNNING · RUN_FINISHED 등)는 상태 충돌이다
        return _error(STORAGE_STATUS.get(exc.code, 409), exc.code, str(exc))

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

    @app.exception_handler(PipelineError)
    @app.exception_handler(Exception)
    async def on_unexpected(_: Request, error: Exception) -> JSONResponse:
        info = describe_error(error)
        logging.getLogger(__name__).error(
            "API failure code=%s stage=%s",
            info.code,
            info.stage,
            exc_info=(type(error), error, error.__traceback__),
        )
        status = (
            503
            if info.code
            in {ErrorCode.LOCAL_LLM_UNAVAILABLE, ErrorCode.LOCAL_LLM_TIMEOUT}
            else 500
        )
        return JSONResponse(status_code=status, content=info.model_dump(mode="json"))

    # API-01 ~ 04 채팅

    @app.post("/api/chats")
    async def create_chat() -> ChatCreated:
        chats = pipeline.services.chat_manager
        chat_id = await db(chats.create_chat)
        if comparison is not None:
            try:
                await comparison.on_chat_created(chat_id)
            except Exception as error:
                log.error("Comparison register failed: %s", type(error).__name__)
        return ChatCreated(chat_id=chat_id)

    @app.get("/api/chats")
    async def list_chats() -> list[ChatSummary]:
        chats = pipeline.services.chat_manager
        return [
            ChatSummary.model_validate(chat.model_dump())
            for chat in await db(chats.list_chats)
        ]

    @app.get("/api/chats/{chat_id}")
    async def get_chat_detail(chat_id: int = Path(ge=1)) -> ChatDetail:
        chats = pipeline.services.chat_manager
        return ChatDetail.model_validate(
            (await db(chats.get_chat, chat_id)).model_dump()
        )

    @app.delete("/api/chats/{chat_id}")
    async def delete_chat(chat_id: int = Path(ge=1)) -> Response:
        # 실행 중인 채팅이면 ChatManager가 CHAT_RUNNING(409)을 낸다
        await db(pipeline.services.chat_manager.delete_chat, chat_id)
        if comparison is not None:
            comparison.discard(chat_id)  # 비교 기록은 채팅과 함께 지워졌다
        await db(pipeline.services.main.local_llm_runner.forget_chat, chat_id)
        return Response(status_code=200)

    # API-05 ~ 08, 11 실행

    @app.post("/api/chats/{chat_id}/messages")
    async def send_message(body: SendBody, chat_id: int = Path(ge=1)) -> RunStarted:
        await db(pipeline.services.chat_manager.get_chat, chat_id)  # 없으면 404
        text = body.text.strip()
        if not text:
            raise ApiError(422, "VALIDATION_ERROR", "질문을 입력해 주세요.")
        # 명세서에 없는 경우: 로컬 모델이 하나라 실행을 동시에 돌리지 않는다
        in_progress = ApiError(409, "RUN_IN_PROGRESS", "다른 질문에 답하는 중입니다.")
        # 아래 두 확인과 pipeline.start() 사이에 await를 두지 않는다.
        # 다른 질문이나 비교 재시도가 그 사이에 끼어들지 못하게 하기 위해서다.
        if pipeline.is_busy:
            raise in_progress
        if comparison is not None and not comparison.try_reserve():
            raise ApiError(409, BUSY_CODE, BUSY_MESSAGE)
        try:
            run_id = await pipeline.start(chat_id, text)
        except BaseException as error:
            if comparison is not None:
                comparison.release()
            if isinstance(error, BusyError):
                raise in_progress from None
            raise
        if comparison is not None:
            try:
                await comparison.start(chat_id, run_id, text)
            except Exception as error:
                # 비교를 시작하지 못해도 파이프라인은 계속한다
                log.error("Comparison start failed: %s", type(error).__name__)
        return RunStarted(run_id=run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: int = Path(ge=1),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        chats = pipeline.services.chat_manager
        events = await db(chats.get_events, run_id)  # 없는 실행이면 404
        after = 0
        if last_event_id is not None:
            try:
                after = int(last_event_id)
            except ValueError:
                after = -1
            last = events[-1].event_id if events else 0
            if not 0 <= after <= last:
                raise ApiError(
                    422, "VALIDATION_ERROR", "재접속 이벤트 ID가 올바르지 않습니다."
                )
        return StreamingResponse(
            stream_run(chats, pipeline.hub, run_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: int = Path(ge=1)) -> Response:
        run = await db(pipeline.services.chat_manager.get_run, run_id)
        # pipeline.stop()은 정리가 끝날 때까지 기다리므로 비교에 먼저 알린다
        compared = comparison is not None and comparison.stop(run_id)
        stopped = run.status != "finished" and await pipeline.stop(run_id)
        if not (stopped or compared):
            raise ApiError(409, "RUN_FINISHED", "이미 종료된 실행입니다.")
        # 실제 종료는 스트림의 done(stopped)으로 알린다
        return Response(status_code=200)

    @app.post("/api/runs/{run_id}/consent")
    async def answer_consent(
        body: ConsentBody, run_id: int = Path(ge=1)
    ) -> ConsentReply:
        services = pipeline.services
        run = await db(services.chat_manager.get_run, run_id)
        if run.status == "finished":
            raise ApiError(409, "RUN_FINISHED", "이미 종료된 실행입니다.")
        if not body.text.strip():
            raise ApiError(422, "VALIDATION_ERROR", "응답을 입력해 주세요.")
        try:
            # 로컬 LLM이 응답을 항목별 결정으로 해석한다. (빠른 응답 버튼은 모델 없이)
            reply = await services.consent_manager.receive(
                run_id, body.consent_id, body.text
            )
        except NoPendingConsentError:
            raise ApiError(
                409, "NO_PENDING_CONSENT", "현재 응답할 수 있는 동의 요청이 아닙니다."
            ) from None
        except (ConnectionError, TimeoutError) as error:
            info = describe_error(error, ErrorStage.INTERPRET_CONSENT)
            raise PipelineError(info.code, info.stage) from error
        return ConsentReply.model_validate(reply.model_dump())

    @app.get("/api/runs/{run_id}")
    async def get_run_detail(run_id: int = Path(ge=1)) -> RunDetail:
        run = await db(pipeline.services.chat_manager.get_run, run_id)
        return RunDetail.model_validate(run.model_dump())

    # API-09 · 10 공개 허용 설정

    @app.get("/api/settings/privacy")
    async def get_setting() -> PrivacySetting:
        setting = await db(pipeline.services.privacy_settings.load)
        return PrivacySetting.model_validate(setting.model_dump())

    @app.put("/api/settings/privacy")
    async def save_setting(body: SettingBody) -> SettingSaved:
        try:
            # 로컬 LLM이 설정문을 규칙으로 해석한다. 해석 실패면 기존 설정을 유지한다.
            result = await db(pipeline.services.privacy_settings.save, body.text)
        except (ConnectionError, TimeoutError) as error:
            info = describe_error(error, ErrorStage.PARSE_RULES)
            raise PipelineError(info.code, info.stage) from error
        except InterruptedError:
            raise _local_llm_unavailable() from None
        return SettingSaved.model_validate(result.model_dump())

    return app


app = create_app(comparison_factory=create_manager)

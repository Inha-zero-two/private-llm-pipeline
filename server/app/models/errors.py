"""오류 원인·발생 단계와 사용자 안내. 예외 원문은 공개 응답에 사용하지 않는다."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class ErrorCode(StrEnum):
    LOCAL_LLM_UNAVAILABLE = "LOCAL_LLM_UNAVAILABLE"
    LOCAL_LLM_TIMEOUT = "LOCAL_LLM_TIMEOUT"
    MODEL_RESPONSE_INVALID = "MODEL_RESPONSE_INVALID"
    MODEL_OUTPUT_LIMIT = "MODEL_OUTPUT_LIMIT"
    INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"
    COMPACTION_FAILED = "COMPACTION_FAILED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    STATE_CONSISTENCY_ERROR = "STATE_CONSISTENCY_ERROR"
    CLEANUP_ERROR = "CLEANUP_ERROR"
    RUN_INTERRUPTED = "RUN_INTERRUPTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    FRONTIER_UNAVAILABLE = "FRONTIER_UNAVAILABLE"
    FRONTIER_TIMEOUT = "FRONTIER_TIMEOUT"
    FRONTIER_AUTH_ERROR = "FRONTIER_AUTH_ERROR"
    FRONTIER_RATE_LIMITED = "FRONTIER_RATE_LIMITED"
    FRONTIER_QUOTA_EXCEEDED = "FRONTIER_QUOTA_EXCEEDED"
    FRONTIER_REQUEST_REJECTED = "FRONTIER_REQUEST_REJECTED"
    FRONTIER_RESPONSE_INVALID = "FRONTIER_RESPONSE_INVALID"


class ErrorStage(StrEnum):
    STARTUP = "STARTUP"
    DETECT = "DETECT"
    MATCH_RULES = "MATCH_RULES"
    PARSE_RULES = "PARSE_RULES"
    NEEDS = "NEEDS"
    JUDGE = "JUDGE"
    CONSENT = "CONSENT"
    INTERPRET_CONSENT = "INTERPRET_CONSENT"
    WRITE = "WRITE"
    REWRITE = "REWRITE"
    GUARD = "GUARD"
    FRONTIER = "FRONTIER"
    MEMO = "MEMO"
    MEMO_SUMMARY = "MEMO_SUMMARY"
    COMPACTION = "COMPACTION"
    ANSWER = "ANSWER"
    STORAGE = "STORAGE"
    CLEANUP = "CLEANUP"
    PIPELINE = "PIPELINE"


MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.LOCAL_LLM_UNAVAILABLE: "로컬 모델에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
    ErrorCode.LOCAL_LLM_TIMEOUT: "로컬 모델의 응답 시간이 초과되었습니다.",
    ErrorCode.MODEL_RESPONSE_INVALID: "모델 응답을 처리하지 못했습니다.",
    ErrorCode.MODEL_OUTPUT_LIMIT: "모델이 출력 한도 안에서 응답을 완성하지 못했습니다.",
    ErrorCode.INPUT_LIMIT_EXCEEDED: "모델이 처리할 수 있는 입력 길이를 초과했습니다.",
    ErrorCode.COMPACTION_FAILED: "대화 또는 수집한 정보를 처리 가능한 길이로 줄이지 못했습니다.",
    ErrorCode.CONFIGURATION_ERROR: "모델 또는 서버 설정을 확인해야 합니다.",
    ErrorCode.STORAGE_ERROR: "기록을 읽거나 저장하지 못했습니다.",
    ErrorCode.STATE_CONSISTENCY_ERROR: "실행 상태를 확인하지 못했습니다.",
    ErrorCode.CLEANUP_ERROR: "실행 종료 후 상태 정리를 완료하지 못했습니다.",
    ErrorCode.RUN_INTERRUPTED: "앱 종료로 실행이 중단되었습니다.",
    ErrorCode.INTERNAL_ERROR: "실행 중 오류가 발생했습니다.",
    ErrorCode.FRONTIER_UNAVAILABLE: "외부 모델에 연결하지 못했습니다.",
    ErrorCode.FRONTIER_TIMEOUT: "외부 모델의 응답 시간이 초과되었습니다.",
    ErrorCode.FRONTIER_AUTH_ERROR: "외부 모델의 인증 또는 접근 권한을 확인해야 합니다.",
    ErrorCode.FRONTIER_RATE_LIMITED: "외부 모델의 요청 빈도 제한에 도달했습니다.",
    ErrorCode.FRONTIER_QUOTA_EXCEEDED: "외부 모델의 사용 할당량이 소진되었습니다.",
    ErrorCode.FRONTIER_REQUEST_REJECTED: "외부 모델이 요청을 수락하지 않았습니다.",
    ErrorCode.FRONTIER_RESPONSE_INVALID: "외부 모델의 응답을 처리하지 못했습니다.",
}


def safe_message(code: str) -> str:
    return MESSAGES.get(code, MESSAGES[ErrorCode.INTERNAL_ERROR])


class ErrorInfo(BaseModel):
    code: ErrorCode
    stage: ErrorStage
    message: str


class PipelineError(RuntimeError):
    def __init__(
        self, code: ErrorCode, stage: ErrorStage, detail: str | None = None
    ) -> None:
        super().__init__(detail or MESSAGES[code])
        self.code = code
        self.stage = stage


class ServiceError(ConnectionError):
    def __init__(self, detail: str, code: ErrorCode, stage: ErrorStage) -> None:
        super().__init__(detail)
        self.code = code
        self.stage = stage


class StateConflictError(RuntimeError):
    """저장소의 참조 관계나 실행 상태 충돌. API 요청 충돌과 실행 실패를 구분한다."""


def describe_error(
    error: BaseException, stage: ErrorStage = ErrorStage.PIPELINE
) -> ErrorInfo:
    stage = getattr(error, "pipeline_stage", stage)
    if isinstance(error, PipelineError):
        code = error.code
        if error.stage != ErrorStage.PIPELINE:
            stage = error.stage
    elif isinstance(error, ServiceError):
        code, stage = error.code, error.stage
    elif isinstance(error, sqlite3.Error):
        code, stage = ErrorCode.STORAGE_ERROR, ErrorStage.STORAGE
    elif isinstance(error, OSError) and stage == ErrorStage.STORAGE:
        code = ErrorCode.STORAGE_ERROR
    elif isinstance(error, StateConflictError):
        code, stage = ErrorCode.STATE_CONSISTENCY_ERROR, ErrorStage.STORAGE
    elif isinstance(error, FileNotFoundError) and stage == ErrorStage.STARTUP:
        code = ErrorCode.CONFIGURATION_ERROR
    elif isinstance(error, TimeoutError):
        code = ErrorCode.LOCAL_LLM_TIMEOUT
    elif isinstance(error, ConnectionError):
        code = ErrorCode.LOCAL_LLM_UNAVAILABLE
    else:
        code = ErrorCode.INTERNAL_ERROR
    return ErrorInfo(code=code, stage=stage, message=MESSAGES[code])


def model_failure(
    reason: str, stage: ErrorStage
) -> PipelineError | asyncio.CancelledError:
    if reason == "stopped":
        return asyncio.CancelledError()
    code = {
        "unavailable": ErrorCode.LOCAL_LLM_UNAVAILABLE,
        "timeout": ErrorCode.LOCAL_LLM_TIMEOUT,
        "length_failed": ErrorCode.MODEL_OUTPUT_LIMIT,
        "length": ErrorCode.MODEL_OUTPUT_LIMIT,
        "context_full": ErrorCode.INPUT_LIMIT_EXCEEDED,
        "compaction_failed": ErrorCode.COMPACTION_FAILED,
    }.get(reason, ErrorCode.MODEL_RESPONSE_INVALID)
    return PipelineError(code, stage, f"Model failed: {reason}")


def log_diagnostic(
    store: Any,
    run_id: int | None,
    info: ErrorInfo,
    *,
    recovered: bool,
    error: BaseException | None = None,
    query_id: int | None = None,
) -> None:
    """내부 진단 저장 실패가 원래 실패나 복구 결과를 덮어쓰지 않게 한다."""
    log = logging.getLogger(__name__)
    log.warning(
        "pipeline diagnostic run_id=%s code=%s stage=%s recovered=%s",
        run_id,
        info.code,
        info.stage,
        recovered,
        exc_info=(type(error), error, error.__traceback__)
        if error is not None
        else None,
    )
    if store is not None and run_id is not None:
        try:
            store.log_event(
                run_id,
                "diagnostic",
                {
                    **info.model_dump(mode="json"),
                    "recovered": recovered,
                    "query_id": query_id,
                },
            )
        except Exception:
            log.exception("Failed to persist diagnostic run_id=%s", run_id)

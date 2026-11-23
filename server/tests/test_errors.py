"""오류 원인 분류·원인 전달·복구와 공개 안내의 경계 검사."""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.data.chat_manager import SCHEMA_PATH, ChatManager
from app.data.memo_manager import MemoManager
from app.models.errors import (
    MESSAGES,
    ErrorCode,
    ErrorStage,
    PipelineError,
    describe_error,
    model_failure,
)
from app.models.schemas import RunConfig
from app.pipeline.leak_guard import LeakGuard
from app.pipeline.planner import Planner
from app.pipeline.request_writer import RequestWriter
from app.privacy.pd_detector import PDDetector
from app.runtime.context_manager import ContextManager


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("unavailable", ErrorCode.LOCAL_LLM_UNAVAILABLE),
        ("timeout", ErrorCode.LOCAL_LLM_TIMEOUT),
        ("parse_failed", ErrorCode.MODEL_RESPONSE_INVALID),
        ("length_failed", ErrorCode.MODEL_OUTPUT_LIMIT),
        ("context_full", ErrorCode.INPUT_LIMIT_EXCEEDED),
    ],
)
@pytest.mark.parametrize(
    "stage",
    [
        ErrorStage.DETECT,
        ErrorStage.NEEDS,
        ErrorStage.JUDGE,
        ErrorStage.WRITE,
        ErrorStage.GUARD,
    ],
)
def test_modules_keep_runner_failure_reason(
    reason: str, code: ErrorCode, stage: ErrorStage
) -> None:
    runner = Mock()
    runner.request.return_value = (None, False, None, None, reason)
    runner.tokenize.return_value = []
    runner.prompt_manager = None
    table = [{"pd_id": 1, "text": "secret", "category": 0, "allowed": False}]
    operations = {
        ErrorStage.DETECT: lambda: PDDetector(runner).detect(1, 1, 1, "question"),
        ErrorStage.NEEDS: lambda: Planner(runner).list_needs("question", 1, 1),
        ErrorStage.JUDGE: lambda: Planner(runner).judge(
            "question", "", table, 1, 1, ["need"]
        ),
        ErrorStage.WRITE: lambda: RequestWriter(runner).write(
            "question", "", table, ["need"], 1, 1
        ),
        ErrorStage.GUARD: lambda: LeakGuard(runner).detect(1, "request", table),
    }
    with pytest.raises(PipelineError) as caught:
        operations[stage]()
    assert (caught.value.code, caught.value.stage) == (code, stage)


@pytest.mark.parametrize(
    "error", [RuntimeError("/private/key=secret"), RuntimeError("another message")]
)
def test_unknown_exception_text_does_not_control_code_or_message(
    error: Exception,
) -> None:
    info = describe_error(error, ErrorStage.JUDGE)
    assert (info.code, info.stage) == (ErrorCode.INTERNAL_ERROR, ErrorStage.JUDGE)
    assert str(error) not in info.message


def test_all_codes_have_fixed_messages_and_stop_is_not_an_error() -> None:
    assert set(MESSAGES) == set(ErrorCode)
    assert isinstance(
        model_failure("stopped", ErrorStage.DETECT), asyncio.CancelledError
    )
    assert (
        describe_error(sqlite3.OperationalError("private path")).code
        == ErrorCode.STORAGE_ERROR
    )


@pytest.mark.parametrize("version", [0, 1])
def test_existing_db_upgrade_preserves_errors_and_is_repeatable(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "history.sqlite3"
    schema = (
        SCHEMA_PATH.read_text(encoding="utf-8")
        .split("-- Context persistence")[0]
        .replace(", errorStage TEXT", "")
        + f"\nPRAGMA user_version = {version};"
    )
    if version == 0:
        schema = schema.replace("category BETWEEN 0 AND 3", "category BETWEEN 0 AND 2")
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute("INSERT INTO chats VALUES (1,'saved','','')")
        db.execute("INSERT INTO messages VALUES (1,1,'user','question','')")
        db.execute(
            "INSERT INTO runs(runId,chatId,userMessageId,config,status,errorCode,notice,startedAt) VALUES (1,1,1,?, 'finished','INTERNAL_ERROR','private detail','')",
            (
                RunConfig(call_limit=3, regen_limit=3, budget_limit=1).model_dump_json(
                    by_alias=True
                ),
            ),
        )
    for _ in range(2):
        with ChatManager(path) as store:
            run = store.get_run(1)
            assert run.error_stage is None
            assert run.notice == MESSAGES[ErrorCode.INTERNAL_ERROR]
            assert store.get_chat(1).messages[0].content == "question"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_primary_error_and_cleanup_error_keep_first_code_stage_and_safe_message(
    tmp_path: Path,
) -> None:
    with ChatManager(tmp_path / "errors.sqlite3") as store:
        chat = store.create_chat()
        started = store.start_turn(
            chat,
            "question",
            None,
            RunConfig(call_limit=3, regen_limit=3, budget_limit=1),
        )
        run_id = started.run_id
        for code, stage in [
            (ErrorCode.LOCAL_LLM_TIMEOUT, ErrorStage.JUDGE),
            (ErrorCode.CLEANUP_ERROR, ErrorStage.CLEANUP),
        ]:
            store.log_event(
                run_id,
                "error",
                {"code": code, "stage": stage, "message": "/private/secret"},
            )
        terminal = store.finish_run(
            run_id, None, None, None, notice="unrelated limited notice"
        )
        run = store.get_run(run_id)
        assert (run.error_code, run.error_stage) == (
            ErrorCode.LOCAL_LLM_TIMEOUT,
            ErrorStage.JUDGE,
        )
        assert terminal.payload == {
            "code": ErrorCode.LOCAL_LLM_TIMEOUT,
            "stage": ErrorStage.JUDGE,
            "message": MESSAGES[ErrorCode.LOCAL_LLM_TIMEOUT],
        }
        assert len([e for e in store.get_events(run_id) if e.type == "run_error"]) == 2


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("parse_failed", ErrorCode.MODEL_RESPONSE_INVALID),
        ("length_failed", ErrorCode.MODEL_OUTPUT_LIMIT),
        ("timeout", ErrorCode.LOCAL_LLM_TIMEOUT),
    ],
)
def test_memo_compaction_preserves_model_failure(reason: str, code: ErrorCode) -> None:
    runner = Mock()
    runner.tokenize.side_effect = lambda text: text.split()
    runner.get_input_limit.return_value = 6000
    runner.request.return_value = (None, False, 1, 1, reason)
    runner.chat_manager = None
    prompts = Mock()
    manager = MemoManager(runner, prompts)
    manager.start(1, "question")
    manager.set_needs(["need"])
    before = manager.read_all()
    for _ in range(2):
        with pytest.raises(PipelineError) as caught:
            manager.add_answer(["need"], "word " * 1500)
        assert (caught.value.code, caught.value.stage) == (
            code,
            ErrorStage.MEMO_SUMMARY,
        )
        assert manager.read_all() == before
    assert runner.request.call_count == 1


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (("", False, 1, 1, "length_failed"), ErrorCode.MODEL_OUTPUT_LIMIT),
        (("word " * 120, True, 1, 1, "stop"), ErrorCode.COMPACTION_FAILED),
        (TimeoutError("private"), ErrorCode.LOCAL_LLM_TIMEOUT),
    ],
)
def test_context_compaction_failure_keeps_previous_content(
    response: tuple | Exception, code: ErrorCode
) -> None:
    runner = Mock()
    runner.tokenize.side_effect = lambda text: text.split()
    runner.get_input_limit.return_value = 6000
    if isinstance(response, Exception):
        runner.manual_request.side_effect = response
    else:
        runner.manual_request.return_value = response
    manager = ContextManager(runner, 1, compaction_limit=100, compaction_target=10)
    context_id = manager.new_id()
    manager.append(context_id, "word " * 60)
    before = manager.get_context(context_id)
    with pytest.raises(PipelineError) as caught:
        manager.append(context_id, "word " * 60)
    assert (caught.value.code, caught.value.stage) == (code, ErrorStage.COMPACTION)
    assert manager.get_context(context_id) == before


def test_failed_schema_upgrade_can_be_retried_without_data_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.sqlite3"
    schema = (
        SCHEMA_PATH.read_text(encoding="utf-8")
        .split("-- Context persistence")[0]
        .replace(", errorStage TEXT", "")
        + "\nPRAGMA user_version = 1;"
    )
    with sqlite3.connect(path) as db:
        db.executescript(schema)
        db.execute("INSERT INTO chats VALUES (1,'saved','','')")
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, *args: object, **kwargs: object) -> sqlite3.Cursor:
            if sql.startswith("ALTER TABLE runs"):
                raise sqlite3.OperationalError("injected migration failure")
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            "app.data.chat_manager.sqlite3.connect",
            lambda *args, **kwargs: connect(*args, **kwargs, factory=FailingConnection),
        )
        with pytest.raises(sqlite3.OperationalError):
            ChatManager(path)
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert "errorStage" not in {
            row[1] for row in db.execute("PRAGMA table_info(runs)")
        }
    with ChatManager(path) as store:
        assert store.list_chats()[0].title == "saved"


def test_invalid_model_configuration_is_not_an_internal_error() -> None:
    from app.runtime.frontier_client import FrontierClient, FrontierError
    from app.runtime.local_llm_runner import LocalLLMRunner

    with pytest.raises(FrontierError) as external:
        FrontierClient(model="unregistered-model", env_file=None)
    assert describe_error(external.value).code == ErrorCode.CONFIGURATION_ERROR
    with pytest.raises(PipelineError) as local:
        LocalLLMRunner(timeout=0)
    assert describe_error(local.value).code == ErrorCode.CONFIGURATION_ERROR

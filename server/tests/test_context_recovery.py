"""#59: DB 보존·복원 경계·압축 채택 및 실패 원자성."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.data.chat_manager import SCHEMA_PATH, ChatManager, StorageError
from app.models.enums import EndReason
from app.models.errors import ErrorCode, PipelineError, StateConflictError
from app.models.schemas import RunConfig
from app.runtime.context_manager import ContextManager

CONFIG = RunConfig(call_limit=3, regen_limit=3, budget_limit=1)


class SummaryRunner:
    def __init__(self, store: ChatManager) -> None:
        self.chat_manager = store
        self.inputs: list[str] = []
        self.fail_at = 0
        self.summary = "kept facts"
        self.input_limit = 10000

    def tokenize(self, text: str) -> list[int]:
        return list(range(len(text)))

    def get_input_limit(self, system_prompt: str, **kwargs: object) -> int:
        return self.input_limit - len(system_prompt)

    def manual_request(self, text: str, system_prompt: str, **kwargs: object) -> tuple:
        self.inputs.append(text)
        assert kwargs["run_id"] is not None
        assert kwargs["interruptible"] is False
        assert kwargs["log_call"] is False
        if len(self.inputs) == self.fail_at:
            raise ConnectionError("summary unavailable")
        return self.summary, True, 1, 1, "stop"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ChatManager]:
    with ChatManager(tmp_path / "chat.sqlite3") as chat:
        yield chat


def history(store: ChatManager) -> tuple[int, int, int]:
    chat = store.create_chat()
    store.save_message(chat, "user", "AI glasses at exams")
    store.save_message(chat, "assistant", "Cheating news was discussed")
    turn = store.start_turn(chat, "Actual links please", None, CONFIG)
    return chat, turn.run_id, store.context_boundary(chat, turn.user_message_id)


def test_restore_incremental_and_cold_input_are_identical(store: ChatManager) -> None:
    chat, run, boundary = history(store)
    runner = SummaryRunner(store)
    warm = ContextManager(runner, chat)
    first = warm.prepare_input("current", "system", boundary, run)
    assert "AI glasses" in first.text and "Cheating news" in first.text
    assert "Actual links please" not in first.text
    store.log_event(run, "llm_call", {"output": "FAILED ATTEMPT"})
    store.save_message(chat, "assistant", "Final links")
    following = store.start_turn(chat, "and that?", None, CONFIG)
    boundary = store.context_boundary(chat, following.user_message_id)
    warm_input = warm.prepare_input("and that?", "system", boundary, following.run_id)
    cold_input = ContextManager(runner, chat).prepare_input(
        "and that?", "system", boundary, following.run_id
    )
    assert warm_input == cold_input
    assert warm_input.text.count("Actual links please") == 1
    assert warm_input.text.count("Final links") == 1
    assert "FAILED ATTEMPT" not in warm_input.text
    assert warm_input.text.count("and that?") == 1


def test_compaction_covers_old_prefix_and_replays_tail(store: ChatManager) -> None:
    chat, run, boundary = history(store)
    runner = SummaryRunner(store)
    manager = ContextManager(runner, chat, compaction_limit=65, compaction_target=10)
    result = manager.prepare_input("current", "sys", boundary, run)
    snapshot = store.load_context(chat, boundary)
    assert snapshot.compaction.covered_through_seq == 1
    assert [r.context_seq for r in snapshot.records] == [2]
    assert "AI glasses" in runner.inputs[0]
    assert "Cheating news" not in runner.inputs[0]
    assert "kept facts" in result.text and "Cheating news" in result.text
    restored = ContextManager(runner, chat, compaction_limit=65, compaction_target=10)
    assert restored.prepare_input("current", "sys", boundary, run) == result
    assert len(runner.inputs) == 1
    # 반복 압축도 기존 요약과 증분을 사용한다.
    runner.input_limit = len(result.text) - 1
    again = manager.prepare_input("current", "", boundary, run)
    assert "kept facts" in again.text
    assert store.load_context(chat, boundary).compaction.covered_through_seq == 2
    runner.input_limit = 10000
    assert manager.prepare_input("current", "", boundary, run) == ContextManager(
        runner, chat, compaction_limit=65, compaction_target=10
    ).prepare_input("current", "", boundary, run)


@pytest.mark.parametrize("failure", ["model", "no_shrink", "storage"])
def test_failed_summary_keeps_checkpoint_and_can_retry(
    store: ChatManager, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    chat, run, boundary = history(store)
    runner = SummaryRunner(store)
    manager = ContextManager(runner, chat, compaction_limit=65, compaction_target=10)
    with monkeypatch.context() as patch:
        if failure == "model":
            runner.fail_at = 1
        elif failure == "no_shrink":
            runner.summary = "x" * 70
        else:
            patch.setattr(
                store,
                "save_compaction",
                Mock(side_effect=sqlite3.OperationalError("disk full")),
            )
        with pytest.raises((PipelineError, sqlite3.OperationalError)):
            manager.prepare_input("current", "sys", boundary, run)
    assert store.load_context(chat, boundary).compaction is None
    runner.fail_at = 0
    runner.summary = "kept facts"
    assert "kept facts" in manager.prepare_input("current", "sys", boundary, run).text


def test_stale_compaction_invalidates_cache_and_reloads(
    store: ChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, run, boundary = history(store)
    runner = SummaryRunner(store)
    manager = ContextManager(runner, chat, compaction_limit=65, compaction_target=10)
    original = store.save_compaction

    def race(*args: object, **kwargs: object) -> object:
        original(chat, run, 1, "winner", None)
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store, "save_compaction", race)
        with pytest.raises(StateConflictError):
            manager.prepare_input("current", "sys", boundary, run)
    result = manager.prepare_input("current", "sys", boundary, run)
    assert "winner" in result.text
    assert "kept facts" not in result.text


def test_compaction_event_and_checkpoint_rollback_together(
    store: ChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, run, boundary = history(store)
    with monkeypatch.context() as patch:
        patch.setattr(
            store,
            "_append_event",
            Mock(side_effect=sqlite3.OperationalError("event write failed")),
        )
        with pytest.raises(sqlite3.OperationalError):
            store.save_compaction(chat, run, boundary, "summary", None)
    assert store.load_context(chat, boundary).compaction is None
    assert store.get_events(run) == []
    saved = store.save_compaction(chat, run, boundary, "summary", None)
    assert store.get_events(run)[0].type == "compaction"
    assert saved.covered_through_seq == boundary
    with pytest.raises(StateConflictError):
        store.save_compaction(chat, run, 1, "older", saved.compaction_id)
    store.finish_run(run, EndReason.STOPPED, None, None)
    with pytest.raises(StorageError):
        store.save_compaction(chat, run, boundary, "late", saved.compaction_id)
    store.delete_chat(chat)
    assert store._db.execute("SELECT count(*) FROM context_entries").fetchone()[0] == 0
    assert (
        store._db.execute("SELECT count(*) FROM context_compactions").fetchone()[0] == 0
    )


def test_message_and_sequence_are_atomic_and_source_is_unique(
    store: ChatManager,
) -> None:
    chat = store.create_chat()
    store._db.execute(
        "CREATE TRIGGER fail_entry BEFORE INSERT ON context_entries BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.start_turn(chat, "must rollback", None, CONFIG)
    assert store.get_chat(chat).messages == []
    assert store.get_chat(chat).title == ""
    assert store.get_chat(chat).runs == []
    store._db.execute("DROP TRIGGER fail_entry")
    turn = store.start_turn(chat, "kept", None, CONFIG)
    assert store.context_boundary(chat, turn.user_message_id) == 0
    with pytest.raises(sqlite3.IntegrityError):
        with store._transaction() as db:
            db.execute(
                "INSERT INTO context_entries VALUES (?, 'conversation', 2, ?)",
                (chat, turn.user_message_id),
            )


@pytest.mark.parametrize("fail", [False, True])
def test_large_message_is_chunked_without_partial_checkpoint(
    store: ChatManager, fail: bool
) -> None:
    chat = store.create_chat()
    original = "a" * 400 + "END"
    store.save_message(chat, "assistant", original)
    turn = store.start_turn(chat, "follow up", None, CONFIG)
    runner = SummaryRunner(store)
    runner.fail_at = 3 if fail else 0
    manager = ContextManager(runner, chat, compaction_limit=150, compaction_target=10)
    if fail:
        with pytest.raises(PipelineError):
            manager.prepare_input("current", "sys", 1, turn.run_id)
        assert store.load_context(chat, 1).compaction is None
        runner.fail_at = 0
    result = manager.prepare_input("current", "sys", 1, turn.run_id)
    assert "kept facts" in result.text
    assert len(runner.inputs) > 1
    assert all(len(text) < 150 for text in runner.inputs)
    assert any("END" in text for text in runner.inputs)
    assert store.get_chat(chat).messages[0].content == original
    assert store.load_context(chat, 1).compaction.covered_through_seq == 1
    assert len(store.get_events(turn.run_id)) == 1


def test_exact_budget_does_not_save_unusable_summary(store: ChatManager) -> None:
    chat, run, boundary = history(store)
    runner = SummaryRunner(store)
    manager = ContextManager(runner, chat)
    baseline = manager.prepare_input("current", "sys", boundary, run)
    runner.input_limit = len(baseline.text) + 3
    assert manager.prepare_input("current", "sys", boundary, run) == baseline
    assert runner.inputs == []
    runner.input_limit = 2
    with pytest.raises(PipelineError) as error:
        manager.prepare_input("current", "sys", boundary, run)
    assert error.value.code == ErrorCode.INPUT_LIMIT_EXCEEDED
    assert store.load_context(chat, boundary).compaction is None


def test_context_migration_rolls_back_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.sqlite3"
    schema = SCHEMA_PATH.read_text(encoding="utf-8").split("-- Context persistence")[0]
    with sqlite3.connect(path) as db:
        db.executescript(schema + "PRAGMA user_version=2;")
        db.execute("INSERT INTO chats VALUES (1, 'kept', '', '')")
        db.execute("INSERT INTO messages VALUES (4,1,'user','original','')")
        db.execute(
            "INSERT INTO messages VALUES (6,1,'context_compacted','legacy summary','')"
        )
        db.execute("INSERT INTO messages VALUES (8,1,'assistant','actual answer','')")
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql: str, *args: object, **kwargs: object) -> sqlite3.Cursor:
            if "INSERT INTO context_entries" in sql:
                raise sqlite3.OperationalError("interrupted backfill")
            return super().execute(sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(
            "app.data.chat_manager.sqlite3.connect",
            lambda *args, **kwargs: connect(*args, **kwargs, factory=FailingConnection),
        )
        with pytest.raises(sqlite3.OperationalError):
            ChatManager(path)
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='context_entries'"
            ).fetchone()
            is None
        )
    for _ in range(2):
        with ChatManager(path) as store:
            snapshot = store.load_context(1, 2)
            assert snapshot.compaction is None
            assert [
                (r.context_seq, r.message_id, r.content) for r in snapshot.records
            ] == [(1, 4, "original"), (2, 8, "actual answer")]
            assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_summary_finishing_after_run_end_cannot_be_committed(
    store: ChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, run, boundary = history(store)
    runner = SummaryRunner(store)
    manager = ContextManager(runner, chat, compaction_limit=65, compaction_target=10)
    original = runner.manual_request

    def finish_during_summary(*args: object, **kwargs: object) -> tuple:
        result = original(*args, **kwargs)
        store.finish_run(run, EndReason.STOPPED, None, None)
        return result

    monkeypatch.setattr(runner, "manual_request", finish_during_summary)
    with pytest.raises(StorageError):
        manager.prepare_input("current", "sys", boundary, run)
    assert store.load_context(chat, boundary).compaction is None
    assert [event.type for event in store.get_events(run)] == ["done"]


def test_warm_cache_reads_only_new_records(
    store: ChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, run, boundary = history(store)
    runner = SummaryRunner(store)
    manager = ContextManager(runner, chat)
    manager.prepare_input("current", "sys", boundary, run)
    read = Mock(wraps=store._context_records)
    monkeypatch.setattr(store, "_context_records", read)
    manager.prepare_input("another stage", "sys", boundary, run)
    assert read.call_args.args == (chat, "conversation", boundary, boundary)
    store.save_message(chat, "assistant", "new answer")
    next_turn = store.start_turn(chat, "next", None, CONFIG)
    next_boundary = store.context_boundary(chat, next_turn.user_message_id)
    manager.prepare_input("next", "sys", next_boundary, next_turn.run_id)
    assert read.call_args.args == (chat, "conversation", boundary, next_boundary)

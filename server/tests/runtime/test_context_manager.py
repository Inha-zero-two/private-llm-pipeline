"""컨텍스트 분리, 실제 토큰 수 사용, 압축과 실패 시 원문 보존을 검증한다."""

from typing import Any

import pytest

from app.runtime.context_manager import ContextManager


class FakeRunner:
    def __init__(self) -> None:
        self.summary = "short"
        self.error: Exception | None = None
        self.success = True
        self.calls: list[tuple[str, str, int]] = []

    def tokenize(self, text: str) -> list[int]:
        # 단어 1개를 토큰 1개로 보는 대역. 문자 수를 대신 쓰는 구현을 검출한다.
        return list(range(len(text.split())))

    def manual_request(
        self,
        text: str,
        system: str,
        *,
        run_id: str,
        max_tokens: int,
        log_call: bool = True,
        interruptible: bool = True,
    ) -> tuple[str, bool, int, int, str]:
        assert run_id == "run" and interruptible is False
        assert log_call is False  # 압축 원문이 일반 호출 로그로 나가지 않아야 한다.
        self.calls.append((text, system, max_tokens))
        if self.error:
            raise self.error
        return self.summary, self.success, 10, 1, "stop" if self.success else "stopped"


@pytest.fixture
def setup_context() -> tuple[ContextManager, Any]:
    runner = FakeRunner()
    manager = ContextManager(runner, "run", compaction_limit=10, compaction_target=3)
    return manager, runner


def test_ids_are_stable_and_isolated(setup_context: tuple[ContextManager, Any]) -> None:
    manager, _ = setup_context
    first = manager.get_id("chat1", "ANSWER")
    assert first == manager.get_id("chat1", "ANSWER")
    assert first != manager.get_id("chat2", "ANSWER")
    assert first != manager.get_id("chat1", "GUARD")
    assert manager.get_context(first) == ""
    assert manager.new_id() != first


def test_token_length_uses_model_tokenizer(
    setup_context: tuple[ContextManager, Any],
) -> None:
    manager, _ = setup_context
    context_id = manager.new_id()
    manager.append(context_id, "oneverylongword second")
    assert manager.get_context_length(context_id) == 2


def test_append_compacts_before_reaching_limit(
    setup_context: tuple[ContextManager, Any],
) -> None:
    manager, runner = setup_context
    context_id = manager.new_id()
    original = "one two three four five six seven eight"
    manager.append(context_id, original)
    manager.append(context_id, " nine ten")
    assert runner.calls[0][0] == original
    assert runner.calls[0][2] == 7000
    assert manager.get_context(context_id) == "short nine ten"


@pytest.mark.parametrize("failure", ["exception", "stopped", "empty"])
def test_compaction_failure_preserves_original(
    setup_context: tuple[ContextManager, Any], failure: str
) -> None:
    manager, runner = setup_context
    context_id = manager.new_id()
    original = "one two three four five six seven eight"
    manager.append(context_id, original)
    error = OSError("synthetic proxy failure")
    if failure == "exception":
        runner.error = error
    elif failure == "stopped":
        runner.success = False
    else:
        runner.summary = ""
    with pytest.raises((OSError, RuntimeError)) as raised:
        manager.append(context_id, " nine ten")
    if failure == "exception":
        assert raised.value is error
    assert manager.get_context(context_id) == original


def test_explicit_compaction_and_empty_context(
    setup_context: tuple[ContextManager, Any],
) -> None:
    manager, runner = setup_context
    context_id = manager.new_id()
    manager.compact_context(context_id)
    assert not runner.calls
    manager.append(context_id, "one two three four")
    manager.compact_context(context_id)
    assert manager.get_context(context_id) == "short"


def test_oversized_append_is_atomic(setup_context: tuple[ContextManager, Any]) -> None:
    manager, _ = setup_context
    context_id = manager.new_id()
    addition = " ".join(["large"] * 11)
    with pytest.raises(ValueError):
        manager.append(context_id, addition)
    assert manager.get_context(context_id) == ""
    manager.append(context_id, "original")
    with pytest.raises(ValueError):
        manager.append(context_id, " " + addition)
    assert manager.get_context(context_id) == "original"


def test_unknown_id_and_invalid_thresholds(
    setup_context: tuple[ContextManager, Any],
) -> None:
    manager, runner = setup_context
    with pytest.raises(KeyError):
        manager.get_context(999)
    with pytest.raises(ValueError):
        ContextManager(runner, "run", compaction_limit=3, compaction_target=3)


def test_compaction_log_receives_context_id_and_summary_after_commit(
    setup_context: tuple[ContextManager, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = setup_context
    logged: list[tuple[int, str]] = []

    def log(context_id: int, result: str) -> None:
        assert manager.get_context(context_id) == "short nine ten"
        logged.append((context_id, result))

    monkeypatch.setattr(manager, "_log_compaction", log)
    context_id = manager.new_id()
    manager.append(context_id, "one two three four five six seven eight")
    manager.append(context_id, " nine ten")
    assert logged == [(context_id, "short")]


def test_record_only_once_for_nonempty_explicit_compaction(
    setup_context: tuple[ContextManager, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = setup_context
    logged: list[tuple[int, str]] = []
    context_id = manager.new_id()

    def log(saved_id: int, result: str) -> None:
        assert manager.get_context(saved_id) == result
        logged.append((saved_id, result))

    monkeypatch.setattr(manager, "_log_compaction", log)
    manager.compact_context(context_id)
    manager.append(context_id, "one two")
    assert logged == []  # 빈 컨텍스트와 일반 append는 저장 연결점을 부르지 않는다.
    manager.compact_context(context_id)
    assert logged == [(context_id, "short")]


@pytest.mark.parametrize(
    "failure", ["exception", "stopped", "empty", "still_too_large"]
)
def test_failed_compaction_does_not_record(
    setup_context: tuple[ContextManager, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    manager, runner = setup_context
    logged = []
    monkeypatch.setattr(manager, "_log_compaction", lambda *args: logged.append(args))
    context_id = manager.new_id()
    original = "one two three four five six seven eight"
    manager.append(context_id, original)
    addition = " nine ten"
    if failure == "exception":
        runner.error = OSError("synthetic model failure")
    elif failure == "stopped":
        runner.success = False
    elif failure == "empty":
        runner.summary = ""
    else:
        addition = " a b c d e f g h i"
    with pytest.raises((OSError, RuntimeError, ValueError)):
        manager.append(context_id, addition)
    assert manager.get_context(context_id) == original
    assert logged == []


def test_default_28k_boundary_and_5k_target() -> None:
    runner = FakeRunner()
    manager = ContextManager(runner, "run")
    context_id = manager.new_id()
    manager.append(context_id, " ".join(["word"] * 27999))
    assert not runner.calls
    manager.append(context_id, " final")
    assert len(runner.calls) == 1
    assert "at most 5000 tokens" in runner.calls[0][1]
    assert runner.calls[0][2] == 7000
    assert manager.get_context(context_id) == "short final"


@pytest.mark.parametrize("automatic", [False, True])
def test_summary_above_target_is_used_without_truncation(
    setup_context: tuple[ContextManager, Any], automatic: bool
) -> None:
    manager, runner = setup_context
    runner.summary = "one two three four"  # 목표 3토큰을 넘지만 유효한 생성 결과다.
    context_id = manager.new_id()
    manager.append(context_id, "one two three four five six seven eight")
    if automatic:
        manager.append(context_id, " nine ten")
        assert manager.get_context(context_id) == "one two three four nine ten"
    else:
        manager.compact_context(context_id)
        assert manager.get_context(context_id) == runner.summary

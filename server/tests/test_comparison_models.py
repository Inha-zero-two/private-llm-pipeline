"""비교 기능의 부품 검사: 의존 방향, 저장, 앞 대화, 로컬·프론티어 호출.

실제 모델을 부르지 않는다. 로컬은 어댑터 대역, 프론티어는 httpx.MockTransport를 쓴다.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.data.chat_manager import SCHEMA_PATH, ChatManager
from comparison import frontier as frontier_module
from comparison import local as local_module
from comparison.frontier import FrontierStandalone, build_messages, fit_messages
from comparison.local import LocalStandalone
from comparison.local_context import INPUT_LIMIT, build_input, fit_input
from comparison.models import (
    Cancellation,
    Cancelled,
    ErrorCode,
    ModelFailure,
    Result,
    Status,
    StreamEvent,
    Turn,
    to_item,
)
from comparison.store import ComparisonStore

SERVER = Path(__file__).resolve().parents[1]
Q1, A1 = "이직하려면 뭘 준비해요?", "답 1"
Q2 = "그럼 포트폴리오는 어떻게 만들어요?"


# 1. 의존 방향


def imports_of(path: Path) -> set[str]:
    """파일이 불러오는 최상위 패키지. 함수 안의 import도 본다. 상대 import는 제외."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_comparison_depends_only_on_allowed_packages() -> None:
    stdlib = {
        *("__future__", "ast", "asyncio", "collections", "contextlib", "dataclasses"),
        *("datetime", "enum", "json", "logging", "pathlib", "sqlite3", "threading"),
        *("time", "typing"),
    }
    allowed = stdlib | {"fastapi", "httpx", "pydantic", "src"}
    files = list((SERVER / "comparison").glob("*.py"))
    assert len(files) == 8
    for path in files:
        assert imports_of(path) <= allowed, path.name


def test_only_api_app_knows_comparison() -> None:
    users = {
        path.relative_to(SERVER / "app").as_posix()
        for path in (SERVER / "app").rglob("*.py")
        if "comparison" in imports_of(path)
    }
    assert users == {"api/app.py"}


def test_comparison_uses_only_its_own_tables() -> None:
    sql = (SERVER / "comparison" / "store.py").read_text(encoding="utf-8")
    for table in ("messages", "runs ", "chats ", "context_entries", "pd_items"):
        assert f"FROM {table}" not in sql and f"INTO {table}" not in sql


# 15. DB 갱신


def old_schema(version: int) -> str:
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    if version == 3:
        return text.split("-- Comparison")[0]
    text = text.split("-- Context persistence")[0]
    if version < 2:
        text = text.replace(", errorStage TEXT", "")
    if version < 1:
        text = text.replace("category BETWEEN 0 AND 3", "category BETWEEN 0 AND 2")
    return text + f"\nPRAGMA user_version = {version};"


@pytest.mark.parametrize("version", [0, 1, 2, 3])
def test_old_db_becomes_version_4_without_losing_data(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(old_schema(version))
        db.execute("INSERT INTO chats VALUES (7, '보존', '', '')")
        db.execute("INSERT INTO messages VALUES (3, 7, 'user', '질문', '')")
        db.execute("INSERT INTO messages VALUES (4, 7, 'assistant', '답', '')")
        db.commit()
    for _ in range(2):  # 반복 기동해도 같다
        with ChatManager(path) as store:
            contents = [m.content for m in store.get_chat(7).messages]
            assert contents == ["질문", "답"]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (4,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT COUNT(*) FROM comparisons").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM comparison_chats").fetchone() == (0,)


def test_new_and_migrated_db_have_the_same_schema(tmp_path: Path) -> None:
    def schema(path: Path) -> list[tuple[str, ...]]:
        with closing(sqlite3.connect(path)) as db:
            return db.execute(
                "SELECT type, name, tbl_name FROM sqlite_master ORDER BY 1, 2"
            ).fetchall()

    new, old = tmp_path / "new.sqlite3", tmp_path / "old.sqlite3"
    ChatManager(new).close()
    with closing(sqlite3.connect(old)) as db:
        db.executescript(old_schema(3))
    ChatManager(old).close()
    assert schema(new) == schema(old)


@pytest.mark.parametrize("version", [2, 3])
def test_failed_comparison_migration_rolls_back_and_retries(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(old_schema(version))
        # 같은 이름의 인덱스가 있으면 갱신의 마지막 문장에서 실패한다
        db.execute("CREATE INDEX comparisons_history ON chats(title)")
        db.commit()
    with pytest.raises(sqlite3.OperationalError):
        ChatManager(path)
    with closing(sqlite3.connect(path)) as db:
        # 앞 단계(2→3)는 끝났고, 비교 구역만 되돌린다
        assert db.execute("PRAGMA user_version").fetchone() == (3,)
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'comparison%'"
            " AND type = 'table'"
        ).fetchall()
        assert tables == []
        db.execute("DROP INDEX comparisons_history")
        db.commit()
    ChatManager(path).close()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (4,)


# 저장


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[tuple[ChatManager, ComparisonStore]]:
    chats = ChatManager(tmp_path / "chat.sqlite3")
    store = ComparisonStore(tmp_path / "chat.sqlite3")
    yield chats, store
    store.close()
    chats.close()


def add_run(chats: ChatManager, chat_id: int) -> int:
    """comparisons가 참조할 실행을 만든다."""
    with chats._transaction() as db:
        message = db.execute(
            "INSERT INTO messages(chatId, role, content, createdAt)"
            " VALUES (?, 'user', 'q', '')",
            (chat_id,),
        ).lastrowid
        return int(
            db.execute(
                "INSERT INTO runs(chatId, userMessageId, config, status, startedAt)"
                " VALUES (?, ?, '{}', 'finished', '')",
                (chat_id, message),
            ).lastrowid
        )


def ok(answer: str, **values: Any) -> Result:
    return Result(status=Status.OK, answer=answer, **values)


def test_history_uses_only_ok_answers_of_the_same_model(
    stores: tuple[ChatManager, ComparisonStore],
) -> None:
    chats, store = stores
    chat = chats.create_chat()
    runs = [add_run(chats, chat) for _ in range(6)]
    results = [
        ok("로컬 1"),
        Result(status=Status.TRUNCATED, answer="잘림"),
        Result(status=Status.FAILED, answer="실패", error_code=ErrorCode.TIMEOUT),
        Result(status=Status.STOPPED, answer="중지"),
        ok("로컬 5"),
    ]
    for index, (run, result) in enumerate(zip(runs, results, strict=False)):
        store.begin(chat, run, f"질문 {index}")
        store.finish(run, "local", result)
        store.finish(run, "frontier", ok(f"프론티어 {index}"))
    store.begin(chat, runs[5], "지금 질문")
    assert store.history(chat, "local", runs[5]) == [
        Turn(question="질문 0", answer="로컬 1"),
        Turn(question="질문 4", answer="로컬 5"),
    ]
    assert len(store.history(chat, "frontier", runs[5])) == 5
    assert store.history(chat, "frontier", runs[0]) == []
    # 다른 채팅의 답은 섞이지 않는다
    assert store.history(chats.create_chat(), "local", runs[5]) == []


def test_used_pairs_is_previous_ok_minus_dropped(
    stores: tuple[ChatManager, ComparisonStore],
) -> None:
    chats, store = stores
    chat = chats.create_chat()
    for _ in range(3):
        run = add_run(chats, chat)
        store.begin(chat, run, "q")
        store.finish(run, "local", ok("a"))
    run = add_run(chats, chat)
    store.begin(chat, run, "q")
    store.finish(run, "local", ok("a", dropped_pairs=1))
    store.finish(
        run,
        "frontier",
        Result(status=Status.FAILED, error_code=ErrorCode.KEY_MISSING),
    )
    local, frontier = store.get(run, "local"), store.get(run, "frontier")
    assert local is not None and frontier is not None
    assert to_item(local).used_pairs == 2
    assert to_item(frontier).used_pairs is None
    assert to_item(frontier).message == "모델에 접속할 키가 없습니다."


def test_restart_reuses_the_row_and_counts_attempts(
    stores: tuple[ChatManager, ComparisonStore],
) -> None:
    chats, store = stores
    chat = chats.create_chat()
    run = add_run(chats, chat)
    store.begin(chat, run, "q")
    failed = Result(
        status=Status.FAILED,
        answer="일부",
        error_code=ErrorCode.UNAVAILABLE,
        elapsed_ms=1,
        cost=0.1,
    )
    store.finish(run, "frontier", failed)
    store.restart(run, "frontier")
    record = store.get(run, "frontier")
    assert record is not None
    assert (record.status, record.attempts) == (Status.RUNNING, 2)
    assert record.answer is record.error_code is record.elapsed_ms is None
    assert record.cost is record.ended_at is None
    store.finish(run, "frontier", ok("성공"))
    store.restart(run, "frontier")  # 성공한 줄은 다시 실행하지 않는다
    record = store.get(run, "frontier")
    assert record is not None and record.status is Status.OK
    assert len(store.list_run(run)) == 2


def test_finish_only_changes_a_running_row_and_ignores_deleted_rows(
    stores: tuple[ChatManager, ComparisonStore],
) -> None:
    chats, store = stores
    chat = chats.create_chat()
    store.register_chat(chat)
    run = add_run(chats, chat)
    store.begin(chat, run, "q")
    store.finish(run, "local", ok("처음"))
    store.finish(run, "local", ok("늦게 온 결과"))
    record = store.get(run, "local")
    assert record is not None and record.answer == "처음"

    chats.delete_chat(chat)
    assert store.list_chat(chat) == []
    assert not store.is_registered(chat)
    store.finish(run, "frontier", ok("늦게 온 결과"))
    assert store.list_run(run) == []


def test_recover_interrupted_marks_running_rows_failed(
    stores: tuple[ChatManager, ComparisonStore],
) -> None:
    chats, store = stores
    chat = chats.create_chat()
    run = add_run(chats, chat)
    store.begin(chat, run, "q")
    store.finish(run, "local", ok("답"))
    assert store.recover_interrupted() == 1
    assert store.recover_interrupted() == 0
    frontier, local = store.list_run(run)
    assert (frontier.status, frontier.error_code) == (
        Status.FAILED,
        ErrorCode.INTERRUPTED,
    )
    assert frontier.ended_at is not None
    assert local.status is Status.OK


# 2 · 5. 앞 대화와 길이


def test_local_input_shape() -> None:
    assert build_input([], Q1) == Q1
    assert build_input([Turn(question=Q1, answer=A1)], Q2) == (
        "[이전 대화: 현재 입력을 이해하기 위한 참고 자료]\n"
        f"\n[user]\n{Q1}\n\n[assistant]\n{A1}\n"
        f"\n\n[현재 입력]\n{Q2}"
    )


def test_frontier_messages_shape() -> None:
    assert build_messages([], Q1) == [{"role": "user", "content": Q1}]
    assert build_messages([Turn(question=Q1, answer=A1)], Q2) == [
        {"role": "user", "content": Q1},
        {"role": "assistant", "content": A1},
        {"role": "user", "content": Q2},
    ]


@pytest.mark.parametrize("size", [1, 2000, 5000, 9000, 13000, 26000])
@pytest.mark.parametrize("count", [0, 1, 2, 5, 9])
def test_local_fit_drops_the_same_pairs_as_one_by_one(size: int, count: int) -> None:
    turns = [Turn(question=f"{i}", answer="가" * (size + i)) for i in range(count)]
    expected = next(
        n
        for n in range(count + 1)
        if len(build_input(turns[n:], "질문")) <= INPUT_LIMIT
    )
    assert fit_input(turns, "질문", len) == (
        build_input(turns[expected:], "질문"),
        expected,
    )


def test_local_question_over_the_limit_fails() -> None:
    turns = [Turn(question="q", answer="a")] * 3
    assert fit_input([], "가" * INPUT_LIMIT, len)[1] == 0
    for history in ([], turns):
        with pytest.raises(ModelFailure) as failure:
            fit_input(history, "가" * (INPUT_LIMIT + 1), len)
        assert failure.value.code is ErrorCode.INPUT_TOO_LONG
        assert failure.value.dropped_pairs == len(history)


def test_frontier_fit_drops_oldest_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frontier_module, "INPUT_LIMIT_CHARS", 10)
    turns = [Turn(question=str(i) * 2, answer=str(i) * 2) for i in range(3)]
    messages, dropped = fit_messages(turns, "qq")
    assert dropped == 1
    assert [m["content"] for m in messages] == ["11", "11", "22", "22", "qq"]
    assert fit_messages(turns, "q" * 10) == (
        [{"role": "user", "content": "q" * 10}],
        3,
    )
    with pytest.raises(ModelFailure) as failure:
        fit_messages(turns, "q" * 11)
    assert failure.value.code is ErrorCode.INPUT_TOO_LONG
    assert failure.value.dropped_pairs == 3


# 로컬 호출


class FakeLlama:
    """어댑터 대역. 실제 어댑터처럼 tokenize는 bytes만 받는다."""

    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks
        self.requests: list[dict[str, Any]] = []
        self.closed = 0

    def tokenize(
        self, text: bytes, add_bos: bool = True, special: bool = False
    ) -> list[int]:
        assert isinstance(text, bytes) and not add_bos and special
        return [0] * len(text.decode("utf-8"))

    def create_chat_completion(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.requests.append(kwargs)
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self) -> None:
        self.closed += 1


def delta(text: str, reason: str | None = None) -> dict[str, Any]:
    return {
        "model": "gemma-test",
        "choices": [{"delta": {"content": text}, "finish_reason": reason}],
    }


def collect(
    model: LocalStandalone | FrontierStandalone,
    turns: list[Turn] | None = None,
    question: str = Q1,
) -> tuple[list[StreamEvent], BaseException | None]:
    events: list[StreamEvent] = []
    try:
        for event in model.stream(turns or [], question, Cancellation()):
            events.append(event)
    except Exception as error:
        return events, error
    return events, None


def text_of(events: list[StreamEvent]) -> str:
    return "".join(e.text for e in events)


def test_local_sends_one_user_message_without_system() -> None:
    usage = {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    llama = FakeLlama([delta("안"), delta("녕", "stop"), usage])
    events, error = collect(
        LocalStandalone(lambda: llama), [Turn(question=Q1, answer=A1)], Q2
    )
    assert error is None
    assert text_of(events) == "안녕"
    assert events[0].dropped_pairs == 0
    assert events[-1].input_tokens == 3 and events[-1].output_tokens == 2
    assert events[1].model_name == "gemma-test"
    (request,) = llama.requests
    assert request == {
        "messages": [
            {
                "role": "user",
                "content": build_input([Turn(question=Q1, answer=A1)], Q2),
            }
        ],
        "stream": True,
        "max_tokens": 5000,
    }
    assert llama.closed == 1


def test_local_adapter_is_remote_and_created_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeLlama:
        made.append(kwargs)
        return FakeLlama([delta("답", "stop")])

    monkeypatch.setattr("src.localLLM_API_proxy.llama_wrapper.Llama", factory)
    model = LocalStandalone()
    assert made == []  # 만들 때는 접속하지 않는다
    collect(model)
    collect(model)
    assert made == [{"backend": "remote", "n_ctx": 32768, "timeout": 120.0}] * 2


class ServerError(RuntimeError):
    """어댑터의 RemoteServerError와 같은 모양. 본문에 질문이 되풀이될 수 있다."""

    def __init__(self, status_code: int | None) -> None:
        super().__init__(f"body echoes {Q1}")
        self.status_code = status_code


@pytest.mark.parametrize(
    ("error", "started", "code"),
    [
        (ServerError(401), False, ErrorCode.AUTH_ERROR),
        (ServerError(403), False, ErrorCode.AUTH_ERROR),
        (ServerError(429), False, ErrorCode.RATE_LIMITED),
        (ServerError(503), False, ErrorCode.UNAVAILABLE),
        (ServerError(400), False, ErrorCode.REJECTED),
        (ServerError(None), False, ErrorCode.UNAVAILABLE),
        (ServerError(None), True, ErrorCode.STREAM_BROKEN),
        (httpx.ConnectError("x"), False, ErrorCode.UNAVAILABLE),
        (httpx.ReadTimeout("x"), False, ErrorCode.TIMEOUT),
        (httpx.RemoteProtocolError("x"), True, ErrorCode.STREAM_BROKEN),
        (ValueError(Q1), False, ErrorCode.INTERNAL),
    ],
)
def test_local_errors_become_fixed_codes_without_bodies(
    error: Exception, started: bool, code: ErrorCode
) -> None:
    llama = FakeLlama([delta("글"), error] if started else [error])
    events, failure = collect(LocalStandalone(lambda: llama))
    assert isinstance(failure, ModelFailure) and failure.code is code
    assert Q1 not in str(failure)
    assert text_of(events) == ("글" if started else "")
    assert llama.closed == 1


@pytest.mark.parametrize(
    "error", [FileNotFoundError("k"), PermissionError("k"), ValueError("k")]
)
def test_local_key_problem_is_key_missing(error: Exception) -> None:
    def factory() -> Any:
        raise error

    _, failure = collect(LocalStandalone(factory))
    assert isinstance(failure, ModelFailure)
    assert failure.code is ErrorCode.KEY_MISSING


# 21. 취소


class BlockingLlama(FakeLlama):
    """지정한 곳에서 close()가 불릴 때까지 멈추는 어댑터."""

    def __init__(self, where: str) -> None:
        super().__init__([])
        self.where = where
        self.reached = threading.Event()
        self.released = threading.Event()

    def _block(self) -> None:
        self.reached.set()
        assert self.released.wait(5)
        raise httpx.RemoteProtocolError("closed")

    def tokenize(
        self, text: bytes, add_bos: bool = True, special: bool = False
    ) -> list[int]:
        if self.where == "tokenize":
            self._block()
        return [0]

    def create_chat_completion(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        if self.where == "mid_stream":
            yield delta("받은 글")
        self._block()

    def close(self) -> None:
        super().close()
        self.released.set()


def run_in_thread(
    model: LocalStandalone | FrontierStandalone, cancellation: Cancellation
) -> tuple[threading.Thread, list[Any]]:
    out: list[Any] = []

    def work() -> None:
        try:
            for event in model.stream([], Q1, cancellation):
                out.append(event)
        except Exception as error:
            out.append(error)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, out


@pytest.mark.parametrize("where", ["tokenize", "before_first_token", "mid_stream"])
def test_local_cancel_closes_the_adapter_and_is_not_a_failure(where: str) -> None:
    llama = BlockingLlama(where)
    cancellation = Cancellation()
    thread, out = run_in_thread(LocalStandalone(lambda: llama), cancellation)
    assert llama.reached.wait(5)
    cancellation.cancel()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(out[-1], Cancelled)
    assert llama.closed >= 1


def test_cancel_before_connecting_closes_immediately() -> None:
    cancellation = Cancellation()
    cancellation.cancel()
    llama = FakeLlama([delta("답", "stop")])
    with pytest.raises(Cancelled):
        list(LocalStandalone(lambda: llama).stream([], Q1, cancellation))
    assert llama.requests == []
    closed: list[bool] = []
    with pytest.raises(Cancelled), cancellation.closing(lambda: closed.append(True)):
        raise AssertionError("닫힌 뒤에는 요청하지 않는다")
    assert closed == [True]


# 프론티어 호출


def sse(*chunks: Any, done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return ("".join(lines) + ("data: [DONE]\n\n" if done else "")).encode()


USAGE = {"choices": [], "usage": {"prompt_tokens": 450, "completion_tokens": 380}}
Handler = Callable[[httpx.Request], httpx.Response]


class BrokenStream(httpx.SyncByteStream):
    """앞부분을 보낸 뒤 끊기는 응답 본문."""

    def __init__(self, head: bytes) -> None:
        self.head = head

    def __iter__(self) -> Iterator[bytes]:
        if self.head:
            yield self.head
        raise httpx.ReadError("connection lost")


def make_frontier(handler: Handler, *, key: str | None = "sk-secret") -> Any:
    return FrontierStandalone(
        api_key=key,
        base_url="https://gateway.test/v1/",
        model="bedrock-test",
        input_price=0.11,
        output_price=0.55,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frontier_module, "RETRY_DELAYS", (0.0, 0.0))


def test_frontier_request_and_stream() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = sse(delta("안"), delta("녕"), delta("", "stop"), USAGE)
        return httpx.Response(200, content=b": ping\n\n" + body)

    model = make_frontier(handler)
    events, error = collect(model, [Turn(question=Q1, answer=A1)], Q2)
    assert error is None
    assert text_of(events) == "안녕"
    assert [e.finish_reason for e in events if e.finish_reason] == ["stop"]
    (request,) = requests
    assert str(request.url) == "https://gateway.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-secret"
    assert json.loads(request.content) == {
        "model": "bedrock-test",
        "messages": build_messages([Turn(question=Q1, answer=A1)], Q2),
        "max_completion_tokens": 4096,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    assert model.cost(450, 380) == pytest.approx((450 * 0.11 + 380 * 0.55) / 1e6)
    assert model.cost(None, 380) is None
    assert model.model_name == "bedrock-test"


def test_frontier_without_key_sends_nothing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("보내지 않아야 한다")

    _, failure = collect(make_frontier(handler, key=None))
    assert isinstance(failure, ModelFailure)
    assert failure.code is ErrorCode.KEY_MISSING


def error_body(code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": f"echo {Q1}", "code": code}}


@pytest.mark.parametrize(
    ("status", "body", "code", "calls"),
    [
        (429, error_body(), ErrorCode.RATE_LIMITED, 3),
        (500, {}, ErrorCode.UNAVAILABLE, 3),
        (503, error_body(), ErrorCode.UNAVAILABLE, 3),
        (429, error_body("insufficient_quota"), ErrorCode.QUOTA_EXCEEDED, 1),
        (429, error_body("quota_exceeded"), ErrorCode.QUOTA_EXCEEDED, 1),
        (403, error_body("budget_exceeded"), ErrorCode.QUOTA_EXCEEDED, 1),
        (422, error_body(), ErrorCode.QUOTA_EXCEEDED, 1),
        (401, error_body(), ErrorCode.AUTH_ERROR, 1),
        (403, error_body(), ErrorCode.AUTH_ERROR, 1),
        (400, error_body(), ErrorCode.REJECTED, 1),
    ],
)
def test_frontier_http_errors(
    status: int, body: dict[str, Any], code: ErrorCode, calls: int
) -> None:
    seen: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        seen.append(status)
        return httpx.Response(status, json=body)

    _, failure = collect(make_frontier(handler))
    assert isinstance(failure, ModelFailure) and failure.code is code
    assert Q1 not in str(failure)
    assert len(seen) == calls


@pytest.mark.parametrize("first", ["connect", "500", "broken", "stream_error"])
def test_frontier_retries_before_text(first: str) -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            if first == "connect":
                raise httpx.ConnectError("down")
            if first == "500":
                return httpx.Response(500)
            if first == "stream_error":
                return httpx.Response(200, content=sse(error_body()))
            return httpx.Response(200, stream=BrokenStream(b""))
        return httpx.Response(200, content=sse(delta("성공", "stop"), USAGE))

    events, error = collect(make_frontier(handler))
    assert error is None and text_of(events) == "성공"
    assert len(calls) == 3


@pytest.mark.parametrize("how", ["broken", "stream_error", "quota", "bad_json"])
def test_frontier_does_not_retry_after_text(how: str) -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        head = sse(delta("받은 글"), done=False)
        if how == "broken":
            return httpx.Response(200, stream=BrokenStream(head))
        if how == "bad_json":
            return httpx.Response(200, content=head + b"data: {oops\n\n")
        code = "quota_exceeded" if how == "quota" else None
        return httpx.Response(200, content=head + sse(error_body(code)))

    events, failure = collect(make_frontier(handler))
    assert isinstance(failure, ModelFailure)
    assert failure.code is ErrorCode.STREAM_BROKEN
    assert text_of(events) == "받은 글" and len(calls) == 1


def test_frontier_network_failure_after_three_attempts() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    _, failure = collect(make_frontier(handler))
    assert isinstance(failure, ModelFailure)
    assert failure.code is ErrorCode.UNAVAILABLE


def test_frontier_cancel_while_waiting_to_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frontier_module, "RETRY_DELAYS", (30.0, 30.0))
    called = threading.Event()

    def handler(_: httpx.Request) -> httpx.Response:
        called.set()
        return httpx.Response(500)

    cancellation = Cancellation()
    thread, out = run_in_thread(make_frontier(handler), cancellation)
    assert called.wait(5)
    started = time.monotonic()
    cancellation.cancel()
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - started < 3
    assert isinstance(out[-1], Cancelled)


def test_frontier_cancel_closes_the_connection() -> None:
    reached, released = threading.Event(), threading.Event()

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            reached.set()
            assert released.wait(5)
            raise httpx.RemoteProtocolError("closed")

        def close(self) -> None:
            released.set()

    model = FrontierStandalone(
        api_key="k", base_url="https://g.test", model="m", transport=Transport()
    )
    cancellation = Cancellation()
    thread, out = run_in_thread(model, cancellation)
    assert reached.wait(5)
    cancellation.cancel()
    thread.join(5)
    assert not thread.is_alive()
    assert isinstance(out[-1], Cancelled)


def test_local_module_does_not_import_the_adapter_until_called() -> None:
    assert "llama_wrapper" not in vars(local_module)

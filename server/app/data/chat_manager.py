"""SQLite 채팅·실행 저장소. 모든 쓰기는 잠금과 트랜잭션으로 직렬화한다."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Self

from pydantic import BaseModel, JsonValue

from app.models.enums import EndReason
from app.models.llm_results import Rule
from app.models.schemas import (
    ChatDetail,
    ChatSummary,
    ConsentDecision,
    ConsentItem,
    ConsentRequest,
    FrontierQuery,
    FrontierQueryInput,
    Message,
    PDItem,
    PDTable,
    PrivacySetting,
    QueryItem,
    RunConfig,
    RunDetail,
    RunEvent,
    RunSummary,
    StartedRun,
)

SCHEMA_SQL = """
CREATE TABLE chats (
    chatId INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL DEFAULT '',
    createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL
);
CREATE TABLE messages (
    messageId INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL, createdAt TEXT NOT NULL
);
CREATE TABLE privacy_settings (
    settingId INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
    rules TEXT NOT NULL, updatedAt TEXT NOT NULL
);
CREATE TABLE pd_items (
    pdId INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    messageId INTEGER NOT NULL REFERENCES messages ON DELETE CASCADE,
    text TEXT NOT NULL, category INTEGER NOT NULL CHECK(category BETWEEN 0 AND 2),
    allowed INTEGER NOT NULL DEFAULT 0 CHECK(allowed IN (0, 1)),
    allowedBy TEXT NOT NULL DEFAULT 'none', createdAt TEXT NOT NULL,
    UNIQUE(chatId, text, category)
);
CREATE TABLE runs (
    runId INTEGER PRIMARY KEY AUTOINCREMENT,
    chatId INTEGER NOT NULL REFERENCES chats ON DELETE CASCADE,
    userMessageId INTEGER NOT NULL REFERENCES messages,
    answerMessageId INTEGER REFERENCES messages,
    settingId INTEGER REFERENCES privacy_settings,
    config TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running'
        CHECK(status IN ('running', 'finished')),
    endReason TEXT, notice TEXT, errorCode TEXT,
    frontierCalls INTEGER NOT NULL DEFAULT 0,
    regenerations INTEGER NOT NULL DEFAULT 0, spentCost REAL NOT NULL DEFAULT 0,
    memoFinal TEXT, startedAt TEXT NOT NULL, endedAt TEXT
);
CREATE TABLE run_events (
    eventId INTEGER NOT NULL,
    runId INTEGER NOT NULL REFERENCES runs ON DELETE CASCADE,
    type TEXT NOT NULL, payload TEXT NOT NULL, createdAt TEXT NOT NULL,
    PRIMARY KEY(runId, eventId)
);
CREATE TABLE consent_requests (
    consentId INTEGER PRIMARY KEY AUTOINCREMENT,
    runId INTEGER NOT NULL REFERENCES runs ON DELETE CASCADE,
    items TEXT NOT NULL, purpose TEXT NOT NULL, benefit TEXT NOT NULL,
    risk TEXT NOT NULL, result TEXT CHECK(result IN ('answered', 'cancelled')),
    responseText TEXT, decisions TEXT NOT NULL DEFAULT '[]',
    createdAt TEXT NOT NULL, respondedAt TEXT
);
CREATE UNIQUE INDEX pending_consent ON consent_requests(runId) WHERE result IS NULL;
CREATE TABLE frontier_queries (
    queryId INTEGER PRIMARY KEY AUTOINCREMENT,
    runId INTEGER NOT NULL REFERENCES runs ON DELETE CASCADE,
    round INTEGER NOT NULL, request TEXT NOT NULL, answer TEXT,
    status TEXT NOT NULL CHECK(status IN ('ok', 'failed', 'uncertain')),
    contextMode TEXT, inputTokens INTEGER, outputTokens INTEGER,
    cost REAL NOT NULL DEFAULT 0, elapsedMs REAL, createdAt TEXT NOT NULL,
    UNIQUE(runId, round)
);
CREATE TABLE query_items (
    itemId INTEGER PRIMARY KEY AUTOINCREMENT,
    queryId INTEGER NOT NULL REFERENCES frontier_queries ON DELETE CASCADE,
    pdId INTEGER NOT NULL REFERENCES pd_items ON DELETE CASCADE,
    allowed INTEGER NOT NULL CHECK(allowed IN (0, 1)), allowedBy TEXT NOT NULL,
    UNIQUE(queryId, pdId)
);
CREATE INDEX messages_chat ON messages(chatId, messageId);
CREATE INDEX runs_chat ON runs(chatId, runId);
CREATE INDEX events_run ON run_events(runId, eventId);
CREATE INDEX consents_run ON consent_requests(runId, consentId);
CREATE INDEX queries_run ON frontier_queries(runId, round);
"""

NOTICES: dict[EndReason, str] = {
    EndReason.CALL_LIMIT: "프론티어 호출 상한에 도달했습니다.",
    EndReason.REGEN_LIMIT: "안전한 요청문 재생성 상한에 도달했습니다.",
    EndReason.BUDGET_LIMIT: "실행 비용 한도에 도달했습니다.",
    EndReason.OFFLINE: "프론티어에 연결할 수 없습니다.",
    EndReason.STOPPED: "실행이 중지되었습니다.",
    EndReason.CONSENT_DENIED: "개인정보 공개가 거부되었습니다.",
}


class StorageError(RuntimeError):
    """API 계층이 code를 HTTP 오류로 변환할 수 있는 저장소 오류."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: JsonValue | list[BaseModel]) -> str:
    if isinstance(value, list):
        value = [
            v.model_dump(by_alias=True) if isinstance(v, BaseModel) else v
            for v in value
        ]
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class ChatManager:
    """단일 프로세스에서 공유. 재시작 복구는 실행 작업을 띄우기 전에 한 번 호출한다."""

    def __init__(self, db_path: str | Path = "chat.sqlite3") -> None:
        self._lock = RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._closed = False
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            # 스키마 버전은 관리하지 않는다. 빈 DB에만 스키마를 만들고, 스키마가 바뀌면
            # 기존 DB 파일을 수동으로 지우고 다시 만든다.
            tables = self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if not tables:
                self._db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL + "\nCOMMIT;")
        except BaseException:
            self._db.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError("저장소가 닫혔습니다.")
            self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _require(self, table: str, key: str, id_: int) -> sqlite3.Row:
        # table/key는 내부 상수만 전달한다. 사용자 값은 항상 바인딩한다.
        row = self._db.execute(
            f"SELECT * FROM {table} WHERE {key} = ?", (id_,)
        ).fetchone()
        if row is None:
            raise StorageError("NOT_FOUND", "기록을 찾을 수 없습니다.")
        return row

    def _running(self, run_id: int) -> sqlite3.Row:
        row = self._require("runs", "runId", run_id)
        if row["status"] != "running":
            raise StorageError("RUN_FINISHED", "이미 종료된 실행입니다.")
        return row

    def _message(self, chat_id: int, message_id: int, role: str) -> None:
        row = self._require("messages", "messageId", message_id)
        if row["chatId"] != chat_id or row["role"] != role:
            raise ValueError("메시지의 채팅 또는 역할이 맞지 않습니다.")

    def _items(self, chat_id: int, ids: list[int]) -> None:
        if len(ids) != len(set(ids)):
            raise ValueError("개인정보 ID가 중복되었습니다.")
        for pd_id in ids:
            if self._require("pd_items", "pdId", pd_id)["chatId"] != chat_id:
                raise ValueError("다른 채팅의 개인정보입니다.")

    def create_chat(self) -> int:
        with self._transaction() as db:
            now = _now()
            return int(
                db.execute(
                    "INSERT INTO chats(createdAt, updatedAt) VALUES (?, ?)", (now, now)
                ).lastrowid
            )

    def list_chats(self) -> list[ChatSummary]:
        with self._transaction(write=False) as db:
            return [
                ChatSummary.model_validate(dict(row))
                for row in db.execute(
                    "SELECT chatId, title FROM chats ORDER BY updatedAt DESC, chatId DESC"
                )
            ]

    def get_chat(self, chat_id: int) -> ChatDetail:
        with self._transaction(write=False) as db:
            row = self._require("chats", "chatId", chat_id)
            messages = [
                Message.model_validate(dict(m))
                for m in db.execute(
                    "SELECT * FROM messages WHERE chatId=? AND role IN ('user','assistant') ORDER BY messageId",
                    (chat_id,),
                )
            ]
            runs = [
                RunSummary.model_validate(dict(r))
                for r in db.execute(
                    "SELECT * FROM runs WHERE chatId=? ORDER BY runId", (chat_id,)
                )
            ]
            return ChatDetail(
                chat_id=chat_id, title=row["title"], messages=messages, runs=runs
            )

    def delete_chat(self, chat_id: int) -> None:
        with self._transaction() as db:
            self._require("chats", "chatId", chat_id)
            if db.execute(
                "SELECT 1 FROM runs WHERE chatId=? AND status='running'", (chat_id,)
            ).fetchone():
                raise StorageError(
                    "CHAT_RUNNING", "실행 중인 채팅은 삭제할 수 없습니다."
                )
            # messages를 참조하는 runs를 먼저 삭제한다.
            db.execute("DELETE FROM runs WHERE chatId=?", (chat_id,))
            db.execute("DELETE FROM chats WHERE chatId=?", (chat_id,))

    def save_message(self, chat_id: int, role: str, content: str) -> int:
        with self._transaction():
            return self._save_message(chat_id, role, content)

    def _save_message(self, chat_id: int, role: str, content: str) -> int:
        if role not in {"user", "assistant", "context_compacted"}:
            raise ValueError("지원하지 않는 메시지 역할입니다.")
        self._require("chats", "chatId", chat_id)
        now = _now()
        first = (
            role == "user"
            and not self._db.execute(
                "SELECT 1 FROM messages WHERE chatId=? AND role='user'", (chat_id,)
            ).fetchone()
        )
        message_id = self._db.execute(
            "INSERT INTO messages(chatId, role, content, createdAt) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, now),
        ).lastrowid
        self._db.execute(
            "UPDATE chats SET updatedAt=?, title=CASE WHEN ? THEN ? ELSE title END WHERE chatId=?",
            (now, first, content[:30], chat_id),
        )
        return int(message_id)

    def save_setting(self, text: str, rules: list[Rule]) -> PrivacySetting:
        rules = [Rule.model_validate(rule) for rule in rules]
        with self._transaction() as db:
            now = _now()
            id_ = db.execute(
                "INSERT INTO privacy_settings(text, rules, updatedAt) VALUES (?, ?, ?)",
                (text, _json(rules), now),
            ).lastrowid
            return PrivacySetting(
                setting_id=id_, text=text, rules=rules, updated_at=now
            )

    def load_setting(self, setting_id: int | None = None) -> PrivacySetting:
        with self._transaction(write=False) as db:
            if setting_id is None:
                row = db.execute(
                    "SELECT * FROM privacy_settings ORDER BY settingId DESC LIMIT 1"
                ).fetchone()
            else:
                row = self._require("privacy_settings", "settingId", setting_id)
            return self._setting(row) if row else PrivacySetting()

    def _setting(self, row: sqlite3.Row) -> PrivacySetting:
        return PrivacySetting.model_validate(
            {**dict(row), "rules": json.loads(row["rules"])}
        )

    def load_settings(self) -> list[PrivacySetting]:
        with self._transaction(write=False) as db:
            return [
                self._setting(row)
                for row in db.execute(
                    "SELECT * FROM privacy_settings ORDER BY settingId"
                )
            ]

    def load_run_setting(self, run_id: int) -> PrivacySetting:
        """실행 시작 시 고정된 설정 조회. 설정이 없던 실행은 빈 설정을 유지한다."""
        with self._transaction(write=False):
            run = self._require("runs", "runId", run_id)
            if run["settingId"] is None:
                return PrivacySetting()
            return self._setting(
                self._require("privacy_settings", "settingId", run["settingId"])
            )

    def start_run(
        self,
        chat_id: int,
        user_message_id: int,
        setting_id: int | None,
        config: RunConfig,
    ) -> int:
        config = RunConfig.model_validate(config)
        with self._transaction():
            return self._start_run(chat_id, user_message_id, setting_id, config)

    def _start_run(
        self,
        chat_id: int,
        user_message_id: int,
        setting_id: int | None,
        config: RunConfig,
    ) -> int:
        self._message(chat_id, user_message_id, "user")
        if setting_id is not None:
            self._require("privacy_settings", "settingId", setting_id)
        return int(
            self._db.execute(
                "INSERT INTO runs(chatId, userMessageId, settingId, config, startedAt) VALUES (?, ?, ?, ?, ?)",
                (
                    chat_id,
                    user_message_id,
                    setting_id,
                    config.model_dump_json(by_alias=True),
                    _now(),
                ),
            ).lastrowid
        )

    def start_turn(
        self,
        chat_id: int,
        question: str,
        setting_id: int | None,
        config: RunConfig,
    ) -> StartedRun:
        """질문과 실행을 한 트랜잭션으로 생성한다. 실패하면 제목 변경까지 롤백한다."""
        config = RunConfig.model_validate(config)
        with self._transaction():
            message_id = self._save_message(chat_id, "user", question)
            run_id = self._start_run(chat_id, message_id, setting_id, config)
            return StartedRun(user_message_id=message_id, run_id=run_id)

    def finish_run(
        self,
        run_id: int,
        end_reason: EndReason | None,
        answer_message_id: int | None,
        memo_final: str | None,
        *,
        error_code: str | None = None,
        notice: str | None = None,
    ) -> RunEvent:
        if end_reason is not None:
            end_reason = EndReason(end_reason)
        with self._transaction() as db:
            row = self._require("runs", "runId", run_id)
            if row["status"] == "finished":
                return self._terminal_event(row)
            if answer_message_id is not None:
                self._message(row["chatId"], answer_message_id, "assistant")
            error_code = error_code or row["errorCode"]
            if error_code:
                end_reason = None
            notice = notice or row["notice"]
            if end_reason is None and error_code is None:
                error_code = "INTERNAL_ERROR"
            notice = notice or (
                "실행 중 오류가 발생했습니다."
                if error_code
                else NOTICES.get(end_reason)
            )
            now = _now()
            db.execute(
                "UPDATE runs SET status='finished', endReason=?, answerMessageId=?, memoFinal=?, errorCode=?, notice=?, endedAt=? WHERE runId=?",
                (
                    end_reason,
                    answer_message_id,
                    memo_final,
                    error_code,
                    notice,
                    now,
                    run_id,
                ),
            )
            db.execute(
                "UPDATE consent_requests SET result='cancelled', respondedAt=? WHERE runId=? AND result IS NULL",
                (now, run_id),
            )
            return self._terminal_event(self._require("runs", "runId", run_id))

    def _append_event(self, run_id: int, type_: str, payload: JsonValue) -> RunEvent:
        event_id = self._db.execute(
            "SELECT COALESCE(MAX(eventId), 0)+1 FROM run_events WHERE runId=?",
            (run_id,),
        ).fetchone()[0]
        now = _now()
        self._db.execute(
            "INSERT INTO run_events(runId, eventId, type, payload, createdAt) VALUES (?, ?, ?, ?, ?)",
            (run_id, event_id, type_, _json(payload), now),
        )
        return RunEvent(
            event_id=event_id,
            run_id=run_id,
            type=type_,
            payload=payload,
            created_at=now,
        )

    def _terminal_event(self, run: sqlite3.Row) -> RunEvent:
        existing = self._db.execute(
            "SELECT * FROM run_events WHERE runId=? AND type IN ('done', 'error') ORDER BY eventId DESC LIMIT 1",
            (run["runId"],),
        ).fetchone()
        if existing is not None:
            return RunEvent.model_validate(
                {**dict(existing), "payload": json.loads(existing["payload"])}
            )
        if run["errorCode"] or run["endReason"] is None:
            return self._append_event(
                run["runId"],
                "error",
                {
                    "code": run["errorCode"] or "INTERNAL_ERROR",
                    "message": run["notice"] or "실행 중 오류가 발생했습니다.",
                },
            )
        return self._append_event(
            run["runId"],
            "done",
            {
                "endReason": run["endReason"],
                "limited": run["notice"],
                "answerMessageId": run["answerMessageId"],
            },
        )

    def recover_interrupted_runs(self) -> int:
        """단일 서버 부팅 시 호출. 이전 프로세스의 실행·동의 대기를 오류 종료한다."""
        with self._transaction() as db:
            now = _now()
            db.execute(
                "UPDATE consent_requests SET result='cancelled', respondedAt=? WHERE result IS NULL AND runId IN (SELECT runId FROM runs WHERE status='running')",
                (now,),
            )
            count = db.execute(
                "UPDATE runs SET status='finished', endReason=NULL, errorCode='RUN_INTERRUPTED', notice='앱 종료로 실행이 중단되었습니다.', endedAt=? WHERE status='running'",
                (now,),
            ).rowcount
            for row in db.execute(
                "SELECT * FROM runs WHERE endedAt=? AND errorCode='RUN_INTERRUPTED'",
                (now,),
            ).fetchall():
                self._terminal_event(row)
            return count

    def set_pd_items(self, chat_id: int, message_id: int, items: PDTable) -> PDTable:
        with self._transaction() as db:
            self._message(chat_id, message_id, "user")
            result: PDTable = []
            for item in items:
                db.execute(
                    "INSERT INTO pd_items(chatId, messageId, text, category, createdAt) VALUES (?, ?, ?, ?, ?) ON CONFLICT(chatId, text, category) DO NOTHING",
                    (chat_id, message_id, item.text, item.category, _now()),
                )
                row = db.execute(
                    "SELECT * FROM pd_items WHERE chatId=? AND text=? AND category=?",
                    (chat_id, item.text, item.category),
                ).fetchone()
                result.append(PDItem.model_validate(dict(row)))
            return result

    def get_pd_table(self, chat_id: int) -> PDTable:
        with self._transaction(write=False) as db:
            self._require("chats", "chatId", chat_id)
            return [
                PDItem.model_validate(dict(row))
                for row in db.execute(
                    "SELECT * FROM pd_items WHERE chatId=? ORDER BY pdId", (chat_id,)
                )
            ]

    def update_pd_allowed(self, chat_id: int, items: PDTable) -> None:
        """사전 설정만 저장. 실행 중 동의·거부 상태는 이 메서드에 전달할 수 없다."""
        if any(item.allowed_by not in {"none", "setting"} for item in items):
            raise ValueError(
                "영구 저장에는 none 또는 setting 근거만 사용할 수 있습니다."
            )
        if any(item.allowed and item.allowed_by != "setting" for item in items):
            raise ValueError("영구 허용은 사전 설정 근거가 필요합니다.")
        with self._transaction() as db:
            self._require("chats", "chatId", chat_id)
            self._items(chat_id, [item.pd_id for item in items])
            db.executemany(
                "UPDATE pd_items SET allowed=?, allowedBy=? WHERE pdId=?",
                [(item.allowed, item.allowed_by, item.pd_id) for item in items],
            )

    def save_consent(
        self,
        run_id: int,
        items: list[ConsentItem],
        purpose: str,
        benefit: str,
        risk: str,
    ) -> ConsentRequest:
        if not items:
            raise ValueError("동의 요청 항목이 없습니다.")
        with self._transaction() as db:
            run = self._running(run_id)
            self._items(run["chatId"], [item.pd_id for item in items])
            if db.execute(
                "SELECT 1 FROM consent_requests WHERE runId=? AND result IS NULL",
                (run_id,),
            ).fetchone():
                raise StorageError(
                    "CONSENT_PENDING", "이미 대기 중인 동의 요청이 있습니다."
                )
            id_ = db.execute(
                "INSERT INTO consent_requests(runId, items, purpose, benefit, risk, createdAt) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, _json(items), purpose, benefit, risk, _now()),
            ).lastrowid
            return self._consent(self._require("consent_requests", "consentId", id_))

    def _consent(self, row: sqlite3.Row) -> ConsentRequest:
        return ConsentRequest.model_validate(
            {
                **dict(row),
                "items": json.loads(row["items"]),
                "decisions": json.loads(row["decisions"]),
            }
        )

    def get_pending_consent(self, run_id: int) -> ConsentRequest | None:
        with self._transaction(write=False) as db:
            self._require("runs", "runId", run_id)
            row = db.execute(
                "SELECT * FROM consent_requests WHERE runId=? AND result IS NULL",
                (run_id,),
            ).fetchone()
            return self._consent(row) if row else None

    def update_consent(
        self,
        run_id: int,
        consent_id: int,
        decisions: list[ConsentDecision],
        response_text: str | None = None,
        *,
        cancelled: bool = False,
    ) -> ConsentRequest:
        with self._transaction() as db:
            self._running(run_id)
            row = db.execute(
                "SELECT * FROM consent_requests WHERE consentId=? AND runId=?",
                (consent_id, run_id),
            ).fetchone()
            if row is None or row["result"] is not None:
                raise StorageError(
                    "NO_PENDING_CONSENT", "현재 응답할 수 있는 동의 요청이 아닙니다."
                )
            ids = [item.pd_id for item in decisions]
            requested = {item["pdId"] for item in json.loads(row["items"])}
            if cancelled:
                if decisions or response_text is not None:
                    raise ValueError("취소에는 응답이나 결정을 저장할 수 없습니다.")
            elif (
                not response_text
                or not response_text.strip()
                or len(ids) != len(set(ids))
                or set(ids) != requested
            ):
                raise ValueError(
                    "응답 원문과 요청 항목별 정확히 하나의 결정이 필요합니다."
                )
            db.execute(
                "UPDATE consent_requests SET result=?, responseText=?, decisions=?, respondedAt=? WHERE consentId=?",
                (
                    "cancelled" if cancelled else "answered",
                    response_text,
                    _json(decisions),
                    _now(),
                    consent_id,
                ),
            )
            return self._consent(
                self._require("consent_requests", "consentId", consent_id)
            )

    def save_frontier_query(
        self,
        run_id: int,
        query: FrontierQueryInput,
        items: PDTable,
    ) -> int:
        """전송 직전 uncertain으로 저장하고 수신 후 finish_frontier_query로 갱신한다."""
        with self._transaction() as db:
            run = self._running(run_id)
            self._items(run["chatId"], [item.pd_id for item in items])
            round_ = db.execute(
                "SELECT COALESCE(MAX(round), 0)+1 FROM frontier_queries WHERE runId=?",
                (run_id,),
            ).fetchone()[0]
            id_ = db.execute(
                "INSERT INTO frontier_queries(runId, round, request, answer, status, contextMode, inputTokens, outputTokens, cost, elapsedMs, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    round_,
                    query.request,
                    query.answer,
                    query.status,
                    query.context_mode,
                    query.input_tokens,
                    query.output_tokens,
                    query.cost,
                    query.elapsed_ms,
                    _now(),
                ),
            ).lastrowid
            db.executemany(
                "INSERT INTO query_items(queryId, pdId, allowed, allowedBy) VALUES (?, ?, ?, ?)",
                [(id_, item.pd_id, item.allowed, item.allowed_by) for item in items],
            )
            db.execute(
                "UPDATE runs SET frontierCalls=frontierCalls+1, spentCost=spentCost+? WHERE runId=?",
                (query.cost, run_id),
            )
            return int(id_)

    def finish_frontier_query(
        self, run_id: int, query_id: int, query: FrontierQueryInput
    ) -> None:
        with self._transaction() as db:
            self._running(run_id)
            old = self._require("frontier_queries", "queryId", query_id)
            if old["runId"] != run_id or old["request"] != query.request:
                raise ValueError("질의의 실행 또는 요청문이 일치하지 않습니다.")
            if old["status"] != "uncertain":
                raise StorageError("QUERY_FINISHED", "이미 처리된 질의입니다.")
            db.execute(
                "UPDATE frontier_queries SET answer=?, status=?, contextMode=?, inputTokens=?, outputTokens=?, cost=?, elapsedMs=? WHERE queryId=?",
                (
                    query.answer,
                    query.status,
                    query.context_mode,
                    query.input_tokens,
                    query.output_tokens,
                    query.cost,
                    query.elapsed_ms,
                    query_id,
                ),
            )
            db.execute(
                "UPDATE runs SET spentCost=spentCost+? WHERE runId=?",
                (query.cost - old["cost"], run_id),
            )

    def record_regeneration(self, run_id: int) -> None:
        with self._transaction() as db:
            self._running(run_id)
            db.execute(
                "UPDATE runs SET regenerations=regenerations+1 WHERE runId=?", (run_id,)
            )

    def log_event(self, run_id: int, type_: str, payload: JsonValue) -> int:
        if type_ == "error" and not isinstance(payload, dict):
            raise ValueError("오류 이벤트의 payload는 객체여야 합니다.")
        with self._transaction() as db:
            self._running(run_id)
            if type_ == "done":
                raise ValueError("종료 이벤트는 finish_run()으로 기록합니다.")
            if type_ == "error" and isinstance(payload, dict):
                db.execute(
                    "UPDATE runs SET errorCode=COALESCE(errorCode, ?), notice=COALESCE(notice, ?) WHERE runId=?",
                    (
                        payload.get("code", "INTERNAL_ERROR"),
                        payload.get("message", "실행 중 오류가 발생했습니다."),
                        run_id,
                    ),
                )
                type_ = "run_error"
            return self._append_event(run_id, type_, payload).event_id

    def get_events(self, run_id: int, after_event_id: int = 0) -> list[RunEvent]:
        with self._transaction(write=False) as db:
            self._require("runs", "runId", run_id)
            return [
                RunEvent.model_validate(
                    {**dict(row), "payload": json.loads(row["payload"])}
                )
                for row in db.execute(
                    "SELECT * FROM run_events WHERE runId=? AND eventId>? ORDER BY eventId",
                    (run_id, after_event_id),
                )
            ]

    def get_run(self, run_id: int) -> RunDetail:
        with self._transaction(write=False) as db:
            row = self._require("runs", "runId", run_id)
            consents = [
                self._consent(c)
                for c in db.execute(
                    "SELECT * FROM consent_requests WHERE runId=? ORDER BY consentId",
                    (run_id,),
                )
            ]
            queries = []
            for q in db.execute(
                "SELECT * FROM frontier_queries WHERE runId=? ORDER BY round", (run_id,)
            ):
                items = [
                    QueryItem.model_validate(dict(i))
                    for i in db.execute(
                        "SELECT * FROM query_items WHERE queryId=? ORDER BY itemId",
                        (q["queryId"],),
                    )
                ]
                queries.append(
                    FrontierQuery.model_validate({**dict(q), "items": items})
                )
            return RunDetail.model_validate(
                {
                    **dict(row),
                    "config": json.loads(row["config"]),
                    "consents": consents,
                    "frontier_queries": queries,
                }
            )

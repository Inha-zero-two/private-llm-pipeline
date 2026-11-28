"""비교 기록 읽기 · 쓰기.

자기 테이블(comparison_chats · comparisons)만 다룬다. 파이프라인 테이블은 조회하지 않는다.
테이블은 ChatManager가 만들므로 ChatManager가 DB를 준비한 뒤에 연다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from .models import KINDS, ErrorCode, Kind, Record, Result, Turn

# previousOk: 이 줄을 만들 때 앞 대화로 쓸 수 있었던 줄의 수
_SELECT = """
    SELECT c.*, (
        SELECT COUNT(*) FROM comparisons p
        WHERE p.chatId = c.chatId AND p.kind = c.kind
          AND p.status = 'ok' AND p.runId < c.runId
    ) AS previousOk
    FROM comparisons c
"""
_ORDER = " ORDER BY c.runId, c.kind"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record(row: sqlite3.Row) -> Record:
    return Record(
        chat_id=row["chatId"],
        run_id=row["runId"],
        kind=row["kind"],
        question=row["question"],
        answer=row["answer"],
        status=row["status"],
        error_code=row["errorCode"],
        model_name=row["modelName"],
        attempts=row["attempts"],
        dropped_pairs=row["droppedPairs"],
        input_tokens=row["inputTokens"],
        output_tokens=row["outputTokens"],
        cost=row["cost"],
        elapsed_ms=row["elapsedMs"],
        created_at=row["createdAt"],
        ended_at=row["endedAt"],
        previous_ok=row["previousOk"],
    )


class ComparisonStore:
    def __init__(self, db_path: str | Path) -> None:
        self._lock = RLock()
        self._closed = False
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._db.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _read(self, sql: str, values: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, values).fetchall()

    # 비교 대상 채팅

    def register_chat(self, chat_id: int) -> None:
        with self._write() as db:
            db.execute(
                "INSERT OR IGNORE INTO comparison_chats(chatId) VALUES (?)", (chat_id,)
            )

    def unregister_chat(self, chat_id: int) -> None:
        with self._write() as db:
            db.execute("DELETE FROM comparison_chats WHERE chatId = ?", (chat_id,))

    def is_registered(self, chat_id: int) -> bool:
        return bool(
            self._read("SELECT 1 FROM comparison_chats WHERE chatId = ?", (chat_id,))
        )

    # 비교 기록

    def begin(self, chat_id: int, run_id: int, question: str) -> None:
        """두 모델의 줄을 running으로 함께 추가한다."""
        now = _now()
        with self._write() as db:
            db.executemany(
                "INSERT INTO comparisons"
                "(chatId, runId, kind, question, status, createdAt)"
                " VALUES (?, ?, ?, ?, 'running', ?)",
                [(chat_id, run_id, kind, question, now) for kind in KINDS],
            )

    def history(self, chat_id: int, kind: Kind, before_run_id: int) -> list[Turn]:
        """같은 모델이 정상으로 끝낸 앞 질문·답변."""
        rows = self._read(
            "SELECT question, answer FROM comparisons"
            " WHERE chatId = ? AND kind = ? AND status = 'ok' AND runId < ?"
            " ORDER BY runId",
            (chat_id, kind, before_run_id),
        )
        return [Turn(question=r["question"], answer=r["answer"]) for r in rows]

    def finish(self, run_id: int, kind: Kind, result: Result) -> str:
        """진행 중인 줄에 결과를 저장하고 저장 시각을 돌려준다.

        채팅과 함께 줄이 지워졌으면 아무것도 하지 않는다.
        """
        now = _now()
        with self._write() as db:
            db.execute(
                "UPDATE comparisons SET answer=?, status=?, errorCode=?, modelName=?,"
                " droppedPairs=?, inputTokens=?, outputTokens=?, cost=?, elapsedMs=?,"
                " endedAt=? WHERE runId=? AND kind=? AND status='running'",
                (
                    result.answer,
                    result.status,
                    result.error_code,
                    result.model_name,
                    result.dropped_pairs,
                    result.input_tokens,
                    result.output_tokens,
                    result.cost,
                    result.elapsed_ms,
                    now,
                    run_id,
                    kind,
                ),
            )
        return now

    def restart(self, run_id: int, kind: Kind) -> None:
        """재시도: 같은 줄의 이전 결과를 비우고 시도 횟수를 올린다."""
        with self._write() as db:
            db.execute(
                "UPDATE comparisons SET status='running', answer=NULL, errorCode=NULL,"
                " modelName=NULL, droppedPairs=0, inputTokens=NULL, outputTokens=NULL,"
                " cost=NULL, elapsedMs=NULL, endedAt=NULL, attempts=attempts+1"
                " WHERE runId=? AND kind=? AND status<>'ok'",
                (run_id, kind),
            )

    def get(self, run_id: int, kind: Kind) -> Record | None:
        rows = self._read(_SELECT + " WHERE c.runId=? AND c.kind=?", (run_id, kind))
        return _record(rows[0]) if rows else None

    def list_run(self, run_id: int) -> list[Record]:
        rows = self._read(_SELECT + " WHERE c.runId=?" + _ORDER, (run_id,))
        return [_record(row) for row in rows]

    def list_chat(self, chat_id: int) -> list[Record]:
        rows = self._read(_SELECT + " WHERE c.chatId=?" + _ORDER, (chat_id,))
        return [_record(row) for row in rows]

    def recover_interrupted(self) -> int:
        """시작할 때, 끝나지 않은 줄을 실패로 정리하고 그 수를 돌려준다."""
        with self._write() as db:
            return db.execute(
                "UPDATE comparisons SET status='failed', errorCode=?, endedAt=?"
                " WHERE status='running'",
                (ErrorCode.INTERRUPTED, _now()),
            ).rowcount

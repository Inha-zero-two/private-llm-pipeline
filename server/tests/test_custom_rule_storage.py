"""추가 보호 분류를 도입할 때 기존 개인정보와 질의 기록을 보존한다."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.data.chat_manager import SCHEMA_PATH, ChatManager
from app.models.schemas import PDItem


def test_upgrade_preserves_query_items_and_sequence(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    old_schema = (
        SCHEMA_PATH.read_text(encoding="utf-8")
        .replace("category BETWEEN 0 AND 3", "category BETWEEN 0 AND 2")
        .replace("PRAGMA user_version = 2;", "")
        .replace(", errorStage TEXT", "")
    )
    with closing(sqlite3.connect(path)) as db:
        db.executescript(old_schema)
        db.executescript("""
            PRAGMA foreign_keys = ON;
            INSERT INTO chats VALUES (1, '', '', '');
            INSERT INTO messages VALUES (1, 1, 'user', '질문', '');
            INSERT INTO privacy_settings VALUES
                (1, '삼성 관련 금지', '[{"target":"회사","scope":"item","action":"deny"}]', '');
            INSERT INTO pd_items VALUES (5, 1, 1, '삼성전자', 2, 0, 'setting', '');
            INSERT INTO pd_items VALUES (8, 1, 1, '삭제됨', 2, 0, 'none', '');
            DELETE FROM pd_items WHERE pdId=8;
            INSERT INTO runs (runId, chatId, userMessageId, settingId, config, status, startedAt)
                VALUES (1, 1, 1, 1, '{}', 'finished', '');
            INSERT INTO frontier_queries (queryId, runId, round, request, status, createdAt)
                VALUES (1, 1, 1, '요청', 'ok', '');
            INSERT INTO query_items VALUES (1, 1, 5, 0, 'setting');
        """)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO pd_items VALUES (9, 1, 1, '0.62V', 3, 0, 'none', '')"
            )

    with ChatManager(path) as store:
        setting = store.load_run_setting(1)
        assert setting.rules[0].detect_en is None and setting.text == "삼성 관련 금지"
        assert store.get_pd_table(1)[0].pd_id == 5
        (item,) = store.set_pd_items(1, 1, [PDItem(text="0.62V", category=3)])
        assert item.pd_id == 9
    with ChatManager(path) as reopened:
        assert reopened.get_pd_table(1)[1] == item
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT pdId, allowedBy FROM query_items").fetchall() == [
            (5, "setting")
        ]
        assert db.execute("PRAGMA user_version").fetchone() == (2,)

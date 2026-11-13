"""DB 대신 쓰는 메모리 저장소. 서버를 끄면 사라진다.

나중에 ChatManager(설계 6.3)가 생기면 이 클래스의 자리를 대신한다.
ID는 명세서대로 1부터 증가하는 정수다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from app.api.events import RunEvents
from app.api.schemas import (
    ConsentOut,
    Decision,
    FrontierQueryOut,
    MessageOut,
    PDItem,
    PrivacySetting,
    Rule,
)

TITLE_LENGTH = 30  # 채팅 제목은 첫 질문의 앞 30자


def now_iso() -> str:
    """시간대를 포함한 ISO 8601 시각."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class ChatRecord:
    chat_id: int
    title: str = ""
    updated_at: float = 0.0  # 목록 정렬용 (최근 수정 순)
    message_ids: list[int] = field(default_factory=list)
    run_ids: list[int] = field(default_factory=list)


@dataclass
class RunRecord:
    run_id: int
    chat_id: int
    user_message_id: int
    status: Literal["running", "finished"] = "running"
    answer_message_id: int | None = None
    end_reason: str | None = None
    notice: str | None = None
    error_code: str | None = None
    frontier_queries: list[FrontierQueryOut] = field(default_factory=list)
    consents: list[ConsentOut] = field(default_factory=list)
    events: RunEvents = field(default_factory=RunEvents)
    # 실행 중에만 쓰는 값
    task: asyncio.Task[None] | None = None
    consent_waiter: asyncio.Future[list[Decision]] | None = None

    def pending_consent(self) -> ConsentOut | None:
        """응답을 기다리는 동의 요청."""
        for consent in self.consents:
            if consent.result is None:
                return consent
        return None


class Store:
    def __init__(self) -> None:
        self.chats: dict[int, ChatRecord] = {}
        self.messages: dict[int, MessageOut] = {}
        self.runs: dict[int, RunRecord] = {}
        self.setting = PrivacySetting(
            setting_id=None, text="", rules=[], updated_at=None
        )
        self._seq: dict[str, int] = {}
        self._clock = 0.0

    def _next(self, kind: str) -> int:
        self._seq[kind] = self._seq.get(kind, 0) + 1
        return self._seq[kind]

    def _touch(self, chat: ChatRecord) -> None:
        # 같은 초에 여러 번 바뀌어도 순서가 유지되도록 단조 증가하는 값을 쓴다
        self._clock += 1
        chat.updated_at = self._clock

    # 채팅

    def create_chat(self) -> ChatRecord:
        chat = ChatRecord(chat_id=self._next("chat"))
        self._touch(chat)
        self.chats[chat.chat_id] = chat
        return chat

    def list_chats(self) -> list[ChatRecord]:
        return sorted(self.chats.values(), key=lambda c: c.updated_at, reverse=True)

    def delete_chat(self, chat_id: int) -> None:
        chat = self.chats.pop(chat_id)
        for message_id in chat.message_ids:
            self.messages.pop(message_id, None)
        for run_id in chat.run_ids:
            self.runs.pop(run_id, None)

    def is_running(self, chat: ChatRecord) -> bool:
        return any(self.runs[r].status == "running" for r in chat.run_ids)

    # 메시지 · 실행

    def save_message(
        self, chat_id: int, role: Literal["user", "assistant"], content: str
    ) -> MessageOut:
        chat = self.chats[chat_id]
        message = MessageOut(
            message_id=self._next("message"),
            role=role,
            content=content,
            created_at=now_iso(),
        )
        self.messages[message.message_id] = message
        chat.message_ids.append(message.message_id)
        if role == "user" and not chat.title:
            chat.title = content[:TITLE_LENGTH]
        self._touch(chat)
        return message

    def start_run(self, chat_id: int, user_message_id: int) -> RunRecord:
        run = RunRecord(
            run_id=self._next("run"), chat_id=chat_id, user_message_id=user_message_id
        )
        self.runs[run.run_id] = run
        self.chats[chat_id].run_ids.append(run.run_id)
        return run

    def add_frontier_query(
        self, run: RunRecord, round_: int, request: str, answer: str | None
    ) -> FrontierQueryOut:
        query = FrontierQueryOut(
            query_id=self._next("query"),
            round=round_,
            request=request,
            answer=answer,
            status="ok" if answer is not None else "failed",
        )
        run.frontier_queries.append(query)
        return query

    def add_consent(
        self, run: RunRecord, items: list[PDItem], purpose: str, benefit: str, risk: str
    ) -> ConsentOut:
        consent = ConsentOut(
            consent_id=self._next("consent"),
            items=items,
            purpose=purpose,
            benefit=benefit,
            risk=risk,
            result=None,
            response_text=None,
            decisions=[],
            created_at=now_iso(),
            responded_at=None,
        )
        run.consents.append(consent)
        return consent

    def next_pd_id(self) -> int:
        return self._next("pd")

    # 설정

    def save_setting(self, text: str, rules: list[Rule]) -> PrivacySetting:
        self.setting = PrivacySetting(
            setting_id=self._next("setting"),
            text=text,
            rules=rules,
            updated_at=now_iso(),
        )
        return self.setting

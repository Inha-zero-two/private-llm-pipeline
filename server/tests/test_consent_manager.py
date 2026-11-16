"""ConsentManager 단위 테스트. 실제 DB·모델 없이 저장소 대역과 스크립트 Runner 사용."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.consent_results import ConsentDecision, ConsentMessage, ConsentResult
from app.models.enums import PromptIdx
from app.models.llm_results import ConsentText
from app.privacy.consent_manager import (
    UNCLEAR_MESSAGE,
    ConsentManager,
    NoPendingConsentError,
)
from app.privacy.pd_detector import PDItem

RUN_ID = 5
ALLOW_ALL = "모두 공개해도 돼"
DENY_ALL = "모두 공개하지 마"


class FakeStore:
    """#34 ChatManager의 동의 저장 인터페이스 대역."""

    def __init__(self) -> None:
        self.saved: list[tuple[Any, ...]] = []
        self.updates: list[tuple[Any, ...]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    def save_consent(
        self, run_id: Any, items: Any, purpose: str, benefit: str, risk: str
    ) -> SimpleNamespace:
        self.saved.append((run_id, items, purpose, benefit, risk))
        return SimpleNamespace(consent_id=len(self.saved))

    def update_consent(
        self,
        run_id: Any,
        consent_id: int,
        decisions: list[ConsentDecision],
        response_text: str | None = None,
        *,
        cancelled: bool = False,
    ) -> None:
        self.updates.append((run_id, consent_id, decisions, response_text, cancelled))

    def get_pending_consent(self, run_id: Any) -> None:
        return None

    def log_event(self, run_id: Any, type_: str, payload: Any) -> int:
        self.events.append((type_, payload))
        return len(self.events)


class ScriptedRunner:
    """미리 정한 응답을 순서대로 돌려주는 Runner. 예외를 넣으면 그대로 발생한다."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[PromptIdx] = []

    def request(
        self,
        run_id: Any,
        input_: str,
        prompt_idx: PromptIdx,
        chat_id: Any,
        use_context: bool = True,
    ) -> tuple[Any, ...]:
        self.calls.append(prompt_idx)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def ok(result: Any) -> tuple[Any, ...]:
    return result, True, 1, 1, "stop"


def failed(reason: str) -> tuple[Any, ...]:
    return None, False, None, None, reason


def table() -> list[PDItem]:
    return [
        PDItem(pd_id=11, text="김민수", category=0),
        PDItem(pd_id=12, text="인하대 컴공 3학년", category=2),
        PDItem(pd_id=13, text="삼성전자 인턴", category=2),
    ]


def make(*responses: Any) -> tuple[ConsentManager, FakeStore, ScriptedRunner]:
    store = FakeStore()
    runner = ScriptedRunner(*responses)
    manager = ConsentManager(store, runner)  # type: ignore[arg-type]
    return manager, store, runner


def message(manager: ConsentManager, pd_table: list[PDItem]) -> ConsentMessage:
    """CONSENT 문구 모델이 실패해 고정 안내로 만든 12·13번 동의 요청."""
    built = manager.build_message(RUN_ID, [12, 13], ["인턴 준비"], pd_table)
    assert built is not None
    return built


async def wait_for_request(store: FakeStore) -> None:
    """request()가 요청을 저장·발행할 때까지 기다린다."""
    for _ in range(200):
        if any(event == "consent_request" for event, _ in store.events):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("동의 요청이 발행되지 않았습니다")


async def ask_and_answer(
    manager: ConsentManager, store: FakeStore, msg: ConsentMessage, *answers: str
) -> tuple[ConsentResult, list[Any]]:
    """동의를 요청하고 answers를 차례로 보낸 뒤 결과와 응답 목록을 돌려준다."""
    task = asyncio.create_task(manager.request(msg))
    await wait_for_request(store)
    replies = [await manager.receive(RUN_ID, 1, text) for text in answers]
    return await task, replies


# 동의 요청 만들기 (build_message)


def test_build_message_skips_allowed_denied_and_unrequested() -> None:
    manager, _, _ = make(failed("format_failed"))
    pd_table = table()
    pd_table[1].allowed, pd_table[1].allowed_by = True, "setting"
    pd_table[2].allowed_by = "denied"  # 이번 실행에서 거절한 항목
    assert manager.build_message(RUN_ID, [12, 13], [], pd_table) is None


def test_build_message_uses_model_text() -> None:
    text = ConsentText(purpose="목적", benefit="이점", risk="위험")
    manager, _, runner = make(ok(text))
    msg = manager.build_message(RUN_ID, [12, 13], ["인턴 준비"], table())
    assert msg is not None
    assert [i.pd_id for i in msg.items] == [12, 13]  # 11번은 요청하지 않음
    assert (msg.purpose, msg.benefit, msg.risk) == ("목적", "이점", "위험")
    assert runner.calls == [PromptIdx.CONSENT]


@pytest.mark.parametrize(
    "response",
    [
        failed("format_failed"),
        ok(ConsentText(purpose=" ", benefit="이점", risk="위험")),
        ConnectionError("Runner 종료"),
    ],
)
def test_build_message_falls_back_to_fixed_text(response: Any) -> None:
    manager, _, _ = make(response)
    msg = manager.build_message(RUN_ID, [12], [], table())
    assert msg is not None
    assert msg.purpose.strip() and msg.benefit.strip() and msg.risk.strip()


# 요청과 응답 (request, receive)


def test_quick_answer_decides_all_without_model() -> None:
    async def scenario() -> None:
        manager, store, runner = make(failed("format_failed"))
        msg = message(manager, table())
        result, (reply,) = await ask_and_answer(manager, store, msg, ALLOW_ALL)
        assert [(d.pd_id, d.allow) for d in result.decisions] == [
            (12, True),
            (13, True),
        ]
        assert reply.ok is True
        assert runner.calls == [PromptIdx.CONSENT]  # 해석 모델은 부르지 않음
        assert store.updates == [(RUN_ID, 1, result.decisions, ALLOW_ALL, False)]
        assert [event for event, _ in store.events] == [
            "consent_request",
            "consent_result",
        ]

    asyncio.run(scenario())


def test_natural_answer_unmentioned_item_is_denied() -> None:
    async def scenario() -> None:
        interpreted = [ConsentDecision(pd_id=12, allow=True)]  # 13번은 언급 없음
        manager, store, runner = make(failed("format_failed"), ok(interpreted))
        msg = message(manager, table())
        text = "학교는 공개해도 돼"
        result, (reply,) = await ask_and_answer(manager, store, msg, text)
        assert [(d.pd_id, d.allow) for d in result.decisions] == [
            (12, True),
            (13, False),
        ]
        assert reply.message == "공개: 인하대 컴공 3학년 · 비공개: 삼성전자 인턴"
        assert runner.calls[-1] == PromptIdx.INTERPRET_CONSENT

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "interpreted",
    [
        ok([]),  # 결정을 하나도 고르지 못함
        ok([ConsentDecision(pd_id=99, allow=True)]),  # 묻지 않은 항목
        ok([ConsentDecision(pd_id=12, allow=True)] * 2),  # 중복
        failed("format_failed"),
    ],
)
def test_unclear_answer_asks_again_and_keeps_waiting(interpreted: Any) -> None:
    async def scenario() -> None:
        manager, store, _ = make(failed("format_failed"), interpreted)
        msg = message(manager, table())
        result, (first, second) = await ask_and_answer(
            manager, store, msg, "글쎄요", DENY_ALL
        )
        assert first.ok is False
        assert first.message == UNCLEAR_MESSAGE
        assert second.ok is True  # 첫 응답 뒤에도 같은 요청이 계속 대기
        assert not any(d.allow for d in result.decisions)
        assert len(store.updates) == 1  # 해석 실패는 저장하지 않음

    asyncio.run(scenario())


def test_model_unavailable_raises_and_keeps_waiting() -> None:
    async def scenario() -> None:
        manager, store, _ = make(failed("format_failed"), failed("unavailable"))
        msg = message(manager, table())
        task = asyncio.create_task(manager.request(msg))
        await wait_for_request(store)
        with pytest.raises(ConnectionError):
            await manager.receive(RUN_ID, 1, "학교는 공개해도 돼")
        await manager.receive(RUN_ID, 1, ALLOW_ALL)
        assert all(d.allow for d in (await task).decisions)

    asyncio.run(scenario())


def test_receive_without_matching_request_is_rejected() -> None:
    async def scenario() -> None:
        manager, store, _ = make(failed("format_failed"))
        with pytest.raises(NoPendingConsentError):
            await manager.receive(RUN_ID, 1, ALLOW_ALL)  # 대기 중인 요청 없음
        msg = message(manager, table())
        task = asyncio.create_task(manager.request(msg))
        await wait_for_request(store)
        with pytest.raises(NoPendingConsentError):
            await manager.receive(RUN_ID, 2, ALLOW_ALL)  # 다른 요청 ID
        with pytest.raises(ValueError):
            await manager.receive(RUN_ID, 1, "  ")  # 빈 응답
        await manager.receive(RUN_ID, 1, ALLOW_ALL)
        await task

    asyncio.run(scenario())


def test_second_request_for_same_run_is_rejected() -> None:
    async def scenario() -> None:
        manager, store, _ = make(failed("format_failed"), failed("format_failed"))
        pd_table = table()
        task = asyncio.create_task(manager.request(message(manager, pd_table)))
        await wait_for_request(store)
        with pytest.raises(NoPendingConsentError):
            await manager.request(message(manager, pd_table))
        await manager.receive(RUN_ID, 1, DENY_ALL)
        await task

    asyncio.run(scenario())


# 중지 (cancel)


def test_cancel_ends_waiting_request() -> None:
    async def scenario() -> None:
        manager, store, _ = make(failed("format_failed"))
        task = asyncio.create_task(manager.request(message(manager, table())))
        await wait_for_request(store)
        await manager.cancel(RUN_ID)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.updates == [(RUN_ID, 1, [], None, True)]
        assert store.events[-1] == (
            "consent_result",
            {"consentId": 1, "result": "cancelled"},
        )
        with pytest.raises(NoPendingConsentError):
            await manager.receive(RUN_ID, 1, ALLOW_ALL)  # 취소 뒤 응답은 거부

    asyncio.run(scenario())


# 실행 테이블 반영과 복구 (apply, release)


def test_apply_and_release_restore_original_state() -> None:
    async def scenario() -> None:
        interpreted = [
            ConsentDecision(pd_id=12, allow=True),
            ConsentDecision(pd_id=13, allow=False),
        ]
        manager, store, _ = make(failed("format_failed"), ok(interpreted))
        pd_table = table()
        msg = message(manager, pd_table)
        result, _ = await ask_and_answer(
            manager, store, msg, "학교는 되고 회사는 안 돼"
        )
        manager.apply(result, msg, pd_table)
        assert [(i.allowed, i.allowed_by) for i in pd_table] == [
            (False, "none"),
            (True, "consent"),
            (False, "denied"),  # 같은 실행에서 다시 묻지 않음
        ]
        await manager.release(RUN_ID, pd_table)
        assert [(i.allowed, i.allowed_by) for i in pd_table] == [(False, "none")] * 3

    asyncio.run(scenario())


def test_apply_rejects_result_not_from_user_answer() -> None:
    manager, _, _ = make(failed("format_failed"))
    pd_table = table()
    msg = message(manager, pd_table)
    msg.consent_id = 1
    forged = ConsentResult(
        run_id=RUN_ID,
        consent_id=1,
        decisions=[
            ConsentDecision(pd_id=12, allow=True),
            ConsentDecision(pd_id=13, allow=True),
        ],
    )
    with pytest.raises(ValueError):
        manager.apply(forged, msg, pd_table)
    assert not any(i.allowed for i in pd_table)

"""Main 흐름 테스트. 다른 모듈은 아직 없으므로 정해진 값을 돌려주는 가짜 모듈을 넣는다."""

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from app.main import STOPPED_MARK, Main
from app.models.enums import EndReason


class Recorder:
    """불린 함수와 인자를 순서대로 기록하는 가짜 모듈의 공통 부모."""

    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        self.log = log

    def record(self, *call: Any) -> None:
        self.log.append(call)


class FakeChatManager(Recorder):
    def save_message(self, chat_id: str, role: str, content: str) -> str:
        self.record("save_message", role, content)
        return f"msg-{role}"

    def start_run(
        self, chat_id: str, message_id: str, setting_id: int, config: dict[str, Any]
    ) -> str:
        self.record("start_run", chat_id)
        return "run-1"

    def set_pd_items(self, chat_id: str, message_id: str, items: list[Any]) -> None:
        self.record("set_pd_items")

    def get_pd_table(self, chat_id: str) -> list[Any]:
        return []

    def finish_run(
        self,
        run_id: str,
        end_reason: EndReason | None,
        answer_message_id: str | None,
        memo_final: str,
    ) -> None:
        self.record("finish_run", end_reason, answer_message_id)


class FakeMemoManager(Recorder):
    def start(self, run_id: str, question: str) -> None:
        self.answers: list[str] = []

    def set_needs(self, needs: list[str]) -> None:
        pass

    def add_answer(self, answer: str) -> None:
        self.answers.append(answer)

    def read_all(self) -> str:
        return " ".join(self.answers)

    def clear(self) -> None:
        self.record("memo_clear")


class FakePlanner(Recorder):
    """답변을 enough_after개 모으면 충분하다고 판단한다."""

    def __init__(self, log: list[tuple[Any, ...]], enough_after: int = 0) -> None:
        super().__init__(log)
        self.enough_after = enough_after
        self.need_consent = False

    def list_needs(self, question: str) -> list[str]:
        return ["경력"]

    def judge(self, question: str, memo: str, pd_table: list[Any]) -> SimpleNamespace:
        answers = len(memo.split()) if memo else 0
        return SimpleNamespace(
            enough=answers >= self.enough_after,
            need=["경력"],
            need_consent=self.need_consent,
            consent_items=[1],
        )


class FakeRequestWriter(Recorder):
    def write(
        self, question: str, memo: str, pd_table: list[Any], need: list[str]
    ) -> str:
        self.record("write")
        return "요청문"

    def rewrite(
        self, previous: str, hits: list[Any], need: list[str], pd_table: list[Any]
    ) -> str:
        self.record("rewrite", hits)
        return "고친 요청문"


class FakeLeakGuard(Recorder):
    def __init__(
        self, log: list[tuple[Any, ...]], hits: list[Any] | None = None
    ) -> None:
        super().__init__(log)
        self.hits = hits or []
        self.passed = not self.hits

    def detect(
        self, run_id: str, request_text: str, pd_table: list[Any]
    ) -> SimpleNamespace:
        return SimpleNamespace(passed=self.passed, hits=self.hits)


class FakeFrontierClient(Recorder):
    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        super().__init__(log)
        self.online = True
        self.fail_send = False

    def check_connection(self) -> bool:
        return self.online

    def send(self, prompt: str) -> SimpleNamespace:
        self.record("send", prompt)
        if self.fail_send:
            raise ConnectionError("API 연결 실패")
        return SimpleNamespace(answer="답", cost=0.1)


class FakeConsentManager(Recorder):
    def __init__(self, log: list[tuple[Any, ...]], allow: bool = True) -> None:
        super().__init__(log)
        self.allow = allow
        self.wait_forever = False

    def build_message(
        self, run_id: str, items: list[Any], need: list[str], pd_table: list[Any]
    ) -> SimpleNamespace:
        return SimpleNamespace(run_id=run_id)

    async def request(self, req: SimpleNamespace) -> SimpleNamespace:
        self.record("consent_request")
        if self.wait_forever:
            await asyncio.Event().wait()
        return SimpleNamespace(run_id=req.run_id, allow=self.allow)

    def apply(
        self, result: SimpleNamespace, req: SimpleNamespace, pd_table: list[Any]
    ) -> None:
        self.record("consent_apply")

    def release(self, run_id: str, pd_table: list[Any]) -> None:
        self.record("consent_release")

    def cancel(self, run_id: str) -> None:
        self.record("consent_cancel")


class FakeAnswerGenerator(Recorder):
    def generate(self, question: str, memo: str) -> Iterator[str]:
        self.record("generate")
        return iter(["최종", " 답변"])

    def notify_limited(self, end_reason: EndReason) -> None:
        self.record("notify_limited", end_reason)


class FakeSimple(Recorder):
    """Main이 부르기만 하는 모듈들(PDDetector · PrivacySettings · LocalLLMRunner · PromptManager)."""

    setting_id = 1

    def detect(
        self, run_id: str, chat_id: str, message_id: str, question: str
    ) -> list[Any]:
        return []

    def apply_to_table(self, run_id: str, pd_table: list[Any]) -> None:
        pass

    def load_rules(self) -> None:
        self.record("load_rules")

    def load_prompts(self) -> bool:
        self.record("load_prompts")
        return True

    def model_load(self) -> bool:
        self.record("model_load")
        return True

    def stop(self) -> None:
        self.record("local_stop")


def make_main(
    log: list[tuple[Any, ...]],
    *,
    regen_limit: int = 3,
    call_limit: int = 5,
    **fakes: Any,
) -> Main:
    simple = FakeSimple(log)
    modules: dict[str, Any] = {
        "local_llm_runner": simple,
        "frontier_client": FakeFrontierClient(log),
        "chat_manager": FakeChatManager(log),
        "memo_manager": FakeMemoManager(log),
        "prompt_manager": simple,
        "pd_detector": simple,
        "privacy_settings": simple,
        "consent_manager": FakeConsentManager(log),
        "planner": FakePlanner(log),
        "request_writer": FakeRequestWriter(log),
        "leak_guard": FakeLeakGuard(log),
        "answer_generator": FakeAnswerGenerator(log),
    }
    modules.update(fakes)
    return Main(
        call_limit=call_limit, regen_limit=regen_limit, budget_limit=1.0, **modules
    )


def finished_with(log: list[tuple[Any, ...]]) -> EndReason | None:
    return next(call[1] for call in log if call[0] == "finish_run")


def names(log: list[tuple[Any, ...]]) -> list[str]:
    return [call[0] for call in log]


@pytest.mark.parametrize(
    ("online", "calls", "cost", "expected"),
    [
        (True, 0, 0.0, None),
        (False, 0, 0.0, EndReason.OFFLINE),
        (True, 5, 0.0, EndReason.CALL_LIMIT),
        (True, 0, 1.5, EndReason.BUDGET_LIMIT),
        (True, 0, 1.0, None),  # 비용은 한도를 '초과'해야 끝난다
        # 여러 조건이 겹치면 offline → call_limit → budget_limit 순
        (False, 5, 1.5, EndReason.OFFLINE),
        (True, 5, 1.5, EndReason.CALL_LIMIT),
    ],
)
def test_check_end(
    online: bool, calls: int, cost: float, expected: EndReason | None
) -> None:
    main = make_main([])
    assert main.check_end(online, calls, 5, cost, 1.0) == expected


def test_init_loads_model_prompts_and_rules() -> None:
    log: list[tuple[Any, ...]] = []
    asyncio.run(make_main(log).init())
    assert names(log) == ["model_load", "load_prompts", "load_rules"]


def test_enough_from_start_answers_without_frontier() -> None:
    log: list[tuple[Any, ...]] = []
    asyncio.run(make_main(log).run_pipeline("chat-1", "질문"))

    assert "send" not in names(log)
    assert ("save_message", "assistant", "최종 답변") in log
    assert "notify_limited" not in names(log)
    assert finished_with(log) == EndReason.INFO_ENOUGH
    # 정리는 release → clear → finish_run 순서
    tail = names(log)[-3:]
    assert tail == ["consent_release", "memo_clear", "finish_run"]


def test_one_round_trip_then_enough() -> None:
    log: list[tuple[Any, ...]] = []
    main = make_main(log, planner=FakePlanner(log, enough_after=1))
    asyncio.run(main.run_pipeline("chat-1", "질문"))

    assert names(log).count("send") == 1
    assert finished_with(log) == EndReason.INFO_ENOUGH


def test_call_limit_stops_round_trips() -> None:
    log: list[tuple[Any, ...]] = []
    main = make_main(log, call_limit=2, planner=FakePlanner(log, enough_after=99))
    asyncio.run(main.run_pipeline("chat-1", "질문"))

    assert names(log).count("send") == 2
    assert finished_with(log) == EndReason.CALL_LIMIT
    assert ("notify_limited", EndReason.CALL_LIMIT) in log


def test_leak_guard_failures_end_with_regen_limit() -> None:
    log: list[tuple[Any, ...]] = []
    main = make_main(
        log,
        regen_limit=2,
        planner=FakePlanner(log, enough_after=99),
        leak_guard=FakeLeakGuard(log, hits=["pd-1"]),
    )
    asyncio.run(main.run_pipeline("chat-1", "질문"))

    assert names(log).count("rewrite") == 2
    assert "send" not in names(log)  # 통과하지 못한 요청문은 보내지 않는다
    assert finished_with(log) == EndReason.REGEN_LIMIT


def test_guard_format_failure_does_not_rewrite() -> None:
    log: list[tuple[Any, ...]] = []
    guard = FakeLeakGuard(log)
    guard.passed = False  # hits 없이 실패 = 검사 모델이 형식을 못 맞춤
    main = make_main(log, planner=FakePlanner(log, enough_after=99), leak_guard=guard)
    asyncio.run(main.run_pipeline("chat-1", "질문"))

    assert "rewrite" not in names(log)
    assert finished_with(log) == EndReason.REGEN_LIMIT


def test_consent_denied() -> None:
    log: list[tuple[Any, ...]] = []
    planner = FakePlanner(log, enough_after=99)
    planner.need_consent = True
    main = make_main(
        log, planner=planner, consent_manager=FakeConsentManager(log, allow=False)
    )
    asyncio.run(main.run_pipeline("chat-1", "질문"))

    assert "consent_apply" not in names(log)
    assert "send" not in names(log)
    assert finished_with(log) == EndReason.CONSENT_DENIED


def test_send_failure_ends_offline() -> None:
    log: list[tuple[Any, ...]] = []
    frontier = FakeFrontierClient(log)
    frontier.fail_send = True
    main = make_main(
        log, planner=FakePlanner(log, enough_after=99), frontier_client=frontier
    )
    asyncio.run(main.run_pipeline("chat-1", "질문"))

    assert finished_with(log) == EndReason.OFFLINE
    assert "generate" in names(log)  # 연결이 끊겨도 로컬 모델로 답한다


def test_stop_while_waiting_for_consent() -> None:
    log: list[tuple[Any, ...]] = []
    planner = FakePlanner(log, enough_after=99)
    planner.need_consent = True
    consent = FakeConsentManager(log)
    consent.wait_forever = True
    main = make_main(log, planner=planner, consent_manager=consent)

    async def scenario() -> None:
        task = asyncio.create_task(main.run_pipeline("chat-1", "질문"))
        while "consent_request" not in names(log):
            await asyncio.sleep(0.01)
        await main.stop("run-1")
        assert task.cancelled()

    asyncio.run(scenario())

    assert "consent_cancel" in names(log)
    assert "generate" not in names(log)  # 1)~6) 도중 중지면 7)을 하지 않는다
    assert finished_with(log) == EndReason.STOPPED
    assert "consent_release" in names(log)


def test_stop_during_answer_keeps_partial_answer() -> None:
    log: list[tuple[Any, ...]] = []
    main = make_main(log)

    async def scenario() -> None:
        first_token = asyncio.Event()

        async def emit(event: str, data: dict[str, Any]) -> None:
            if event == "token":
                first_token.set()
                await asyncio.sleep(10)  # 다음 토큰 전에 멈춰서 중지를 기다린다

        main._emit = emit
        task = asyncio.create_task(main.run_pipeline("chat-1", "질문"))
        await first_token.wait()
        await main.stop("run-1")
        assert task.cancelled()

    asyncio.run(scenario())

    assert ("save_message", "assistant", "최종" + STOPPED_MARK) in log
    assert finished_with(log) == EndReason.STOPPED

"""Main 흐름 테스트. 다른 모듈은 아직 없으므로 정해진 값을 돌려주는 가짜 모듈을 넣는다."""

import asyncio
import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app.data.memo_manager import MemoManager
from app.main import STOPPED_MARK, Main
from app.models.enums import EndReason
from app.models.errors import ErrorCode, ErrorStage, PipelineError
from app.models.schemas import PrivacySetting, RunConfig, RunEvent, StartedRun


class Recorder:
    """불린 함수와 인자를 순서대로 기록하는 가짜 모듈의 공통 부모."""

    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        self.log = log

    def record(self, *call: Any) -> None:
        self.log.append(call)


class FakeChatManager(Recorder):
    # 저장소 상세 동작은 외부 ChatManager 테스트에서 검사한다.
    # 이 대역은 Main의 새 저장 인터페이스만 제공한다.
    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        super().__init__(log)
        self.event_id = 0
        # 프론티어 질의 기록: query_id → 마지막 상태 (전송 직전 uncertain)
        self.queries: dict[int, str] = {}
        self.notice: str | None = None

    def recover_interrupted_runs(self) -> int:
        return 0

    def load_run_setting(self, run_id: int) -> PrivacySetting:
        return PrivacySetting()

    def log_event(self, *args: Any) -> int:
        self.event_id += 1
        return self.event_id

    def save_frontier_query(self, run_id: int, query: Any, items: Any) -> int:
        query_id = len(self.queries) + 1
        self.queries[query_id] = query.status
        return query_id

    def finish_frontier_query(self, run_id: int, query_id: int, query: Any) -> None:
        self.queries[query_id] = query.status

    def record_regeneration(self, *args: Any) -> None:
        pass

    def save_message(self, chat_id: int, role: str, content: str) -> int:
        self.record("save_message", role, content)
        return 1 if role == "user" else 2

    def start_run(
        self, chat_id: int, message_id: int, setting_id: int | None, config: RunConfig
    ) -> int:
        self.record("start_run", chat_id)
        return 1

    def start_turn(
        self,
        chat_id: int,
        question: str,
        setting_id: int | None,
        config: RunConfig,
    ) -> StartedRun:
        message_id = self.save_message(chat_id, "user", question)
        run_id = self.start_run(chat_id, message_id, setting_id, config)
        return StartedRun(user_message_id=message_id, run_id=run_id)

    def set_pd_items(self, chat_id: int, message_id: int, items: list[Any]) -> None:
        self.record("set_pd_items")

    def get_pd_table(self, chat_id: int) -> list[Any]:
        return []

    def finish_run(
        self,
        run_id: int,
        end_reason: EndReason | None,
        answer_message_id: int | None,
        memo_final: str,
        *,
        error_code: str | None = None,
        notice: str | None = None,
    ) -> RunEvent:
        self.record("finish_run", end_reason, answer_message_id)
        self.notice = notice
        self.event_id += 1
        return RunEvent(
            event_id=self.event_id,
            run_id=run_id,
            type="error" if end_reason is None else "done",
            payload=(
                {"code": "INTERNAL_ERROR", "message": "실행 오류"}
                if end_reason is None
                else {
                    "endReason": end_reason,
                    "limited": None,
                    "answerMessageId": answer_message_id,
                }
            ),
            created_at="2026-11-12T00:00:00+00:00",
        )


class FakeMemoManager(Recorder):
    def start(self, run_id: int, question: str) -> None:
        self.answers: list[str] = []

    def set_needs(self, needs: list[str]) -> None:
        pass

    def add_answer(self, needs: list[str], answer: str) -> None:
        self.record("add_answer", list(needs), answer)
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

    def list_needs(self, question: str, run_id: int, chat_id: int) -> list[str]:
        self.record("list_needs", run_id, chat_id)
        return ["경력"]

    def judge(
        self,
        question: str,
        memo: str,
        pd_table: list[Any],
        run_id: int,
        chat_id: int,
        needs: list[str],
    ) -> SimpleNamespace:
        """실행·채팅 ID와 등록된 need를 기록하고 메모의 답변 수로 충분한지 판단한다."""
        self.record("judge", run_id, chat_id)
        self.record("judge_needs", list(needs))
        answers = len(memo.split()) if memo else 0
        return SimpleNamespace(
            enough=answers >= self.enough_after,
            need=["경력"],
            need_consent=self.need_consent,
            consent_items=[1],
        )


class FakeRequestWriter(Recorder):
    def write(
        self,
        question: str,
        memo: str,
        pd_table: list[Any],
        need: list[str],
        run_id: int | None = None,
        chat_id: int | None = None,
    ) -> str:
        self.record("write")
        return "요청문"

    def rewrite(
        self,
        previous: str,
        hits: list[Any],
        need: list[str],
        pd_table: list[Any],
        run_id: int | None = None,
        chat_id: int | None = None,
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
        self, run_id: int, request_text: str, pd_table: list[Any]
    ) -> SimpleNamespace:
        return SimpleNamespace(passed=self.passed, hits=self.hits)


class FakeFrontierClient(Recorder):
    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        super().__init__(log)
        self.online = True
        self.fail_send = False
        # stop·clear 호출과 run_id. 정리 순서를 보는 log와 섞지 않으려고 따로 기록
        self.calls: list[tuple[str, str]] = []
        self.sent_needs: list[list[str] | None] = []  # send()가 받은 need
        # 설정하면 send()가 stop()이 불릴 때까지 멈춘다. 전송 중 중지를 흉내 내기 위함
        self.block_send = False
        # 중지로 끊긴 send()가 낼 예외. 실제 FrontierClient는 FrontierCancelledError
        self.stop_error: Exception = ConnectionError("중지로 연결 끊김")
        self.sending = threading.Event()
        self._stopped = threading.Event()

    def check_connection(self) -> bool:
        return self.online

    def send(
        self,
        run_id: int,
        prompt: str,
        pd_table: list[Any] | None = None,
        need: list[str] | None = None,
    ) -> SimpleNamespace:
        self.record("send", prompt)
        self.sent_needs.append(need)
        if self.block_send:
            self.sending.set()
            self._stopped.wait(5)
            raise self.stop_error
        if self.fail_send:
            raise ConnectionError("API 연결 실패")
        return SimpleNamespace(
            answer="답",
            cost=0.1,
            input_tokens=3,
            output_tokens=2,
            elapsed_ms=5,
            context_mode="reset",
        )

    def stop(self, run_id: int) -> None:
        self.calls.append(("stop", run_id))
        self._stopped.set()

    def clear(self, run_id: int) -> None:
        self.calls.append(("clear", run_id))


class FakeConsentManager(Recorder):
    def __init__(self, log: list[tuple[Any, ...]], allow: bool = True) -> None:
        super().__init__(log)
        self.allow = allow
        self.wait_forever = False

    def build_message(
        self, run_id: int, items: list[Any], need: list[str], pd_table: list[Any]
    ) -> SimpleNamespace:
        return SimpleNamespace(run_id=run_id)

    async def request(self, req: SimpleNamespace) -> SimpleNamespace:
        self.record("consent_request")
        if self.wait_forever:
            await asyncio.Event().wait()
        return SimpleNamespace(
            run_id=req.run_id, decisions=[SimpleNamespace(pd_id=1, allow=self.allow)]
        )

    def apply(
        self, result: SimpleNamespace, req: SimpleNamespace, pd_table: list[Any]
    ) -> None:
        self.record("consent_apply")

    def release(self, run_id: int, pd_table: list[Any]) -> None:
        self.record("consent_release")

    def cancel(self, run_id: int) -> None:
        self.record("consent_cancel")


class FakeAnswerGenerator(Recorder):
    def generate(
        self,
        question: str,
        memo: str,
        run_id: int | None = None,
        chat_id: int | None = None,
    ) -> Iterator[str]:
        self.record("generate")
        return iter(["최종", " 답변"])

    def notify_limited(self, end_reason: EndReason) -> str | None:
        self.record("notify_limited", end_reason)
        return None


class FakeSimple(Recorder):
    """Main이 부르기만 하는 모듈들(PDDetector · PrivacySettings · LocalLLMRunner · PromptManager)."""

    setting_id = 1

    def detect(
        self,
        run_id: int,
        chat_id: int,
        message_id: int,
        question: str,
        setting: PrivacySetting,
    ) -> list[Any]:
        return []

    def apply_to_table(
        self, run_id: int, pd_table: list[Any], setting: PrivacySetting
    ) -> None:
        pass

    def load_rules(self) -> None:
        self.record("load_rules")

    def load_prompts(self) -> bool:
        self.record("load_prompts")
        return True

    def model_load(self) -> bool:
        self.record("model_load")
        return True

    def begin_turn_context(
        self, run_id: int, chat_id: int, user_message_id: int
    ) -> None:
        pass

    def end_turn_context(self, run_id: int) -> None:
        pass

    def stop(self, run_id: int) -> None:
        self.record("local_stop", run_id)


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
    asyncio.run(make_main(log).run_pipeline(1, "질문"))

    assert "send" not in names(log)
    assert ("save_message", "assistant", "최종 답변") in log
    assert "notify_limited" not in names(log)
    assert finished_with(log) == EndReason.INFO_ENOUGH
    assert ("list_needs", 1, 1) in log
    assert ("judge", 1, 1) in log
    # list_needs로 정한 need를 judge에 넘겨, 메모에 등록된 이름으로만 판단하게 한다
    assert ("judge_needs", ["경력"]) in log
    # 정리는 release → clear → finish_run 순서
    tail = names(log)[-3:]
    assert tail == ["consent_release", "memo_clear", "finish_run"]


def test_one_round_trip_then_enough() -> None:
    log: list[tuple[Any, ...]] = []
    frontier = FakeFrontierClient(log)
    main = make_main(
        log, planner=FakePlanner(log, enough_after=1), frontier_client=frontier
    )
    asyncio.run(main.run_pipeline(1, "질문"))

    assert names(log).count("send") == 1
    assert ("add_answer", ["경력"], "답") in log
    assert finished_with(log) == EndReason.INFO_ENOUGH
    # 컨텍스트 유지 판단에 쓰도록 판단 결과의 need를 함께 넘긴다
    assert frontier.sent_needs == [["경력"]]
    # 실행이 끝나면 그 실행의 프론티어 대화만 지우고, 중지는 부르지 않는다
    assert frontier.calls == [("clear", 1)]


class SlowCancelConsentManager(FakeConsentManager):
    """동의 취소가 늦게 끝난다. 끊긴 send()의 예외가 task.cancel()보다 먼저 오게 하려고."""

    def cancel(self, run_id: int) -> None:
        time.sleep(0.2)
        super().cancel(run_id)


@pytest.mark.parametrize(
    "stop_error",
    [ConnectionError("중지로 연결 끊김"), RuntimeError("전송 취소")],
    ids=["connection_error", "cancelled_error"],
)
def test_stop_during_frontier_send(stop_error: Exception) -> None:
    log: list[tuple[Any, ...]] = []
    events: list[str] = []
    frontier = FakeFrontierClient(log)
    frontier.block_send = True
    frontier.stop_error = stop_error
    chat = FakeChatManager(log)
    main = make_main(
        log,
        planner=FakePlanner(log, enough_after=99),
        frontier_client=frontier,
        consent_manager=SlowCancelConsentManager(log),
        chat_manager=chat,
    )

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append(event)

    main._emit = emit

    async def scenario() -> None:
        task = asyncio.create_task(main.run_pipeline(1, "질문"))
        async with asyncio.timeout(5):
            while not frontier.sending.is_set():
                assert not task.done(), "전송 전에 실행이 끝남"
                await asyncio.sleep(0.01)
        await main.stop(1)
        assert task.cancelled()

    asyncio.run(scenario())

    # 끊긴 전송의 예외가 먼저 와도 오류·offline이 아니라 중지로 끝난다
    assert finished_with(log) == EndReason.STOPPED
    assert "error" not in events
    assert "generate" not in names(log)
    # 같은 run_id로 전송을 끊은 뒤 정리한다
    assert frontier.calls == [("stop", 1), ("clear", 1)]
    # 중지로 끊긴 질의도 failed로 남긴다 (#45). 전송 직전에는 uncertain으로 기록됐다
    assert chat.queries == {1: "failed"}


def test_frontier_query_is_recorded_before_and_after_send() -> None:
    """전송 직전 uncertain으로 기록하고, 받은 응답의 토큰·비용·시간으로 ok를 남긴다."""
    log: list[tuple[Any, ...]] = []
    chat = FakeChatManager(log)
    statuses: list[str] = []
    original = chat.save_frontier_query

    def save(run_id: int, query: Any, items: Any) -> int:
        statuses.append(query.status)
        return original(run_id, query, items)

    finished: list[Any] = []
    chat.save_frontier_query = save  # type: ignore[method-assign]
    chat.finish_frontier_query = (  # type: ignore[method-assign]
        lambda run_id, query_id, query: finished.append(query)
    )
    main = make_main(log, planner=FakePlanner(log, enough_after=1), chat_manager=chat)
    asyncio.run(main.run_pipeline(1, "질문"))

    assert statuses == ["uncertain"]
    (query,) = finished
    assert (query.status, query.answer, query.cost) == ("ok", "답", 0.1)
    assert (query.input_tokens, query.output_tokens, query.elapsed_ms) == (3, 2, 5)
    assert query.context_mode == "reset"


def test_send_failure_is_recorded_as_failed_and_ends_offline() -> None:
    log: list[tuple[Any, ...]] = []
    chat = FakeChatManager(log)
    frontier = FakeFrontierClient(log)
    frontier.fail_send = True
    main = make_main(
        log,
        planner=FakePlanner(log, enough_after=99),
        frontier_client=frontier,
        chat_manager=chat,
    )
    asyncio.run(main.run_pipeline(1, "질문"))
    assert finished_with(log) == EndReason.OFFLINE
    assert chat.queries == {1: "failed"}


def test_limited_notice_is_saved_with_finish_run() -> None:
    """정상 종료가 아니면 AnswerGenerator의 제한 안내를 finish_run의 notice로 저장한다."""
    log: list[tuple[Any, ...]] = []
    chat = FakeChatManager(log)
    answer = FakeAnswerGenerator(log)
    answer.notify_limited = lambda end_reason: "제한 안내"  # type: ignore[method-assign]
    frontier = FakeFrontierClient(log)
    frontier.online = False
    main = make_main(
        log, frontier_client=frontier, chat_manager=chat, answer_generator=answer
    )
    asyncio.run(main.run_pipeline(1, "질문"))
    assert finished_with(log) == EndReason.OFFLINE
    assert chat.notice == "제한 안내"


@pytest.mark.parametrize("selected_needs", [["학력"], ["경력", "직무"]])
def test_round_trip_links_reply_to_selected_needs(selected_needs: list[str]) -> None:
    """실제 메모에 선택한 need만 연결하고 다음 판단과 최종 답변에 전달한다."""
    log: list[tuple[Any, ...]] = []
    needs = ["경력", "학력", "직무"]
    runner = Mock()
    runner.get_input_limit.return_value = 26_000
    runner.tokenize.side_effect = str.split
    prompt_manager = Mock()
    prompt_manager.get_prompt.return_value = "요약 프롬프트"
    memo_manager = Mock(wraps=MemoManager(runner, prompt_manager))
    planner = Mock(spec=FakePlanner)
    planner.list_needs.return_value = needs
    planner.judge.side_effect = [
        SimpleNamespace(enough=False, need=selected_needs, need_consent=False),
        SimpleNamespace(enough=True),
    ]
    answer_generator = Mock(wraps=FakeAnswerGenerator(log))
    main = make_main(
        log,
        memo_manager=memo_manager,
        planner=planner,
        answer_generator=answer_generator,
    )

    asyncio.run(main.run_pipeline(1, "질문"))

    memo_manager.add_answer.assert_called_once_with(selected_needs, "답")
    assert planner.judge.call_count == 2
    memo = planner.judge.call_args_list[1].args[1]
    expected_sections = [
        f"Need:\n{need}\n원문:\n" + ("[답변 1]\n답" if need in selected_needs else "")
        for need in needs
    ]
    assert memo == (
        "Question: 질문\nNeeds:\n- 경력\n- 학력\n- 직무\nAnswers:\n"
        + "\n\n".join(expected_sections)
    )
    answer_generator.generate.assert_called_once_with("질문", memo, 1, 1)
    runner.request.assert_not_called()
    assert names(log).count("send") == 1
    assert ("save_message", "assistant", "최종 답변") in log
    assert finished_with(log) == EndReason.INFO_ENOUGH


def test_call_limit_stops_round_trips() -> None:
    log: list[tuple[Any, ...]] = []
    main = make_main(log, call_limit=2, planner=FakePlanner(log, enough_after=99))
    asyncio.run(main.run_pipeline(1, "질문"))

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
    asyncio.run(main.run_pipeline(1, "질문"))

    assert names(log).count("rewrite") == 2
    assert "send" not in names(log)  # 통과하지 못한 요청문은 보내지 않는다
    assert finished_with(log) == EndReason.REGEN_LIMIT


def test_guard_format_failure_does_not_rewrite() -> None:
    log: list[tuple[Any, ...]] = []
    guard = FakeLeakGuard(log)
    guard.passed = False  # hits 없이 실패 = 검사 모델이 형식을 못 맞춤
    main = make_main(log, planner=FakePlanner(log, enough_after=99), leak_guard=guard)
    with pytest.raises(PipelineError) as caught:
        asyncio.run(main.run_pipeline(1, "질문"))
    assert (caught.value.code, caught.value.stage) == (
        ErrorCode.MODEL_RESPONSE_INVALID,
        ErrorStage.GUARD,
    )

    assert "rewrite" not in names(log)
    assert finished_with(log) is None


def test_deny_all_can_finish_when_replan_has_enough_information() -> None:
    log: list[tuple[Any, ...]] = []
    planner = Mock(wraps=FakePlanner(log))
    planner.judge.side_effect = [
        SimpleNamespace(
            enough=False, need=["경력"], need_consent=True, consent_items=[1]
        ),
        SimpleNamespace(enough=True),
    ]
    main = make_main(
        log, planner=planner, consent_manager=FakeConsentManager(log, allow=False)
    )
    asyncio.run(main.run_pipeline(1, "질문"))

    assert names(log).count("consent_apply") == 1
    assert planner.judge.call_count == 2
    assert "send" not in names(log)
    assert finished_with(log) == EndReason.INFO_ENOUGH


def test_send_failure_ends_offline() -> None:
    log: list[tuple[Any, ...]] = []
    frontier = FakeFrontierClient(log)
    frontier.fail_send = True
    main = make_main(
        log, planner=FakePlanner(log, enough_after=99), frontier_client=frontier
    )
    asyncio.run(main.run_pipeline(1, "질문"))

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
        task = asyncio.create_task(main.run_pipeline(1, "질문"))
        while "consent_request" not in names(log):
            await asyncio.sleep(0.01)
        await main.stop(1)
        assert task.cancelled()

    asyncio.run(scenario())

    assert "consent_cancel" in names(log)
    assert ("local_stop", 1) in log
    assert "generate" not in names(log)  # 1)~6) 도중 중지면 7)을 하지 않는다
    assert finished_with(log) == EndReason.STOPPED
    assert "consent_release" in names(log)


def test_stop_during_answer_keeps_partial_answer() -> None:
    log: list[tuple[Any, ...]] = []
    main = make_main(log)

    async def scenario() -> None:
        first_token = asyncio.Event()

        async def emit(event: str, data: dict[str, Any], event_id: int | None) -> None:
            if event == "token":
                first_token.set()
                await asyncio.sleep(10)  # 다음 토큰 전에 멈춰서 중지를 기다린다

        main._send_event = emit
        task = asyncio.create_task(main.run_pipeline(1, "질문"))
        await first_token.wait()
        await main.stop(1)
        assert task.cancelled()

    asyncio.run(scenario())

    assert ("save_message", "assistant", "최종" + STOPPED_MARK) in log
    assert finished_with(log) == EndReason.STOPPED


def test_unexpected_error_still_cleans_up() -> None:
    log: list[tuple[Any, ...]] = []
    planner = FakePlanner(log, enough_after=99)

    def boom(*args: Any) -> None:
        raise RuntimeError("boom")

    planner.judge = boom  # type: ignore[method-assign]
    main = make_main(log, planner=planner)
    events: list[str] = []

    async def emit(event: str, data: dict[str, Any], event_id: int | None) -> None:
        events.append(event)

    main._send_event = emit

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(main.run_pipeline(1, "질문"))

    assert events.count("error") == 1
    assert names(log)[-3:] == ["consent_release", "memo_clear", "finish_run"]
    assert main._runs == {}


class FailingReleaseConsentManager(FakeConsentManager):
    def release(self, run_id: int, pd_table: list[Any]) -> None:
        self.record("consent_release")
        raise RuntimeError("release 실패")


def test_cleanup_continues_after_release_failure() -> None:
    log: list[tuple[Any, ...]] = []
    main = make_main(log, consent_manager=FailingReleaseConsentManager(log))
    terminal_events: list[tuple[str, int | None]] = []

    async def emit(event: str, data: dict[str, Any], event_id: int | None) -> None:
        if event in {"done", "error"}:
            terminal_events.append((event, event_id))

    main._send_event = emit

    # 본 흐름은 성공했으므로 정리 오류가 호출자에게 올라간다.
    with pytest.raises(RuntimeError, match="release 실패"):
        asyncio.run(main.run_pipeline(1, "질문"))

    assert "memo_clear" in names(log)
    # 개인정보 복구 실패도 오류 종료로 기록한다.
    assert finished_with(log) is None
    assert main._runs == {}
    assert len(terminal_events) == 1
    assert terminal_events[0][0] == "error"
    assert terminal_events[0][1] is not None


class SlowReleaseConsentManager(FakeConsentManager):
    """release()가 go 신호를 받을 때까지 멈춰 있다. 정리 도중에 stop()을 부르기 위함."""

    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        super().__init__(log)
        self.go = threading.Event()

    def release(self, run_id: int, pd_table: list[Any]) -> None:
        self.record("consent_release")
        self.go.wait(timeout=5)


def test_stop_during_cleanup_does_not_interrupt() -> None:
    log: list[tuple[Any, ...]] = []
    consent = SlowReleaseConsentManager(log)
    main = make_main(log, consent_manager=consent)

    async def scenario() -> None:
        task = asyncio.create_task(main.run_pipeline(1, "질문"))
        while "consent_release" not in names(log):
            await asyncio.sleep(0.01)
        stopping = asyncio.create_task(main.stop(1))
        await asyncio.sleep(0.05)
        consent.go.set()
        await stopping
        await task  # 취소되지 않고 정상 종료해야 한다
        assert not task.cancelled()

    asyncio.run(scenario())

    assert "local_stop" not in names(log)  # 끝나는 중인 실행은 모델을 멈추지 않는다
    assert names(log)[-2:] == ["memo_clear", "finish_run"]
    assert finished_with(log) == EndReason.INFO_ENOUGH


class SlowAnswerChatManager(FakeChatManager):
    """최종 답변 저장이 go 신호를 받을 때까지 멈춰 있다. 저장 도중에 stop()을 부르기 위함."""

    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        super().__init__(log)
        self.go = threading.Event()

    def save_message(self, chat_id: int, role: str, content: str) -> str:
        message_id = super().save_message(chat_id, role, content)
        if role == "assistant":
            self.go.wait(timeout=5)
        return message_id


def test_stop_after_answer_completes_saves_once() -> None:
    log: list[tuple[Any, ...]] = []
    chat = SlowAnswerChatManager(log)
    main = make_main(log, chat_manager=chat)

    async def scenario() -> None:
        task = asyncio.create_task(main.run_pipeline(1, "질문"))
        while ("save_message", "assistant", "최종 답변") not in log:
            await asyncio.sleep(0.01)
        stopping = asyncio.create_task(main.stop(1))
        await asyncio.sleep(0.05)
        chat.go.set()
        await stopping
        await task
        assert not task.cancelled()

    asyncio.run(scenario())

    saved = [call for call in log if call[:2] == ("save_message", "assistant")]
    assert saved == [("save_message", "assistant", "최종 답변")]
    assert finished_with(log) == EndReason.INFO_ENOUGH

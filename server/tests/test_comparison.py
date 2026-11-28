"""비교 기능의 실행·중지·재시도·API 검사.

파이프라인은 실제 모듈로 돌고(test_api의 대역 사용), 두 단독 모델만 대역으로 바꾼다.
실제 모델은 부르지 않는다.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_api import (
    ANSWER,
    JUDGE_ASK_BOTH,
    JUDGE_ENOUGH,
    QUESTION,
    SIMPLE_RUN,
    Gateway,
    RoutedLlama,
    pending_consent,
    wait_finished,
    wait_for,
)
from test_api import CONSENT_RUN as CONSENT_OUTPUTS

from app import config
from app.api import app as app_module
from app.api.app import comparison_settings, create_app
from app.data.prompt_manager import PromptManager
from app.runtime import frontier_client as fc
from app.runtime.frontier_client import FrontierClient
from comparison import manager as manager_module
from comparison.local import LocalStandalone
from comparison.manager import ComparisonManager, build_manager
from comparison.models import (
    Cancellation,
    Cancelled,
    ComparisonSettings,
    ErrorCode,
    ModelFailure,
    StreamEvent,
    Turn,
)
from comparison.store import ComparisonStore

SECRET = "sk-super-secret"
CONSENT_RUN = {**CONSENT_OUTPUTS, "JUDGE": [JUDGE_ASK_BOTH, JUDGE_ENOUGH]}


class FakeModel:
    """단독 모델 대역. 부를 때마다 scripts에서 차례로 꺼내고, 비면 기본 답을 낸다.

    script의 항목: 글자(조각), StreamEvent, 예외(발생), threading.Event(풀릴 때까지 대기).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.scripts: list[list[Any]] = []
        self.calls: list[tuple[list[Turn], str]] = []
        self.reached = threading.Event()
        self.gates: list[threading.Event] = []

    @property
    def model_name(self) -> str | None:
        return f"{self.name}-model"

    def cost(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        return None if input_tokens is None else 0.5

    def gate(self) -> threading.Event:
        event = threading.Event()
        self.gates.append(event)
        return event

    def stream(
        self, turns: list[Turn], question: str, cancellation: Cancellation
    ) -> Iterator[StreamEvent]:
        self.calls.append((list(turns), question))
        script = self.scripts.pop(0) if self.scripts else None
        if script is None:
            script = [f"{self.name} 답 {len(self.calls)}", done_event()]
        yield StreamEvent(dropped_pairs=0)
        for step in script:
            if isinstance(step, threading.Event):
                self.reached.set()
                while not step.wait(0.01):
                    if cancellation.cancelled:
                        raise Cancelled
            elif isinstance(step, BaseException):
                raise step
            elif isinstance(step, StreamEvent):
                yield step
            else:
                yield StreamEvent(text=step)


def done_event(reason: str = "stop") -> StreamEvent:
    return StreamEvent(finish_reason=reason, input_tokens=10, output_tokens=5)


class World:
    """앱 하나와 대역들. 같은 DB로 다시 켤 수 있다."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.db_path = tmp_path / "chat.sqlite3"
        self.monkeypatch = monkeypatch
        self.local = FakeModel("로컬")
        self.frontier = FakeModel("프론티어")
        self.gateway = Gateway()
        self.timeout = 60.0  # wait_for보다 길게. 시간 초과가 중지를 대신하지 않게 한다
        self.manager: ComparisonManager | None = None
        self.clients: list[TestClient] = []

    def factory(self, settings: ComparisonSettings, **kwargs: Any) -> ComparisonManager:
        self.manager = build_manager(
            settings,
            local=self.local,
            frontier=self.frontier,
            timeout=self.timeout,
            **kwargs,
        )
        return self.manager

    def open(
        self,
        outputs: dict[str, list[str]] | None = None,
        *,
        factory: Callable[..., ComparisonManager] | None = None,
        compare: bool = True,
        model: RoutedLlama | None = None,
    ) -> TestClient:
        self.model = model or RoutedLlama(outputs or SIMPLE_RUN)
        self.monkeypatch.setattr(
            "src.localLLM_API_proxy.llama_wrapper.Llama", Mock(return_value=self.model)
        )
        app = create_app(
            db_path=self.db_path,
            prompt_chooser_factory=lambda *_: None,
            frontier_factory=lambda prompts: FrontierClient(
                prompts,
                api_key=SECRET,
                base_url="https://gateway.test/v1",
                env_file=None,
                transport=httpx.MockTransport(self.gateway),
            ),
            comparison_factory=(factory or self.factory) if compare else None,
        )
        client = TestClient(app)
        client.__enter__()
        self.clients.append(client)
        return client

    def close(self, client: TestClient) -> None:
        self.clients.remove(client)
        client.__exit__(None, None, None)

    def release(self) -> None:
        for model in (self.local, self.frontier):
            for gate in model.gates:
                gate.set()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    # 개발자의 server/.env와 환경변수가 결과에 영향을 주지 않게 한다
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "none.env")
    for name in ("COMPARISON_ENABLED", "FRONTIER_API_KEY", "FRONTIER_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(fc, "RETRY_DELAYS", (0.0, 0.0))
    monkeypatch.setattr(manager_module, "SAVE_RETRY_SECONDS", 0.0)
    world = World(tmp_path, monkeypatch)
    yield world
    world.release()
    for client in list(world.clients):
        world.close(client)


def new_chat(client: TestClient) -> int:
    return client.post("/api/chats").json()["chatId"]


def send(client: TestClient, chat_id: int, text: str = QUESTION) -> int:
    res = client.post(f"/api/chats/{chat_id}/messages", json={"text": text})
    assert res.status_code == 200, res.text
    return res.json()["runId"]


def status_of(client: TestClient) -> dict[str, Any]:
    return client.get("/api/comparisons/status").json()


def comparisons(client: TestClient, chat_id: int) -> dict[str, Any]:
    return client.get(f"/api/chats/{chat_id}/comparisons").json()


def item(client: TestClient, chat_id: int, run_id: int, kind: str) -> dict[str, Any]:
    return next(
        i
        for i in comparisons(client, chat_id)["items"]
        if i["runId"] == run_id and i["kind"] == kind
    )


def wait_idle(client: TestClient, run_id: int) -> None:
    wait_finished(client, run_id)
    wait_for(lambda: status_of(client)["running"] is None)


def ask(client: TestClient, chat_id: int, text: str = QUESTION) -> int:
    """질문을 보내고 세 출력이 모두 끝날 때까지 기다린다."""
    run_id = send(client, chat_id, text)
    wait_idle(client, run_id)
    return run_id


def retry(client: TestClient, run_id: int, kind: str) -> httpx.Response:
    return client.post(f"/api/runs/{run_id}/comparisons/{kind}/retry")


def pipeline_answers(client: TestClient, chat_id: int) -> list[str]:
    messages = client.get(f"/api/chats/{chat_id}").json()["messages"]
    return [m["content"] for m in messages if m["role"] == "assistant"]


# 3. 세 대화


def test_three_conversations_stay_separate(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    ask(client, chat, "질문 하나")
    ask(client, chat, "질문 둘")
    assert world.local.calls == [
        ([], "질문 하나"),
        ([Turn(question="질문 하나", answer="로컬 답 1")], "질문 둘"),
    ]
    assert world.frontier.calls == [
        ([], "질문 하나"),
        ([Turn(question="질문 하나", answer="프론티어 답 1")], "질문 둘"),
    ]
    assert pipeline_answers(client, chat) == [ANSWER, ANSWER]
    for _, text in world.model.inputs:
        assert "로컬 답" not in text and "프론티어 답" not in text
    for text in world.gateway.requests:
        assert "로컬 답" not in text and "프론티어 답" not in text


def test_pipeline_inputs_are_the_same_with_and_without_comparison(
    tmp_path: Path, world: World
) -> None:
    def inputs(compare: bool, name: str) -> Any:
        world.db_path = tmp_path / name
        world.gateway = Gateway()
        client = world.open(compare=compare)
        chat = new_chat(client)
        for text in ("질문 하나", "질문 둘"):
            run = send(client, chat, text)
            wait_finished(client, run)
            wait_for(lambda: status_of(client)["running"] is None)
        result = (world.model.inputs, world.gateway.requests)
        world.close(client)
        return result

    assert inputs(True, "a.sqlite3") == inputs(False, "b.sqlite3")


def test_saved_result_fields(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    run = ask(client, chat)
    data = comparisons(client, chat)
    assert data["registered"] is True
    assert [(i["runId"], i["kind"]) for i in data["items"]] == [
        (run, "frontier"),
        (run, "local"),
    ]
    frontier = data["items"][0]
    assert frontier["question"] == QUESTION
    assert frontier["answer"] == "프론티어 답 1"
    assert frontier["status"] == "ok" and frontier["errorCode"] is None
    assert frontier["message"] is None and frontier["modelName"] == "프론티어-model"
    assert frontier["attempts"] == 1
    assert frontier["usedPairs"] == 0 and frontier["droppedPairs"] == 0
    assert (frontier["inputTokens"], frontier["outputTokens"]) == (10, 5)
    assert frontier["cost"] == 0.5 and frontier["elapsedMs"] >= 0
    assert frontier["createdAt"] and frontier["endedAt"]
    assert frontier["retryable"] is False


# 6. 서로 영향을 주지 않는다


@pytest.mark.parametrize("failing", ["local", "frontier"])
def test_one_failure_does_not_stop_the_others(world: World, failing: str) -> None:
    client = world.open()
    chat = new_chat(client)
    model = world.local if failing == "local" else world.frontier
    model.scripts.append(["일부", ModelFailure(ErrorCode.STREAM_BROKEN)])
    run = ask(client, chat)
    other = "frontier" if failing == "local" else "local"
    failed = item(client, chat, run, failing)
    assert (failed["status"], failed["errorCode"]) == ("failed", "STREAM_BROKEN")
    assert failed["answer"] == "일부" and failed["retryable"] is True
    assert failed["message"] == "답변을 받는 도중 연결이 끊겼습니다."
    assert item(client, chat, run, other)["status"] == "ok"
    assert pipeline_answers(client, chat) == [ANSWER]
    # 실패한 답은 다음 질문의 앞 대화에 들어가지 않는다
    ask(client, chat, "다음")
    assert model.calls[-1] == ([], "다음")


def test_unexpected_model_error_is_internal(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append([RuntimeError("bug")])
    run = ask(client, chat)
    assert item(client, chat, run, "local")["errorCode"] == "INTERNAL"
    assert item(client, chat, run, "frontier")["status"] == "ok"


def test_pipeline_error_does_not_stop_comparison(world: World) -> None:
    client = world.open({**SIMPLE_RUN, "DETECT": ["읽을 수 없는 출력"]})
    chat = new_chat(client)
    run = ask(client, chat)
    detail = client.get(f"/api/runs/{run}").json()
    assert detail["errorCode"] is not None
    assert item(client, chat, run, "local")["status"] == "ok"
    assert item(client, chat, run, "frontier")["status"] == "ok"


def test_question_goes_out_before_consent(world: World) -> None:
    client = world.open(CONSENT_RUN)
    chat = new_chat(client)
    run = send(client, chat)
    pending_consent(client, run)
    wait_for(lambda: status_of(client)["running"] is None)
    assert world.frontier.calls == [([], QUESTION)]
    client.post(f"/api/runs/{run}/stop")
    wait_finished(client, run)


# 끝나는 조건


@pytest.mark.parametrize(
    ("script", "status", "code"),
    [
        (["글", done_event("length")], "truncated", None),
        (["글", done_event("content_filter")], "failed", "REJECTED"),
        (["글", done_event("tool_calls")], "failed", "INTERNAL"),
        ([" \n", done_event()], "failed", "EMPTY_RESPONSE"),
        ([done_event()], "failed", "EMPTY_RESPONSE"),
        (["글"], "failed", "STREAM_BROKEN"),
        ([], "failed", "STREAM_BROKEN"),
        # 글과 끝난 이유를 받은 뒤의 끊김은 답변에 영향이 없다
        (
            ["글", done_event(), ModelFailure(ErrorCode.STREAM_BROKEN)],
            "ok",
            None,
        ),
        (
            [ModelFailure(ErrorCode.INPUT_TOO_LONG, dropped_pairs=2)],
            "failed",
            "INPUT_TOO_LONG",
        ),
    ],
)
def test_end_conditions(
    world: World, script: list[Any], status: str, code: str | None
) -> None:
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append(script)
    run = ask(client, chat)
    result = item(client, chat, run, "local")
    assert (result["status"], result["errorCode"]) == (status, code)
    if code == "INPUT_TOO_LONG":
        assert result["usedPairs"] is None and result["droppedPairs"] == 2
    ask(client, chat, "다음")
    expected = [Turn(question=QUESTION, answer="글")] if status == "ok" else []
    assert world.local.calls[-1] == (expected, "다음")


def test_timeout_keeps_received_text(world: World) -> None:
    world.timeout = 0.3
    client = world.open()
    chat = new_chat(client)
    world.frontier.scripts.append(["받은 글", world.frontier.gate()])
    run = ask(client, chat)
    result = item(client, chat, run, "frontier")
    assert (result["status"], result["errorCode"]) == ("failed", "TIMEOUT")
    assert result["answer"] == "받은 글"
    assert item(client, chat, run, "local")["status"] == "ok"


def test_late_text_after_timeout_does_not_change_the_result(world: World) -> None:
    """취소를 모르는 모델이 늦게 보낸 글이 저장된 결과를 덮어쓰지 않는다."""

    class Stubborn(FakeModel):
        finished = threading.Event()

        def stream(
            self, turns: list[Turn], question: str, cancellation: Cancellation
        ) -> Iterator[StreamEvent]:
            yield StreamEvent(text="앞")
            time.sleep(0.6)
            self.finished.set()
            yield StreamEvent(text="늦은 글", finish_reason="stop")

    world.timeout = 0.2
    world.local = Stubborn("로컬")
    client = world.open()
    chat = new_chat(client)
    run = ask(client, chat)
    assert Stubborn.finished.wait(5)
    time.sleep(0.1)
    result = item(client, chat, run, "local")
    assert (result["errorCode"], result["answer"]) == ("TIMEOUT", "앞")


def test_save_failure_is_reported_and_does_not_block_next_question(
    world: World,
) -> None:
    client = world.open()
    chat = new_chat(client)
    original = ComparisonStore.finish

    def broken(self: ComparisonStore, run_id: int, kind: Any, result: Any) -> str:
        if kind == "local":
            raise sqlite3.OperationalError("database is locked")
        return original(self, run_id, kind, result)

    world.monkeypatch.setattr(ComparisonStore, "finish", broken)
    run = ask(client, chat)
    result = item(client, chat, run, "local")
    assert (result["status"], result["errorCode"]) == ("failed", "INTERRUPTED")
    assert result["retryable"] is True
    world.monkeypatch.setattr(ComparisonStore, "finish", original)
    assert retry(client, run, "local").status_code == 200
    wait_idle(client, run)
    assert item(client, chat, run, "local")["status"] == "ok"


# 7. 중지


def test_stop_stops_pipeline_and_comparison(world: World) -> None:
    client = world.open(CONSENT_RUN)
    chat = new_chat(client)
    world.local.scripts.append(["일부", world.local.gate()])
    run = send(client, chat)
    pending_consent(client, run)
    assert world.local.reached.wait(5)
    wait_for(lambda: item(client, chat, run, "frontier")["status"] == "ok")
    assert client.post(f"/api/runs/{run}/stop").status_code == 200
    wait_idle(client, run)
    assert client.get(f"/api/runs/{run}").json()["endReason"] == "stopped"
    stopped = item(client, chat, run, "local")
    assert (stopped["status"], stopped["answer"]) == ("stopped", "일부")
    assert stopped["errorCode"] is None and stopped["retryable"] is True
    # 이미 끝난 답은 바꾸지 않는다
    assert item(client, chat, run, "frontier")["status"] == "ok"
    # 모두 끝난 뒤에는 기존과 같다
    res = client.post(f"/api/runs/{run}/stop")
    assert (res.status_code, res.json()["code"]) == (409, "RUN_FINISHED")


def test_stop_only_pipeline(world: World) -> None:
    client = world.open(CONSENT_RUN)
    chat = new_chat(client)
    run = send(client, chat)
    pending_consent(client, run)
    wait_for(lambda: status_of(client)["running"] is None)
    assert client.post(f"/api/runs/{run}/stop").status_code == 200
    wait_finished(client, run)
    assert {i["status"] for i in comparisons(client, chat)["items"]} == {"ok"}


def test_stop_only_comparison(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    world.frontier.scripts.append([world.frontier.gate()])
    run = send(client, chat)
    wait_finished(client, run)
    assert status_of(client)["running"] == {"chatId": chat, "runId": run}
    assert client.post(f"/api/runs/{run}/stop").status_code == 200
    wait_idle(client, run)
    stopped = item(client, chat, run, "frontier")
    assert (stopped["status"], stopped["answer"]) == ("stopped", None)
    assert client.get(f"/api/runs/{run}").json()["endReason"] == "info_enough"
    assert client.post(f"/api/runs/{run}/stop").status_code == 409
    ask(client, chat, "다음")  # 중지해도 다음 질문이 동작한다


def test_stop_of_unknown_run_is_404(world: World) -> None:
    client = world.open()
    assert client.post("/api/runs/999/stop").status_code == 404


# 8. 재시도


def test_retry_updates_the_same_row(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    first = ask(client, chat, "하나")
    world.frontier.scripts.append([ModelFailure(ErrorCode.UNAVAILABLE)])
    second = ask(client, chat, "둘")
    assert item(client, chat, second, "frontier")["retryable"] is True
    assert item(client, chat, second, "local")["retryable"] is False

    assert retry(client, second, "frontier").status_code == 200
    wait_idle(client, second)
    result = item(client, chat, second, "frontier")
    assert (result["status"], result["attempts"]) == ("ok", 2)
    assert result["errorCode"] is None and result["usedPairs"] == 1
    # 앞 대화는 처음 실행과 같고, 로컬은 다시 부르지 않는다
    assert world.frontier.calls[-1] == world.frontier.calls[-2]
    assert len(world.local.calls) == 2
    assert len(comparisons(client, chat)["items"]) == 4

    ask(client, chat, "셋")
    assert [t.question for t in world.frontier.calls[-1][0]] == ["하나", "둘"]
    assert item(client, chat, first, "local")["usedPairs"] == 0


def test_retry_conditions(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append([ModelFailure(ErrorCode.UNAVAILABLE)])
    first = ask(client, chat, "하나")
    # 성공한 답은 다시 시도할 수 없다
    res = retry(client, first, "frontier")
    assert (res.status_code, res.json()["code"]) == (409, "NOT_RETRYABLE")
    # 없는 기록
    res = retry(client, 999, "local")
    assert (res.status_code, res.json()["code"]) == (404, "NOT_FOUND")
    assert retry(client, first, "pipeline").status_code == 422
    # 마지막 질문이 아니면 다시 시도할 수 없다
    ask(client, chat, "둘")
    assert item(client, chat, first, "local")["retryable"] is False
    assert retry(client, first, "local").json()["code"] == "NOT_RETRYABLE"
    # 거절해도 자리가 남지 않는다
    ask(client, chat, "셋")


@pytest.mark.parametrize("script", [["글", done_event("length")], ["글", "stop"]])
def test_truncated_and_stopped_can_be_retried(world: World, script: list[Any]) -> None:
    client = world.open()
    chat = new_chat(client)
    if script[-1] == "stop":
        world.local.scripts.append(["글", world.local.gate()])
        run = send(client, chat)
        assert world.local.reached.wait(5)
        client.post(f"/api/runs/{run}/stop")
        wait_idle(client, run)
    else:
        world.local.scripts.append(script)
        run = ask(client, chat)
    assert item(client, chat, run, "local")["retryable"] is True
    assert retry(client, run, "local").status_code == 200
    wait_idle(client, run)
    assert item(client, chat, run, "local")["status"] == "ok"


def test_retry_is_refused_while_anything_is_running(world: World) -> None:
    client = world.open(CONSENT_RUN)
    chat = new_chat(client)
    world.local.scripts.append([ModelFailure(ErrorCode.UNAVAILABLE)])
    run = send(client, chat)
    pending_consent(client, run)
    wait_for(lambda: status_of(client)["running"] is None)
    # 파이프라인이 진행 중
    res = retry(client, run, "local")
    assert (res.status_code, res.json()["code"]) == (409, "COMPARISON_RUNNING")
    client.post(f"/api/runs/{run}/stop")
    wait_finished(client, run)
    # 비교 재시도가 진행 중
    world.local.scripts.append([world.local.gate()])
    wait_for(lambda: retry(client, run, "local").status_code == 200)
    assert retry(client, run, "local").json()["code"] == "COMPARISON_RUNNING"
    # 재시도하는 동안에는 다음 질문도 막는다
    res = client.post(f"/api/chats/{chat}/messages", json={"text": "다음"})
    assert (res.status_code, res.json()["code"]) == (409, "COMPARISON_RUNNING")
    # 재시도도 중지할 수 있다
    assert client.post(f"/api/runs/{run}/stop").status_code == 200
    wait_idle(client, run)
    result = item(client, chat, run, "local")
    assert (result["status"], result["attempts"]) == ("stopped", 2)


# 10 · 22. 동시 요청


def test_next_question_waits_for_all_outputs(world: World) -> None:
    client = world.open()
    chat, other = new_chat(client), new_chat(client)
    world.frontier.scripts.append([world.frontier.gate()])
    run = send(client, chat)
    wait_finished(client, run)
    for target in (chat, other):
        res = client.post(f"/api/chats/{target}/messages", json={"text": "다음"})
        assert (res.status_code, res.json()["code"]) == (409, "COMPARISON_RUNNING")
    world.release()
    wait_idle(client, run)
    ask(client, other, "다음")


def test_pipeline_running_keeps_the_existing_error(world: World) -> None:
    client = world.open(CONSENT_RUN)
    chat = new_chat(client)
    run = send(client, chat)
    pending_consent(client, run)
    res = client.post(f"/api/chats/{chat}/messages", json={"text": "다음"})
    assert (res.status_code, res.json()["code"]) == (409, "RUN_IN_PROGRESS")
    client.post(f"/api/runs/{run}/stop")
    wait_idle(client, run)


def test_send_and_retry_together_run_only_one(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append([ModelFailure(ErrorCode.UNAVAILABLE)])
    run = ask(client, chat, "하나")
    barrier = threading.Barrier(4)
    codes: list[int] = []

    def call(action: Callable[[], httpx.Response]) -> None:
        barrier.wait()
        codes.append(action().status_code)

    gate = world.local.gate()
    world.local.scripts.append([gate])

    def send_next() -> httpx.Response:
        return client.post(f"/api/chats/{chat}/messages", json={"text": "둘"})

    actions = [send_next, send_next] + [lambda: retry(client, run, "local")] * 2
    threads = [threading.Thread(target=call, args=(a,)) for a in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(codes) == [200, 409, 409, 409]
    gate.set()
    wait_for(lambda: status_of(client)["running"] is None)


def test_pipeline_start_failure_releases_the_seat(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    pipeline = client.app.state.pipeline  # type: ignore[attr-defined]
    original = pipeline.start

    async def broken(chat_id: int, text: str) -> int:
        raise RuntimeError("시작 실패")

    pipeline.start = broken
    with pytest.raises(RuntimeError):
        send(client, chat)
    pipeline.start = original
    assert world.local.calls == []
    ask(client, chat)


def test_retry_before_run_id_is_refused(world: World) -> None:
    """실행 번호를 받기 전(_starting)에 온 재시도도 진행 중으로 본다."""
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append([ModelFailure(ErrorCode.UNAVAILABLE)])
    run = ask(client, chat)
    pipeline = client.app.state.pipeline  # type: ignore[attr-defined]
    pipeline._starting = Mock()
    assert pipeline.is_busy
    assert retry(client, run, "local").json()["code"] == "COMPARISON_RUNNING"
    pipeline._starting = None
    assert retry(client, run, "local").status_code == 200
    wait_idle(client, run)


# 24. 비교 기록을 만들지 못한 질문


def test_question_without_record_keeps_pipeline_and_blocks_older_retry(
    world: World,
) -> None:
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append([ModelFailure(ErrorCode.UNAVAILABLE)])
    first = ask(client, chat, "하나")
    assert item(client, chat, first, "local")["retryable"] is True

    def broken(*_: Any) -> None:
        raise sqlite3.OperationalError("disk full")

    original = ComparisonStore.begin
    world.monkeypatch.setattr(ComparisonStore, "begin", broken)
    second = ask(client, chat, "둘")
    world.monkeypatch.setattr(ComparisonStore, "begin", original)
    assert len(pipeline_answers(client, chat)) == 2
    assert {i["runId"] for i in comparisons(client, chat)["items"]} == {first}
    assert item(client, chat, first, "local")["retryable"] is False
    assert retry(client, first, "local").json()["code"] == "NOT_RETRYABLE"
    assert retry(client, second, "local").json()["code"] == "NOT_FOUND"
    third = ask(client, chat, "셋")
    assert item(client, chat, third, "local")["status"] == "ok"


def test_failure_after_begin_does_not_leave_running_rows(world: World) -> None:
    client = world.open()
    chat = new_chat(client)

    def broken(*_: Any) -> None:
        raise sqlite3.OperationalError("read failed")

    original = ComparisonStore.history
    world.monkeypatch.setattr(ComparisonStore, "history", broken)
    run = ask(client, chat)
    world.monkeypatch.setattr(ComparisonStore, "history", original)
    assert world.local.calls == []
    for kind in ("local", "frontier"):
        result = item(client, chat, run, kind)
        assert (result["status"], result["errorCode"]) == ("failed", "INTERNAL")
    assert retry(client, run, "local").status_code == 200
    wait_idle(client, run)
    assert item(client, chat, run, "local")["status"] == "ok"


# 13. 채팅 삭제


def test_deleting_a_chat_stops_and_removes_its_comparison(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append(["일부", world.local.gate()])
    run = send(client, chat)
    wait_finished(client, run)
    assert client.delete(f"/api/chats/{chat}").status_code == 200
    wait_for(lambda: status_of(client)["running"] is None)
    assert comparisons(client, chat) == {"registered": False, "items": []}
    with sqlite3.connect(world.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM comparisons").fetchone() == (0,)
    ask(client, new_chat(client))


def test_refused_delete_does_not_touch_comparison(world: World) -> None:
    client = world.open(CONSENT_RUN)
    chat = new_chat(client)
    world.local.scripts.append([world.local.gate(), "글", done_event()])
    run = send(client, chat)
    pending_consent(client, run)
    assert world.local.reached.wait(5)
    res = client.delete(f"/api/chats/{chat}")
    assert (res.status_code, res.json()["code"]) == (409, "CHAT_RUNNING")
    world.release()
    wait_for(lambda: item(client, chat, run, "local")["status"] == "ok")
    client.post(f"/api/runs/{run}/stop")
    wait_idle(client, run)


# 14. 비교 대상 채팅


@pytest.mark.parametrize("value", ["0", "false", "OFF", "No"])
def test_disabled_never_calls_models(world: World, value: str) -> None:
    world.monkeypatch.setenv("COMPARISON_ENABLED", value)
    client = world.open()
    chat = new_chat(client)
    ask(client, chat)
    assert status_of(client) == {"enabled": False, "running": None}
    assert comparisons(client, chat) == {"registered": False, "items": []}
    assert world.local.calls == world.frontier.calls == []


def test_chat_asked_while_off_is_never_compared_again(world: World) -> None:
    client = world.open()
    asked_off, untouched = new_chat(client), new_chat(client)
    run = ask(client, asked_off, "켜진 동안")
    world.close(client)

    world.monkeypatch.setenv("COMPARISON_ENABLED", "off")
    client = world.open()
    made_off = new_chat(client)
    ask(client, asked_off, "꺼진 동안")
    world.close(client)
    calls = len(world.frontier.calls)

    world.monkeypatch.setenv("COMPARISON_ENABLED", "on")
    client = world.open()
    for chat in (asked_off, made_off):
        ask(client, chat, "다시 켠 뒤")
        assert comparisons(client, chat)["registered"] is False
    assert len(world.frontier.calls) == calls
    # 저장된 기록은 남고 볼 수 있다. 다시 시도할 수는 없다
    saved = comparisons(client, asked_off)["items"]
    assert [i["runId"] for i in saved] == [run, run]
    assert not any(i["retryable"] for i in saved)
    # 꺼진 동안 질문하지 않은 채팅은 계속 비교한다
    ask(client, untouched, "비교")
    assert len(world.frontier.calls) == calls + 1


def test_unregistered_failed_record_cannot_be_retried(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append([ModelFailure(ErrorCode.UNAVAILABLE)])
    run = ask(client, chat)
    assert world.manager is not None
    world.manager._store.unregister_chat(chat)
    assert item(client, chat, run, "local")["retryable"] is False
    assert retry(client, run, "local").json()["code"] == "NOT_RETRYABLE"


def test_chat_from_before_the_feature_is_not_compared(world: World) -> None:
    client = world.open(compare=False)
    chat = new_chat(client)
    ask(client, chat)
    world.close(client)
    client = world.open()
    ask(client, chat)
    assert comparisons(client, chat) == {"registered": False, "items": []}
    assert world.frontier.calls == []


# 12. 재시작


def test_restart_recovers_running_rows_and_keeps_context(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    ask(client, chat, "하나")
    run = ask(client, chat, "둘")
    world.close(client)
    with sqlite3.connect(world.db_path) as db:  # 답변 중에 서버가 꺼진 상태
        db.execute(
            "UPDATE comparisons SET status='running', answer=NULL"
            " WHERE runId=? AND kind='frontier'",
            (run,),
        )
    client = world.open()
    with sqlite3.connect(world.db_path) as db:  # 화면뿐 아니라 DB도 정리한다
        assert db.execute(
            "SELECT status, errorCode FROM comparisons"
            " WHERE runId=? AND kind='frontier'",
            (run,),
        ).fetchone() == ("failed", "INTERRUPTED")
    result = item(client, chat, run, "frontier")
    assert (result["status"], result["errorCode"]) == ("failed", "INTERRUPTED")
    assert result["retryable"] is True
    ask(client, chat, "셋")
    assert [t.answer for t in world.local.calls[-1][0]] == ["로컬 답 1", "로컬 답 2"]
    assert [t.answer for t in world.frontier.calls[-1][0]] == ["프론티어 답 1"]


def test_same_input_before_and_after_restart(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    ask(client, chat, "하나")
    world.local.scripts.append([ModelFailure(ErrorCode.UNAVAILABLE)])
    run = ask(client, chat, "둘")
    before = world.local.calls[-1]
    world.close(client)
    client = world.open()
    assert retry(client, run, "local").status_code == 200
    wait_idle(client, run)
    assert world.local.calls[-1] == before


def test_shutdown_stops_running_comparison(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    world.local.scripts.append(["일부", world.local.gate()])
    run = send(client, chat)
    wait_finished(client, run)
    assert world.local.reached.wait(5)
    world.close(client)
    with sqlite3.connect(world.db_path) as db:
        assert db.execute(
            "SELECT status, answer FROM comparisons WHERE runId=? AND kind='local'",
            (run,),
        ).fetchone() == ("stopped", "일부")


# 16 · 20. 준비


def test_app_without_comparison_behaves_as_before(world: World) -> None:
    client = world.open(compare=False)
    chat = new_chat(client)
    run = ask(client, chat)
    assert pipeline_answers(client, chat) == [ANSWER]
    assert status_of(client) == {"enabled": False, "running": None}
    assert comparisons(client, chat) == {"registered": False, "items": []}
    assert retry(client, run, "local").json()["code"] == "NOT_RETRYABLE"
    body = client.get(f"/api/runs/{run}/comparisons/events").text
    assert [name for name, _ in parse(body)] == ["snapshot", "done"]


def test_preparation_failure_starts_the_server_without_comparison(
    world: World,
) -> None:
    def broken(*_: Any, **__: Any) -> ComparisonManager:
        raise RuntimeError("준비 실패")

    client = world.open(factory=broken)
    ask(client, new_chat(client))
    assert status_of(client) == {"enabled": False, "running": None}


def test_missing_local_key_fails_only_the_local_panel(world: World) -> None:
    def no_key() -> Any:
        raise FileNotFoundError("No llama_api_key*.key file")

    world.local = LocalStandalone(no_key)  # type: ignore[assignment]
    client = world.open()
    chat = new_chat(client)
    run = ask(client, chat)
    result = item(client, chat, run, "local")
    assert (result["status"], result["errorCode"]) == ("failed", "KEY_MISSING")
    assert result["usedPairs"] is None
    assert item(client, chat, run, "frontier")["status"] == "ok"
    world.local = FakeModel("로컬")


def test_settings_follow_the_pipeline_configuration(world: World) -> None:
    path = world.db_path
    settings = comparison_settings(path)
    assert settings.enabled and settings.db_path == path
    assert settings.frontier_api_key is None
    assert settings.frontier_model == fc.DEFAULT_MODEL
    assert settings.frontier_base_url == fc.DEFAULT_BASE_URL
    assert (settings.input_price, settings.output_price) == fc.MODEL_PRICES[
        fc.DEFAULT_MODEL
    ]
    world.monkeypatch.setenv("FRONTIER_MODEL", "bedrock-haiku")
    world.monkeypatch.setenv("FRONTIER_API_KEY", SECRET)
    settings = comparison_settings(path)
    assert settings.frontier_model == "bedrock-haiku"
    assert settings.frontier_api_key == SECRET


def test_only_the_real_app_creates_real_comparison() -> None:
    default = inspect.signature(create_app).parameters["comparison_factory"].default
    assert default is None
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    last = source.rstrip().splitlines()[-1]
    assert last == "app = create_app(comparison_factory=create_manager)"


# 11. 실시간 이벤트


def parse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for chunk in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in chunk.splitlines())
        if "event" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_events_of_a_finished_run(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    run = ask(client, chat)
    res = client.get(f"/api/runs/{run}/comparisons/events")
    assert res.headers["content-type"].startswith("text/event-stream")
    (first, snapshot), last = parse(res.text)
    assert first == "snapshot" and last == ("done", {})
    assert {i["kind"]: i["answer"] for i in snapshot["items"]} == {
        "local": "로컬 답 1",
        "frontier": "프론티어 답 1",
    }


def test_events_of_a_running_comparison(world: World) -> None:
    client = world.open()
    chat = new_chat(client)
    gate = world.local.gate()
    world.local.scripts.append(["가", "나", gate, "다", done_event()])
    run = send(client, chat)
    wait_for(lambda: item(client, chat, run, "local")["answer"] == "가나")
    assert item(client, chat, run, "local")["status"] == "running"
    wait_for(lambda: item(client, chat, run, "frontier")["status"] == "ok")
    threading.Timer(0.3, gate.set).start()
    events = parse(client.get(f"/api/runs/{run}/comparisons/events").text)
    assert [name for name, _ in events] == ["snapshot", "token", "result", "done"]
    local = next(i for i in events[0][1]["items"] if i["kind"] == "local")
    assert (local["status"], local["answer"]) == ("running", "가나")
    assert events[1][1] == {"kind": "local", "text": "다"}
    assert events[2][1]["answer"] == "가나다" and events[2][1]["status"] == "ok"


class Bench:
    """이벤트 루프에서 관리자만 직접 다루는 검사용."""

    def __init__(self, tmp_path: Path) -> None:
        from app.data.chat_manager import ChatManager

        self.chats = ChatManager(tmp_path / "chat.sqlite3")
        self.chat_id = self.chats.create_chat()
        self.local, self.frontier = FakeModel("로컬"), FakeModel("프론티어")
        self.run_id = 0
        self.manager = build_manager(
            ComparisonSettings(
                enabled=True,
                db_path=tmp_path / "chat.sqlite3",
                frontier_api_key=None,
                frontier_base_url="",
                frontier_model="m",
            ),
            local=self.local,
            frontier=self.frontier,
            pipeline_busy=lambda: False,
            latest_run_id=lambda _: self.run_id,
            timeout=60,
        )

    async def start(self, question: str = "q") -> int:
        from test_comparison_models import add_run

        self.run_id = add_run(self.chats, self.chat_id)
        assert self.manager.try_reserve()
        await self.manager.start(self.chat_id, self.run_id, question)
        return self.run_id

    async def close(self) -> None:
        for model in (self.local, self.frontier):
            for gate in model.gates:
                gate.set()
        await self.manager.shutdown()
        self.chats.close()


def test_reconnecting_never_loses_or_repeats_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        bench = Bench(tmp_path)
        await bench.manager.on_chat_created(bench.chat_id)
        pieces = [f"[{i}]" for i in range(400)]
        bench.local.scripts.append([*pieces, done_event()])
        run = await bench.start()

        async def watch(delay: float) -> str:
            await asyncio.sleep(delay)
            text = ""
            async for event in bench.manager.events(run):
                assert event is not None
                name, data = event
                if name == "snapshot":
                    text = next(
                        i["answer"] or "" for i in data["items"] if i["kind"] == "local"
                    )
                elif name == "token" and data["kind"] == "local":
                    text += data["text"]
                elif name == "result" and data["kind"] == "local":
                    assert text == data["answer"]
            return text

        texts = await asyncio.gather(*(watch(i / 500) for i in range(20)))
        assert set(texts) == {"".join(pieces)}
        await bench.close()

    asyncio.run(scenario())


def test_stop_during_preparation_never_calls_models(tmp_path: Path) -> None:
    async def scenario() -> None:
        bench = Bench(tmp_path)
        await bench.manager.on_chat_created(bench.chat_id)
        store = bench.manager._store
        entered, proceed = threading.Event(), threading.Event()
        original = store.begin

        def slow_begin(*args: Any) -> None:
            entered.set()
            assert proceed.wait(5)
            original(*args)

        store.begin = slow_begin  # type: ignore[method-assign]
        task = asyncio.create_task(bench.start())
        await asyncio.to_thread(entered.wait, 5)
        assert bench.manager.stop(bench.run_id) is True
        proceed.set()
        run = await task
        events = [e async for e in bench.manager.events(run)]
        assert events[-1] == ("done", {})
        records = store.list_run(run)
        assert {r.status.value for r in records} == {"stopped"}
        assert bench.local.calls == bench.frontier.calls == []
        assert bench.manager.busy() is None and bench.manager.try_reserve()
        await bench.close()

    asyncio.run(scenario())


def test_stop_is_not_accepted_when_only_saving_is_left(tmp_path: Path) -> None:
    """답이 정해지고 저장만 남았으면 중지해도 결과가 바뀌지 않으므로 받지 않는다."""

    async def scenario() -> None:
        bench = Bench(tmp_path)
        await bench.manager.on_chat_created(bench.chat_id)
        store = bench.manager._store
        entered, proceed = threading.Event(), threading.Event()
        saving: list[Any] = []
        original = store.finish

        def slow_finish(*args: Any) -> str:
            saving.append(args)
            if len(saving) == 2:  # 두 모델 모두 답을 정하고 저장 중이다
                entered.set()
            assert proceed.wait(5)
            return original(*args)

        store.finish = slow_finish  # type: ignore[method-assign]
        run = await bench.start()
        await asyncio.to_thread(entered.wait, 5)
        assert bench.manager.stop(run) is False
        proceed.set()
        async for _ in bench.manager.events(run):
            pass
        assert {r.status.value for r in store.list_run(run)} == {"ok"}
        await bench.close()

    asyncio.run(scenario())


def test_cancelled_start_does_not_leave_running_rows(tmp_path: Path) -> None:
    """줄을 추가한 뒤 요청이 취소되어도 running으로 남기지 않는다."""

    async def scenario() -> None:
        bench = Bench(tmp_path)
        await bench.manager.on_chat_created(bench.chat_id)
        store = bench.manager._store
        entered = threading.Event()
        original = store.list_run

        def slow_list_run(run_id: int) -> Any:
            entered.set()
            time.sleep(0.2)
            return original(run_id)

        store.list_run = slow_list_run  # type: ignore[method-assign]
        task = asyncio.create_task(bench.start())
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.list_run = original  # type: ignore[method-assign]
        records = store.list_run(bench.run_id)
        assert {(r.status.value, r.error_code) for r in records} == {
            ("failed", ErrorCode.INTERNAL)
        }
        assert bench.manager.busy() is None and bench.manager.try_reserve()
        bench.manager.release()
        await bench.close()

    asyncio.run(scenario())


def test_keepalive_when_nothing_happens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "KEEPALIVE_SECONDS", 0.05)

    async def scenario() -> None:
        bench = Bench(tmp_path)
        await bench.manager.on_chat_created(bench.chat_id)
        bench.local.scripts.append([bench.local.gate()])
        bench.frontier.scripts.append([bench.frontier.gate()])
        run = await bench.start()
        stream = bench.manager.events(run)
        first = await anext(stream)
        assert first is not None and first[0] == "snapshot"
        assert await anext(stream) is None
        bench.manager.stop(run)
        names = [e[0] async for e in stream if e is not None]
        assert names == ["result", "result", "done"]
        await bench.close()

    asyncio.run(scenario())


def test_stop_takes_effect_even_if_the_model_does_not_respond(tmp_path: Path) -> None:
    class Deaf(FakeModel):
        def stream(
            self, turns: list[Turn], question: str, cancellation: Cancellation
        ) -> Iterator[StreamEvent]:
            yield StreamEvent(text="글")
            time.sleep(3)

    async def scenario() -> None:
        bench = Bench(tmp_path)
        bench.manager._models["local"] = Deaf("로컬")
        await bench.manager.on_chat_created(bench.chat_id)
        run = await bench.start()
        await asyncio.sleep(0.1)
        started = time.monotonic()
        assert bench.manager.stop(run)
        async for _ in bench.manager.events(run):
            pass
        assert time.monotonic() - started < 1
        await bench.close()

    asyncio.run(scenario())


# 17. 로그


def test_logs_have_no_key_question_or_answer(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    secret_question = "주민번호는 900101-1234567입니다"
    secret_answer = "비밀 답변 조각"
    world.monkeypatch.setenv("FRONTIER_API_KEY", SECRET)

    def factory(settings: ComparisonSettings, **kwargs: Any) -> ComparisonManager:
        from comparison.frontier import FrontierStandalone

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.content.decode()
            return httpx.Response(400, json={"error": {"message": body}})

        return build_manager(
            settings,
            local=world.local,
            frontier=FrontierStandalone(
                api_key=settings.frontier_api_key,
                base_url="https://gateway.test/v1",
                model="m",
                transport=httpx.MockTransport(handler),
            ),
            **kwargs,
        )

    world.local.scripts.append([secret_answer, RuntimeError(secret_question)])
    client = world.open(factory=factory)
    chat = new_chat(client)
    run = ask(client, chat, secret_question)
    assert item(client, chat, run, "local")["status"] == "failed"
    assert item(client, chat, run, "frontier")["errorCode"] == "REJECTED"
    ours = [r for r in caplog.records if r.name.startswith("comparison")]
    assert ours
    text = "\n".join(
        r.getMessage() + (caplog.handler.format(r) if r.exc_info else "") for r in ours
    )
    for secret in (SECRET, secret_question, secret_answer, "900101"):
        assert secret not in text


# API-11


def test_run_detail_has_start_and_end_time(world: World) -> None:
    client = world.open(CONSENT_RUN)
    chat = new_chat(client)
    run = send(client, chat)
    pending_consent(client, run)
    detail = client.get(f"/api/runs/{run}").json()
    assert detail["startedAt"] and detail["endedAt"] is None
    client.post(f"/api/runs/{run}/stop")
    wait_idle(client, run)
    detail = client.get(f"/api/runs/{run}").json()
    assert detail["endedAt"] > detail["startedAt"]
    assert detail["consents"][0]["respondedAt"]


def test_prompts_are_not_used_by_comparison() -> None:
    folder = Path(manager_module.__file__).parent
    for path in folder.glob("*.py"):
        assert '"role": "system"' not in path.read_text(encoding="utf-8")
    assert PromptManager().get_version()

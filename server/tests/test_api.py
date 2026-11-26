"""화면 API 종단 검사: API → 실제 Main → 실제 모든 모듈 → SQLite ChatManager.

모델·외부 API의 전송 계층만 테스트 대역으로 바꾼다.
- 로컬 LLM: llama_wrapper.Llama 자리에 작업(시스템 프롬프트)별로 정해 둔 출력을 내는 대역
- 프론티어: 실제 FrontierClient에 httpx.MockTransport를 넣어 게이트웨이 응답을 흉내
나머지(Runner·파서·검출·판단·메모·요청문·누출 검사·동의·설정·답변·저장)는 모두 실제로 돈다.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.data.prompt_manager import PromptManager
from app.models.enums import EndReason, PromptIdx
from app.pipeline.answer_generator import LIMITED_MESSAGES
from app.privacy.consent_manager import UNCLEAR_MESSAGE
from app.runtime import frontier_client as fc
from app.runtime.frontier_client import FrontierClient

T = TypeVar("T")

QUESTION = "저는 27살이고 삼성전자 다니는데 이직 고민이에요"
DETECT_TWO = (
    '{"items": [{"text": "27살", "category": 2}, {"text": "삼성전자", "category": 2}]}'
)
DETECT_NONE = '{"items": []}'
NEEDS = '{"needs": ["이직 준비 방법"]}'
JUDGE_ASK_BOTH = (
    '{"enough": false, "needConsent": true, "consentItems": [1, 2], "needIdx": [0]}'
)
JUDGE_ASK_NOTHING = (
    '{"enough": false, "needConsent": false, "consentItems": [], "needIdx": [0]}'
)
JUDGE_ENOUGH = (
    '{"enough": true, "needConsent": false, "consentItems": [], "needIdx": []}'
)
CONSENT_TEXT = '{"purpose": "목적", "benefit": "이점", "risk": "위험"}'
REQUEST = "27살 직장인이 이직을 준비할 때 무엇부터 하면 좋을지 알려 주세요."
GUARD_PASS = '{"hits": []}'
FRONTIER_ANSWER = "경력 기술서를 먼저 정리해 보세요."
ANSWER = "경력 기술서부터 정리하고 지원할 회사를 좁혀 보세요."


class RoutedLlama:
    """llama_wrapper.Llama 대역. 시스템 프롬프트로 작업을 알아내 정해 둔 출력을 낸다.

    outputs[작업 이름]은 차례로 쓰고, 마지막 출력은 이후 요청에도 계속 쓴다.
    """

    def __init__(self, outputs: dict[str, list[str]]) -> None:
        prompts = PromptManager()
        self._task_of = {prompts.get_prompt(idx): idx.name for idx in PromptIdx}
        self._task_of["Answer briefly."] = "CHECK"  # Runner의 연결 확인
        self.outputs = {name: deque(values) for name, values in outputs.items()}
        self.outputs.setdefault("CHECK", deque(["OK"]))
        self.calls: list[str] = []
        self.inputs: list[tuple[str, str]] = []
        self.detect_prompts: list[str] = []
        self._base_detect = prompts.get_prompt(PromptIdx.DETECT)

    def tokenize(self, text: bytes, **kwargs: Any) -> list[int]:
        return list(range(len(text.decode("utf-8"))))

    def create_chat_completion(
        self, *, messages: list[dict[str, str]], **kwargs: Any
    ) -> Iterator[dict[str, Any]]:
        system = next(m["content"] for m in messages if m["role"] == "system")
        task = self._task_of.get(system)
        if system.startswith(self._base_detect):
            task = "DETECT"
            self.detect_prompts.append(system)
        if system.startswith("Summarize the conversation in at most"):
            task = "COMPACTION"
        assert task is not None, f"알 수 없는 시스템 프롬프트: {system[:40]}"
        self.calls.append(task)
        self.inputs.append((task, messages[-1]["content"]))
        queue = self.outputs.get(task)
        assert queue, f"{task} 작업의 출력을 정해 두지 않았습니다"
        output = queue.popleft() if len(queue) > 1 else queue[0]
        yield {
            "choices": [{"delta": {"content": output}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def close(self) -> None:
        pass


class Gateway:
    """프론티어 게이트웨이 대역 (httpx.MockTransport 처리기)."""

    def __init__(self, *, online: bool = True) -> None:
        self.online = online
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.online:
            return httpx.Response(503)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        assert request.url.path.endswith("/responses")
        assert body["store"] is False
        assert "previous_response_id" not in body
        self.requests.append(body["input"][-1]["content"])
        return httpx.Response(
            200,
            json={
                "id": f"resp_{len(self.requests)}",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": FRONTIER_ANSWER}],
                    }
                ],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        )


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fc, "RETRY_DELAYS", (0.0, 0.0))


@pytest.fixture
def make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., tuple[TestClient, RoutedLlama, Gateway]]]:
    clients: list[TestClient] = []
    closed: set[int] = set()

    def make(
        outputs: dict[str, list[str]], *, gateway: Gateway | None = None
    ) -> tuple[TestClient, RoutedLlama, Gateway]:
        model = RoutedLlama(outputs)
        monkeypatch.setattr(
            "src.localLLM_API_proxy.llama_wrapper.Llama", Mock(return_value=model)
        )
        gateway = gateway or Gateway()

        def frontier(prompts: PromptManager) -> FrontierClient:
            return FrontierClient(
                prompts,
                api_key="sk-test",
                base_url="https://gateway.test/v1",
                env_file=None,
                transport=httpx.MockTransport(gateway),
            )

        client = TestClient(
            create_app(db_path=tmp_path / "chat.sqlite3", frontier_factory=frontier)
        )
        client.__enter__()
        clients.append(client)
        return client, model, gateway

    def close(client: TestClient) -> None:
        if id(client) not in closed:
            closed.add(id(client))
            client.__exit__(None, None, None)

    make.close = close  # type: ignore[attr-defined]
    yield make
    for client in clients:
        close(client)


def parse_sse(body: str) -> list[dict[str, Any]]:
    """SSE 본문을 [{id, event, data}] 목록으로 바꾼다. 연결 유지 주석은 건너뛴다."""
    events = []
    for chunk in body.strip().split("\n\n"):
        event: dict[str, Any] = {"id": None}
        for line in chunk.splitlines():
            key, _, value = line.partition(": ")
            if key == "id":
                event["id"] = int(value)
            elif key == "event":
                event["event"] = value
            elif key == "data":
                event["data"] = json.loads(value)
        if "event" in event:
            events.append(event)
    return events


def wait_for(fn: Callable[[], T | None], timeout: float = 10.0) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("시간 안에 조건을 만족하지 못했습니다.")


def start(client: TestClient, text: str = QUESTION) -> tuple[int, int]:
    chat_id = client.post("/api/chats").json()["chatId"]
    res = client.post(f"/api/chats/{chat_id}/messages", json={"text": text})
    assert res.status_code == 200, res.text
    return chat_id, res.json()["runId"]


def events_of(client: TestClient, run_id: int, **headers: str) -> list[dict[str, Any]]:
    """실행이 끝날 때까지의 스트림을 받는다. (끝나지 않을 실행에는 쓰지 않는다)"""
    res = client.get(f"/api/runs/{run_id}/events", headers=headers)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    return parse_sse(res.text)


def wait_finished(client: TestClient, run_id: int) -> None:
    wait_for(lambda: client.get(f"/api/runs/{run_id}").json()["status"] == "finished")


def pending_consent(client: TestClient, run_id: int) -> dict[str, Any]:
    def pending() -> dict[str, Any] | None:
        consents = client.get(f"/api/runs/{run_id}").json()["consents"]
        return next((c for c in consents if c["result"] is None), None)

    return wait_for(pending)


def answer(client: TestClient, run_id: int, consent_id: int, text: str) -> Any:
    return client.post(
        f"/api/runs/{run_id}/consent", json={"consentId": consent_id, "text": text}
    )


SIMPLE_RUN = {
    "DETECT": [DETECT_NONE],
    "NEEDS": [NEEDS],
    "JUDGE": [JUDGE_ASK_NOTHING, JUDGE_ENOUGH],
    "WRITE": [REQUEST],
    "GUARD": [GUARD_PASS],
    "ANSWER": [ANSWER],
}

CONSENT_RUN = {
    "DETECT": [DETECT_TWO],
    "NEEDS": [NEEDS],
    "CONSENT": [CONSENT_TEXT],
    "WRITE": [REQUEST],
    "GUARD": [GUARD_PASS],
    "ANSWER": [ANSWER],
}


# API-01 ~ 04 채팅


def test_chat_create_list_detail_delete(make_client: Any) -> None:
    client, _, _ = make_client(SIMPLE_RUN)
    first = client.post("/api/chats").json()["chatId"]
    second = client.post("/api/chats").json()["chatId"]
    assert [c["chatId"] for c in client.get("/api/chats").json()] == [second, first]

    detail = client.get(f"/api/chats/{first}").json()
    assert detail == {"chatId": first, "title": "", "messages": [], "runs": []}

    assert client.delete(f"/api/chats/{first}").status_code == 200
    res = client.get(f"/api/chats/{first}")
    assert res.status_code == 404
    assert res.json()["code"] == "NOT_FOUND"


def test_errors_use_spec_format(make_client: Any) -> None:
    client, _, _ = make_client(SIMPLE_RUN)
    assert client.get("/api/chats/0").json()["code"] == "VALIDATION_ERROR"
    assert client.get("/api/nowhere").json() == {
        "code": "NOT_FOUND",
        "message": "없는 주소입니다.",
    }
    chat_id = client.post("/api/chats").json()["chatId"]
    res = client.post(
        f"/api/chats/{chat_id}/messages",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 422
    res = client.post(f"/api/chats/{chat_id}/messages", json={"text": "  "})
    assert res.json()["code"] == "VALIDATION_ERROR"
    assert client.post(
        "/api/chats/999/messages", json={"text": "질문"}
    ).status_code == (404)


# API-05 · 06 · 11 실행과 진행 상황


def test_run_streams_steps_frontier_tokens_and_done(make_client: Any) -> None:
    client, model, gateway = make_client(SIMPLE_RUN)
    chat_id, run_id = start(client, "이직 준비는 어떻게 하나요")

    # 진행 중에 연결하면 기록을 다시 보낸 뒤 replay_complete(ID 없음)를 한 번 보낸다
    events = [e for e in events_of(client, run_id) if e["event"] != "replay_complete"]
    names = [e["event"] for e in events]
    steps = [e["data"]["label"] for e in events if e["event"] == "step"]
    assert steps[:3] == ["개인정보 검출 중", "허용 설정 적용 중", "필요한 정보 정리 중"]
    assert "외부 모델에 묻는 중 (1회차)" in steps
    # 저장된 이벤트 ID로 보내고, 내부 기록(run·llm_call)은 화면에 보내지 않는다
    ids = [e["id"] for e in events]
    assert None not in ids and ids == sorted(ids)
    assert set(names) <= {"step", "frontier", "token", "done"}

    frontier = [e["data"] for e in events if e["event"] == "frontier"]
    assert frontier == [{"request": REQUEST, "answer": FRONTIER_ANSWER}]
    assert gateway.requests == [REQUEST]  # 실제 FrontierClient가 보낸 요청문
    tokens = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert tokens == ANSWER
    assert events[-1] == {
        "id": ids[-1],
        "event": "done",
        "data": {"endReason": "info_enough", "limited": None, "answerMessageId": 2},
    }

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "finished"
    assert detail["endReason"] == "info_enough"
    assert detail["answerMessageId"] == 2
    assert [
        (q["round"], q["request"], q["answer"], q["status"])
        for q in detail["frontierQueries"]
    ] == [(1, REQUEST, FRONTIER_ANSWER, "ok")]

    chat = client.get(f"/api/chats/{chat_id}").json()
    assert chat["title"] == "이직 준비는 어떻게 하나요"
    assert [(m["role"], m["content"]) for m in chat["messages"]] == [
        ("user", "이직 준비는 어떻게 하나요"),
        ("assistant", ANSWER),
    ]
    assert chat["runs"][0]["status"] == "finished"
    # 모든 단계가 실제 모듈을 거쳐 로컬 모델에 요청했다
    assert {"DETECT", "NEEDS", "JUDGE", "WRITE", "ANSWER"} <= set(model.calls)


def test_reconnect_resends_only_after_last_event_id(make_client: Any) -> None:
    client, _, _ = make_client(SIMPLE_RUN)
    _, run_id = start(client)
    wait_finished(client, run_id)
    # 끝난 실행은 저장된 기록만 보내고 닫는다 (replay_complete 없음)
    events = events_of(client, run_id)
    assert all(e["id"] is not None for e in events)
    middle = events[len(events) // 2]["id"]

    again = events_of(client, run_id, **{"Last-Event-ID": str(middle)})
    assert again == [e for e in events if e["id"] > middle]

    res = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "x"})
    assert res.status_code == 422
    last = events[-1]["id"]
    res = client.get(
        f"/api/runs/{run_id}/events", headers={"Last-Event-ID": str(last + 1000)}
    )
    assert res.status_code == 422
    assert client.get("/api/runs/999/events").status_code == 404


def test_offline_frontier_answers_with_local_model(make_client: Any) -> None:
    client, _, gateway = make_client(SIMPLE_RUN, gateway=Gateway(online=False))
    _, run_id = start(client)
    done = events_of(client, run_id)[-1]
    assert done["event"] == "done"
    assert done["data"]["endReason"] == "offline"
    assert done["data"]["limited"] == LIMITED_MESSAGES[EndReason.OFFLINE]
    assert gateway.requests == []


# API-07 · 08 동의


def test_consent_partial_allow_masks_denied_item(make_client: Any) -> None:
    outputs = {
        **CONSENT_RUN,
        # 동의 요청 → 일부 거절 후 다시 판단(묻지 않음) → 정보 충분
        "JUDGE": [JUDGE_ASK_BOTH, JUDGE_ASK_NOTHING, JUDGE_ENOUGH],
        "INTERPRET_CONSENT": [
            '{"decisions": [{"pdId": 1, "allow": true}, {"pdId": 2, "allow": false}]}'
        ],
    }
    client, model, _ = make_client(outputs)
    chat_id, run_id = start(client)

    consent = pending_consent(client, run_id)
    assert [(i["pdId"], i["text"], i["label"]) for i in consent["items"]] == [
        (1, "27살", "속성정보"),
        (2, "삼성전자", "속성정보"),
    ]
    assert (consent["purpose"], consent["benefit"], consent["risk"]) == (
        "목적",
        "이점",
        "위험",
    )

    # 로컬 모델이 하나라 다른 질문은 받지 않고, 실행 중인 채팅은 지우지 못한다
    other = client.post("/api/chats").json()["chatId"]
    busy = client.post(f"/api/chats/{other}/messages", json={"text": "다른 질문"})
    assert (busy.status_code, busy.json()["code"]) == (409, "RUN_IN_PROGRESS")
    running = client.delete(f"/api/chats/{chat_id}")
    assert (running.status_code, running.json()["code"]) == (409, "CHAT_RUNNING")

    reply = answer(client, run_id, consent["consentId"], "나이는 공개하고 직장은 빼줘")
    assert reply.json() == {
        "ok": True,
        "decisions": [{"pdId": 1, "allow": True}, {"pdId": 2, "allow": False}],
        "message": "공개: 27살 · 비공개: 삼성전자",
    }
    assert "INTERPRET_CONSENT" in model.calls  # 자연어 응답은 로컬 모델이 해석

    events = events_of(client, run_id)
    names = [e["event"] for e in events]
    assert names.index("consent_request") < names.index("consent_result")
    result = next(e["data"] for e in events if e["event"] == "consent_result")
    assert result["result"] == "answered"
    assert events[-1]["data"]["endReason"] == "info_enough"

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["consents"][0]["result"] == "answered"
    assert detail["consents"][0]["responseText"] == "나이는 공개하고 직장은 빼줘"
    # 일부 거절 뒤 바로 보내지 않고 다시 판단했고(외부 질의 1번),
    # 거절한 직장은 다시 묻지 않았다 (동의 요청 1번)
    assert model.calls.count("JUDGE") == 3
    assert len(detail["frontierQueries"]) == 1
    assert len(detail["consents"]) == 1


def test_quick_deny_all_sends_only_generalized_request(make_client: Any) -> None:
    safe_request = "직장인의 일반적인 이직 준비 방법을 알려 주세요."
    client, model, gateway = make_client(
        {
            **CONSENT_RUN,
            # 재판단에서 모델이 같은 항목을 요구해도 동의는 한 번만 묻는다.
            "JUDGE": [JUDGE_ASK_BOTH, JUDGE_ASK_BOTH, JUDGE_ENOUGH],
            "WRITE": ["삼전 직원의 이직 준비 방법을 알려 주세요."],
            "GUARD": ['{"hits": [1]}', GUARD_PASS],
            "REWRITE": [safe_request],
        }
    )
    _, run_id = start(client)
    consent = pending_consent(client, run_id)

    reply = answer(client, run_id, consent["consentId"], "모두 공개하지 마")
    assert reply.json()["decisions"] == [
        {"pdId": 1, "allow": False},
        {"pdId": 2, "allow": False},
    ]
    assert "INTERPRET_CONSENT" not in model.calls  # 빠른 응답은 모델 없이

    wait_finished(client, run_id)
    events = events_of(client, run_id)
    assert [e["event"] for e in events].count("consent_request") == 1
    assert model.calls.count("REWRITE") == 1
    assert gateway.requests == [safe_request]
    assert [e["data"] for e in events if e["event"] == "frontier"] == [
        {"request": safe_request, "answer": FRONTIER_ANSWER}
    ]
    assert events[-1]["data"]["endReason"] == "info_enough"


def test_unclear_consent_answer_keeps_waiting(make_client: Any) -> None:
    outputs = {
        **CONSENT_RUN,
        "JUDGE": [JUDGE_ASK_BOTH, JUDGE_ENOUGH],
        "INTERPRET_CONSENT": ['{"decisions": []}'],
    }
    client, _, _ = make_client(outputs)
    _, run_id = start(client)
    consent = pending_consent(client, run_id)

    unclear = answer(client, run_id, consent["consentId"], "글쎄요")
    assert unclear.json() == {
        "ok": False,
        "decisions": None,
        "message": UNCLEAR_MESSAGE,
    }
    wrong = answer(client, run_id, consent["consentId"] + 1, "모두 공개해도 돼")
    assert (wrong.status_code, wrong.json()["code"]) == (409, "NO_PENDING_CONSENT")

    assert answer(client, run_id, consent["consentId"], "모두 공개해도 돼").json()["ok"]
    assert events_of(client, run_id)[-1]["data"]["endReason"] == "info_enough"


def test_stop_while_waiting_consent(make_client: Any) -> None:
    client, _, _ = make_client({**CONSENT_RUN, "JUDGE": [JUDGE_ASK_BOTH]})
    _, run_id = start(client)
    consent = pending_consent(client, run_id)

    assert client.post(f"/api/runs/{run_id}/stop").status_code == 200
    events = events_of(client, run_id)
    assert [e["event"] for e in events[-2:]] == ["consent_result", "done"]
    assert events[-2]["data"] == {
        "consentId": consent["consentId"],
        "result": "cancelled",
    }
    assert events[-1]["data"]["endReason"] == "stopped"

    assert client.get(f"/api/runs/{run_id}").json()["status"] == "finished"
    again = client.post(f"/api/runs/{run_id}/stop")
    assert (again.status_code, again.json()["code"]) == (409, "RUN_FINISHED")
    late = answer(client, run_id, consent["consentId"], "모두 공개해도 돼")
    assert (late.status_code, late.json()["code"]) == (409, "RUN_FINISHED")


# API-09 · 10 공개 허용 설정


def test_privacy_setting_is_parsed_saved_and_applied(make_client: Any) -> None:
    outputs = {
        **SIMPLE_RUN,
        "DETECT": [DETECT_TWO],
        "PARSE_RULES": [
            '{"rules": [{"target": "나이", "scope": "category", "action": "allow", "detect_en": "Age"}]}',
            '{"rules": []}',
        ],
        # 27살은 나이 규칙으로 허용, 삼성전자는 대응 규칙 없음
        "MATCH_RULES": [
            '{"matches": [{"item": 0, "rules": [0]}, {"item": 1, "rules": []}]}'
        ],
    }
    client, model, _ = make_client(outputs)
    assert client.get("/api/settings/privacy").json() == {
        "settingId": None,
        "text": "",
        "rules": [],
        "updatedAt": None,
    }

    saved = client.put("/api/settings/privacy", json={"text": "나이는 공개해도 돼"})
    rule = {"target": "나이", "scope": "category", "action": "allow"}
    assert saved.json()["ok"] is True
    assert saved.json()["rules"] == [rule]

    # 해석하지 못한 설정문은 저장하지 않고 기존 규칙을 돌려준다
    unclear = client.put("/api/settings/privacy", json={"text": "날씨 알려줘"})
    assert unclear.json()["ok"] is False
    assert unclear.json()["rules"] == [rule]
    setting = client.get("/api/settings/privacy").json()
    assert (setting["text"], setting["rules"]) == ("나이는 공개해도 돼", [rule])

    _, run_id = start(client)
    events_of(client, run_id)
    query = client.get(f"/api/runs/{run_id}").json()["frontierQueries"][0]
    assert query["status"] == "ok"
    # 실행 시작 때 저장된 설정을 로컬 모델이 항목에 대응시키고, 남은 개인정보로 누출 검사를 했다
    assert model.calls.count("PARSE_RULES") == 2
    assert "MATCH_RULES" in model.calls
    assert "GUARD" in model.calls
    assert model.detect_prompts == [PromptManager().get_prompt(PromptIdx.DETECT)]


def test_custom_rule_reaches_consent_and_survives_restart(make_client: Any) -> None:
    rule = {
        "target": "삼성 공정 수치",
        "scope": "category",
        "action": "deny",
        "detect_en": "Process measurements at 삼성 (Samsung)",
    }
    client, _, _ = make_client({"PARSE_RULES": [json.dumps({"rules": [rule]})]})
    saved = client.put(
        "/api/settings/privacy", json={"text": "삼성 공정 수치는 보호해 줘"}
    )
    assert saved.json()["ok"]
    make_client.close(client)

    safe_request = "공정의 에너지 효율을 개선하는 일반적인 방법을 알려 주세요."
    client, model, gateway = make_client(
        {
            **CONSENT_RUN,
            "DETECT": ['{"items":[{"text":"0.62V","category":3}]}'],
            "MATCH_RULES": ['{"matches":[{"item":0,"rules":[0]}]}'],
            "NEEDS": ['{"needs":["공정 개선 방법"]}'],
            "JUDGE": [
                JUDGE_ASK_NOTHING,
                '{"enough":false,"needConsent":true,"consentItems":[1],"needIdx":[0]}',
                JUDGE_ENOUGH,
            ],
            "WRITE": ["동작 전압이 0.62V인 공정을 개선하려면 어떻게 해야 하나요?"],
            "GUARD": ['{"hits":[0]}', GUARD_PASS],
            "REWRITE": [safe_request],
            "INTERPRET_CONSENT": ['{"decisions":[{"pdId":1,"allow":false}]}'],
        }
    )
    _, run_id = start(client, "삼성 공정의 동작 전압은 0.62V야. 개선안을 알려 줘.")
    consent = pending_consent(client, run_id)
    assert rule["detect_en"] in model.detect_prompts[0]
    assert consent["items"] == [
        {"pdId": 1, "text": "0.62V", "label": "기타", "category": 3}
    ]
    reply = answer(client, run_id, consent["consentId"], "공개하지 마")
    assert reply.json()["decisions"] == [{"pdId": 1, "allow": False}]
    wait_finished(client, run_id)
    assert model.calls.count("REWRITE") == 1
    assert gateway.requests == [safe_request]


def test_history_survives_restart(make_client: Any) -> None:
    client, _, _ = make_client(SIMPLE_RUN)
    chat_id, run_id = start(client, "다시 켜도 남아야 하는 질문")
    events_of(client, run_id)
    make_client.close(client)  # 서버 종료

    restarted, _, _ = make_client(SIMPLE_RUN)
    assert [c["chatId"] for c in restarted.get("/api/chats").json()] == [chat_id]
    chat = restarted.get(f"/api/chats/{chat_id}").json()
    assert chat["messages"][0]["content"] == "다시 켜도 남아야 하는 질문"
    assert restarted.get(f"/api/runs/{run_id}").json()["status"] == "finished"
    # 끝난 실행의 스트림은 DB 기록으로 다시 보낸다
    assert events_of(restarted, run_id)[-1]["event"] == "done"


@pytest.mark.parametrize(
    "stage", ["DETECT", "NEEDS", "JUDGE", "WRITE", "GUARD", "REWRITE", "ANSWER"]
)
def test_failed_model_output_keeps_code_stage_and_never_sends_unchecked_request(
    make_client: Any, stage: str
) -> None:
    outputs = {**SIMPLE_RUN, "DETECT": [DETECT_TWO]}
    outputs[stage] = [""] if stage in {"WRITE", "REWRITE", "ANSWER"} else ["not-json"]
    if stage == "REWRITE":
        outputs["GUARD"] = ['{"hits": [0]}']
    client, _, gateway = make_client(outputs)
    chat_id, run_id = start(client)
    events = events_of(client, run_id)
    terminal = events[-1]
    detail = client.get(f"/api/runs/{run_id}").json()
    assert terminal["event"] == "error"
    assert terminal["data"]["code"] == "MODEL_RESPONSE_INVALID"
    assert terminal["data"]["stage"] == stage
    assert detail["errorCode"] == terminal["data"]["code"]
    assert detail["errorStage"] == stage
    assert detail["notice"] == terminal["data"]["message"]
    assert detail["endReason"] is None
    if stage != "ANSWER":
        assert gateway.requests == []
    assert client.get(f"/api/chats/{chat_id}").status_code == 200


def test_external_fallback_has_diagnostics_without_terminal_error(
    make_client: Any,
) -> None:
    client, _, _ = make_client(SIMPLE_RUN, gateway=Gateway(online=False))
    _, run_id = start(client)
    assert events_of(client, run_id)[-1]["data"]["endReason"] == "offline"
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["errorCode"] is None
    store = client.app.state.pipeline.services.chat_manager
    diagnostics = [
        e.payload for e in store.get_events(run_id) if e.type == "diagnostic"
    ]
    assert any(
        e["code"] == "FRONTIER_UNAVAILABLE" and e["recovered"] is False
        for e in diagnostics
    )
    assert any(
        e["code"] == "FRONTIER_UNAVAILABLE" and e["recovered"] is True
        for e in diagnostics
    )


def test_primary_and_cleanup_failures_do_not_expose_exception_text(
    make_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models.errors import ErrorCode, ErrorStage, PipelineError

    client, _, gateway = make_client(SIMPLE_RUN)
    services = client.app.state.pipeline.services
    secret = "/private/secret-path token=private-token"

    def fail_judge(*args: Any, **kwargs: Any) -> None:
        raise PipelineError(ErrorCode.LOCAL_LLM_TIMEOUT, ErrorStage.JUDGE, secret)

    def fail_cleanup(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(services.main.planner, "judge", fail_judge)
    monkeypatch.setattr(services.main.consent_manager, "release", fail_cleanup)
    chat_id, run_id = start(client)
    events = events_of(client, run_id)
    detail = client.get(f"/api/runs/{run_id}")
    chat = client.get(f"/api/chats/{chat_id}")
    assert events[-1]["data"]["code"] == "LOCAL_LLM_TIMEOUT"
    assert events[-1]["data"]["stage"] == "JUDGE"
    assert detail.json()["errorCode"] == "LOCAL_LLM_TIMEOUT"
    assert detail.json()["errorStage"] == "JUDGE"
    assert secret not in json.dumps(events) + detail.text + chat.text
    errors = [
        e.payload
        for e in services.chat_manager.get_events(run_id)
        if e.type == "run_error"
    ]
    assert [e["code"] for e in errors] == ["LOCAL_LLM_TIMEOUT", "CLEANUP_ERROR"]
    assert gateway.requests == []


@pytest.mark.parametrize("restore", ["warm", "evicted", "restart", "compacted"])
def test_followup_model_inputs_use_prior_question_and_delivered_answer(
    make_client: Callable[..., tuple[TestClient, RoutedLlama, Gateway]], restore: str
) -> None:
    client, model, gateway = make_client(SIMPLE_RUN)
    question = "AI glasses exam cheating news"
    chat, first = start(client, question)
    events_of(client, first)
    services = client.app.state.pipeline.services
    runner = services.main.local_llm_runner
    store = services.chat_manager
    if restore == "compacted":
        # The summary must be the actual input after a cold restore.
        model.outputs["COMPACTION"] = deque(["AI glasses"])
        runner.get_chat_context_manager(chat)._limit = 80
    if restore == "evicted":
        for _ in range(9):
            runner.get_chat_context_manager(store.create_chat())
        assert chat not in runner._chat_contexts
    if restore == "restart":
        make_client.close(client)
        client, model, gateway = make_client(SIMPLE_RUN)
        runner = client.app.state.pipeline.services.main.local_llm_runner
    model.outputs["JUDGE"] = deque([JUDGE_ASK_NOTHING, JUDGE_ENOUGH])
    model.inputs.clear()
    followup = "Actual links please"
    response = client.post(f"/api/chats/{chat}/messages", json={"text": followup})
    second = response.json()["runId"]
    events_of(client, second)
    detail = client.get(f"/api/runs/{second}").json()
    assert detail["errorCode"] is None
    for task in ["NEEDS", "JUDGE", "WRITE", "ANSWER"]:
        inputs = [text for stage, text in model.inputs if stage == task]
        assert inputs, task
        for text in inputs:
            expected_history = "AI glasses" if restore == "compacted" else question
            assert expected_history in text
            previous, current = text.split("[현재 입력]\n", 1)
            assert followup not in previous
            assert followup in current
            if task == "NEEDS":
                assert current.count(followup) == 1
            if restore != "compacted":
                assert text.count(ANSWER) == 1
    assert [text for task, text in model.inputs if task == "DETECT"] == [followup]
    assert all(
        "[user]" not in text
        for task, text in model.inputs
        if task in {"GUARD", "REWRITE"}
    )
    assert runner._chat_bindings == {}
    assert runner.context_managers == {}
    assert gateway.requests[-1] == REQUEST
    if restore == "compacted":
        compacted_input = [text for task, text in model.inputs if task == "NEEDS"][0]
        runner.forget_chat(chat)
        manager = runner.get_chat_context_manager(chat)
        # Same boundary and current input after cache loss use the adopted summary.
        needs_input = next(
            e["payload"]["input"]
            for e in [event.model_dump() for event in store.get_events(second)]
            if e["type"] == "llm_call" and e["payload"]["prompt_idx"] == "NEEDS"
        )
        assert (
            manager.prepare_input(needs_input, "sys", 2, second).text == compacted_input
        )
        make_client.close(client)
        client, _, _ = make_client(SIMPLE_RUN)
        restored = client.app.state.pipeline.services.main.local_llm_runner
        assert (
            restored.get_chat_context_manager(chat)
            .prepare_input(needs_input, "sys", 2, second)
            .text
            == compacted_input
        )


def test_prior_chat_is_not_injected_into_another_chat(
    make_client: Callable[..., tuple[TestClient, RoutedLlama, Gateway]],
) -> None:
    client, model, _ = make_client(SIMPLE_RUN)
    _, run = start(client, "unique previous question")
    events_of(client, run)
    model.inputs.clear()
    model.outputs["JUDGE"] = deque([JUDGE_ENOUGH])
    _, other = start(client, "different chat")
    events_of(client, other)
    assert all("unique previous question" not in text for _, text in model.inputs)
    assert all(ANSWER not in text for _, text in model.inputs)

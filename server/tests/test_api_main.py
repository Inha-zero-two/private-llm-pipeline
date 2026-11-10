"""PIPELINE=main 경로 검사: API → 실제 Main → 실제 PDDetector·Planner·MemoManager·Runner.

모델 전송(Llama)만 정해진 출력을 내는 대역으로 바꾸고, 없는 모듈은 stand_ins를 쓴다.
저장소 루트를 PYTHONPATH에 넣고 실행한다 (래퍼 src 패키지 import).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_api import events_of, wait_pending_consent

from app.api.app import create_app


class ScriptedLlama:
    """요청마다 정해 둔 출력을 차례로 돌려주는 모델 대역."""

    def __init__(self, outputs: list[str]) -> None:
        self.outputs: deque[str] = deque(["OK", *outputs])  # 첫 줄은 연결 확인
        self.messages: list[list[dict[str, str]]] = []

    def tokenize(self, text: bytes, **kwargs: Any) -> list[int]:
        return list(range(len(text.decode("utf-8"))))

    def create_chat_completion(
        self, *, messages: list[dict[str, str]], **kwargs: Any
    ) -> Iterator[dict[str, Any]]:
        self.messages.append(messages)
        yield {
            "choices": [
                {"delta": {"content": self.outputs.popleft()}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def close(self) -> None:
        pass


DETECT_AGE = '{"items": [{"text": "25살", "category": 2}]}'
NEEDS = '{"needs": ["진로 탐색 방법"]}'
JUDGE_NEED_AGE = (
    '{"enough": false, "needConsent": true, "consentItems": [1], '
    '"need": ["진로 탐색 방법"]}'
)
JUDGE_ENOUGH = '{"enough": true, "needConsent": false, "consentItems": [], "need": []}'
ANSWER = "25살이라면 관심 분야를 먼저 좁혀 보세요."


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    clients: list[TestClient] = []

    def make(outputs: list[str]) -> tuple[TestClient, ScriptedLlama]:
        model = ScriptedLlama(outputs)
        monkeypatch.setattr(
            "src.localLLM_API_proxy.llama_wrapper.Llama", Mock(return_value=model)
        )
        client = TestClient(create_app("main", delay=0))
        client.__enter__()
        clients.append(client)
        return client, model

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def start(client: TestClient, text: str) -> int:
    chat_id = client.post("/api/chats").json()["chatId"]
    res = client.post(f"/api/chats/{chat_id}/messages", json={"text": text})
    assert res.status_code == 200, res.text
    return res.json()["runId"]


def test_real_main_detects_asks_consent_and_answers(make_client: Any) -> None:
    client, model = make_client(
        [DETECT_AGE, NEEDS, JUDGE_NEED_AGE, JUDGE_ENOUGH, ANSWER]
    )
    run_id = start(client, "25살인데 진로 상담해줘")

    # 실제 PDDetector가 찾은 항목으로, 실제 Planner가 동의를 요청했다
    consent = wait_pending_consent(client, run_id)
    assert [(i["text"], i["category"]) for i in consent["items"]] == [("25살", 2)]
    assert consent["items"][0]["label"] == "속성정보"

    # 다른 질문은 동시에 받지 않는다 (로컬 모델이 하나)
    busy = client.post("/api/chats", json={}).json()["chatId"]
    res = client.post(f"/api/chats/{busy}/messages", json={"text": "다른 질문"})
    assert res.status_code == 409
    assert res.json()["code"] == "RUN_IN_PROGRESS"

    reply = client.post(
        f"/api/runs/{run_id}/consent",
        json={"consentId": consent["consentId"], "text": "공개해도 돼"},
    )
    assert reply.json()["ok"] is True

    events = events_of(client, run_id)
    names = [e["event"] for e in events if e["event"] != "replay_complete"]
    steps = [e["data"]["label"] for e in events if e["event"] == "step"]
    assert steps[:3] == ["개인정보 검출 중", "허용 설정 적용 중", "필요한 정보 정리 중"]
    assert "공개 동의를 기다리는 중" in steps
    assert names.count("frontier") == 1
    frontier = next(e["data"] for e in events if e["event"] == "frontier")
    assert "25살" in frontier["request"]  # 동의로 허용해서 가리지 않았다
    answer = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert answer == ANSWER  # 최종 답변은 로컬 모델 출력 그대로
    assert events[-1]["event"] == "done"
    assert events[-1]["data"]["endReason"] == "info_enough"

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "finished"
    assert detail["consents"][0]["result"] == "answered"
    chat = client.get(f"/api/chats/{detail['chatId']}").json()
    assert [m["content"] for m in chat["messages"]] == [
        "25살인데 진로 상담해줘",
        ANSWER,
    ]
    assert not model.outputs  # 준비한 모델 출력을 모두 썼다


def test_real_main_without_consent_keeps_item_masked(make_client: Any) -> None:
    client, _ = make_client([DETECT_AGE, NEEDS, JUDGE_NEED_AGE, "거부 후 답변"])
    run_id = start(client, "25살인데 진로 상담해줘")
    consent = wait_pending_consent(client, run_id)
    client.post(
        f"/api/runs/{run_id}/consent",
        json={"consentId": consent["consentId"], "text": "공개하지 마"},
    )
    # 모두 거부 → 외부 모델에 묻지 않고 consent_denied로 바로 최종 답변
    events = events_of(client, run_id)
    assert not any(e["event"] == "frontier" for e in events)
    done = events[-1]["data"]
    assert done["endReason"] == "consent_denied"
    assert done["limited"].startswith("공개 동의가 필요한 정보는")


def test_stop_while_waiting_consent_in_real_main(make_client: Any) -> None:
    client, _ = make_client([DETECT_AGE, NEEDS, JUDGE_NEED_AGE])
    run_id = start(client, "25살인데 진로 상담해줘")
    consent = wait_pending_consent(client, run_id)

    assert client.post(f"/api/runs/{run_id}/stop").status_code == 200
    events = events_of(client, run_id)
    assert [e["event"] for e in events[-2:]] == ["consent_result", "done"]
    assert events[-2]["data"]["consentId"] == consent["consentId"]
    assert events[-2]["data"]["result"] == "cancelled"
    assert events[-1]["data"]["endReason"] == "stopped"
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "finished"

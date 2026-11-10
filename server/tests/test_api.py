"""화면 ↔ 서버 API(가짜 실행) 테스트. 명세서 docs/screen-api-spec.md 기준."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.events import RunEvents

T = TypeVar("T")


@pytest.fixture
def client() -> Iterator[TestClient]:
    # delay=0: 가짜 실행이 기다리지 않고 바로 진행한다
    with TestClient(create_app(delay=0)) as c:
        yield c


def parse_sse(body: str) -> list[dict[str, Any]]:
    """SSE 본문을 [{id, event, data}] 목록으로 바꾼다."""
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
        events.append(event)
    return events


def wait_for(fn: Callable[[], T], timeout: float = 5.0) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("시간 안에 조건을 만족하지 못했습니다.")


def start(client: TestClient, text: str) -> tuple[int, int]:
    chat_id = client.post("/api/chats").json()["chatId"]
    res = client.post(f"/api/chats/{chat_id}/messages", json={"text": text})
    assert res.status_code == 200
    return chat_id, res.json()["runId"]


def events_of(client: TestClient, run_id: int, **headers: str) -> list[dict[str, Any]]:
    """실행이 끝날 때까지의 스트림을 받는다. (끝나지 않을 실행에는 쓰지 않는다)"""
    res = client.get(f"/api/runs/{run_id}/events", headers=headers)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    return parse_sse(res.text)


def wait_pending_consent(client: TestClient, run_id: int) -> dict[str, Any]:
    def pending() -> dict[str, Any] | None:
        consents = client.get(f"/api/runs/{run_id}").json()["consents"]
        return next((c for c in consents if c["result"] is None), None)

    return wait_for(pending)


# API-01 ~ 04


def test_chat_create_list_detail_delete(client: TestClient) -> None:
    assert client.get("/api/chats").json() == []
    first = client.post("/api/chats").json()["chatId"]
    second = client.post("/api/chats").json()["chatId"]
    assert (first, second) == (1, 2)
    # 최근 수정 순, 첫 질문 전에는 제목이 빈 문자열
    assert client.get("/api/chats").json() == [
        {"chatId": 2, "title": ""},
        {"chatId": 1, "title": ""},
    ]
    assert client.get("/api/chats/1").json() == {
        "chatId": 1,
        "title": "",
        "messages": [],
        "runs": [],
    }
    assert client.delete("/api/chats/1").status_code == 200
    res = client.get("/api/chats/1")
    assert res.status_code == 404
    assert res.json()["code"] == "NOT_FOUND"
    assert client.delete("/api/chats/1").json()["code"] == "NOT_FOUND"


def test_invalid_ids_are_validation_errors(client: TestClient) -> None:
    for path in ("/api/chats/abc", "/api/chats/0", "/api/runs/abc"):
        res = client.get(path)
        assert res.status_code == 422
        assert res.json()["code"] == "VALIDATION_ERROR"


def test_errors_from_framework_use_spec_format(client: TestClient) -> None:
    chat_id = client.post("/api/chats").json()["chatId"]
    broken = client.post(
        f"/api/chats/{chat_id}/messages",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert broken.status_code == 422
    assert broken.json()["code"] == "VALIDATION_ERROR"
    missing = client.post(f"/api/chats/{chat_id}/messages", json={})
    assert missing.json()["code"] == "VALIDATION_ERROR"
    assert client.get("/api/nothing").json()["code"] == "NOT_FOUND"


# API-05 · 06 기본 흐름


def test_normal_run_streams_steps_frontier_tokens_and_done(client: TestClient) -> None:
    chat_id, run_id = start(client, "진로 상담해줘")
    events = events_of(client, run_id)
    names = [e["event"] for e in events if e["event"] != "replay_complete"]

    assert names[0] == "step"
    assert names.count("frontier") == 2
    assert names[-1] == "done"
    done = events[-1]["data"]
    assert done["endReason"] == "info_enough"
    assert done["limited"] is None

    # 기록 이벤트의 ID는 1부터 빈틈없이 증가한다
    ids = [e["id"] for e in events if e["event"] != "replay_complete"]
    assert ids == list(range(1, len(ids) + 1))

    answer = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    detail = client.get(f"/api/chats/{chat_id}").json()
    assert detail["title"] == "진로 상담해줘"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == answer
    assert detail["messages"][1]["messageId"] == done["answerMessageId"]
    assert detail["runs"] == [
        {
            "runId": run_id,
            "userMessageId": detail["messages"][0]["messageId"],
            "status": "finished",
            "notice": None,
        }
    ]

    run = client.get(f"/api/runs/{run_id}").json()
    assert [q["round"] for q in run["frontierQueries"]] == [1, 2]
    assert run["endReason"] == "info_enough"


def test_empty_question_is_rejected(client: TestClient) -> None:
    chat_id = client.post("/api/chats").json()["chatId"]
    res = client.post(f"/api/chats/{chat_id}/messages", json={"text": "   "})
    assert res.status_code == 422
    assert res.json() == {
        "code": "VALIDATION_ERROR",
        "message": "질문을 입력해 주세요.",
    }
    res = client.post("/api/chats/99/messages", json={"text": "안녕"})
    assert res.status_code == 404


def test_scenarios_offline_limit_and_error(client: TestClient) -> None:
    _, offline = start(client, "오프라인 상황")
    assert events_of(client, offline)[-1]["data"]["endReason"] == "offline"

    _, limited = start(client, "상한 테스트")
    done = events_of(client, limited)[-1]["data"]
    assert done["endReason"] == "call_limit"
    assert done["limited"].startswith("질문 횟수 한도")

    chat_id, failed = start(client, "오류 내줘")
    last = events_of(client, failed)[-1]
    assert last["event"] == "error"
    assert last["data"]["code"] == "LOCAL_LLM_ERROR"
    summary = client.get(f"/api/chats/{chat_id}").json()["runs"][0]
    assert summary["notice"] == last["data"]["message"]
    assert client.get(f"/api/runs/{failed}").json()["errorCode"] == "LOCAL_LLM_ERROR"


# API-06 재접속


def test_reconnect_resends_only_after_last_event_id(client: TestClient) -> None:
    _, run_id = start(client, "진로 상담해줘")
    full = events_of(client, run_id)
    resumed = events_of(client, run_id, **{"Last-Event-ID": "3"})
    assert resumed[0]["id"] == 4
    assert resumed == [e for e in full if e["id"] and e["id"] > 3]

    for bad in ("9999", "abc", "-1"):
        res = client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": bad})
        assert res.status_code == 422
    assert client.get("/api/runs/99/events").status_code == 404


def test_replay_complete_reports_pending_consent() -> None:
    async def scenario() -> list[str]:
        events = RunEvents()
        await events.push("step", {"label": "a"})
        await events.push("consent_request", {"consentId": 7, "items": []})
        stream = events.stream(0)
        out = [await anext(stream) for _ in range(3)]
        await events.push("done", {"endReason": "stopped"})
        out += [chunk async for chunk in stream]
        return out

    chunks = asyncio.run(scenario())
    assert chunks[2] == (
        'event: replay_complete\ndata: {"lastEventId": 2, "pendingConsentId": 7}\n\n'
    )
    assert chunks[3].startswith("id: 3\nevent: done")


# API-08 동의


def test_consent_allow_continues_with_item_unmasked(client: TestClient) -> None:
    _, run_id = start(client, "27살인데 진로 상담해줘")
    consent = wait_pending_consent(client, run_id)
    assert [(i["label"], i["text"]) for i in consent["items"]] == [("나이", "27살")]
    pd_id = consent["items"][0]["pdId"]
    url = f"/api/runs/{run_id}/consent"

    wrong = client.post(url, json={"consentId": 999, "text": "공개해도 돼"})
    assert wrong.status_code == 409
    assert wrong.json()["code"] == "NO_PENDING_CONSENT"

    unclear = client.post(url, json={"consentId": consent["consentId"], "text": "음"})
    assert unclear.status_code == 200
    assert unclear.json()["ok"] is False

    res = client.post(
        url, json={"consentId": consent["consentId"], "text": "나이는 공개해도 돼"}
    )
    assert res.json() == {
        "ok": True,
        "decisions": [{"pdId": pd_id, "allow": True}],
        "message": "모든 항목을 공개합니다.",
    }

    events = events_of(client, run_id)
    result = next(e for e in events if e["event"] == "consent_result")
    assert result["data"]["result"] == "answered"
    assert result["data"]["decisions"] == [{"pdId": pd_id, "allow": True}]
    first_request = next(e for e in events if e["event"] == "frontier")
    assert "27살" in first_request["data"]["request"]
    assert events[-1]["data"]["endReason"] == "info_enough"

    again = client.post(url, json={"consentId": consent["consentId"], "text": "네"})
    assert again.json()["code"] == "RUN_FINISHED"


def test_consent_deny_all_ends_with_consent_denied(client: TestClient) -> None:
    _, run_id = start(client, "제 이름은 홍길동이고 27살이에요")
    consent = wait_pending_consent(client, run_id)
    assert {i["label"] for i in consent["items"]} == {"이름", "나이"}
    res = client.post(
        f"/api/runs/{run_id}/consent",
        json={"consentId": consent["consentId"], "text": "다 빼줘"},
    )
    assert all(not d["allow"] for d in res.json()["decisions"])
    done = events_of(client, run_id)[-1]["data"]
    assert done["endReason"] == "consent_denied"
    assert done["answerMessageId"] is not None


# API-07 중지 · API-04 실행 중 삭제


def test_stop_while_waiting_consent(client: TestClient) -> None:
    chat_id, run_id = start(client, "27살인데 진로 상담해줘")
    consent = wait_pending_consent(client, run_id)

    running = client.delete(f"/api/chats/{chat_id}")
    assert running.status_code == 409
    assert running.json()["code"] == "CHAT_RUNNING"

    assert client.post(f"/api/runs/{run_id}/stop").status_code == 200
    events = events_of(client, run_id)
    assert [e["event"] for e in events[-2:]] == ["consent_result", "done"]
    assert events[-2]["data"] == {
        "consentId": consent["consentId"],
        "result": "cancelled",
    }
    assert events[-1]["data"] == {
        "endReason": "stopped",
        "limited": "중지되었습니다.",
        "answerMessageId": None,
    }

    stopped_again = client.post(f"/api/runs/{run_id}/stop")
    assert stopped_again.status_code == 409
    assert stopped_again.json()["code"] == "RUN_FINISHED"
    assert client.delete(f"/api/chats/{chat_id}").status_code == 200


# API-09 · 10 설정


def test_privacy_setting_save_and_apply_to_next_run(client: TestClient) -> None:
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
    current = client.get("/api/settings/privacy").json()
    assert current["settingId"] == 1
    assert current["rules"] == [rule]

    # 허용 설정이 있으면 나이에 대해 동의를 묻지 않는다
    _, run_id = start(client, "27살인데 진로 상담해줘")
    events = events_of(client, run_id)
    assert not any(e["event"] == "consent_request" for e in events)

    unclear = client.put("/api/settings/privacy", json={"text": "안녕하세요"})
    assert unclear.json()["ok"] is False
    assert unclear.json()["rules"] == [rule]  # 기존 설정 유지
    assert client.get("/api/settings/privacy").json()["settingId"] == 1

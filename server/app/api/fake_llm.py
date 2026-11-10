"""모델 파일·원격 키 없이 실제 파이프라인을 화면에서 돌려 보기 위한 가짜 모델 (LLM=fake).

llama_wrapper.Llama 자리에 끼운다. 그 위의 LocalLLMRunner · PDDetector · Planner ·
MemoManager · Main · API · web은 모두 실제 코드가 돈다. 이 클래스는 시스템 프롬프트를 보고
어느 단계의 요청인지 알아낸 뒤, 그 단계가 기대하는 형식의 출력을 규칙으로 만든다.

    연결 확인       → "OK"
    DETECT(검출)    → 전화번호 · 이름 · 나이 · 직장을 정규식으로 찾은 {"items": [...]}
    NEEDS(필요 정보) → 정해진 두 항목
    JUDGE(판단)     → 메모에 답이 있으면 충분, 없고 허용 안 된 개인정보가 있으면 동의 요청
    최종 답변       → 질문과 메모를 넣은 문장 (가짜임을 표시)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from app.api.fake_interpret import DETECTORS

NEEDS = ["질문에 답하는 데 필요한 일반 정보", "바로 실천할 수 있는 방법"]

# MemoManager는 need마다 "[답변 n]" 아래에 모은 답을 적는다
ANSWER_PATTERN = re.compile(r"\[답변 \d+\]\n(.*?)(?=\n\[답변 \d+\]|\n\nNeed:|\Z)", re.S)


def _detect(text: str) -> dict[str, Any]:
    items = []
    for d in DETECTORS:
        for m in d.pattern.finditer(text):
            value = m.group(1) if m.groups() else m.group(0)
            items.append({"text": value, "category": d.category})
    return {"items": items}


def _section(text: str, name: str) -> str:
    """'[이름]' 줄 아래부터 다음 '[...]' 구간 전까지를 꺼낸다."""
    m = re.search(rf"\[{name}\]\n(.*?)(?=\n\n\[(?!답변)|\Z)", text, re.S)
    return m.group(1) if m else ""


def _collected(memo: str) -> list[str]:
    """메모에 모인 답변들 (같은 답은 한 번만)."""
    found = (a.strip() for a in ANSWER_PATTERN.findall(memo))
    return list(dict.fromkeys(a for a in found if a))


def _judge(text: str) -> dict[str, Any]:
    if _collected(_section(text, "메모")):
        return {"enough": True, "needConsent": False, "consentItems": [], "need": []}
    hidden = [
        int(m.group(1))
        for m in re.finditer(r"id=(\d+), category=\d, text=.*?, allowed=False", text)
    ]
    return {
        "enough": False,
        "needConsent": bool(hidden),
        "consentItems": hidden,
        "need": NEEDS,
    }


def _answer(text: str) -> str:
    question = _section(text, "질문").strip()
    answers = "\n".join(f"- {a}" for a in _collected(_section(text, "메모")))
    lines = [
        "> (가짜 로컬 모델의 답변입니다. 실제 모델을 연결하면 이 자리에 진짜 답이 나옵니다.)",
        "",
        f"**질문**: {question}",
        "",
        "## 모은 정보",
        "",
        answers or "외부에서 모은 정보가 없습니다.",
        "",
        "## 정리",
        "",
        "1. 목표를 한 문장으로 정합니다.",
        "2. 지금 상황과 쓸 수 있는 자원을 정리합니다.",
        "3. 이번 주에 할 수 있는 일부터 시작합니다.",
    ]
    return "\n".join(lines)


def respond(system: str, user: str) -> str:
    """요청 하나에 대한 출력 문자열."""
    if system == "Answer briefly.":
        return "OK"
    if system.startswith("Extract all personal data"):
        return json.dumps(_detect(user), ensure_ascii=False)
    if system.startswith("Break down the user's question"):
        return json.dumps({"needs": NEEDS}, ensure_ascii=False)
    if system.startswith("Evaluate whether the collected answers"):
        return json.dumps(_judge(user), ensure_ascii=False)
    if "[질문]" in user:
        return _answer(user)
    return user[:200]  # 요약 등 그 밖의 요청은 입력을 줄여 돌려준다


class RuleBasedLlama:
    """llama_wrapper.Llama 대신 쓰는 가짜 모델. Runner가 부르는 메서드만 있다."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def tokenize(self, text: bytes, **kwargs: Any) -> list[int]:
        return list(range(len(text.decode("utf-8", errors="ignore"))))

    def create_chat_completion(
        self, *, messages: list[dict[str, str]], **kwargs: Any
    ) -> Iterator[dict[str, Any]]:
        system = messages[0]["content"] if messages else ""
        user = messages[-1]["content"] if messages else ""
        output = respond(system, user)
        for i in range(0, len(output), 4):  # 실제처럼 조금씩 흘려보낸다
            yield {"choices": [{"delta": {"content": output[i : i + 4]}}]}
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(system) + len(user), "completion_tokens": 1},
        }

    def close(self) -> None:
        pass

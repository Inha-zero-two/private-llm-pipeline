"""아직 구현되지 않은 모듈의 대역. 실제 Main에 끼워 화면과 끝까지 연결해 보기 위한 것이다.

이미 있는 모듈(LocalLLMRunner · PromptManager · PDDetector · Planner · MemoManager)은
실제 것을 쓰고, 아래 클래스는 각 담당자의 모듈이 생기면 하나씩 그것으로 바꾼다.
호출 규격은 Main이 부르는 방식(test_main.py의 가짜 모듈과 같은 모양)을 따른다.

| 대역                  | 설계서 모듈         | 지금 하는 일                                     |
| --------------------- | ------------------- | ------------------------------------------------ |
| StoreChatManager      | 6.3 ChatManager     | 메모리 저장소(store)에 메시지·실행·개인정보 표 기록 |
| ScreenConsentManager  | 4.3 ConsentManager  | 화면에 동의 요청(API-06)을 띄우고 API-08 응답 대기 |
| StorePrivacySettings  | 4.2 PrivacySettings | API-10에 저장된 규칙을 낱말 일치로 적용           |
| MaskingRequestWriter  | 3.2 RequestWriter   | 허용되지 않은 개인정보를 가린 요청문              |
| TextLeakGuard         | 3.3 LeakGuard       | 허용되지 않은 개인정보 원문이 요청문에 있는지 검사 |
| CannedFrontierClient  | 5.3 FrontierClient  | 외부 모델 대신 정해진 답                          |
| LocalAnswerGenerator  | 3.4 AnswerGenerator | 로컬 LLM으로 최종 답변을 만들어 흘려보냄          |
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from app.api import schemas
from app.api.consent_flow import ask_consent
from app.api.fake_interpret import DETECTORS
from app.api.store import Store
from app.models.enums import EndReason
from app.privacy.pd_detector import CATEGORY_LABEL, PDItem

if TYPE_CHECKING:
    from app.runtime.local_llm_runner import LocalLLMRunner

PDTable = list[PDItem]


def label_of(item: PDItem) -> str:
    """화면에 보일 항목 이름 (예: 나이, 직장).

    검출 결과(PDItem)에는 종류 이름이 없어 원문 모양으로 추정하고, 모르면 분류 이름을 쓴다.
    사용자가 "나이는 공개해도 돼"처럼 이름으로 답하므로 동의 해석에 필요하다.
    """
    for detector in DETECTORS:
        if detector.pattern.fullmatch(item.text):
            return detector.label
    return CATEGORY_LABEL.get(item.category, "개인정보")


def mask(text: str, pd_table: PDTable) -> str:
    """허용되지 않은 개인정보 원문을 [분류 이름]으로 가린다."""
    for item in sorted(pd_table, key=lambda i: len(i.text), reverse=True):
        if not item.allowed:
            text = text.replace(item.text, f"[{label_of(item)}]")
    return text


class StoreChatManager:
    """6.3 ChatManager 대역. Main은 ID를 문자열로, 저장소는 정수로 다룬다."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._tables: dict[str, PDTable] = {}  # 채팅별 개인정보 표

    def save_message(
        self, chat_id: str, role: Literal["user", "assistant"], content: str
    ) -> str:
        return str(self._store.save_message(int(chat_id), role, content).message_id)

    def start_run(
        self, chat_id: str, message_id: str, setting_id: int | None, config: Any
    ) -> str:
        return str(self._store.start_run(int(chat_id), int(message_id)).run_id)

    def set_pd_items(self, chat_id: str, message_id: str, items: PDTable) -> None:
        table = self._tables.setdefault(chat_id, [])
        known = {item.text for item in table}
        for item in items:
            if item.text not in known:  # 같은 채팅에서 이미 나온 정보는 한 번만
                item.pd_id = self._store.next_pd_id()
                table.append(item)
                known.add(item.text)

    def get_pd_table(self, chat_id: str) -> PDTable:
        return self._tables.setdefault(chat_id, [])

    def finish_run(
        self,
        run_id: str,
        end_reason: EndReason | None,
        answer_message_id: str | None,
        memo_final: str | None,
    ) -> None:
        run = self._store.runs.get(int(run_id))
        if run is not None:
            run.end_reason = end_reason.value if end_reason else None
            run.answer_message_id = (
                int(answer_message_id) if answer_message_id else None
            )


@dataclass
class ConsentRequest:
    run_id: str
    items: list[schemas.PDItem]
    purpose: str
    benefit: str
    risk: str


@dataclass
class ConsentResult:
    allowed_ids: set[int] = field(default_factory=set)

    @property
    def allow(self) -> bool:
        """하나라도 허용했으면 계속 진행, 모두 거부면 consent_denied."""
        return bool(self.allowed_ids)


class ScreenConsentManager:
    """4.3 ConsentManager 대역. 목적·이점·위험 문구는 아직 로컬 LLM 대신 정해진 문장."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def build_message(
        self, run_id: str, consent_items: list[int], need: list[str], pd_table: PDTable
    ) -> ConsentRequest | None:
        wanted = set(consent_items)
        items = [
            schemas.PDItem(
                pd_id=item.pd_id,
                label=label_of(item),
                text=item.text,
                category=item.category,
            )
            for item in pd_table
            if item.pd_id in wanted and not item.allowed and item.pd_id is not None
        ]
        if not items:
            return None
        topics = ", ".join(need) if need else "질문"
        return ConsentRequest(
            run_id=run_id,
            items=items,
            purpose=f"'{topics}'에 대해 외부 모델에 구체적으로 묻기 위해 필요합니다.",
            benefit="상황에 맞춘 더 정확한 답변을 받을 수 있습니다.",
            risk="허용한 정보가 외부 모델 서버로 전송됩니다.",
        )

    async def request(self, req: ConsentRequest) -> ConsentResult:
        run = self._store.runs[int(req.run_id)]
        decisions = await ask_consent(
            self._store,
            run,
            req.items,
            purpose=req.purpose,
            benefit=req.benefit,
            risk=req.risk,
        )
        return ConsentResult({d.pd_id for d in decisions if d.allow})

    def apply(
        self, result: ConsentResult, req: ConsentRequest, pd_table: PDTable
    ) -> None:
        asked = {item.pd_id for item in req.items}
        for item in pd_table:
            if item.pd_id in result.allowed_ids:
                item.allowed = True
                item.allowed_by = "consent"
            elif item.pd_id in asked:
                # 이번 실행에서 거부한 항목. Planner가 같은 실행에서 다시 묻지 않는다
                item.allowed_by = "denied"

    def release(self, run_id: str, pd_table: PDTable) -> None:
        """동의 결과(허용·거부)는 이번 실행에만 적용한다."""
        for item in pd_table:
            if item.allowed_by in ("consent", "denied"):
                item.allowed = False
                item.allowed_by = "none"

    def cancel(self, run_id: str) -> None:
        """대기 취소는 Main.stop()이 작업을 취소할 때 ask_consent가 처리한다."""


class StorePrivacySettings:
    """4.2 PrivacySettings 대역. 실제 모듈은 로컬 LLM이 규칙과 항목을 짝짓는다."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @property
    def setting_id(self) -> int | None:
        return self._store.setting.setting_id

    def load_rules(self) -> None:
        pass

    def apply_to_table(self, run_id: str, pd_table: PDTable) -> None:
        rules = self._store.setting.rules
        for item in pd_table:
            if item.allowed_by in (
                "setting",
                "denied",
            ):  # 지난 실행의 설정 결과는 지운다
                item.allowed = False
                item.allowed_by = "none"
            for rule in rules:
                names = (item.text, label_of(item))
                if any(rule.target in name or name in rule.target for name in names):
                    item.allowed = rule.action == "allow"
                    item.allowed_by = "setting" if item.allowed else "denied"


class MaskingRequestWriter:
    """3.2 RequestWriter 대역."""

    def write(
        self, question: str, memo: str, pd_table: PDTable, need: list[str]
    ) -> str:
        topics = "\n".join(f"- {n}" for n in need) or "- 질문에 답하는 데 필요한 정보"
        return (
            "다음 질문에 답하는 데 필요한 일반적인 정보를 알려 주세요.\n\n"
            f"질문: {mask(question, pd_table)}\n\n알고 싶은 것:\n{topics}"
        )

    def rewrite(
        self, previous: str, hits: list[Any], need: list[str], pd_table: PDTable
    ) -> str:
        return mask(previous, pd_table)


@dataclass
class GuardResult:
    passed: bool
    hits: list[int]


class TextLeakGuard:
    """3.3 LeakGuard 대역. 실제 모듈은 로컬 LLM이 바꿔 쓴 표현까지 찾는다."""

    def detect(self, run_id: str, request_text: str, pd_table: PDTable) -> GuardResult:
        hits = [
            item.pd_id
            for item in pd_table
            if not item.allowed and item.pd_id is not None and item.text in request_text
        ]
        return GuardResult(passed=not hits, hits=hits)


@dataclass
class FrontierReply:
    answer: str
    cost: float = 0.0


class CannedFrontierClient:
    """5.3 FrontierClient 대역. 외부 모델을 부르지 않는다."""

    def check_connection(self) -> bool:
        return True

    def send(self, prompt: str) -> FrontierReply:
        return FrontierReply(
            "(외부 모델 대역의 정해진 답) 목표를 먼저 정하고, 현재 상황을 정리한 뒤, "
            "우선순위에 따라 짧은 주기로 실행하고 점검하는 방식이 일반적으로 효과적입니다."
        )


ANSWER_SYSTEM_PROMPT = (
    "너는 사용자의 질문에 한국어로 답하는 도우미다. [메모]에는 외부에서 모은 일반 정보가 있다. "
    "[질문]의 개인 상황을 반영하고 [메모]를 참고해 구체적이고 실천 가능한 답을 마크다운으로 써라."
)


class LocalAnswerGenerator:
    """3.4 AnswerGenerator 대역. 로컬 LLM이 실제로 답을 만들고, 토큰을 바로 흘려보낸다."""

    def __init__(self, runner: LocalLLMRunner) -> None:
        self._runner = runner
        # Runner.stop(run_id)이 최종 답변 생성을 끊으려면 같은 실행 ID로 요청해야 한다.
        # generate()에는 실행 ID가 없어 Main 이벤트(run)를 받을 때 채운다. 실행은 한 번에 하나다.
        self.run_id: str | None = None

    def generate(self, question: str, memo: str) -> Iterator[str]:
        tokens: queue.Queue[str | None] = queue.Queue()
        failure: list[BaseException] = []

        def work() -> None:
            try:
                text, ok, *_ = self._runner.manual_request(
                    f"[질문]\n{question}\n\n[메모]\n{memo}",
                    ANSWER_SYSTEM_PROMPT,
                    run_id=self.run_id,
                    on_token=tokens.put,
                )
                if not ok and not text:
                    failure.append(
                        RuntimeError("로컬 모델이 답변을 만들지 못했습니다.")
                    )
            except BaseException as error:  # 스레드 밖의 Main에 그대로 전달한다
                failure.append(error)
            finally:
                tokens.put(None)

        threading.Thread(target=work, daemon=True).start()

        def stream() -> Iterator[str]:
            while (token := tokens.get()) is not None:
                yield token
            if failure:
                raise failure[0]

        return stream()

    def notify_limited(self, end_reason: EndReason) -> None:
        """제한 안내 문구는 API가 done 이벤트의 limited로 보낸다."""

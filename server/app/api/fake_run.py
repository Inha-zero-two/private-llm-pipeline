"""가짜 파이프라인. 설계서 흐름(①~⑧)대로 이벤트를 쌓아 실제 실행처럼 보이게 한다.

나중에 Main.run_pipeline()으로 바꿀 자리다. 바꿀 때 지켜야 할 약속은 하나다:
진행에 따라 run.events.push()로 명세서 API-06의 이벤트를 보내고, 끝나면 done 또는 error를 보낸다.

질문에 아래 낱말을 넣으면 해당 상황을 볼 수 있다 (web 데모와 같다).
    "오류" → error 이벤트로 끝남        "오프라인" → offline으로 끝남
    "상한" → call_limit으로 끝남        "27살" · 전화번호 · "제 이름은 홍길동" · "삼성전자" → 동의 요청
    "동의" → 위 항목이 없어도 예시 항목으로 동의 요청
"""

from __future__ import annotations

import asyncio
import random

from app.api.fake_interpret import detect, is_allowed, mask
from app.api.schemas import PDItem, Rule
from app.api.store import RunRecord, Store, now_iso
from app.models.enums import EndReason

# 상세 설계서 3.4 EndReason 표의 화면 안내
LIMITED_TEXT: dict[EndReason, str] = {
    EndReason.CALL_LIMIT: "질문 횟수 한도에 도달해 지금까지 모은 정보로 답했습니다.",
    EndReason.OFFLINE: (
        "외부 모델에 연결할 수 없어 현재까지 취합한 정보를 바탕으로 로컬 모델로 답했습니다."
    ),
    EndReason.STOPPED: "중지되었습니다.",
    EndReason.CONSENT_DENIED: (
        "공개 동의가 필요한 정보는 보내지 않고 지금까지 모은 정보로 답했습니다."
    ),
}
STOPPED_MARK = "\n\n(중지됨)"  # Main과 같은 표시

FRONTIER_ANSWERS = [
    "이런 요청에는 보통 목표를 먼저 정하고, 현재 상황을 정리한 뒤, "
    "우선순위에 따라 계획을 세우는 방식이 효과적입니다.",
    "단계별로는 (1) 현재 상태 점검 (2) 짧은 주기의 목표 설정 (3) 결과 확인과 수정을 "
    "반복하는 것이 좋습니다. 무리한 일정은 피하세요.",
    "마지막으로 진행 상황을 기록하고 주기적으로 돌아보면 계획을 고치기 쉽습니다.",
]

FINAL_ANSWER = "\n".join(
    [
        "질문하신 내용을 바탕으로 정리해 드릴게요.",
        "",
        "## 핵심 정리",
        "",
        "1. **목표 정하기** — 무엇을 이루고 싶은지 한 문장으로 적어 보세요.",
        "2. **현재 상황 점검** — 지금 가진 자원과 제약을 정리합니다.",
        "3. **계획 세우기** — 우선순위가 높은 것부터 짧은 주기로 나눕니다.",
        "",
        "## 다음 단계",
        "",
        "- 이번 주에 할 수 있는 일 한 가지를 정해 바로 시작해 보세요.",
        "- 진행 상황을 기록해 두면 계획을 고치기 쉽습니다.",
        "",
        "> 외부 모델에는 개인정보를 뺀 일반적인 질문만 보냈고, "
        "개인 상황은 이 PC의 로컬 모델이 반영했습니다.",
    ]
)


class FakeLocalLLMError(Exception):
    """'오류' 시나리오에서 로컬 모델 실패를 흉내 낸다."""


async def fake_run(
    store: Store, run: RunRecord, question: str, rules: list[Rule], delay: float
) -> None:
    """실행 하나를 끝까지 진행한다. API-05가 asyncio 작업으로 띄우고, API-07이 cancel한다."""
    events = run.events
    answer_parts: list[str] = []

    async def step(label: str, seconds: float = 0.7) -> None:
        await events.push("step", {"label": label})
        await asyncio.sleep(seconds * delay)

    try:
        await step("개인정보 검출 중")
        if "오류" in question:
            raise FakeLocalLLMError("로컬 모델 응답을 처리하지 못했습니다.")
        detected = detect(question, store.next_pd_id)
        await step("허용 설정 적용 중", 0.4)
        need_consent = [item for item in detected if not is_allowed(item, rules)]
        await step("필요한 정보 정리 중")

        rounds = 3 if "상한" in question else 2
        end_reason = EndReason.INFO_ENOUGH
        allowed: set[int] = set()
        for round_ in range(1, len(FRONTIER_ANSWERS) + 2):
            await step("답변 가능 여부 판단 중")
            if "오프라인" in question:
                await step("외부 모델 연결 확인 중")
                end_reason = EndReason.OFFLINE
                break
            if round_ > rounds:
                if "상한" in question:
                    end_reason = EndReason.CALL_LIMIT
                break

            if round_ == 1 and need_consent:
                await events.push("step", {"label": "공개 동의를 기다리는 중"})
                allowed = await _ask_consent(store, run, need_consent)
                if not allowed:
                    end_reason = EndReason.CONSENT_DENIED
                    break

            await step("도움 요청문 작성 중")
            await step("누출 검사 중", 0.5)
            hidden = [item for item in need_consent if item.pd_id not in allowed]
            request = (
                "다음 요청에 답하는 데 필요한 일반적인 정보를 알려 주세요. "
                "개인을 특정할 수 있는 내용은 빼고 답해 주세요.\n\n"
                f"요청: {mask(question, hidden)}"
                if round_ == 1
                else "앞의 답변에서 실제로 따라 할 수 있는 단계와 주의할 점을 "
                "더 구체적으로 알려 주세요."
            )
            await step(f"외부 모델에 묻는 중 ({round_}회차)", 1.2)
            answer = FRONTIER_ANSWERS[round_ - 1]
            store.add_frontier_query(run, round_, request, answer)
            await events.push("frontier", {"request": request, "answer": answer})
            await step("임시 메모 정리 중", 0.4)

        await step("최종 답변 생성 중", 0.5)
        i = 0
        while i < len(FINAL_ANSWER):
            n = random.randint(2, 5)
            chunk = FINAL_ANSWER[i : i + n]
            i += n
            answer_parts.append(chunk)
            await events.push("token", {"text": chunk})
            await asyncio.sleep(0.025 * delay)

        message = store.save_message(run.chat_id, "assistant", "".join(answer_parts))
        await _finish(run, end_reason, message.message_id)

    except asyncio.CancelledError:
        # API-07 중지. 동의 대기 중이었으면 그 요청을 먼저 취소로 닫는다.
        pending = run.pending_consent()
        if pending is not None:
            pending.result = "cancelled"
            pending.responded_at = now_iso()
            await events.push(
                "consent_result",
                {"consentId": pending.consent_id, "result": "cancelled"},
            )
        # 최종 답변 도중이면 부분 답변을 남긴다.
        answer_id = None
        if answer_parts:
            partial = "".join(answer_parts) + STOPPED_MARK
            answer_id = store.save_message(run.chat_id, "assistant", partial).message_id
        await _finish(run, EndReason.STOPPED, answer_id)

    except FakeLocalLLMError as error:
        await _fail(run, "LOCAL_LLM_ERROR", str(error))

    except Exception:  # 예상하지 못한 오류도 화면에는 error 이벤트로 알린다
        await _fail(run, "INTERNAL_ERROR", "실행 중 오류가 발생했습니다.")
        raise


async def _ask_consent(store: Store, run: RunRecord, items: list[PDItem]) -> set[int]:
    """동의 요청을 보내고 API-08의 응답을 기다린다. 허용된 pd_id 집합을 돌려준다."""
    consent = store.add_consent(
        run,
        items,
        purpose="질문에 맞는 구체적인 답변을 받기 위해 외부 모델에 함께 보냅니다.",
        benefit="상황에 맞춘 더 정확한 답변을 받을 수 있습니다.",
        risk="선택한 정보가 외부 서버로 전송됩니다.",
    )
    run.consent_waiter = asyncio.get_running_loop().create_future()
    await run.events.push(
        "consent_request",
        consent.model_dump(
            by_alias=True, include={"consent_id", "items", "purpose", "benefit", "risk"}
        ),
    )
    try:
        decisions = await run.consent_waiter
    finally:
        run.consent_waiter = None
    return {d.pd_id for d in decisions if d.allow}


async def _finish(run: RunRecord, end_reason: EndReason, answer_id: int | None) -> None:
    limited = LIMITED_TEXT.get(end_reason)
    run.status = "finished"
    run.end_reason = end_reason.value
    run.notice = limited
    run.answer_message_id = answer_id
    await run.events.push(
        "done",
        {
            "endReason": end_reason.value,
            "limited": limited,
            "answerMessageId": answer_id,
        },
    )


async def _fail(run: RunRecord, code: str, message: str) -> None:
    run.status = "finished"
    run.notice = message
    run.error_code = code
    await run.events.push("error", {"code": code, "message": message})

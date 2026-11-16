"""설계 3.1: 필요 정보 나누기(list_needs)와 답변 판단(judge)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import PromptIdx
from app.models.llm_results import JudgeResult

if TYPE_CHECKING:
    from app.runtime.local_llm_runner import LocalLLMRunner

# judge 출력의 need 번호(needIdx)가 필요 정보 목록 범위를 벗어났을 때 다시 묻는 횟수
NEED_RETRIES = 2


class Plan(BaseModel):
    """답변 가능 여부, 추가 동의 대상, 물어볼 항목."""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True, strict=True)

    enough: bool
    need_consent: bool = Field(default=False, alias="needConsent")
    consent_items: list[int] = Field(default_factory=list, alias="consentItems")
    need: list[str] = Field(default_factory=list)


class Planner:
    """필요한 정보를 나누고 현재 정보로 답할 수 있는지 판단하는 클래스."""

    def __init__(self, local_llm_runner: LocalLLMRunner | None = None) -> None:
        """로컬 모델 실행기 저장."""
        self._runner = local_llm_runner

    def list_needs(
        self,
        question: str,
        run_id: str = "",
        chat_id: str | None = None,
        *,
        needs: list[str] | None = None,
        ok: bool = True,
    ) -> list[str]:
        """질문에 답하는 데 필요한 정보 목록 반환."""
        # 1. 질문이 비어 있는지 확인
        if not isinstance(question, str) or not question.strip():
            raise ValueError("질문이 비어 있습니다")

        # 2. 직접 전달한 결과가 없으면 공유 실행기로 필요 정보 분류 요청
        if needs is None and ok and self._runner is not None:
            needs, ok, *_ = self._runner.request(
                run_id, question, PromptIdx.NEEDS, chat_id
            )
        if (
            not ok
            or not isinstance(needs, list)
            or not all(isinstance(item, str) and item.strip() for item in needs)
        ):
            raise RuntimeError("필요 정보 분류에 실패했습니다")

        result = list(needs)
        # 메모 저장은 호출자인 Main에서 담당
        return result

    def judge(
        self,
        question: str,
        memo: str,
        pd_table: list[Any],
        run_id: str = "",
        chat_id: str | None = None,
        needs: list[str] | None = None,
        *,
        raw_plan: Any = None,
        ok: bool = True,
    ) -> Plan:
        """질문, 메모, 개인정보 테이블을 보고 답변 가능 여부와 다음 행동(Plan) 판단.

        needs: list_needs()로 정해 메모에 등록한 필요 정보 목록. 0부터 번호를 붙여
        [필요 정보]로 입력에 넣고, 모델이 고른 번호(needIdx)를 이 목록의 문자열로 바꿔
        Plan.need로 돌려준다. (MemoManager.add_answer가 등록된 이름만 받기 때문)
        """
        # 1. 입력값 자료형과 빈 질문 확인
        if not isinstance(question, str) or not question.strip():
            raise ValueError("질문이 비어 있습니다")
        if not isinstance(memo, str) or not isinstance(pd_table, list):
            raise TypeError("memo는 문자열, pd_table은 목록이어야 합니다")

        # 2. 개인정보 테이블을 문자열로 묶고 동의 요청이 가능한 ID 추리기
        pd_lines: list[str] = []
        consent_candidates: set[int] = set()
        for item in pd_table:
            # 필수 필드가 빠졌거나 잘못된 형식이면 예외 발생
            try:
                pd_id = item["pd_id"] if isinstance(item, dict) else item.pd_id
                text = item["text"] if isinstance(item, dict) else item.text
                category = item["category"] if isinstance(item, dict) else item.category
                allowed = item["allowed"] if isinstance(item, dict) else item.allowed
                allowed_by = (
                    item.get("allowed_by", "none")
                    if isinstance(item, dict)
                    else getattr(item, "allowed_by", "none")
                )
            except (KeyError, AttributeError, TypeError) as exc:
                raise RuntimeError("개인정보 테이블 형식이 맞지 않습니다") from exc

            pd_lines.append(
                f"- id={pd_id}, category={category}, text={text}, allowed={allowed}"
            )
            # 아직 허용되지 않았고 거부된 적 없는 항목만 동의 후보에 추가
            if (
                not isinstance(pd_id, bool)
                and isinstance(pd_id, int)
                and not allowed
                and allowed_by != "denied"
            ):
                consent_candidates.add(pd_id)

        # 3. 필요 정보에 0부터 번호를 붙이고 질문·필요 정보·메모·개인정보를 입력 하나로 합치기
        need_list = list(needs or [])
        need_text = "\n".join(f"{i}. {n}" for i, n in enumerate(need_list)) or "없음"
        pd_text = "\n".join(pd_lines) if pd_lines else "없음"
        judge_input = (
            f"[질문]\n{question}\n\n[필요 정보]\n{need_text}\n\n"
            f"[메모]\n{memo}\n\n[개인정보]\n{pd_text}"
        )

        # 4. 직접 전달한 결과가 없으면 공유 실행기로 판단 요청.
        #    목록 범위 밖 번호는 형식 오류로 보고 최대 NEED_RETRIES번 다시 요청한다.
        asks_model = raw_plan is None and ok and self._runner is not None
        judged = self._request_judge(run_id, chat_id, judge_input, raw_plan, ok)
        retries = 0
        while not _need_idx_in_range(judged, len(need_list)):
            if not asks_model or retries >= NEED_RETRIES:
                raise RuntimeError(
                    "판단 결과의 필요 정보 번호가 목록 범위를 벗어났습니다"
                )
            retries += 1
            judged = self._request_judge(run_id, chat_id, judge_input, None, True)
        # 번호를 메모에 등록된 need 문자열로 바꾼다 (중복 번호는 한 번만).
        # 정보가 충분하면 번호를 쓰지 않는다.
        need = (
            []
            if judged.enough
            else [need_list[i] for i in dict.fromkeys(judged.need_idx)]
        )

        # 5. 물어볼 내용(need)이 비어 있으면 답변 가능(enough=True)으로 보정
        enough = judged.enough or not need

        # 6. 이미 정보가 충분하면 동의 요청과 질문 목록을 비워 반환
        if enough:
            result = Plan(enough=True, need_consent=False, consent_items=[], need=[])
            # 미구현: 판단 결과 동작 로그 기록 (ChatManager 구현 후 주석 해제)
            # self._chat_manager.log_event(run_id, "judge", result.model_dump())
            return result

        # 7. 이미 허용·거부된 항목은 빼고, 남은 동의 대상이 없으면 need_consent=False로 보정
        consent_items: list[int] = []
        for pid in judged.consent_items:
            if pid in consent_candidates and pid not in consent_items:
                consent_items.append(pid)

        need_consent = judged.need_consent and bool(consent_items)
        result = Plan(
            enough=False,
            need_consent=need_consent,
            consent_items=consent_items if need_consent else [],
            need=need,
        )
        # 미구현: 판단 결과 동작 로그 기록 (ChatManager 구현 후 주석 해제)
        # self._chat_manager.log_event(run_id, "judge", result.model_dump())
        return result

    def _request_judge(
        self,
        run_id: str,
        chat_id: str | None,
        judge_input: str,
        raw_plan: Any,
        ok: bool,
    ) -> JudgeResult:
        """판단 결과를 모델에 요청(또는 직접 받은 값 사용)하고 JudgeResult 형식을 검사한다."""
        if raw_plan is None and ok and self._runner is not None:
            raw_plan, ok, *_ = self._runner.request(
                run_id, judge_input, PromptIdx.JUDGE, chat_id
            )
        if not ok or raw_plan is None:
            raise RuntimeError("답변 판단에 실패했습니다")

        # 모델 출력이 판단 필드·자료형에 맞는지 검사
        try:
            return JudgeResult.model_validate(raw_plan)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("판단 결과 형식이 맞지 않습니다") from exc


def _need_idx_in_range(judged: JudgeResult, count: int) -> bool:
    """정보가 충분하면 번호를 쓰지 않으므로 통과. 아니면 모든 번호가 목록 안이어야 한다."""
    return judged.enough or all(i < count for i in judged.need_idx)

"""설계 3.4: 최종 답변 생성(generate)과 제한 안내(notify_limited)."""

from __future__ import annotations

from collections.abc import Iterator
from queue import Queue
from threading import Thread
from typing import TYPE_CHECKING

from app.models.enums import EndReason, PromptIdx

if TYPE_CHECKING:
    from app.data.prompt_manager import PromptManager
    from app.runtime.local_llm_runner import LocalLLMRunner

# 종료 사유별 화면 안내 문구(설계 3.4 EndReason 표). SSE done의 limited 필드 값
LIMITED_MESSAGES: dict[EndReason, str | None] = {
    EndReason.INFO_ENOUGH: None,
    EndReason.CALL_LIMIT: "질문 횟수 한도에 도달해 지금까지 모은 정보로 답했습니다.",
    EndReason.REGEN_LIMIT: (
        "개인정보가 포함되지 않은 질문을 만들지 못해 외부 모델에 묻지 못했습니다."
    ),
    EndReason.BUDGET_LIMIT: "사용 비용 한도에 도달해 지금까지 모은 정보로 답했습니다.",
    EndReason.OFFLINE: (
        "외부 모델에 연결할 수 없어 현재까지 취합한 정보를 바탕으로 "
        "로컬 모델로 답했습니다."
    ),
    EndReason.STOPPED: "중지되었습니다.",
    EndReason.CONSENT_DENIED: (
        "공개 동의가 필요한 정보는 보내지 않고 지금까지 모은 정보로 답했습니다."
    ),
}


class AnswerGenerationError(RuntimeError):
    """로컬 모델이 최종 답변을 만들지 못함. Main은 SSE error와 종료 정리로 처리."""


class AnswerGenerator:
    """질문 원문과 메모를 로컬 모델로 종합해 최종 답변을 토큰 단위로 내보내는 클래스."""

    def __init__(
        self,
        local_llm_runner: LocalLLMRunner | None = None,
        prompt_manager: PromptManager | None = None,
    ) -> None:
        """로컬 모델 실행기와 프롬프트 관리자 저장.

        prompt_manager를 생략하면 Runner에 연결된 관리자 사용.
        """
        self._runner = local_llm_runner
        self._prompts = prompt_manager

    def generate(
        self,
        question: str,
        memo: str,
        run_id: str = "",
        chat_id: str | None = None,
    ) -> Iterator[str]:
        """최종 답변을 생성하며 받은 토큰을 순서대로 내보내는 반복자 반환.

        생성은 별도 스레드에서 바로 시작. 호출자는 next()로 토큰을 하나씩 꺼냄.
        중지(stop(run_id))되면 그때까지의 토큰만 내보내고 조용히 끝남.
        생성 실패는 반복 도중 AnswerGenerationError로 전달.
        chat_id는 Main 호출 규격을 다른 모듈과 맞추기 위한 자리(이전 대화 미사용).
        """
        # 1. 입력값 확인과 모델 입력 구성. 질문 원문은 로컬에서만 쓰므로 그대로 사용
        if not isinstance(question, str) or not question.strip():
            raise ValueError("질문이 비어 있습니다")
        if not isinstance(memo, str):
            raise TypeError("memo는 문자열이어야 합니다")
        if self._runner is None:
            raise AnswerGenerationError("로컬 모델이 연결되지 않았습니다")
        system_prompt = self._system_prompt()
        answer_input = f"[질문]\n{question.strip()}\n\n[메모]\n{memo.strip() or '없음'}"

        # 2. 작업 스레드가 받은 토큰을 큐에 넣고, 끝나면 None과 실패 정보를 넣음
        tokens: Queue[str | None] = Queue()
        failure: list[BaseException] = []
        runner = self._runner

        def work() -> None:
            try:
                text, ok, _, _, reason = runner.manual_request(
                    answer_input,
                    system_prompt,
                    run_id=run_id or None,
                    on_token=tokens.put,
                )
                if reason == "stopped":
                    return  # 중지는 Main이 처리. 부분 답변은 이미 내보낸 토큰으로 보존
                if not ok or not text.strip():
                    failure.append(
                        AnswerGenerationError(
                            f"최종 답변을 만들지 못했습니다 ({reason})"
                        )
                    )
            except BaseException as exc:  # 스레드 밖의 호출자에게 그대로 전달
                failure.append(exc)
            finally:
                tokens.put(None)

        Thread(target=work, daemon=True).start()
        # 미구현: 생성 동작 로그 기록 (ChatManager 구현 후 주석 해제)
        # self._chat_manager.log_event(run_id, "answer", {"end": "started"})
        return self._stream(tokens, failure)

    @staticmethod
    def _stream(
        tokens: Queue[str | None], failure: list[BaseException]
    ) -> Iterator[str]:
        """큐의 토큰을 순서대로 내보내고, 끝난 뒤 실패가 있으면 예외 발생."""
        while (token := tokens.get()) is not None:
            if token:
                yield token
        if failure:
            raise failure[0]

    def notify_limited(self, end_reason: EndReason | str) -> str | None:
        """종료 사유별 제한 안내 문구 반환. 정상 종료(info_enough)는 None.

        Main은 이 문구를 SSE done 이벤트의 limited 필드로 화면에 전달.
        """
        return LIMITED_MESSAGES[EndReason(end_reason)]

    def _system_prompt(self) -> str:
        """ANSWER 시스템 프롬프트 조회."""
        prompts = self._prompts or getattr(self._runner, "prompt_manager", None)
        if prompts is None:
            raise AnswerGenerationError("PromptManager가 연결되지 않았습니다")
        return prompts.get_prompt(PromptIdx.ANSWER)

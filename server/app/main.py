"""Main (컨트롤러). 설계서 2.4

사용자가 질문 하나를 보내면, 그 질문에 답하기까지의 전 과정을 "실행(run)" 하나라고 부른다.
Main은 실행 하나를 처음부터 끝까지 진행하는 컨트롤러다. 직접 개인정보를 찾거나 답을 만들지 않고,
각 담당 모듈을 정해진 순서대로 부르며 언제 끝낼지만 판단한다.

    1) 준비: 개인정보 검출, 허용 설정 적용, 메모 시작, 필요한 정보 목록 만들기
    2)~6) 왕복(반복): 종료 확인 → 판단 → (동의) → 요청문 작성·누출 검사 → 프론티어 전송
    7) 최종 답변 생성 후 정리

프레임워크 없이 asyncio로 직접 작성한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from app.models.enums import EndReason

if TYPE_CHECKING:
    # 타입 표시용으로만 import한다.
    # 현재 개발되지 않아서 연결하지 않음.
    # 실제 객체는 생성자로 넘겨받으므로 모듈이 생겨도 이 import는 그대로.
    from app.data.chat_manager import ChatManager
    from app.data.memo_manager import MemoManager
    from app.data.prompt_manager import PromptManager
    from app.models.schemas import PDTable
    from app.pipeline.answer_generator import AnswerGenerator
    from app.pipeline.leak_guard import LeakGuard
    from app.pipeline.planner import Planner
    from app.pipeline.request_writer import RequestWriter
    from app.privacy.consent_manager import ConsentManager
    from app.privacy.pd_detector import PDDetector
    from app.privacy.privacy_settings import PrivacySettings
    from app.runtime.frontier_client import FrontierClient
    from app.runtime.local_llm_runner import LocalLLMRunner

T = TypeVar("T")

# 화면으로 이벤트(SSE)를 보내는 함수. (이벤트 이름, 데이터) 형태로 부른다.
# 7장 Chat API를 만들 때 실제 전송 함수를 Main에 넘겨준다.
EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

# 7) 답변 도중 중지했을 때 저장하는 부분 답변 뒤에 붙이는 표시
STOPPED_MARK = "\n\n(중지됨)"


async def _no_emit(event: str, data: dict[str, Any]) -> None:
    """Chat API가 연결되기 전의 기본값. 아무것도 보내지 않는다."""


@dataclass
class _RunState:
    """진행 중인 실행 하나의 상태. stop()이 이 정보를 보고 실행을 끊는다."""

    chat_id: str
    task: asyncio.Task[Any]  # run_pipeline()을 돌리고 있는 asyncio 작업
    answer_parts: list[str] = field(default_factory=list)  # 7)에서 나온 토큰들
    stopping: bool = False  # stop()이 이미 불렸는지
    # 이 시점 이후로는 취소하지 않는다 (답변 완료 또는 정리 시작)
    finishing: bool = False


class Main:
    def __init__(
        self,
        *,
        call_limit: int,
        regen_limit: int,
        budget_limit: float,
        local_llm_runner: LocalLLMRunner,
        frontier_client: FrontierClient,
        chat_manager: ChatManager,
        memo_manager: MemoManager,
        prompt_manager: PromptManager,
        pd_detector: PDDetector,
        privacy_settings: PrivacySettings,
        consent_manager: ConsentManager,
        planner: Planner,
        request_writer: RequestWriter,
        leak_guard: LeakGuard,
        answer_generator: AnswerGenerator,
        emit: EmitFn = _no_emit,
    ) -> None:
        """Main이 쓸 상한값과 담당 모듈들을 받아 둔다.

        모듈을 Main 안에서 직접 만들지 않고 밖에서 받는다. 그래서 각 담당자가 만든 클래스를
        그대로 끼울 수 있고, 테스트에서는 가짜 모듈을 넣어 Main만 따로 검사할 수 있다.

        상한(call_limit · regen_limit · budget_limit)은 설정 파일에서 읽어 넘긴다.
        최종값은 튜닝 절차 이후에 고정한다.
        """
        self.call_limit = call_limit  # 프론티어 질문 상한
        self.regen_limit = regen_limit  # 요청문 재생성 상한
        self.budget_limit = budget_limit  # 실행 한 번의 API 비용 한도

        self.local_llm_runner = local_llm_runner
        self.frontier_client = frontier_client
        self.chat_manager = chat_manager
        self.memo_manager = memo_manager
        self.prompt_manager = prompt_manager
        self.pd_detector = pd_detector
        self.privacy_settings = privacy_settings
        self.consent_manager = consent_manager
        self.planner = planner
        self.request_writer = request_writer
        self.leak_guard = leak_guard
        self.answer_generator = answer_generator
        self._emit = emit

        # 진행 중인 실행들. run_id → 상태. stop(run_id)이 여기서 실행을 찾는다.
        self._runs: dict[str, _RunState] = {}

    async def init(self) -> None:
        """init: 로컬 LLM 초기화, 프론티어 API 수립(통신 테스트) 후
        PromptManager.loadPrompts()와 PrivacySettings.loadRules()를 호출한다.

        앱 띄우기(웹 서버)와 DB 구동은 Chat API · chatManager 쪽에서 Main을 만들기 전에 한다.
        """
        if not await self._call(self.local_llm_runner.model_load):
            raise RuntimeError("로컬 LLM 모델을 GPU에 불러오지 못했습니다.")
        # 연결이 안 돼도 앱은 뜬다. 실행마다 2)의 check_end()가 연결을 다시 확인하고,
        # 끊겨 있으면 offline으로 끝내 로컬 모델만으로 답한다.
        await self._call(self.frontier_client.check_connection)
        if not await self._call(self.prompt_manager.load_prompts):
            raise RuntimeError("시스템 프롬프트 파일을 불러오지 못했습니다.")
        await self._call(self.privacy_settings.load_rules)

    async def run_pipeline(self, chat_id: str, question: str) -> None:
        """검출부터 최종 답변까지 전체 흐름을 실행한다.

        종료 사유(end_reason)를 실행 기록에 남긴다.
        Chat API는 이 함수를 asyncio.create_task()로 따로 돌려야 stop()으로 끊을 수 있다.
        """
        run_id: str | None = None
        pd_table: PDTable | None = None
        end_reason: EndReason | None = None
        answer_message_id: str | None = None
        original: BaseException | None = None

        try:
            # 1) 실행 준비
            # 질문을 대화 기록에 저장하고, 이번 실행의 기록(run)을 만든다. run_id는 여기서 생긴다.
            user_message_id = await self._call(
                self.chat_manager.save_message, chat_id, "user", question
            )
            run_id = await self._call(
                self.chat_manager.start_run,
                chat_id,
                user_message_id,
                self.privacy_settings.setting_id,
                self._run_config(),
            )
            task = asyncio.current_task()
            assert task is not None  # run_pipeline은 항상 asyncio 작업 안에서 돈다
            self._runs[run_id] = _RunState(chat_id=chat_id, task=task)
            await self._emit("run", {"run_id": run_id})  # 화면이 중지 버튼에 쓸 ID

            # 질문 속 개인정보를 찾아 이 채팅의 개인정보 테이블(PDTable)에 더한다.
            found_items = await self._call(
                self.pd_detector.detect, run_id, chat_id, user_message_id, question
            )
            await self._call(
                self.chat_manager.set_pd_items, chat_id, user_message_id, found_items
            )
            pd_table = await self._call(self.chat_manager.get_pd_table, chat_id)

            # 사용자가 미리 정한 허용/금지 설정을 테이블의 각 항목에 적용한다.
            await self._call(self.privacy_settings.apply_to_table, run_id, pd_table)

            # 임시 메모를 시작하고, 답하는 데 필요한 정보 목록을 적어 둔다.
            await self._call(self.memo_manager.start, run_id, question)
            needs = await self._call(self.planner.list_needs, question)
            await self._call(self.memo_manager.set_needs, needs)

            # 2)~6) 왕복: 끝날 때까지 정보를 모은다
            end_reason = await self._collect(run_id, question, pd_table)

            # 7) 최종 답변
            answer_message_id = await self._answer(
                run_id, chat_id, question, end_reason
            )

        except asyncio.CancelledError as error:
            # stop()이 이 작업을 취소하면 여기로 온다. 7)은 새로 시작하지 않는다.
            # 원래 오류가 뭔지 기록해 두고, finally에서 다시 raise한다.
            original = error
            end_reason = EndReason.STOPPED
            state = self._runs.get(run_id) if run_id is not None else None
            if state is not None and state.answer_parts:
                # 7) 도중에 멈췄다면 그때까지 나온 부분 답변을 '중지됨'과 함께 저장한다.
                partial = "".join(state.answer_parts) + STOPPED_MARK
                answer_message_id = await self._call(
                    self.chat_manager.save_message, chat_id, "assistant", partial
                )
            await self._emit("stopped", {"run_id": run_id})
            raise

        except Exception as error:
            # 예상하지 못한 오류: 화면에 알리고, 아래 finally에서 같은 정리를 한다.
            # 원래 오류가 뭔지 기록해 두고, finally에서 다시 raise한다.
            original = error
            await self._emit("error", {"run_id": run_id, "message": str(error)})
            raise

        finally:
            # 정상 종료 · 중지 · 오류 모두 같은 정리 절차를 거친다.
            if run_id is not None:
                state = self._runs.get(run_id)
                if state is not None:
                    state.finishing = True

                # 정리 중에 오류가 생기면 원래 오류가 없을 때만 그 오류를 올린다.
                # (원래 오류가 있으면 그것을 살리고, 정리 오류는 메모로 붙인다)
                cleanup_errors = await self._finish(
                    run_id, pd_table, end_reason, answer_message_id
                )
                if cleanup_errors and original is None:
                    # 본 흐름은 성공했는데 정리가 실패한 경우만 정리 오류를 올린다
                    raise cleanup_errors[0]
                # 원래 오류가 있으면 그것을 살리고, 정리 오류는 메모로 붙인다
                for e in cleanup_errors:
                    if original is not None:
                        original.add_note(f"정리 중 오류: {e!r}")

    def check_end(
        self,
        online: bool,
        frontier_calls: int,
        call_limit: int,
        spent_cost: float,
        budget_limit: float,
    ) -> EndReason | None:
        """종료 조건을 확인해 EndReason을, 해당 없으면 None을 반환한다.

        여러 조건이 동시에 참이면 OFFLINE → CALL_LIMIT → BUDGET_LIMIT 순으로 반환한다.
        """
        if not online:
            return EndReason.OFFLINE
        if frontier_calls >= call_limit:
            return EndReason.CALL_LIMIT
        if spent_cost > budget_limit:
            return EndReason.BUDGET_LIMIT
        return None

    async def stop(self, run_id: str) -> None:
        """사용자가 중지 버튼을 누르면(POST stop) 어느 단계에서든 실행을 즉시 끊는다.

        stop()은 진행 중인 일을 멈추게만 하고, 'stopped' 기록과 정리는
        run_pipeline()의 CancelledError 처리와 finally가 맡는다. 정리 코드를 한 곳에만 둬서
        두 번 정리되거나 빠지는 일을 막는다.
        """
        state = self._runs.get(run_id)
        if state is None or state.stopping:
            return  # 이미 끝났거나 중지 중인 실행
        state.stopping = True

        if not state.finishing:  # 이미 끝나는 중이면 모델 중지·동의 취소도 하지 않는다
            # 별도 스레드에서 돌고 있는 로컬 모델 생성을 멈춘다. (stopFlag)
            await self._call(self.local_llm_runner.stop)
            # 사용자 동의를 기다리는 중이었다면 그 대기를 끝낸다.
            await self._call(self.consent_manager.cancel, run_id)

        # await 뒤에 다시 확인. 이 줄과 cancel() 사이엔 await 없음
        if not state.finishing:
            # run_pipeline 작업을 취소하고, 정리가 끝날 때까지 기다린다.
            state.task.cancel()

        await asyncio.gather(state.task, return_exceptions=True)

    # run_pipeline()을 단계별로 나눈 내부 함수

    async def _collect(
        self, run_id: str, question: str, pd_table: PDTable
    ) -> EndReason:
        """2)~6) 왕복을 반복하며 정보를 모은다. 끝나야 하는 이유가 생기면 그 사유를 반환한다."""
        frontier_calls = 0  # 이번 실행에서 프론티어에 물어본 횟수
        spent_cost = 0.0  # 이번 실행에서 쓴 API 비용

        while True:
            # 2) 왕복을 시작하기 전에 끝내야 하는지 먼저 확인한다.
            online = await self._call(self.frontier_client.check_connection)
            end_reason = self.check_end(
                online, frontier_calls, self.call_limit, spent_cost, self.budget_limit
            )
            if end_reason is not None:
                return end_reason

            # 지금까지 모은 메모로 답할 수 있는지 판단한다.
            memo = await self._call(self.memo_manager.read_all)
            plan = await self._call(self.planner.judge, question, memo, pd_table)

            # 3) 정보가 충분하면 정상 종료.
            if plan.enough:
                return EndReason.INFO_ENOUGH

            # 4) 아직 허용되지 않은 개인정보를 더 보내야 하면 사용자에게 묻는다.
            if plan.need_consent:
                consent_req = await self._call(
                    self.consent_manager.build_message,
                    run_id,
                    plan.consent_items,
                    plan.need,
                    pd_table,
                )
                if consent_req is not None:  # None이면 실제로 물어볼 항목이 없다
                    # 사용자가 응답할 때까지 기다린다. (중지되면 stop()이 이 대기를 끊는다)
                    consent_result = await self.consent_manager.request(consent_req)
                    if not consent_result.allow:
                        return EndReason.CONSENT_DENIED
                    # 허용한 항목은 이번 실행 동안만 허용으로 바뀐다.
                    await self._call(
                        self.consent_manager.apply,
                        consent_result,
                        consent_req,
                        pd_table,
                    )

            # 5) 개인정보가 새지 않는 요청문을 만든다.
            request_text = await self._write_safe_request(
                run_id, question, memo, pd_table, plan.need
            )
            if request_text is None:
                return EndReason.REGEN_LIMIT

            # 6) 검사를 통과한 요청문만 프론티어로 보낸다.
            try:
                reply = await self._call(self.frontier_client.send, request_text)
            except ConnectionError:
                return EndReason.OFFLINE
            frontier_calls += 1
            spent_cost += reply.cost
            await self._call(self.memo_manager.add_answer, plan.need, reply.answer)
            # 다시 2)로 돌아가 판단한다.

    async def _write_safe_request(
        self,
        run_id: str,
        question: str,
        memo: str,
        pd_table: PDTable,
        need: list[str],
    ) -> str | None:
        """5) 요청문을 쓰고, 누출 검사를 통과할 때까지 고친다.

        통과한 요청문을 반환한다. regen_limit번 고쳐도 안 되면 None을 반환한다.
        """
        # 중지 버튼이 로컬 생성을 끊을 수 있도록 실행 ID와 채팅 ID를 함께 넘긴다.
        state = self._runs.get(run_id)
        chat_id = state.chat_id if state is not None else None
        request_text = await self._call(
            self.request_writer.write, question, memo, pd_table, need, run_id, chat_id
        )
        regen_count = 0  # 이 요청문을 다시 쓴 횟수

        while True:
            hits: list[Any] = []  # 누출 검사에 걸린 개인정보 항목 ID
            # request_text가 None이면 생성 실패. 검사에 걸린 것과 같이 센다.
            if request_text is not None:
                guard_result = await self._call(
                    self.leak_guard.detect, run_id, request_text, pd_table
                )
                if guard_result.passed:
                    return request_text
                if not guard_result.hits:
                    # 검사 모델이 형식을 끝내 못 맞췄다. 무엇을 고칠지 모르므로
                    # 다시 쓰지 않고 실패로 처리한다. (설계서 3.3)
                    return None
                hits = guard_result.hits

            if regen_count >= self.regen_limit:
                return None
            regen_count += 1

            if request_text is None:
                request_text = await self._call(
                    self.request_writer.write,
                    question,
                    memo,
                    pd_table,
                    need,
                    run_id,
                    chat_id,
                )
            else:
                request_text = await self._call(
                    self.request_writer.rewrite,
                    request_text,
                    hits,
                    need,
                    pd_table,
                    run_id,
                    chat_id,
                )

    async def _answer(
        self, run_id: str, chat_id: str, question: str, end_reason: EndReason
    ) -> str:
        """7) 모은 정보로 최종 답변을 만들어 화면에 흘려보내고 저장한다. 메시지 ID를 반환한다."""
        state = self._runs[run_id]
        memo = await self._call(self.memo_manager.read_all)
        tokens = await self._call(self.answer_generator.generate, question, memo)

        # 토큰을 하나씩 꺼내 화면에 보낸다. 중지되면 그때까지의 토큰이 answer_parts에 남는다.
        while (token := await self._call(next, tokens, None)) is not None:
            state.answer_parts.append(token)
            await self._emit("token", {"run_id": run_id, "text": token})

        state.finishing = True  # 답변이 다 나왔으니 이후로는 끊지 않는다

        # 정상 종료가 아니면 어떤 이유로 제한된 답인지 답변 아래에 안내한다.
        if end_reason != EndReason.INFO_ENOUGH:
            await self._call(self.answer_generator.notify_limited, end_reason)

        answer = "".join(state.answer_parts)
        return await self._call(
            self.chat_manager.save_message, chat_id, "assistant", answer
        )

    async def _finish(
        self,
        run_id: str,
        pd_table: PDTable | None,
        end_reason: EndReason | None,
        answer_message_id: str | None,
    ) -> list[Exception]:
        """실행 정리. 한 단계가 실패해도 나머지 단계는 모두 시도하고, 생긴 오류를 돌려준다."""
        errors: list[Exception] = []
        try:
            if pd_table is not None:
                # 이번 실행에서 동의로 허용한 항목을 다시 금지로 되돌린다.
                # 개인정보와 직결되므로 가장 먼저, 무조건 시도한다.
                await self._try(errors, self.consent_manager.release, run_id, pd_table)
            # 최종 메모를 읽는다. 실패하면 None으로 기록한다.
            memo_final = await self._try(errors, self.memo_manager.read_all)
            await self._try(errors, self.memo_manager.clear)
            await self._try(
                errors,
                self.chat_manager.finish_run,
                run_id,
                end_reason,
                answer_message_id,
                memo_final,
            )

        finally:
            # 무슨 일이 있어도 진행 중 실행 목록에서 제거한다.
            self._runs.pop(run_id, None)
        return errors

    async def _try(
        self, errors: list[Exception], func: Callable[..., T], *args: Any
    ) -> T | None:
        """_call과 같지만 예외를 errors에 모으고 None을 돌려준다. (정리 단계 전용)"""
        try:
            return await self._call(func, *args)
        except Exception as error:
            errors.append(error)
            return None

    def _run_config(self) -> dict[str, Any]:
        """startRun()에 함께 저장할 이번 실행의 설정값."""
        return {
            "call_limit": self.call_limit,
            "regen_limit": self.regen_limit,
            "budget_limit": self.budget_limit,
        }

    async def _call(self, func: Callable[..., T], *args: Any) -> T:
        """오래 걸리는 동기 함수(로컬 LLM · DB · API 호출)를 별도 스레드에서 실행한다.

        그동안 이벤트 루프가 막히지 않아야 중지(stop) 요청을 바로 받을 수 있다.
        """
        return await asyncio.to_thread(func, *args)

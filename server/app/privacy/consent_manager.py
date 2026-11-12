"""설계 4.3 및 API-08: 실행별 자연어 동의와 임시 공개 허용."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from app.models.consent_results import (
    ConsentDecision,
    ConsentInterpretation,
    ConsentMessage,
    ConsentReply,
    ConsentResult,
    ConsentTarget,
)
from app.models.enums import PromptIdx
from app.models.llm_results import ConsentText
from app.privacy.pd_detector import CATEGORY_LABEL, PDTable
from app.runtime.local_llm_runner import RunnerBusyError

if TYPE_CHECKING:
    from app.models.schemas import ConsentRequest
    from app.runtime.local_llm_runner import LocalLLMRunner

T = TypeVar("T")
RunId = int | str
EmitFn = Callable[[str, dict[str, Any], int], Awaitable[None]]


class _ConsentStore(Protocol):
    """PR #34의 동기 저장 계약. 영구 개인정보 허용 상태는 수정하지 않는다."""

    def save_consent(
        self,
        run_id: RunId,
        items: list[ConsentTarget],
        purpose: str,
        benefit: str,
        risk: str,
    ) -> ConsentRequest: ...

    def update_consent(
        self,
        run_id: RunId,
        consent_id: int,
        decisions: list[ConsentDecision],
        response_text: str | None = None,
        *,
        cancelled: bool = False,
    ) -> ConsentRequest: ...

    def get_pending_consent(self, run_id: RunId) -> ConsentRequest | None: ...

    def log_event(self, run_id: RunId, type_: str, payload: Any) -> int: ...


class NoPendingConsentError(ValueError):
    """API-08: HTTP 409, NO_PENDING_CONSENT."""


async def _no_emit(event: str, data: dict[str, Any], event_id: int) -> None:
    pass


@dataclass
class _Pending:
    message: ConsentMessage
    future: asyncio.Future[ConsentResult]
    interpreting: bool = False
    cancelled: bool = False
    answered: bool = False


class ConsentManager:
    def __init__(
        self,
        chat_manager: _ConsentStore,
        local_llm_runner: LocalLLMRunner,
        *,
        emit: EmitFn = _no_emit,
    ) -> None:
        self._store = chat_manager
        self._runner = local_llm_runner
        self._emit = emit
        self._lock = asyncio.Lock()
        self._pending: dict[RunId, _Pending] = {}
        self._applied: dict[RunId, dict[int, tuple[bool, str]]] = {}
        self._completed: dict[RunId, ConsentResult] = {}
        self._table_lock = Lock()

    def build_message(
        self,
        run_id: RunId,
        consent_items: list[int],
        need: list[str],
        pd_table: PDTable,
    ) -> ConsentMessage | None:
        """허용·거절 이력이 없는 후보만 안내한다. 모델 실패 시 고정 안내를 사용한다."""
        requested = {value for value in consent_items if type(value) is int}
        items = [
            ConsentTarget(
                pd_id=item.pd_id,
                label=CATEGORY_LABEL[item.category],
                text=item.text,
                category=item.category,
            )
            for item in pd_table
            if item.pd_id in requested
            and not item.allowed
            and item.allowed_by != "denied"
        ]
        if not items:
            return None
        text = ConsentText(
            purpose="질문에 필요한 추가 정보를 외부 모델에 요청합니다.",
            benefit="공개한 정보를 바탕으로 더 구체적인 답변을 받을 수 있습니다.",
            risk="허용한 개인정보가 외부 모델에 전송됩니다. 이번 실행에만 허용됩니다.",
        )
        try:
            parsed, ok, *_ = self._runner.request(
                run_id,
                json.dumps(
                    {
                        "items": [i.model_dump(by_alias=True) for i in items],
                        "need": need,
                    },
                    ensure_ascii=False,
                ),
                PromptIdx.CONSENT,
                None,
                use_context=False,
            )
            if (
                ok
                and isinstance(parsed, ConsentText)
                and all(
                    value.strip()
                    for value in (parsed.purpose, parsed.benefit, parsed.risk)
                )
            ):
                text = parsed
        except (ConnectionError, TimeoutError, RunnerBusyError):
            pass
        return ConsentMessage(run_id=run_id, items=items, **text.model_dump())

    async def _io(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    async def _settle(self, operation: Awaitable[T]) -> T:
        """저장·발행·대기 상태 갱신을 마친 후 호출자의 취소를 전달한다."""
        task = asyncio.ensure_future(operation)
        cancelled = False
        while True:
            try:
                value = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return value

    async def _publish(self, run_id: RunId, event: str, data: dict[str, Any]) -> None:
        event_id = await self._io(self._store.log_event, run_id, event, data)
        try:
            await self._emit(event, data, event_id)
        except (ConnectionError, TimeoutError):
            # 저장된 이벤트는 재접속 시 같은 ID로 재전송한다.
            pass

    async def request(self, message: ConsentMessage) -> ConsentResult:
        """요청을 저장·발행한 뒤 명시적인 사용자 응답 또는 중지까지 기다린다."""
        owned: list[_Pending] = []
        try:
            pending = await self._settle(self._start(message, owned))
            return await asyncio.shield(pending.future)
        except BaseException:
            if owned:
                await self.cancel(message.run_id)
            raise

    async def _start(
        self,
        message: ConsentMessage,
        owned: list[_Pending],
    ) -> _Pending:
        async with self._lock:
            if message.run_id in self._pending or message.consent_id is not None:
                raise NoPendingConsentError("이미 요청했거나 대기 중인 동의입니다.")
            saved = await self._io(
                self._store.save_consent,
                message.run_id,
                message.items,
                message.purpose,
                message.benefit,
                message.risk,
            )
            message.consent_id = saved.consent_id
            pending = _Pending(
                message.model_copy(deep=True),
                asyncio.get_running_loop().create_future(),
            )
            self._pending[message.run_id] = pending
            owned.append(pending)
            await self._publish(
                message.run_id,
                "consent_request",
                {
                    "consentId": saved.consent_id,
                    "items": [item.model_dump(by_alias=True) for item in message.items],
                    "purpose": message.purpose,
                    "benefit": message.benefit,
                    "risk": message.risk,
                },
            )
            return pending

    async def receive(self, run_id: RunId, consent_id: int, text: str) -> ConsentReply:
        """API-08 진입점. 모델 의미 판단 후 ID·중복·결과 형식을 검증한다."""
        try:
            return await self._settle(self._receive(run_id, consent_id, text))
        except RunnerBusyError as error:
            raise ConnectionError("로컬 LLM이 다른 요청을 처리 중입니다.") from error

    async def _receive(self, run_id: RunId, consent_id: int, text: str) -> ConsentReply:
        if type(consent_id) is not int or not isinstance(text, str) or not text.strip():
            raise ValueError("요청 ID와 비어 있지 않은 응답이 필요합니다.")
        async with self._lock:
            pending = self._pending.get(run_id)
            if (
                pending is None
                or pending.message.consent_id != consent_id
                or pending.interpreting
                or pending.cancelled
            ):
                raise NoPendingConsentError("현재 응답할 수 있는 동의 요청이 아닙니다.")
            pending.interpreting = True
        try:
            parsed, ok, *_, reason = await asyncio.to_thread(
                self._runner.request,
                run_id,
                json.dumps(
                    {
                        "items": [
                            i.model_dump(by_alias=True) for i in pending.message.items
                        ],
                        "text": text,
                    },
                    ensure_ascii=False,
                ),
                PromptIdx.INTERPRET_CONSENT,
                None,
                use_context=False,
            )
            async with self._lock:
                if self._pending.get(run_id) is not pending or pending.cancelled:
                    raise NoPendingConsentError(
                        "동의 요청이 취소되거나 종료되었습니다."
                    )
                if reason == "unavailable":
                    raise ConnectionError("로컬 LLM에 동의 해석을 요청하지 못했습니다.")
                if (
                    not ok
                    or not isinstance(parsed, ConsentInterpretation)
                    or not parsed.ok
                    or not parsed.decisions
                ):
                    return ConsentReply(
                        ok=False, message="공개할 정보를 명확히 답해 주세요."
                    )
                requested = {item.pd_id for item in pending.message.items}
                ids = [d.pd_id for d in parsed.decisions]
                if len(ids) != len(set(ids)) or not set(ids) <= requested:
                    return ConsentReply(
                        ok=False, message="공개할 정보를 명확히 답해 주세요."
                    )
                selected = {d.pd_id: d.allow for d in parsed.decisions}
                decisions = [
                    ConsentDecision(pd_id=i.pd_id, allow=selected.get(i.pd_id, False))
                    for i in pending.message.items
                ]
                await self._io(
                    self._store.update_consent, run_id, consent_id, decisions, text
                )
                pending.answered = True
                result = ConsentResult(
                    run_id=run_id, consent_id=consent_id, decisions=decisions
                )
                with self._table_lock:
                    self._completed[run_id] = result.model_copy(deep=True)
                try:
                    await self._publish(
                        run_id,
                        "consent_result",
                        {
                            "consentId": consent_id,
                            "result": "answered",
                            "text": text,
                            "decisions": [
                                d.model_dump(by_alias=True) for d in decisions
                            ],
                        },
                    )
                except Exception as error:
                    self._pending.pop(run_id)
                    pending.future.set_exception(error)
                    raise
                self._pending.pop(run_id)
                pending.future.set_result(result)
                return ConsentReply(
                    ok=True, decisions=decisions, message=parsed.message
                )
        finally:
            pending.interpreting = False

    def apply(
        self,
        result: ConsentResult,
        message: ConsentMessage,
        pd_table: PDTable,
    ) -> None:
        """저장된 결과만 적용하며 원래 상태를 보관한다. 영구 설정은 변경하지 않는다."""
        with self._table_lock:
            if (
                self._completed.get(result.run_id) != result
                or message.run_id != result.run_id
                or message.consent_id != result.consent_id
            ):
                raise ValueError("확정된 동의 결과가 아닙니다.")
            requested = {i.pd_id for i in message.items}
            if {d.pd_id for d in result.decisions} != requested:
                raise ValueError("동의 항목이 일치하지 않습니다.")
            originals = self._applied.setdefault(result.run_id, {})
            by_id = {item.pd_id: item for item in pd_table}
            for decision in result.decisions:
                item = by_id[decision.pd_id]
                originals.setdefault(decision.pd_id, (item.allowed, item.allowed_by))
                item.allowed = decision.allow
                item.allowed_by = "consent" if decision.allow else "denied"
            self._completed.pop(result.run_id)

    async def cancel(self, run_id: RunId) -> None:
        """해석 중에도 먼저 취소를 표시하여 뒤늦은 해석 결과를 폐기한다."""
        pending = self._pending.get(run_id)
        if pending is not None:
            pending.cancelled = True
        await self._settle(self._cancel(run_id))

    async def _cancel(self, run_id: RunId) -> None:
        async with self._lock:
            pending = self._pending.get(run_id)
            if pending is None:
                return
            try:
                if not pending.answered:
                    await self._io(
                        self._store.update_consent,
                        run_id,
                        pending.message.consent_id,
                        [],
                        cancelled=True,
                    )
                    await self._publish(
                        run_id,
                        "consent_result",
                        {
                            "consentId": pending.message.consent_id,
                            "result": "cancelled",
                        },
                    )
            finally:
                self._pending.pop(run_id, None)
                pending.future.cancel()

    async def release(self, run_id: RunId, pd_table: PDTable) -> None:
        """정상·오류·중지 공통 정리. 허용과 거절을 원래 실행 시작 상태로 복구한다."""
        with self._table_lock:
            originals = self._applied.pop(run_id, {})
            self._completed.pop(run_id, None)
            for item in pd_table:
                if item.pd_id in originals:
                    item.allowed, item.allowed_by = originals[item.pd_id]
        await self.cancel(run_id)

    async def get_pending(self, run_id: RunId) -> ConsentRequest | None:
        """SSE 재접속의 대기 요청 복원은 DB에 저장한 요청을 기준으로 한다."""
        return await self._io(self._store.get_pending_consent, run_id)

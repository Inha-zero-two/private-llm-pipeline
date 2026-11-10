"""API 뒤에서 실행을 돌리는 두 가지 방식.

- MainBackend: 실제 Main.run_pipeline(). 있는 모듈은 실제 것, 없는 모듈은 stand_ins의 대역.
- FakeBackend: 모델 없이 fake_run이 흐름만 흉내 낸다. (모델 키가 없을 때 화면 확인용)

둘 다 같은 약속을 지킨다: start()는 실행 ID를 돌려주고, 실행 중에는 run.events에
API-06 이벤트를 쌓으며, 끝나면 done 또는 error를 남긴다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from app.api.fake_run import fake_run, push_done, push_error
from app.api.store import RunRecord, Store
from app.models.enums import EndReason

log = logging.getLogger(__name__)

# Main 상한값. 튜닝 전 임시값 (설계서: 설정 파일에서 읽어 넘김)
CALL_LIMIT = 3
REGEN_LIMIT = 2
BUDGET_LIMIT = 1.0


class BusyError(Exception):
    """로컬 모델은 요청을 하나씩만 처리하므로 실행도 하나씩만 돌린다."""


class Backend(Protocol):
    name: str

    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...
    async def start(self, chat_id: int, text: str) -> int: ...
    async def stop(self, run: RunRecord) -> None: ...


class FakeBackend:
    name = "fake"

    def __init__(self, store: Store, delay: float) -> None:
        self._store = store
        self._delay = delay

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        await _cancel_all(self._store)

    async def start(self, chat_id: int, text: str) -> int:
        message = self._store.save_message(chat_id, "user", text)
        run = self._store.start_run(chat_id, message.message_id)
        # 설정은 실행을 시작할 때의 것으로 고정한다 (새 설정은 다음 실행부터)
        rules = list(self._store.setting.rules)
        run.task = asyncio.create_task(
            fake_run(self._store, run, text, rules, self._delay)
        )
        return run.run_id

    async def stop(self, run: RunRecord) -> None:
        if run.task is not None:
            run.task.cancel()


class MainBackend:
    """실제 Main을 돌린다. Main의 이벤트(run_id 포함)를 실행별 기록으로 옮긴다."""

    name = "main"

    def __init__(self, store: Store, runner_factory: Any = None) -> None:
        self._store = store
        self._runner_factory = runner_factory  # 테스트에서 Runner를 바꿔 끼울 때
        self._main: Any = None
        self._runner: Any = None
        self._answer_generator: Any = None
        self._started: dict[int, asyncio.Future[str]] = {}  # chat_id → 실행 ID 대기

    async def startup(self) -> None:
        """모듈을 만들고 Main.init()으로 모델·프롬프트를 준비한다. 실패하면 서버가 뜨지 않는다."""
        from app.api.stand_ins import (
            CannedFrontierClient,
            LocalAnswerGenerator,
            MaskingRequestWriter,
            ScreenConsentManager,
            StoreChatManager,
            StorePrivacySettings,
            TextLeakGuard,
        )
        from app.data.memo_manager import MemoManager
        from app.data.prompt_manager import PromptManager
        from app.main import Main
        from app.pipeline.planner import Planner
        from app.privacy.pd_detector import PDDetector
        from app.runtime.local_llm_runner import LocalLLMRunner

        prompts = PromptManager()
        factory = self._runner_factory or LocalLLMRunner
        # Runner는 만들 때 모델(또는 원격 서버)에 실제로 짧게 물어 연결을 확인한다.
        try:
            self._runner = await asyncio.to_thread(factory, prompt_manager=prompts)
        except Exception:
            log.error(
                "로컬 LLM을 준비하지 못해 PIPELINE=main으로 시작할 수 없습니다. "
                "모델(또는 원격 서버 키)을 준비하거나, 모델 없이 화면만 확인하려면 "
                "PIPELINE=fake로 실행하세요."
            )
            raise
        self._answer_generator = LocalAnswerGenerator(self._runner)
        self._main = Main(
            call_limit=CALL_LIMIT,
            regen_limit=REGEN_LIMIT,
            budget_limit=BUDGET_LIMIT,
            local_llm_runner=self._runner,
            prompt_manager=prompts,
            pd_detector=PDDetector(self._runner),
            planner=Planner(self._runner),
            memo_manager=MemoManager(self._runner, prompts),
            chat_manager=StoreChatManager(self._store),
            consent_manager=ScreenConsentManager(self._store),
            privacy_settings=StorePrivacySettings(self._store),
            request_writer=MaskingRequestWriter(),
            leak_guard=TextLeakGuard(),
            frontier_client=CannedFrontierClient(),
            answer_generator=self._answer_generator,
            emit=self._on_event,
        )
        await self._main.init()

    async def shutdown(self) -> None:
        await _cancel_all(self._store)
        if self._runner is not None:
            await asyncio.to_thread(self._runner.close)

    async def start(self, chat_id: int, text: str) -> int:
        if any(r.status == "running" for r in self._store.runs.values()):
            raise BusyError
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._started[chat_id] = started
        task = asyncio.create_task(self._main.run_pipeline(str(chat_id), text))
        try:
            # Main이 실행 기록을 만들고 run 이벤트를 보낼 때까지 기다린다.
            await asyncio.wait({started, task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._started.pop(chat_id, None)
        if not started.done():
            task.result()  # 실행 기록을 만들기도 전에 실패했다면 그 오류를 올린다
            raise RuntimeError("실행을 시작하지 못했습니다.")
        run = self._store.runs[int(started.result())]
        run.task = task
        task.add_done_callback(lambda t: asyncio.ensure_future(self._after(run, t)))
        return run.run_id

    async def stop(self, run: RunRecord) -> None:
        await self._main.stop(str(run.run_id))

    async def _on_event(self, event: str, data: dict[str, Any]) -> None:
        """Main 이벤트 → 명세서 API-06 이벤트."""
        if data.get("run_id") is None:
            return  # 실행 기록을 만들기 전의 오류. start()가 task 결과로 처리한다
        run = self._store.runs.get(int(data["run_id"]))
        if run is None:
            return
        if event == "run":
            self._answer_generator.run_id = data["run_id"]
            waiter = self._started.get(run.chat_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(data["run_id"])
        elif event == "step":
            await run.events.push("step", {"label": data["label"]})
        elif event == "frontier":
            self._store.add_frontier_query(
                run, len(run.frontier_queries) + 1, data["request"], data["answer"]
            )
            await run.events.push(
                "frontier", {"request": data["request"], "answer": data["answer"]}
            )
        elif event == "token":
            await run.events.push("token", {"text": data["text"]})
        elif event == "done":
            answer_id = data["answer_message_id"]
            await push_done(
                run,
                EndReason(data["end_reason"]),
                int(answer_id) if answer_id else None,
            )
        elif event == "error":
            log.error("실행 %s 오류: %s", run.run_id, data.get("message"))
            await push_error(
                run,
                "INTERNAL_ERROR",
                f"실행 중 오류가 발생했습니다: {data.get('message')}",
            )

    async def _after(self, run: RunRecord, task: asyncio.Task[None]) -> None:
        """Main이 done·error 없이 끝났다면(정리 실패 등) 화면이 기다리지 않게 오류로 닫는다."""
        if not task.cancelled() and task.exception() is not None:
            log.error("실행 %s 종료 오류", run.run_id, exc_info=task.exception())
        if not run.events.closed:
            await push_error(
                run, "INTERNAL_ERROR", "실행이 정상적으로 끝나지 않았습니다."
            )


async def _cancel_all(store: Store) -> None:
    tasks = [r.task for r in store.runs.values() if r.task and not r.task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

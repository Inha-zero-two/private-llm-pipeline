"""화면 API 뒤에서 실제 모듈을 만들고 실행을 시작·중지한다.

모든 모듈은 실제 구현이다. 채팅·실행 기록은 SQLite ChatManager에 저장하고, 실행 중에만 필요한
것(실행 작업, 이벤트 알림)만 메모리에 둔다. 로컬 LLM은 하나라 실행도 하나씩만 돌린다.

    LocalLLMRunner ─┬─ PDDetector · Planner · MemoManager · RequestWriter · LeakGuard
                    ├─ AnswerGenerator · PrivacySettings · ConsentManager
    FrontierClient ─┤
    ChatManager ────┴─ Main ──emit──▶ EventHub ──▶ SSE
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.api.events import EventHub

if TYPE_CHECKING:
    from app.data.chat_manager import ChatManager
    from app.data.prompt_manager import PromptManager
    from app.main import Main
    from app.privacy.consent_manager import ConsentManager
    from app.privacy.privacy_settings import PrivacySettings
    from app.runtime.frontier_client import FrontierClient
    from app.runtime.local_llm_runner import LocalLLMRunner

log = logging.getLogger(__name__)

# Main 상한값. 튜닝 전 임시값 (설계서: 설정 파일에서 읽어 넘김)
CALL_LIMIT = 3
REGEN_LIMIT = 2
BUDGET_LIMIT = 1.0

# 모듈을 만드는 함수. 테스트는 모델·외부 API 전송 계층만 바꾼 것을 넘긴다.
RunnerFactory = Callable[..., "LocalLLMRunner"]
FrontierFactory = Callable[["PromptManager"], "FrontierClient"]


class BusyError(Exception):
    """로컬 모델은 요청을 하나씩만 처리하므로 실행도 하나씩만 돌린다."""


def _default_runner(
    *, prompt_manager: PromptManager, chat_manager: ChatManager
) -> LocalLLMRunner:
    from app.runtime.local_llm_runner import LocalLLMRunner

    return LocalLLMRunner(prompt_manager=prompt_manager, chat_manager=chat_manager)


def _default_frontier(prompt_manager: PromptManager) -> FrontierClient:
    from app.runtime.frontier_client import FrontierClient

    return FrontierClient(prompt_manager)


@dataclass
class Services:
    """API 처리기가 쓰는 실제 모듈 묶음."""

    chat_manager: ChatManager
    privacy_settings: PrivacySettings
    consent_manager: ConsentManager
    main: Main


class Pipeline:
    """모듈 생성·정리와 실행 시작·중지를 맡는다. 앱이 켜질 때 startup(), 꺼질 때 shutdown()."""

    def __init__(
        self,
        db_path: Path,
        *,
        runner_factory: RunnerFactory | None = None,
        frontier_factory: FrontierFactory | None = None,
    ) -> None:
        self._db_path = db_path
        self._runner_factory = runner_factory or _default_runner
        self._frontier_factory = frontier_factory or _default_frontier
        self.hub = EventHub()
        self._services: Services | None = None
        self._runner: LocalLLMRunner | None = None
        self._starting: asyncio.Future[int] | None = None  # 시작 중인 실행의 ID 대기
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def services(self) -> Services:
        if self._services is None:
            raise RuntimeError("서버가 아직 준비되지 않았습니다.")
        return self._services

    async def startup(self) -> None:
        """모듈을 만들고 Main.init()으로 기록 복구·모델·프롬프트·설정을 준비한다.

        모델이나 DB를 준비하지 못하면 예외가 나서 서버가 뜨지 않는다.
        """
        from app.data.chat_manager import ChatManager
        from app.data.memo_manager import MemoManager
        from app.data.prompt_manager import PromptManager
        from app.main import Main
        from app.pipeline.answer_generator import AnswerGenerator
        from app.pipeline.leak_guard import LeakGuard
        from app.pipeline.planner import Planner
        from app.pipeline.request_writer import RequestWriter
        from app.privacy.consent_manager import ConsentManager
        from app.privacy.pd_detector import PDDetector
        from app.privacy.privacy_settings import PrivacySettings

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        chat_manager = ChatManager(self._db_path)
        try:
            prompts = PromptManager()
            try:
                # Runner는 만들 때 모델(또는 원격 서버)에 실제로 짧게 물어 연결을 확인한다.
                runner = await asyncio.to_thread(
                    self._runner_factory,
                    prompt_manager=prompts,
                    chat_manager=chat_manager,
                )
            except Exception:
                log.error(
                    "로컬 LLM을 준비하지 못해 서버를 시작할 수 없습니다. 모델 파일 또는 "
                    "원격 서버 키(src/localLLM_API_proxy/keys/llama_api_key*.key)를 "
                    "확인하세요."
                )
                raise
            self._runner = runner
            frontier = self._frontier_factory(prompts)
            privacy_settings = PrivacySettings(chat_manager, runner)
            consent_manager = ConsentManager(chat_manager, runner, emit=self._on_event)
            main = Main(
                call_limit=CALL_LIMIT,
                regen_limit=REGEN_LIMIT,
                budget_limit=BUDGET_LIMIT,
                local_llm_runner=runner,
                frontier_client=frontier,
                chat_manager=chat_manager,
                memo_manager=MemoManager(runner, prompts),
                prompt_manager=prompts,
                pd_detector=PDDetector(runner),
                privacy_settings=privacy_settings,
                consent_manager=consent_manager,
                planner=Planner(runner),
                request_writer=RequestWriter(runner),
                leak_guard=LeakGuard(runner),
                answer_generator=AnswerGenerator(runner, prompts),
                emit=self._on_event,
            )
            await main.init()
        except BaseException:
            await self._close(chat_manager)
            raise
        self._services = Services(chat_manager, privacy_settings, consent_manager, main)

    async def shutdown(self) -> None:
        """진행 중인 실행을 중지하고 모델·DB를 닫는다."""
        if self._services is None:
            return
        main = self._services.main
        for run_id in main.running_ids():
            await main.stop(run_id)
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._close(self._services.chat_manager)
        self._services = None

    async def _close(self, chat_manager: ChatManager) -> None:
        if self._runner is not None:
            await asyncio.to_thread(self._runner.close)
            self._runner = None
        await asyncio.to_thread(chat_manager.close)

    async def start(self, chat_id: int, text: str) -> int:
        """질문 하나의 실행을 시작하고, 실행 기록이 만들어지면 실행 ID를 돌려준다.

        실행 기록을 만들기 전에 실패하면(없는 채팅 등) 그 예외를 그대로 올린다.
        """
        main = self.services.main
        if self._starting is not None or main.running_ids():
            raise BusyError
        started: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._starting = started
        task = asyncio.create_task(main.run_pipeline(chat_id, text))
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        try:
            # Main이 실행 기록을 만들고 run 이벤트를 보낼 때까지 기다린다.
            await asyncio.wait({started, task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._starting = None
        if not started.done():
            task.result()  # 실행 기록을 만들기도 전에 실패했다면 그 오류를 올린다
            raise RuntimeError("실행을 시작하지 못했습니다.")
        return started.result()

    async def stop(self, run_id: int) -> bool:
        """진행 중인 실행을 중지한다. 이미 정리가 끝난 실행이면 False."""
        main = self.services.main
        if not main.is_running(run_id):
            return False
        await main.stop(run_id)
        return True

    async def _on_event(
        self, event: str, data: dict[str, Any], event_id: int | None
    ) -> None:
        """Main·ConsentManager가 이벤트를 저장한 뒤 부른다. 기다리는 스트림을 깨운다."""
        if event == "run" and self._starting is not None and not self._starting.done():
            self._starting.set_result(int(data["run_id"]))
        await self.hub.notify()

    def _finished(self, task: asyncio.Task[None]) -> None:
        """실행 작업이 끝나면 목록에서 빼고, 예상하지 못한 오류는 로그로 남긴다.

        화면에 보낼 종료 이벤트(done·error)는 Main이 finish_run으로 이미 저장했다.
        """
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("실행 작업 오류", exc_info=task.exception())

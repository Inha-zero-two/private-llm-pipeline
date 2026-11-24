"""두 비교의 실행과 중지.

    app.py ──try_reserve · start · stop──▶ ComparisonManager ─┬─▶ LocalStandalone
                                                              ├─▶ FrontierStandalone
                                                              └─▶ ComparisonStore

상태는 모두 이벤트 루프에서만 바꾼다. 모델 호출은 작업 스레드에서 하고 조각을 큐로 넘긴다.
답변은 끝났을 때 한 번만 저장하고, 생성 중인 글은 메모리에서 화면으로 보낸다.
로그에는 질문·답변·키를 남기지 않는다. 예외 원문도 이들을 담을 수 있어 종류만 남긴다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from threading import Thread
from time import monotonic
from typing import Any

from .frontier import FrontierStandalone
from .local import LocalStandalone
from .models import (
    KINDS,
    RETRYABLE_STATUS,
    TIMEOUT_SECONDS,
    Cancellation,
    Cancelled,
    ComparisonSettings,
    ErrorCode,
    Item,
    Kind,
    ModelFailure,
    Record,
    Result,
    RunningRef,
    StandaloneModel,
    Status,
    StreamEvent,
    Turn,
    to_item,
)
from .store import ComparisonStore

log = logging.getLogger(__name__)

# 새 이벤트가 없을 때 연결 유지용 주석을 보내는 간격(초)
KEEPALIVE_SECONDS = 15.0
# 결과 저장에 실패했을 때 한 번 더 시도하기 전 대기(초)
SAVE_RETRY_SECONDS = 0.5

# (이벤트 이름, 데이터). 연결 유지용 주석은 None.
ScreenEvent = tuple[str, dict[str, Any]]


class ComparisonBusyError(Exception):
    """비교 또는 파이프라인이 진행 중이다."""


class NotRetryableError(Exception):
    """다시 시도할 수 있는 조건이 아니다."""


class RecordNotFoundError(Exception):
    """비교 기록이 없다."""


def _kind_of(error: BaseException) -> str:
    """로그에 남길 예외 종류. 원문은 질문이나 응답을 담을 수 있어 남기지 않는다."""
    return str(error) if isinstance(error, ModelFailure) else type(error).__name__


@dataclass
class _Output:
    """모델 하나의 진행 상태."""

    record: Record
    turns: list[Turn] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    running: bool = False
    stopped: bool = False
    cancellation: Cancellation = field(default_factory=Cancellation)
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def item(self) -> Item:
        item = to_item(self.record)
        if self.running:
            item = item.model_copy(update={"answer": "".join(self.text) or None})
        return item

    def stop(self) -> None:
        self.stopped = True
        self.cancellation.cancel()
        self.wake.set()


@dataclass
class _Job:
    """진행 중인 질문 하나. 준비가 끝나면 ready, 모두 저장하면 done."""

    chat_id: int
    run_id: int
    outputs: dict[Kind, _Output] = field(default_factory=dict)
    stop_requested: bool = False
    done: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: set[asyncio.Queue[ScreenEvent]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None

    def publish(self, name: str, data: dict[str, Any]) -> None:
        for queue in self.subscribers:
            queue.put_nowait((name, data))

    def snapshot(self) -> dict[str, Any]:
        return {"items": [_json(o.item()) for o in self.outputs.values()]}


def _json(item: Item) -> dict[str, Any]:
    return item.model_dump(mode="json", by_alias=True)


class ComparisonManager:
    def __init__(
        self,
        settings: ComparisonSettings,
        *,
        store: ComparisonStore,
        local: StandaloneModel,
        frontier: StandaloneModel,
        pipeline_busy: Callable[[], bool],
        latest_run_id: Callable[[int], int | None],
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        """pipeline_busy: 파이프라인이 시작 중이거나 진행 중인지.
        latest_run_id: 채팅의 마지막 파이프라인 실행. 파일을 읽으므로 스레드에서 부른다.
        """
        self._settings = settings
        self._store = store
        self._models: dict[Kind, StandaloneModel] = {
            "local": local,
            "frontier": frontier,
        }
        self._pipeline_busy = pipeline_busy
        self._latest_run_id = latest_run_id
        self._timeout = timeout
        self._reserved = False
        self._job: _Job | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    # 자리 예약. await가 없어 확인과 예약 사이에 다른 요청이 끼어들지 못한다.

    def try_reserve(self) -> bool:
        if self._reserved:
            return False
        self._reserved = True
        return True

    def release(self) -> None:
        self._reserved = False
        self._job = None

    def busy(self) -> RunningRef | None:
        job = self._job
        return RunningRef(chat_id=job.chat_id, run_id=job.run_id) if job else None

    # 채팅과 실행

    async def on_chat_created(self, chat_id: int) -> None:
        if self.enabled:
            await asyncio.to_thread(self._store.register_chat, chat_id)

    async def start(self, chat_id: int, run_id: int, question: str) -> None:
        """잡아 둔 자리에서 두 비교를 시작한다. 비교하지 않거나 실패하면 자리를 놓는다."""
        job = _Job(chat_id, run_id)
        self._job = job
        begun = launched = False
        try:
            if not self.enabled:
                # 꺼 둔 동안 질문한 채팅은 다시 켜도 비교하지 않는다.
                await asyncio.to_thread(self._store.unregister_chat, chat_id)
                return
            if not await asyncio.to_thread(self._store.is_registered, chat_id):
                return
            await asyncio.to_thread(self._store.begin, chat_id, run_id, question)
            begun = True
            await self._load(job, KINDS)
            self._launch(job)
            launched = True
        except Exception:
            if begun:
                await self._abandon(run_id)
            raise
        finally:
            if not launched:
                self._close_job(job)

    def stop(self, run_id: int) -> bool:
        """진행 중인 비교에 중지 신호를 보냈으면 True. 이미 끝난 답은 바꾸지 않는다."""
        job = self._job
        if job is None or job.run_id != run_id or job.done:
            return False
        return self._stop(job)

    def discard(self, chat_id: int) -> None:
        """삭제된 채팅의 진행 중인 비교를 중지한다."""
        job = self._job
        if job is not None and job.chat_id == chat_id and not job.done:
            self._stop(job)

    @staticmethod
    def _stop(job: _Job) -> bool:
        if not job.ready.is_set():
            job.stop_requested = True  # 준비가 끝나는 대로 모델을 부르지 않고 끝낸다
            return True
        running = [o for o in job.outputs.values() if o.running]
        for output in running:
            output.stop()
        return bool(running)

    async def retry(self, run_id: int, kind: Kind) -> None:
        """실패·잘림·중지로 끝난 모델 하나를 같은 줄에서 다시 실행한다."""
        # 두 확인 사이에 await를 두지 않는다.
        if self._pipeline_busy() or not self.try_reserve():
            raise ComparisonBusyError
        job: _Job | None = None
        launched = restarted = False
        try:
            record = await asyncio.to_thread(self._store.get, run_id, kind)
            if record is None:
                raise RecordNotFoundError
            if not await self._retryable(record):
                raise NotRetryableError
            job = _Job(record.chat_id, run_id)
            self._job = job
            await asyncio.to_thread(self._store.restart, run_id, kind)
            restarted = True
            await self._load(job, (kind,))
            self._launch(job)
            launched = True
        except Exception:
            if restarted:
                await self._abandon(run_id)
            raise
        finally:
            if not launched:
                if job is not None:
                    self._close_job(job)
                else:
                    self.release()

    async def shutdown(self) -> None:
        """진행 중인 비교를 중지하고 저장을 마친 뒤 DB를 닫는다."""
        job = self._job
        if job is not None:
            self._stop(job)
            if job.task is not None:
                await asyncio.gather(job.task, return_exceptions=True)
        await asyncio.to_thread(self._store.close)

    # 조회

    async def list_chat(self, chat_id: int) -> tuple[bool, list[Item]]:
        """비교 대상 채팅인지와 저장된 결과. 생성 중인 글도 함께 담는다."""
        registered = await asyncio.to_thread(self._store.is_registered, chat_id)
        latest: int | None = None
        if self.enabled and registered:
            latest = await asyncio.to_thread(self._latest_run_id, chat_id)
        records = await asyncio.to_thread(self._store.list_chat, chat_id)
        if any(r.status is Status.RUNNING and not self._live(r) for r in records):
            # 읽는 사이에 끝난 비교를 중단된 것으로 보지 않도록 다시 읽는다.
            records = await asyncio.to_thread(self._store.list_chat, chat_id)
        items = []
        for record in records:
            live = self._live(record)
            if live is not None:
                items.append(live.item())
                continue
            record = self._settled(record)
            items.append(
                to_item(
                    record,
                    retryable=record.run_id == latest
                    and record.status in RETRYABLE_STATUS,
                )
            )
        return registered, items

    async def events(self, run_id: int) -> AsyncIterator[ScreenEvent | None]:
        """snapshot 한 번, 이어서 token · result, 마지막에 done. None은 연결 유지."""
        job = self._job
        if job is not None and job.run_id == run_id:
            await job.ready.wait()
        else:
            job = None
        if job is None or job.done or not job.outputs:
            records = await asyncio.to_thread(self._store.list_run, run_id)
            items = [_json(to_item(self._settled(r))) for r in records]
            yield "snapshot", {"items": items}
            yield "done", {}
            return
        # snapshot을 만드는 것과 구독 사이에 await가 없어 글이 빠지거나 겹치지 않는다.
        queue: asyncio.Queue[ScreenEvent] = asyncio.Queue()
        job.subscribers.add(queue)
        snapshot = job.snapshot()
        try:
            yield "snapshot", snapshot
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield None
                    continue
                yield event
                if event[0] == "done":
                    return
        finally:
            job.subscribers.discard(queue)

    def _live(self, record: Record) -> _Output | None:
        job = self._job
        if job is None or job.run_id != record.run_id or not job.ready.is_set():
            return None
        output = job.outputs.get(record.kind)
        return output if output is not None and output.running else None

    def _settled(self, record: Record) -> Record:
        """진행 중이 아닌데 running으로 남은 줄은 중단된 것으로 보여 준다."""
        job = self._job
        active = job is not None and job.run_id == record.run_id
        if record.status is not Status.RUNNING or active:
            return record
        return record.model_copy(
            update={"status": Status.FAILED, "error_code": ErrorCode.INTERRUPTED}
        )

    async def _retryable(self, record: Record) -> bool:
        if not self.enabled:
            return False
        if self._settled(record).status not in RETRYABLE_STATUS:
            return False
        if not await asyncio.to_thread(self._store.is_registered, record.chat_id):
            return False
        latest = await asyncio.to_thread(self._latest_run_id, record.chat_id)
        return latest == record.run_id

    # 준비와 정리

    async def _load(self, job: _Job, kinds: tuple[Kind, ...]) -> None:
        """줄과 앞 대화를 읽어 이번 실행 동안 고정한다."""
        records = await asyncio.to_thread(self._store.list_run, job.run_id)
        for record in records:
            output = _Output(record)
            if record.kind in kinds:
                output.running = True
                output.turns = await asyncio.to_thread(
                    self._store.history, job.chat_id, record.kind, job.run_id
                )
            job.outputs[record.kind] = output

    def _launch(self, job: _Job) -> None:
        if job.stop_requested:
            for output in job.outputs.values():
                if output.running:
                    output.stop()
        job.task = asyncio.create_task(self._run(job))
        job.ready.set()

    async def _abandon(self, run_id: int) -> None:
        """줄을 추가한 뒤 시작하지 못했을 때, running으로 남은 줄을 실패로 끝낸다."""
        failed = Result(status=Status.FAILED, error_code=ErrorCode.INTERNAL)
        for kind in KINDS:
            try:
                await asyncio.to_thread(self._store.finish, run_id, kind, failed)
            except Exception as error:
                log.error("Comparison cleanup failed: %s", _kind_of(error))

    def _close_job(self, job: _Job) -> None:
        job.done = True
        self.release()
        job.publish("done", {})
        job.ready.set()

    # 실행

    async def _run(self, job: _Job) -> None:
        try:
            await asyncio.gather(
                *(
                    self._run_output(job, output)
                    for output in job.outputs.values()
                    if output.running
                )
            )
        finally:
            self._close_job(job)

    async def _run_output(self, job: _Job, output: _Output) -> None:
        kind = output.record.kind
        started = monotonic()
        try:
            result = await self._generate(job, output)
        except Exception as error:
            log.error("Comparison %s failed: %s", kind, _kind_of(error))
            result = Result(
                status=Status.FAILED,
                answer="".join(output.text) or None,
                error_code=ErrorCode.INTERNAL,
                model_name=self._models[kind].model_name,
            )
        finally:
            # 시간 초과·오류로 끝났을 때 남은 연결을 닫는다. 정상 종료면 닫을 것이 없다.
            output.cancellation.cancel()
        result.elapsed_ms = round((monotonic() - started) * 1000, 1)
        ended_at = await self._save(output.record, result)
        output.record = output.record.model_copy(
            update={**dict(result), "ended_at": ended_at}
        )
        output.running = False
        job.publish("result", _json(output.item()))

    async def _save(self, record: Record, result: Result) -> str | None:
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(SAVE_RETRY_SECONDS)
            try:
                return await asyncio.to_thread(
                    self._store.finish, record.run_id, record.kind, result
                )
            except Exception as error:
                log.error("Comparison save failed: %s", _kind_of(error))
        # 저장하지 못한 답은 다음 질문의 앞 대화에 쓸 수 없다. 화면에도 실패로 알린다.
        result.status = Status.FAILED
        result.error_code = ErrorCode.INTERNAL
        return None

    async def _generate(self, job: _Job, output: _Output) -> Result:
        """작업 스레드에서 모델을 부르고, 중지와 제한 시간은 여기서 확인한다."""
        kind = output.record.kind
        model = self._models[kind]
        state = StreamEvent(model_name=model.model_name, dropped_pairs=0)
        if output.stopped:
            return self._result(model, state, output, Status.STOPPED)

        loop = asyncio.get_running_loop()
        # 조각, 실패, 또는 끝(None)
        events: asyncio.Queue[StreamEvent | BaseException | None] = asyncio.Queue()

        def emit(value: StreamEvent | BaseException | None) -> None:
            with contextlib.suppress(RuntimeError):  # 서버가 이미 닫힌 경우
                loop.call_soon_threadsafe(events.put_nowait, value)

        def work() -> None:
            question = output.record.question
            try:
                with contextlib.closing(
                    model.stream(output.turns, question, output.cancellation)
                ) as stream:
                    for event in stream:
                        output.cancellation.check()
                        emit(event)
            except Cancelled:
                pass
            except BaseException as error:
                emit(error)
            finally:
                emit(None)

        deadline = monotonic() + self._timeout
        Thread(target=work, daemon=True, name=f"comparison-{kind}").start()
        while True:
            event = await self._next(events, output, deadline)
            if output.stopped:
                return self._result(model, state, output, Status.STOPPED)
            if event is _TIMED_OUT:
                return self._result(
                    model, state, output, Status.FAILED, ErrorCode.TIMEOUT
                )
            if isinstance(event, StreamEvent):
                _merge(state, event)
                if event.text:
                    output.text.append(event.text)
                    job.publish("token", {"kind": kind, "text": event.text})
                continue
            if isinstance(event, BaseException):
                # 글과 끝난 이유를 모두 받은 뒤의 끊김은 답변에 영향이 없다.
                if state.finish_reason is not None and "".join(output.text).strip():
                    return self._finished(model, state, output)
                return self._failed(model, state, output, event)
            return self._finished(model, state, output)

    @staticmethod
    async def _next(
        events: asyncio.Queue[Any], output: _Output, deadline: float
    ) -> Any:
        """다음 조각. 중지 신호가 먼저 오면 None, 제한 시간을 넘으면 _TIMED_OUT."""
        remaining = deadline - monotonic()
        if output.stopped:
            return None
        if remaining <= 0:
            return _TIMED_OUT
        if not events.empty():
            return events.get_nowait()
        get = asyncio.ensure_future(events.get())
        wake = asyncio.ensure_future(output.wake.wait())
        try:
            await asyncio.wait(
                {get, wake}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if get.done():
                return get.result()
            return None if wake.done() else _TIMED_OUT
        finally:
            get.cancel()
            wake.cancel()

    def _finished(
        self, model: StandaloneModel, state: StreamEvent, output: _Output
    ) -> Result:
        """스트림이 끝까지 왔을 때의 결과."""
        failed = Status.FAILED
        reason = state.finish_reason
        if reason is None:
            return self._result(model, state, output, failed, ErrorCode.STREAM_BROKEN)
        if not "".join(output.text).strip():
            return self._result(model, state, output, failed, ErrorCode.EMPTY_RESPONSE)
        if reason == "stop":
            return self._result(model, state, output, Status.OK)
        if reason == "length":
            return self._result(model, state, output, Status.TRUNCATED)
        code = ErrorCode.REJECTED if reason == "content_filter" else ErrorCode.INTERNAL
        return self._result(model, state, output, failed, code)

    def _failed(
        self,
        model: StandaloneModel,
        state: StreamEvent,
        output: _Output,
        error: BaseException,
    ) -> Result:
        code = ErrorCode.INTERNAL
        if isinstance(error, ModelFailure):
            code = error.code
            if error.dropped_pairs is not None:
                state.dropped_pairs = error.dropped_pairs
        log.warning("Comparison %s failed: %s", output.record.kind, _kind_of(error))
        return self._result(model, state, output, Status.FAILED, code)

    @staticmethod
    def _result(
        model: StandaloneModel,
        state: StreamEvent,
        output: _Output,
        status: Status,
        code: ErrorCode | None = None,
    ) -> Result:
        return Result(
            status=status,
            answer="".join(output.text) or None,
            error_code=code,
            model_name=state.model_name,
            dropped_pairs=state.dropped_pairs or 0,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            cost=model.cost(state.input_tokens, state.output_tokens),
        )


_TIMED_OUT = object()


def _merge(state: StreamEvent, event: StreamEvent) -> None:
    """새로 받은 값만 덮어쓴다."""
    for name in (
        "finish_reason",
        "input_tokens",
        "output_tokens",
        "model_name",
        "dropped_pairs",
    ):
        value = getattr(event, name)
        if value is not None:
            setattr(state, name, value)


def create_manager(
    settings: ComparisonSettings,
    *,
    pipeline_busy: Callable[[], bool],
    latest_run_id: Callable[[int], int | None],
) -> ComparisonManager:
    """실제 모델을 부르는 비교 기능. ChatManager가 DB를 준비한 뒤에 부른다."""
    return build_manager(
        settings,
        local=LocalStandalone(),
        frontier=FrontierStandalone(
            api_key=settings.frontier_api_key,
            base_url=settings.frontier_base_url,
            model=settings.frontier_model,
            input_price=settings.input_price,
            output_price=settings.output_price,
        ),
        pipeline_busy=pipeline_busy,
        latest_run_id=latest_run_id,
    )


def build_manager(
    settings: ComparisonSettings,
    *,
    local: StandaloneModel,
    frontier: StandaloneModel,
    pipeline_busy: Callable[[], bool],
    latest_run_id: Callable[[int], int | None],
    timeout: float = TIMEOUT_SECONDS,
) -> ComparisonManager:
    """저장소를 열고 지난 실행을 정리한 관리자. 테스트는 모델만 대역으로 바꿔 부른다."""
    store = ComparisonStore(settings.db_path)
    try:
        store.recover_interrupted()
    except BaseException:
        store.close()
        raise
    return ComparisonManager(
        settings,
        store=store,
        local=local,
        frontier=frontier,
        pipeline_busy=pipeline_busy,
        latest_run_id=latest_run_id,
        timeout=timeout,
    )

"""설계 5.1: 공통 Llama API를 사용하는 실행·중지·재생성 모듈."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Any

from app.models.enums import PromptIdx
from app.models.local_llm_outputs import ParsedResult

from .local_res_parser import OutputFormatError, parser

if TYPE_CHECKING:
    from .context_manager import ContextManager

RequestResult = tuple[str, bool, int | None, int | None, str]
# 수동 호출은 본문, 작업 호출은 파싱 결과가 첫 값이다. 나머지 순서는 같다.
ParsedRequestResult = tuple[ParsedResult | None, bool, int | None, int | None, str]
# 최초 응답을 포함한 형식 불일치 횟수. 요청문 누출 재작성 상한과는 별개다.
MAX_FORMAT_FAILURES = 3
N_CTX = 32768


class LocalLLMRunner:
    def __init__(
        self,
        *,
        model_path: str = "models/gemma.gguf",
        timeout: float = 120.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._model_path = model_path
        self._timeout = timeout
        self._busy = Lock()
        self._state_lock = Lock()
        self._stop_event = Event()
        self._closed = False
        self._llm: Any = None
        # 모델은 하나만 유지하고 컨텍스트 보관소는 실행마다 분리한다.
        self.context_managers: dict[str, ContextManager] = {}
        self.model_load()

    def get_context_manager(self, run_id: str) -> ContextManager:
        """같은 실행에는 같은 관리자, 다른 실행에는 별도 관리자를 반환한다."""
        from .context_manager import ContextManager

        with self._state_lock:
            if run_id not in self.context_managers:
                self.context_managers[run_id] = ContextManager(self, run_id)
            return self.context_managers[run_id]

    def model_load(self) -> bool:
        """공통 Llama 생성자로 모델을 준비하고 실제 생성 응답을 확인한다."""
        if self._closed:
            raise RuntimeError("Runner is closed")
        if self._llm is not None:
            return True
        # 네이티브 전환 시 이 임포트만 `from llama_cpp import Llama`로 바꾼다.
        # 키 탐색·원격 주소·원격 호출 옵션은 래퍼에서만 처리한다.
        from src.localLLM_API_proxy.llama_wrapper import Llama

        self._llm = Llama(
            model_path=self._model_path,
            n_ctx=N_CTX,
            n_gpu_layers=-1,
        )
        try:
            result = self.manual_request(
                "Reply OK only.", "Answer briefly.", max_tokens=32
            )
            if not result[1] or not result[0].strip():
                raise ConnectionError(f"Model response check failed: {result[4]}")
        except BaseException:
            # 초기화 실패 시 연결만 정리하고 원래 예외를 그대로 전파한다.
            self.close()
            raise
        return True

    def manual_request(
        self,
        input_: str,
        system_prompt: str,
        *,
        run_id: str | None = None,
        max_tokens: int = 2048,
        log_call: bool = True,
        on_token: Callable[[str], None] | None = None,
        interruptible: bool = True,
    ) -> RequestResult:
        """직접 받은 입력·시스템 프롬프트로 생성하고 일반 호출 로그를 남긴다.

        압축 호출은 log_call=False, interruptible=False로 끝까지 수신한다.
        """
        output: list[str] = []
        started = monotonic()
        try:
            return self._generate(
                input_,
                system_prompt,
                output=output,
                max_tokens=max_tokens,
                on_token=on_token,
                interruptible=interruptible,
            )
        finally:
            if log_call:
                self._log_call(
                    run_id,
                    input_,
                    "".join(output),
                    prompt_idx=None,
                    prompt_version=None,
                    elapsed_ms=(monotonic() - started) * 1000,
                    format_valid=None,
                )

    def _generate(
        self,
        input_: str,
        system_prompt: str,
        *,
        output: list[str],
        max_tokens: int,
        on_token: Callable[[str], None] | None = None,
        stop_signal: Event | None = None,
        interruptible: bool = True,
    ) -> RequestResult:
        """공통 스트리밍 수신. 파싱·컨텍스트·로그는 호출한 공개 함수가 처리한다."""
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("A request is still running or being cleaned up")
        with self._state_lock:
            if self._closed:
                self._busy.release()
                raise RuntimeError("Runner is closed")
            if stop_signal is None:
                # 압축은 바깥 실행의 중지 신호를 보존하되 수신 중에는 무시한다.
                stop_signal = Event() if interruptible else self._stop_event
            self._stop_event = stop_signal
            if interruptible and stop_signal.is_set():
                self._busy.release()
                return "", False, 0, 0, "stopped"
        # 수신 정리 신호가 정상 응답 후에도 요청 전체를 중지시키지 않도록 분리한다.
        cancel = Event()
        events: Queue[dict[str, Any] | Exception | None] = Queue()

        def receive() -> None:
            try:
                if interruptible and stop_signal.is_set():
                    return
                stream = self._llm.create_chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": input_},
                    ],
                    stream=True,
                    max_tokens=max_tokens,
                )
                with closing(stream):
                    for chunk in stream:
                        if cancel.is_set() or (interruptible and stop_signal.is_set()):
                            break
                        events.put(chunk)
            except Exception as exc:
                # 스레드 경계를 넘겨 전달하기 위한 수집이며 호출자에서 다시 던진다.
                events.put(exc)
            finally:
                self._busy.release()
                events.put(None)

        deadline = monotonic() + self._timeout
        worker = Thread(target=receive, daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self._busy.release()
            raise
        text = output
        input_tokens = output_tokens = None
        finish_reason = None
        try:
            while True:
                if interruptible and stop_signal.is_set():
                    return "".join(text), False, input_tokens, output_tokens, "stopped"
                if monotonic() >= deadline:
                    raise TimeoutError("Local LLM request exceeded its time limit")
                try:
                    event = events.get(timeout=0.05)
                except Empty:
                    continue
                # 대기 중 stop이 오면 종료 표식이 먼저 도착할 수 있으므로 다시 확인한다.
                if interruptible and stop_signal.is_set():
                    return "".join(text), False, input_tokens, output_tokens, "stopped"
                if isinstance(event, Exception):
                    raise event
                if event is None:
                    answer = "".join(text)
                    if finish_reason is None:
                        if not answer.strip():
                            raise RuntimeError("Local LLM returned no response")
                        raise RuntimeError(
                            "Local LLM stream ended without a finish reason"
                        )
                    success = finish_reason in {"stop", "length"}
                    return answer, success, input_tokens, output_tokens, finish_reason
                # usage는 choices=[]인 마지막 별도 청크에 올 수 있다.
                usage = event.get("usage") or {}
                input_tokens = usage.get("prompt_tokens", input_tokens)
                output_tokens = usage.get("completion_tokens", output_tokens)
                choices = event.get("choices") or []
                if choices:
                    piece = choices[0].get("delta", {}).get("content") or ""
                    if piece:
                        text.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    finish_reason = choices[0].get("finish_reason") or finish_reason
        finally:
            cancel.set()

    def request(
        self,
        run_id: str,
        input_: str,
        prompt_idx: PromptIdx | str,
        chat_id: str | None,
        *,
        use_context: bool = False,
        system_prompt: str | None = None,
        prompt_version: str | None = None,
        max_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> ParsedRequestResult:
        """파싱 결과와 사용량을 반환한다. 형식 불일치가 3회면 실패값을 반환한다."""
        prompt_idx = PromptIdx(prompt_idx)
        # TODO 6.1: system_prompt, prompt_version = self.prompt_manager.get_prompt(prompt_idx)
        # PromptManager는 (프롬프트 본문, 버전)을 함께 반환한다. 현재는 인자로 전달한다.
        if system_prompt is None:
            raise NotImplementedError(
                "PromptManager is not connected; pass system_prompt"
            )
        context_id = None
        manager = None
        model_input = input_
        if use_context:
            if chat_id is None:
                raise ValueError("컨텍스트를 사용하는 작업에는 chat_id가 필요함")
            manager = self.get_context_manager(run_id)
            context_id = manager.get_id(chat_id, prompt_idx)
            context = manager.get_context(context_id)
            if context:
                model_input = (
                    f"Previous context:\n{context}\n\nCurrent input:\n{input_}"
                )
        # 최초 생성·파싱·재생성이 같은 중지 신호를 사용한다.
        stop_signal = Event()
        input_tokens: int | None = 0
        output_tokens: int | None = 0
        last_output = ""
        for _ in range(MAX_FORMAT_FAILURES):
            output: list[str] = []
            format_valid = None
            started = monotonic()
            try:
                result = self._generate(
                    model_input,
                    system_prompt,
                    output=output,
                    max_tokens=max_tokens,
                    on_token=on_token,
                    stop_signal=stop_signal,
                )
                # 재생성 사용량도 합산한다. 하나라도 모르면 합계도 None으로 둔다.
                input_tokens = (
                    input_tokens + result[2]
                    if input_tokens is not None and result[2] is not None
                    else None
                )
                output_tokens = (
                    output_tokens + result[3]
                    if output_tokens is not None and result[3] is not None
                    else None
                )
                if not result[1] or stop_signal.is_set():
                    # 중지된 부분 응답은 파싱·재생성하거나 컨텍스트에 추가하지 않는다.
                    return (
                        result[0] or last_output,
                        False,
                        input_tokens,
                        output_tokens,
                        "stopped" if stop_signal.is_set() else result[4],
                    )
                # 파서 호출만 감싸므로 같은 타입의 래퍼 오류도 그대로 전파한다.
                try:
                    parsed = parser(result[0], prompt_idx)
                except OutputFormatError:
                    format_valid = False
                    last_output = result[0]
                    if stop_signal.is_set():
                        return result[0], False, input_tokens, output_tokens, "stopped"
                    continue
                format_valid = True
            finally:
                # 시도마다 새 입력·원시 출력·검사 결과를 한 번만 기록한다.
                self._log_call(
                    run_id,
                    input_,
                    "".join(output),
                    prompt_idx=prompt_idx,
                    prompt_version=prompt_version,
                    elapsed_ms=(monotonic() - started) * 1000,
                    format_valid=format_valid,
                )
            if stop_signal.is_set():
                return result[0], False, input_tokens, output_tokens, "stopped"
            if manager is not None and context_id is not None:
                # 입력과 유효한 최종 응답을 여기서 한 번만 누적한다. 재시도 중에는 추가하지 않는다.
                manager.append(
                    context_id, f"\nUser: {input_}\nAssistant: {result[0]}\n"
                )
            # 압축 중 stop은 요약 반영까지 마친 다음 바깥 요청에 적용한다.
            if stop_signal.is_set():
                return result[0], False, input_tokens, output_tokens, "stopped"
            return parsed, True, input_tokens, output_tokens, result[4]
        return None, False, input_tokens, output_tokens, "parse_failed"

    def regen(
        self,
        input_: str,
        system_prompt: str,
        *,
        run_id: str | None = None,
        use_context: bool = False,
        context_id: int | None = None,
        max_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> RequestResult:
        """기존 컨텍스트 사용 여부에 따라 같은 입력을 다시 생성한다. 이력은 추가하지 않는다."""
        model_input = input_
        if use_context:
            if context_id is None:
                raise ValueError("컨텍스트를 사용하는 재생성에는 context_id가 필요함")
            if run_id is None:
                raise ValueError("컨텍스트를 사용하는 재생성에는 run_id가 필요함")
            context = self.get_context_manager(run_id).get_context(context_id)
            if context:
                model_input = (
                    f"Previous context:\n{context}\n\nCurrent input:\n{input_}"
                )
        output: list[str] = []
        started = monotonic()
        try:
            return self._generate(
                model_input,
                system_prompt,
                output=output,
                max_tokens=max_tokens,
                on_token=on_token,
            )
        finally:
            self._log_call(
                run_id,
                input_,
                "".join(output),
                prompt_idx=None,
                prompt_version=None,
                elapsed_ms=(monotonic() - started) * 1000,
                format_valid=None,
            )

    def _log_call(
        self,
        run_id: str | None,
        new_input: str,
        output: str,
        *,
        prompt_idx: PromptIdx | None,
        prompt_version: str | None,
        elapsed_ms: float,
        format_valid: bool | None,
    ) -> None:
        """호출 메타데이터와 새 입력·출력의 저장 연결점. 실제 DB 모듈은 미구현이다."""
        # TODO: self.chat_manager.log_event(
        #     run_id, "llm_call", {
        #         "prompt_idx": prompt_idx, "prompt_version": prompt_version,
        #         "elapsed_ms": elapsed_ms, "format_valid": format_valid,
        #         "input": new_input, "output": output,
        #     }
        # )
        # 수동 호출·단독 regen은 형식 검사를 하지 않으므로 format_valid=None이다.
        # 컨텍스트 전체/시스템 프롬프트/인증키를 이 payload에 추가하지 않는다.

    def tokenize(self, text: str) -> list[int]:
        """5.4에서도 동일한 모델의 실제 토크나이저를 사용한다."""
        with self._busy:
            if self._closed:
                raise RuntimeError("Runner is closed")
            return self._llm.tokenize(text.encode("utf-8"), add_bos=False, special=True)

    def stop(self) -> None:
        """중지 신호만 설정하므로 수신 스레드의 네트워크 응답을 기다리지 않는다."""
        with self._state_lock:
            self._stop_event.set()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            self._stop_event.set()
        # 네이티브 엔진에서도 생성/토큰화 도중 모델 메모리를 해제하지 않는다.
        # stop()은 즉시 반환하고, close()만 진행 중인 작업 정리를 기다린다.
        with self._busy:
            if self._llm is not None:
                self._llm.close()

    def __enter__(self) -> LocalLLMRunner:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

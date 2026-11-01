"""설계 5.1: 공통 Llama API를 사용하는 실행·중지·재생성 모듈."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context_manager import ContextManager

RequestResult = tuple[str, bool, int | None, int | None, str]
# 반환 순서: 본문, 성공, 입력 토큰, 출력 토큰, 종료 사유.
# 파싱 실패용 상한이며 요청문 누출에 의한 재작성 상한과는 별개다.
MAX_REGEN = 3
N_CTX = 32768
CONTEXT_TASKS: frozenset[str | int] = frozenset()
# 작업별 PromptIdx 정책은 아직 없으므로 호출자가 context_tasks로 지정한다.


class LocalLLMRunner:
    def __init__(
        self,
        *,
        model_path: str = "models/gemma.gguf",
        timeout: float = 120.0,
        context_tasks: frozenset[str | int] = CONTEXT_TASKS,
        context_manager: ContextManager | None = None,
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
        self.context_tasks = context_tasks
        self.model_load()
        from .context_manager import ContextManager

        self.context_manager = context_manager or ContextManager(self)

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
            if not result[1]:
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
        max_tokens: int = 2048,
        log_call: bool = True,
        new_input: str | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> RequestResult:
        """스트리밍 생성. 중지는 부분 답변을 반환하고 호출 오류는 그대로 던진다.

        수신 스레드가 응답을 기다려도 stop()은 별도 스레드에서 호출할 수 있다.
        중지 후 다음 청크/읽기 timeout으로 수신이 정리되기 전 새 생성은 거부한다.
        on_token은 본문 청크를 받을 때 호출한다. 최종 반환 튜플은 그대로 유지한다.
        """
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("A request is still running or being cleaned up")
        with self._state_lock:
            if self._closed:
                self._busy.release()
                raise RuntimeError("Runner is closed")
            cancel = Event()
            self._stop_event = cancel
        events: Queue[dict[str, Any] | Exception | None] = Queue()

        def receive() -> None:
            try:
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
                        if cancel.is_set():
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
        text: list[str] = []
        input_tokens = output_tokens = None
        finish_reason = None
        try:
            while True:
                if cancel.is_set():
                    return "".join(text), False, input_tokens, output_tokens, "stopped"
                if monotonic() >= deadline:
                    raise TimeoutError("Local LLM request exceeded its time limit")
                try:
                    event = events.get(timeout=0.05)
                except Empty:
                    continue
                # 대기 중 stop이 오면 종료 표식이 먼저 도착할 수 있으므로 다시 확인한다.
                if cancel.is_set():
                    return "".join(text), False, input_tokens, output_tokens, "stopped"
                if isinstance(event, Exception):
                    raise event
                if event is None:
                    answer = "".join(text)
                    if not answer.strip():
                        raise RuntimeError("Local LLM returned no response")
                    if finish_reason is None:
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
            if log_call:
                self._log_call(
                    input_ if new_input is None else new_input, "".join(text)
                )

    def request(
        self,
        run_id: str,
        input_: str,
        prompt_idx: str | int,
        chat_id: str,
        *,
        system_prompt: str | None = None,
        max_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> RequestResult:
        """작업별 컨텍스트를 사용하는 호출. 현재는 파싱 전 원시 본문을 반환한다."""
        # TODO 6.1: system_prompt, version = self.prompt_manager.get_prompt(prompt_idx)
        if system_prompt is None:
            raise NotImplementedError(
                "PromptManager is not connected; pass system_prompt"
            )
        context_id = None
        model_input = input_
        if prompt_idx in self.context_tasks:
            context_id = self.context_manager.get_id(chat_id, prompt_idx)
            context = self.context_manager.get_context(context_id)
            if context:
                model_input = (
                    f"Previous context:\n{context}\n\nCurrent input:\n{input_}"
                )
        result = self.manual_request(
            model_input,
            system_prompt,
            max_tokens=max_tokens,
            new_input=input_,
            on_token=on_token,
        )
        # TODO 5.2: valid, error = self.local_res_parser.check(result[0], prompt_idx)
        # TODO 5.2: for _ in range(MAX_REGEN):  # 출력 형식 실패만 재시도
        #     if valid or not result[1]: break
        #     result = self.regen(model_input, result[0], error, system_prompt)
        #     valid, error = self.local_res_parser.check(result[0], prompt_idx)
        # 일반 호출 로그는 manual_request의 _log_call 한 곳에서만 연결한다.
        # 이전 컨텍스트나 model_input 전체를 다시 로그로 넘기지 않는다.
        # success는 생성 완료이며 5.2 스키마 검증 통과를 의미하지 않는다.
        if result[1] and context_id is not None:
            self.context_manager.append(
                context_id, f"\nUser: {input_}\nAssistant: {result[0]}\n"
            )
        return result

    def regen(
        self,
        input_: str,
        previous_output: str,
        error: str,
        system_prompt: str,
        *,
        max_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> RequestResult:
        """원래 입력 뒤에 직전 출력·형식 오류를 붙여 동일한 설정으로 재생성한다."""
        revised = (
            f"{input_}\n\nPrevious output:\n{previous_output}\n"
            f"Format error:\n{error}\nRegenerate the output in the required format."
        )
        return self.manual_request(
            revised,
            system_prompt,
            max_tokens=max_tokens,
            new_input=f"Previous output:\n{previous_output}\nFormat error:\n{error}",
            on_token=on_token,
        )

    def _log_call(self, new_input: str, output: str) -> None:
        """로그 저장 모듈 연결용 스켈레톤. 매번 새 입력·출력만 저장할 자리다."""
        # TODO: self.chat_manager.log_event(
        #     None, "llm_call", {"input": new_input, "output": output}
        # )
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

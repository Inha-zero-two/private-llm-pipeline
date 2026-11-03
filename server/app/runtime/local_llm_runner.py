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
        _stop_signal: Event | None = None,
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
            stop_signal = _stop_signal if _stop_signal is not None else Event()
            self._stop_event = stop_signal
            if stop_signal.is_set():
                self._busy.release()
                return "", False, 0, 0, "stopped"
        # 수신 정리 신호가 정상 응답 후에도 요청 전체를 중지시키지 않도록 분리한다.
        cancel = Event()
        events: Queue[dict[str, Any] | Exception | None] = Queue()

        def receive() -> None:
            try:
                if stop_signal.is_set():
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
                        if cancel.is_set() or stop_signal.is_set():
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
                if stop_signal.is_set():
                    return "".join(text), False, input_tokens, output_tokens, "stopped"
                if monotonic() >= deadline:
                    raise TimeoutError("Local LLM request exceeded its time limit")
                try:
                    event = events.get(timeout=0.05)
                except Empty:
                    continue
                # 대기 중 stop이 오면 종료 표식이 먼저 도착할 수 있으므로 다시 확인한다.
                if stop_signal.is_set():
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
        run_id: str | None,
        input_: str,
        prompt_idx: PromptIdx | str,
        chat_id: str | None,
        *,
        use_context: bool = False,
        system_prompt: str | None = None,
        max_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
    ) -> ParsedRequestResult:
        """파싱 결과와 사용량을 반환한다. 형식 불일치가 3회면 실패값을 반환한다."""
        prompt_idx = PromptIdx(prompt_idx)
        # TODO 6.1: system_prompt, version = self.prompt_manager.get_prompt(prompt_idx)
        if system_prompt is None:
            raise NotImplementedError(
                "PromptManager is not connected; pass system_prompt"
            )
        context_id = None
        model_input = input_
        if use_context:
            if chat_id is None:
                raise ValueError("컨텍스트를 사용하는 작업에는 chat_id가 필요함")
            context_id = self.context_manager.get_id(chat_id, prompt_idx)
            context = self.context_manager.get_context(context_id)
            if context:
                model_input = (
                    f"Previous context:\n{context}\n\nCurrent input:\n{input_}"
                )
        # 최초 생성·파싱·재생성이 같은 중지 신호를 사용한다.
        stop_signal = Event()
        result = self.manual_request(
            model_input,
            system_prompt,
            max_tokens=max_tokens,
            new_input=input_,
            on_token=on_token,
            _stop_signal=stop_signal,
        )
        failures = 0
        input_tokens: int | None = 0
        output_tokens: int | None = 0
        while True:
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
                    result[0],
                    False,
                    input_tokens,
                    output_tokens,
                    "stopped" if stop_signal.is_set() else result[4],
                )
            try:
                parsed = parser(result[0], prompt_idx)
            except OutputFormatError as exc:
                if stop_signal.is_set():
                    return result[0], False, input_tokens, output_tokens, "stopped"
                failures += 1
                if failures >= MAX_FORMAT_FAILURES:
                    return None, False, input_tokens, output_tokens, "parse_failed"
                # 파서의 형식 오류만 재시도한다. 래퍼·통신·코드 오류는 잡지 않는다.
                regenerated = self.regen(
                    model_input,
                    result[0],
                    str(exc),
                    system_prompt,
                    max_tokens=max_tokens,
                    on_token=on_token,
                    _stop_signal=stop_signal,
                )
                # 새 본문이 나오기 전에 중지되면 이미 받은 직전 본문을 보존한다.
                if regenerated[4] == "stopped" and not regenerated[0]:
                    regenerated = (result[0], *regenerated[1:])
                result = regenerated
                continue
            if stop_signal.is_set():
                return result[0], False, input_tokens, output_tokens, "stopped"
            if context_id is not None:
                # 유효한 최종 응답만 원문으로 남긴다. 실패한 생성문은 호출 로그에만 남는다.
                self.context_manager.append(
                    context_id, f"\nUser: {input_}\nAssistant: {result[0]}\n"
                )
            return parsed, True, input_tokens, output_tokens, result[4]

    def regen(
        self,
        input_: str,
        previous_output: str,
        error: str,
        system_prompt: str,
        *,
        max_tokens: int = 2048,
        on_token: Callable[[str], None] | None = None,
        _stop_signal: Event | None = None,
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
            _stop_signal=_stop_signal,
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

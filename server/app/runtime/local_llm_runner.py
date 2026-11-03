"""설계 5.1: 공통 Llama API를 사용하는 실행·중지·재생성 모듈."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Any

from .local_res_parser import OutputFormatError, ParsedResult, PromptIdx, parser

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
        """프로그램에서 공유할 모델 하나와 실행별 컨텍스트 보관소를 준비한다.

        timeout은 생성 한 번의 전체 대기시간이다. 초기화 중 짧은 생성 요청으로
        실제 응답을 확인하며, 모델 준비나 응답 확인 실패는 호출자에게 전파한다.
        """
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
        """run_id에 연결된 ContextManager를 조회하고 없으면 생성한다.

        같은 실행은 같은 인스턴스를 재사용하며, 실행별 관리자는 이 Runner의
        모델을 공유한다. 잠금 안에서 조회·생성해 같은 실행의 중복 생성을 막는다.
        """
        from .context_manager import ContextManager

        with self._state_lock:
            if run_id not in self.context_managers:
                self.context_managers[run_id] = ContextManager(self, run_id)
            return self.context_managers[run_id]

    def model_load(self) -> bool:
        """공통 Llama 생성자로 모델을 준비하고 짧은 응답을 확인한 뒤 True를 반환한다.

        이미 준비된 모델은 재사용한다. 새 모델은 최대 32토큰의 요청으로 확인하고,
        이 확인이 실패하면 자원을 닫고 원래 예외를 다시 던진다.
        """
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
        """입력과 시스템 프롬프트를 그대로 사용해 한 번 생성하고 원문을 반환한다.

        반환은 (원문, 성공 여부, 입력 토큰, 출력 토큰, 종료 사유)이며,
        알 수 없는 토큰 수는 None이다. 컨텍스트 조회·누적과 결과 파싱은 수행하지 않는다.
        일반 호출은 오류·중지를 포함해 로그 연결점을 거친다. 압축 호출은
        log_call=False로 일반 로그를 끄고 interruptible=False로 stop을 보류한다.
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
        """별도 스레드로 스트림을 수신하고 본문·사용량·종료 사유를 모은다.

        output에 받은 본문을 누적하고 on_token에는 본문 청크를 즉시 전달한다.
        중지하면 수신한 부분 원문과 stopped를 반환하며, 수신 오류는 다시 던진다.
        interruptible=False는 stop만 보류하고 호출 시간 제한은 유지한다.
        파싱·컨텍스트 누적·로그 기록은 이 함수를 호출한 공개 함수가 담당한다.
        """
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
            """모델의 청크와 예외를 큐로 전달하고, 스트림 정리 후 None으로 끝을 알린다.

            네트워크 수신을 별도 스레드에서 수행해 호출 측이 stop을 확인할 수 있게 한다.
            모델 접근 잠금은 이 스레드가 실제 수신을 마친 뒤 해제한다.
            """
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
                            # 한 청크에는 여러 토큰이 들어갈 수 있다.
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
        """작업별로 생성·파싱하고 선택한 실행의 컨텍스트에 최종 대화를 누적한다.

        use_context=True이면 run_id와 채팅·작업으로 기존 이력을 찾는다.
        형식 오류는 같은 입력으로 최초 포함 3회까지 생성하고 각 시도를 로그 연결점에 전달한다.
        성공하면 (파싱 결과, True, 누적 입력 토큰, 누적 출력 토큰, 종료 사유),
        3회 형식 실패면 (None, False, 누적 입력 토큰, 누적 출력 토큰, parse_failed)를 반환한다.
        중지는 부분 원문을 반환하고, 파싱 형식 오류 이외의 호출 오류는 전파한다.
        """
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
        # 재시도에서도 이 입력을 재사용해 같은 요청이 이력에 여러 번 쌓이지 않게 한다.
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
        """같은 입력·시스템 프롬프트로 한 번 더 생성하고 수동 호출과 같은 튜플을 반환한다.

        use_context=True이면 run_id의 context_id에 저장된 이력을 입력 앞에 붙인다.
        이전 출력이나 수정 지시를 추가하지 않으며, 파싱과 이력 누적도 수행하지 않는다.
        로그에는 기존 이력을 제외한 이번 입력과 새 원문 응답만 전달한다.
        """
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
        """생성 한 번의 메타데이터와 새 입력·출력을 저장할 연결점이다.

        실행·작업·프롬프트 버전·소요 시간·형식 검사 결과를 llm_call에 전달할 자리다.
        아직 ChatManager 호출은 주석 상태이므로 실제 DB 저장은 수행하지 않는다.
        """
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
        """문자열을 생성 모델과 같은 토크나이저로 변환해 토큰 ID 목록을 반환한다.

        ContextManager는 이 목록의 길이로 컨텍스트 사용량을 계산한다.
        모델 접근 잠금을 사용하므로 진행 중인 수신이 정리될 때까지 기다릴 수 있다.
        """
        with self._busy:
            if self._closed:
                raise RuntimeError("Runner is closed")
            return self._llm.tokenize(text.encode("utf-8"), add_bos=False, special=True)

    def stop(self) -> None:
        """다른 스레드에서도 현재 요청의 중지 신호를 설정하고 즉시 반환한다.

        생성 함수가 신호를 확인하면 받은 부분 원문을 반환한다. 진행 중인 압축은
        완료 후 반영하며, 원격 서버의 추론 자체를 취소하는 기능은 아니다.
        """
        with self._state_lock:
            self._stop_event.set()

    def close(self) -> None:
        """Runner를 닫힌 상태로 바꾸고 진행 중인 수신 정리 후 모델 자원을 해제한다.

        중지 신호를 먼저 보내며, 모델 접근 잠금을 얻을 때까지 기다린다.
        프로그램 종료 시 호출하고 이후에는 이 인스턴스로 새 요청을 시작하지 않는다.
        """
        with self._state_lock:
            self._closed = True
            self._stop_event.set()
        # 네이티브 엔진에서도 생성/토큰화 도중 모델 메모리를 해제하지 않는다.
        # stop()은 즉시 반환하고, close()만 진행 중인 작업 정리를 기다린다.
        with self._busy:
            if self._llm is not None:
                self._llm.close()

    def __enter__(self) -> LocalLLMRunner:
        """with 문에서 사용할 Runner 자신을 반환한다. 모델 준비는 생성자가 수행한다."""
        return self

    def __exit__(self, *_: object) -> None:
        """with 블록을 나갈 때 close를 호출한다. 블록에서 발생한 예외는 억제하지 않는다."""
        self.close()

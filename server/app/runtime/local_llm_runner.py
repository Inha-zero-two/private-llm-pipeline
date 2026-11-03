"""설계 5.1: Llama 모델에 응답 요청, 생성 중지 및 재생성."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Any

from .local_res_parser import OutputFormatError, ParsedResult, PromptIdx, parser

if TYPE_CHECKING:
    from .context_manager import ContextManager

# 직접 요청한 결과: 답변, 성공 여부, 입력 토큰 수, 출력 토큰 수, 종료 사유
RequestResult = tuple[str, bool, int | None, int | None, str]
# 작업별 요청 결과: 파싱 결과, 성공 여부, 입력 토큰 수, 출력 토큰 수, 종료 사유
ParsedRequestResult = tuple[ParsedResult | None, bool, int | None, int | None, str]
# 최초 응답을 포함한 출력 형식 오류 허용 횟수
MAX_FORMAT_FAILURES = 3
# 모델의 전체 입력·출력 토큰 한도
N_CTX = 32768


class LocalLLMRunner:
    def __init__(
        self,
        *,
        model_path: str = "models/gemma.gguf",
        timeout: float = 120.0,
    ) -> None:
        """공유 모델과 실행별 대화 관리 객체 초기화.

        model_path: 모델 파일 경로. timeout: 한 번의 응답을 기다리는 최대 시간(초).
        초기화 시 짧은 요청으로 실제 응답 확인. 모델 준비나 응답 확인 실패 시 예외 전달.
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._model_path = model_path
        self._timeout = timeout
        self._busy = Lock()  # 모델 사용 중 중복 접근 방지용 잠금
        self._state_lock = Lock()  # 종료 상태와 중지 신호 변경용 잠금
        self._stop_event: Event | None = None  # 진행 중인 요청의 중지 신호
        self._active_run_id: str | None = None  # 진행 중인 요청의 실행 ID
        self._closed = False
        self._llm: Any = None
        # 실행 ID별 대화 관리 객체. 모든 객체가 모델 하나를 공유
        self.context_managers: dict[str, ContextManager] = {}
        self.model_load()

    @contextmanager
    def _request_scope(
        self, run_id: str | None, *, interruptible: bool = True
    ) -> Iterator[Event]:
        """요청의 실행 ID와 중지 신호 등록 후 요청 종료 시 정리.

        파싱·재생성·대화 저장까지 같은 실행으로 관리.
        같은 실행의 대화 압축은 기존 중지 신호를 공유하고 완료까지 처리.
        """
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Runner is closed")
            stop_signal = self._stop_event
            owns_request = stop_signal is None  # 실행 ID와 중지 신호 정리 담당 여부
            if stop_signal is None:
                stop_signal = Event()
                self._stop_event = stop_signal
                self._active_run_id = run_id
            elif interruptible or self._active_run_id != run_id:
                raise RuntimeError("A request is still running or being cleaned up")
        try:
            yield stop_signal
        finally:
            if owns_request:
                with self._state_lock:
                    self._stop_event = None
                    self._active_run_id = None

    def get_context_manager(self, run_id: str) -> ContextManager:
        """실행 ID에 해당하는 ContextManager 조회 또는 생성.

        같은 실행 ID는 기존 객체 재사용. 새 객체도 이 Runner의 모델을 공유.
        동시에 같은 실행 ID로 조회할 때 객체가 중복 생성되지 않도록 잠금 사용.
        """
        from .context_manager import ContextManager

        with self._state_lock:
            if run_id not in self.context_managers:
                self.context_managers[run_id] = ContextManager(self, run_id)
            return self.context_managers[run_id]

    def model_load(self) -> bool:
        """모델을 준비하고 짧은 응답으로 연결 확인 후 True 반환.

        이미 준비된 모델은 재사용. 새 모델은 최대 32토큰의 응답으로 확인.
        응답 확인 실패 시 자원 정리 후 원래 예외 전달.
        """
        if self._closed:
            raise RuntimeError("Runner is closed")
        if self._llm is not None:
            return True
        # llama-cpp-python 직접 사용 시 `from llama_cpp import Llama`로 변경
        # 인증키 검색, 서버 주소 및 원격 요청 설정은 기존 래퍼에서 처리
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
            # 초기화 실패 시 연결 정리 후 원래 예외 전달
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
        """직접 지정한 입력과 시스템 프롬프트로 응답 요청 후 답변 반환.

        반환값: (답변, 성공 여부, 입력 토큰 수, 출력 토큰 수, 종료 사유).
        사용량을 받지 못한 경우 토큰 수는 None. 이전 대화 추가와 결과 파싱은 생략.
        run_id: 실행 ID. stop(run_id)로 중지할 요청은 같은 ID 전달 필요.
        log_call: 일반 요청 로그 함수 호출 여부. interruptible: stop으로 중지 가능 여부.
        대화 압축 시 두 설정 모두 False로 지정해 일반 로그를 생략하고 압축 완료까지 수신.
        """
        with self._request_scope(run_id, interruptible=interruptible) as stop_signal:
            output: list[str] = []
            started = monotonic()
            try:
                return self._generate(
                    input_,
                    system_prompt,
                    stop_signal=stop_signal,
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
        stop_signal: Event,
        output: list[str],
        max_tokens: int,
        on_token: Callable[[str], None] | None = None,
        interruptible: bool = True,
    ) -> RequestResult:
        """별도 스레드에서 생성 중인 답변을 받아 본문, 사용량 및 종료 사유 반환.

        output: 수신한 답변 조각 목록. on_token: 답변 일부를 받을 때 실행할 함수.
        중지 시 지금까지 받은 답변과 stopped 반환. 수신 오류 발생 시 예외 전달.
        interruptible=False일 때 중지 신호는 무시하지만 응답 대기 시간 제한은 유지.
        결과 파싱, 이전 대화 저장 및 로그 기록은 이 함수를 호출한 함수에서 처리.
        """
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("A request is still running or being cleaned up")
        with self._state_lock:
            if self._closed:
                self._busy.release()
                raise RuntimeError("Runner is closed")
            if interruptible and stop_signal.is_set():
                self._busy.release()
                return "", False, 0, 0, "stopped"
        # 수신 스레드 종료 신호. 요청 전체의 중지 신호와 구분
        cancel = Event()
        # 수신한 응답, 발생한 예외 또는 수신 완료 표시(None)를 전달할 큐
        events: Queue[dict[str, Any] | Exception | None] = Queue()

        def receive() -> None:
            """모델 응답과 수신 중 발생한 예외를 큐로 전달.

            별도 스레드에서 응답을 받아 요청 함수가 중지 신호를 확인할 수 있도록 분리.
            수신 종료 후 모델 잠금을 해제하고 None으로 수신 완료 전달.
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
                # 큐를 통해 수신 스레드의 예외를 요청 함수로 전달
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
                # 응답 대기 중 중지 신호가 설정됐을 수 있으므로 다시 확인
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
                # 답변 없이 사용량만 포함한 마지막 응답도 처리
                usage = event.get("usage") or {}
                input_tokens = usage.get("prompt_tokens", input_tokens)
                output_tokens = usage.get("completion_tokens", output_tokens)
                choices = event.get("choices") or []
                if choices:
                    piece = choices[0].get("delta", {}).get("content") or ""
                    if piece:
                        text.append(piece)
                        if on_token is not None:
                            # 한 번에 받은 문자열은 여러 토큰을 포함할 수 있음
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
        """작업별 응답 요청, 결과 파싱 및 최종 대화 저장.

        run_id: 실행 ID. prompt_idx: 작업 종류. chat_id: 채팅 ID.
        use_context=True인 경우 해당 실행, 채팅 및 작업에 저장된 이전 대화를 함께 사용.
        출력 형식 오류 시 같은 입력으로 최초 요청 포함 최대 3회 생성하고 요청마다 로그 함수 호출.
        성공 시 (파싱 결과, True, 총 입력 토큰 수, 총 출력 토큰 수, 종료 사유) 반환.
        3회 형식 오류 시 첫 값은 None, 성공 여부는 False, 종료 사유는 parse_failed.
        중지 시 받은 답변까지 반환. 출력 형식 오류 이외의 예외는 호출자에게 전달.
        """
        prompt_idx = PromptIdx(prompt_idx)
        # 미구현: 프롬프트 조회. 반환값은 (프롬프트 본문, 버전)
        # system_prompt, prompt_version = self.prompt_manager.get_prompt(prompt_idx)
        # 현재는 함수 인자로 본문과 버전 전달
        if system_prompt is None:
            raise NotImplementedError(
                "PromptManager is not connected; pass system_prompt"
            )
        with self._request_scope(run_id) as stop_signal:
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
            # 재생성에도 같은 입력 사용. 최종 응답까지 이전 대화 내용은 유지
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
                    # 재생성을 포함한 총 사용량. 사용량을 받지 못한 요청이 있으면 None
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
                        # 중지 시 지금까지 받은 답변 반환. 파싱·재생성·이전 대화 추가는 생략
                        return (
                            result[0] or last_output,
                            False,
                            input_tokens,
                            output_tokens,
                            "stopped" if stop_signal.is_set() else result[4],
                        )
                    # 파싱 중 발생한 출력 형식 오류만 처리. 래퍼의 예외는 호출자에게 전달
                    try:
                        parsed = parser(result[0], prompt_idx)
                    except OutputFormatError:
                        format_valid = False
                        last_output = result[0]
                        if stop_signal.is_set():
                            return (
                                result[0],
                                False,
                                input_tokens,
                                output_tokens,
                                "stopped",
                            )
                        continue
                    format_valid = True
                finally:
                    # 요청마다 새 입력, 모델이 생성한 답변 및 형식 검사 결과로 로그 함수 호출
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
                    # 입력과 형식 검사를 통과한 최종 답변만 이전 대화에 한 번 추가
                    manager.append(
                        context_id, f"\nUser: {input_}\nAssistant: {result[0]}\n"
                    )
                # 대화 압축 중 중지 신호를 받으면 요약 저장을 마친 뒤 요청 중지
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
        """같은 입력과 시스템 프롬프트로 답변을 한 번 더 생성.

        반환값은 manual_request와 같은 순서의 튜플.
        use_context=True인 경우 run_id의 context_id에 저장된 이전 대화를 입력 앞에 추가.
        이전 출력이나 수정 지시는 추가하지 않고 결과 파싱과 이전 대화 저장도 생략.
        로그에는 이번 입력과 새로 생성한 답변만 전달.
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
        with self._request_scope(run_id) as stop_signal:
            output: list[str] = []
            started = monotonic()
            try:
                return self._generate(
                    model_input,
                    system_prompt,
                    stop_signal=stop_signal,
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
        """요청 정보와 새 입력·답변의 로그 저장. 실제 저장은 미구현.

        저장 대상: 실행 ID, 작업 종류, 프롬프트 버전, 소요 시간, 형식 검사 결과, 입력 및 답변.
        ChatManager 구현 후 아래 주석을 해제해 요청 로그 저장.
        """
        # self.chat_manager.log_event(
        #     run_id, "llm_call", {
        #         "prompt_idx": prompt_idx, "prompt_version": prompt_version,
        #         "elapsed_ms": elapsed_ms, "format_valid": format_valid,
        #         "input": new_input, "output": output,
        #     }
        # )
        # format_valid: 형식 검사 결과. manual_request와 regen은 검사하지 않으므로 None
        # 저장 제외 항목: 이전 대화 전체, 시스템 프롬프트, 인증키

    def tokenize(self, text: str) -> list[int]:
        """응답 생성에 사용하는 모델로 문자열을 토큰 ID 목록으로 변환.

        ContextManager에서 반환된 목록의 길이로 저장된 대화의 토큰 수 계산.
        응답 수신 중이라면 모델 접근 잠금을 얻을 때까지 대기한 뒤 토큰화.
        """
        with self._busy:
            if self._closed:
                raise RuntimeError("Runner is closed")
            return self._llm.tokenize(text.encode("utf-8"), add_bos=False, special=True)

    def stop(self, run_id: str) -> None:
        """실행 ID가 일치하는 현재 요청에 중지 신호 전달 후 즉시 반환.

        다른 스레드에서 호출 가능. 생성 함수에서 신호 확인 후 받은 답변까지 반환.
        다른 실행 ID 또는 진행 중인 요청이 없는 경우 생략.
        대화 압축 중에는 압축 완료 후 중지 처리. 원격 서버의 생성 작업 취소는 별도 기능.
        """
        with self._state_lock:
            if self._active_run_id == run_id and self._stop_event is not None:
                self._stop_event.set()

    def close(self) -> None:
        """Runner 사용을 종료하고 진행 중인 수신이 끝나면 모델 자원 해제.

        중지 신호 전달 후 모델 접근 잠금을 얻을 때까지 대기.
        프로그램 종료 시 호출. 종료 후 같은 객체로 새 요청 불가.
        """
        with self._state_lock:
            self._closed = True
            if self._stop_event is not None:
                self._stop_event.set()
        # 진행 중인 생성·토큰화가 끝난 후 모델 메모리 해제
        # stop은 즉시 반환. close는 진행 중인 작업이 끝날 때까지 대기
        with self._busy:
            if self._llm is not None:
                self._llm.close()

    def __enter__(self) -> LocalLLMRunner:
        """with 문에서 사용할 현재 객체 반환. 모델 준비는 생성자에서 처리."""
        return self

    def __exit__(self, *_: object) -> None:
        """with 블록 종료 시 close 호출. 블록에서 발생한 예외는 호출자에게 전달."""
        self.close()

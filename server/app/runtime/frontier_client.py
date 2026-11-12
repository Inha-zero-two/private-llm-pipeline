"""설계 5.3: 프론티어 모델 호출, 컨텍스트 유지·초기화 판단 및 사용량·비용 계산."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel

from ..models.enums import PromptIdx
from ..models.llm_results import ContextMode, FrontierReply

if TYPE_CHECKING:
    from ..data.prompt_manager import PromptManager
    from ..privacy.pd_detector import PDItem
    from .local_llm_runner import LocalLLMRunner

# 학교 Bedrock 게이트웨이(OpenAI 호환) 기본 주소와 모델
DEFAULT_BASE_URL = "https://bedrock.nxtcloud.kr/v1"
DEFAULT_MODEL = "bedrock-gpt-6-luna"
# 모델별 100만 토큰당 (입력, 출력) 표준 단가(달러). 긴 문맥·캐시 단가는 미반영
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "bedrock-gpt-6-luna": (0.11, 0.55),
    "bedrock-haiku": (0.11, 0.55),
    "bedrock-gpt-6.1-sol": (2.20, 11.00),
}
# 최초 요청을 포함한 최대 전송 시도 횟수. 일시 오류(429·5xx·네트워크)만 재시도
MAX_ATTEMPTS = 3
# 재시도 전 대기 시간(초). 시도 순서대로 사용
RETRY_DELAYS = (1.0, 2.0)
# 추론 모델은 생각에도 토큰을 쓰므로 출력 상한을 넉넉하게 지정
MAX_OUTPUT_TOKENS = 4096
# 연결 확인 요청의 제한 시간(초)
CHECK_TIMEOUT = 5.0
# 다시 시도해도 같은 결과가 나오는 응답 코드의 설명
FATAL_STATUS = {
    400: "모델 이름이 틀렸거나 허용되지 않은 모델",
    401: "API 키가 틀렸거나 회수·차단된 키",
    403: "허용되지 않은 요청",
    422: "1인 한도 또는 교육 총 한도 소진",
}
# 환경변수 파일 기본 위치: server/.env
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class FrontierError(ConnectionError):
    """프론티어 연결·전송 실패. Main은 ConnectionError로 받아 offline으로 종료."""


class FrontierCancelledError(RuntimeError):
    """stop(run_id)으로 진행 중인 전송을 취소함. Main에서는 실행 취소로 처리."""


class FrontierQuery(BaseModel):
    """프론티어 질의 한 건의 기록. DB frontier_queries·query_items 저장 대상."""

    run_id: str
    round: int  # 이번 실행에서 몇 번째 질의인지(1부터)
    request: str  # 실제로 보낸 요청문
    answer: str | None  # 프론티어 응답. 실패·취소 시 None
    status: str  # ok · failed · cancelled
    context_mode: ContextMode
    input_tokens: int | None
    output_tokens: int | None
    cost: float
    elapsed_ms: int
    created_at: str
    # 전송 당시 개인정보 허용 상태: (pd_id, allowed, allowed_by)
    items: list[tuple[int | None, bool, str]]


def _read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE 형식의 환경변수 파일을 읽어 대문자 키로 반환. 파일이 없으면 빈 dict."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip().strip("\"'")
    return values


def _setting(name: str, env_file: dict[str, str], default: str | None) -> str | None:
    """환경변수 → .env 파일 → 기본값 순서로 설정값 조회."""
    return os.environ.get(name) or env_file.get(name) or default


class FrontierClient:
    def __init__(
        self,
        local_llm_runner: LocalLLMRunner | None = None,
        prompt_manager: PromptManager | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = MAX_OUTPUT_TOKENS,
        env_file: Path | None = ENV_FILE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """프론티어 접속 설정과 실행별 컨텍스트 저장 공간 초기화.

        local_llm_runner: 컨텍스트 유지 판단(DECIDE_CONTEXT)에 쓸 로컬 모델.
        prompt_manager: FRONTIER 시스템 프롬프트를 조회할 관리자.
        api_key·base_url·model을 생략하면 FRONTIER_API_KEY·FRONTIER_BASE_URL·
        FRONTIER_MODEL 환경변수, 없으면 server/.env 파일 값 사용.
        transport: 테스트에서 가짜 응답을 넣을 httpx 전송 계층.
        생성자에서는 네트워크 요청을 하지 않음. 연결 확인은 check_connection()으로.
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        file_values = _read_env_file(env_file) if env_file is not None else {}
        self._api_key = api_key or _setting("FRONTIER_API_KEY", file_values, None)
        self._base_url = (
            base_url or _setting("FRONTIER_BASE_URL", file_values, DEFAULT_BASE_URL)
        ).rstrip("/")
        self.model = model or _setting("FRONTIER_MODEL", file_values, DEFAULT_MODEL)
        if self.model not in MODEL_PRICES:
            raise ValueError(f"단가가 등록되지 않은 모델: {self.model}")
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._transport = transport
        self.local_llm_runner = local_llm_runner
        self.prompt_manager = prompt_manager
        # 설계서 변수: 프론티어·컨텍스트 판단에 쓰는 시스템 프롬프트 번호
        self.frontier_sys_prom_idx = PromptIdx.FRONTIER
        self.context_recognize_sys_prom_idx = PromptIdx.DECIDE_CONTEXT
        # 가장 최근 질의 기록
        self.last_request: FrontierQuery | None = None
        self._lock = Lock()  # 실행별 상태 변경용 잠금
        # 실행 ID별 프론티어 대화 기록(user·assistant 메시지). 실행 사이 공유하지 않음
        self._contexts: dict[str, list[dict[str, str]]] = {}
        self._rounds: dict[str, int] = {}  # 실행 ID별 질의 횟수
        self._stop_events: dict[str, Event] = {}  # 진행 중인 전송의 중지 신호
        self._clients: dict[str, httpx.Client] = {}  # 진행 중인 전송의 HTTP 연결

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def check_connection(self) -> bool:
        """API 키가 있고 게이트웨이 모델 목록을 받을 수 있으면 True.

        비용이 들지 않는 GET /models로 확인. 실패는 예외 없이 False 반환.
        """
        if not self._api_key:
            return False
        try:
            with httpx.Client(
                timeout=CHECK_TIMEOUT, transport=self._transport
            ) as client:
                response = client.get(
                    f"{self._base_url}/models", headers=self._headers()
                )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def decide_context(self, run_id: str, request_text: str) -> ContextMode:
        """이번 요청에 이전 프론티어 대화를 이어 붙일지(keep) 새로 시작할지(reset) 판단.

        이전 대화가 없으면 모델 호출 없이 reset. 로컬 모델이 없거나 판단에 실패하면
        개인정보·비용 측면에서 안전한 reset 반환.
        """
        with self._lock:
            history = list(self._contexts.get(run_id, []))
        if not history or self.local_llm_runner is None:
            return "reset"
        previous = "\n".join(f"{m['role']}: {m['content']}" for m in history[-2:])
        input_ = f"Previous exchange:\n{previous}\n\nNew request:\n{request_text}"
        try:
            parsed, success, *_ = self.local_llm_runner.request(
                run_id, input_, self.context_recognize_sys_prom_idx, None
            )
        except Exception:
            return "reset"
        return "keep" if success and parsed == "keep" else "reset"

    def send(
        self,
        run_id: str,
        request_text: str,
        pd_table: list[PDItem] | None = None,
    ) -> FrontierReply:
        """누출 검사를 통과한 요청문을 프론티어로 보내고 답변·사용량·비용 반환.

        pd_table: 전송 당시 개인정보 허용 상태를 기록할 표.
        일시 오류(429·5xx·네트워크·타임아웃)는 최대 3회 시도.
        키·모델·한도 오류와 재시도 소진은 FrontierError(ConnectionError).
        stop(run_id) 호출 시 즉시 FrontierCancelledError.
        """
        if not self._api_key:
            raise FrontierError("FRONTIER_API_KEY가 설정되지 않음")
        context_mode = self.decide_context(run_id, request_text)
        stop_event = Event()
        with self._lock:
            if run_id in self._stop_events:
                raise RuntimeError("같은 실행의 전송이 이미 진행 중")
            self._stop_events[run_id] = stop_event
            if context_mode == "reset":
                self._contexts[run_id] = []
            history = list(self._contexts.get(run_id, []))
            self._rounds[run_id] = self._rounds.get(run_id, 0) + 1
            round_ = self._rounds[run_id]
        messages = [{"role": "system", "content": self._system_prompt()}]
        messages += history
        messages.append({"role": "user", "content": request_text})
        items = [(i.pd_id, i.allowed, i.allowed_by) for i in pd_table or []]
        started = monotonic()
        status, answer, usage = "failed", None, None
        try:
            answer, usage = self._post_with_retry(run_id, messages, stop_event)
            status = "ok"
        except FrontierCancelledError:
            status = "cancelled"
            raise
        finally:
            with self._lock:
                self._stop_events.pop(run_id, None)
            input_tokens = usage.get("prompt_tokens") if usage else None
            output_tokens = usage.get("completion_tokens") if usage else None
            cost = self._cost(input_tokens, output_tokens)
            elapsed_ms = int((monotonic() - started) * 1000)
            self._log_query(
                FrontierQuery(
                    run_id=run_id,
                    round=round_,
                    request=request_text,
                    answer=answer,
                    status=status,
                    context_mode=context_mode,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost=cost,
                    elapsed_ms=elapsed_ms,
                    created_at=datetime.now(UTC).isoformat(),
                    items=items,
                )
            )
        assert answer is not None
        with self._lock:
            # 성공한 질의만 다음 판단에 쓸 대화로 저장
            self._contexts.setdefault(run_id, []).extend(
                [
                    {"role": "user", "content": request_text},
                    {"role": "assistant", "content": answer},
                ]
            )
        return FrontierReply(
            answer=answer,
            cost=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            elapsed_ms=elapsed_ms,
            context_mode=context_mode,
            model=self.model,
        )

    def _system_prompt(self) -> str:
        """FRONTIER 시스템 프롬프트. PromptManager가 없으면 빈 문자열."""
        if self.prompt_manager is None:
            return ""
        return self.prompt_manager.get_prompt(self.frontier_sys_prom_idx)

    def _post_with_retry(
        self, run_id: str, messages: list[dict[str, str]], stop_event: Event
    ) -> tuple[str, dict[str, Any]]:
        """일시 오류만 재시도하며 전송. (답변, usage) 반환."""
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                # 대기 중에도 중지 신호를 받으면 바로 취소
                if stop_event.wait(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]):
                    raise FrontierCancelledError(run_id)
            try:
                response = self._post(run_id, messages, stop_event)
            except (httpx.TransportError, TimeoutError) as exc:
                last_error = exc
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = FrontierError(f"HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                reason = FATAL_STATUS.get(response.status_code, "요청 실패")
                raise FrontierError(
                    f"HTTP {response.status_code}: {reason} ({response.text[:200]})"
                )
            try:
                data = response.json()
                answer = data["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise FrontierError(f"응답 형식 오류: {exc}") from exc
            if not answer.strip():
                # 추론에 출력 토큰을 다 쓰면 빈 답변이 옴. 같은 입력으로 다시 시도
                last_error = FrontierError("빈 답변")
                continue
            return answer, data.get("usage") or {}
        raise FrontierError(f"전송 {MAX_ATTEMPTS}회 실패: {last_error}")

    def _post(
        self, run_id: str, messages: list[dict[str, str]], stop_event: Event
    ) -> httpx.Response:
        """별도 스레드에서 POST /chat/completions 요청. 중지 신호를 받으면 연결을 닫음.

        Main은 send()를 스레드에서 부르므로 이 함수가 중지 신호를 직접 확인해야
        stop(run_id) 즉시 반환 가능.
        """
        client = httpx.Client(timeout=self._timeout, transport=self._transport)
        with self._lock:
            self._clients[run_id] = client
        results: Queue[httpx.Response | BaseException] = Queue()

        def worker() -> None:
            try:
                results.put(
                    client.post(
                        f"{self._base_url}/chat/completions",
                        headers=self._headers(),
                        json={
                            "model": self.model,
                            "messages": messages,
                            "max_tokens": self._max_tokens,
                        },
                    )
                )
            except BaseException as exc:  # 스레드 예외를 호출 스레드로 전달
                results.put(exc)

        Thread(target=worker, daemon=True).start()
        try:
            while True:
                if stop_event.is_set():
                    raise FrontierCancelledError(run_id)
                try:
                    result = results.get(timeout=0.05)
                except Empty:
                    continue
                if isinstance(result, BaseException):
                    if stop_event.is_set():
                        raise FrontierCancelledError(run_id)
                    raise result
                return result
        finally:
            with self._lock:
                self._clients.pop(run_id, None)
            client.close()

    def _cost(self, input_tokens: int | None, output_tokens: int | None) -> float:
        """사용량과 모델 단가로 비용(달러) 계산. 사용량을 모르면 0."""
        input_price, output_price = MODEL_PRICES[self.model]
        return (
            (input_tokens or 0) * input_price + (output_tokens or 0) * output_price
        ) / 1_000_000

    def stop(self, run_id: str) -> None:
        """실행 ID의 진행 중인 전송을 취소하고 즉시 반환. 다른 스레드에서 호출 가능.

        진행 중인 HTTP 연결을 닫아 요청을 끊음. 이미 서버에 도착한 요청의 비용은
        게이트웨이에서 차감될 수 있음.
        """
        with self._lock:
            event = self._stop_events.get(run_id)
            client = self._clients.get(run_id)
        if event is not None:
            event.set()
        if client is not None:
            client.close()

    def clear(self, run_id: str) -> None:
        """실행이 끝나면 그 실행의 프론티어 대화와 질의 횟수 삭제."""
        with self._lock:
            self._contexts.pop(run_id, None)
            self._rounds.pop(run_id, None)

    def _log_query(self, query: FrontierQuery) -> None:
        """질의 기록 저장. DB 저장은 ChatManager 구현 후 연결.

        저장 대상: frontier_queries 한 건과 전송 당시 허용 상태(query_items).
        저장 제외: API 키, 시스템 프롬프트.
        """
        self.last_request = query
        # self.chat_manager.save_frontier_query(query)

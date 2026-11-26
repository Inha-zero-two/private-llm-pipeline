"""설계 5.3: 프론티어 모델 호출, 컨텍스트 유지·초기화 판단 및 사용량·비용 계산."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from ..config import ENV_FILE, read_env_file, setting
from ..models.enums import PromptIdx
from ..models.errors import (
    ErrorCode,
    ErrorInfo,
    ErrorStage,
    ServiceError,
    describe_error,
)
from ..models.llm_results import ContextMode, FrontierReply

if TYPE_CHECKING:
    from ..data.prompt_manager import PromptManager
    from ..privacy.pd_detector import PDItem

# OpenAI Responses API. 기존 FRONTIER_* 설정 이름과 httpx 전송 계층을 유지한다.
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-6-luna"
# 2026-11-26 기준 Standard·짧은 문맥의 100만 토큰당 (입력, 출력) 달러 단가.
# gpt-6-luna의 캐시·긴 문맥 요율과 웹 검색 비용은 _cost()에서 반영한다.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-luna": (0.10, 0.50),
}
WEB_SEARCH_PRICE = 0.01  # 검색 1건당 달러. 검색 콘텐츠 토큰은 usage 입력에 포함.
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


class FrontierError(ServiceError):
    """프론티어 연결·전송 실패. Main은 ConnectionError로 받아 offline으로 종료."""

    def __init__(
        self, detail: str, code: ErrorCode = ErrorCode.FRONTIER_UNAVAILABLE
    ) -> None:
        super().__init__(detail, code, ErrorStage.FRONTIER)


class FrontierCancelledError(RuntimeError):
    """stop(run_id)으로 진행 중인 전송을 취소함. Main에서는 실행 취소로 처리."""


class FrontierQuery(BaseModel):
    """프론티어 질의 한 건의 기록. DB frontier_queries·query_items 저장 대상."""

    run_id: int
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


class _Usage(BaseModel):
    """재시도별 요율을 적용한 뒤 합산하는 내부 사용량."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float = 0.0


class FrontierClient:
    def __init__(
        self,
        prompt_manager: PromptManager | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        max_tokens: int = MAX_OUTPUT_TOKENS,
        web_search: bool | None = None,
        env_file: Path | None = ENV_FILE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """프론티어 접속 설정과 실행별 컨텍스트 저장 공간 초기화.

        prompt_manager: FRONTIER 시스템 프롬프트를 조회할 관리자.
        api_key·base_url·model을 생략하면 FRONTIER_API_KEY·FRONTIER_BASE_URL·
        FRONTIER_MODEL 환경변수, 없으면 server/.env 파일 값 사용.
        transport: 테스트에서 가짜 응답을 넣을 httpx 전송 계층.
        생성자에서는 네트워크 요청을 하지 않음. 연결 확인은 check_connection()으로.
        """
        if timeout <= 0:
            raise FrontierError(
                "timeout must be positive", ErrorCode.CONFIGURATION_ERROR
            )
        file_values = read_env_file(env_file) if env_file is not None else {}
        self._api_key = api_key or setting("FRONTIER_API_KEY", file_values, None)
        self._base_url = (
            base_url or setting("FRONTIER_BASE_URL", file_values, DEFAULT_BASE_URL)
        ).rstrip("/")
        self.model = model or setting("FRONTIER_MODEL", file_values, DEFAULT_MODEL)
        if self.model not in MODEL_PRICES:
            raise FrontierError(
                f"단가가 등록되지 않은 모델: {self.model}",
                ErrorCode.CONFIGURATION_ERROR,
            )
        search_setting = setting("FRONTIER_WEB_SEARCH", file_values, "true")
        if web_search is None and search_setting.lower() not in {"true", "false"}:
            raise FrontierError(
                "FRONTIER_WEB_SEARCH는 true 또는 false여야 함",
                ErrorCode.CONFIGURATION_ERROR,
            )
        self._web_search = (
            search_setting.lower() == "true" if web_search is None else web_search
        )
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._transport = transport
        self.prompt_manager = prompt_manager
        # 설계서 변수: 프론티어 시스템 프롬프트 번호
        self.frontier_sys_prom_idx = PromptIdx.FRONTIER
        # 가장 최근 질의 기록
        self.last_request: FrontierQuery | None = None
        self.connection_failure: ErrorInfo | None = None
        self._lock = Lock()  # 실행별 상태 변경용 잠금
        # 실행별 성공한 요청·응답 이력. Responses 입력 항목을 백엔드가 직접 관리한다.
        self._contexts: dict[int, list[dict[str, Any]]] = {}
        # 실행별 대화가 다룬 need. 기존 keep/reset 판단을 그대로 유지한다.
        self._context_needs: dict[int, frozenset[str]] = {}
        self._rounds: dict[int, int] = {}  # 실행 ID별 질의 횟수
        self._stop_events: dict[int, Event] = {}  # 진행 중인 전송의 중지 신호
        # stop()이 불린 실행 ID. 중지 뒤에 시작된 send()도 막으려고 clear()까지 유지
        self._stopped: set[int] = set()
        self._clients: dict[int, httpx.Client] = {}  # 진행 중인 전송의 HTTP 연결

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def check_connection(self) -> bool:
        """API 키가 있고 OpenAI 모델 목록을 받을 수 있으면 True.

        비용이 들지 않는 GET /models로 확인. 실패는 예외 없이 False 반환.
        """
        if not self._api_key:
            self.connection_failure = describe_error(
                FrontierError("API key missing", ErrorCode.CONFIGURATION_ERROR)
            )
            return False
        try:
            with httpx.Client(
                timeout=CHECK_TIMEOUT, transport=self._transport
            ) as client:
                response = client.get(
                    f"{self._base_url}/models", headers=self._headers()
                )
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            # InvalidURL은 HTTPError 하위가 아님. 주소가 잘못돼도 offline으로 처리
            code = (
                ErrorCode.FRONTIER_TIMEOUT
                if isinstance(error, httpx.TimeoutException)
                else ErrorCode.CONFIGURATION_ERROR
                if isinstance(error, httpx.InvalidURL)
                else ErrorCode.FRONTIER_UNAVAILABLE
            )
            self.connection_failure = describe_error(FrontierError(str(error), code))
            return False
        self.connection_failure = (
            None
            if response.status_code == 200
            else describe_error(
                FrontierError(
                    f"HTTP {response.status_code}", self._status_code(response)
                )
            )
        )
        return response.status_code == 200

    @staticmethod
    def _status_code(response: httpx.Response) -> ErrorCode:
        try:
            body = response.json()
            detail = body.get("error", {}) if isinstance(body, dict) else {}
            provider_code = detail.get("code") if isinstance(detail, dict) else None
        except ValueError:
            provider_code = None
        if isinstance(provider_code, str) and provider_code in {
            "insufficient_quota",
            "quota_exceeded",
            "budget_exceeded",
            "credit_balance_exhausted",
            "organization_usage_limit_exceeded",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
        }:
            return ErrorCode.FRONTIER_QUOTA_EXCEEDED
        if response.status_code in {401, 403}:
            return ErrorCode.FRONTIER_AUTH_ERROR
        if response.status_code == 429:
            return ErrorCode.FRONTIER_RATE_LIMITED
        if response.status_code >= 500:
            return ErrorCode.FRONTIER_UNAVAILABLE
        return ErrorCode.FRONTIER_REQUEST_REJECTED

    def decide_context(self, run_id: int, need: list[str] | None) -> ContextMode:
        """이번 요청에 이전 프론티어 대화를 이어 붙일지(keep) 새로 시작할지(reset) 판단.

        직전 회차와 need가 완전히 같을 때만 keep. 같은 정보를 다시 묻는 경우라 앞 답을
        알아야 더 구체적으로 물을 수 있음. need가 다르거나 일부만 겹치면 reset.
        앞서 받은 답은 메모에 있어 요청문에 반영되므로, 끊어도 잃는 정보가 적고 비용이 줆.
        설계서는 로컬 모델 판단(DECIDE_CONTEXT)이지만 지연을 줄이려고 need 비교로 대체.
        need 순서는 보지 않음. 이전 대화나 need가 없으면 reset.
        """
        if not need:
            return "reset"
        with self._lock:
            has_history = bool(self._contexts.get(run_id))
            previous = self._context_needs.get(run_id)
        return "keep" if has_history and previous == frozenset(need) else "reset"

    def send(
        self,
        run_id: int,
        request_text: str,
        pd_table: list[PDItem] | None = None,
        need: list[str] | None = None,
    ) -> FrontierReply:
        """누출 검사를 통과한 요청문을 프론티어로 보내고 답변·사용량·비용 반환.

        pd_table: 전송 당시 개인정보 허용 상태를 기록할 표.
        need: 이번 회차에 묻는 정보(Plan.need). 직전 회차와 같을 때만 이전 대화를 붙임.
        일시 오류(429·5xx·네트워크·타임아웃)는 최대 3회 시도.
        키·모델·한도 오류와 재시도 소진은 FrontierError(ConnectionError).
        stop(run_id) 호출 시 즉시 FrontierCancelledError.
        """
        if not self._api_key:
            raise FrontierError(
                "FRONTIER_API_KEY가 설정되지 않음", ErrorCode.CONFIGURATION_ERROR
            )
        # 중지 신호는 전송 준비보다 먼저 등록해야 그동안의 stop()도 받음
        stop_event = Event()
        with self._lock:
            if run_id in self._stop_events:
                raise RuntimeError("같은 실행의 전송이 이미 진행 중")
            if run_id in self._stopped:
                raise FrontierCancelledError(run_id)
            self._stop_events[run_id] = stop_event
        try:
            return self._send(run_id, request_text, pd_table, need, stop_event)
        finally:
            with self._lock:
                self._stop_events.pop(run_id, None)

    def _send(
        self,
        run_id: int,
        request_text: str,
        pd_table: list[PDItem] | None,
        need: list[str] | None,
        stop_event: Event,
    ) -> FrontierReply:
        """send()의 본문. 중지 신호 등록·해제는 send()가 맡음."""
        context_mode = self.decide_context(run_id, need)
        with self._lock:
            if context_mode == "reset":
                self._contexts.pop(run_id, None)
                self._context_needs.pop(run_id, None)
            history = deepcopy(self._contexts.get(run_id, []))
            self._rounds[run_id] = self._rounds.get(run_id, 0) + 1
            round_ = self._rounds[run_id]
        user_input = {"role": "user", "content": request_text}
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": self._system_prompt(),
            "input": [*history, user_input],
            "max_output_tokens": self._max_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if self._web_search:
            payload.update(tools=[{"type": "web_search"}], tool_choice="auto")
        items = [(i.pd_id, i.allowed, i.allowed_by) for i in pd_table or []]
        started = monotonic()
        status, answer = "failed", None
        # 모든 시도(빈 답변 포함)의 사용량 합계. 실패·취소여도 쓴 만큼 기록
        usage = _Usage()
        try:
            answer, output = self._post_with_retry(run_id, payload, stop_event, usage)
            status = "ok"
        except FrontierCancelledError:
            status = "cancelled"
            raise
        finally:
            input_tokens = usage.input_tokens
            output_tokens = usage.output_tokens
            cost = usage.cost
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
            # 성공한 질의만 다음 판단에 쓸 대화로 저장. 중지된 실행은 clear()가 먼저
            # 지웠을 수 있으므로 다시 만들지 않음
            if not stop_event.is_set():
                # 검색·추론 항목과 원본 출처를 포함한 output을 그대로 이어 보낸다.
                # 화면용 Markdown 답변으로 대체하면 도구·추론 맥락을 잃는다.
                self._contexts[run_id] = [*history, user_input, *deepcopy(output)]
                if need:
                    self._context_needs[run_id] = frozenset(need)
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
        self,
        run_id: int,
        payload: dict[str, Any],
        stop_event: Event,
        usage: _Usage,
    ) -> tuple[str, list[dict[str, Any]]]:
        """일시 오류만 재시도하며 전송하고 답변 반환.

        usage: 응답마다 토큰 수·비용을 더해 넣을 객체. 빈 답변도 과금될 수 있으므로
        성공·실패와 상관없이 호출한 쪽이 이 값으로 비용을 계산.
        """
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                # 대기 중에도 중지 신호를 받으면 바로 취소
                if stop_event.wait(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS)) - 1]):
                    raise FrontierCancelledError(run_id)
            try:
                response = self._post(run_id, payload, stop_event)
            except (httpx.TransportError, TimeoutError) as exc:
                code = (
                    ErrorCode.FRONTIER_TIMEOUT
                    if isinstance(exc, (httpx.TimeoutException, TimeoutError))
                    else ErrorCode.FRONTIER_UNAVAILABLE
                )
                last_error = FrontierError(str(exc), code)
                continue
            except httpx.InvalidURL as exc:
                # 주소 설정 오류는 다시 시도해도 같음
                raise FrontierError(
                    f"잘못된 FRONTIER_BASE_URL: {exc}", ErrorCode.CONFIGURATION_ERROR
                ) from exc
            if response.status_code == 429 or response.status_code >= 500:
                last_error = FrontierError(
                    f"HTTP {response.status_code}", self._status_code(response)
                )
                if last_error.code == ErrorCode.FRONTIER_QUOTA_EXCEEDED:
                    raise last_error
                continue
            if response.status_code != 200:
                reason = FATAL_STATUS.get(response.status_code, "요청 실패")
                raise FrontierError(
                    f"HTTP {response.status_code}: {reason}",
                    self._status_code(response),
                )
            try:
                data = response.json()
                self._add_usage(usage, data)
                answer = self._answer(data)
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                raise FrontierError(
                    f"응답 형식 오류: {exc}", ErrorCode.FRONTIER_RESPONSE_INVALID
                ) from exc
            if data.get("status") != "completed" or not answer.strip():
                # 추론에 출력 토큰을 다 쓰면 빈 답변이 옴. 같은 입력으로 다시 시도
                last_error = FrontierError(
                    "완료되지 않았거나 빈 답변", ErrorCode.FRONTIER_RESPONSE_INVALID
                )
                continue
            return answer, data["output"]
        raise FrontierError(
            f"전송 {MAX_ATTEMPTS}회 실패: {last_error}",
            last_error.code
            if isinstance(last_error, FrontierError)
            else ErrorCode.FRONTIER_UNAVAILABLE,
        ) from last_error

    def _post(
        self, run_id: int, payload: dict[str, Any], stop_event: Event
    ) -> httpx.Response:
        """별도 스레드에서 POST /responses 요청. 중지 신호를 받으면 연결을 닫음.

        Main은 send()를 스레드에서 부르므로 이 함수가 중지 신호를 직접 확인해야
        stop(run_id) 즉시 반환 가능.
        """
        client = httpx.Client(timeout=self._timeout, transport=self._transport)
        # 중지 확인과 연결 등록을 stop()과 같은 잠금 안에서 함. stop()이 먼저면 여기서
        # 중지를 보고, 등록이 먼저면 stop()이 이 연결을 찾아 닫음. 스레드를 띄우면
        # 요청이 바로 나가므로 그 전에 확인
        with self._lock:
            stopped = stop_event.is_set()
            if not stopped:
                self._clients[run_id] = client
        if stopped:
            client.close()
            raise FrontierCancelledError(run_id)
        results: Queue[httpx.Response | BaseException] = Queue()

        def worker() -> None:
            try:
                results.put(
                    client.post(
                        f"{self._base_url}/responses",
                        headers=self._headers(),
                        json=payload,
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

    @staticmethod
    def _answer(data: dict[str, Any]) -> str:
        """도구·추론 출력을 건너뛰고 답변 텍스트와 클릭 가능한 검색 출처를 반환."""
        parts: list[str] = []
        for item in data["output"]:
            if item.get("type") != "message":
                continue
            for content in item["content"]:
                if content.get("type") != "output_text":
                    continue
                text = content["text"]
                if not isinstance(text, str):
                    raise TypeError("External answer must be text")
                citations = [
                    a
                    for a in content.get("annotations", [])
                    if a.get("type") == "url_citation"
                ]
                for citation in sorted(
                    citations, key=lambda a: a["start_index"], reverse=True
                ):
                    start, end, url = (
                        citation["start_index"],
                        citation["end_index"],
                        citation["url"],
                    )
                    if 0 <= start <= end <= len(text) and url.startswith(
                        ("https://", "http://")
                    ):
                        link = f"[출처]({quote(url, safe=':/?&=#%')})"
                        text = text[:start] + link + text[end:]
                parts.append(text)
        return "\n".join(parts)

    def _add_usage(self, total: _Usage, data: dict[str, Any]) -> None:
        """실패·빈 답변도 포함해 응답별 토큰·캐시·검색 요율을 적용해 합산."""
        usage = data.get("usage") or {}
        for name in ("input_tokens", "output_tokens"):
            value = usage.get(name)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise TypeError("Token usage must be a non-negative integer")
                setattr(total, name, (getattr(total, name) or 0) + value)
        details = usage.get("input_tokens_details") or {}
        writes = details.get("cache_write_tokens_details") or {}
        search = (data.get("tool_usage") or {}).get("web_search")
        search_calls = (
            search["num_requests"]
            if search is not None
            else sum(
                1
                for item in data.get("output", [])
                if item.get("type") == "web_search_call"
                and (item.get("action") or {}).get("type") == "search"
            )
        )
        total.cost += self._cost(
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            cached_tokens=details.get("cached_tokens", 0),
            cache_write_tokens=details.get("cache_write_tokens", 0),
            cache_write_12h_tokens=writes.get("ttl_12h_tokens", 0),
            search_calls=search_calls,
        )

    def _cost(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        *,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_write_12h_tokens: int = 0,
        search_calls: int = 0,
    ) -> float:
        """gpt-6-luna Standard 요금 추정. 추론 토큰은 출력에 포함되어 중복 합산 안 함."""
        input_price, output_price = MODEL_PRICES[self.model]
        if (input_tokens or 0) > 272_000:
            input_price *= 2
            output_price *= 1.5
        uncached = max(0, (input_tokens or 0) - cached_tokens - cache_write_tokens)
        return (
            uncached * input_price
            + cached_tokens * input_price * 0.1
            + (cache_write_tokens - cache_write_12h_tokens) * input_price * 1.25
            + cache_write_12h_tokens * input_price * 2.5
            + (output_tokens or 0) * output_price
        ) / 1_000_000 + search_calls * WEB_SEARCH_PRICE

    def stop(self, run_id: int) -> None:
        """실행 ID의 진행 중인 전송을 취소하고 즉시 반환. 다른 스레드에서 호출 가능.

        진행 중인 HTTP 연결을 닫아 요청을 끊음. 이미 서버에 도착한 요청의 비용은
        OpenAI에서 차감될 수 있음. 진행 중인 전송이 없어도 기록해 두어, 이후
        clear(run_id) 전까지 같은 실행의 send()는 보내지 않고 FrontierCancelledError.
        """
        with self._lock:
            self._stopped.add(run_id)
            event = self._stop_events.get(run_id)
            # 신호 설정과 연결 조회를 한 잠금 안에서 해야 _post()의 등록과 엇갈리지 않음
            if event is not None:
                event.set()
            client = self._clients.get(run_id)
        if client is not None:
            client.close()

    def clear(self, run_id: int) -> None:
        """실행이 끝나면 그 실행의 프론티어 대화와 질의 횟수 삭제."""
        with self._lock:
            self._contexts.pop(run_id, None)
            self._context_needs.pop(run_id, None)
            self._rounds.pop(run_id, None)
            self._stopped.discard(run_id)

    def _log_query(self, query: FrontierQuery) -> None:
        """최근 질의 진단용 기록. DB 저장은 Main._send_frontier가 담당한다."""
        self.last_request = query

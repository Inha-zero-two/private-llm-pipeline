# LocalLLMRunner와 ContextManager

상세 설계서 5.1과 5.4의 로컬 개발 구현이다. 실제 모델 호출은 기존
`src/localLLM_API_proxy/llama_wrapper.py`의 원격 프록시를 사용한다.
각 모듈은 `app/runtime/local_llm_runner.py`의 `LocalLLMRunner` 클래스와
`app/runtime/context_manager.py`의 `ContextManager` 클래스로 분리했다.

## 실행 환경

Python 3.12와 uv를 사용한다. 이 폴더에서 실행하며, 기존 프록시를 import할 수
있도록 저장소 루트를 `PYTHONPATH`에 둔다. 새 FastAPI 서버는 필요하지 않다.

```powershell
# server/ 폴더에서 실행
uv sync --locked
$env:PYTHONPATH = (Resolve-Path '..').Path
uv run python
```

`pyproject.toml`, `.python-version`, `uv.lock`이 실행 환경을 고정한다.
개발 검사도 이 폴더에서 실행한다.

```powershell
uv run ruff check app tests
uv run ruff format --check app tests
uv run pytest -q
```

## 인증키

기존 `llama_api_key_01.key`처럼 번호만 다른 키를 저장소의 `keys/` 폴더에 둔다.
기본 탐색은 현재 작업 폴더가 아니라 저장소 루트 아래에서만 수행한다.
번호가 작은 파일부터 선택하며 특정 키는 `server_token_file`로 지정할 수 있다.
키 내용은 출력하거나 로그에 남기지 않는다. `keys/`와 `*.key`는 Git 제외 대상이다.
실제 키와 가상환경을 커밋하지 않는다.

생성자는 짧은 실제 생성 요청으로 연결·인증·응답을 검사한다. 키가 없거나
프록시가 실패하면 예외가 발생한다. `model_load()`는 원격 연결 초기화이며,
원격 GPU 가중치 적재는 서버가 담당한다.

## 기본 호출

```python
from app.runtime.local_llm_runner import LocalLLMRunner

with LocalLLMRunner(context_tasks=frozenset({"ANSWER"})) as runner:
    result = runner.manual_request("안녕하세요.", "한국어로 짧게 답하세요.")
    text, success, input_tokens, output_tokens, finish_reason = result

    # PromptManager가 아직 없으므로 system_prompt를 명시한다.
    runner.request(
        "run-1", "제 이름은 민수입니다.", "ANSWER", "chat-1",
        system_prompt="최근 대화를 참고해 짧게 답하세요.",
    )
    answer = runner.request(
        "run-2", "제 이름이 무엇인가요?", "ANSWER", "chat-1",
        system_prompt="최근 대화를 참고해 짧게 답하세요.",
    )
```

반환은 이전에 합의한 `(본문, 성공, 입력 토큰, 출력 토큰, 종료 사유)` 튜플이다.
사용량은 `include_usage=True`의 마지막 청크에서 읽고, 미제공 시 `None`을 유지한다.
`length`는 답변 상한에 도달했다는 서버 종료 사유다. 프록시 오류, 무응답,
불완전한 스트림, 전체 대기시간 초과는 예외로 전달한다. 오류를 빈 답변이나
임의 응답으로 대체하지 않고 자동 재시도도 하지 않는다.

`request()`는 지정한 `context_tasks`의 작업에만 컨텍스트를 사용한다. 기본값은
빈 집합이며 작업 정책을 임의로 정하지 않는다. `manual_request()`는 컨텍스트를
자동으로 읽거나 추가하지 않는다. `regen()`은 원래 입력을 유지하고 직전 출력과
형식 오류를 덧붙여 한 번 생성한다. 파싱 실패 재시도 상수 `MAX_REGEN=3`은
파서 연결 전 설정값이며 요청문 누출에 의한 재작성 상한과 별개다.

## 중지와 스레드

네트워크 수신은 별도 스레드에서 동작한다. 다른 스레드에서 `runner.stop()`을
호출하면 `Event` 신호로 대기 중인 호출을 끊고 지금까지 받은 부분 본문과
`success=False`, `finish_reason="stopped"`를 반환한다. `stop()` 자체는 응답을
기다리지 않는다. 수신 스레드가 다음 청크 또는 읽기 timeout으로 정리되기 전에는
다음 생성을 거부하여 동시 생성 1개를 유지한다. 서버 추론 자체의 즉시 취소는
보장하지 않는다. 사용 후 `close()` 또는 `with`로 HTTP 연결을 정리한다.

## 컨텍스트 압축

`get_id(chat_id, prompt_idx)`는 채팅·작업 조합별 ID를 생성/조회하고 `new_id()`는
독립된 빈 저장소를 만든다. `get_context`, `append`, `get_context_length`,
`compact_context`로 내용을 관리한다. 모든 길이는 같은 프록시 모델의
`tokenize`로 계산하며 문자 수를 토큰 수로 간주하지 않는다.

기본 압축 임계값은 28,000토큰, 요약 목표는 5,000토큰이다. 설계의 임계값 검사에
더해 새 내용을 붙이면 한도를 넘는 경우도 미리 검사한다. 기존 내용을
`manual_request()`로 요약한 뒤 새 내용을 추가한다. 새 내용만으로 한도를 넘거나,
요약 실패·중지·토큰 목표 초과가 발생하면 예외를 내고 기존 컨텍스트를 보존한다.
임의로 원문을 잘라 버리는 fallback은 없다. 현재 저장은 메모리이며 재시작 후
DB 복원은 아직 연결하지 않았다.

## 미구현 모듈과 로그 연결

PromptManager, LocalResParser, ChatManager의 예상 호출은 해당 위치에 주석으로
남겼다. 따라서 `request()`는 아직 스키마를 검증하지 않은 원시 본문을 반환한다.
성공 여부는 모델 생성 완료이며 형식 검증 통과를 의미하지 않는다.

로그 저장도 아직 미구현이다. `_log_call()`과 `_log_compaction()`은 연결용
스켈레톤이며 실제 `chat_manager.log_event()` 호출은 주석 처리했다.

- 일반 모델 호출은 새 입력과 새 출력만 전달한다. `request()`가 모델에 보낸
  이전 컨텍스트 전체를 매 로그마다 다시 저장하지 않는다. 오류 시에도 이미
  받은 부분 출력으로 같은 로그 연결 지점을 거친 뒤 원래 예외를 전파한다.
- 압축 호출은 `manual_request(..., log_call=False)`로 일반 호출 로그를 생략한다.
  압축이 실제 메모리에 반영된 후 별도 `context_compacted` 이벤트로 압축 시점
  UTC와 결과 요약만 전달한다. 압축 대상 원문은 이 로그에 포함하지 않는다.

## 확인 결과

2026년 11월 1일 실제 서버에서 저장소 내부 키 자동 탐색, 응답과 토큰 사용량,
두 번의 `request()`를 통한 코드 기억, 실제 모델 토크나이저와 요약 호출을 확인했다.
압축 경로 검증에는 작은 시험 임계값을 사용했고 원문 211토큰이 새 입력 포함
64토큰으로 줄어들면서 지정한 코드가 보존되었다. 기본 28K/5K 경계와 실패·중지·로그
분기 등은 모델 대역을 사용하는 자동 테스트로 검사한다.

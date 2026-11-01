# LocalLLMRunner와 ContextManager 사용 안내

이 문서는 다음 두 모듈의 역할, 호출 방법과 현재 구현 범위를 설명한다.

- **설계 5.1 LocalLLMRunner**: 모델 호출·재생성·중지를 담당한다. 구현 파일은 [server/app/runtime/local_llm_runner.py](../server/app/runtime/local_llm_runner.py)다.
- **설계 5.4 ContextManager**: 채팅·작업별 컨텍스트 보관과 압축을 담당한다. 구현 파일은 [server/app/runtime/context_manager.py](../server/app/runtime/context_manager.py)다.
- **공통 호출 대상**: 기존 [llama_wrapper.py](../src/localLLM_API_proxy/llama_wrapper.py)를 사용한다. 원격·로컬 선택과 인증은 래퍼가 담당한다.

## 실행 환경

**Python 3.12와 uv를 사용하고, 명령은 `server/`에서 실행한다.**

- 저장소 루트를 `PYTHONPATH`에 지정해 기존 프록시를 import한다.
- `server/pyproject.toml`, `.python-version`, `uv.lock`으로 환경을 고정한다.
- 저장소 밖 터미널 채팅기의 입력기는 개발 의존성 `prompt-toolkit`을 사용한다.

```powershell
# server/ 폴더에서 실행
uv sync --locked
$env:PYTHONPATH = (Resolve-Path '..').Path
uv run python
```

## 개발용 터미널 채팅기 입력

**여러 줄을 붙여넣은 뒤 Enter를 누르면 하나의 요청으로 전송한다.**

- 붙여넣기의 줄바꿈·빈 줄·들여쓰기를 본문에 유지한다.
- 키보드 Enter는 전송하고, Ctrl+J 또는 Alt+Enter는 줄바꿈을 추가한다.
- `/stats`, `/compact` 같은 명령은 한 줄 입력일 때만 처리한다.
- 생성·압축 중 Ctrl+C는 기존 `stop()`을 호출한다.

## 인증키와 초기화

**키 탐색은 래퍼가 담당하며 Runner에는 키 위치나 원격 접속 설정이 없다.**

- 테스트 키는 `src/localLLM_API_proxy/`에 `llama_api_key_01.key`처럼 파일만 둔다.
- 래퍼는 실행 파일 주변을 먼저 찾고, 키가 없으면 래퍼 자체 폴더에서 찾는다.
- 저장소 루트의 `keys/` 폴더와 Runner의 키 탐색 코드는 제거했다.
- 생성자는 공통 Llama 생성자와 짧은 생성으로 모델·응답을 검사한다. 오류는 그대로 전파한다.
- 키는 Git에서 제외한다. 테스트 후 키 파일을 정리하며 내용은 출력·로그·커밋에 남기지 않는다.

## 원본 Llama로 전환

**Runner의 Llama 임포트 한 줄을 바꾸고 동일한 코드를 사용한다.**

- 현재: `from src.localLLM_API_proxy.llama_wrapper import Llama`
- 전환: `from llama_cpp import Llama`
- Runner는 `model_path`, `n_ctx`, `n_gpu_layers` 등 공통 인자만 사용한다.
- 원격 사용량 요청과 추론 옵션은 래퍼 내부에서만 처리한다.
- 원본 사용 시 해당 환경에 llama-cpp-python과 실제 GGUF 모델을 준비한다. 모델 경로는 `LocalLLMRunner(model_path="...")`로 지정한다.
- 임포트만 바꾼 코드의 호출·컨텍스트·재생성을 원본 규격 대역으로 검사한다. 실제 GPU 모델 실행은 별도 검증 대상이다.
- 공통 호출 규격은 [llama-cpp-python 0.3.36의 Llama 구현](https://github.com/abetlen/llama-cpp-python/blob/v0.3.36/llama_cpp/llama.py)을 기준으로 확인했다.

## 5.1 모델 호출

**LocalLLMRunner는 수동 호출과 컨텍스트를 사용하는 작업 호출을 제공한다.**

- `manual_request()`: 입력과 시스템 프롬프트로 생성한다. 컨텍스트는 자동 변경하지 않는다.
- `on_token`: 수동·작업 호출·재생성에서 본문 청크를 받는 즉시 전달하는 선택 콜백이다. 최종 반환 튜플은 그대로 유지한다.
- `request()`: `context_tasks`에 지정한 작업만 컨텍스트를 읽고 갱신한다. 기본값은 빈 집합이다.
- `regen()`: 원래 입력 뒤에 직전 출력·형식 오류를 붙여 한 번 재생성한다.
- `MAX_REGEN=3`: 파서 연결 후 사용할 형식 재시도 설정이다. 요청문 누출 재작성 상한과 구분한다.
- PromptManager가 없으므로 현재 `request()`에는 `system_prompt`를 명시한다.

```python
from app.runtime.local_llm_runner import LocalLLMRunner

with LocalLLMRunner(context_tasks=frozenset({"ANSWER"})) as runner:
    result = runner.manual_request("안녕하세요.", "한국어로 짧게 답하세요.")
    text, success, input_tokens, output_tokens, finish_reason = result

    runner.request(
        "run-1", "제 이름은 민수입니다.", "ANSWER", "chat-1",
        system_prompt="최근 대화를 참고해 짧게 답하세요.",
    )
```

## 반환과 오류

**호출 결과는 `(본문, 성공, 입력 토큰, 출력 토큰, 종료 사유)` 튜플이다.**

- 토큰 수는 usage 청크에서 읽는다. 해당 실행 엔진의 스트림이 제공하지 않으면 `None`이다.
- `length`는 답변 토큰 상한에 도달했다는 종료 사유다.
- 프록시 오류·무응답·불완전 수신·전체 대기시간 초과는 예외로 전달한다.
- 오류 응답을 임의로 대체하거나 자동 재시도하지 않는다.

## 중지와 연결 정리

**수신 스레드와 중지 신호를 분리해 다른 스레드에서도 `stop()`을 호출할 수 있다.**

- `stop()`은 `Event`만 설정하며 네트워크 응답을 기다리지 않는다.
- 수신 대기 중 신호가 도착해도 종료 표식 처리 전에 다시 확인해 `stopped`로 반환한다.
- 호출자는 부분 본문과 `success=False`, `finish_reason="stopped"`를 받는다.
- 이미 `on_token`으로 전달한 본문은 최종 부분 본문에 그대로 남는다. 화면에서는 재출력 없이 보존할 수 있다.
- 수신 스레드는 다음 청크 또는 읽기 timeout에서 정리한다. 정리 전에는 새 생성을 거부한다.
- 동시 생성은 1개다. 서버 추론 자체의 즉시 취소는 보장하지 않는다.
- 사용 후 `close()` 또는 `with`로 모델과 연결 자원을 정리한다. `close()`는 진행 중인 작업이 끝날 때까지 기다려 네이티브 모델의 사용 중 해제를 막는다.

## 5.4 컨텍스트 관리와 압축

**ContextManager는 채팅·작업별 내용을 분리하고 28K에 도달하기 전에 5K 목표로 압축한다.**

- `get_id(chat_id, prompt_idx)`는 조합별 ID를 조회·생성하고, `new_id()`는 독립 ID를 만든다.
- `get_context()`, `append()`, `get_context_length()`, `compact_context()`로 내용을 관리한다.
- 길이는 동일 모델의 실제 토크나이저로 계산한다.
- 새 내용을 붙이면 28,000토큰에 도달하는 경우, 기존 내용을 요약한 뒤 새 내용을 추가한다.
- 요약 목표는 5,000토큰, 압축 요청의 출력 상한은 7,000토큰이다. 압축은 `manual_request()`로 수행한다.
- 출력 상한은 `COMPACTION_MAX_TOKENS`로 지정하며 실제 생성 길이는 실행 엔진의 남은 컨텍스트 공간에도 제한된다.
- 새 입력 과다·요약 실패·중지·목표 초과 시 예외를 내고 기존 내용을 보존한다.
- 저장은 현재 메모리 방식이다. DB 복원은 연결하지 않았다.

## 미구현 모듈과 로그

**외부 모듈 연결과 로그 저장은 아직 주석·스켈레톤 상태다.**

- PromptManager·LocalResParser·ChatManager의 예상 호출 위치를 주석으로 남겼다.
- `request()`는 파싱 전 원시 본문을 반환한다. 성공은 생성 완료를 뜻한다.
- `_log_call()`: 새 입력·출력만 전달한다. 이전 컨텍스트 전체는 반복 저장하지 않는다.
- 호출 오류 시에도 이미 받은 부분 출력을 로그 연결 지점에 넘기고 원래 예외를 전파한다.
- 압축 호출은 `log_call=False`로 일반 호출 로그를 생략한다.
- `_log_compaction()`: 압축 반영 시점 UTC와 결과 요약만 전달한다. 압축 대상 원문은 제외한다.
- 실제 `chat_manager.log_event()` 호출은 주석 처리했다.

## 검증

**자동 테스트와 실서버에서 호출·컨텍스트·압축 동작을 확인했다.**

- 자동 테스트: 두 모듈과 프록시의 중지·예외 전파·로그·인증·호출 규격을 검사한다.
- 실서버 확인일: 2026년 11월 1일.
- 확인 범위: 래퍼 내부 키 탐색, 토큰 사용량, 대화 기억, 실제 토크나이저와 요약 호출.
- 작은 시험 임계값에서 원문 211토큰이 새 입력 포함 64토큰으로 줄고 지정 코드가 유지됐다.
- 기본 28K/5K 경계, 실패·중지·로그 분기는 대역 테스트로 확인했다.
- 터미널 입력 테스트 4개로 붙여넣기의 전송 대기·줄바꿈 보존, 직접 줄바꿈과 EOF를 검사했다.
- Windows 터미널과 실제 채팅기에서 여러 줄을 붙여넣은 뒤 Enter로 한 번 전송하고 `/context`에 빈 줄·들여쓰기가 유지되는 것을 확인했다.

```powershell
# server/ 폴더에서 실행
uv run ruff check app tests
uv run ruff format --check app tests
uv run pytest -q
```

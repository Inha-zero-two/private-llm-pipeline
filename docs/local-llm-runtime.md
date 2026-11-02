# LocalLLMRunner와 결과 파서 및 ContextManager 사용 안내

이 문서는 다음 세 모듈의 역할, 호출 방법과 현재 구현 범위를 설명한다.

- **설계 5.1 LocalLLMRunner**: 모델 호출·재생성·중지를 담당한다. 구현 파일은 [server/app/runtime/local_llm_runner.py](../server/app/runtime/local_llm_runner.py)다.
- **설계 5.2 결과 파서**: 상태 없는 `parser()` 함수가 출력 형식 검사와 변환을 담당한다. 구현 파일은 [server/app/runtime/local_res_parser.py](../server/app/runtime/local_res_parser.py)다.
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
- 일반 답변의 출력 상한은 기본 7,000토큰이며 `--max-tokens`로 변경한다. 시작 화면에서 적용값을 확인한다.

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
- `MAX_FORMAT_FAILURES=3`: 최초 응답을 포함해 형식 불일치가 3회면 예외를 낸다. 최초 생성 후 최대 2회 재생성하며, 요청문 누출 재작성 상한과 구분한다.
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

**수동 호출은 `(본문, 성공, 입력 토큰, 출력 토큰, 종료 사유)`, 작업 호출은 `(파싱 결과, 성공, 입력 토큰, 출력 토큰, 종료 사유)`를 반환한다.**

- `manual_request()`와 `regen()`의 첫 값은 원문 문자열이다. `request()` 성공 시에는 작업별 파싱 결과이며, 성공은 생성과 출력 형식 검사 통과를 뜻한다.
- 토큰 수는 usage 청크에서 읽는다. `request()`는 최초 생성과 재생성 사용량을 합산하며, 한 번이라도 해당 사용량을 받지 못하면 그 합계는 `None`이다.
- `length`는 답변 토큰 상한에 도달했다는 종료 사유다.
- 프록시 오류·무응답·불완전 수신·전체 대기시간 초과는 예외로 전달한다.
- `request()`는 파서의 `OutputFormatError`만 잡아 재생성한다. 3번째 형식 불일치에서는 `생성문 형식 불일치` 메시지의 `OutputFormatError`를 상위로 올린다.
- 프록시·통신·코드 오류를 형식 재시도로 감추거나 대체 응답으로 바꾸지 않는다.

## 5.2 결과 파싱

**`parser(raw_output, prompt_idx)`는 검사·변환을 한 번 수행하고 결과만 반환한다. 별도 `check()`나 파서 클래스는 없다.**

- JSON을 한 번 읽고 필수 필드·자료형·허용값을 검사한다. 문자열이나 bool을 정수로 자동 보정하지 않는다.
- JSON 전체를 감싼 코드 블록만 제거한다. 모델 설명문에서 JSON을 추측해서 꺼내거나 잘못된 출력을 복구하지 않는다.
- 텍스트는 공백뿐인 출력을 거부하고, 정상 문자열의 줄바꿈·들여쓰기를 보존한다.
- JSON·필드·타입 오류는 `OutputFormatError`다. 알 수 없는 작업과 외부 모델용 `FRONTIER`는 설정 오류인 `ValueError`다.
- 공유 객체는 `app/models/local_llm_outputs.py`, 작업 열거형은 `app/models/enums.py`에 둔다.

| 작업 | 반환 타입 |
| --- | --- |
| `DETECT` | `list[DetectedItem]`: `text`, `category`(정수 0~2) |
| `NEEDS` | `list[str]` |
| `JUDGE` | `Plan`: `enough`, `need_consent`, `consent_items`, `need` |
| `GUARD` | `list[int]`: 입력 금지 목록의 0부터 시작하는 번호 |
| `PARSE_RULES` | `list[Rule]`: `target`, `scope`(item/category), `action`(allow/deny) |
| `MATCH_RULES` | `list[RuleMatch]`: `item`, `rules`(각각 입력 목록의 번호) |
| `CONSENT` | `ConsentText`: `purpose`, `benefit`, `risk` |
| `DECIDE_CONTEXT` | `keep` 또는 `reset` 문자열 |
| `WRITE`, `REWRITE`, `ANSWER`, `MEMO_SUMMARY` | `str` |

- 정상 빈 목록은 그대로 반환한다. `GUARD` 번호를 실제 개인정보 ID로 바꾸는 작업, 번호의 입력 목록 범위 검사, 판단 보정과 규칙 충돌 처리는 호출 모듈의 책임이다.
- DB ID나 실행 중 허용 상태는 모델 검출 결과에 붙이지 않는다. 최신 DB 스키마의 전송 당시 허용 여부·근거는 `query_items`에서 관리한다.
- 형식 검사를 통과한 마지막 응답만 컨텍스트에 원문으로 추가한다. 각 생성의 호출 로그 연결점은 유지한다.

```python
from app.runtime.local_res_parser import parser

hits = parser('{"hits":[0,2]}', "GUARD")  # [0, 2]
```

## 중지와 연결 정리

**수신 스레드와 중지 신호를 분리해 다른 스레드에서도 `stop()`을 호출할 수 있다.**

- `stop()`은 `Event`만 설정하며 네트워크 응답을 기다리지 않는다.
- 수신 대기 중 신호가 도착해도 종료 표식 처리 전에 다시 확인해 `stopped`로 반환한다.
- 호출자는 부분 본문과 `success=False`, `finish_reason="stopped"`를 받는다.
- 작업 호출도 중지 시에는 부분 원문을 반환하며 파싱·재생성·컨텍스트 추가를 하지 않는다.
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

- PromptManager·ChatManager의 예상 호출 위치를 주석으로 남겼다. 결과 파서와 형식 재시도는 연결했다.
- `_log_call()`: 새 입력·출력만 전달한다. 이전 컨텍스트 전체는 반복 저장하지 않는다.
- 호출 오류 시에도 이미 받은 부분 출력을 로그 연결 지점에 넘기고 원래 예외를 전파한다.
- 압축 호출은 `log_call=False`로 일반 호출 로그를 생략한다.
- `_log_compaction()`: 압축 반영 시점 UTC와 결과 요약만 전달한다. 압축 대상 원문은 제외한다.
- 실제 `chat_manager.log_event()` 호출은 주석 처리했다.

## 검증

**자동 테스트와 실서버에서 호출·컨텍스트·압축 동작을 확인했다.**

- 자동 테스트: Runner·파서·ContextManager와 프록시의 중지·예외 전파·로그·인증·호출 규격을 검사한다.
- 2026년 11월 3일 파서 연결 검증: 서버 테스트 92개와 Ruff 검사 통과. 작업별 타입·필수 필드·엄격한 정수 검사·정상 빈 목록, 3회 실패 상한·재생성 사용량 합산·중지·원본 예외 전파를 대역으로 검증했다. 실모델 호출은 추가하지 않았다.
- 실서버 확인일: 2026년 11월 1일.
- 확인 범위: 래퍼 내부 키 탐색, 토큰 사용량, 대화 기억, 실제 토크나이저와 요약 호출.
- 작은 시험 임계값에서 원문 211토큰이 새 입력 포함 64토큰으로 줄고 지정 코드가 유지됐다.
- 기본 28K/5K 경계, 실패·중지·로그 분기는 대역 테스트로 확인했다.
- 터미널 입력 테스트 4개로 붙여넣기의 전송 대기·줄바꿈 보존, 직접 줄바꿈과 EOF를 검사했다.
- Windows 터미널과 실제 채팅기에서 여러 줄을 붙여넣은 뒤 Enter로 한 번 전송하고 `/context`에 빈 줄·들여쓰기가 유지되는 것을 확인했다.
- 일반 답변 상한을 1,024에서 7,000으로 변경한 뒤 실서버에서 출력 2,292토큰·종료 사유 `stop`으로 정상 완료했다.

### 긴 작업 대화의 실제 압축 검증

**실제 토큰 계수와 기본 28K 자동 압축은 통과했으며, 수동 요약에서 원문에 없는 작업 번호가 추가되는 내용 오류를 발견했다.**

- 입력은 합성 `User:` / `Assistant:` 대화 110쌍이다. 53,403글자이며 실제 모델 기준 27,536토큰이다. 압축·새 응답·후속 회상은 실제 원격 모델로 수행했다.

| 경로 | 압축 전 → 저장 후 토큰 | 핵심 정보 회상 |
| --- | --- | --- |
| `compact_context()` 수동 압축 | 27,536 → 1,917 | 11/11 |
| `request()`에서 새 입력·응답 추가 후 자동 압축 | 28,246 → 2,292 | 13/13 |

- 자동 압축의 요약 자체는 1,581토큰이다. 새 유저 입력과 모델 응답은 뒤에 원문 그대로 보존됐다.
- 두 압축 요청 모두 출력 상한 7,000, 목표 5,000 이하이며 종료 사유는 `stop`이었다. 서버 출력 사용량은 수동 1,918·자동 1,582토큰으로, 저장 본문의 재토큰화 길이와 구분했다.
- 로그 연결점에서 일반 호출은 새 입력·출력만, 압축 이벤트는 시점·요약만 전달되는 것을 확인했다. DB 로그 저장은 여전히 미구현이다.
- 수동 요약이 실제 마지막 작업 W-107 뒤에 없는 W-108·W-109를 추가했다. 자동 요약에는 이 번호가 없었다. 핵심 정보 회상 통과를 전체 요약의 충실성 보장으로 해석하지 않는다.
- 환경은 현재 원격 래퍼와 실제 65,536토큰 서버다. Runner 설정은 32,768이며 네이티브 GPU 실행은 별도 검증 대상이다.
- 원문·요약·회상·사용량은 저장소 밖 `context-test-20261101/`에 보존했다. 테스트 키는 래퍼 폴더에 임시 배치 후 삭제했다.

### 압축 프롬프트의 재시도와 내용 검토

**번호 추가율만으로 B가 우수하다는 이전 해석은 사용하지 않는다. 독립된 내용 검토에서는 세 후보 모두 중요한 구현 계약이나 미완료 상태를 잃었으며, 형식만 바꿔서는 인수인계 품질이 확보되지 않았다.**

- 대화 3종(연속 번호·불규칙 번호·코드/다양한 상태), 각 약 27.5K토큰에 시드 4개를 적용해 프롬프트별 동일한 12조건을 비교했다. 목표 5K·출력 상한 7K·샘플링 설정은 고정했다.
- 최초 타임아웃 6건은 같은 입력·프롬프트·시드로 모두 재시행했다. 대기 한도만 600초에서 1,800초로 늘렸고 Runner 6개를 병렬 실행했다. 최초 36회와 재시도 6회를 보존하며, 내용 평가에서 빠진 조건은 없다.
- 응답 36개 중 33개는 `stop`으로 종료했다. A의 3개는 7K에서 `length`로 끝나 5K 목표 초과로 ContextManager가 저장을 거부했다. 이 결과도 내용 평가에 포함하고 길이 위반은 별도로 기록했다.
- B는 [OpenCode의 구조화된 인수인계 형식](https://github.com/anomalyco/opencode/blob/69269e4ebe309a08919b3c9be7948b9fd699c3a4/packages/core/src/session/compaction.ts)을, C의 추가 규칙은 [Cline의 사실·추정 구분 및 임의 작업 추가 금지 지침](https://github.com/cline/cline/blob/a6507a62a147db4a35e27f4294589742ccac5060/sdk/packages/core/src/extensions/context/summarize-conversation.ts)을 참고한 실험용 변형이다.
- 서브에이전트 3개가 원문과 후보 전체를 읽고 검토했다. 후보는 익명화해 프롬프트 이름과 이전 점수를 숨겼고, 다른 검토자가 조건별 9개를 독립 교차 검토했다.

| 프롬프트 | 내용 왜곡·허위 추가 | 그중 중대 왜곡 | 중요 정보 누락 | 인수인계 가능 판정 |
| --- | --- | --- | --- | --- |
| A 현재 프롬프트 | 6/12 (50.0%) | 1/12 | 10/12 | 5/12 |
| B 구조화 | 8/12 (66.7%) | 2/12 | 12/12 | 0/12 |
| C 구조화+근거/추측 금지 | 8/12 (66.7%) | 2/12 | 12/12 | 3/12 |

- 내용 왜곡은 기능·상태·검증 사실·수량의 잘못된 주장과 작업-모듈 오연결이다. 기능 의미가 유지되는 국소적 ID 표기 문제는 분리한다. 반복 상세의 정상적인 생략은 중요 누락으로 세지 않는다.
- 중요 누락은 다음 구현에 필요한 파서의 성공/JSON 검증 분리와 두 종류 재시도 구분, 미구현 모듈 상태, 예외 전달 원칙 등을 잃은 경우다. 고정 키워드의 존재나 후속 질의 정답률만으로 보존 여부를 판정하지 않았다.
- 실제 사례로 원문의 W-426은 `terminal_chat.py`의 터미널 입력 재현 작업인데 요약에서 개인정보 모듈 작업으로 바뀌었다. 원문에 없는 단위 테스트 완료 주장도 있었다.
- 교차 검토의 내용 왜곡 유무는 9/9, 중요 누락은 8/9, 인수인계 가능 여부는 7/9가 일치했다. 인수인계 가능은 주의사항을 전제로 핵심 작업을 이어갈 수 있는지의 별도 종합 판단이며 오류 없음과 같은 뜻은 아니다.
- 작은 합성 표본에 대한 검토자 판정이다. 실제 서비스 오류율이나 확정적 우열을 뜻하지 않는다. 원문·전체 응답·익명 리뷰·근거·재시도 기록과 최신 `semantic_comparison_report.txt`는 저장소 밖 `context-prompt-eval-20261101/`에 보존했다. 테스트 키는 정리했다.

```powershell
# server/ 폴더에서 실행
uv run ruff check app tests
uv run ruff format --check app tests
uv run pytest -q
```

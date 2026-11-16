# FrontierClient 사용과 처리 흐름

**대상:** [frontier_client.py](../server/app/runtime/frontier_client.py)의 `FrontierClient`. 설계 5.3의 연결 확인, 프론티어 전송, 컨텍스트 유지·초기화 판단, 사용량·비용 계산 및 전송 취소.

**객체 수:** 프로그램당 1개를 만들어 Main에 넘김. 프론티어 대화(컨텍스트)는 `run_id`별로 따로 저장하므로 실행·채팅 사이에 섞이지 않음. 실행이 끝나면 Main이 `clear(run_id)`로 지움.

**호출 대상:** 학교 Bedrock 게이트웨이(OpenAI 호환 API). AWS 계정은 필요 없고 API 키 하나로 호출. 이미 쓰던 `httpx`로 `POST /chat/completions`를 보내므로 패키지 추가는 없음.

## 설정 (환경변수)

[server/.env.example](../server/.env.example)을 `server/.env`로 복사해 값을 채움. `.env`는 git에 올라가지 않음. 환경변수 → `server/.env` → 기본값 순서로 읽고, `.env` 안의 키는 대소문자를 구분하지 않음.

| 이름 | 기본값 | 내용 |
| --- | --- | --- |
| `FRONTIER_API_KEY` | 없음 | 운영자에게 받은 `sk-` 키. 없으면 `check_connection()`이 False라 offline 종료 |
| `FRONTIER_BASE_URL` | `https://bedrock.nxtcloud.kr/v1` | 게이트웨이 주소 |
| `FRONTIER_MODEL` | `bedrock-gpt-6-luna` | 호출할 모델 |

| 모델 | 입력 / 출력 (100만 토큰당) |
| --- | --- |
| `bedrock-gpt-6-luna` | $0.11 / $0.55 |
| `bedrock-haiku` | $0.11 / $0.55 |
| `bedrock-gpt-6.1-sol` | $2.20 / $11.00 |

단가가 없는 모델은 생성자에서 ValueError. 긴 문맥·캐시 단가는 반영하지 않음. 추론 모델이 빈 답을 내지 않도록 출력 상한은 4,096토큰.

## 처리 흐름

```mermaid
flowchart TD
    A["send(run_id, 요청문, pd_table, need)"] --> B{"이전 프론티어 대화가 있고<br>직전 회차 need와 완전히 같나"}
    B -->|아니오| R["reset: 새 대화로 보냄"]
    B -->|예| K["keep: 이전 대화를 붙여 보냄"]
    R --> P["FRONTIER 시스템 프롬프트 + 요청문으로 POST /chat/completions"]
    K --> P
    P -->|200| O["답변·토큰 수·비용·소요 시간 반환, 대화 저장"]
    P -->|429·5xx·네트워크·빈 답변| T["잠깐 쉬고 다시 시도 (최대 3회)"]
    T --> P
    T -->|3회 실패| E["FrontierError (ConnectionError) → Main offline 종료"]
    P -->|400·401·403·422| E
    P -->|stop(run_id)| X["연결을 끊고 FrontierCancelledError"]
```

- `check_connection()`: 비용이 들지 않는 `GET /models`로 확인. 키가 없거나 실패하면 예외 없이 False
- 응답 코드: 400 모델 이름·허용 오류, 401 키 오류, 422 한도 소진은 다시 시도하지 않고 바로 실패
- 모든 질의(성공·실패·취소)는 `_log_query()`로 기록. 지금은 `last_request`에만 남기고 DB 저장(`frontier_queries`·`query_items`)은 ChatManager 구현 후 연결. API 키와 시스템 프롬프트는 저장하지 않음
- 전송 당시 개인정보 허용 상태는 `pd_table`의 `(pd_id, allowed, allowed_by)`로 함께 기록
- `stop(run_id)`: 진행 중인 요청의 연결을 닫고 즉시 반환. 서버에 이미 도착한 요청의 비용은 게이트웨이에서 차감될 수 있음
- 중지 신호는 전송 준비 전에 등록함. 진행 중인 전송이 없을 때 `stop(run_id)`가 불려도 기록해 두어, `clear(run_id)` 전까지 그 실행의 `send()`는 보내지 않고 `FrontierCancelledError`
- 비용은 빈 답변으로 다시 시도한 응답까지 모든 시도의 사용량을 더해 계산. 재시도 끝에 실패해도 질의 기록에는 쓴 만큼 남음
- `FRONTIER_BASE_URL`이 잘못된 주소면 `check_connection()`은 False, `send()`는 재시도 없이 `FrontierError`

## 컨텍스트 유지 판단

`decide_context(run_id, need)`는 로컬 모델을 부르지 않고, 이번 회차의 `need`(Planner `Plan.need`)를 직전 회차와 비교함. 설계서 5.3은 로컬 모델(`DECIDE_CONTEXT`) 판단이지만, 회차마다 로컬 모델 호출만큼 지연이 생겨 PR 리뷰 합의로 바꿈.

| 직전 → 이번 need | 결과 | 이유 |
| --- | --- | --- |
| `["경력"]` → `["경력"]` | keep | 같은 정보를 다시 묻는 경우. 앞 답을 알아야 더 구체적으로 물을 수 있음 |
| `["경력", "희망 직무"]` → `["희망 직무", "경력"]` | keep | 순서는 보지 않음 |
| `["경력"]` → `["희망 직무"]` | reset | 주제가 바뀜. 앞 답은 메모에 있어 요청문에 반영됨 |
| `["경력", "희망 직무"]` → `["경력"]` | reset | 일부만 겹치면 reset |
| 첫 회차, 직전 전송 실패, need 없음 | reset | 이어 붙일 대화가 없음 |

`DECIDE_CONTEXT` 프롬프트(10번)와 파서 규칙은 남아 있지만 지금은 쓰지 않음.

## Main 연결

| Main 단계 | 호출 |
| --- | --- |
| 준비·왕복 시작 | `check_connection()` |
| 6) 프론티어 전송 | `send(run_id, request_text, pd_table, plan.need)` → `FrontierReply` (`answer`, `cost`, 토큰 수, 소요 시간, `context_mode`, `model`) |
| 중지 | `stop(run_id)` |
| 정리 | `clear(run_id)` |

`FrontierReply`는 Main과 함께 쓰므로 [llm_results.py](../server/app/models/llm_results.py)에 둠.

## 사용법

```python
from app.data.prompt_manager import PromptManager
from app.runtime.frontier_client import FrontierClient

prompts = PromptManager()
client = FrontierClient(prompt_manager=prompts)
if client.check_connection():
    reply = client.send("run-1", "백엔드 신입 직무의 핵심 역량을 알려 줘.", need=["핵심 역량"])
    print(reply.answer, reply.cost, reply.context_mode)  # 첫 회차는 reset
client.clear("run-1")
```

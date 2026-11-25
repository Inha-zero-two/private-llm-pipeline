# FrontierClient 사용과 처리 흐름

**대상:** [frontier_client.py](../server/app/runtime/frontier_client.py)의 `FrontierClient`. 설계 5.3의 연결 확인, 프론티어 전송, 컨텍스트 유지·초기화 판단, 사용량·비용 계산 및 전송 취소.

**객체 수:** 프로그램당 1개를 만들어 Main에 넘김. 마지막 성공 응답 ID를 `run_id`별로 관리하므로 실행·채팅 사이에 섞이지 않음. 서로 다른 실행은 동시 전송 가능하고, 같은 실행의 중복 전송은 거부함. 실행이 끝나면 Main이 `clear(run_id)`로 로컬 응답 ID를 지움.

**호출 대상:** OpenAI의 `POST /v1/responses`. 기존 `httpx`와 `FrontierClient.send()`·`FrontierReply` 호출 규격을 유지하므로 새 패키지나 DB·화면 변경은 없음.

## 설정 (환경변수)

[server/.env.example](../server/.env.example)을 `server/.env`로 복사해 값을 채움. `.env`는 git에 올라가지 않음. 환경변수 → `server/.env` → 기본값 순서로 읽고, `.env` 안의 키는 대소문자를 구분하지 않음.

| 이름 | 기본값 | 내용 |
| --- | --- | --- |
| `FRONTIER_API_KEY` | 없음 | 프로젝트용 OpenAI API 키. 없으면 `check_connection()`이 False라 offline 종료 |
| `FRONTIER_BASE_URL` | `https://api.openai.com/v1` | `/responses`를 제외한 기본 주소 |
| `FRONTIER_MODEL` | `gpt-6-luna` | 현재 단가·캐시 요율을 지원하는 모델 |
| `FRONTIER_WEB_SEARCH` | `true` | `true`: 검색 도구 제공 및 `tool_choice=auto`. `false`: 검색 도구 미제공 |

| 모델 | 입력 / 출력 (100만 토큰당) |
| --- | --- |
| `gpt-6-luna` | $0.10 / $0.50 |

2026-11-26 기준 Standard 요금의 추정 비용을 반환함. 캐시 읽기($0.01), 30분 쓰기($0.125), 12시간 쓰기($0.25)를 구분하고 입력이 272K를 넘는 응답은 입력 2배·출력 1.5배 요율을 적용함. 검색은 `tool_usage.web_search.num_requests` 기준 건당 $0.01을 더함. 이 필드가 없는 호환 응답은 `web_search_call`의 search 액션 수로 추정하므로 실제 검색 청구 건수와 다를 수 있음. 검색 콘텐츠 입력과 추론 출력은 이미 `usage`에 포함되어 중복 합산하지 않음. 재시도별 비용을 계산한 뒤 합산함.

단가가 없는 모델은 생성자에서 `CONFIGURATION_ERROR`. 출력 상한은 `max_output_tokens=4096`. 실제 청구·크레딧 잔액은 응답에 포함되지 않으므로 Billing·비용 집계와 구분함. 단가 변경이나 다른 처리 등급·지역 사용 시 계산식을 함께 갱신해야 함. [모델·캐시 요금](https://developers.openai.com/api/docs/models/gpt-6-luna), [검색 요금](https://developers.openai.com/api/docs/pricing#built-in-tools).

### 웹 검색과 대화 유지

- 검색을 켜면 `tools=[{"type":"web_search"}]`, `tool_choice="auto"`를 보냄. 모델이 필요할 때만 검색하며, 별도 검색 API나 판단 모델을 호출하지 않음. 검색 출처의 `url_citation`은 답변 안의 Markdown 링크로 변환함.
- 기존 `need` 비교 정책으로 keep인 경우만 `previous_response_id`에 같은 실행의 마지막 성공 ID를 보냄. 이전 메시지 전체를 다시 보내지 않음. reset이면 응답 ID를 제외해 새 대화로 시작함.
- 최상위 `instructions`는 응답 ID로 이어지지 않으므로 FRONTIER 시스템 프롬프트를 매 요청에 다시 보냄. 실패·중지·미완료 응답 ID는 다음 컨텍스트로 저장하지 않음.
- `store=true`로 OpenAI가 응답을 저장함. 일반 응답의 기본 보관 기간은 30일이며, `clear()`는 원격 저장 데이터를 삭제하는 API 호출이 아님. 현재처럼 실행이 끝나면 대화를 초기화하며, 이전 문맥 입력 토큰도 과금 대상임. 원격 저장을 허용하지 않는 운영 조건에서는 이 응답 ID 방식을 사용하기 전에 별도 구성이 필요함.

[OpenAI 웹 검색](https://developers.openai.com/api/docs/guides/tools-web-search), [응답 ID와 보관 기간](https://developers.openai.com/api/docs/guides/conversation-state).

## 처리 흐름

```mermaid
flowchart TD
    A["send(run_id, 요청문, pd_table, need)"] --> B{"이전 프론티어 대화가 있고<br>직전 회차 need와 완전히 같나"}
    B -->|아니오| R["reset: 새 대화로 보냄"]
    B -->|예| K["keep: previous_response_id 지정"]
    R --> P["FRONTIER instructions + 요청문으로 POST /responses"]
    K --> P
    P -->|200| O["답변·토큰 수·비용·소요 시간 반환, 대화 저장"]
    P -->|429·5xx·네트워크·빈 답변| T["잠깐 쉬고 다시 시도 (최대 3회)"]
    T --> P
    T -->|3회 실패| E["FrontierError (ConnectionError) → Main offline 종료"]
    P -->|400·401·403·422| E
    P -->|stop(run_id)| X["연결을 끊고 FrontierCancelledError"]
```

- `check_connection()`: 비용이 들지 않는 `GET /models`로 확인. 키가 없거나 실패하면 예외 없이 False
- 인증·요청 오류와 잔액·지출 한도 관련 429는 바로 실패. 요청 속도 제한 429와 5xx·네트워크 오류만 재시도. 빈 답변·미완료 응답도 최대 3회까지 재시도하며 사용량을 합산함
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
| 성공 응답이 없는 첫 회차, need 없음 | reset | 이어 붙일 대화가 없음 |

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
    reply = client.send(1, "백엔드 신입 직무의 핵심 역량을 알려 줘.", need=["핵심 역량"])
    print(reply.answer, reply.cost, reply.context_mode)  # 첫 회차는 reset
client.clear(1)
```

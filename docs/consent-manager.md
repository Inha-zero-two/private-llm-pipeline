# ConsentManager 사용과 처리 흐름

상세설계서 4.3과 [화면 API 명세](screen-api-spec.md)의 API-06·08에 따라 실행 중 개인정보 공개 동의를 처리한다. 최신 `dev`를 기준으로 구현했으며, 저장소는 PR #34의 ChatManager 계약을 주입받는다. PR #35의 영구 공개 설정과 별도로 동작한다.

## 생성과 연결

```python
from app.privacy.consent_manager import ConsentManager

# send_event(event, data, event_id)는 저장을 반복하지 않고 SSE 전송 큐에 넣는다.
consent_manager = ConsentManager(chat_manager, runner, emit=send_event)
# 같은 인스턴스를 Main의 consent_manager 인자로 전달한다.
```

프로그램에서 인스턴스 하나를 같은 이벤트 루프에서 공유한다. `build_message()`와 `apply()`는 동기 함수이며, `request()`, `receive()`, `cancel()`, `release()`, `get_pending()`은 비동기 함수다. Main의 `_call()`은 동기·비동기 함수를 모두 처리한다. DB와 모델의 동기 호출은 별도 스레드에서 실행한다.

| 함수 | 처리 |
| --- | --- |
| `build_message(run_id, consent_items, need, pd_table)` | 아직 허용되지 않았고 이번 실행에서 거절하지 않은 항목의 안내를 만든다. 항목이 없으면 None, 모델 생성 실패 시 고정 안내를 반환한다. |
| `await request(message)` | DB 저장으로 consent_id를 발급하고 요청 이벤트를 저장·전달한 뒤 사용자 응답까지 기다린다. 자동 응답과 대기 시간 제한은 없다. |
| `await receive(run_id, consent_id, text)` | 자연어를 로컬 모델로 해석하고 요청 항목의 결정을 저장·발행한다. 성공 시 request의 대기를 풀어 실행을 재개한다. |
| `apply(result, message, pd_table)` | 이 인스턴스에서 확정한 결과만 메모리 테이블에 적용한다. 허용은 consent, 거절은 denied로 표시한다. |
| `await cancel(run_id)` | 진행 중인 해석 결과를 무효화하고 취소를 저장·발행한다. request의 대기는 CancelledError로 종료한다. |
| `await release(run_id, pd_table)` | 변경한 항목을 원래 allowed·allowed_by로 복구하고 남은 대기를 취소한다. 반복 호출할 수 있다. |
| `await get_pending(run_id)` | 재접속 복원을 위해 DB의 현재 대기 요청을 조회한다. |

`ConsentMessage`는 저장 전 안내이며 요청 후 consent_id가 채워진다. 저장 계층의 `ConsentRequest`는 요청 시각·응답 시각·결과를 포함하는 이력이다. 전자의 자료형과 `ConsentResult`, `ConsentReply`, 모델 해석 결과는 `app/models/consent_results.py`에 정의한다. 저장 계층의 파일이나 스키마를 이 브랜치로 복사하지 않는다.

## 자연어 해석 계약

`CONSENT`는 안내 전용이다. 입력은 items·need, 출력은 `ConsentText(purpose, benefit, risk)`다. 목적과 현실적인 이점, 외부 모델 전송의 위험, 실행 동안만 유효한 허용과 거절 선택을 안내한다. 생성에 실패해도 동의 대상과 고정 안내를 사용자에게 보여주며 자동으로 응답하지 않는다.

`INTERPRET_CONSENT`는 별도 작업으로 추가했다. 기존 프롬프트 번호 0~12는 유지하고 13에 등록한다. 입력은 요청의 items와 사용자 text이며 이전 대화는 사용하지 않는다.

```json
{"ok":true,"decisions":[{"pdId":11,"allow":true}],"message":"나이는 공개하고 직장은 공개하지 않습니다."}
```

모델은 명확히 언급한 항목만 반환한다. 전체 허용·전체 거절은 모든 요청 항목을 반환한다. 서버는 미언급 항목에 false를 채워 요청 항목마다 정확히 하나의 결정을 만든다. 모호한 응답은 `{"ok":false,"decisions":[],"message":"공개할 정보를 명확히 답해 주세요."}`다.

형식 오류, 빈 결정, 중복 ID, 요청에 없는 ID는 해석 실패다. 빈 사용자 입력은 검증 오류다. 해석 실패에는 결과 이벤트를 보내거나 실행을 재개하지 않는다. 수정한 응답을 다시 제출할 수 있다. 허용 여부의 의미 판단은 로컬 모델이 담당하며 서버는 키워드로 허용을 추정하지 않는다.

## API와 SSE 연결 계약

API-08 핸들러는 실행 존재·종료 상태를 먼저 확인한 뒤 다음 함수를 호출한다. 현재 dev에는 HTTP 라우터와 SSE 재전송 계층이 없으므로 이 모듈은 서비스 진입점과 저장·전송 콜백을 제공한다.

```python
reply = await consent_manager.receive(run_id, body.consent_id, body.text)
return reply.model_dump(by_alias=True, exclude_none=True)
```

| 결과·예외 | HTTP 처리 |
| --- | --- |
| ConsentReply(ok=True) | 200, 요청 항목 전체의 decisions 포함 |
| ConsentReply(ok=False) | 200, decisions 생략, 입력과 동의 대기 유지 |
| NoPendingConsentError | 409 NO_PENDING_CONSENT. 요청 불일치·중복·처리 중 응답도 포함 |
| ValueError | 422 VALIDATION_ERROR |
| ConnectionError / TimeoutError | 503 LOCAL_LLM_UNAVAILABLE, 대기 유지 |
| 저장 실패 등 그 밖의 예외 | 500 INTERNAL_ERROR, 기록과 스트림을 조회하여 상태 확인 |

Runner의 요청 충돌은 `RunnerBusyError`로 구분하여 503에 해당하는 ConnectionError로 전달한다. 연결·수신 실패는 Runner의 `unavailable`, 출력 형식 실패는 `parse_failed`로 구분한다. 의미 해석 실패와 모델 장애를 같은 자동 거절로 처리하지 않는다.

요청·결과는 ChatManager에 저장하고 `log_event()`로 발급한 ID를 `emit(event, data, event_id)`에 그대로 전달한다. payload는 API-06의 camelCase 필드와 일치한다. 콜백은 이미 저장한 이벤트를 다시 저장하지 않으며, 실행별 전송 큐를 선택하도록 구성한다. 연결 끊김과 전송 시간 초과는 저장된 이벤트의 재전송으로 복구한다. 결과 이벤트 전달을 시도한 후에 request의 대기를 푼다.

재접속에서는 ChatManager의 이벤트와 `get_pending_consent()`를 이용해 API-06의 재전송·`replay_complete` 흐름을 구성한다. 서비스는 DB의 요청 ID를 복원하며, 프론트엔드는 재전송 완료 전 입력을 활성화하지 않는다. 프로세스 재시작 후 실행 재개는 지원하지 않는다. PR #34의 `recover_interrupted_runs()`가 대기 요청을 취소하고 중단된 실행을 종료한다.

PR #34는 동의 이력 갱신과 이벤트 저장을 별도 트랜잭션으로 제공한다. 이벤트 저장까지 실패하면 500으로 처리하며 자동 허용하지 않는다. 요청·결과 이력은 실행 상세에서도 조회할 수 있다. 이력과 이벤트의 원자적 저장 및 연결 시점의 이벤트·대기 요청 스냅샷은 저장·SSE 계층의 후속 통합 대상이다.

## 실행 적용과 종료

모든 항목 거절이면 Main은 consent_denied로 가능한 정보의 최종 답변을 생성한다. 일부만 허용하면 허용·거절을 테이블에 적용한 뒤 Planner가 다시 판단한다. 거절된 항목은 같은 실행에서 다시 묻지 않는다. 전체 허용이면 기존 계획으로 진행한다.

임시 상태를 `update_pd_allowed()`나 영구 설정에 저장하지 않는다. Main의 finally에서 release를 호출하므로 정상 종료·오류·중지에서 모두 원래 상태로 복구한다. 다음 실행에는 거절 이력도 넘기지 않는다. 저장·발행 중 HTTP 작업이 취소돼도 시작한 처리를 끝내 상태를 맞춘 뒤 취소를 전달한다. 중지가 모델 해석보다 먼저 반영되면 늦게 도착한 결과는 폐기한다.

## 검증

저장소 컨벤션에 따라 신규 테스트는 저장소 밖에서 실행했다. 실제 SQLite ChatManager(PR #34), 실제 Runner·Parser·Planner·Main과 모델 전송 대역으로 항목별 해석, 재판단, 종료 시 복구, 저장 실패, 중복 응답, 재접속 조회, 해석 중 중지와 반복 취소를 검증했다. 기존·외부 테스트 총 106개가 통과했다.

HTTP·브라우저 SSE 동작은 이번 검증에 포함하지 않았다.

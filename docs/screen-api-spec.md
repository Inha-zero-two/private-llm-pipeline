# 화면과 앱 서버 API 명세서

## API-01 새 채팅 생성

- **기능:** 빈 채팅을 생성한다.
- **HTTP 메서드와 경로:** `POST /api/chats`

### 요청 필드

없음. 요청 본문을 보내지 않는다.

### 응답 필드와 JSON 예시

성공: `200 OK`, `application/json`.

| 이름 | 타입 | 설명 |
| --- | --- | --- |
| `chatId` | integer | 생성한 채팅 ID. DB의 INTEGER PRIMARY KEY |

```json
{"chatId":1}
```

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 500 | `INTERNAL_ERROR` | 생성 실패 안내, 기존 채팅 유지 |

오류 본문 예시:

```json
{"code":"INTERNAL_ERROR","message":"채팅을 생성하지 못했습니다."}
```

## API-02 채팅 목록 조회

- **기능:** 채팅 목록을 최근 수정 순으로 조회한다. 제목은 첫 질문의 앞 30자이며, 첫 질문이 없으면 빈 문자열이다.
- **HTTP 메서드와 경로:** `GET /api/chats`

### 요청 필드

없음.

### 응답 필드와 JSON 예시

성공: `200 OK`, `application/json`. 최상위는 배열이며 채팅이 없으면 `[]`다.

| 이름 | 타입 | 설명 |
| --- | --- | --- |
| `[].chatId` | integer | 채팅 ID |
| `[].title` | string | 채팅 제목 |

```json
[{"chatId":1,"title":"자기소개서 작성 도와줘"}]
```

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 500 | `INTERNAL_ERROR` | 목록 조회 실패 안내와 재시도 제공, 기존 목록 유지 |

```json
{"code":"INTERNAL_ERROR","message":"채팅 목록을 불러오지 못했습니다."}
```

## API-03 채팅 상세 조회

- **기능:** 선택한 채팅의 전체 메시지와 실행 요약을 조회한다. 프론티어 질의·동의 내역은 사용자가 펼칠 때 API-11로 조회한다.
- **HTTP 메서드와 경로:** `GET /api/chats/{chatId}`

### 요청 필드

| 위치 | 이름 | 타입 | 필수 | 기본값 | 제약 |
| --- | --- | --- | --- | --- | --- |
| path | `chatId` | integer | 예 | 없음 | 1 이상의 존재하는 채팅 ID |

### 응답 필드와 JSON 예시

성공: `200 OK`, `application/json`. nullable 필드는 값이 없어도 `null`로 포함한다. 배열은 기록이 없으면 `[]`다.

| 이름 | 타입 | 설명 |
| --- | --- | --- |
| `chatId` | integer | 채팅 ID |
| `title` | string | 채팅 제목 |
| `messages` | object[] | 생성 시각 오름차순 메시지 |
| `messages[].messageId` | integer | 메시지 ID |
| `messages[].role` | string | `user` 또는 `assistant` |
| `messages[].content` | string | 질문 또는 답변 내용 |
| `messages[].createdAt` | string | 시간대를 포함한 ISO 8601 생성 시각 |
| `runs` | object[] | 시작 시각 오름차순 실행 요약 |
| `runs[].runId` | integer | 실행 ID. SSE 재연결·중지와 API-11 상세 조회에 사용 |
| `runs[].userMessageId` | integer | 실행을 시작한 질문 메시지 ID. 요약을 해당 질문 아래에 연결 |
| `runs[].status` | string | `running` 또는 `finished` |
| `runs[].notice` | string 또는 null | 중지·상한 도달·공개 거부·오류 안내. 진행 중·정상 완료는 null |

`status: 'running'`인 실행은 `runId`로 API-06에 재연결한다. 답변 생성 전에 중지·오류로 종료돼 답변 메시지가 없어도 `notice`를 질문 아래에 표시한다. SSE의 `done.limited` 또는 `error.message`를 저장된 실행 상태에서 동일한 `notice`로 복원한다.

```json
{
  "chatId": 1,
  "title": "27살인데 진로 상담해줘",
  "messages": [
    {"messageId":1,"role":"user","content":"27살인데 진로 상담해줘","createdAt":"2026-11-07T14:00:00+09:00"},
    {"messageId":2,"role":"assistant","content":"관심 직무부터 정리해 보세요.","createdAt":"2026-11-07T14:00:30+09:00"}
  ],
  "runs": [
    {"runId":1,"userMessageId":1,"status":"finished","notice":null}
  ]
}
```

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 삭제되었거나 없는 채팅 안내, 목록 갱신 |
| 422 | `VALIDATION_ERROR` | 채팅 ID 형식 확인, 목록 갱신 |
| 500 | `INTERNAL_ERROR` | 불러오기 실패 안내와 재시도 제공 |

```json
{"code":"NOT_FOUND","message":"채팅을 찾을 수 없습니다."}
```

## API-04 채팅 삭제

- **기능:** 채팅과 연결된 기록을 삭제한다. 앱 공통 공개 허용 설정은 유지한다.
- **HTTP 메서드와 경로:** `DELETE /api/chats/{chatId}`

### 요청 필드

| 위치 | 이름 | 타입 | 필수 | 기본값 | 제약 |
| --- | --- | --- | --- | --- | --- |
| path | `chatId` | integer | 예 | 없음 | 1 이상의 존재하고 실행 중이 아닌 채팅 ID |

### 응답 필드와 JSON 예시

성공: `200 OK`. 본문과 JSON 필드가 없다.

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 이미 삭제된 채팅 안내, 목록 갱신 |
| 409 | `CHAT_RUNNING` | 삭제 취소, 실행 종료 후 다시 삭제하도록 안내 |
| 500 | `INTERNAL_ERROR` | 삭제 실패 안내, 목록에서 해당 채팅 유지 |

```json
{"code":"CHAT_RUNNING","message":"실행 중인 채팅은 삭제할 수 없습니다."}
```

## API-05 질문 전송

- **기능:** 질문을 저장하고 실행을 시작한다. 반환된 `runId`로 API-06에 연결해 진행 상황과 답변을 받는다.
- **HTTP 메서드와 경로:** `POST /api/chats/{chatId}/messages`

### 요청 필드

본문 형식: `application/json`.

| 위치 | 이름 | 타입 | 필수 | 기본값 | 제약 |
| --- | --- | --- | --- | --- | --- |
| path | `chatId` | integer | 예 | 없음 | 1 이상의 존재하는 채팅 ID |
| body | `text` | string | 예 | 없음 | 공백 제거 후 빈 문자열 금지. 최대 길이 미정 |

### 응답 필드와 JSON 예시

성공: `200 OK`, `application/json`.

| 이름 | 타입 | 설명 |
| --- | --- | --- |
| `runId` | integer | 시작한 실행 ID |

```json
{"runId":1}
```

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 채팅 없음 안내, 입력 보존 후 목록 갱신 |
| 422 | `VALIDATION_ERROR` | 입력 유지, 질문 입력·수정 안내 |
| 500 | `INTERNAL_ERROR` | 실패 안내, 입력 보존 |

응답을 받기 전에 연결이 끊기면 자동으로 같은 질문을 재전송하지 않는다. API-03으로 실제 실행 생성 여부를 확인한다.

```json
{"code":"VALIDATION_ERROR","message":"질문을 입력해 주세요."}
```

## API-06 진행 상황과 답변 스트림

- **기능:** 실행 단계, 프론티어 내역, 동의 요청·결과, 답변 청크 및 종료를 SSE로 전달한다. 재접속 시 누락된 이벤트를 재전송한다.
- **HTTP 메서드와 경로:** `GET /api/runs/{runId}/events`

### 요청 필드

| 위치 | 이름 | 타입 | 필수 | 기본값 | 제약 |
| --- | --- | --- | --- | --- | --- |
| path | `runId` | integer | 예 | 없음 | 1 이상의 존재하는 실행 ID |
| header | `Last-Event-ID` | integer | 아니요 | 없음 · 처음부터 재전송 | 해당 실행의 0 이상 이벤트 ID. HTTP 헤더에는 정수 문자열로 전송. 0은 처음부터, 마지막 ID보다 큰 값은 422 |

### 응답 필드와 JSON 예시

성공: `200 OK`, `text/event-stream`. 각 `data`는 JSON이며 빈 줄로 청크를 구분한다. 아래 이벤트의 이름을 `event` 필드에 기록한다.

| 이벤트 | data 필드와 타입 | 화면 처리 |
| --- | --- | --- |
| `step` | `label: string` | 현재 동작 교체 |
| `frontier` | `request: string`, `answer: string` | 실제 전송한 요청문과 답변 추가 |
| `consent_request` | `consentId: integer`, `items: object[]`, `purpose: string`, `benefit: string`, `risk: string` | 자연어 동의 입력 표시. items의 항목은 `{ pdId: integer, label: string, text: string, category: 0 또는 1 또는 2 }` |
| `consent_result` | `consentId: integer`, `result: 'answered' 또는 'cancelled'`. answered이면 `text: string`, `decisions: { pdId: integer, allow: boolean }[]` 추가 | 해당 요청 입력 닫기, 항목별 결과 표시. cancelled에는 text·decisions 없음 |
| `token` | `text: string` | 답변에 순서대로 누적 |
| `done` | `endReason: string`, `limited: string 또는 null`, `answerMessageId: integer 또는 null` | 실행 완료, 안내 표시, 전송 버튼 복귀 |
| `error` | `code: string`, `message: string` | 오류 안내, 전송 버튼 복귀 |
| `replay_complete` | `lastEventId: integer`, `pendingConsentId: integer 또는 null` | 재전송 복원 완료, 현재 대기 중인 동의 입력 활성화 |

`done.endReason`은 `info_enough`, `call_limit`, `regen_limit`, `budget_limit`, `offline`, `stopped`, `consent_denied` 중 하나다. `info_enough`의 `limited`는 null이며, 나머지는 서버 안내 문구다. `stopped`는 새 최종 답변을 만들지 않고 이미 생성한 부분 답변만 보존한다. 나머지 종료 사유는 가능한 정보로 최종 답변을 생성한 후 완료한다.

### 청크 형식과 JSON 예시

실행 기록 이벤트의 `id`는 실행별로 1부터 증가한다. `replay_complete`는 연결 제어 이벤트로 ID가 없고, 기록에 저장하지 않는다.

```text
id: 1
event: step
data: {"label":"개인정보 검출 중"}

id: 2
event: consent_request
data: {"consentId":1,"items":[{"pdId":11,"label":"나이","text":"27살","category":2}],"purpose":"연령에 맞는 정보 확인","benefit":"구체적인 답변","risk":"외부 모델로 전송됨"}

event: replay_complete
data: {"lastEventId":2,"pendingConsentId":1}

id: 3
event: consent_result
data: {"consentId":1,"result":"answered","text":"나이는 공개해도 돼","decisions":[{"pdId":11,"allow":true}]}

id: 4
event: frontier
data: {"request":"27세의 진로 탐색 방법을 알려줘.","answer":"관심사와 직무 역량을 정리하세요."}

id: 5
event: token
data: {"text":"관심 직무부터 정리해 보세요."}

id: 6
event: done
data: {"endReason":"info_enough","limited":null,"answerMessageId":2}

```

### 종료 조건과 재접속 처리

- `done` 또는 `error` 중 하나가 마지막 이벤트다. 처리 후 서버는 스트림을 종료하고 화면도 `EventSource.close()`를 호출한다. 단순 연결 단절은 실행 종료로 판단하지 않는다.
- 서버는 실행 시작부터 이벤트를 기록한다. `Last-Event-ID`가 없으면 처음부터, 있으면 그다음 이벤트부터 재전송한다. 진행 중인 실행의 기록은 만료시키지 않는다.
- 서버는 연결 시점의 마지막 이벤트 ID·대기 동의 요청을 함께 읽고, 그 ID까지 재전송한 뒤 `replay_complete`를 한 번 보낸다. 그동안 발생한 실시간 이벤트는 이어서 전달해 누락을 막는다. 이미 종료된 실행은 마지막 종료 이벤트까지만 재전송하고 닫는다.
- 화면은 중복 ID를 무시한다. 처음부터 복원할 때 해당 실행의 임시 답변·질의 내역을 재구성하고, 조회한 답변에 토큰을 중복으로 붙이지 않는다.
- 재전송 중 동의 입력을 비활성화하고, `replay_complete.pendingConsentId`와 일치하며 아직 결과가 없는 요청만 활성화한다. 이후 실시간 요청은 즉시 활성화한다.
- 연결 단절 시 재접속 중 안내를 표시한다. 기존 EventSource의 자동 재접속은 마지막 ID를 사용하고, 새 객체를 만들면 처음부터 복원한다.
- 종료 후 보존 기간이 지난 기록은 404다. 화면은 자동 재접속을 중단하고 API-03으로 메시지·실행 요약을 복원한다. 상세 내역이 필요하면 API-11을 조회한다. EventSource에서 HTTP 상태를 직접 읽을 수 없으므로 연결 실패 시 별도 fetch로 상태를 확인한다.

### 오류 코드와 화면 처리

연결 전 HTTP 오류는 `application/json`으로 반환한다.

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 없는 실행 또는 만료된 기록 안내. API-03으로 요약 복원, 상세는 API-11로 조회 |
| 422 | `VALIDATION_ERROR` | 재접속 ID 확인, 전체 조회 후 처음부터 복원 |
| 500 | `INTERNAL_ERROR` | 연결 실패 안내, 재시도 제공 |

```json
{"code":"NOT_FOUND","message":"실행이 없거나 스트림 기록이 만료되었습니다."}
```

연결 후 중간 오류는 HTTP 상태를 변경하지 않고 마지막 `error` 이벤트로 보낸다. 예시 코드는 `LOCAL_LLM_ERROR` 또는 `INTERNAL_ERROR`이며, 화면은 이미 받은 답변을 유지하고 오류 안내를 표시한다. 연결 오류 콜백과 JSON data가 있는 서버의 `error` 이벤트를 구분한다. 프론티어 연결 불가는 가능한 경우 `offline` 사유의 최종 답변과 `done`으로 처리한다.

```text
id: 5
event: error
data: {"code":"LOCAL_LLM_ERROR","message":"로컬 모델 응답을 처리하지 못했습니다."}

```

## API-07 실행 중지

- **기능:** 진행·동의 대기·최종 답변 생성 중인 실행에 중지를 요청한다.
- **HTTP 메서드와 경로:** `POST /api/runs/{runId}/stop`

### 요청 필드

| 위치 | 이름 | 타입 | 필수 | 기본값 | 제약 |
| --- | --- | --- | --- | --- | --- |
| path | `runId` | integer | 예 | 없음 | 1 이상의 존재하고 진행 중인 실행 ID |

### 응답 필드와 JSON 예시

성공: `200 OK`. 본문과 JSON 필드가 없다. 성공은 중지 접수를 뜻하며 실제 종료는 API-06의 `done.endReason: 'stopped'`로 확인한다. 동의 대기 중이면 해당 요청의 `consent_result: cancelled`가 먼저 온다. 부분 답변이 있으면 저장하고 `done.answerMessageId`에 반환한다.

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 실행 없음 안내, 채팅 상세 갱신 |
| 409 | `RUN_FINISHED` | 이미 종료됨 안내, 채팅 상세 갱신 |
| 500 | `INTERNAL_ERROR` | 중지 접수 실패 안내, 스트림 구독 유지 |

```json
{"code":"RUN_FINISHED","message":"이미 종료된 실행입니다."}
```

## API-08 자연어 동의 응답

- **기능:** 사용자의 자연어 응답을 로컬 LLM으로 해석해 현재 요청의 항목별 공개 허용 여부를 정한다. 언급하지 않은 항목은 금지하고, 허용은 이번 실행에만 적용한다. 모든 항목을 거부하면 `consent_denied`로 최종 답변을 생성한다.
- **HTTP 메서드와 경로:** `POST /api/runs/{runId}/consent`

### 요청 필드

본문 형식: `application/json`.

| 위치 | 이름 | 타입 | 필수 | 기본값 | 제약 |
| --- | --- | --- | --- | --- | --- |
| path | `runId` | integer | 예 | 없음 | 1 이상의 존재하고 진행 중인 실행 ID |
| body | `consentId` | integer | 예 | 없음 | 해당 실행에서 현재 응답을 기다리는 요청 ID |
| body | `text` | string | 예 | 없음 | 공백 제거 후 빈 문자열 금지. 최대 길이 미정 |

### 응답 필드와 JSON 예시

성공: `200 OK`, `application/json`. 자연어를 확정적으로 해석하지 못한 경우도 `ok: false`로 반환하고 동의 대기를 유지한다.

| 이름 | 타입 | 설명 |
| --- | --- | --- |
| `ok` | boolean | 해석·적용 성공 여부 |
| `decisions` | object[] | ok가 true일 때만 포함. 요청 항목별로 정확히 하나의 결정 |
| `decisions[].pdId` | integer | 현재 요청에 포함된 개인정보 ID |
| `decisions[].allow` | boolean | 공개 허용 여부 |
| `message` | string | 화면 안내 문구 |

```json
{
  "ok": true,
  "decisions": [{"pdId":11,"allow":true},{"pdId":12,"allow":false}],
  "message": "나이는 공개하고 직장은 공개하지 않습니다."
}
```

해석 실패 응답:

```json
{"ok":false,"message":"공개할 정보를 명확히 답해 주세요."}
```

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 200 | `ok: false` · 오류 코드 없음 | 입력·대기 유지, 안내 후 수정·재제출 |
| 404 | `NOT_FOUND` | 실행 없음 안내, 채팅 상세 갱신 |
| 409 | `NO_PENDING_CONSENT` | 대기 요청 없음·요청 불일치·응답 완료 안내, 스트림 상태로 동의 입력 갱신 |
| 409 | `RUN_FINISHED` | 입력 닫기, 채팅 상세 갱신 |
| 422 | `VALIDATION_ERROR` | 입력 유지, 응답 수정 안내 |
| 503 | `LOCAL_LLM_UNAVAILABLE` | 해석 불가 안내, 대기 유지 후 재시도 또는 중지 |
| 500 | `INTERNAL_ERROR` | 처리 실패 안내, 스트림·상세 조회로 적용 여부 확인 |

성공 시 서버는 결정을 저장하고 `consent_result`를 보낸 뒤 실행을 재개한다. HTTP와 SSE의 도착 순서를 가정하지 않고, 화면은 해당 요청의 `consent_result`로 입력을 닫는다. 해석 실패 시 결과 이벤트를 보내지 않는다. 중지가 먼저 반영되면 뒤늦은 해석 결과를 적용하지 않는다.

```json
{"code":"NO_PENDING_CONSENT","message":"현재 응답할 수 있는 동의 요청이 아닙니다."}
```

## API-09 공개 허용 설정 조회

- **기능:** 앱 전체에 적용되는 현재 자연어 설정문과 로컬 LLM의 응답을 저장한 설정 결과를 조회한다.
- **HTTP 메서드와 경로:** `GET /api/settings/privacy`

### 요청 필드

없음.

### 응답 필드와 JSON 예시

성공: `200 OK`, `application/json`.

| 이름 | 타입 | 설명 |
| --- | --- | --- |
| `settingId` | integer 또는 null | 설정 ID. 저장 이력이 없으면 null |
| `text` | string | 현재 설정 원문. 저장 이력이 없으면 빈 문자열 |
| `rules` | object[] | 로컬 LLM이 반환해 저장된 설정 결과. 서버가 직접 생성한 규칙이 아님. 저장 이력이 없으면 빈 배열 |
| `rules[].target` | string | 구체 항목 또는 정보 종류 |
| `rules[].scope` | string | `item` 또는 `category` |
| `rules[].action` | string | `allow` 또는 `deny` |
| `updatedAt` | string 또는 null | 시간대를 포함한 ISO 8601 수정 시각. 저장 이력이 없으면 null |

```json
{
  "settingId": 1,
  "text": "나이는 공개해도 돼",
  "rules": [{"target":"나이","scope":"category","action":"allow"}],
  "updatedAt": "2026-11-07T14:00:00+09:00"
}
```

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 500 | `INTERNAL_ERROR` | 조회 실패 안내, 재시도 제공. 빈 설정으로 오인해 덮어쓰지 않도록 저장 비활성화 |

```json
{"code":"INTERNAL_ERROR","message":"공개 허용 설정을 불러오지 못했습니다."}
```

## API-10 공개 허용 설정 저장

- **기능:** 사용자가 자연어로 작성한 공개 허용 설정문을 로컬 LLM에 전달하고, 로컬 LLM이 반환한 설정 결과를 저장해 기존 설정을 교체한다. 공개 허용 대상의 판단은 로컬 LLM이 담당한다. 서버는 설정 내용을 직접 규칙으로 변환하거나 허용·금지 충돌을 판단하지 않는다. 새 설정은 다음 실행부터 적용하고 진행 중인 실행의 기준은 유지한다.
- **HTTP 메서드와 경로:** `PUT /api/settings/privacy`

### 요청 필드

본문 형식: `application/json`.

| 위치 | 이름 | 타입 | 필수 | 기본값 | 제약 |
| --- | --- | --- | --- | --- | --- |
| body | `text` | string | 예 | 없음 | 빈 문자열 허용. 공백만 있으면 빈 설정으로 처리. 최대 길이 미정 |

### 응답 필드와 JSON 예시

성공: `200 OK`, `application/json`. `rules`는 로컬 LLM이 반환한 설정 결과를 저장한 값이다. 로컬 LLM이 설정을 처리할 수 없다고 응답한 경우 `ok: false`로 반환하며 기존 설정은 바꾸지 않는다.

| 이름 | 타입 | 설명 |
| --- | --- | --- |
| `ok` | boolean | 로컬 LLM의 설정 처리 및 저장 성공 여부 |
| `rules` | object[] | 성공 시 로컬 LLM의 응답대로 저장한 설정 결과, 실패 시 기존 저장 결과 |
| `rules[].target` | string | 구체 항목 또는 정보 종류 |
| `rules[].scope` | string | `item` 또는 `category` |
| `rules[].action` | string | `allow` 또는 `deny` |
| `message` | string | 저장 결과 안내 또는 로컬 LLM 응답에 따른 수정 안내 |

```json
{
  "ok": true,
  "rules": [{"target":"나이","scope":"category","action":"allow"}],
  "message": "설정을 저장했습니다. 다음 실행부터 적용됩니다."
}
```

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 200 | `ok: false` · 오류 코드 없음 | 기존 설정·편집 내용 유지, 로컬 LLM 응답에 따른 안내 후 수정 |
| 422 | `VALIDATION_ERROR` | 요청 필드 타입·누락 확인, 입력 유지. 설정 내용의 의미 검증이 아님 |
| 503 | `LOCAL_LLM_UNAVAILABLE` | 기존 설정 유지, 로컬 LLM 연결·호출 불가 안내와 재시도 제공 |
| 500 | `INTERNAL_ERROR` | 저장 실패 안내, 설정 재조회 후 실제 저장 여부 확인 |

```json
{"ok":false,"rules":[],"message":"로컬 LLM이 공개 허용 대상을 확인하지 못했습니다. 설정문을 구체적으로 작성해 주세요."}
```

## API-11 실행 상세 조회

- **기능:** 사용자가 실행 내역을 펼칠 때 해당 실행의 프론티어 질의·동의 기록을 조회한다. API-03의 실행 요약에 포함된 `runId`로 요청한다.
- **HTTP 메서드와 경로:** `GET /api/runs/{runId}`

### 요청 필드

| 위치 | 이름 | 타입 | 필수 | 기본값 | 제약 |
| --- | --- | --- | --- | --- | --- |
| path | `runId` | integer | 예 | 없음 | 1 이상의 존재하는 실행 ID |

### 응답 필드와 JSON 예시

성공: `200 OK`, `application/json`. nullable 필드는 값이 없어도 `null`로 포함한다. 기록이 없는 배열은 `[]`다. 진행 중인 실행도 조회 시점까지 저장된 내역을 반환하며, 이후 변경은 API-06으로 받는다.

| 이름 | 타입 | 설명 |
| --- | --- | --- |
| `runId` | integer | 실행 ID |
| `chatId` | integer | 실행이 속한 채팅 ID |
| `userMessageId` | integer | 실행을 시작한 질문 메시지 ID |
| `answerMessageId` | integer 또는 null | 최종·부분 답변 메시지 ID. 답변 없이 종료됐거나 아직 저장 전이면 null |
| `status` | string | `running` 또는 `finished` |
| `endReason` | string 또는 null | `info_enough`, `call_limit`, `regen_limit`, `budget_limit`, `offline`, `stopped`, `consent_denied`. 진행 중·오류 종료는 null |
| `notice` | string 또는 null | API-03과 동일한 종료·오류 안내. 진행 중·정상 완료는 null |
| `errorCode` | string 또는 null | 오류 종료 시 코드, 그 외 null |
| `frontierQueries` | object[] | `round` 오름차순 프론티어 내역 |
| `frontierQueries[].queryId` | integer | 질의 ID |
| `frontierQueries[].round` | integer | 1부터 시작하는 질의 회차 |
| `frontierQueries[].request` | string | 실제 전송한 요청문 |
| `frontierQueries[].answer` | string 또는 null | 프론티어 답변. 수신하지 못하면 null |
| `frontierQueries[].status` | string | `ok`, `failed`, `uncertain` |
| `consents` | object[] | 요청 시각 오름차순 동의 기록 |
| `consents[].consentId` | integer | 동의 요청 ID |
| `consents[].items` | object[] | 요청 항목. 각 항목은 `{ pdId: integer, label: string, text: string, category: integer }`. category는 0 식별·1 민감·2 속성 |
| `consents[].purpose` | string | 공개 목적 |
| `consents[].benefit` | string | 예상 이점 |
| `consents[].risk` | string | 예상 불이익 |
| `consents[].result` | string 또는 null | `answered`, `cancelled`. 응답 대기 중이면 null |
| `consents[].responseText` | string 또는 null | 해석에 성공한 자연어 응답 원문. 대기·취소는 null |
| `consents[].decisions` | object[] | 항목별 `{ pdId: integer, allow: boolean }`. 대기·취소는 빈 배열 |
| `consents[].createdAt` | string | 시간대를 포함한 ISO 8601 요청 시각 |
| `consents[].respondedAt` | string 또는 null | 시간대를 포함한 ISO 8601 응답·취소 처리 시각. 대기는 null |

```json
{
  "runId": 1,
  "userMessageId": 1,
  "answerMessageId": 2,
  "status": "finished",
  "endReason": "info_enough",
  "errorCode": null,
  "frontierQueries": [
    {
      "queryId": 1,
      "round": 1,
      "request": "27세의 진로 탐색 방법을 알려줘.",
      "answer": "관심사와 직무 역량을 정리하세요.",
      "status": "ok"
    }
  ],
  "consents": [
    {
      "consentId": 1,
      "items": [
        {
          "pdId": 11,
          "label": "나이",
          "text": "27살",
          "category": 2
        }
      ],
      "purpose": "연령에 맞는 진로 정보 확인",
      "benefit": "구체적인 조언을 받을 수 있습니다.",
      "risk": "나이가 외부 모델로 전송됩니다.",
      "result": "answered",
      "responseText": "나이는 공개해도 돼",
      "decisions": [
        {
          "pdId": 11,
          "allow": true
        }
      ],
      "createdAt": "2026-11-07T14:00:05+09:00",
      "respondedAt": "2026-11-07T14:00:10+09:00"
    }
  ],
  "chatId": 1,
  "notice": null
}
```

### 오류 코드와 화면 처리

| HTTP 상태 | 오류 코드 | 화면 처리 |
| --- | --- | --- |
| 404 | `NOT_FOUND` | 실행이 없거나 삭제됨 안내. 채팅 목록·상세 갱신 |
| 422 | `VALIDATION_ERROR` | 실행 ID 형식 확인, 실행 요약 다시 조회 |
| 500 | `INTERNAL_ERROR` | 내역 조회 실패 안내와 재시도 제공. 기존 대화와 실행 요약 유지 |

```json
{"code":"NOT_FOUND","message":"실행을 찾을 수 없습니다."}
```

# DB 스키마

구현 기준은 `server/app/data/chat_manager.py`의 `SCHEMA_SQL`이다.
최신 화면 API에 맞춰 모든 ID와 참조 ID는 INTEGER로 통일한다. 시간은 UTC 오프셋을 포함한 ISO 8601 TEXT다.
사용법과 저장 책임은 [ChatManager](chat-manager.md)를 참고한다.

설정은 새 버전을 추가하고 기존 실행이 참조하는 settingId는 유지한다.
pd_items의 allowed/allowedBy는 사전 설정 상태만 저장한다. 임시 동의는 실행별 메모리에만 적용하고,
전송 당시 허용 상태는 query_items에 보존한다. 항목별 동의 결정은 JSON으로 저장한다.
오류는 status=finished, endReason=NULL, errorCode/notice로 기록한다.

스키마 버전과 마이그레이션은 관리하지 않는다. 빈 DB에서만 스키마를 초기화하고,
스키마가 바뀌면 기존 DB 파일을 수동으로 지운 뒤 새로 만든다.

실행 종료 후 채팅을 삭제할 때 runs를 먼저 삭제하고 chats를 삭제한다.
채팅 관련 하위 기록은 ON DELETE CASCADE로 제거한다. 앱 공통 privacy_settings는 유지한다.
아래 표는 실제 스키마에서 추출했다. INTEGER PRIMARY KEY는 SQLite가 ID를 자동 발급한다.

## chats

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `chatId` | INTEGER | PK / AUTOINCREMENT | — | — |
| `title` | TEXT | NOT NULL | — | '' |
| `createdAt` | TEXT | NOT NULL | — | — |
| `updatedAt` | TEXT | NOT NULL | — | — |

## messages

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `messageId` | INTEGER | PK / AUTOINCREMENT | — | — |
| `chatId` | INTEGER | NOT NULL | chats / CASCADE | — |
| `role` | TEXT | NOT NULL | — | — |
| `content` | TEXT | NOT NULL | — | — |
| `createdAt` | TEXT | NOT NULL | — | — |

## privacy_settings

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `settingId` | INTEGER | PK / AUTOINCREMENT | — | — |
| `text` | TEXT | NOT NULL | — | — |
| `rules` | TEXT | NOT NULL | — | — |
| `updatedAt` | TEXT | NOT NULL | — | — |

## pd_items

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `pdId` | INTEGER | PK / AUTOINCREMENT | — | — |
| `chatId` | INTEGER | NOT NULL | chats / CASCADE | — |
| `messageId` | INTEGER | NOT NULL | messages / CASCADE | — |
| `text` | TEXT | NOT NULL | — | — |
| `category` | INTEGER | NOT NULL | — | — |
| `allowed` | INTEGER | NOT NULL | — | 0 |
| `allowedBy` | TEXT | NOT NULL | — | 'none' |
| `createdAt` | TEXT | NOT NULL | — | — |

## runs

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `runId` | INTEGER | PK / AUTOINCREMENT | — | — |
| `chatId` | INTEGER | NOT NULL | chats / CASCADE | — |
| `userMessageId` | INTEGER | NOT NULL | messages / NO ACTION | — |
| `answerMessageId` | INTEGER | NULL 허용 | messages / NO ACTION | — |
| `settingId` | INTEGER | NULL 허용 | privacy_settings / NO ACTION | — |
| `config` | TEXT | NOT NULL | — | — |
| `status` | TEXT | NOT NULL | — | 'running' |
| `endReason` | TEXT | NULL 허용 | — | — |
| `notice` | TEXT | NULL 허용 | — | — |
| `errorCode` | TEXT | NULL 허용 | — | — |
| `frontierCalls` | INTEGER | NOT NULL | — | 0 |
| `regenerations` | INTEGER | NOT NULL | — | 0 |
| `spentCost` | REAL | NOT NULL | — | 0 |
| `memoFinal` | TEXT | NULL 허용 | — | — |
| `startedAt` | TEXT | NOT NULL | — | — |
| `endedAt` | TEXT | NULL 허용 | — | — |

## run_events

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `eventId` | INTEGER | 복합 PK / NOT NULL / 실행별 순번 | — | — |
| `runId` | INTEGER | 복합 PK / NOT NULL | runs / CASCADE | — |
| `type` | TEXT | NOT NULL | — | — |
| `payload` | TEXT | NOT NULL | — | — |
| `createdAt` | TEXT | NOT NULL | — | — |

## consent_requests

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `consentId` | INTEGER | PK / AUTOINCREMENT | — | — |
| `runId` | INTEGER | NOT NULL | runs / CASCADE | — |
| `items` | TEXT | NOT NULL | — | — |
| `purpose` | TEXT | NOT NULL | — | — |
| `benefit` | TEXT | NOT NULL | — | — |
| `risk` | TEXT | NOT NULL | — | — |
| `result` | TEXT | NULL 허용 | — | — |
| `responseText` | TEXT | NULL 허용 | — | — |
| `decisions` | TEXT | NOT NULL | — | '[]' |
| `createdAt` | TEXT | NOT NULL | — | — |
| `respondedAt` | TEXT | NULL 허용 | — | — |

## frontier_queries

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `queryId` | INTEGER | PK / AUTOINCREMENT | — | — |
| `runId` | INTEGER | NOT NULL | runs / CASCADE | — |
| `round` | INTEGER | NOT NULL | — | — |
| `request` | TEXT | NOT NULL | — | — |
| `answer` | TEXT | NULL 허용 | — | — |
| `status` | TEXT | NOT NULL | — | — |
| `contextMode` | TEXT | NULL 허용 | — | — |
| `inputTokens` | INTEGER | NULL 허용 | — | — |
| `outputTokens` | INTEGER | NULL 허용 | — | — |
| `cost` | REAL | NOT NULL | — | 0 |
| `elapsedMs` | REAL | NULL 허용 | — | — |
| `createdAt` | TEXT | NOT NULL | — | — |

## query_items

| 컬럼 | 타입 | 제약 | 참조 / 삭제 정책 | 기본값 |
| --- | --- | --- | --- | --- |
| `itemId` | INTEGER | PK / AUTOINCREMENT | — | — |
| `queryId` | INTEGER | NOT NULL | frontier_queries / CASCADE | — |
| `pdId` | INTEGER | NOT NULL | pd_items / CASCADE | — |
| `allowed` | INTEGER | NOT NULL | — | — |
| `allowedBy` | TEXT | NOT NULL | — | — |

## JSON 필드

- privacy_settings.rules: `[{target, scope: item|category, action: allow|deny}]`
- runs.config: `{callLimit, regenLimit, budgetLimit}`
- consent_requests.items: `[{pdId, label, text, category}]`의 요청 시점 복사본
- consent_requests.decisions: `[{pdId, allow}]`. 미응답·취소는 빈 배열
- run_events.payload: JSON 값. eventId 이후 로그를 순서대로 조회

## 추가 제약과 인덱스

- pd_items: `(chatId, text, category)` 유일. category는 0~2, allowed는 0/1
- consent_requests: result는 NULL/answered/cancelled. 미응답 요청은 runId마다 하나만 허용하는 부분 유일 인덱스
- runs: status는 running/finished
- frontier_queries: `(runId, round)` 유일. status는 ok/failed/uncertain
- query_items: `(queryId, pdId)` 유일, allowed는 0/1
- messages(chatId, messageId), runs(chatId, runId), run_events(runId, eventId),
  consent_requests(runId, consentId), frontier_queries(runId, round)에 조회 인덱스

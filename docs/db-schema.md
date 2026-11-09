# DB 스키마

## chats

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `chatId` | TEXT | PK | 채팅 ID |
| `title` | TEXT | — | 제목 |
| `createdAt` | TEXT | — | 생성 시각 |
| `updatedAt` | TEXT | — | 수정 시각 |

## messages

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `messageId` | TEXT | PK | 메시지 ID |
| `role` | TEXT | — | 발신 역할 |
| `content` | TEXT | — | 메시지 내용 |
| `createdAt` | TEXT | — | 생성 시각 |
| `chatId` | TEXT | FK → `chats.chatId` | 채팅 ID |

## privacy_settings

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `settingId` | INTEGER | PK | 설정 ID |
| `text` | TEXT | — | 사용자 설정 문장 |
| `rules` | TEXT | — | 설정 규칙(JSON) |
| `updatedAt` | TEXT | — | 수정 시각 |

## pd_items

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `pdId` | INTEGER | PK | 개인정보 항목 ID |
| `text` | TEXT | — | 개인정보 내용 |
| `category` | INTEGER | — | 개인정보 분류 |
| `createdAt` | TEXT | — | 생성 시각 |
| `chatId` | TEXT | FK → `chats.chatId` | 채팅 ID |
| `messageId` | TEXT | FK → `messages.messageId` | 처음 나온 메시지 ID |

## runs

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `runId` | TEXT | PK | 실행 ID |
| `config` | TEXT | — | 실행 설정 |
| `status` | TEXT | — | 실행 상태 |
| `endReason` | TEXT | — | 종료 사유 |
| `frontierCalls` | INTEGER | — | 프론티어 호출 횟수 |
| `regenerations` | INTEGER | — | 재생성 횟수 |
| `spentCost` | REAL | — | 사용 비용 |
| `memoFinal` | TEXT | — | 최종 메모 |
| `startedAt` | TEXT | — | 시작 시각 |
| `endedAt` | TEXT | — | 종료 시각 |
| `chatId` | TEXT | FK → `chats.chatId` | 채팅 ID |
| `settingId` | INTEGER | FK → `privacy_settings.settingId` | 설정 ID |
| `userMessageId` | TEXT | FK → `messages.messageId` | 사용자 메시지 ID |
| `answerMessageId` | TEXT | FK → `messages.messageId` | 답변 메시지 ID |

## run_events

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `eventId` | INTEGER | PK | 이벤트 ID |
| `type` | TEXT | — | 이벤트 종류 |
| `payload` | TEXT | — | 이벤트 내용(JSON) |
| `createdAt` | TEXT | — | 생성 시각 |
| `runId` | TEXT | FK → `runs.runId` | 실행 ID |

## consent_requests

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `consentId` | INTEGER | PK | 동의 요청 ID |
| `items` | TEXT | — | 요청 개인정보 목록(JSON) |
| `result` | TEXT | — | 응답 결과 |
| `createdAt` | TEXT | — | 생성 시각 |
| `runId` | TEXT | FK → `runs.runId` | 실행 ID |

## frontier_queries

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `queryId` | INTEGER | PK | 질의 ID |
| `round` | INTEGER | — | 질의 회차 |
| `request` | TEXT | — | 전송 요청문 |
| `answer` | TEXT | — | 프론티어 응답 |
| `status` | TEXT | — | 처리 상태 |
| `contextMode` | TEXT | — | 컨텍스트 모드 |
| `inputTokens` | INTEGER | — | 입력 토큰 수 |
| `outputTokens` | INTEGER | — | 출력 토큰 수 |
| `cost` | REAL | — | 비용 |
| `elapsedMs` | INTEGER | — | 소요 시간(ms) |
| `createdAt` | TEXT | — | 생성 시각 |
| `runId` | TEXT | FK → `runs.runId` | 실행 ID |

## query_items

| 컬럼 | 타입 | 키 / 참조 | 항목 |
| --- | --- | --- | --- |
| `itemId` | INTEGER | PK | 질의 항목 ID |
| `allowed` | INTEGER | — | 전송 당시 허용 여부 |
| `allowedBy` | TEXT | — | 전송 당시 허용 근거 |
| `queryId` | INTEGER | FK → `frontier_queries.queryId` | 질의 ID |
| `pdId` | INTEGER | FK → `pd_items.pdId` | 개인정보 항목 ID |

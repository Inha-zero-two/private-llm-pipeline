# 설계서 ↔ 코드 이름 대응표

상세 설계서(2026.10.20)는 Java식 camelCase로 이름을 적었고, 코드는 [코드 컨벤션](convention-code.md)대로 PEP 8을 따릅니다. 설계서의 이름을 코드에서 어떻게 쓰는지 여기서 찾아 주세요.

## 예시

설계서 4.2의 `PrivacySettings.parseRules()`는 코드에서 이렇게 씁니다.

```python
# 설계서: PrivacySettings.parseRules(text) → list[Rule]
# 코드:   app/privacy/privacy_settings.py

class PrivacySettings:
    def parse_rules(self, text: str) -> list[Rule]:
        ...
```

- 모듈 이름 `PrivacySettings`는 클래스 이름으로 그대로 씁니다.
- 파일 이름은 모듈 이름을 snake_case로 바꾼 `privacy_settings.py`입니다.
- 함수 이름 `parseRules`는 snake_case로 바꾼 `parse_rules`입니다.

## 바꾸는 규칙

| 설계서 | 코드 | 예시 |
| --- | --- | --- |
| 모듈 | 클래스 이름은 그대로, 파일 이름은 snake_case | `LeakGuard` → `class LeakGuard`, `leak_guard.py` |
| 소문자로 시작하는 모듈 | 클래스 이름을 PascalCase로 | `memoManager` → `class MemoManager`, `memo_manager.py` |
| 약어가 들어간 모듈 | 클래스 이름은 약어를 대문자 그대로, 파일 이름은 약어를 한 단어로 | `PDDetector` → `class PDDetector`, `pd_detector.py` |
| 함수 · 변수 · 매개변수 | snake_case | `listNeeds()` → `list_needs()`, `stopFlag` → `stop_flag` |
| 상수 | 그대로 | `MAX_REGEN`, `CATEGORY_LABEL` |
| 데이터 구조 · 열거형 이름 | 그대로 | `PDTable`, `ConsentRequest`, `EndReason` |
| 데이터 구조의 필드 | snake_case | `allowedBy` → `allowed_by`, `hitIdx` → `hit_idx` |
| 열거형 항목 | 대문자 + `_` (값은 설계서 그대로) | `EndReason.INFO_ENOUGH = "info_enough"` |
| Python 내장 이름과 겹치는 이름 | 뒤에 `_` | `Input` → `input_`, `type` → `type_` |

> 화면 ↔ 서버 API · SSE 필드 이름과 DB 컬럼 이름은 이 문서에서 다루지 않습니다.

## 여러 모듈에 나오는 이름

| 설계서 | 코드 |
| --- | --- |
| `runId` | `run_id` |
| `chatId` | `chat_id` |
| `messageId` | `message_id` |
| `settingId` | `setting_id` |
| `pdTable` (Main에선 `PDTable`) | `pd_table` |
| `promptIdx` | `prompt_idx` |
| `requestText` | `request_text` |
| `contextMode` | `context_mode` |

## 데이터 구조 · 열거형

이름은 그대로 쓰고, `app/models/`에 둡니다. 위치 기준은 [코드 컨벤션](convention-code.md#각-모듈에서-주고받는-데이터-형식)을 따릅니다.

`PDItem`, `PDTable`, `PDId`, `Rule`, `SaveResult`, `ConsentRequest`, `ConsentResult`, `GuardResult`, `Memo`, `ChatSummary`, `ChatDetail`, `EndReason`, `PromptIdx`

| 설계서 `EndReason` 항목 | 코드 |
| --- | --- |
| `info_enough` | `EndReason.INFO_ENOUGH` |
| `call_limit` | `EndReason.CALL_LIMIT` |
| `regen_limit` | `EndReason.REGEN_LIMIT` |
| `budget_limit` | `EndReason.BUDGET_LIMIT` |
| `offline` | `EndReason.OFFLINE` |
| `stopped` | `EndReason.STOPPED` |
| `consent_denied` | `EndReason.CONSENT_DENIED` |

`PromptIdx` 항목(`DETECT` 등)은 설계서에서 이미 대문자이므로 그대로 씁니다.

## 모듈별 대응표

함수 · 변수에서 이름이 그대로인 것은 화살표 없이 적었습니다. 상수는 모두 그대로입니다.

### 2.4 Main

| 설계서 | 코드 |
| --- | --- |
| 파일 | `app/main.py` |
| 함수 | `runPipeline()` → `run_pipeline()`, `checkEnd()` → `check_end()`, `stop()` |
| 변수 | `callLimit` → `call_limit`, `regenLimit` → `regen_limit`, `budgetLimit` → `budget_limit`, `spentCost` → `spent_cost`, `frontierCalls` → `frontier_calls`, `endReason` → `end_reason`, `consentReq` → `consent_req`, `consentResult` → `consent_result`, `guardResult` → `guard_result` |

### 3. 파이프라인 핵심 (`app/pipeline/`)

| 설계서 모듈 | 파일 · 클래스 | 함수 | 변수 |
| --- | --- | --- | --- |
| 3.1 `Planner` | `planner.py` · `Planner` | `listNeeds()` → `list_needs()`, `judge()` | |
| 3.2 `RequestWriter` | `request_writer.py` · `RequestWriter` | `write()`, `rewrite()` | |
| 3.3 `LeakGuard` | `leak_guard.py` · `LeakGuard` | `detect()`, `buildGuardInput()` → `build_guard_input()`, `toGuardResult()` → `to_guard_result()` | `targetItems` → `target_items`, `guardInput` → `guard_input` |
| 3.4 `AnswerGenerator` | `answer_generator.py` · `AnswerGenerator` | `generate()`, `notifyLimited()` → `notify_limited()` | |

### 4. 개인정보 통제 (`app/privacy/`)

| 설계서 모듈 | 파일 · 클래스 | 함수 | 변수 |
| --- | --- | --- | --- |
| 4.1 `PDDetector` | `pd_detector.py` · `PDDetector` | `detect()` | `foundItems` → `found_items` |
| 4.2 `PrivacySettings` | `privacy_settings.py` · `PrivacySettings` | `save()`, `parseRules()` → `parse_rules()`, `applyToTable()` → `apply_to_table()`, `isAllowed()` → `is_allowed()`, `loadRules()` → `load_rules()`, `load()` | `matchInput` → `match_input`, `itemRules` → `item_rules`, `categoryRules` → `category_rules` |
| 4.3 `ConsentManager` | `consent_manager.py` · `ConsentManager` | `buildMessage()` → `build_message()`, `request()`, `receive()`, `apply()`, `release()`, `cancel()` | `consentItems` → `consent_items`, `consentInput` → `consent_input` |

### 5. 실행 환경 (`app/runtime/`)

| 설계서 모듈 | 파일 · 클래스 | 함수 | 변수 |
| --- | --- | --- | --- |
| 5.1 `LocalLLMRunner` | `local_llm_runner.py` · `LocalLLMRunner` | `request()`, `manualRequest()` → `manual_request()`, `reGen()` → `regen()`, `stop()` → `stop(run_id)`, `modelLoad()` → `model_load()` | `Input` → `input_`, `stopFlag` → `stop_flag` |
| 5.2 `LocalResParser` | `local_res_parser.py` · 상태 없는 함수 | `parser()` | `rawOutput` → `raw_output` |
| 5.3 `FrontierClient` | `frontier_client.py` · `FrontierClient` | `checkConnection()` → `check_connection()`, `send()`, `decideContext()` → `decide_context()` | `lastRequest` → `last_request`, `frontierSysPromIdx` → `frontier_sys_prom_idx`, `contextRecognizeSysPromIdx` → `context_recognize_sys_prom_idx` |
| 5.4 `ContextManager` | `context_manager.py` · `ContextManager` | `getId()` → `get_id()`, `newId()` → `new_id()`, `getContext()` → `get_context()`, `append()`, `getContextLength()` → `get_context_length()`, `compactContext()` → `compact_context()` | |

`reGen()`의 파이썬 함수 이름은 `regen`. 설계서의 요청문 재작성 횟수 변수 `regenLimit`과 같은 단어 표기.

5.1·5.2·5.4 구현 기준:

- `use_context`: 요청에 이전 대화를 사용할지 선택하는 값. 기본 False. True일 때만 채팅·작업별 이전 대화를 조회하고 최종 입력·답변 저장. 생성자의 context_tasks 설정은 사용하지 않음
- `regen()`: 같은 입력과 시스템 프롬프트로 답변 재생성. 설계서의 이전 출력·오류 안내 추가는 생략. 이전 대화 사용 시 해당 내용을 조회만 하고 저장은 request에서 최종 성공 시 한 번 처리
- `run_id`: request마다 전달할 작업 실행 ID. Runner 생성자에서는 받지 않음. manual_request와 regen을 중지하려면 stop에 전달할 ID와 같은 run_id 지정. 이전 대화를 사용하는 regen에도 필요
- `get_context_manager(run_id)`: 해당 실행의 ContextManager 조회 또는 생성. 프로그램에서 모델 하나를 공유하고 실행별 대화 내용은 분리. 직접 생성할 때는 `ContextManager(runner, run_id)` 사용
- `_generate()`: manual_request, request, regen에서 공유하는 응답 생성 함수. request 안에서 다른 공개 요청 함수를 호출하는 방식은 사용하지 않음
- `stop(run_id)`: 현재 요청의 실행 ID와 일치할 때만 중지 신호 전달 후 None 반환. 생성·파싱·재생성·대화 저장까지 같은 실행으로 관리하고 요청 종료 시 실행 ID 정리. 다른 실행 또는 진행 중인 요청이 없는 경우 생략
- 대화 압축: 기존 대화와 새 내용을 합쳐 요약 입력 예산 이상이면 기존 대화를 요약. 예산은 32,768에서 출력 5,000, 채팅 형식 여유분 1,024 및 요약 시스템 프롬프트 토큰 수를 제외한 값. 목표와 출력 상한은 5,000토큰. 중지 신호가 와도 요약 완료까지 처리
- 일반 요청 로그: 실행 ID, 작업 종류, 프롬프트 버전, 소요 시간, 형식 검사 결과, 새 입력 및 생성된 답변. 이전 대화 전체는 기록 대상에서 제외
- 프롬프트 조회: PromptManager에서 `(프롬프트 본문, 버전)`을 반환할 예정. 미구현 조회·저장 코드는 주석 처리
- 압축 결과 저장: 별도 컨텍스트 테이블 없이 요약 반영 후 `ChatManager.save_message(chat_id, "context_compacted", result)` 한 번 호출 예정. messages의 role은 기록 종류, content는 요약 결과, createdAt은 ChatManager에서 기록할 저장 시각. 일반 대화의 데이터베이스 저장·복원은 생략
- `parser()`: 클래스와 check 함수 없이 출력 형식 검사 후 결과 반환. 형식 오류는 OutputFormatError로 전달하고 Runner의 파서 호출에서 이 예외만 처리. 공유 작업 종류는 `app/models/enums.py`, 반환 자료형은 `app/models/llm_results.py`에 정의
- `MAX_ATTEMPTS`: 최초 요청을 포함한 최대 생성 시도 횟수 3회. 출력 형식 오류, 연결·수신 오류, 타임아웃, 무응답, length 종료 및 생성 실패를 합산. 빈 답변이나 공백뿐인 답변도 포함하며 중지 요청은 제외. manual_request와 regen은 length 종료만 재시도
- 3회 모두 실패하면 설계 5.1의 실패 반환 방식에 따라 `(None, False, 총 입력 토큰 수, 총 출력 토큰 수, 종료 사유)` 반환. 마지막 실패가 length 종료이면 length_failed, 나머지는 parse_failed. 입력 예산 초과는 context_full로 구분

### 6. 데이터 관리 (`app/data/`)

| 설계서 모듈 | 파일 · 클래스 | 함수 | 변수 |
| --- | --- | --- | --- |
| 6.1 `PromptManager` | `prompt_manager.py` · `PromptManager` | `getPrompt()` → `get_prompt()`, `loadPrompts()` → `load_prompts()` | |
| 6.2 `memoManager` | `memo_manager.py` · `MemoManager` | `start()`, `setNeeds()` → `set_needs()`, `addAnswer()` → `add_answer()`, `compactAnswer()` → `compact_answer()`, `readAll()` → `read_all()`, `clear()` | |
| 6.3 `chatManager` | `chat_manager.py` · `ChatManager` | `createChat()` → `create_chat()`, `listChats()` → `list_chats()`, `getChat()` → `get_chat()`, `deleteChat()` → `delete_chat()`, `saveMessage()` → `save_message()`, `startRun()` → `start_run()`, `finishRun()` → `finish_run()`, `logEvent()` → `log_event()`, `setPDItems()` → `set_pd_items()`, `getPDTable()` → `get_pd_table()`, `updatePDAllowed()` → `update_pd_allowed()`, `saveConsent()` → `save_consent()`, `updateConsent()` → `update_consent()`, `saveFrontierQuery()` → `save_frontier_query()`, `saveSetting()` → `save_setting()`, `loadSetting()` → `load_setting()`, `loadSettings()` → `load_settings()` | `memoFinal` → `memo_final`, `type` → `type_` |

설계서에 새 이름이 추가되거나 바뀌면 이 표도 같이 고쳐 주세요.

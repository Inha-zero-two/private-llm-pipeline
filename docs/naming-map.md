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
| 5.1 `LocalLLMRunner` | `local_llm_runner.py` · `LocalLLMRunner` | `request()`, `manualRequest()` → `manual_request()`, `reGen()` → `regen()`, `stop()`, `modelLoad()` → `model_load()` | `Input` → `input_`, `stopFlag` → `stop_flag` |
| 5.2 `LocalResParser` | `local_res_parser.py` · `LocalResParser` | `check()`, `parser()` | `rawOutput` → `raw_output` |
| 5.3 `FrontierClient` | `frontier_client.py` · `FrontierClient` | `checkConnection()` → `check_connection()`, `send()`, `decideContext()` → `decide_context()` | `lastRequest` → `last_request`, `frontierSysPromIdx` → `frontier_sys_prom_idx`, `contextRecognizeSysPromIdx` → `context_recognize_sys_prom_idx` |
| 5.4 `ContextManager` | `context_manager.py` · `ContextManager` | `getId()` → `get_id()`, `newId()` → `new_id()`, `getContext()` → `get_context()`, `append()`, `getContextLength()` → `get_context_length()`, `compactContext()` → `compact_context()` | |

`reGen()`은 `regenLimit`, `MAX_REGEN`과 맞춰 `regen`을 한 단어로 봅니다.

### 6. 데이터 관리 (`app/data/`)

| 설계서 모듈 | 파일 · 클래스 | 함수 | 변수 |
| --- | --- | --- | --- |
| 6.1 `PromptManager` | `prompt_manager.py` · `PromptManager` | `getPrompt()` → `get_prompt()`, `loadPrompts()` → `load_prompts()` | |
| 6.2 `memoManager` | `memo_manager.py` · `MemoManager` | `start()`, `setNeeds()` → `set_needs()`, `addAnswer()` → `add_answer()`, `compactAnswer()` → `compact_answer()`, `readAll()` → `read_all()`, `clear()` | |
| 6.3 `chatManager` | `chat_manager.py` · `ChatManager` | `createChat()` → `create_chat()`, `listChats()` → `list_chats()`, `getChat()` → `get_chat()`, `deleteChat()` → `delete_chat()`, `saveMessage()` → `save_message()`, `startRun()` → `start_run()`, `finishRun()` → `finish_run()`, `logEvent()` → `log_event()`, `setPDItems()` → `set_pd_items()`, `getPDTable()` → `get_pd_table()`, `updatePDAllowed()` → `update_pd_allowed()`, `saveConsent()` → `save_consent()`, `updateConsent()` → `update_consent()`, `saveFrontierQuery()` → `save_frontier_query()`, `saveSetting()` → `save_setting()`, `loadSetting()` → `load_setting()`, `loadSettings()` → `load_settings()` | `memoFinal` → `memo_final`, `type` → `type_` |

설계서에 새 이름이 추가되거나 바뀌면 이 표도 같이 고쳐 주세요.

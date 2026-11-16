# 3.1 Planner

**파일:** [planner.py](../server/app/pipeline/planner.py)

## 구현 내역

- **`Plan` 모델 정의:** `enough`, `need_consent`(`needConsent`), `consent_items`(`consentItems`), `need` 필드 구성
- **`list_needs(question, run_id="", chat_id=None, *, needs=None, ok=True) -> list[str]` 구현:** 질문을 해결하는 데 필요한 세부 정보 목록 검증 및 반환 (`needs`, `ok`로 분류 결과를 직접 전달할 수도 있음)
- **`judge(question, memo, pd_table, run_id="", chat_id=None, needs=None, *, raw_plan=None, ok=True) -> Plan` 구현:** 질문·메모·개인정보 테이블로 모델 입력 문자열을 구성하고, 판단 결과에 설계서 보정 규칙 4가지를 적용해 `Plan` 반환 (`raw_plan`, `ok`로 판단 결과를 직접 전달할 수도 있음). `needs`(list_needs로 정해 메모에 등록한 목록)에 0부터 번호를 붙여 입력에 넣고, 모델이 고른 번호(`needIdx`)를 그 목록의 문자열로 바꿔 `Plan.need`로 반환

## 함수별 처리 흐름

| 함수 | 단계 | 처리 내용 | 실패 · 예외 / 보정 처리 |
| --- | :---: | --- | --- |
| `list_needs()` | 1 | 입력 `question`이 비어 있는지 확인 | 비어 있거나 문자열이 아니면 `ValueError` |
| | 2 | 로컬 모델(`NEEDS`) 호출 결과 검증 후 문자열 목록(`list[str]`) 반환 | `ok=False`이거나 빈 항목이 섞여 있으면 `RuntimeError` |
| `judge()` | 1 | `question`, `memo`, `pd_table` 자료형 확인 | `question`이 비었으면 `ValueError`, `memo`·`pd_table` 타입 불일치 시 `TypeError` |
| | 2 | `pd_table`에서 미허용·미거부 ID만 동의 후보로 추리고 `[질문]` · `[필요 정보]`(needs에 0부터 번호) · `[메모]` · `[개인정보]` 입력 문자열 생성 | `pd_table` 항목에 필수 필드가 없으면 `RuntimeError` |
| | 3 | 로컬 모델(`JUDGE`) 호출 결과의 `JudgeResult` 스키마 검증 (`app/models/llm_results.py`) | `ok=False`이거나 스키마와 맞지 않으면 `RuntimeError` |
| | 3-1 | `needIdx` 번호를 `needs` 문자열로 변환 (번호 순서 유지, 중복 제거). `enough=true`면 번호를 쓰지 않음 | 목록 범위 밖 번호는 형식 오류로 보고 최대 `NEED_RETRIES`(2)번 다시 요청, 그래도 범위 밖이면 `RuntimeError`. `raw_plan`으로 직접 받은 결과는 다시 요청하지 않음 |
| | 4 | 보정 후 최종 `Plan` 반환 | ① `need`가 비면 `enough=True` 보정<br>② `enough=True`면 `need_consent=False`, `consent_items=[]`, `need=[]` 고정<br>③ 이미 허용·거부된 ID는 `consent_items`에서 제외<br>④ 남은 `consent_items`가 없으면 `need_consent=False` 보정 |

## 설계서 대비 변경 사항

- **need 이름 규격:** 모델이 판단 단계에서 need 이름을 바꿔 쓰면(`이직을 고려하는 이유` → `이직을 고려하는 구체적인 이유`) `MemoManager.add_answer`가 등록되지 않은 이름으로 거부해 실행이 멈췄습니다. 그래서 모델이 이름 대신 번호로 고르게 바꿨습니다. Main이 `needs`를 넘기면 judge가 `[필요 정보]`에 0부터 번호를 붙여 넣고(JUDGE 프롬프트 0.3), 모델 출력의 `needIdx`를 등록된 이름으로 바꿔 반환합니다. 모델 출력 형식은 `JudgeResult`(`enough`, `needConsent`, `consentItems`, `needIdx`)이고, `Plan`은 judge의 반환 형식으로 그대로 씁니다.

- **모듈 호출:** `LocalLLMRunner.request()` 호출을 연결했습니다. 프롬프트는 LocalLLMRunner가 PromptManager에서 읽습니다. 메모 저장·조회는 Main에서 호출하며, `ChatManager.log_event()` 호출은 주석 상태입니다.
- **반환 객체 호환:** Pydantic `from_attributes=True` 및 `populate_by_name=True` 설정으로 `LocalResParser`가 반환하는 `Plan` 객체와 `dict`를 모두 수용

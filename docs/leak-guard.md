# LeakGuard 사용과 처리 흐름

**대상:** [leak_guard.py](../server/app/pipeline/leak_guard.py)의 `LeakGuard`. 설계 3.3의 요청문 누출 검사.

**객체 수:** 프로그램당 1개. 생성자에 공유 `LocalLLMRunner`를 넘김. 상태를 저장하지 않음.

**역할:** RequestWriter가 쓴 요청문에 허용되지 않은 개인정보가 드러났는지 로컬 모델이 판단. `passed=True`인 요청문만 Main이 FrontierClient로 보냄.

## 함수

| 함수 | 입력 | 반환 | 내용 |
| --- | --- | --- | --- |
| `detect()` | `run_id`, `request_text`, `pd_table`, `chat_id=None` | `GuardResult` | 비허용 항목을 골라 모델로 검사 |
| `build_guard_input()` | `request_text`, `target_items` | `str` | 금지 항목에 0부터 번호·종류를 붙인 목록 + 요청문 |
| `to_guard_result()` | `hit_idx`, `target_items` | `GuardResult` | 모델이 돌려준 번호를 PDId로 변환 |

`GuardResult`(`passed: bool`, `hits: list[int]`)는 Main과 함께 쓰므로 [llm_results.py](../server/app/models/llm_results.py)에 둠.

## 처리 흐름

```mermaid
flowchart TD
    A["detect(run_id, request_text, pd_table)"] --> B{"비허용 항목이 있나"}
    B -->|없음| P["passed=True, hits=[] (모델 호출 없음)"]
    B -->|있음| C["build_guard_input → LocalLLMRunner.request(GUARD), 이전 대화 없이"]
    C -->|검사 장애·형식 실패| F["오류 전파, 외부 전송 금지"]
    C -->|"{hits: [번호]}"| D["to_guard_result"]
    D -->|빈 목록| P2["passed=True"]
    D -->|번호 → PDId| H["passed=False, hits=[PDId...]"]
    D -->|번호가 모두 범위 밖| F
```

모델 입력 예:

```text
[금지 목록]
0.[속성정보] 25살
1.[식별정보] 김민수

[요청문]
...
```

- 검사 대상은 `allowed=False`인 항목. 이 순서가 모델에 보이는 번호
- `GUARD` 출력 형식(`{"hits": [번호]}`)은 LocalResParser가 검사하고, 형식 오류 재시도(최대 3회)는 Runner가 처리
- 번호는 중복 제거한다. 범위 밖 번호·잘못된 번호·누락된 PDId는 MODEL_RESPONSE_INVALID / GUARD로 처리한다.
- 모델 연결·시간 초과·출력 형식 실패를 실제 누출 검출과 구분한다. [오류 계약](errors.md)을 따른다.
- 잘못된 호출(빈 요청문, 표 형식 오류)은 예외

## Main 연결

Main의 `_write_safe_request()`는 정상 검사 결과와 검사 장애를 구분한다.

| 결과 | Main 동작 |
| --- | --- |
| `passed=True` | 프론티어로 전송 |
| `passed=False`, hits 있음 | `RequestWriter.rewrite()`로 고친 뒤 다시 검사 |
| 검사 자체 실패 또는 `passed=False`, hits 없음 | 해당 오류를 전파하고 전송하지 않음. regen_limit으로 오분류하지 않음 |

- 동작 로그(`log_event(run_id, "guard", ...)`)는 ChatManager 구현 후 연결. 지금은 주석으로 위치만 표시

## 사용법

```python
from app.pipeline.leak_guard import LeakGuard

guard = LeakGuard(runner)
result = guard.detect(run_id, request_text, pd_table)
if result.passed:
    ...  # 전송
else:
    ...  # result.hits로 재작성 (비어 있으면 실패 처리)
```
